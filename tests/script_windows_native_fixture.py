"""Run inside the cached pinned image, stdin = API projection cases (no GPU).

Load only the two pure upstream modules; importing shared.utils would initialize
unrelated model libraries. Neither module is altered or copied into the backend.
"""

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import types

ROOT = Path("/opt/workspace-internal/Wan2GP/shared/utils")
package = types.ModuleType("pinned_fixture")
package.__path__ = [str(ROOT)]
sys.modules[package.__name__] = package
modules = {}
for name in ("frame_scheduler", "prompt_parser"):
    spec = importlib.util.spec_from_file_location(
        "pinned_fixture." + name, ROOT / (name + ".py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    modules[name] = module

cases = json.load(sys.stdin)
for case in cases:
    projected = case["timing"]
    shared = projected["initial_shared_frames"]
    native = modules["frame_scheduler"].build_default_window_plan(
        total_frames=projected["requested_frames"],
        window_size=projected["window_frames"],
        default_overlap=18,
        discard_last_frames=0,
        minimum=107,
        step=17,
        frame_offset=5,
        overlap_offset=1,
        max_overlap=120,
        first_window_overlap=shared,
        first_window_available_overlap=shared,
        initial_shared_frames=shared,
    )
    assert len(native) == len(projected["windows"]), case
    for actual, expected in zip(native, projected["windows"]):
        assert (
            actual["frame_num"],
            actual["overlap_frames"],
            actual["output_frames"],
        ) == (
            expected["generation_frames"],
            expected["overlap_frames"],
            expected["native_contribution_frames"],
        ), (actual, expected)
    # wgp.py 8395-8416 preserves all frames of the first image-conditioned
    # sample; only subsequent windows remove their reuse prefix.
    assembled = native[0]["frame_num"] + sum(
        w["frame_num"] - w["overlap_frames"] for w in native[1:]
    )
    assert assembled == projected["output_frames"], (assembled, projected)
    prompts = [
        "Scene action %s.\n(S1) <d>[Chinese] Line %s</d>" % (i, i)
        for i in range(len(native))
    ]
    parsed = modules["prompt_parser"].split_prompt_units("\n\n".join(prompts), "PW")
    assert parsed == prompts, parsed
print(
    json.dumps(
        {
            "cases": len(cases),
            "status": "pass",
            "gpu": False,
            "native_module_sha256": {
                name: hashlib.sha256((ROOT / (name + ".py")).read_bytes()).hexdigest()
                for name in modules
            },
        }
    )
)
