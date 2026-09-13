"""Agent 内核：ReAct 循环 + Skill 加载。

后续工具注册表（registry.py）、内置工具（builtin.py）、
MCP 客户端（mcp_client.py）都会挂在这个包下。
"""

from .react import SYSTEM_PROMPT, run_react
from .skills import compose_system_prompt, scan as scan_skills

__all__ = ["SYSTEM_PROMPT", "run_react", "compose_system_prompt", "scan_skills"]
