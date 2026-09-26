import logging
import time
from dataclasses import dataclass
from typing import Any

import requests

logger = logging.getLogger(__name__)

# 仅对这些瞬时/可重试错误重试（连接失败 + 上游瞬时 4xx/5xx）。
# 注意：超时（requests.exceptions.Timeout）不重试——重负载请求重试通常仍会超时，只会拉长阻塞。
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_TRANSPORT_ERRORS = (requests.exceptions.ConnectionError,)

from config import (
    LLM_API_KEY,
    LLM_BASE_URL,
    LLM_CONTEXT_MARGIN_TOKENS,
    LLM_CONTEXT_WINDOW,
    LLM_MIN_COMPLETION_TOKENS,
    LLM_MAX_OUTPUT_TOKENS,
    LLM_REASONING_EFFORT,
    LLM_TIMEOUT,
    VIDEO_MODEL,
)


@dataclass(frozen=True)
class CompletionOptions:
    model: str = VIDEO_MODEL



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

    def complete_text(
        self,
        messages: list[dict[str, Any]],
        options: CompletionOptions,
        max_attempts: int = 2,
        backoff: float = 3.0,
    ) -> str:
        prompt_estimate = _estimate_text_tokens(messages)
        available = max(LLM_MIN_COMPLETION_TOKENS, LLM_CONTEXT_WINDOW - prompt_estimate - LLM_CONTEXT_MARGIN_TOKENS)
        payload = {
            "model": options.model,
            "messages": messages,
            "max_tokens": min(available, LLM_MAX_OUTPUT_TOKENS),
        }
        if LLM_REASONING_EFFORT in {"low", "medium", "high"}:
            # OpenAI 兼容思考模型（如 kimi-k2.7-code）：用 reasoning_effort 控制思考力度。
            payload["reasoning_effort"] = LLM_REASONING_EFFORT
        elif self.base_url.startswith(("http://127.0.0.1", "http://localhost")):
            # 本地 vLLM 风格部署：显式关闭思考输出。
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        last_error: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                response = self.session.post(
                    f"{self.base_url}/chat/completions",
                    json=payload,
                    headers=headers,
                    timeout=LLM_TIMEOUT,
                )
                if not response.ok:
                    detail = response.text[:1000]
                    if response.status_code in _RETRYABLE_STATUS and attempt < max_attempts:
                        logger.warning(
                            "LLM 上游返回 %s，准备重试（第 %d/%d 次）：%s",
                            response.status_code, attempt, max_attempts, detail,
                        )
                        time.sleep(backoff)
                        continue
                    logger.error("LLM 请求失败（%s）：%s", response.status_code, detail)
                    raise requests.HTTPError(
                        f"LLM 请求失败（{response.status_code}）：{detail}", response=response
                    )
                try:
                    body = response.json()
                except ValueError as error:
                    logger.error(
                        "LLM 响应不是合法 JSON（status=%s）：%s",
                        response.status_code, response.text[:500],
                    )
                    raise RuntimeError("LLM 响应不是合法 JSON") from error
                try:
                    content = body["choices"][0]["message"].get("content")
                    if isinstance(content, list):
                        content = "\n".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
                    result = str(content or "").strip()
                    if not result:
                        raise RuntimeError("提示词模型未返回正文，请重试或直接使用原提示词")
                    return result
                except (KeyError, IndexError, TypeError) as error:
                    logger.error("LLM 响应缺少 choices[0].message.content：%s", str(body)[:500])
                    raise RuntimeError("LLM 响应中缺少 choices[0].message.content") from error
            except _TRANSPORT_ERRORS as error:
                last_error = error
                if attempt < max_attempts:
                    logger.warning(
                        "LLM 传输错误（%s），准备重试（第 %d/%d 次）：%s",
                        type(error).__name__, attempt, max_attempts, error,
                    )
                    time.sleep(backoff)
                    continue
                logger.error("LLM 传输错误，重试后仍失败：%s", error)
                raise
        if last_error:
            raise last_error
        raise RuntimeError("LLM 调用未返回结果")
