"""RunPod Serverless worker: Open-H3-Regenerate enhance of one H3 draft to 1080P/2K (#795).

ComfyUI runs as a local background server (started once per worker); each job converts the plugin's
"Draft Video" UI workflow to an API prompt, as in the 2026-10-29 5090 benchmark (round 22), and waits on
/history. Weights are not in the image: ComfyUI's model folders are symlinked to the network volume.
Pattern after runpod-workers/worker-comfyui (background server + HTTP queue); results go to a presigned
PUT URL rather than worker-held bucket credentials.

Input:  {video_url, res: "1080"|"2k", steps, sigma, audio: "draft"|"silence", unet: "int8"|"fp8",
         prompt?, output_put_url?}
Output: {seconds, bytes, uploaded: true} after a PUT to output_put_url, otherwise {seconds, bytes, video_base64}.
Tiers (owner 2026-10-29): standard = steps 2, sigma 0 (workflow 0.7 grid); enhanced = steps 3, sigma 0.8.

Volume layout under $ENHANCE_MODELS (default /runpod-volume/models):
  diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors, minimax_h3_fl2va_pruned_fp8_scaled.safetensors,
                   minimax_h3_ref2va_pruned_adaln_t_table_only.safetensors (32 KB curve table)
  loras/open_h3_regenerate_2k_lora.safetensors, sharp_curve_branches.safetensors
  vae/fast_h3_vae_noncausal_24l.safetensors, minimax_h3_video_vae_fp16.safetensors, minimax_h3_audio_vae_fp32.safetensors
  text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors
  latent_upscale_models/minimax_h3_latent_upscaler_3d_conv_v1_bf16.safetensors
"""

import base64, glob, json, os, shutil, subprocess, time, urllib.error, urllib.request, uuid

COMFY = '/opt/ComfyUI'
MODELS = os.environ.get('ENHANCE_MODELS', '/runpod-volume/models')
MODEL_DIRS = (
    'diffusion_models',
    'loras',
    'vae',
    'text_encoders',
    'latent_upscale_models',
)
URL = 'http://127.0.0.1:8188'
WF = (
    COMFY
    + '/custom_nodes/ComfyUI-Open-H3-Regenerate/example_workflows/Open-H3 Regenerate 2K - Draft Video.json'
)
SKIP = {'MarkdownNote', 'PreviewAny', 'OpenH3VideoToPrompt'}
WIDGET = {'INT', 'FLOAT', 'STRING', 'BOOLEAN', 'COMBO'}
RES = {'2k': '2k (2560x1440)', '1080': '1080p (1920x1080)'}
AUDIO = {'silence': 'silence (reference)', 'draft': 'draft audio (AUDIO input)'}
UNET = {
    'int8': 'minimax_h3_fl2va_pruned_int8_convrot.safetensors',
    'fp8': 'minimax_h3_fl2va_pruned_fp8_scaled.safetensors',
}
DEFAULT_PROMPT = (
    'integrated_multimodal_description: [Shot 1] A cinematic film shot, 35mm movie look, shallow depth of field.'
    ' Clean frame: no subtitles, captions or any on-screen text.'
)
# RunPod caps a job result at roughly 10-20 MB; larger outputs need output_put_url.
INLINE_LIMIT = 9 * 1024 * 1024
_base = None


def get(path):
    return json.loads(urllib.request.urlopen(URL + path, timeout=60).read())


