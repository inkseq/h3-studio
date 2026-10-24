"""Inkseq API client for one linked Telegram user (#531).

Every call uses that person's connection token, so stories, credits and limits are theirs.
The bot never plans, writes cards or renders by itself: it only calls the same story-chat,
preflight, batch and actions endpoints the website uses.
"""

from __future__ import annotations

import time

import httpx


class StudioError(Exception):
    def __init__(self, status, code, detail=None):
        self.status, self.code, self.detail = status, code, detail or {}
        super().__init__(f"{status} {code}")


class StudioClient:
    def __init__(self, origin, token, *, transport=None, timeout=150):
        if not origin.startswith("https://") and not origin.startswith("http://127."):
            raise ValueError("origin must be https")
        self._client = httpx.Client(
            base_url=origin,
            transport=transport,
            timeout=httpx.Timeout(timeout, connect=15),
            follow_redirects=False,
            headers={"Authorization": "Bearer " + token},
        )

    def __repr__(self):
        return "StudioClient(<token hidden>)"

    def request(self, method, path, body=None, key=None):
        headers = {"Idempotency-Key": key} if key else None
        try:
            response = self._client.request(method, path, json=body, headers=headers)
        except httpx.HTTPError as error:
            raise StudioError(0, "network_" + type(error).__name__) from None
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail")
            except ValueError:
                detail = None
            code = (
                detail.get("code")
                if isinstance(detail, dict)
                else detail
                if isinstance(detail, str)
                else "http_" + str(response.status_code)
            )
            raise StudioError(response.status_code, str(code)[:120], detail)
        return response.json() if response.content else {}

    # Story chat: the same conversation the website's Quick Create shows.
    def chats(self, limit=10):
        return self.request("GET", f"/v1/story-chats?limit={limit}")["story_chats"]

    def create_chat(self, language):
        return self.request("POST", "/v1/story-chats", {"language": language})

    def chat(self, project_id):
        return self.request("GET", f"/v1/story-chats/{project_id}")

    def turn(self, project_id, turn_id, text):
        return self.request(
            "POST",
            f"/v1/story-chats/{project_id}/turns",
            {"turn_id": turn_id, "text": text},
        )

    # Generation: preflight (quote) → confirmed batch, exactly like Works.
    def preflight(self, project_id, expected_version, shot_ids, key, copies=1):
        return self.request(
            "POST",
            f"/v1/projects/{project_id}/storyboard/preflights",
            {
                "expected_version": expected_version,
                "shot_ids": shot_ids,
                "copies": copies,
            },
            key=key,
        )

    def preflight_status(self, project_id, preflight_id, *, wait=60, sleep=time.sleep):
        deadline = time.monotonic() + wait
        while True:
            value = self.request(
                "GET", f"/v1/projects/{project_id}/storyboard/preflights/{preflight_id}"
            )
            if value.get("status") != "pending" or time.monotonic() > deadline:
                return value
            sleep(2)

    def submit(self, project_id, plan_ids, key):
        return self.request(
            "POST",
            "/v1/batches",
            {"client_project_id": project_id, "plan_ids": plan_ids},
            key=key,
        )

    def versions(self, project_id, shot_id):
        return self.request(
            "GET", f"/v1/projects/{project_id}/shots/{shot_id}/versions"
        )

    def choose(
        self, project_id, shot_id, artifact_id, expected_version, clear_segments=False
    ):
        actions = [
            {"op": "storyboard.select", "shot_id": shot_id, "artifact_id": artifact_id}
        ]
        if clear_segments:
            # Kept parts belong to the chosen take (#512): clear them in the same edit.
            actions.insert(
                0, {"op": "shot.segments", "shot_id": shot_id, "segments": []}
            )
        return self.request(
            "POST",
            f"/v1/projects/{project_id}/actions",
            {"expected_version": expected_version, "actions": actions},
        )

    def download(self, artifact_id, dest, rendition=None):
        path = f"/v1/artifacts/{artifact_id}/content"
        if rendition:
            path += "?rendition=" + rendition
        try:
            with self._client.stream("GET", path) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location", "")
                    if not location.startswith("https://"):
                        raise StudioError(response.status_code, "download_redirect")
                    with httpx.stream("GET", location, timeout=120) as signed:
                        signed.raise_for_status()
                        _write(signed, dest)
                    return dest
                if response.status_code >= 400:
                    raise StudioError(response.status_code, "download_failed")
                _write(response, dest)
        except httpx.HTTPError as error:
            raise StudioError(0, "network_" + type(error).__name__) from None
        return dest


def _write(response, dest, limit=50 * 1024 * 1024):
    size = 0
    with open(dest, "wb") as handle:
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > limit:
                raise StudioError(413, "download_too_large")
            handle.write(chunk)
