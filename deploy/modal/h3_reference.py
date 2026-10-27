"""Serverless test (owner 2026-10-27): TaoMate 5-step H3 (pruned INT8, WanGP 0e58385) on Modal.
Measures cold start, model load and warm generation time per 480P 5 s clip. Weights live in a
Modal Volume so only the first-ever run downloads them. Test budget: within USD 20.

uv run --with modal modal run modal_h3.py::main --runs 3
"""
import json
import time

import modal

GPU = "L40S"
app = modal.App("inkseq-h3-serverless-test")
vol = modal.Volume.from_name("inkseq-h3-ckpts", create_if_missing=True)
image = (
    modal.Image.from_registry("nvidia/cuda:12.8.1-devel-ubuntu22.04", add_python="3.11")
    .apt_install("git", "ffmpeg", "libgl1", "libglib2.0-0", "clang", "build-essential")
    .pip_install(
        "torch==2.7.1", "torchvision==0.22.1", "torchaudio==2.7.1",
        index_url="https://download.pytorch.org/whl/cu128",
    )
    .run_commands(
        "git clone https://github.com/deepbeepmeep/Wan2GP /opt/Wan2GP",
        "cd /opt/Wan2GP && git checkout 0e58385",
        "cd /opt/Wan2GP && pip install -r requirements.txt",
        "rm -rf /opt/Wan2GP/ckpts /opt/Wan2GP/loras && ln -s /ckpts/ckpts /opt/Wan2GP/ckpts && ln -s /ckpts/loras /opt/Wan2GP/loras",
    )
)

ROOT = "/opt/Wan2GP"
TAO = ("https://huggingface.co/DeepBeepMeep/MiniMax-H3/resolve/main/loras/"
       "minimax_h3_taomate_3step_lora_avg_rank_19_bf16.safetensors")
CFG = {"attention_mode": "sdpa", "audio_output_codec": "aac_128", "boost": 1, "compile": "",
       "fit_canvas": 2, "int8_kernels": "kitchen", "kernel_precision": "strict", "mixed_precision": "0",
       "perc_reserved_mem_max": 20, "preload_model_policy": [], "profile": 4, "read_ahead": False,
       "smart_memory_pinning": True, "text_encoder_quantization": "int8", "transformer_dtype_policy": "bf16",
       "transformer_quantization": "int8", "vae_config": 3, "vae_precision": "16", "video_container": "mp4",
       "video_output_codec": "libx264_8", "video_preload_in_VRAM": 0, "video_profile": 4}
PROMPT = ("integrated_multimodal_description: [Shot 1] A slow aerial push over misty green tea terraces on a "
          "mountainside at sunrise; small figures of pickers with bamboo baskets move between the rows as golden "
          "light breaks through the clouds. Clean frame: no subtitles, captions or any on-screen text.\n"
          "overall_soundscape: Wind across the slopes, distant birdsong.\nnon_diegetic_music: N/A")


@app.cls(gpu=GPU, image=image, volumes={"/ckpts": vol}, timeout=3600, scaledown_window=60)
class H3:
    @modal.enter()
    def load(self):
        import os, sys
        t0 = time.time()
        os.makedirs("/ckpts/ckpts", exist_ok=True)
        os.makedirs("/ckpts/loras", exist_ok=True)
        sys.path.insert(0, ROOT)
        os.chdir(ROOT)
        json.dump(CFG, open("/tmp/wgp_config.json", "w"))
        from shared import api
        self.s = api.init(root=ROOT, config_path="/tmp/wgp_config.json", output_dir="/tmp/out",
                          cli_args=["--attention", "sdpa", "--profile", "4", "--perc-reserved-mem-max", "0.2"],
                          console_output=False)
        self.init_s = round(time.time() - t0, 1)
        self.started = time.time()
        self.runs = 0

    @modal.method()
    def generate(self, seed: int, resolution: str = "832x480", frames: int = 124):
        t0 = time.time()
        v = self.s.get_default_settings("minimax_h3_fl2va_pruned")
        v.update({"activated_loras": [TAO], "loras_multipliers": "1.0", "num_inference_steps": 5,
                  "guidance_scale": 1, "resolution": resolution, "video_length": frames,
                  "multi_prompts_gen_type": "FG", "prompt": PROMPT, "seed": seed})
        if frames > 124:  # long shot: 5 s windows, overlap 18 (owner 2026-10-25)
            v.update({"sliding_window_size": 124, "sliding_window_overlap": 18})
        r = self.s.submit_task(v).result()
        vol.commit()  # keep any first-run downloads for the next cold start
        self.runs += 1
        import os
        mp4s = [f for f in r.generated_files if f.endswith(".mp4")]
        video = max(mp4s, key=os.path.getsize) if mp4s else None
        data = open(video, "rb").read() if video else b""
        return {"ok": r.success, "errors": [str(e)[:300] for e in r.errors][:2], "gen_s": round(time.time() - t0, 1),
                "init_s": self.init_s, "run_in_container": self.runs, "bytes": len(data), "files": [os.path.basename(f) for f in mp4s]}, data


@app.local_entrypoint()
def main(runs: int = 2, seed: int = 701, resolution: str = "832x480", frames: int = 124):
    h = H3()
    out = []
    for i in range(runs):
        t0 = time.time()
        info, data = h.generate.remote(seed + i, resolution, frames)
        info["wall_s"] = round(time.time() - t0, 1)
        if data:
            open(f"modal_h3_{resolution}_{frames}_{seed + i}.mp4", "wb").write(data)
        print(json.dumps(info), flush=True)
        out.append(info)
    json.dump(out, open(f"modal_h3_runs_{int(time.time())}.json", "w"), indent=1)
