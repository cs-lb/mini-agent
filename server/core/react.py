"""ReAct 主循环 —— 整个应用的心脏，约 60 行，无任何框架依赖。

循环就是四步：
    Think    把 system + 历史 + 工具 schema 交给模型
    Act      模型返回「思考文本 + tool_calls」
    Execute  按名字分发到内置工具 / MCP 工具 / Skill（统一由 executor 处理）
    Observe  把工具结果以 role=tool 追加回 messages，回到 Think

退出条件：模型不再要调工具（Final Answer），或达到 max_steps 强制收尾。

第①步 executor 为 None、tools 为空，循环自然退化成「单轮流式生成」；
第②步注入工具后，同一份代码就是完整 ReAct。
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator, Awaitable, Callable

# 工具执行器签名：(name, arguments) -> 结果字符串
Executor = Callable[[str, dict[str, Any]], Awaitable[str]]

SYSTEM_PROMPT = """你是一个运行在用户本机上的 AI Agent，采用 ReAct（Reasoning + Acting）模式工作。

工作方式：
- 动手之前先用一两句中文说明你的判断和下一步打算，再执行。
- 需要外部信息或副作用时调用工具，不要凭空编造文件内容、命令输出或网页结果。
- 工具返回后据实修正判断，再决定继续调用工具还是给出结论。
- 拿不到结论时，如实说明卡在哪一步，以及你还缺什么。

输出风格：简洁、直接，不客套，不重复用户已经说过的话。"""


async def run_react(
    provider,
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    executor: Executor | None = None,
    max_steps: int = 12,
    system_prompt: str = SYSTEM_PROMPT,
) -> AsyncIterator[dict[str, Any]]:
    """驱动一轮完整对话，边跑边 yield 事件（供 SSE 推给前端）。

    messages 会被就地追加（assistant / tool 消息），调用方拿到的就是最新历史。
    """
    tools = tools or []
    schemas = [t["schema"] for t in tools] or None

    # system prompt 常驻在最前面，且只保留一条，避免重复叠加
    if not messages or messages[0].get("role") != "system":
        messages.insert(0, {"role": "system", "content": system_prompt})

    for step in range(max_steps):
        text_buf: list[str] = []
        calls: list[dict[str, Any]] = []
        stop = False

        # -------- Think + Act：向模型要一次输出 --------
        async for event in provider.stream(messages, tools=schemas):
            kind = event["type"]
            if kind == "delta":
                text_buf.append(event["text"])
                yield event
            elif kind == "tool_calls":
                calls = event["calls"]
            elif kind == "done":
                stop = True
            elif kind == "error":
                yield event
                return

        # -------- 把 assistant 消息回写历史（OpenAI 原生格式）--------
        assistant_msg: dict[str, Any] = {"role": "assistant", "content": "".join(text_buf) or None}
        if calls:
            assistant_msg["tool_calls"] = [
                {
                    "id": call["id"],
                    "type": "function",
                    "function": {
                        "name": call["name"],
                        "arguments": json.dumps(call.get("arguments") or {}, ensure_ascii=False),
                    },
                }
                for call in calls
            ]
        messages.append(assistant_msg)

        # -------- 没有工具调用 = Final Answer，循环结束 --------
        if not calls or executor is None:
            if not stop:
                yield {"type": "done", "finish_reason": "stop"}
            return

        # -------- Execute + Observe：逐个执行工具，结果追加回历史 --------
        for call in calls:
            yield {
                "type": "tool_start",
                "id": call["id"],
                "name": call["name"],
                "arguments": call.get("arguments") or {},
            }
            try:
                result = await executor(call["name"], call.get("arguments") or {})
            except Exception as exc:  # 工具崩了不能拖垮整个循环，把错误喂回模型让它自己改
                result = f"[tool error] {type(exc).__name__}: {exc}"
            yield {"type": "tool_end", "id": call["id"], "name": call["name"], "result": str(result)}
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": str(result)})

    yield {"type": "error", "message": f"已达到最大步数 {max_steps}，本轮强制收尾。"}
