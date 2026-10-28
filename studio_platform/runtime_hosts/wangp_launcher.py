"""Explicit private engine entry point; importing it is inert.

Provisioning, authorization windows and rental budgets remain controller-owned.
This process accepts no cloud credentials and never rents or downloads a model.
"""

import argparse
from contextlib import redirect_stderr, redirect_stdout
import json
import math
import os
from pathlib import Path
import time

from .wangp_startup import write_startup_failure

# Keep dependency/import failures observable before a journal can be created.
# No exception text is printed or persisted by the startup receipt.
_EARLY_IMPORT_FAILURE = None
try:
    from ..inference.wangp_contract import EngineManifest
    from ..inference.wangp_factory import read_document
    from .wangp import WanGPHost
    from .wangp_http import StagedInputs, create_app, private_token_file
    from .wangp_receipts import (
        ReceiptJournal,
        checked_directory,
        VERIFICATION_RECEIPT,  # noqa: F401 - public receipt filename used by launcher callers.
        read_verification_receipt,  # noqa: F401 - public launcher receipt reader.
        write_verification_receipt,
    )
except Exception as error:
    _EARLY_IMPORT_FAILURE = error


class PendingSession:
    def __init__(self):
        self.delegate = None

    def is_idle(self):
        return self.delegate is not None and self.delegate.is_idle() is True

    def preparation(self):
        # The host is bound to this wrapper before runtime initialization.
        # Forward optional observations without loading or submitting a model;
        # WanGPHost remains responsible for sanitizing the public projection.
        probe = getattr(self.delegate, "preparation", None)
        return probe() if callable(probe) else None

    def submit_task(self, settings):
        if self.delegate is None:
            raise ValueError("wangp_runtime_not_initialized")
        return self.delegate.submit_task(settings)

    def close_when_idle(self):
        if self.delegate is None:
            raise ValueError("wangp_runtime_not_initialized")
        return self.delegate.close_when_idle()


def shutdown_owned_host(
    host,
    session,
    *,
    grace_seconds=180,
    terminate=None,
    clock=time.monotonic,
    sleeper=time.sleep,
):
    """Keep the process lock until the runtime is closed or the process dies.

    Stopping HTTP is not evidence of GPU stop. Never cancel or erase receipts:
    normal completion gets a grace period; an uncertain runtime requires whole
    process termination, which also releases its OS-owned journal lock.
    """
    if (
        type(grace_seconds) not in (int, float)
        or not math.isfinite(grace_seconds)
        or not 0 < grace_seconds <= 3600
    ):
        raise ValueError("wangp_invalid_shutdown_grace")
    if session is None:
        host.close()  # The upstream initialization was never entered.
        return

    def terminate_process():
        (terminate or os._exit)(1)
        # A test hook or broken termination primitive must not fall through
        # to host.close. Production os._exit does not return.
        raise RuntimeError("wangp_process_termination_required")

    deadline = clock() + grace_seconds
    while True:
        try:
            if session.is_idle() is True:
                session.close_when_idle()
                host.close()
                return
        except (KeyboardInterrupt, SystemExit):
            terminate_process()
        except Exception:
            # A close/idle error is uncertainty, not permission to release the
            # slot. Retain the same ownership until the process is terminated.
            pass
        remaining = deadline - clock()
        if remaining <= 0:
            terminate_process()
        try:
            sleeper(min(0.25, remaining))
        except (KeyboardInterrupt, SystemExit):
            terminate_process()


