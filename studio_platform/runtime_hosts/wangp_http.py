"""Private host HTTP assembly; no listener or model starts on import.

Bind the explicit launcher to 127.0.0.1 only. Access is carried by the existing
protected SSH tunnel. This is not the public business API or a second job queue.
"""

from dataclasses import asdict
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import uuid
from fractions import Fraction
import subprocess

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..inference.wangp_contract import InputDescriptor, PreparedRequest
from ..storage import LocalObjectStore
from ..media_process import run_media_process


def private_token_file(path):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError("wangp_invalid_token_file")
    from .wangp_receipts import checked_reader

    with checked_reader(path, path.parent) as source:
        info = os.fstat(source.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or os.name != "nt"
            and info.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
        ):
            raise ValueError("wangp_token_permissions")
        value = source.read(513).strip()
    if (
        not 32 <= len(value) <= 512
        or not value.isascii()
        or any(chr(c).isspace() for c in value)
    ):
        raise ValueError("wangp_invalid_token")
    return value.decode("ascii")


class StagedInputs:
    def __init__(self, root, *, max_bytes=512 * 1024**2):
        self.root = Path(root).absolute()
        if self.root.is_symlink():
            raise ValueError("wangp_linked_input_root")
        self.store = LocalObjectStore(self.root)
        self.max_bytes = max_bytes

    def key(self, descriptor):
        if (
            not isinstance(descriptor, InputDescriptor)
            or descriptor.size_bytes > self.max_bytes
        ):
            raise ValueError("wangp_input_limit")
        # Content-addressed objects are not user filenames or arbitrary paths.
        return "inputs/" + descriptor.sha256 + "/blob"

    def resolve(self, descriptor):
        key = self.key(descriptor)
        digest, size = hashlib.sha256(), 0
        with self.store.open(key) as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                size += len(chunk)
                if size > descriptor.size_bytes:
                    raise ValueError("wangp_staged_input_mismatch")
                digest.update(chunk)
        if size != descriptor.size_bytes or digest.hexdigest() != descriptor.sha256:
            raise ValueError("wangp_staged_input_mismatch")
        # LocalObjectStore deliberately hashes keys; never join a logical key
        # to the root as if it were the actual physical object location.
        return self.store._directory(key) / "blob"

    def save(self, descriptor, source):
        key = self.key(descriptor)
        from ..storage import ObjectAlreadyExists

        try:
            self.store.put(
                key,
                source,
                max_bytes=descriptor.size_bytes,
                expected_sha256=descriptor.sha256,
            )
        except ObjectAlreadyExists:
            # Replay checks the same bytes rather than overwriting a live input.
            pass
        self.resolve(descriptor)

    def image_path(self, descriptor, *, reference=False, deployment_profile_id=None):
        """Expose inspected normalized PNG bytes under a runtime-safe extension.

        WanGP rejects extensionless inputs. The public asset service supplies
        normalized PNGs; this copy preserves their hash and never re-encodes.
        """
        from PIL import Image

        if descriptor.kind != "image":
            raise ValueError("wangp_image_input_required")
        self.resolve(descriptor)
        key = self.key(descriptor)
        with self.store.open(key) as source, Image.open(source) as picture:
            if (
                picture.format != "PNG"
                or getattr(picture, "n_frames", 1) != 1
                or not 256 <= picture.width <= 5760
                or not 256 <= picture.height <= 5760
            ):
                raise ValueError("wangp_normalized_png_required")
            if reference:
                self._reference_dimensions(
                    picture.width, picture.height, deployment_profile_id
                )
            picture.verify()
        return self._typed_copy(descriptor, "images", ".png")

    @staticmethod
    def _reference_dimensions(width, height, deployment_profile_id=None):
        maximum, pixels = 832, 832 * 480
        if deployment_profile_id is not None:
            from ..runtime_catalog import get_profile

            get_profile(deployment_profile_id)
            maximum, pixels = 5760, 5760**2
        if (
            type(width) is not int
            or type(height) is not int
            or min(width, height) < 256
            or max(width, height) > maximum
            or width * height > pixels
            or not 0.4 <= width / height <= 2.5
        ):
            raise ValueError("wangp_ref_invalid_dimensions")

    def _probe_reference(self, descriptor, kind):
        if descriptor.kind != kind:
            raise ValueError("wangp_typed_input_kind_mismatch")
        path = self.resolve(descriptor)
        # Force a local demuxer, suppress all diagnostic/media text, and inspect
        # the actual hash-bound object rather than trusting uploaded metadata.
        args = [
            "ffprobe",
            "-v",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-f",
            "mov" if kind == "video" else "wav",
        ]
        if kind == "video":
            args += ["-enable_drefs", "0", "-use_absolute_path", "0", "-count_frames"]
        args += [
            "-max_streams",
            "4",
            "-show_entries",
            "stream=codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate,nb_read_frames,duration,sample_rate,channels:format=duration",
            "-of",
            "json",
            str(path),
        ]
        try:
            result = run_media_process(
                args,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=20,
                memory_bytes=512 * 1024**2,
            )
            if len(result.stdout) > 16384:
                raise ValueError()
            value = json.loads(result.stdout)
            streams = value["streams"]
            if (
                not isinstance(streams, list)
                or len(streams) != 1
                or streams[0]["codec_type"] != kind
            ):
                raise ValueError()
            return streams[0], value.get("format", {})
        except (subprocess.SubprocessError, OSError, ValueError, KeyError, TypeError):
            raise ValueError("wangp_ref_media_probe_rejected") from None

    def video_path(self, descriptor, *, deployment_profile_id=None):
        counts = (56, 73)
        if deployment_profile_id is not None:
            from ..runtime_catalog import get_profile

            get_profile(deployment_profile_id)  # Unknown IDs never widen admission.
            counts = tuple(range(56, 363, 17))
        stream, _ = self._probe_reference(descriptor, "video")
        try:
            self._reference_dimensions(
                stream["width"], stream["height"], deployment_profile_id
            )
            count = int(stream["nb_read_frames"])
            duration = float(stream["duration"])
            if (
                stream["codec_name"] != "h264"
                or count not in counts
                or Fraction(stream["avg_frame_rate"]) != 24
                or Fraction(stream["r_frame_rate"]) != 24
                or not math.isfinite(duration)
                or abs(duration - count / 24) > 0.00001
            ):
                raise ValueError()
        except (ValueError, KeyError, TypeError, ZeroDivisionError, OverflowError):
            raise ValueError("wangp_ref_normalized_silent_video_required") from None
        return self._typed_copy(descriptor, "videos", ".mp4")

    def audio_path(self, descriptor, *, deployment_profile_id=None):
        maximum = 3
        if deployment_profile_id is not None:
            from ..runtime_catalog import get_profile

            get_profile(deployment_profile_id)
            maximum = 15
        stream, info = self._probe_reference(descriptor, "audio")
        try:
            if (
                stream["codec_name"] != "pcm_s16le"
                or int(stream["sample_rate"]) != 32000
                or stream["channels"] != 2
                or not 2 <= float(info["duration"]) <= maximum
            ):
                raise ValueError()
        except (ValueError, KeyError, TypeError, OverflowError):
            raise ValueError("wangp_ref_normalized_audio_required") from None
        return self._typed_copy(descriptor, "audios", ".wav")

    def validate_reference_totals(self, descriptors, *, deployment_profile_id):
        """Recheck aggregate actual bytes before upstream can trim multiple refs."""
        from ..runtime_catalog import get_profile

        get_profile(deployment_profile_id)
        video_frames, audio_seconds = 0, 0
        try:
            for descriptor in descriptors:
                if descriptor.kind == 'video':
                    stream, _ = self._probe_reference(descriptor, 'video')
                    video_frames += int(stream['nb_read_frames'])
                elif descriptor.kind == 'audio':
                    _, info = self._probe_reference(descriptor, 'audio')
                    duration = float(info['duration'])
                    if not math.isfinite(duration) or duration < 2:
                        raise ValueError()
                    audio_seconds += duration
        except (ValueError, KeyError, TypeError, OverflowError):
            raise ValueError('wangp_ref_media_probe_rejected') from None
        if video_frames > 362:
            raise ValueError('wangp_ref_total_video_duration_exceeded')
        if audio_seconds > 15:
            raise ValueError('wangp_ref_total_audio_duration_exceeded')

    @staticmethod
    def _full_profile(deployment_profile_id):
        from ..runtime_catalog import get_profile

        if (
            get_profile(deployment_profile_id).get('compiler_epoch')
            != 'full-controls-v2'
        ):
            raise ValueError('wangp_full_profile_required')

    @staticmethod
    def _full_probe(path, kind):
        """Local normalized-container probe; never accepts URLs or demuxer lists."""
        args = [
            'ffprobe',
            '-v',
            'error',
            '-protocol_whitelist',
            'file,pipe',
            '-f',
            'mov' if kind == 'video' else 'wav',
        ]
        if kind == 'video':
            args += ['-enable_drefs', '0', '-use_absolute_path', '0', '-count_frames']
        args += [
            '-max_streams',
            '4',
            '-show_entries',
            'stream=codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate,nb_read_frames,duration,sample_rate,channels:format=duration',
            '-of',
            'json',
            str(path),
        ]
        try:
            result = run_media_process(
                args,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=20,
                memory_bytes=512 * 1024**2,
            )
            if len(result.stdout) > 16384:
                raise ValueError()
            data = json.loads(result.stdout)
            if not isinstance(data, dict) or not isinstance(data.get('streams'), list):
                raise ValueError()
            return data
        except (subprocess.SubprocessError, OSError, ValueError, KeyError, TypeError):
            raise ValueError('wangp_ref_media_probe_rejected') from None

    def reference_metadata(self, descriptor, *, deployment_profile_id):
        """Facts from the actual staged hash-bound bytes for the additive epoch."""
        self._full_profile(deployment_profile_id)
        path = self.resolve(descriptor)
        kind = descriptor.kind
        try:
            if kind == 'image':
                from PIL import Image
                from .wangp_receipts import checked_reader

                with (
                    checked_reader(path, self.root) as source,
                    Image.open(source) as image,
                ):
                    if image.format != 'PNG' or getattr(image, 'n_frames', 1) != 1:
                        raise ValueError()
                    width, height = image.size
                    self._reference_dimensions(width, height, deployment_profile_id)
                    image.verify()
                return {
                    'kind': 'image',
                    'model_ready': True,
                    'width': width,
                    'height': height,
                }
            data = self._full_probe(path, kind)
            streams = data['streams']
            audio = [s for s in streams if s.get('codec_type') == 'audio']
            video = [s for s in streams if s.get('codec_type') == 'video']
            if kind == 'audio':
                if len(streams) != 1 or len(audio) != 1:
                    raise ValueError()
                track = audio[0]
                duration = float(data['format']['duration'])
                if (
                    track['codec_name'] != 'pcm_s16le'
                    or int(track['sample_rate']) != 32000
                    or track['channels'] != 2
                    or not math.isfinite(duration)
                    or not 2 <= duration <= 15
                ):
                    raise ValueError()
                return {
                    'kind': 'audio',
                    'model_ready': True,
                    'duration': duration,
                    'sample_rate': 32000,
                    'channels': 2,
                }
            if (
                kind != 'video'
                or len(video) != 1
                or len(audio) > 1
                or len(streams) != len(video) + len(audio)
            ):
                raise ValueError()
            track = video[0]
            width, height = track['width'], track['height']
            count = int(track['nb_read_frames'])
            duration = float(track['duration'])
            self._reference_dimensions(width, height, deployment_profile_id)
            if (
                track['codec_name'] != 'h264'
                or count not in range(56, 363, 17)
                or Fraction(track['avg_frame_rate']) != 24
                or Fraction(track['r_frame_rate']) != 24
                or not math.isfinite(duration)
                or abs(duration - count / 24) > 0.00001
            ):
                raise ValueError()
            if audio:
                a = audio[0]
                audio_duration = float(a['duration'])
                if (
                    a['codec_name'] != 'aac'
                    or int(a['sample_rate']) != 32000
                    or a['channels'] != 2
                    or not math.isfinite(audio_duration)
                    or audio_duration <= 0
                    or abs(audio_duration - duration) > 0.1
                ):
                    raise ValueError()
            return {
                'kind': 'video',
                'model_ready': True,
                'width': width,
                'height': height,
                'frame_count': count,
                'fps': 24,
                'duration': duration,
                'has_audio': bool(audio),
            }
        except (
            subprocess.SubprocessError,
            OSError,
            ValueError,
            KeyError,
            TypeError,
            ZeroDivisionError,
            OverflowError,
        ):
            raise ValueError('wangp_full_normalized_media_required') from None

    def full_input(self, descriptor, *, deployment_profile_id):
        metadata = self.reference_metadata(
            descriptor, deployment_profile_id=deployment_profile_id
        )
        plural, extension = {
            'image': ('images', '.png'),
            'video': ('videos', '.mp4'),
            'audio': ('audios', '.wav'),
        }[descriptor.kind]
        return self._typed_copy(descriptor, plural, extension), metadata

    def audio_from_video_path(self, descriptor, *, deployment_profile_id):
        """Extract one selected track locally; never alter the staged video.

        Source SHA and derived SHA both bind the private filename. Repeated
        resolution verifies the newly extracted bytes against an existing copy;
        it never overwrites that copy or creates an owner asset/upload.
        """
        from .wangp_receipts import checked_directory, checked_reader, sync_directory

        metadata = self.reference_metadata(
            descriptor, deployment_profile_id=deployment_profile_id
        )
        if descriptor.kind != 'video' or not metadata['has_audio']:
            raise ValueError('wangp_selected_video_has_no_audio')
        source = self.resolve(descriptor)
        folder = checked_directory(self.root / 'typed-video-audio', create=True)
        temporary = folder / (uuid.uuid4().hex + '.pending.wav')
        created = False
        try:
            with temporary.open('xb'):
                pass
            created = True
            os.chmod(temporary, 0o600)
            args = [
                'ffmpeg',
                '-v',
                'error',
                '-nostdin',
                '-y',
                '-threads',
                '1',
                '-protocol_whitelist',
                'file,pipe',
                '-f',
                'mov',
                '-enable_drefs',
                '0',
                '-use_absolute_path',
                '0',
                '-i',
                str(source),
                '-map',
                '0:a:0',
                '-vn',
                '-ar',
                '32000',
                '-ac',
                '2',
                '-c:a',
                'pcm_s16le',
                '-f',
                'wav',
                '-fs',
                str(min(self.max_bytes, 4 * 1024**2) + 1),
                str(temporary),
            ]
            run_media_process(
                args,
                timeout=30,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                memory_bytes=512 * 1024**2,
            )
            data = self._full_probe(temporary, 'audio')
            streams = data['streams']
            if len(streams) != 1:
                raise ValueError()
            track = streams[0]
            duration = float(data['format']['duration'])
            if (
                track['codec_type'] != 'audio'
                or track['codec_name'] != 'pcm_s16le'
                or int(track['sample_rate']) != 32000
                or track['channels'] != 2
                or not math.isfinite(duration)
                or abs(duration - metadata['duration']) > 0.1
            ):
                raise ValueError()
            checksum, size = hashlib.sha256(), 0
            with checked_reader(temporary, self.root) as stream:
                while chunk := stream.read(1024 * 1024):
                    size += len(chunk)
                    if size > min(self.max_bytes, 4 * 1024**2):
                        raise ValueError()
                    checksum.update(chunk)
            # Windows rejects fsync on a read-only descriptor; reopen only this
            # freshly created private temporary, without modifying its bytes.
            with temporary.open('r+b') as stream:
                os.fsync(stream.fileno())
            final = folder / (descriptor.sha256 + '-' + checksum.hexdigest() + '.wav')
            try:
                os.link(temporary, final)
            except FileExistsError:
                pass
            temporary.unlink()
            created = False
            digest, total = hashlib.sha256(), 0
            with checked_reader(final, self.root) as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
                    total += len(chunk)
                    if total > size:
                        raise ValueError()
            if total != size or digest.hexdigest() != checksum.hexdigest():
                raise ValueError()
            sync_directory(folder)
            return final
        except (
            subprocess.SubprocessError,
            OSError,
            ValueError,
            KeyError,
            TypeError,
            OverflowError,
        ):
            raise ValueError('wangp_video_audio_extraction_rejected') from None
        finally:
            if created and temporary.exists():
                temporary.unlink()

    def _typed_copy(self, descriptor, plural, extension):
        from .wangp_receipts import checked_directory, checked_reader, sync_directory

        key = self.key(descriptor)
        folder = checked_directory(self.root / ("typed-" + plural), create=True)
        final = folder / (descriptor.sha256 + extension)
        if not final.exists():
            temporary = folder / (uuid.uuid4().hex + ".part")
            try:
                checksum, size = hashlib.sha256(), 0
                with self.store.open(key) as source, temporary.open("xb") as target:
                    while chunk := source.read(1024 * 1024):
                        size += len(chunk)
                        if size > descriptor.size_bytes:
                            raise ValueError("wangp_staged_input_mismatch")
                        checksum.update(chunk)
                        target.write(chunk)
                    target.flush()
                    os.fsync(target.fileno())
                if (
                    size != descriptor.size_bytes
                    or checksum.hexdigest() != descriptor.sha256
                ):
                    raise ValueError("wangp_staged_input_mismatch")
                os.replace(temporary, final)
                sync_directory(folder)
            finally:
                if temporary.exists():
                    temporary.unlink()
        checksum, size = hashlib.sha256(), 0
        with checked_reader(final, folder) as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                size += len(chunk)
                if size > descriptor.size_bytes:
                    raise ValueError("wangp_staged_input_mismatch")
                checksum.update(chunk)
        if size != descriptor.size_bytes or checksum.hexdigest() != descriptor.sha256:
            raise ValueError("wangp_staged_input_mismatch")
        return final


