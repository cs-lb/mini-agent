"""Provider 工厂。

新厂商接入方式：在 providers/ 下加一个实现 LLMProvider 的模块，
然后在这个文件里按 type 字段分流即可，ReAct 循环完全不用动。
"""

from __future__ import annotations

from typing import Any

from .base import LLMProvider
from .openai_compat import OpenAICompatProvider, build_provider, probe

__all__ = ["LLMProvider", "OpenAICompatProvider", "build_provider", "probe"]


def create(
    config: dict[str, Any],
    provider_key: str | None = None,
    model: str | None = None,
) -> LLMProvider:
    """model 非空时用于本次请求临时覆盖配置里保存的型号。"""
    return build_provider(config, provider_key, model=model)
