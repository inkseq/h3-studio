"""Google AI Studio text adapter. No GPU, tools, retries, or credential copies."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import httpx

ORIGIN = "https://generativelanguage.googleapis.com"
PROFILE = "gemini--user-supplied"
MODELS = {"gemma-4-31b-it": "Gemma 4 31B IT", "gemini-3.8-flash": "Gemini 3.8 Flash"}
DEFAULT_INSTRUCTION = """你是视频创作对话助手。帮助用户逐轮表达主体、场景、动作、镜头、声音和时长。
用户提出修改时，保留此前未要求改变的创作内容；区分改一个版本与续拍下一段。
意图明确就给出更新后的方案，不反复确认。只有影响结果的关键歧义才简短追问。
用户问问题时先回答，不把每句话都当成生成指令。不要声称看过未提供的图片或视频。
你只负责讨论和提示词，不会执行视频生成、租GPU、上传或修改项目。
需要给方案时，简短列出本轮改变与保留项，并给出可独立使用的完整视频提示词。"""


class ChatError(Exception):
    def __init__(self, code, message, http_status=502):
        self.code, self.message, self.http_status = code, message, http_status
        super().__init__(code)


def central_config():
    import os

    root = Path(
        os.environ.get("AI_REGISTRY_ROOT", r"C:\Users\danmo\Desktop\AI-Registry")
    )
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from api_registry import load_api

    return load_api("gemini", profile=PROFILE)


def payload(
    model,
    messages,
    instruction=DEFAULT_INSTRUCTION,
    max_tokens=2048,
    json_mode=False,
):
    if not isinstance(model, str) or model not in MODELS:
        raise ChatError("model_not_allowed", "请选择已接入的两个模型之一。", 422)
    if not isinstance(messages, list) or not 1 <= len(messages) <= 40:
        raise ChatError("history_limit", "每个模型最多保留20轮，请另开一次对话。", 422)
    if not isinstance(instruction, str) or len(instruction) > 6000:
        raise ChatError("invalid_instruction", "助手指令不能超过6000字。", 422)
    if type(max_tokens) is not int or not 64 <= max_tokens <= 4096:
        raise ChatError("output_limit", "输出上限必须在64–4096 tokens之间。", 422)
    contents = []
    total = len(instruction)
    for index, message in enumerate(messages):
        expected = "user" if index % 2 == 0 else "model"
        if (
            not isinstance(message, dict)
            or set(message) != {"role", "text"}
            or message.get("role") != expected
            or not isinstance(message.get("text"), str)
            or not message["text"].strip()
            or len(message["text"]) > 16000
        ):
            raise ChatError(
                "invalid_history",
                "对话需从用户开始并按用户、助手交替，单条最多16000字。",
                422,
            )
        total += len(message["text"])
        contents.append({"role": expected, "parts": [{"text": message["text"]}]})
    if contents[-1]["role"] != "user" or total > 60000:
        raise ChatError(
            "history_limit",
            "请以用户消息结束；上下文总量最多60000字，超限请新建对话。",
            422,
        )
    # Both models receive the same user-role preamble. Do not assume Gemma
    # accepts Gemini's systemInstruction parameter.
    if instruction.strip():
        contents[0]["parts"].insert(
            0, {"text": "【创作助手工作约定】\n" + instruction + "\n【用户对话】"}
        )
    config = {"maxOutputTokens": max_tokens}
    if json_mode:
        # Gemini JSON mode: the reply is guaranteed to be valid JSON; the prompt defines
        # its shape. (A strict response schema degraded story-chat plans in real tests.)
        if not model.startswith("gemini"):
            raise ChatError("json_mode_not_supported", "该模型不支持JSON输出。", 422)
        config["responseMimeType"] = "application/json"
    return {"contents": contents, "generationConfig": config}


class GoogleChatClient:
    def __init__(self, loader=central_config, transport=None, *, timeout=None):
        self.loader, self.transport = loader, transport
        self.timeout = timeout or httpx.Timeout(120, connect=15)

    def _request(self, method, path, body=None):
        try:
            config = self.loader()
            if config.base_url != ORIGIN or not config.api_key:
                raise ChatError(
                    "profile_mismatch", "中央Google配置端点或凭据不匹配。", 503
                )
            with httpx.Client(
                base_url=ORIGIN,
                headers={"x-goog-api-key": config.api_key},
                timeout=self.timeout,
                follow_redirects=False,
                transport=self.transport,
            ) as client:
                response = client.request(method, path, json=body)
        except ChatError:
            raise
        except httpx.TimeoutException:
            raise ChatError(
                "upstream_timeout",
                "Google响应超时，调用可能已执行并计费；没有自动重发。",
            ) from None
        except Exception:
            raise ChatError(
                "connection_failed",
                "无法完成Google连接，请检查中央配置或网络；没有自动重发。",
                503,
            ) from None
        if response.status_code != 200:
            code = response.status_code
            messages = {
                400: "Google拒绝了当前参数。",
                401: "Google凭据未获授权。",
                403: "Google拒绝访问，请检查该Key的权限及地区设置。",
                404: "Google未提供这个准确的模型ID。",
                429: "Google额度或速率限制，请稍后再试。",
            }
            raise ChatError(
                "google_http_" + str(code),
                messages.get(code, "Google服务暂时未完成请求。"),
                502,
            )
        try:
            return response.json()
        except Exception:
            raise ChatError(
                "invalid_response", "Google返回了无法解析的响应。"
            ) from None

    def models(self):
        result = []
        for model, label in MODELS.items():
            try:
                data = self._request("GET", "/v1beta/models/" + model)
                available = data.get(
                    "name"
                ) == "models/" + model and "generateContent" in data.get(
                    "supportedGenerationMethods", []
                )
                result.append(
                    {
                        "id": model,
                        "label": label,
                        "catalog_available": available,
                        "input_token_limit": data.get("inputTokenLimit"),
                        "output_token_limit": data.get("outputTokenLimit"),
                    }
                )
            except ChatError as error:
                result.append(
                    {
                        "id": model,
                        "label": label,
                        "catalog_available": False,
                        "error": error.code,
                        "message": error.message,
                    }
                )
        return result

    def generate(
        self,
        model,
        messages,
        instruction=DEFAULT_INSTRUCTION,
        max_tokens=2048,
        json_mode=False,
    ):
        body = payload(model, messages, instruction, max_tokens, json_mode)
        started = time.monotonic()
        data = self._request(
            "POST", "/v1beta/models/" + model + ":generateContent", body
        )
        candidates = data.get("candidates", [])
        candidate = candidates[0] if candidates else {}
        parts = candidate.get("content", {}).get("parts", [])
        text = "\n".join(
            p["text"]
            for p in parts
            if isinstance(p.get("text"), str) and not p.get("thought")
        )
        if not text.strip():
            raise ChatError(
                "empty_or_blocked",
                "没有收到可展示的文本，可能被拦截或输出额度耗尽；没有自动重试。",
            )
        usage = data.get("usageMetadata", {})
        return {
            "model": model,
            "reported_model_version": data.get("modelVersion"),
            "text": text,
            "finish_reason": candidate.get("finishReason"),
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "usage": {
                k: usage[k]
                for k in (
                    "promptTokenCount",
                    "candidatesTokenCount",
                    "thoughtsTokenCount",
                    "totalTokenCount",
                )
                if k in usage
            },
        }
