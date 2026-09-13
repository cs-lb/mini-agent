"""Provider 抽象层。

所有模型厂商在这里被收敛成同一个接口：
    provider.stream(messages, tools) -> AsyncIterator[event]

事件只有四种，够 ReAct 循环用：
    {"type": "delta",      "text": "..."}                  逐 token 正文（含模型「思考」文本）
    {"type": "tool_calls", "calls": [{"id","name","arguments"}]}  结构化工具调用
    {"type": "done",       "finish_reason": "stop"|"tool_calls"}
    {"type": "error",      "message": "..."}
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, AsyncIterator


class LLMProvider(ABC):
    """模型适配器基类。新增厂商 = 新增一个子类，ReAct 循环不用改。"""

    name: str = "base"
    model: str = ""

    @abstractmethod
    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        ...

    async def aclose(self) -> None:  # 默认无需清理，有连接池的子类可覆写
        return None