def resolve_inputs(prepared, inputs, manifest=None, model_root=None):
    settings = prepared.settings
    from ..inference.wangp_compiler import MAX_SEED

    # Protect both legacy and native manifests before resolving assets or
    # dispatching. Never truncate a previously accepted request's seed.
    seed = settings.get("seed")
    if type(seed) is not int or not 0 <= seed <= MAX_SEED:
        raise ValueError("wangp_invalid_seed")
    profile_id = None
    if manifest is not None:
        from ..inference.wangp_compiler import (
            COMPILER_ID as FL_COMPILER_ID,
            PROFILE_ID as FL_PROFILE_ID,
        )
        from ..inference.wangp_ref_compiler import (
            COMPILER_ID as REF_COMPILER_ID,
            PROFILE_ID as REF_PROFILE_ID,
        )

        profile_id = manifest.document.get("deployment_profile_id")
        model = {
            (FL_COMPILER_ID, FL_PROFILE_ID): "minimax_h3_fl2va",
            (REF_COMPILER_ID, REF_PROFILE_ID): "minimax_h3_ref2va",
        }.get((manifest.document["compiler_id"], manifest.document["profile_id"]))
        if profile_id is not None:
            from ..inference.wangp_profile_dispatch import validate_prepared
            from ..runtime_catalog import model_for

            validate_prepared(prepared, manifest)
            model = model_for(profile_id, manifest.document['mode'])['model_type']
        if (
            model is None
            or settings.get("model_type") != model
            or prepared.manifest_digest != manifest.digest
        ):
            raise ValueError("wangp_manifest_binding_mismatch")
        if manifest.document['compiler_id'] == 'sixnine-h3-native-profile-v2':
            from ..inference.wangp_full_compiler import (
                validate_prepared_media,
                audio_source_handles,
            )

            descriptors = {d.handle: d for d in prepared.inputs}
            paths, metadata = {}, {}
            for handle, descriptor in descriptors.items():
                path, facts = inputs.full_input(
                    descriptor, deployment_profile_id=profile_id
                )
                paths[handle] = str(path)
                metadata[descriptor.asset_id] = facts
            # Complete actual-byte checks before materializing selected audio or
            # entering the upstream Session. One input may serve several roles.
            validate_prepared_media(prepared, manifest, metadata)
            audio_paths = {}
            for handle in audio_source_handles(settings):
                descriptor = descriptors[handle]
                audio_paths[handle] = (
                    str(
                        inputs.audio_from_video_path(
                            descriptor, deployment_profile_id=profile_id
                        )
                    )
                    if descriptor.kind == 'video'
                    else paths[handle]
                )
            for field in (
                'image_start',
                'image_end',
                'video_guide',
                'video_guide2',
                'video_guide3',
            ):
                if settings.get(field) is not None:
                    settings[field] = paths[settings[field]]
            for field in ('audio_guide', 'audio_guide2', 'audio_guide3'):
                if settings.get(field) is not None:
                    settings[field] = audio_paths[settings[field]]
            if settings.get('image_refs') is not None:
                settings['image_refs'] = [
                    paths[handle] for handle in settings['image_refs']
                ]
            if settings.get('activated_loras'):
                from ..inference.wangp_full_compiler import acceleration
                from .wangp_session import _contained_file

                turbo = acceleration(profile_id)
                # validate_prepared already bound this list to the profile; the
                # verified file under this slot's model root is the only target.
                if (
                    turbo is None
                    or model_root is None
                    or settings['activated_loras'] != [turbo['path']]
                ):
                    raise ValueError('wangp_manifest_binding_mismatch')
                settings['activated_loras'] = [
                    str(_contained_file(Path(model_root), Path(turbo['path'])))
                ]
            return settings
    handles = {item.handle: item for item in prepared.inputs}
    used = set()
    if settings.get("model_type") in {"minimax_h3_ref2va", "minimax_h3_ref2va_pruned"}:
        blocked = ['image_start', 'image_end', 'video_source', 'audio_source']
        if profile_id is None:
            blocked += ['video_guide2', 'video_guide3', 'audio_guide2', 'audio_guide3']
        if any(settings.get(field) is not None for field in blocked):
            raise ValueError("wangp_ref_unsupported_input_role")

        def resolve(handle, kind):
            if (
                not isinstance(handle, str)
                or handle not in handles
                or handle in used
                or handles[handle].kind != kind
            ):
                raise ValueError("wangp_unbound_input_handle")
            used.add(handle)
            if kind == "image":
                if profile_id is not None:
                    return str(
                        inputs.image_path(
                            handles[handle],
                            reference=True,
                            deployment_profile_id=profile_id,
                        )
                    )
                return str(inputs.image_path(handles[handle], reference=True))
            if profile_id is not None:
                return str(
                    getattr(inputs, kind + "_path")(
                        handles[handle], deployment_profile_id=profile_id
                    )
                )
            return str(getattr(inputs, kind + "_path")(handles[handle]))

        references = settings.get("image_refs")
        if references is not None:
            if not isinstance(references, list) or not 1 <= len(references) <= (
                9 if profile_id else 1
            ):
                raise ValueError("wangp_ref_qualification_count_exceeded")
            settings["image_refs"] = [resolve(handle, "image") for handle in references]
        for kind in ('video', 'audio'):
            for suffix in ('', '2', '3') if profile_id else ('',):
                field = kind + '_guide' + suffix
                if settings.get(field) is not None:
                    settings[field] = resolve(settings[field], kind)
        if used != set(handles) or not used:
            raise ValueError("wangp_unused_input_handle")
        if profile_id is not None and any(
            sum(d.kind == kind for d in handles.values()) > 1
            for kind in ('video', 'audio')
        ):
            inputs.validate_reference_totals(
                tuple(handles.values()), deployment_profile_id=profile_id
            )
        return settings
    for field in ("image_start", "image_end"):
        handle = settings.get(field)
        if handle is not None:
            if handle not in handles:
                raise ValueError("wangp_unbound_input_handle")
            settings[field] = str(inputs.image_path(handles[handle]))
            used.add(handle)
    if used != set(handles):
        raise ValueError("wangp_unused_input_handle")
    return settings


