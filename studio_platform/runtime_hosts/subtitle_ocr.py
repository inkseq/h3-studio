"""Bounded CPU-only OCR child. Models come from the packaged wheel, never the network."""

import json
import os
from pathlib import Path
import subprocess
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from studio_platform.subtitle_quality import assess, evidence, validate_quality


def inspect_video(path, lines, duration):
    if not lines:
        return evidence('no_dialogue')
    python = Path('/opt/inkseq-ocr/bin/python')
    try:
        run = subprocess.run(
            [
                str(python) if python.is_file() else sys.executable,
                '-B',
                str(Path(__file__).resolve()),
            ],
            input=json.dumps({'path': str(path), 'lines': lines, 'duration': duration}),
            text=True,
            encoding='utf-8',
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            # Pin BLAS pools before NumPy imports in the bounded child. Linux
            # OpenBLAS otherwise reserves enough thread stacks to exhaust RLIMIT_AS.
            # Never change the parent inference process environment.
            env={
                **os.environ,
                "OPENBLAS_NUM_THREADS": "1",
                "OMP_NUM_THREADS": "1",
                "MKL_NUM_THREADS": "1",
            },
            timeout=45,
            check=False,
        )
        if run.returncode or len(run.stdout) > 12000:
            return evidence('failed')
        return validate_quality(json.loads(run.stdout), duration)
    except subprocess.TimeoutExpired:
        return evidence('timeout')
    except (OSError, ValueError, TypeError):
        return evidence('failed')


def scan(value):
    try:
        import cv2
        import numpy as np
        from rapidocr_onnxruntime import RapidOCR
    except ImportError:
        return evidence('unavailable')
    duration = value['duration']
    if not 0 < duration <= 60:
        return evidence('unavailable')
    cv2.setNumThreads(1)
    engine = RapidOCR(
        det_use_cuda=False,
        cls_use_cuda=False,
        rec_use_cuda=False,
        det_use_dml=False,
        cls_use_dml=False,
        rec_use_dml=False,
        intra_op_num_threads=1,
        inter_op_num_threads=1,
        print_verbose=False,
        det_limit_side_len=960,
        det_limit_type='max',
    )
    cap = cv2.VideoCapture(value['path'])
    samples, previous = [], None
    try:
        if not cap.isOpened():
            return evidence('failed')
        for i in range(min(120, int(duration * 2))):
            t = i / 2
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
            ok, frame = cap.read()
            if not ok:
                return evidence('failed')
            h, w = frame.shape[:2]
            bg = (
                cv2.resize(
                    cv2.cvtColor(frame[: h * 2 // 3], cv2.COLOR_BGR2GRAY), (32, 16)
                ).astype(float)
                / 255
            )
            motion = float(np.abs(bg - previous).mean()) if previous is not None else 0
            previous = bg
            crop = frame[h * 2 // 3 :]
            scale = min(1, 960 / w)
            crop = cv2.resize(crop, (round(w * scale), round(crop.shape[0] * scale)))
            detected, _ = engine(crop, use_cls=False)
            texts = []
            for points, text, score in (detected or [])[:24]:
                xs = [p[0] / (w * scale) for p in points]
                ys = [(p[1] / scale + h * 2 // 3) / h for p in points]
                texts.append(((min(xs), min(ys), max(xs), max(ys)), text, float(score)))
            samples.append({'time': t, 'motion': motion, 'texts': texts})
        return assess(samples, value['lines'])
    finally:
        cap.release()


def main():
    import contextlib

    try:
        if sys.platform == 'linux':
            import resource

            resource.setrlimit(resource.RLIMIT_CPU, (40, 42))
            resource.setrlimit(resource.RLIMIT_AS, (4 * 1024**3, 4 * 1024**3))
        value = json.loads(sys.stdin.read(65536))
        with (
            open(os.devnull, 'w') as sink,
            contextlib.redirect_stdout(sink),
            contextlib.redirect_stderr(sink),
        ):
            result = scan(value)
    except Exception:
        result = evidence('failed')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
