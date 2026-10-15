"""Run inside the CPU platform image with --network none and a disposable /data tmpfs.

Only synthetic projects, in-memory random test passwords and generated PNG media
are used. Nothing here touches a user's data, credentials or inference provider.
"""
import io
import hashlib
import json
import os
from fractions import Fraction
from pathlib import Path
import secrets
import subprocess
import sys
import time

sys.path.insert(0, "/app")

from fastapi.testclient import TestClient
from PIL import Image
from studio_platform.api import create_app
from studio_platform.settings import Settings
from studio_platform.control import WorkerControl, WorkerSpec
from studio_platform.execution_policy import ExecutionPolicies
from studio_platform.render_backend import CPURenderBackend
from studio_platform.render_plans import RECIPE, MODEL, CONFIGURATION, POOL
from studio_platform.worker import WorkerRunner
from studio_platform.caption_server import caption_signature


def check_roughcut(client, app, project, root):
    """Exercise the same HTTP -> ledger -> worker -> private download path as UI."""
    repo, store = app.state.repository, app.state.storage
    control = WorkerControl(repo)
    backend = CPURenderBackend(root / "cpu-work" / "attempts", enabled=True)
    backend.assert_subtitle_ready()
    control.register(WorkerSpec("container-cpu", POOL, "local-cpu", "container-cpu-instance",
        (), (RECIPE,), MODEL, CONFIGURATION, backend="cpu-render"))
    control.mark_ready("container-cpu", upstream_idle_confirmed=True)
    # Two distinct clips make scene order and fit dimensions observable. A
    # source soundtrack is deliberately absent: only the explicit audio track
    # should survive the timeline compilation.
    for index, color in enumerate(("navy", "orange")):
        path = root / f"clip-{index}.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-f", "lavfi", "-i",
            f"color=c={color}:s=256x256:r=24", "-t", "1", "-c:v", "libx264", "-threads", "1",
            "-pix_fmt", "yuv420p", "-an", str(path)], check=True, capture_output=True, timeout=30)
        with path.open("rb") as stream:
            uploaded = client.post("/v1/assets", data={"client_project_id": project["id"]},
                files={"file": (path.name, stream, "video/mp4")})
        assert uploaded.status_code == 201, "Synthetic video upload failed"
        asset = uploaded.json()
        project["entities"].append(dict(id=f"clip-{index}", type="video", parentId=None,
            title=f"Clip {index}", description="", version=1, order=index, status="draft",
            data={"fileId": f"source-{index}", "cloudAssetId": asset["id"]}))
        if index == 0:
            shot = project["entities"][2]
        else:
            shot = dict(id="shot-two", type="shot", parentId="scene", title="Second clip",
                description="", version=1, order=1, status="draft", data={})
            project["entities"].append(shot)
        shot["data"].update(seconds=1, selectedAssetId=f"clip-{index}")
    audio_path = root / "tone.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-f", "lavfi", "-i",
        "sine=frequency=440:sample_rate=32000:duration=1", "-ac", "2", str(audio_path)],
        check=True, capture_output=True, timeout=30)
    with audio_path.open("rb") as stream:
        uploaded = client.post("/v1/assets", data={"client_project_id": project["id"]},
            files={"file": (audio_path.name, stream, "audio/wav")})
    assert uploaded.status_code == 201, "Synthetic audio upload failed"
    project["entities"].append(dict(id="tone", type="audio", parentId=None, title="Tone",
        description="", version=1, order=0, status="draft",
        data={"fileId": "source-tone", "cloudAssetId": uploaded.json()["id"]}))
    project["journey"] = {"brief": {"aspect": "16:9"}, "sound": {"mode": "music"},
        "soundTracks": {"chapter": [{"assetId": "tone", "fileId": "source-tone", "shotId": "shot-two",
            "start": 0, "end": 1, "offset": 0, "gain": .6}]}}
    # Captions are manually confirmed against this exact timeline. Rebuild the
    # signature here just as a user confirmation does; the server verifies it
    # again instead of trusting text or timing sent with the render request.
    captions = {"cues": [
        {"id": "first-line", "start": .25, "end": .75, "text": "月光落在窗前\n她终于回来了"},
        {"id": "second-line", "start": 1.25, "end": 1.75, "text": "这一刻\n故事继续"}]}
    project["journey"]["captionTracks"] = {"chapter": captions}
    shots = [next(entity for entity in project["entities"] if entity["id"] == key)
             for key in ("shot", "shot-two")]
    captions["confirmedSnapshot"] = caption_signature(project, "chapter", shots)
    saved = client.put("/v1/projects/"+project["id"], json={"project": project, "expected_version": 1})
    assert saved.status_code == 200, "CPU timeline could not be saved"
    plan = client.post("/v1/render-plans", json={"client_ref": {"project_id": project["id"],
        "chapter_id": "chapter"}, "resolution": "480P", "burn_subtitles": True})
    assert plan.status_code == 201 and plan.json()["status"] == "ready", "CPU plan not ready"
    assert plan.json()["simulation"] is False
    assert plan.json()["timeline"]["subtitles"]["burned"] is True
    headers = {"Idempotency-Key": "container-real-cpu-roughcut"}
    job = client.post("/v1/jobs", json={"plan_id": plan.json()["plan_id"]}, headers=headers)
    assert job.status_code == 202 and job.json()["status"] == "queued"
    runner = WorkerRunner(repo, store, root / "cpu-work", control=control,
        backend=backend,
        submission_guard=ExecutionPolicies(app.state.settings, repo).submission_allowed)
    deadline = time.monotonic()+90
    while time.monotonic() < deadline:
        result = runner.run_once("container-cpu", POOL)
        done = client.get("/v1/jobs/"+job.json()["id"]).json()
        if done["status"] == "succeeded":
            break
        assert done["status"] not in {"failed", "cancelled"}, "Real CPU rough cut failed"
        time.sleep(.1)
    assert done["status"] == "succeeded", "CPU rough cut exceeded check deadline"
    assert {a["kind"] for a in done["artifacts"]} == {"video", "audio"}
    for artifact in done["artifacts"]:
        inline = client.get(artifact["content_url"])
        attachment = client.get(artifact["download_url"])
        assert inline.status_code == attachment.status_code == 200
        assert inline.content == attachment.content
        assert attachment.headers["content-disposition"].startswith("attachment;")
        assert attachment.headers["content-type"].split(";")[0] == artifact["mime"]
        assert len(attachment.content) == artifact["size_bytes"]
        assert hashlib.sha256(attachment.content).hexdigest() == artifact["sha256"]
        if artifact["kind"] == "video":
            rendered_path = root / "captioned-result.mp4"
            rendered_path.write_bytes(attachment.content)
    # Decode exact output frames, not just metadata. Each cue has an exclusive
    # end frame. The solid source colors contain no white pixels in this area.
    for frame_index in (5, 6, 12, 17, 18, 29, 30, 36, 41, 42):
        raw = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", str(rendered_path),
            "-vf", f"select=eq(n\\,{frame_index})", "-frames:v", "1", "-f", "image2pipe",
            "-vcodec", "png", "-"], check=True, capture_output=True, timeout=20).stdout
        with Image.open(io.BytesIO(raw)) as frame:
            frame = frame.convert("RGB")
            area = frame.crop((int(frame.width*.08), int(frame.height*.65),
                               int(frame.width*.92), int(frame.height*.89)))
            white = sum(r > 210 and g > 210 and b > 210 for r, g, b in area.get_flattened_data())
        expected = 6 <= frame_index < 18 or 30 <= frame_index < 42
        assert (white > 20) == expected, "Burned Chinese cue frame boundary mismatch"
    # The runtime image can ship a different FFmpeg release from the CI host.
    # Exercise sub-second concat through the real API using that exact binary.
    project["entities"] = [e for e in project["entities"] if e["type"] != "shot"]
    for index, frames in enumerate((1, 2, 1, 1, 2, 1)):
        project["entities"].append(dict(id=f"tiny-{index}", type="shot", parentId="scene",
            title=f"Tiny clip {index}", description="", version=1, order=index, status="draft",
            data={"seconds": frames/24, "selectedAssetId": f"clip-{index % 2}"}))
    project["journey"] = {"brief": {"aspect": "16:9"}, "sound": {"mode": "silent"}}
    saved = client.put("/v1/projects/"+project["id"], json={"project": project, "expected_version": 2})
    assert saved.status_code == 200, "Short-clip timeline could not be saved"
    short_plan = client.post("/v1/render-plans", json={"client_ref": {"project_id": project["id"],
        "chapter_id": "chapter"}, "resolution": "480P", "burn_subtitles": False})
    assert short_plan.status_code == 201 and short_plan.json()["status"] == "ready"
    short_job = client.post("/v1/jobs", json={"plan_id": short_plan.json()["plan_id"]},
        headers={"Idempotency-Key": "container-six-short-clips"})
    assert short_job.status_code == 202
    deadline = time.monotonic()+90
    while time.monotonic() < deadline:
        runner.run_once("container-cpu", POOL)
        short_done = client.get("/v1/jobs/"+short_job.json()["id"]).json()
        if short_done["status"] == "succeeded":
            break
        assert short_done["status"] not in {"failed", "cancelled"}, "Short-clip CPU render failed"
        time.sleep(.1)
    assert short_done["status"] == "succeeded", "Short-clip render exceeded check deadline"
    short_video = next(a for a in short_done["artifacts"] if a["kind"] == "video")
    response = client.get(short_video["download_url"])
    assert response.status_code == 200
    assert hashlib.sha256(response.content).hexdigest() == short_video["sha256"]
    short_path = root / "short-grid.mp4"
    short_path.write_bytes(response.content)
    info = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_streams", "-show_packets", "-of", "json", str(short_path)], timeout=20))
    assert Fraction(info["streams"][0]["avg_frame_rate"]) == 24
    packets = sorted(info["packets"], key=lambda p: float(p["pts_time"]))
    assert len(packets) == 8
    for index, packet in enumerate(packets):
        assert abs(float(packet["pts_time"])-index/24) < .00001
        assert abs(float(packet["duration_time"])-1/24) < .00001
    control.drain("container-cpu")
    return done


