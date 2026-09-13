"""OpenAI 兼容协议适配器。

直接用 httpx 打 /chat/completions，不引 openai SDK：
一是少一层依赖，二是各家兼容接口的细微微差异（字段名、SSE 分片粒度）自己看得见。

难点只有一处：流式返回里 tool_calls 是「按 index 分片」的，
id / name 只在首片出现，arguments 是逐字符拼接的 JSON 片段，需要自己累积。
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

import httpx

from .base import LLMProvider


class OpenAICompatProvider(LLMProvider):
    """覆盖 OpenAI / DeepSeek / Kimi / 智谱 / 通义 / vLLM / 自建网关等一切 OpenAI 兼容端点。"""

    name = "openai_compat"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        temperature: float = 0.2,
        timeout: float = 120.0,
        max_tokens: int | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=15.0))

    # ------------------------------------------------------------------ 内部工具

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }

    def _payload(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "temperature": self.temperature,
        }
        if self.max_tokens:
            payload["max_tokens"] = self.max_tokens
        if tools:
            # OpenAI tools 格式：{"type":"function","function":{"name","description","parameters"}}
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        return payload

    @staticmethod
    def _accumulate(buffer: dict[int, dict[str, Any]], pieces: list[dict[str, Any]]) -> None:
        """把流式分片按 index 合并进 buffer。"""
        for piece in pieces:
            idx = piece.get("index", 0)
            slot = buffer.setdefault(idx, {"id": "", "name": "", "arguments": ""})
            if piece.get("id"):
                slot["id"] = piece["id"]
            fn = piece.get("function") or {}
            if fn.get("name"):
                slot["name"] = fn["name"]
            if fn.get("arguments"):
                slot["arguments"] += fn["arguments"]

    @staticmethod
    def _finalize(buffer: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
        """把累积的字符串 arguments 解析成 dict，解析失败就原样塞进 _raw。"""
        calls: list[dict[str, Any]] = []
        for idx in sorted(buffer):
            slot = buffer[idx]
            raw = slot["arguments"].strip()
            try:
                args = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                args = {"_raw": raw}
            if not isinstance(args, dict):
                args = {"_raw": args}
            calls.append({"id": slot["id"] or f"call_{idx}", "name": slot["name"], "arguments": args})
        return calls

    # ------------------------------------------------------------------ 主流程

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        url = f"{self.base_url}/chat/completions"
        buffer: dict[int, dict[str, Any]] = {}

        try:
            async with self._client.stream(
                "POST", url, json=self._payload(messages, tools), headers=self._headers()
            ) as resp:
                if resp.status_code != 200:
                    body = (await resp.aread()).decode("utf-8", errors="replace")[:600]
                    yield {"type": "error", "message": f"HTTP {resp.status_code}: {body}"}
                    return

                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue

                    # 部分兼容层会在 chunk 里塞 error 字段而不是用 HTTP 状态码
                    if chunk.get("error"):
                        yield {"type": "error", "message": str(chunk["error"])}
                        return

                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    choice = choices[0]
                    delta = choice.get("delta") or {}

                    if delta.get("content"):
                        yield {"type": "delta", "text": delta["content"]}
                    if delta.get("reasoning_content"):  # DeepSeek-R1 等推理模型的思考流
                        yield {"type": "delta", "text": delta["reasoning_content"], "kind": "reasoning"}
                    if delta.get("tool_calls"):
                        self._accumulate(buffer, delta["tool_calls"])

                    finish = choice.get("finish_reason")
                    if finish:
                        if buffer:
                            yield {"type": "tool_calls", "calls": self._finalize(buffer)}
                            buffer = {}
                        yield {"type": "done", "finish_reason": finish}
                        return

                # 流正常结束但没有 finish_reason（少数网关的行为）
                if buffer:
                    yield {"type": "tool_calls", "calls": self._finalize(buffer)}
                yield {"type": "done", "finish_reason": "stop"}

        except httpx.HTTPError as exc:
            yield {"type": "error", "message": f"网络请求失败：{exc}"}

    async def aclose(self) -> None:
        await self._client.aclose()


async def probe(base_url: str, api_key: str, timeout: float = 15.0) -> dict[str, Any]:
    """探测 /models，供设置面板的「保存并测试」使用。

    key 无效、被墙、base_url 写错，这三种最常见的配置问题都能在这里暴露出来，
    不用等发消息才发现。
    """
    url = f"{base_url.rstrip('/')}/models"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url, headers={"Authorization": f"Bearer {api_key}"})
    except httpx.HTTPError as exc:
        return {"ok": False, "status": 0, "detail": f"{type(exc).__name__}: {exc}", "models": []}

    models: list[str] = []
    if resp.status_code == 200:
        try:
            models = [item.get("id", "") for item in resp.json().get("data", [])][:100]
        except (json.JSONDecodeError, AttributeError):
            models = []
    return {
        "ok": resp.status_code == 200,
        "status": resp.status_code,
        "detail": "" if resp.status_code == 200 else resp.text[:300],
        "models": models,
    }


def build_provider(
    config: dict[str, Any],
    provider_key: str | None = None,
    model: str | None = None,
) -> OpenAICompatProvider:
    """配置 -> 适配器实例。

    model 传了就用传的（单次请求临时换型号），否则用配置里存的那个。
    将来加 Anthropic 时在这里按 type 字段分流即可。
    """
    from .. import config as config_module  # 注意是上级包 server.config，不是本包

    provider_cfg = config_module.get_provider_config(config, provider_key)
    return OpenAICompatProvider(
        base_url=provider_cfg.get("base_url", ""),
        api_key=provider_cfg.get("api_key", ""),
        model=model or provider_cfg.get("model", ""),
        temperature=config.get("temperature", 0.2),
    )
