"""ComfyUI node: per-job attention for the H3 diffusion model (sdpa | sage | sage16).

The server starts without --use-sage-attention; this node sets the model's own attention override, so one
worker serves all modes without a restart. sage16 keeps sage2's int8 QK but computes PV in fp16 with fp32
accumulation: sage2's automatic fp8 PV kernel turns 2K frames ~94-118 into noise on sm89/sm90 (L40S, H100,
H200; Modal measurement 2026-10-30), while sm120 (5090) is clean.
"""

import comfy.ldm.modules.attention as attention


def _sage16_kernel(q, k, v, **kwargs):
    from sageattention import sageattn_qk_int8_pv_fp16_cuda

    kwargs.pop("attn_mask", None)  # H3 calls attention without a mask
    return sageattn_qk_int8_pv_fp16_cuda(q, k, v, pv_accum_dtype="fp32", **kwargs)


def attention_sage16(*args, **kwargs):
    # attention_sage calls the module-level sageattn; ComfyUI runs one prompt at a time.
    original = attention.sageattn
    attention.sageattn = _sage16_kernel
    try:
        return attention.attention_sage(*args, **kwargs)
    finally:
        attention.sageattn = original


MODES = {
    "sdpa": lambda: attention.attention_pytorch,
    "sage": lambda: attention.attention_sage,
    "sage16": lambda: attention_sage16,
}


class InkseqAttention:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"model": ("MODEL",), "mode": (list(MODES),)}}

    RETURN_TYPES = ("MODEL",)
    FUNCTION = "patch"
    CATEGORY = "inkseq"

    def patch(self, model, mode):
        if mode != "sdpa" and not attention.SAGE_ATTENTION_IS_AVAILABLE:
            raise RuntimeError("sageattention is not importable")
        patched = model.clone()
        patched.set_model_optimized_attention(MODES[mode]())
        return (patched,)


NODE_CLASS_MAPPINGS = {"InkseqAttention": InkseqAttention}