def main():
    # Read these from this exact container image, not the checkout. Catalog
    # metadata is not GPU/production qualification or deployment authority.
    from studio_platform.runtime_catalog import PROFILE_IDS, public_catalog, engine_manifest
    catalog = public_catalog()
    assert {profile['id'] for profile in catalog['profiles']} == set(PROFILE_IDS)
    for profile in catalog['profiles']:
        assert profile['validation']['production_adapter_verified'] is False
        if profile.get('compiler_epoch') == 'full-controls-v2':
            assert profile['validation']['scope'] == 'pinned_native_control_mapping'
            assert profile['verified_cases'] == [] and profile['qualification_cases'] == []
        elif not profile['verified_cases']:
            assert profile['validation']['scope'] == 'pending_hardware_qualification'
            assert profile.get('qualification_cases')
            assert all('measurements' not in case for case in profile['qualification_cases'])
        for mode in (model['mode'] for model in profile['models']):
            assert engine_manifest(profile['id'], mode).document['production_adapter_verified'] is False
    assert os.getuid() == 10001, "Production image must run unprivileged"
    root = Path("/data")
    assert root.is_dir() and not (root/"platform.sqlite3").exists(), "Only fresh disposable data is allowed"
    settings = Settings(root, public_origin="https://studio.example.test", frontend_dir=Path("/app/yingxu-dist"),
                        render_enabled=True)
    app = create_app(settings)
    passwords = {name: secrets.token_urlsafe(24) for name in ("superdan", "supervan")}
    for name, password in passwords.items():
        app.state.auth.set_password(name, password)
    entity = lambda key, kind, parent: dict(id=key, type=kind, parentId=parent, title=key,
        description="Synthetic container check", version=1, order=0, status="draft", data={})
    project = {"schemaVersion": 4, "id": "container-project", "title": "Synthetic test only", "logline": "",
        "entities": [entity("chapter", "chapter", None), entity("scene", "scene", "chapter"), entity("shot", "shot", "scene")],
        "links": [], "jobs": [], "layout": {"positions": {}, "viewport": {"x": 0, "y": 0, "zoom": 1}}}
    with TestClient(app, base_url="https://studio.example.test") as client:
        assert client.get("/").status_code == 200
        assert client.get("/freestyle").status_code == 200
        assert client.get("/v1/projects").status_code == 401
        login = client.post("/api/auth/login", json={"username": "superdan", "password": passwords["superdan"]})
        assert login.status_code == 200
        assert all(flag in login.headers["set-cookie"] for flag in ("HttpOnly", "Secure", "SameSite=lax"))
        assert client.post("/v1/projects", json={"project": project}).status_code == 201
        buffer = io.BytesIO()
        Image.new("RGB", (512, 512), "navy").save(buffer, format="PNG")
        raw = buffer.getvalue()
        upload = client.post("/v1/assets", data={"client_project_id": project["id"], "client_asset_id": "image-one"},
            files={"file": ("synthetic.png", raw, "image/png")})
        assert upload.status_code == 201, "Upload/CPU normalization failed"
        asset = upload.json()
        assert asset["metadata"]["model_ready"]
        response = client.get(asset["content_url"], headers={"Range": "bytes=2-6"})
        assert response.status_code == 206 and response.content == raw[2:7]
        # The new platform can save/validate full controls while execution is off.
        body = {"client_ref": {"project_id": project["id"], "shot_id": "shot", "shot_version": 1},
            "recipe_id": "h3-base-fl2va-v1", "prompt": "A synthetic non-generated scene",
            "inputs": {"first_frame": asset["id"]}, "controls": {"resolution": "480P", "duration": 5}}
        plan = client.post("/v1/generation-plans", json=body)
        assert plan.status_code == 201 and plan.json()["status"] == "blocked"
        headers = {"Idempotency-Key": "container-test-one"}
        job = client.post("/v1/jobs", json={"plan_id": plan.json()["plan_id"]}, headers=headers)
        assert job.status_code == 202 and job.json()["status"] == "blocked"
        repeat = client.post("/v1/jobs", json={"plan_id": plan.json()["plan_id"]}, headers=headers)
        assert repeat.json()["id"] == job.json()["id"]
        roughcut = check_roughcut(client, app, project, root)
        assert client.post("/api/auth/logout").status_code == 200
        assert client.post("/api/auth/login", json={"username": "supervan", "password": passwords["supervan"]}).status_code == 200
        assert client.get("/v1/projects").json() == {"projects": []}
        assert client.get(asset["content_url"]).status_code == 404
        assert client.get("/v1/jobs/"+job.json()["id"]).status_code == 404
        assert client.get("/v1/jobs/"+roughcut["id"]).status_code == 404
        for artifact in roughcut["artifacts"]:
            assert client.get(artifact["download_url"]).status_code == 404
        assert client.get("/healthz").json()["cloud_creation_enabled"] is False
    # A new application process/factory sees the same durable DB and files.
    restarted = create_app(settings)
    with TestClient(restarted, base_url="https://studio.example.test") as client:
        assert client.post("/api/auth/login", json={"username": "superdan", "password": passwords["superdan"]}).status_code == 200
        assert client.get("/v1/projects/"+project["id"]).status_code == 200
        assert client.get(asset["content_url"]).content == raw
        assert client.get("/v1/jobs/"+roughcut["id"]).json()["status"] == "succeeded"
        for artifact in roughcut["artifacts"]:
            assert client.get(artifact["download_url"]).status_code == 200
    forbidden = [".env", "cloud-state.json", "api-vault.dpapi", "cloud_control.py", "ssh-known-hosts"]
    assert not any((Path("/app")/name).exists() for name in forbidden)
    print("PASS: unprivileged Linux image, frontend, secure sessions, private media/jobs, PNG normalization, two-clip CPU rough cut with explicit sound, burned Chinese caption pixels and exact cue frames, six short clips with exact 24fps packet timestamps, MP4/FLAC attachment checksums, Range, restart persistence; no network or GPU")


if __name__ == "__main__":
    main()
