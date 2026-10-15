"""Small authenticated Inkseq client. Requires httpx; never logs credentials."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import sys
import time
from urllib.parse import parse_qs, urlsplit

import httpx


def origin(value):
    parsed = urlsplit(value)
    if (parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username
            or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
            or (parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"})):
        raise ValueError("Use an exact HTTPS origin (HTTP allowed only on loopback)")
    return value.rstrip("/")


def api_path(value):
    parsed = urlsplit(value)
    if (parsed.scheme or parsed.netloc or parsed.fragment or "\\" in value
            or any(ord(c) < 32 for c in value) or not value.startswith("/v1/")
            or "%" in parsed.path or "/../" in value or "/./" in value):
        raise ValueError("Use a relative /v1/ API path")
    if parsed.path.startswith(("/v1/api-keys", "/v1/auth")):
        raise ValueError("Manage account credentials in the website, not this client")
    return value


def credential(args):
    if getattr(args, "connection", None):
        if args.profile or args.registry_root or os.environ.get("SIXNINE_API_KEY"):
            raise ValueError("Choose one explicit credential source; connection cannot be combined with environment or registry credentials")
        import importlib.util
        spec = importlib.util.spec_from_file_location("sixnine_os_connection", Path(__file__).with_name("connect.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        effective_origin, state = module.resolve_connection(module.SecureStore(), args.base_url, args.connection)
        if not state or state.get("protocol") != module.PROTOCOL or state.get("status") != "connected":
            raise ValueError("No activated OS-protected connection for this exact origin")
        args.base_url = effective_origin
        value = state.get("api_key")
    elif args.profile:
        if not args.registry_root:
            raise ValueError("Specify the existing registry root with a profile")
        sys.path.insert(0, str(Path(args.registry_root).resolve()))
        from api_registry import load_api
        config = load_api("sixnine", profile=args.profile)
        if not config.base_url or origin(config.base_url) != args.base_url:
            raise ValueError("Registry profile endpoint does not match the chosen origin")
        value = config.api_key
    else:
        value = os.environ.get("SIXNINE_API_KEY")
    if not value or not value.isascii() or any(c.isspace() for c in value):
        raise ValueError("A valid process-only credential or existing registry profile is required")
    return value


def write_json(result, destination=None):
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if destination:
        with Path(destination).open("x", encoding="utf-8") as handle:
            handle.write(text)
        print("Response saved without overwriting an existing file")
    else:
        print(text, end="")


def safe_authoring_fields(body):
    """Standalone, closed field vocabulary: never print server input values."""
    fields = {'body', 'header', 'Idempotency-Key', 'expected_card_version', 'title', 'prompt',
        'recipe_id', 'controls', 'inputs', 'copies', 'turn_id', 'source_revision_id', 'deployment_profile_id',
        'first_frame', 'last_frame', 'images', 'videos', 'audios', 'guides', 'asset_id', 'media_id',
        'purpose', 'source_range', 'start', 'end', 'include_audio', 'time_seconds', 'use_audio',
        'duration', 'resolution', 'aspect_ratio', 'width', 'height', 'steps', 'seed',
        'sampler_name', 'scheduler', 'denoise', 'shift_video', 'shift_audio', 'ref_image_size',
        'video_decode', 'video_tile_size', 'video_overlap', 'video_temporal_size', 'video_temporal_overlap',
        'audio_decode', 'encoder_device',
        'generate_audio', 'export_crf'}
    reasons = {'missing_field', 'type_mismatch', 'unknown_field', 'invalid_value', 'stale_version'}
    result = []
    errors = body.get('field_errors')
    if not isinstance(errors, list):
        return result
    for entry in errors[:12]:
        if not isinstance(entry, dict):
            continue
        path, reason = entry.get('path'), entry.get('reason')
        if (not isinstance(path, list) or not 1 <= len(path) <= 8
                or path[0] not in ('body', 'header') or not isinstance(reason, str) or reason not in reasons
                or any(not (type(part) is str and part in fields
                    or type(part) is int and 0 <= part <= 16) for part in path)):
            continue
        result.append({'path': path, 'reason': reason})
    return result


def check_response(response):
    if not 200 <= response.status_code < 300:
        retry = retry_delay(response)
        suffix = f" (Retry-After: {retry:g} seconds)" if retry is not None else ""
        # Standalone helper: preserve only closed, source-level diagnostics.
        # Arbitrary bodies/messages/paths/prompts/credentials stay suppressed.
        codes = {'preflight_rejected','incompatible_fl_ref_inputs','invalid_duration',
            'invalid_canvas_grid','invalid_canvas_area','invalid_canvas_aspect','reference_required',
            'reference_video_exceeds_output','invalid_request','invalid_controls','invalid_inputs',
            'revision_hash_conflict','version_conflict','idempotency_conflict','budget_exceeded','job_capacity_exceeded',
            'reference_conflict','reference_not_ready','invalid_bindings','invalid_prompt','invalid_settings',
            'shot_version_conflict','asset_not_ready','source_hash_conflict','insufficient_scope',
            'execution_adapter_unavailable','profile_not_published','validation_error',
            'wangp_invalid_request','wangp_invalid_prompt','wangp_invalid_steps','wangp_invalid_seed',
            'wangp_invalid_duration','wangp_output_spec_mismatch','wangp_invalid_native_frames',
            'wangp_invalid_inputs','wangp_profile_model_mismatch','wangp_input_kind_mismatch',
            'wangp_input_snapshot_mismatch','wangp_profile_reference_count_exceeded',
            'wangp_ref_media_not_ready','wangp_ref_invalid_dimensions','wangp_ref_aligned_video_required',
            'wangp_ref_selected_audio_required','wangp_ref_reference_required',
            'wangp_ref_audio_requires_visual_reference','wangp_ref_total_video_duration_exceeded',
            'wangp_ref_total_audio_duration_exceeded','wangp_adapter_fl_reference_inputs_unmapped',
            'wangp_adapter_ref_first_last_unmapped','wangp_adapter_reference_video_soundtrack_unmapped',
            'wangp_unknown_deployment_profile','wangp_profile_mode_unsupported'}
        codes |= {'wangp_unsupported_'+name for name in ('controls','steps','sampler_name','scheduler',
            'denoise','shift_video','shift_audio','video_decode','video_tile_size','video_overlap',
            'audio_decode','encoder_device','generate_audio','export_crf')}
        codes |= {
            'wangp_asset_owner_mismatch',
            'wangp_audio_excerpts_require_video_soundtrack',
            'wangp_audio_from_video_requires_denoise',
            'wangp_audio_guide_requires_soundtrack',
            'wangp_audio_refinement_soundtrack_conflict',
            'wangp_conflicting_guides_alias',
            'wangp_conflicting_image_anchors',
            'wangp_conflicting_video_audio',
            'wangp_control_audio_requires_soundtrack',
            'wangp_control_source_count_exceeded',
            'wangp_control_video_canvas_mismatch',
            'wangp_control_video_required',
            'wangp_control_video_timeline_mismatch',
            'wangp_denoise_requires_control_video',
            'wangp_duplicate_input_handle',
            'wangp_excerpt_outside_selected_source',
            'wangp_excerpts_require_single_reference_video',
            'wangp_fl_audio_requires_soundtrack',
            'wangp_fl_reference_image_unsupported',
            'wangp_fl_reference_video_unsupported',
            'wangp_frame_injection_denoise_conflict',
            'wangp_guide_outside_output',
            'wangp_image_size_requires_reference_images',
            'wangp_input_handles_mismatch',
            'wangp_input_kind_mismatch',
            'wangp_input_snapshot_mismatch',
            'wangp_invalid_audio_conditioning',
            'wangp_invalid_audio_refinement',
            'wangp_invalid_compiled_request',
            'wangp_invalid_custom_settings',
            'wangp_invalid_duration',
            'wangp_invalid_excerpt_duration',
            'wangp_invalid_excerpt_position',
            'wangp_invalid_excerpts',
            'wangp_invalid_denoise',
            'wangp_invalid_shift_video',
            'wangp_native_timed_video_guide_unsupported',
            'wangp_native_timed_audio_guide_unsupported',
            'wangp_invalid_export_crf',
            'wangp_invalid_generate_audio',
            'wangp_invalid_guide_audio',
            'wangp_invalid_guide_time',
            'wangp_invalid_guides',
            'wangp_invalid_image_positions',
            'wangp_invalid_image_refs_relative_size',
            'wangp_invalid_input_identity',
            'wangp_invalid_inputs',
            'wangp_invalid_mask_mode',
            'wangp_invalid_native_flags',
            'wangp_invalid_prompt',
            'wangp_invalid_request',
            'wangp_invalid_seed',
            'wangp_invalid_steps',
            'wangp_invalid_video_audio',
            'wangp_invalid_video_conditioning',
            'wangp_manifest_binding_mismatch',
            'wangp_mask_mode_requires_denoise',
            'wangp_output_spec_mismatch',
            'wangp_profile_model_mismatch',
            'wangp_profile_reference_count_exceeded',
            'wangp_profile_settings_mismatch',
            'wangp_ref_aligned_video_required',
            'wangp_ref_audio_requires_visual_reference',
            'wangp_ref_invalid_dimensions',
            'wangp_ref_media_not_ready',
            'wangp_ref_reference_required',
            'wangp_ref_selected_audio_required',
            'wangp_ref_total_audio_duration_exceeded',
            'wangp_ref_total_excerpt_duration_exceeded',
            'wangp_ref_total_video_duration_exceeded',
            'wangp_selected_video_has_no_audio',
            'wangp_single_soundtrack_required',
            'wangp_staged_input_mismatch',
            'wangp_unbound_input_handle',
            'wangp_unsupported_controls',
            'wangp_unsupported_sampler_name',
            'wangp_unused_input_handle',
            'wangp_video_conditioning_required',
            'wangp_video_excerpts_require_explicit_audio_excerpts',
            'wangp_video_soundtrack_requires_single_video',
        }
        codes |= {f'wangp_{stage}_{category}' for stage in ('validation','generation','runtime','unknown')
            for category in ('unclassified','cuda_out_of_memory','tensor_shape_mismatch',
                'media_decode_failed','dependency_missing','seed_out_of_range')}
        safe = ''
        try:
            if len(response.content) <= 16384:
                body = response.json()
                code = body.get('code') if type(body) is dict else None
                if type(code) is str and code in codes:
                    safe = '; code='+code
                    stage = body.get('stage',body.get('error_stage'))
                    if stage in ('preflight','validation','generation','runtime','unknown'):
                        safe += '; stage='+stage
                    if code in ('invalid_request', 'version_conflict'):
                        field_errors = safe_authoring_fields(body)
                        if field_errors:
                            safe += '; field_errors='+json.dumps(field_errors, separators=(',', ':'))
        except (ValueError, TypeError, AttributeError, httpx.ResponseNotRead):
            pass
        raise ValueError(f"API returned HTTP {response.status_code}{suffix}{safe}; other response details suppressed")


def retry_delay(response):
    value = response.headers.get("retry-after", "")
    try:
        if re.fullmatch(r"[0-9]{1,6}", value):
            return float(value)
        moment = parsedate_to_datetime(value)
        if moment.tzinfo is None:
            return None
        return max(0.0, (moment - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


def identity(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", value):
        raise ValueError("Use an exact project, asset or job ID")
    return value


def signed_download_target(location, resolver=None):
    """Validate and pin a public HTTPS destination without revealing its bearer URL."""
    try:
        if not isinstance(location, str) or len(location) > 16384 or "\\" in location or any(ord(c) < 33 for c in location):
            raise ValueError()
        parsed = urlsplit(location)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.fragment or parsed.port not in (None, 443) or "%" in parsed.hostname):
            raise ValueError()
        # These are download grants, not general external redirect navigation.
        query = parse_qs(parsed.query)
        if not any(query.get(name) for name in ("X-Amz-Signature", "X-Goog-Signature", "Signature", "sig")):
            raise ValueError()
        host = parsed.hostname.encode("idna").decode("ascii")
        addresses = (resolver or socket.getaddrinfo)(host, 443, type=socket.SOCK_STREAM)
        ips = [ipaddress.ip_address(record[4][0]) for record in addresses]
        if not ips or any(not ip.is_global or ip.is_multicast or (getattr(ip, "ipv4_mapped", None) is not None
                                              and not ip.ipv4_mapped.is_global) for ip in ips):
            raise ValueError()
        # Connecting to this IP prevents a second DNS lookup / rebinding; Host and
        # TLS SNI still name the original storage host for signature and cert checks.
        url = httpx.URL(location).copy_with(host=str(ips[0]))
        host_header = "[" + host + "]" if ":" in host else host
        return url, host_header, host
    except Exception:
        raise ValueError("Storage redirect rejected: require one signed HTTPS URL on public port 443") from None


def save_download(response, args):
    check_response(response)
    digest, size = hashlib.sha256(), 0
    with Path(args.output).open("xb") as output:
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > args.max_bytes:
                raise ValueError("Download limit exceeded; partial file retained for inspection")
            digest.update(chunk)
            output.write(chunk)
    actual = digest.hexdigest()
    if args.sha256 and actual != args.sha256.lower():
        raise ValueError("SHA-256 mismatch; do not adopt the downloaded file")
    return {"bytes": size, "sha256": actual, "verified": bool(args.sha256)}


def download(client, args, transport=None, resolver=None):
    path = api_path(args.path)
    if not re.fullmatch(r"/v1/(?:assets|artifacts)/[A-Za-z0-9_-]{1,160}/content", urlsplit(path).path):
        raise ValueError("Download requires an authenticated asset or artifact content route")
    if not isinstance(args.max_bytes, int) or args.max_bytes < 1 or args.max_bytes > 2 * 1024**3:
        raise ValueError("Download byte limit must be between 1 and 2 GiB")
    if args.sha256 and not re.fullmatch(r"[0-9a-fA-F]{64}", args.sha256):
        raise ValueError("Use the manifest's complete SHA-256")
    with client.stream("GET", path) as response:
        if response.status_code != 307:
            return save_download(response, args)
        target, host_header, server_name = signed_download_target(response.headers.get("location"), resolver)
    # An entirely separate client carries no API credential, Cookie or Referer.
    # Exactly one storage hop is permitted; a second redirect is an error.
    try:
        with httpx.Client(follow_redirects=False, trust_env=False, transport=transport,
                          timeout=httpx.Timeout(300, connect=15)) as storage:
            with storage.stream("GET", target, headers={"Host": host_header},
                                extensions={"sni_hostname": server_name}) as response:
                return save_download(response, args)
    except (OSError, httpx.HTTPError):
        raise ValueError("Storage download incomplete; signed URL details suppressed; retry the original content route") from None


def resume_upload(client, args):
    identity(args.asset_id)
    session = getattr(args, "session", None)
    if session:
        response = client.get("/v1/quick-chat/sessions/"+identity(session)+"/assets",
                              params={"client_asset_id": args.asset_id})
    else:
        identity(args.project)
        response = client.get("/v1/assets", params={"client_project_id": args.project})
    check_response(response)
    matches = [asset for asset in response.json()["assets"] if asset.get("client_asset_id") == args.asset_id]
    if len(matches) != 1:
        raise ValueError("No unique original upload receipt found; reconcile the original upload before sending another file")
    asset = matches[0]
    if asset.get("status") != "ready":
        asset_id = identity(asset.get("asset_id", asset.get("id")))
        target = ("/v1/quick-chat/sessions/"+identity(session)+"/assets/"+asset_id+"/resume"
                  if session else "/v1/assets/"+asset_id+"/resume")
        response = client.post(target)
        check_response(response)
        asset = response.json()
    return asset


def poll_job(client, args, clock=time.monotonic, sleep=time.sleep):
    ident = identity(args.job)
    if not 1 <= args.max_wait <= 3600 or not 2 <= args.interval <= 30:
        raise ValueError("Poll max-wait must be 1..3600 seconds and interval 2..30 seconds")
    deadline, last = clock() + args.max_wait, None
    while clock() < deadline:
        response = client.get("/v1/jobs/" + ident, timeout=min(30, deadline-clock()))
        delay = max(args.interval, retry_delay(response) or 0)
        if response.status_code not in (429, 503):
            check_response(response)
            last = response.json()
            if last.get("status") in {"succeeded", "failed", "cancelled", "blocked", "submission_unknown", "recovery_hold"}:
                return {"job": last, "poll_status": "stopped"}
        if delay >= deadline-clock():
            return {"job": last, "poll_status": "waiting", "retry_after_seconds": delay}
        sleep(delay)
    return {"job": last, "poll_status": "waiting"}


def run(args, transport=None, *, resolver=None, clock=time.monotonic, sleep=time.sleep):
    args.base_url = origin(args.base_url)
    if getattr(args, "output", None) and Path(args.output).exists():
        raise FileExistsError("Output already exists; preserve the previous receipt or take")
    token = credential(args)
    headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}
    if getattr(args, "idempotency_key", None):
        headers["Idempotency-Key"] = args.idempotency_key
    with httpx.Client(base_url=args.base_url, headers=headers, follow_redirects=False,
                      timeout=httpx.Timeout(300, connect=15), trust_env=False, transport=transport) as client:
        if args.command in {"request", "availability"}:
            if args.command == "availability":
                # Read the observation unchanged; this is not an admission or rental action.
                response = client.get("/v1/generation-availability")
            else:
                path = api_path(args.path)
                body = json.loads(Path(args.json_file).read_text(encoding="utf-8")) if args.json_file else None
                response = client.request(args.method, path, json=body)
            check_response(response)
            if len(response.content) > 8 * 1024 * 1024:
                raise ValueError("JSON response exceeded the expected limit")
            write_json(response.json(), args.output)
        elif args.command == "upload":
            session = getattr(args, "session", None)
            if not session:
                identity(args.project)
            identity(args.asset_id)
            path = Path(args.file)
            if not path.is_file() or path.stat().st_size > 512 * 1024 * 1024:
                raise ValueError("Input must be an existing file of at most 512 MiB")
            with path.open("rb") as source:
                target = "/v1/quick-chat/sessions/"+identity(session)+"/assets" if session else "/v1/assets"
                data = {"client_asset_id": args.asset_id}
                if not session:
                    data["client_project_id"] = args.project
                response = client.post(target, data=data, files={"file": (path.name, source)})
            if response.status_code == 422:
                raise ValueError("Upload returned HTTP 422. Check /v1/capabilities upload_constraints and the original receipt; do not upload a new ID blindly. Invalid media needs an explicit source correction, not repeated resume attempts. Server details suppressed.")
            check_response(response)
            write_json(response.json(), args.output)
        elif args.command == "resume-upload":
            write_json(resume_upload(client, args), args.output)
        elif args.command == "poll":
            write_json(poll_job(client, args, clock, sleep), args.output)
        elif args.command == "download":
            write_json(download(client, args, transport, resolver))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--registry-root")
    parser.add_argument("--profile")
    parser.add_argument("--connection", help="Activated one-time connection ID in OS-protected storage")
    commands = parser.add_subparsers(dest="command", required=True)
    availability = commands.add_parser("availability", help="Read authenticated profile/mode availability once; never submit or rent")
    availability.add_argument("--output")
    req = commands.add_parser("request")
    req.add_argument("method", choices=["GET", "POST", "PUT", "PATCH", "DELETE"])
    req.add_argument("path")
    req.add_argument("--json-file")
    req.add_argument("--idempotency-key")
    req.add_argument("--output")
    upload = commands.add_parser("upload")
    upload_target = upload.add_mutually_exclusive_group(required=True)
    upload_target.add_argument("--project")
    upload_target.add_argument("--session")
    upload.add_argument("--asset-id", required=True)
    upload.add_argument("--file", required=True)
    upload.add_argument("--output")
    resume = commands.add_parser("resume-upload", help="Find and resume the original accepted upload; never re-upload bytes")
    resume_target = resume.add_mutually_exclusive_group(required=True)
    resume_target.add_argument("--project")
    resume_target.add_argument("--session")
    resume.add_argument("--asset-id", required=True, help="Original stable client_asset_id, not the receipt ID")
    resume.add_argument("--output")
    poll = commands.add_parser("poll", help="Bounded GET-only polling; no submissions or automatic adoption")
    poll.add_argument("--job", required=True)
    poll.add_argument("--max-wait", type=float, default=600)
    poll.add_argument("--interval", type=float, default=5)
    poll.add_argument("--output")
    download = commands.add_parser("download")
    download.add_argument("path")
    download.add_argument("--output", required=True)
    download.add_argument("--sha256")
    download.add_argument("--max-bytes", type=int, default=512 * 1024 * 1024)
    try:
        run(parser.parse_args())
    except ValueError as error:
        # JSON parser errors may contain user content; do not print those errors.
        print("Invalid JSON input or response" if isinstance(error, json.JSONDecodeError) else str(error), file=sys.stderr)
        return 1
    except (Exception, KeyboardInterrupt):
        print("Operation incomplete; details suppressed. Reconcile the original request before retrying a write.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