def main(argv=None, *, host_factory=None, session_factory=None, serve=None):
    parser = argparse.ArgumentParser(description="Explicit pinned private WanGP slot")
    for name in (
        "runtime-root",
        "config",
        "manifest",
        "model-root",
        "state-dir",
        "token-file",
    ):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--slot-key", required=True)
    parser.add_argument("--port", type=int, default=8199)
    parser.add_argument("--shutdown-grace-seconds", type=float, default=180)
    parser.add_argument(
        "--create-journal",
        action="store_true",
        help="Only a new authorized slot; fails if a journal already exists",
    )
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--startup-status-file", type=Path)
    parser.add_argument("--startup-id")
    parser.add_argument("--expected-manifest-digest")
    args = parser.parse_args(argv)
    diagnostics = (
        args.startup_status_file,
        args.startup_id,
        args.expected_manifest_digest,
    )
    if any(value is not None for value in diagnostics) and not all(
        value is not None for value in diagnostics
    ):
        parser.error("complete startup identity required")
    if (
        args.startup_status_file is not None
        and not args.startup_status_file.is_absolute()
    ):
        parser.error("absolute startup status path required")
    if any(
        not getattr(args, name).is_absolute()
        for name in (
            "runtime_root",
            "config",
            "manifest",
            "model_root",
            "state_dir",
            "token_file",
        )
    ):
        parser.error("explicit absolute paths required")
    if not 1024 <= args.port <= 65535:
        parser.error("invalid private port")
    if (
        not math.isfinite(args.shutdown_grace_seconds)
        or not 0 < args.shutdown_grace_seconds <= 3600
    ):
        parser.error("invalid shutdown grace")
    host = None
    session_initialization_started = False
    pending = None
    phase = "runtime_imports"
    try:
        if _EARLY_IMPORT_FAILURE is not None:
            raise _EARLY_IMPORT_FAILURE
        from .wangp_session import create_session, verify_runtime

        phase = "runtime_manifest"
        manifest = EngineManifest.from_dict(read_document(args.manifest))
        if (
            args.expected_manifest_digest is not None
            and manifest.digest != args.expected_manifest_digest
        ):
            raise ValueError("wangp_verified_manifest_changed")
        if manifest.document.get("synthetic"):
            raise ValueError("wangp_synthetic_manifest_forbidden")
        if args.verify_only:
            phase = "runtime_verification"
            evidence = verify_runtime(
                args.runtime_root, args.config, args.manifest, args.model_root
            )
            if evidence.get("manifest_digest") != manifest.digest:
                raise ValueError("wangp_verified_manifest_changed")
            print(json.dumps({"state": "runtime_files_verified", **evidence}))
            return 0
        phase = "runtime_token"
        token = private_token_file(args.token_file)
        phase = "runtime_journal"
        state = checked_directory(args.state_dir, create=args.create_journal)
        journal = ReceiptJournal(
            state / "operations.sqlite3",
            slot_key=args.slot_key,
            manifest_digest=manifest.digest,
            create=args.create_journal,
        )
        phase = "runtime_inputs"
        output = checked_directory(state / "upstream-output", create=True)
        inputs = StagedInputs(state / "inputs")
        pending = PendingSession()
        # Acquire durable host ownership BEFORE importing/loading the upstream
        # runtime; a competing process cannot load a second copy into this slot.
        phase = "runtime_host"
        host = (host_factory or WanGPHost)(
            session=pending,
            journal=journal,
            manifest=manifest,
            output_root=output,
            sealed_root=state / "sealed-output",
            settings_resolver=lambda prepared: resolve_inputs(
                prepared, inputs, manifest, model_root=args.model_root
            ),
        )
        phase = "runtime_verification"
        evidence = verify_runtime(
            args.runtime_root, args.config, args.manifest, args.model_root
        )
        if evidence.get("manifest_digest") != manifest.digest:
            raise ValueError("wangp_verified_manifest_changed")
        write_verification_receipt(state, host, evidence)
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        # Upstream console output is not an authorized channel for user prompts.
        with (
            open(os.devnull, "w") as quiet,
            redirect_stdout(quiet),
            redirect_stderr(quiet),
        ):
            phase = "runtime_session_initialization"
            session_initialization_started = True
            if manifest.document.get('deployment_profile_id') is not None:
                pending.delegate = (session_factory or create_session)(
                    args.runtime_root, args.config, output, manifest=manifest
                )
            else:
                pending.delegate = (session_factory or create_session)(
                    args.runtime_root, args.config, output
                )
            phase = "runtime_http_service"
            import uvicorn

            app = create_app(host, inputs, token=token)
            if serve is not None:
                serve(app, host, args.port)
            else:
                uvicorn.run(
                    app,
                    host="127.0.0.1",
                    port=args.port,
                    workers=1,
                    access_log=False,
                    log_level="critical",
                    proxy_headers=False,
                )
        return 0
    except Exception as error:
        if args.startup_status_file is not None:
            try:
                write_startup_failure(
                    args.startup_status_file,
                    slot_key=args.slot_key,
                    manifest_digest=args.expected_manifest_digest,
                    launch_id=args.startup_id,
                    phase=phase,
                    error=error,
                )
            except Exception:
                pass  # Failure to publish is unknown; never expose raw fallback logs.
        print(json.dumps({"state": "wangp_runtime_start_failed"}))
        return 1
    finally:
        if host is not None:
            with (
                open(os.devnull, "w") as quiet,
                redirect_stdout(quiet),
                redirect_stderr(quiet),
            ):
                shutdown_owned_host(
                    host,
                    pending if session_initialization_started else None,
                    grace_seconds=args.shutdown_grace_seconds,
                )


if __name__ == "__main__":
    raise SystemExit(main())
