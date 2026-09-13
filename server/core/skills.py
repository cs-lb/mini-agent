"""Skill 加载器：扫描目录 → 解析 frontmatter → 按会话注入 system prompt。

一个 skill 就是一个目录 + 一份 SKILL.md：

    skills/code-review/SKILL.md
    ---
    name: code-review
    description: 代码审查清单，用于 review 代码、查 bug、评估改动风险
    ---
    （正文：写给模型的指令）

成本模型（这是设计的关键取舍）：
- **未被激活**的 skill，只有 name + description 进 prompt，约 30 token/个；
- **被激活**的 skill，正文全文进 system prompt。

第①步还没有工具循环，模型无法主动「读取」一个 skill（那需要 load_skill 工具），
所以这里先做**显式激活**：用户勾选 → 正文注入。等第④步工具注册表就位，
再把「按需加载」补上，两者共存（激活的常驻，未激活的可按需取）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..config import SKILLS_DIR

# 仓库自带的示例技能（随项目走，团队共享）
BUILTIN_DIR = Path(__file__).resolve().parent.parent.parent / "skills"
# 用户自己的技能（跨项目复用，不进仓库）
USER_DIR = SKILLS_DIR

# 后者覆盖前者：同名时用户版本优先
SKILL_ROOTS: list[tuple[Path, str]] = [
    (BUILTIN_DIR, "内置"),
    (USER_DIR, "用户"),
]


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """极简 frontmatter 解析：只认 `---` 之间的 `key: value`。

    刻意不引 pyyaml —— 这点格式用不上一个依赖，而且解析失败时
    退回「整篇当正文」比抛异常更稳妥。
    """
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text.strip()

    meta: dict[str, str] = {}
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return meta, "\n".join(lines[index + 1 :]).strip()
        if ":" in line:
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip().strip('"').strip("'")
    return {}, text.strip()  # 没有闭合的 ---，当作没有 frontmatter


def scan() -> list[dict[str, Any]]:
    """列出所有可用技能（含正文，供 build_prompt_block 用）。

    每次都现扫磁盘：技能是本地小文件，扫一遍几毫秒，
    换来的是「改完 SKILL.md 刷页面就生效」，不用重启服务。
    """
    found: dict[str, dict[str, Any]] = {}
    for root, source in SKILL_ROOTS:
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*/SKILL.md")):
            try:
                meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
            except OSError:
                continue
            name = (meta.get("name") or path.parent.name).strip()
            if not name:
                continue
            found[name] = {
                "name": name,
                "description": (meta.get("description") or "").strip(),
                "source": source,
                "dir": str(path.parent),
                "chars": len(body),
                "body": body,
            }
    return sorted(found.values(), key=lambda item: (item["source"], item["name"]))


def public(skill: dict[str, Any]) -> dict[str, Any]:
    """去掉正文：列表接口不需要把全文发给前端。"""
    return {key: value for key, value in skill.items() if key != "body"}


def roots_info() -> list[dict[str, str]]:
    return [
        {"path": str(root), "source": source, "exists": root.is_dir()}
        for root, source in SKILL_ROOTS
    ]


def build_prompt_block(active: list[str]) -> str:
    """把已激活技能的正文 + 未激活技能的目录，拼成一段追加到 system prompt 的文本。"""
    catalog = {skill["name"]: skill for skill in scan()}
    active_names = [name for name in dict.fromkeys(active) if name in catalog]  # 去重且保序
    if not active_names:
        return ""

    parts: list[str] = [
        "## 已激活技能",
        "",
        "用户为本会话显式启用了以下技能。它们的要求优先于你的默认习惯，请严格照做：",
        "",
    ]
    for name in active_names:
        parts += [f"### 技能：{name}", "", catalog[name]["body"], ""]

    idle = [skill for name, skill in catalog.items() if name not in active_names]
    if idle:
        parts += [
            "## 其他可用技能（本会话未激活）",
            "",
            *[f"- `{skill['name']}`：{skill['description'] or '（无描述）'}" for skill in idle],
            "",
            "如果当前任务明显更适合某个未激活的技能，先说明并建议用户启用它，不要假装已经启用。",
            "",
        ]
    return "\n".join(parts)


def compose_system_prompt(base_prompt: str, active: list[str]) -> str:
    block = build_prompt_block(active)
    return f"{base_prompt}\n\n{block}" if block else base_prompt
