# Enhance worker (#795): Open-H3-Regenerate 1080P/2K redraw of an H3 draft, as a RunPod Serverless handler.
# Same CUDA stack as the v8 WanGP image (torch 2.10.0+cu130, triton 3.6, deepbeepmeep sage2) and the exact
# ComfyUI + node revisions benchmarked on a 5090 on 2026-10-29 (1080P 70/90 s, 2K 145/199 s per 5 s clip).
# No weights in the image: mount the network volume with models/{diffusion_models,loras,vae,text_encoders,
# latent_upscale_models} (handler.py lists the files). Hosts need an NVIDIA driver with CUDA >= 13.0.
FROM nvidia/cuda:13.0.1-base-ubuntu24.04
ENV DEBIAN_FRONTEND=noninteractive PIP_NO_CACHE_DIR=1 PIP_ROOT_USER_ACTION=ignore PATH=/opt/venv/bin:$PATH
# gcc + Python headers: triton compiles its CUDA launcher at first use.
RUN apt-get update && apt-get install -y --no-install-recommends python3.12 python3.12-venv python3.12-dev \
        build-essential git ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/* && python3.12 -m venv /opt/venv
RUN pip install torch==2.10.0+cu130 torchvision==0.25.0+cu130 torchaudio==2.10.0+cu130 \
        --index-url https://download.pytorch.org/whl/cu130 --extra-index-url https://pypi.org/simple
ARG COMFY_COMMIT=63be2f9b82ad0dac88a7ceaa28a9cf3154125a18
ARG REGENERATE_COMMIT=9b686c5
ARG UPSCALER_COMMIT=40316cf008b2fd8663263270669eb4da23f89d2c
RUN git clone https://github.com/comfyanonymous/ComfyUI /opt/ComfyUI && git -C /opt/ComfyUI checkout -q $COMFY_COMMIT \
    && git clone https://github.com/rdh9616/ComfyUI-Open-H3-Regenerate /opt/ComfyUI/custom_nodes/ComfyUI-Open-H3-Regenerate \
    && git -C /opt/ComfyUI/custom_nodes/ComfyUI-Open-H3-Regenerate checkout -q $REGENERATE_COMMIT \
    && git clone https://github.com/LBH-123-AI/Comfyui_Minimax_h3_latent_Upscaler /opt/ComfyUI/custom_nodes/Comfyui_Minimax_h3_latent_Upscaler \
    && git -C /opt/ComfyUI/custom_nodes/Comfyui_Minimax_h3_latent_Upscaler checkout -q $UPSCALER_COMMIT
# Requirements as the projects ship them (no huggingface_hub pin: it breaks ComfyUI's transformers).
RUN cd /opt/ComfyUI && pip install -r requirements.txt \
    && pip install -r custom_nodes/ComfyUI-Open-H3-Regenerate/requirements.txt \
    && if [ -f custom_nodes/Comfyui_Minimax_h3_latent_Upscaler/requirements.txt ]; then \
           pip install -r custom_nodes/Comfyui_Minimax_h3_latent_Upscaler/requirements.txt; fi
ARG SAGE_WHEEL=https://github.com/deepbeepmeep/SageAttention/releases/download/v2.2.0-linux.cg1/sageattention-2.2.0+cu130torch2.10.0.cg1-cp312-cp312-linux_x86_64.whl
RUN pip install "triton==3.6.0" runpod==1.12.0 \
    && pip install --no-deps "sageattention @ ${SAGE_WHEEL}#sha256=28e527a22203af9f858fa5facfa6dd2b89dcc562467ec1d360b78f68e4754a56"
# torch cu130 ships its CUDA 13 runtime under nvidia/cu13/lib (the Qwen3-VL encoder's NVRTC needs it).
ENV LD_LIBRARY_PATH=/opt/venv/lib/python3.12/site-packages/nvidia/cu13/lib
RUN echo /opt/venv/lib/python3.12/site-packages/nvidia/cu13/lib > /etc/ld.so.conf.d/zz-torch-cu13.conf && ldconfig
COPY handler.py /opt/enhance/handler.py
RUN python -c "import torch, triton, sageattention, runpod, importlib.metadata as m; \
assert torch.__version__ == '2.10.0+cu130' and torch.version.cuda == '13.0', torch.__version__; \
assert triton.__version__ == '3.6.0' and m.version('sageattention').startswith('2.2.0'); print('ok')" \
    && test "$(git -C /opt/ComfyUI rev-parse HEAD)" = "$COMFY_COMMIT" \
    && git -C /opt/ComfyUI/custom_nodes/ComfyUI-Open-H3-Regenerate rev-parse HEAD | grep -q "^$REGENERATE_COMMIT" \
    && test -f "/opt/ComfyUI/custom_nodes/ComfyUI-Open-H3-Regenerate/example_workflows/Open-H3 Regenerate 2K - Draft Video.json" \
    && ldconfig -p | grep -q "libnvrtc-builtins.so.13.0 .*nvidia/cu13/lib" \
    && python -m py_compile /opt/enhance/handler.py
WORKDIR /opt/enhance
CMD ["python", "-u", "handler.py"]
