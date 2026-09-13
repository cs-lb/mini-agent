"""会话持久化。

用 JSONL（一行一条消息）而不是数据库：
- 追加写入简单可靠，进程崩了也不会整个文件损坏；
- 人可以直接 cat 出来看，调试友好；
- 单文件一条会话，删会话就是删文件。

会话级的**元信息**（当前是激活的 skill 列表）另存 `<id>.meta.json` 边车文件，
不混进 JSONL：消息流保持纯 append-only 且只含模型要看的角色，
元信息改起来要整份重写，混在一起会破坏 append-only 这个优点。
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from .config import SESSIONS_DIR


def _ensure_dir() -> None:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)


def _path(session_id: str) -> Path:
    return SESSIONS_DIR / f"{session_id}.jsonl"


def _meta_path(session_id: str) -> Path:
    return SESSIONS_DIR / f"{session_id}.meta.json"


def create() -> str:
    _ensure_dir()
    session_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    _path(session_id).write_text("", encoding="utf-8")
    return session_id


def append(session_id: str, record: dict[str, Any]) -> None:
    _ensure_dir()
    with _path(session_id).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def load(session_id: str) -> list[dict[str, Any]]:
    path = _path(session_id)
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # 坏行跳过，不影响整体加载
    return records


def title_of(records: list[dict[str, Any]]) -> str:
    for record in records:
        if record.get("role") == "user":
            text = (record.get("content") or "").strip().replace("\n", " ")
            return text[:40] + ("…" if len(text) > 40 else "") or "新会话"
    return "新会话"


def list_all() -> list[dict[str, Any]]:
    _ensure_dir()
    items: list[dict[str, Any]] = []
    for path in SESSIONS_DIR.glob("*.jsonl"):
        records = load(path.stem)
        if not records:
            path.unlink(missing_ok=True)  # 清理空会话
            _meta_path(path.stem).unlink(missing_ok=True)  # 别留下孤儿元信息
            continue
        items.append(
            {
                "id": path.stem,
                "title": title_of(records),
                "updated_at": path.stat().st_mtime,
                "turns": sum(1 for r in records if r.get("role") == "user"),
            }
        )
    items.sort(key=lambda x: x["updated_at"], reverse=True)
    return items


def get_meta(session_id: str) -> dict[str, Any]:
    """读会话元信息；不存在或损坏都返回空 dict（元信息丢了不该让会话打不开）。"""
    path = _meta_path(session_id)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def set_meta(session_id: str, **fields: Any) -> dict[str, Any]:
    """合并写入元信息（同样走临时文件 + 原子替换）。"""
    _ensure_dir()
    meta = {**get_meta(session_id), **fields}
    path = _meta_path(session_id)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return meta


def delete(session_id: str) -> None:
    _path(session_id).unlink(missing_ok=True)
    _meta_path(session_id).unlink(missing_ok=True)
