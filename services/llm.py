from dataclasses import dataclass
from typing import Any

import requests

from config import (
    LLM_API_KEY,
    LLM_BASE_URL,
    LLM_CONTEXT_MARGIN_TOKENS,
    LLM_CONTEXT_WINDOW,
    LLM_MIN_COMPLETION_TOKENS,
    LLM_TIMEOUT,
    VIDEO_MODEL,
)


@dataclass(frozen=True)
class CompletionOptions:
    model: str = VIDEO_MODEL
    temperature: float = 0.2
    max_tokens: int = 5000


def _estimate_text_tokens(messages: list[dict[str, Any]]) -> int:
    """按文本内容估算提示词 token 数（图片 data URL 由模型服务端自行处理，不占本估算）。"""
    total = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    total += len(str(part.get("text", "")))
    return max(1, total // 3)


class LLMClient:
    def __init__(self, base_url: str | None = None, api_key: str | None = None):
        self.base_url = (base_url or LLM_BASE_URL).rstrip("/")
        self.api_key = LLM_API_KEY if api_key is None else api_key
        self.session = requests.Session()
        self.session.trust_env = False

    def complete_text(self, messages: list[dict[str, Any]], options: CompletionOptions) -> str:
        prompt_estimate = _estimate_text_tokens(messages)
        available = max(LLM_MIN_COMPLETION_TOKENS, LLM_CONTEXT_WINDOW - prompt_estimate - LLM_CONTEXT_MARGIN_TOKENS)
        payload = {
            "model": options.model,
            "messages": messages,
            "temperature": options.temperature,
            "max_tokens": min(options.max_tokens, available),
            "chat_template_kwargs": {"enable_thinking": False},
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        response = self.session.post(
            f"{self.base_url}/chat/completions",
            json=payload,
            headers=headers,
            timeout=LLM_TIMEOUT,
        )
        if not response.ok:
            detail = response.text[:1000]
            raise requests.HTTPError(f"LLM 请求失败（{response.status_code}）：{detail}", response=response)
        body = response.json()
        try:
            return str(body["choices"][0]["message"].get("content") or "").strip()
        except (KeyError, IndexError, TypeError) as error:
            raise RuntimeError("LLM 响应中缺少 choices[0].message.content") from error
