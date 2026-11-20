"""Recorded answers in place of the hosted vendor, for chosen staging accounts.

Four days of staging acceptance walks made 146 real hosted renders (USD 22 of
vendor balance, 2026-11-16 to 19) while what they measured was our own side:
admission, the slot turn, download, checks, storage (#1301). With this, the
jobs of the listed staging accounts get the vendor's answers from a recording.
Everything of ours runs as it does for a paid render; nothing reaches the
vendor and the recorded cost is zero.

The idea is VCR.py's and WireMock's playback: a recording stands in for the
remote. A cassette matches a request exactly; here a render is matched by its
shape (resolution, aspect, duration), because the prompt and the pictures are
new on every run. `tests/test_hosted_e2e` keeps its VCR cassette for the
adapter's contract with the real answers.

Fences, all required:
- the staging deployment target and origin (as Stripe test mode);
- a host-installed `runtime-secrets/everyframe-replay.json`;
- the job's owner is listed in it;
- a replayed render's id starts with `rpl_`, and such an id is only ever
  answered here.

The file: `{"version": 1, "owners": ["qa-credit"], "render_seconds": 60,
"attachment_seconds": 5, "videos": {"480p|16:9|5": "480p-16x9-5.mp4"}}`.
The videos are in `<data>/everyframe-replay/`: finished takes we already paid
for. A shape with no recording is refused like a vendor refusal.
"""

import json
from pathlib import Path
import re
import secrets
import time

import httpx

from ..protected_config import read_protected_json

FILENAME = "everyframe-replay.json"
DIRECTORY = "everyframe-replay"
PREFIX = "rpl_"
STAGING_ORIGIN = "https://staging.inkseq.com"
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_KEY = re.compile(r"[A-Za-z0-9.]{1,16}\|[0-9]{1,2}:[0-9]{1,2}\|[0-9]{1,3}")
_ID = re.compile(r"rpl_([0-9a-z]{1,12})_([0-9]{1,3})_[A-Za-z0-9]{1,24}")


class _Invalid(Exception):
    pass


def _validated(value):
    seconds = (value.get("render_seconds"), value.get("attachment_seconds"))
    if (
        type(value) is not dict
        or set(value)
        != {"version", "owners", "render_seconds", "attachment_seconds", "videos"}
        or type(value["version"]) is not int
        or value["version"] != 1
        or type(value["owners"]) is not list
        or not 1 <= len(value["owners"]) <= 8
        or any(type(o) is not str or not _NAME.fullmatch(o) for o in value["owners"])
        or any(type(s) not in (int, float) or not 0 <= s <= 600 for s in seconds)
        or seconds[1] > 60  # Slept on the worker's thread.
        or type(value["videos"]) is not dict
        or not 1 <= len(value["videos"]) <= 32
        or any(
            type(key) is not str
            or not _KEY.fullmatch(key)
            or type(name) is not str
            or not _NAME.fullmatch(name)
            for key, name in value["videos"].items()
        )
    ):
        raise _Invalid()
    return value


def _config(settings):
    """The validated file, or None: anything missing or odd is None."""
    if (
        getattr(settings, "deployment_target", None) != "staging"
        or getattr(settings, "public_origin", None) != STAGING_ORIGIN
    ):
        return None
    try:
        path = Path(settings.data_dir) / "runtime-secrets" / FILENAME
        if not path.is_file():
            return None
        return _validated(read_protected_json(path, _Invalid))
    except Exception:
        print("everyframe_replay_config_invalid", flush=True)
        return None


_state = {"read_at": None, "replay": None}


def current(settings, *, clock=time.monotonic):
    """The Replay in force, or None; the host file is read at most every 30 s.

    One answer for routing and for every hosted slot of the process: an
    account that is routed to the hosted lane because it is replayed is
    replayed by the slot that takes its job, and neither needs a restart to
    see the file come or go.
    """
    now = clock()
    if _state["read_at"] is None or not 0 <= now - _state["read_at"] < 30:
        config, replay = _config(settings), _state["replay"]
        if config is None:
            replay = None
        elif replay is None or replay.config != config:
            replay = Replay(config, Path(settings.data_dir) / DIRECTORY)
            print("everyframe_replay_configured", flush=True)
        _state.update(read_at=now, replay=replay)
    return _state["replay"]


def owners(settings, **options):
    """Accounts whose jobs are replayed: they spend no vendor balance."""
    replay = current(settings, **options)
    return replay.owners if replay else frozenset()


class Replay:
    def __init__(self, config, directory, *, clock=time.time, sleep=time.sleep):
        self.config, self.owners = config, frozenset(config["owners"])
        self.render_seconds = config["render_seconds"]
        self.attachment_seconds = config["attachment_seconds"]
        self.videos = dict(sorted(config["videos"].items()))
        self.directory, self.clock, self.sleep = Path(directory), clock, sleep
        self.transport = httpx.MockTransport(self.handle)

    def takes(self, job):
        return job.get("owner_id") in self.owners

    def _render(self, identity):
        match = _ID.fullmatch(identity)
        if not match or int(match.group(2)) >= len(self.videos):
            return None
        created, name = (
            int(match.group(1), 36),
            list(self.videos.values())[int(match.group(2))],
        )
        return created, self.directory / name

    def handle(self, request):
        path, method = request.url.path.removeprefix("/v1"), request.method
        if (method, path) == ("POST", "/render/attachment"):
            self.sleep(self.attachment_seconds)  # The upload we do not make.
            identity = PREFIX + "att_" + secrets.token_hex(8)
            return httpx.Response(201, json={"attachment": {"id": identity}})
        if (method, path) == ("POST", "/render"):
            try:
                body = json.loads(request.content)
                key = "%s|%s|%s" % (
                    body["resolution"],
                    body["aspect"],
                    body["duration"],
                )
                index = list(self.videos).index(key)
            except (ValueError, KeyError, TypeError):
                # No recording of this shape: refused, as the vendor refuses.
                return httpx.Response(422, json={"code": "replay_shape_not_recorded"})
            identity = "%s%s_%d_%s" % (
                PREFIX,
                _base36(int(self.clock())),
                index,
                secrets.token_hex(6),
            )
            return httpx.Response(
                201, json={"render": {"id": identity, "status": "queued"}}
            )
        parts = path.strip("/").split("/")
        if method == "GET" and parts[:1] == ["render"] and len(parts) in (2, 3):
            found = self._render(parts[1])
            if found is None:
                return httpx.Response(404, json={"code": "not_found"})
            created, video = found
            done = self.clock() - created >= self.render_seconds
            if len(parts) == 2:
                render = {"id": parts[1], "status": "running", "costUsd": 0}
                if done:
                    render.update(
                        status="succeeded", videoFullUrl="/render/%s/video" % parts[1]
                    )
                return httpx.Response(200, json={"render": render})
            if parts[2] == "video" and done and video.is_file():
                return httpx.Response(200, content=_chunks(video))
        return httpx.Response(404, json={"code": "not_found"})


def _base36(number):
    digits, out = "0123456789abcdefghijklmnopqrstuvwxyz", ""
    while number:
        number, rest = divmod(number, 36)
        out = digits[rest] + out
    return out or "0"


def _chunks(path, size=16 * 1024):
    # The size the vendor's TLS records arrive in, so the download loop sees
    # the same number of chunks.
    with path.open("rb") as handle:
        while chunk := handle.read(size):
            yield chunk