def create_app(host, inputs, *, token):
    if not isinstance(token, str) or not 32 <= len(token) <= 512 or not token.isascii():
        raise ValueError("wangp_private_token_required")
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def authenticate(request, call_next):
        provided = request.headers.get("authorization", "")
        if not hmac.compare_digest(provided.encode(), ("Bearer " + token).encode()):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            return await call_next(request)
        except Exception:
            # Never return raw upstream exception/event/prompt text.
            return JSONResponse(
                {"error": "wangp_private_operation_unknown"}, status_code=503
            )

    @app.get("/v1/readiness")
    def readiness():
        return asdict(host.readiness())

    @app.post("/v1/operations")
    async def submit(request: Request):
        incarnation = request.headers.get("x-wangp-incarnation")
        if incarnation is not None and (
            not re.fullmatch(r"[0-9a-f]{32}", incarnation)
            or incarnation != getattr(host, "incarnation", None)
        ):
            # WanGPHost's incarnation is immutable for its process lifetime.
            # This admission precondition closes the readiness/POST race;
            # reads of old durable receipts remain available after restart.
            return JSONResponse(
                {"error": "wangp_runtime_incarnation_mismatch"}, status_code=409
            )
        try:
            length = int(request.headers.get("content-length", "0"))
            if not 0 < length <= 2 * 1024**2:
                raise ValueError()
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > length:
                    raise ValueError()
            value = json.loads(body)
            value["inputs"] = tuple(
                InputDescriptor(**v) for v in value.get("inputs", ())
            )
            prepared = PreparedRequest(**value)
            for descriptor in prepared.inputs:
                inputs.resolve(descriptor)
        except Exception:
            # This status is only used before host.submit can dispatch.
            return JSONResponse({"error": "wangp_request_rejected"}, status_code=422)
        return host.submit(prepared).to_dict()

    @app.get("/v1/operations/{operation_id}")
    def inspect(operation_id: str):
        from ..inference.wangp_http import _operation

        receipt = host.inspect(_operation(operation_id))
        if receipt is None:
            return JSONResponse({"error": "not_found"}, status_code=404)
        return receipt.to_dict()

    async def delivery_body(request):
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 65536:
                raise ValueError('wangp_delivery_request_invalid')
        if not raw:
            raise ValueError('wangp_delivery_request_invalid')
        value = json.loads(raw)
        if type(value) is not dict:
            raise ValueError('wangp_delivery_request_invalid')
        return value

    @app.post('/v1/operations/{operation_id}/delivery/prepare')
    async def prepare_delivery(operation_id: str, request: Request):
        from ..inference.wangp_http import _operation
        from starlette.concurrency import run_in_threadpool

        try:
            value = await delivery_body(request)
            if value.get('identity', {}).get('operation_id') != _operation(
                operation_id
            ):
                raise ValueError
            return await run_in_threadpool(host.prepare_delivery, value)
        except Exception:
            return JSONResponse(
                {'error': 'wangp_delivery_prepare_unconfirmed'}, status_code=409
            )

    @app.post('/v1/operations/{operation_id}/delivery/upload')
    async def upload_delivery(operation_id: str, request: Request):
        from ..inference.wangp_http import _operation
        from starlette.concurrency import run_in_threadpool

        try:
            value = await delivery_body(request)
            if set(value) != {'identity', 'fingerprint', 'targets'} or value[
                'identity'
            ].get('operation_id') != _operation(operation_id):
                raise ValueError
            return await run_in_threadpool(
                host.upload_delivery,
                value['identity'],
                value['fingerprint'],
                value['targets'],
            )
        except Exception:
            return JSONResponse(
                {'error': 'wangp_delivery_upload_unconfirmed'}, status_code=409
            )

    @app.get('/v1/operations/{operation_id}/delivery')
    def delivery_status(
        operation_id: str,
        job_id: str,
        attempt_tag: str,
        request_hash: str,
        manifest_digest: str,
        slot_key: str,
        incarnation: str,
    ):
        from ..inference.wangp_http import _operation

        try:
            identity = {
                'operation_id': _operation(operation_id),
                'job_id': job_id,
                'attempt_tag': attempt_tag,
                'request_hash': request_hash,
                'manifest_digest': manifest_digest,
                'slot_key': slot_key,
                'incarnation': incarnation,
            }
            value = host.delivery_status(identity)
            return (
                value
                if value is not None
                else JSONResponse({'error': 'not_found'}, status_code=404)
            )
        except Exception:
            return JSONResponse(
                {'error': 'wangp_delivery_status_unconfirmed'}, status_code=409
            )

    @app.post("/v1/operations/{operation_id}/cancel")
    def cancel(operation_id: str):
        from ..inference.wangp_http import _operation

        return {"acknowledged": host.cancel(_operation(operation_id)) is True}

    @app.get("/v1/operations/{operation_id}/artifacts/{kind}")
    def artifact(operation_id: str, kind: str):
        from ..inference.wangp_http import _operation

        if kind not in {"video", "audio"}:
            return JSONResponse({"error": "not_found"}, status_code=404)
        return StreamingResponse(
            host.read_artifact(_operation(operation_id), kind),
            media_type="application/octet-stream",
        )

    @app.put("/v1/inputs/{handle}")
    async def stage(handle: str, request: Request):
        try:
            header = request.headers.get("x-wangp-input", "")
            if len(header) > 4096:
                raise ValueError()
            descriptor = InputDescriptor(**json.loads(header))
            if (
                handle != descriptor.handle
                or int(request.headers.get("content-length", "0"))
                != descriptor.size_bytes
            ):
                raise ValueError()
            inputs.key(descriptor)
            # Bounded spool does not accumulate complete user videos in RAM.
            with tempfile.TemporaryFile() as spool:
                size, checksum = 0, hashlib.sha256()
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > descriptor.size_bytes:
                        raise ValueError()
                    spool.write(chunk)
                    checksum.update(chunk)
                if (
                    size != descriptor.size_bytes
                    or checksum.hexdigest() != descriptor.sha256
                ):
                    raise ValueError()
                spool.seek(0)
                inputs.save(descriptor, spool)
            return asdict(descriptor)
        except Exception:
            return JSONResponse({"error": "wangp_input_rejected"}, status_code=422)

    return app
