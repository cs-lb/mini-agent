"""冒烟测试用的假 OpenAI 兼容服务。

真实模型要花钱要联网，用它可以在完全离线的情况下验证整条链路：
SSE 分片解析、tool_calls 按 index 累积、ReAct 循环的分支判断。

    python tests/mock_llm.py --port 8899
    curl -N -X POST http://127.0.0.1:8000/api/chat \
         -H 'Content-Type: application/json' \
         -d '{"content":"你好","provider":"custom"}'

用户消息里含 "tool" 或 "工具" 时改为返回一次工具调用，用来验证 Act 分支与参数拼接。
回复里会回显收到的 model 与 system prompt 中激活的技能，便于端到端验证。
"""

from __future__ import annotations

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, HTTPServer

BASE = {"id": "chunk", "object": "chat.completion.chunk", "created": 0, "model": "mock-model"}


def delta(content: str | None = None, tool_calls: list[dict] | None = None) -> dict:
    piece: dict = {"content": content} if content is not None else {}
    if tool_calls:
        piece["tool_calls"] = tool_calls
    return {**BASE, "choices": [{"index": 0, "delta": piece, "finish_reason": None}]}


def finish(reason: str) -> dict:
    return {**BASE, "choices": [{"index": 0, "delta": {}, "finish_reason": reason}]}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            payload = {}

        # 用户消息里带 "tool"/"工具" 就走工具调用分支，方便一条命令切换两种场景
        last_user = next(
            (m.get("content", "") for m in reversed(payload.get("messages", [])) if m.get("role") == "user"),
            "",
        )
        use_tool = "tool" in last_user.lower() or "工具" in last_user

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def emit(obj: dict) -> None:
            self.wfile.write(f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode("utf-8"))
            self.wfile.flush()

        if use_tool:
            emit(delta(content="我来查一下当前目录。"))
            # tool_calls 分片：id/name 只在首片，arguments 逐段拼接
            emit(delta(tool_calls=[{"index": 0, "id": "call_1", "type": "function",
                                    "function": {"name": "bash", "arguments": "{\"cm"}}]))
            emit(delta(tool_calls=[{"index": 0, "function": {"arguments": "d\": \"ls\"}"}}]))
            emit(finish("tool_calls"))
        else:
            # 回显收到的 model 与 system prompt 里激活的技能，
            # 用来验证「型号透传」与「会话级技能注入」是否都生效
            model_name = payload.get("model") or "?"
            system = next(
                (m.get("content") or "" for m in payload.get("messages", []) if m.get("role") == "system"),
                "",
            )
            active = re.findall(r"### 技能：([\w.-]+)", system)
            emit(delta(content="你好，"))
            emit(delta(content=f"这是 mock 回复（收到 model={model_name}，system 字符数={len(system)}，"
                               f"激活技能=[{','.join(active)}]）。"))
            emit(finish("stop"))

        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def log_message(self, *args) -> None:  # 静音，避免污染测试输出
        pass


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8899)
    args = parser.parse_args()
    print(f"mock OpenAI-compatible server on http://127.0.0.1:{args.port}/v1")
    HTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
