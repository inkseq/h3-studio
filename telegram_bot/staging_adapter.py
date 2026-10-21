"""Staging adapter: the same chat flow, saved as a real story on staging (#382).

Creates the story, uploads the photos as story materials, writes chapter /
scene / shot cards with their generation settings, and asks the server for an
unpaid storyboard preflight (real cost and blockers). It never submits paid
generation: `generate` stops with GenerationHeld until an owner-approved GPU
window and a reviewed real adapter exist. Staging and its QA account only.
Pictures shown in the chat are still rendered locally from the same photos.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import logging
import time

import httpx

from .adapter import Estimate
from .bot import GenerationHeld
from .demo_adapter import DemoAdapter
from .llm_adapter import LlmAdapter

STAGING = "https://staging.inkseq.com"
log = logging.getLogger("telegram_bot")


class StudioError(Exception):
    def __init__(self, step, status, code=""):
        self.step, self.status, self.code = step, status, code
        super().__init__(f"{step}: HTTP {status} {code}".strip())


class StudioClient:
    """Either a staging QA cookie session or a linked owner's PAT (Bearer).

    The password or key stays in memory and is never logged or repr'd.
    """

    def __init__(self, base_url, username=None, password=None, *, token=None,
                 transport=None):  # fmt: skip
        if base_url != STAGING:
            raise ValueError("staging only")
        if not token and not (username and password):
            raise ValueError("a QA login or a linked key is required")
        self.base_url = base_url
        self._username, self._password = username, password
        headers = {"Origin": base_url}
        if token:
            headers["Authorization"] = "Bearer " + token
        self._token = bool(token)
        self._client = httpx.Client(
            base_url=base_url,
            headers=headers,
            timeout=httpx.Timeout(60, connect=15),
            follow_redirects=False,
            transport=transport,
        )
        self._signed_in = self._token

    def __repr__(self):
        return f"StudioClient({self.base_url}, <session hidden>)"

    def _login(self):
        response = self._client.post(
            "/api/auth/login",
            json={"username": self._username, "password": self._password},
        )
        if response.status_code != 200:
            raise StudioError("login", response.status_code)
        self._signed_in = True

    def request(self, method, path, *, step, key=None, **kwargs):
        if not self._signed_in:
            self._login()
        headers = {"Idempotency-Key": key} if key else {}
        for attempt in (1, 2):
            try:
                response = self._client.request(method, path, headers=headers, **kwargs)
            except httpx.HTTPError as error:
                raise StudioError(step, 0, type(error).__name__) from None
            if response.status_code == 401 and attempt == 1 and not self._token:
                self._login()
                continue
            break
        if response.status_code >= 400:
            try:
                code = str(response.json().get("code") or response.json().get("detail") or "")[:120]
            except ValueError:
                code = ""
            raise StudioError(step, response.status_code, code)
        return response.json() if response.content else {}

    def version(self, project):
        return self.request("GET", f"/v1/projects/{project}/meta", step="meta")["version"]

    def actions(self, project, actions, key):
        body = {"expected_version": self.version(project), "actions": actions}
        return self.request(
            "POST", f"/v1/projects/{project}/actions", step="actions", key=key, json=body
        )

    def upload(self, project, client_asset_id, path):
        with open(path, "rb") as handle:
            receipt = self.request(
                "POST",
                "/v1/assets",
                step="upload",
                data={"client_project_id": project, "client_asset_id": client_asset_id},
                files={"file": (f"{client_asset_id}.jpg", handle, "image/jpeg")},
            )
        deadline = time.monotonic() + 90
        while receipt.get("status") != "ready":
            if receipt.get("status") in ("failed", "rejected") or time.monotonic() > deadline:
                raise StudioError("upload", 422, str(receipt.get("status")))
            time.sleep(1.5)
            receipt = self.request("GET", f"/v1/assets/{receipt['asset_id']}", step="asset")
        return receipt["asset_id"]


class StagingAdapter(DemoAdapter):
    def __init__(self, workdir, client):
        super().__init__(workdir)
        self.client = client

    # ---- helpers ----------------------------------------------------------

    def _key(self, board, label):
        """Stable per logical write, so a retried request is the same request."""
        digest = hashlib.sha256(f"{board.id}:{board.revision}:{label}".encode()).hexdigest()
        return f"tg-{digest[:40]}"

    def web_link(self, board, number=None):
        project = board.remote.get("project")
        if not project:
            return None
        link = f"{self.client.base_url}/?project={project}"
        if number:
            link += f"&entity={board.cards[number - 1].ref}"
        return link

    def _shot_actions(self, board):
        remote = board.remote
        actions, live = [], set()
        for order, card in enumerate(board.cards):
            if not card.ref:
                card.ref = f"shot-{remote['next_shot']}"
                remote["next_shot"] += 1
                actions.append({"op": "entity.create", "entity": {
                    "id": card.ref, "type": "shot", "parentId": "scene-1",
                    "title": card.title, "order": order,
                    "data": {"seconds": card.seconds, "prompt": card.prompt}}})  # fmt: skip
            else:
                actions.append({"op": "entity.update", "entity_id": card.ref, "patch": {
                    "title": card.title, "order": order,
                    "data": {"seconds": card.seconds, "prompt": card.prompt}}})  # fmt: skip
            live.add(card.ref)
            # The material is bound in the document as a first-frame or reference
            # link, exactly as the storyboard page does; generation settings
            # are written separately when a quote is requested.
            role = "firstFrame" if card.mode == "first_frame" else "reference"
            links = remote.setdefault("links", {})
            current = links.get(card.ref)
            if current != {"photo": card.photo, "role": role, "id": (current or {}).get("id")}:
                if current:
                    actions.append({"op": "link.delete", "link_id": current["id"]})
                link_id = f"link-{card.ref}-{remote.setdefault('next_link', 1)}"
                remote["next_link"] += 1
                actions.append({"op": "link.create", "link": {
                    "id": link_id, "source": f"material-{card.photo}",
                    "target": card.ref, "role": role}})  # fmt: skip
                links[card.ref] = {"photo": card.photo, "role": role, "id": link_id}
        # Shots this bot created for cards since deleted in chat: unused drafts only.
        for ref in remote["shots"]:
            if ref not in live:
                actions.append({"op": "entity.delete", "entity_id": ref})
                remote.get("links", {}).pop(ref, None)
        remote["shots"] = sorted(live)
        return actions

    def _configure_actions(self, board):
        actions = []
        for card in board.cards:
            asset = board.remote["assets"][card.photo - 1]
            inputs = (
                {"first_frame": {"asset_id": asset}, "images": []}
                if card.mode == "first_frame"
                else {"first_frame": None, "images": [{"asset_id": asset, "purpose": "reference"}]}
            )
            actions.append({
                "op": "shot.configure_generation", "shot_id": card.ref,
                "tier": "standard", "mode": "fl" if card.mode == "first_frame" else "ref",
                "prompt": card.prompt,
                "controls": {"duration": max(4, min(15, int(round(card.seconds)))),
                             "aspect_ratio": board.aspect},
                "inputs": inputs,
            })  # fmt: skip
        return actions

    def _sync(self, board, label):
        if board.cards and board.remote.get("project"):
            self.client.actions(board.remote["project"], self._shot_actions(board),
                                self._key(board, label))  # fmt: skip
        return board

    # ---- materials and story ------------------------------------------------

    def start(self, chat_id, photos, brief):
        board = super().start(chat_id, photos, brief)
        project, assets = self._create_and_upload(board, photos, brief)
        return self._attach(board, project, assets)

    def _create_and_upload(self, board, photos, brief):
        """Create the story and upload every photo. Names come later."""
        project = self.client.request(
            "POST", "/v1/projects", step="project", key=self._key(board, "project"),
            json={"title": board.title.replace("（演示）", "（Telegram）"),
                  "logline": (brief or "小店宣传片")[:200]},
        )["id"]  # fmt: skip
        # One at a time: the small staging host resets concurrent uploads.
        assets = [self.client.upload(project, f"tg-{board.id}-p{n}", path)
                  for n, path in enumerate(photos, 1)]  # fmt: skip
        return project, assets

    def _attach(self, board, project, assets, title=None):
        actions = [
            {"op": "asset.attach", "asset_id": asset, "entity_id": f"material-{n}",
             "title": board.materials[n - 1]}
            for n, asset in enumerate(assets, 1)
        ]  # fmt: skip
        if title:
            actions.append({"op": "project.update", "patch": {"title": title[:160]}})
        self.client.actions(project, actions, self._key(board, "materials"))
        board.remote.update(project=project, assets=assets, shots=[], next_shot=1)
        return board

    def add_photo(self, board, path):
        number = len(board.remote["assets"]) + 1
        asset = self.client.upload(board.remote["project"], f"tg-{board.id}-p{number}", path)
        self.client.actions(board.remote["project"], [
            {"op": "asset.attach", "asset_id": asset, "entity_id": f"material-{number}",
             "title": board.materials[number - 1]}], self._key(board, f"photo-{number}"))  # fmt: skip
        board.remote["assets"].append(asset)
        return board

    def rename_material(self, board, n, name):
        board = super().rename_material(board, n, name)
        self.client.actions(board.remote["project"], [{
            "op": "library.rename", "target": {"kind": "entity", "id": f"material-{n}"},
            "title": board.materials[n - 1]}], self._key(board, f"rename-{n}-{name}"))  # fmt: skip
        return board

    def plan_shots(self, board):
        return self._save_structure(super().plan_shots(board))

    def _save_structure(self, board):
        """Chapter, scene, story defaults and every part as a shot."""
        project = board.remote["project"]
        self.client.actions(project, [
            {"op": "entity.create", "entity": {"id": "chapter-1", "type": "chapter",
                                               "title": board.title}},
            {"op": "entity.create", "entity": {"id": "scene-1", "type": "scene",
                                               "parentId": "chapter-1", "title": "宣传片",
                                               "data": {"script": "\n".join(board.outline)}}},
        ], self._key(board, "structure"))  # fmt: skip
        try:  # the web storyboard reads journey.brief.aspect; generation reads defaults
            self.client.actions(project, [
                {"op": "journey.update", "patch": {"brief": {
                    "entry": "idea", "aspect": board.aspect,
                    "seconds": int(round(board.duration))}}},
                {"op": "storyboard.defaults", "value": {
                    "controls": {"aspect_ratio": board.aspect}}},
            ], self._key(board, "defaults"))  # fmt: skip
        except StudioError as error:
            log.warning("story aspect not saved: %s", error)
        return self._sync(board, "plan")

    def revise(self, board, instruction):
        board, summary = super().revise(board, instruction)
        return self._sync(board, "revise"), summary

    def swap_material(self, board, n, material):
        return self._sync(super().swap_material(board, n, material), f"swap-{n}")

    def edit_card(self, board, n, text):
        return self._sync(super().edit_card(board, n, text), f"edit-{n}")

    def delete_card(self, board, n):
        return self._sync(super().delete_card(board, n), f"delete-{n}")

    # ---- generation ---------------------------------------------------------

    def estimate(self, board):
        project = board.remote["project"]
        try:
            self.client.actions(project, self._configure_actions(board),
                                self._key(board, "configure"))  # fmt: skip
        except StudioError as error:
            if error.status != 409:
                raise
            return Estimate(
                credits=0,
                seconds=board.duration,
                note="staging 真实检查（未扣费）。故事板和镜头卡已保存在网站上。",
                checks=[f"服务器现在没有可用的生成档位（{error.code}）：显卡授权或机器未就绪"],
            )
        body = {"expected_version": self.client.version(project),
                "shot_ids": [c.ref for c in board.cards], "copies": 1}  # fmt: skip
        base = f"/v1/projects/{project}/storyboard/preflights"
        receipt = self.client.request("POST", base, step="preflight",
                                      key=self._key(board, "preflight"), json=body)  # fmt: skip
        deadline = time.monotonic() + 60
        while receipt.get("status") in ("pending", "running", "planning") \
                and time.monotonic() < deadline:  # fmt: skip
            time.sleep(1.5)
            receipt = self.client.request("GET", f"{base}/{receipt['id']}", step="preflight")
        board.remote["preflight"] = {"id": receipt.get("id"), "status": receipt.get("status")}
        estimate = receipt.get("estimate") or {}
        credits = estimate.get("credits")
        cost = estimate.get("cost_microusd")
        checks = [str(b.get("code") or b)[:80] for b in receipt.get("blockers") or []]
        if receipt.get("status") not in ("ready", None) and not checks:
            checks.append(f"预检状态：{receipt.get('status')}")
        if credits is None and cost is not None:
            note = f"staging 真实预检：约 {cost / 1e6:.2f} 美元（未扣费）"
        elif credits is None:
            note = "staging 真实预检：服务器还没给出价格（未扣费）"
        else:
            note = "staging 真实预检（未扣费）"
        return Estimate(
            credits=int(credits) if credits is not None else 0,
            seconds=board.duration,
            note=note + "\n生成要开显卡，这一步需要老板批准，bot 会停在这里。",
            checks=checks,
        )

    def generate(self, chat_id, board, progress):
        raise GenerationHeld()


class LlmStagingAdapter(StagingAdapter, LlmAdapter):
    """Gemini plans and revises; every change is saved to the staging story.

    The proposal goes to the chat as soon as Gemini is done. Creating the
    story, uploading photos (~12 s each on staging) and writing the shots run
    in the background; the bot is told when the web link is ready. Any later
    change first waits for that save (`_settle`), so the chat and the website
    never diverge. After a restart the save is simply redone in the foreground.
    """

    STAGING_METHODS = ("rename_material", "revise", "swap_material", "edit_card",
                       "delete_card", "add_photo", "estimate")  # fmt: skip

    def __init__(self, workdir, studio, gemini, on_saved=None):
        LlmAdapter.__init__(self, workdir, gemini)
        self.client = studio
        self.on_saved = on_saved  # (chat_id, web link) -> None, called from the worker
        self._pool = ThreadPoolExecutor(max_workers=2)
        self._pending = {}  # board id -> ("uploading", future, chat_id) | ("saving", future)

    def start(self, chat_id, photos, brief):
        board = DemoAdapter.start(self, chat_id, photos, brief)
        upload = self._pool.submit(self._create_and_upload, board, photos, brief)
        board = self._plan_with_model(board, photos, brief)
        self._pending[board.id] = ("uploading", upload, chat_id)
        return board

    def plan_shots(self, board):
        board = LlmAdapter.plan_shots(self, board)
        pending = self._pending.pop(board.id, None)
        if pending and pending[0] == "uploading":
            snapshot = type(board).from_dict(board.to_dict())
            self._pending[board.id] = ("saving", self._pool.submit(
                self._finish_save, snapshot, pending[1], pending[2]))  # fmt: skip
            return board
        return self._settle(board)

    def _finish_save(self, board, upload, chat_id):
        try:
            project, assets = upload.result()
            board = self._attach(board, project, assets, title=board.title)
            board = self._save_structure(board)
        except Exception:
            log.exception("background save to staging failed")
            raise
        if self.on_saved:
            try:
                self.on_saved(chat_id, StagingAdapter.web_link(self, board))
            except Exception:
                log.exception("saved notice failed")
        return board

    def _settle(self, board):
        """Bring the website up to date before touching it again."""
        pending = self._pending.pop(board.id, None)
        if pending:
            try:
                saved = pending[1].result()
            except Exception:
                saved = None
            if pending[0] == "saving" and saved is not None:
                board.remote.update(saved.remote)
                for card, saved_card in zip(board.cards, saved.cards):
                    card.ref = saved_card.ref
                return board
            if pending[0] == "uploading" and saved is not None:
                return self._attach(board, *saved, title=board.title)
        if not board.remote.get("project"):  # lost by a restart or a failed save
            project, assets = self._create_and_upload(board, board.photos, board.brief)
            board = self._attach(board, project, assets, title=board.title)
            if board.cards:
                board = self._save_structure(board)
        return board

    def web_link(self, board, number=None):
        pending = self._pending.get(board.id)
        if pending:
            if not pending[1].done() or pending[1].exception() or pending[0] != "saving":
                return None  # the bot posts the link once the background save is done
            return StagingAdapter.web_link(self, pending[1].result(), number)
        return StagingAdapter.web_link(self, board, number)


def _settled(name):
    method = getattr(StagingAdapter, name)

    def call(self, board, *args, **kwargs):
        return method(self, self._settle(board), *args, **kwargs)

    call.__name__ = name
    return call


for _name in LlmStagingAdapter.STAGING_METHODS:
    setattr(LlmStagingAdapter, _name, _settled(_name))
