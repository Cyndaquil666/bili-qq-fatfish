"""OpenAI-compatible chat client with JSON handling and a daily budget."""
from __future__ import annotations

import asyncio
import base64
import json
import random
import re
from typing import Any

import httpx

from .config import Config
from .memory import Memory
from .util import day_str


class LLMError(RuntimeError):
    pass


class LLMUnavailable(LLMError):
    pass


class LLMBudgetExceeded(LLMError):
    pass


def parse_json_object(text: str) -> dict:
    """Best-effort JSON object extraction from a model reply."""
    text = (text or "").strip()
    if not text:
        raise ValueError("empty reply")
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("no JSON object found")
    body = text[start : end + 1]
    try:
        obj = json.loads(body)
    except json.JSONDecodeError:
        # One cheap repair: remove trailing commas before } or ].
        repaired = re.sub(r",\s*([}\]])", r"\1", body)
        obj = json.loads(repaired)
    if not isinstance(obj, dict):
        raise ValueError("JSON root is not an object")
    return obj


class LLM:
    def __init__(self, cfg: Config, memory: Memory, logger):
        self.cfg = cfg
        self.memory = memory
        self.log = logger
        self.base_url = str(cfg.get("llm.base_url", "")).rstrip("/")
        self.model = str(cfg.get("llm.model", "deepseek-chat"))
        self.api_key = cfg.secret("llm.api_key")
        self.timeout = float(cfg.get("llm.timeout", 90))
        self.retries = int(cfg.get("llm.retries", 2))
        self.json_mode = bool(cfg.get("llm.json_mode", True))
        self._client: httpx.AsyncClient | None = None
        self.last_error = ""

    @property
    def available(self) -> bool:
        return bool(self.api_key and self.base_url)

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout, connect=20.0),
                follow_redirects=True,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _budget_left(self) -> int:
        cap = int(self.cfg.get("safety.max_daily_llm_tokens", 0) or 0)
        if not cap:
            return 10**12
        used = int(self.memory.kv_get(f"llm_tokens:{day_str()}", 0) or 0)
        return max(0, cap - used)

    def _record_tokens(self, usage: dict | None, prompt: str, reply: str) -> None:
        if usage and isinstance(usage, dict):
            used = int(usage.get("total_tokens") or 0)
        else:
            used = int((len(prompt) + len(reply)) / 1.6)
        key = f"llm_tokens:{day_str()}"
        total = int(self.memory.kv_get(key, 0) or 0) + max(0, used)
        self.memory.kv_set(key, total)

    async def chat(
        self,
        messages: list[dict],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = False,
        tag: str = "",
        model: str | None = None,
        extra: dict | None = None,
    ) -> str:
        if not self.available:
            raise LLMUnavailable("没有可用的 LLM API key（设置 FISH_LLM_API_KEY 或 config.llm.api_key）")
        if self._budget_left() <= 0:
            raise LLMBudgetExceeded("今日 LLM token 预算已用完")

        payload: dict[str, Any] = {
            "model": model or self.model,
            "messages": messages,
            "temperature": float(self.cfg.get("llm.temperature", 0.9) if temperature is None else temperature),
            "max_tokens": int(self.cfg.get("llm.max_tokens", 900) if max_tokens is None else max_tokens),
            "stream": False,
        }
        if extra:
            payload.update(extra)
        if json_mode and self.json_mode:
            payload["response_format"] = {"type": "json_object"}

        last_exc: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                client = self._get_client()
                resp = await client.post(f"{self.base_url}/chat/completions", json=payload)
                if resp.status_code in (429, 500, 502, 503, 504):
                    raise httpx.HTTPStatusError(
                        f"HTTP {resp.status_code}: {resp.text[:200]}", request=resp.request, response=resp
                    )
                if resp.status_code >= 400:
                    raise LLMError(f"HTTP {resp.status_code}: {resp.text[:500]}")
                resp.raise_for_status()
                data = resp.json()
                content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
                if not content:
                    raise LLMError("模型返回空内容")
                self._record_tokens(data.get("usage"), json.dumps(messages, ensure_ascii=False), content)
                self.log.debug("llm ok tag=%s chars=%d", tag, len(content))
                return content
            except LLMBudgetExceeded:
                raise
            except Exception as exc:  # noqa: BLE001 - network/model failures are expected
                last_exc = exc
                self.last_error = f"{type(exc).__name__}: {exc}"
                wait = min(20.0, 1.5 * (2**attempt)) + random.uniform(0, 0.8)
                self.log.warning("llm call failed tag=%s attempt=%d/%d: %s", tag, attempt + 1, self.retries + 1, self.last_error)
                if attempt < self.retries:
                    await asyncio.sleep(wait)
        raise LLMError(f"LLM 调用失败：{self.last_error}") from last_exc

    async def vision(self, image: bytes, prompt: str, mime: str = "image/png") -> str:
        """Describe/read one image with DeepSeek's vision model."""
        if not image:
            return ""
        model = str(self.cfg.get("llm.vision_model", "deepseek-v4-flash-vision-exp"))
        b64 = base64.b64encode(image).decode("ascii")
        content = [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
        ]
        return await self.chat(
            [{"role": "user", "content": content}],
            model=model,
            max_tokens=int(self.cfg.get("llm.vision_max_tokens", 220)),
            tag="vision",
            extra={"thinking": {"type": "disabled"}},
        )

    async def json(
        self,
        messages: list[dict],
        *,
        tag: str = "",
        temperature: float | None = None,
        max_tokens: int | None = None,
        retries: int = 1,
    ) -> dict:
        text = await self.chat(
            messages, temperature=temperature, max_tokens=max_tokens, json_mode=True, tag=tag
        )
        try:
            return parse_json_object(text)
        except Exception as exc:
            self.log.warning("json parse failed tag=%s: %s", tag, exc)
            if retries <= 0:
                raise LLMError(f"JSON 解析失败：{exc}") from exc
            repair = messages + [
                {"role": "assistant", "content": text[:2000]},
                {
                    "role": "user",
                    "content": "上面的输出不是合法的 json 对象。请只输出一个合法的 json 对象，不要解释，不要 markdown 代码块。",
                },
            ]
            text2 = await self.chat(
                repair, temperature=0.2, max_tokens=max_tokens, json_mode=True, tag=tag + ":repair"
            )
            return parse_json_object(text2)

    async def ping(self) -> str:
        return await self.chat(
            [{"role": "user", "content": "只回复两个字：在的。"}],
            temperature=0.2,
            max_tokens=20,
            tag="ping",
        )