def post(path, body):
    req = urllib.request.Request(
        URL + path, json.dumps(body).encode(), {'Content-Type': 'application/json'}
    )
    try:
        return json.loads(urllib.request.urlopen(req, timeout=60).read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(e.read().decode()[:2000]) from None


def link_models():
    """Model folders point at the network volume (MODELS/<folder>/<file>), so plain names resolve as in round 22."""
    for folder in MODEL_DIRS:
        source, target = f'{MODELS}/{folder}', f'{COMFY}/models/{folder}'
        if not os.path.isdir(source):
            raise RuntimeError(f'missing model folder {source} on the network volume')
        if os.path.islink(target):
            os.remove(target)
        elif os.path.isdir(target):
            shutil.rmtree(target)
        os.symlink(source, target)


def start_comfy():
    link_models()
    log = open('/tmp/comfy.log', 'ab')
    subprocess.Popen(
        [
            'python',
            'main.py',
            '--listen',
            '127.0.0.1',
            '--port',
            '8188',
            '--use-sage-attention',
            '--disable-auto-launch',
        ],
        cwd=COMFY,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    for _ in range(600):
        try:
            return get('/object_info')
        except Exception:
            time.sleep(1)
    raise RuntimeError('ComfyUI did not start; see /tmp/comfy.log')


def convert(info):
    """UI workflow -> API prompt, assigning widgets_values in object_info input order."""
    wf = json.load(open(WF, encoding='utf-8'))
    links = {l[0]: l for l in wf['links']}
    api = {}
    for n in wf['nodes']:
        if n['type'] in SKIP or n.get('mode', 0) != 0:
            continue
        spec = info[n['type']]['input']
        names = list(spec.get('required', {}).items()) + list(
            spec.get('optional', {}).items()
        )
        ui = {i['name']: i for i in n.get('inputs', [])}
        vals = list(n.get('widgets_values') or [])
        inputs = {}
        for name, s in names:
            typ = s[0] if isinstance(s, (list, tuple)) else s
            opts = (
                s[1]
                if isinstance(s, (list, tuple))
                and len(s) > 1
                and isinstance(s[1], dict)
                else {}
            )
            link = ui.get(name, {}).get('link')
            if isinstance(typ, list) or typ in WIDGET:
                v = vals.pop(0) if vals else None
                if opts.get('control_after_generate'):
                    vals.pop(0)
                if link is None:
                    if v is not None:
                        inputs[name] = v
                    continue
            if link is not None:
                inputs[name] = [str(links[link][1]), links[link][2]]
        api[str(n['id'])] = {'class_type': n['type'], 'inputs': inputs}
    return api


def job_prompt(base, tag, video, res, sigma, audio, steps, unet, text):
    p = json.loads(json.dumps(base))
    p['2']['inputs']['unet_name'] = UNET[unet]
    p['3']['inputs'].update(
        lora_name='open_h3_regenerate_2k_lora.safetensors',
        curve_name='sharp_curve_branches.safetensors',
        curve_table='minimax_h3_ref2va_pruned_adaln_t_table_only.safetensors',
    )
    # Node 4 is the sparse Sol attention patch: dense attention only (owner 2026-10-29).
    p.pop('4')
    p['22']['inputs']['model'] = ['3', 0]
    p['6']['inputs']['clip_name'] = 'qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors'
    p['7']['inputs']['vae_name'] = 'minimax_h3_video_vae_fp16.safetensors'
    p['8']['inputs']['vae_name'] = 'minimax_h3_audio_vae_fp32.safetensors'
    p['9']['inputs']['file'] = video
    p['16']['inputs']['text'] = text
    p['17']['inputs'].update(
        model_name='minimax_h3_latent_upscaler_3d_conv_v1_bf16.safetensors',
        resolution=RES[res],
    )
    p['18']['inputs']['audio_mode'] = AUDIO[audio]
    if audio == 'draft':
        p['18']['inputs']['audio'] = ['10', 1]
    else:
        p['18']['inputs'].pop('audio', None)
    p['19']['inputs']['start_sigma'] = sigma
    p['19']['inputs']['steps'] = steps
    p['24']['inputs']['filename_prefix'] = 'lat_' + tag
    p['25']['inputs']['vae_name'] = 'fast_h3_vae_noncausal_24l.safetensors'
    p['28']['inputs'].update(
        {'filename_prefix': 'video/' + tag, 'format': 'auto', 'format.codec': 'auto'}
    )
    return p


def validate(job):
    res, audio, unet = (
        job.get('res', '1080'),
        job.get('audio', 'draft'),
        job.get('unet', 'int8'),
    )
    steps, sigma = job.get('steps', 2), job.get('sigma', 0.0)
    if res not in RES or audio not in AUDIO or unet not in UNET:
        raise ValueError('res must be 1080|2k, audio draft|silence, unet int8|fp8')
    if (
        type(steps) is not int
        or not 1 <= steps <= 8
        or type(sigma) not in (int, float)
        or not 0 <= sigma <= 1
    ):
        raise ValueError(
            'steps must be an int 1-8 and sigma a number 0-1 (0 = workflow grid)'
        )
    url = job.get('video_url')
    if not isinstance(url, str) or not url.startswith(('https://', 'http://')):
        raise ValueError('video_url must be an http(s) URL')
    text = job.get('prompt') or DEFAULT_PROMPT
    if not isinstance(text, str) or len(text) > 8000:
        raise ValueError('prompt must be a string up to 8000 characters')
    return url, res, float(sigma), audio, steps, unet, text


def handler(event):
    global _base
    url, res, sigma, audio, steps, unet, text = validate(event.get('input') or {})
    if _base is None:
        _base = convert(start_comfy())
    tag = uuid.uuid4().hex
    video = tag + '.mp4'
    with (
        urllib.request.urlopen(url, timeout=120) as r,
        open(f'{COMFY}/input/{video}', 'wb') as f,
    ):
        shutil.copyfileobj(r, f)
    t0 = time.time()
    try:
        pid = post(
            '/prompt',
            {
                'prompt': job_prompt(
                    _base, tag, video, res, sigma, audio, steps, unet, text
                )
            },
        )['prompt_id']
        while True:
            h = get('/history/' + pid)
            if pid in h:
                break
            time.sleep(1)
        status = h[pid].get('status', {})
        if status.get('status_str') != 'success':
            errors = [
                m[1] for m in status.get('messages', []) if m[0] == 'execution_error'
            ]
            raise RuntimeError(json.dumps(errors)[:2000] or 'ComfyUI execution failed')
        found = glob.glob(f'{COMFY}/output/video/{tag}*.mp4')
        if len(found) != 1:
            raise RuntimeError('expected one output mp4, found %d' % len(found))
        data = open(found[0], 'rb').read()
    finally:
        for path in [f'{COMFY}/input/{video}'] + glob.glob(
            f'{COMFY}/output/**/*{tag}*', recursive=True
        ):
            if os.path.isfile(path):
                os.remove(path)
    result = {'seconds': round(time.time() - t0, 1), 'bytes': len(data)}
    put = (event.get('input') or {}).get('output_put_url')
    if put:
        req = urllib.request.Request(
            put, data=data, method='PUT', headers={'Content-Type': 'video/mp4'}
        )
        urllib.request.urlopen(req, timeout=300).read()
        return {**result, 'uploaded': True}
    if len(data) > INLINE_LIMIT:
        raise RuntimeError(
            'output is %d bytes; pass output_put_url (presigned PUT) for large results'
            % len(data)
        )
    return {**result, 'video_base64': base64.b64encode(data).decode()}


if __name__ == '__main__':
    import runpod

    runpod.serverless.start({'handler': handler})
