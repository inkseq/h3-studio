# Modal TaoMate reference (#688)

`h3_reference.py` is the measured prototype from the 2026-10-27 serverless test. It is **not** the production worker. Production Modal runs the existing platform worker loop with the same image and volume ideas (#688: Modal is another worker host).

What it proves (L40S, TaoMate 5 steps, WanGP 0e58385, pruned INT8):

| Case | Load | Generate | Submit → video |
|---|---|---|---|
| 480P warm | – | 36 s | ~39 s |
| 480P cold (weights cached in Volume) | 52 s | 60 s | ~2 min |
| 768P cold | 78 s | 141 s | ~4 min |
| 15 s long shot cold (`frames=361`, windows 124/18) | 75 s | 190 s | ~4.6 min |

Notes for the production worker:
- Image: CUDA 12.8 devel, Python 3.11, torch 2.7.1 cu128, Wan2GP pinned at `0e58385`, `pip install -r requirements.txt`. **`clang` and `build-essential` are required**: insightface compiles, and Modal's Python is clang-built.
- Weights and LoRAs live in a Modal Volume (`ckpts/`, `loras/` symlinked). Only the first run downloads (~6 min). Call `vol.commit()` after it.
- The `shared.api` result lists one mp4 per sliding window **plus** the final file, so pick the full one, not the first. The 361-frame request produced 14.2 s; reconcile the window frame count with the PW compiler.
- `scaledown_window` 60 s gives about 2 min cold starts; keep warm-pool decisions in the platform rule, not here.

Run (needs a Modal token; costs real GPU time):
`uv run --with modal modal run deploy/modal/h3_reference.py::main --runs 2 [--resolution 1344x768] [--frames 361]`
