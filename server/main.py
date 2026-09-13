"""FastAPI 入口：静态前端 + SSE 流式对话。

接口一览：
    GET    /api/health              存活检查
    GET    /api/config              读配置（api_key 已掩码）
    POST   /api/config              写配置
    GET    /api/presets             内置服务商预设
    GET    /api/skills              技能目录
    GET    /api/skills/{name}       技能全文
    PUT    /api/sessions/{id}/skills  设置本会话激活的技能
    GET    /api/sessions            会话列表
    POST   /api/sessions            新建会话
    GET    /api/sessions/{id}       会话详情（含本会话激活的技能）
    DELETE /api/sessions/{id}       删除会话
    POST   /api/chat                发起一轮对话，返回 SSE 事件流
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config as config_module
from . import session as session_module
from .core import skills as skills_module
from .core.react import SYSTEM_PROMPT, run_react
from .providers import create as create_provider
from .providers import probe

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

app = FastAPI(title="Mini ReAct Agent", version="0.1.0")

# 允许前端单独用别的端口/协议打开（例如直接 file:// 调试）时不至于被跨域挡住
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------- 请求模型


class ChatRequest(BaseModel):
    session_id: str | None = None
    content: str = Field(..., min_length=1)
    provider: str | None = None  # 不传则用配置里的 active
    model: str | None = None  # 不传则用配置里保存的型号


class ConfigPayload(BaseModel):
    active: str | None = None
    max_steps: int | None = None
    temperature: float | None = None
    providers: dict[str, Any] | None = None


# ---------------------------------------------------------------------- 工具函数


def sse(event: dict[str, Any]) -> str:
    """把事件序列化成一条 SSE 消息。"""
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def _is_masked(value: str) -> bool:
    """前端把掩码后的 key 原样提交回来时，不能覆盖真实 key。"""
    return "••••" in (value or "")


# ---------------------------------------------------------------------- 配置接口


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "service": "mini-react-agent"}


@app.get("/api/presets")
async def presets() -> dict[str, Any]:
    return config_module.PRESETS


@app.get("/api/config")
async def get_config() -> dict[str, Any]:
    return config_module.mask_config(config_module.load_config())


@app.post("/api/config")
async def update_config(payload: ConfigPayload) -> dict[str, Any]:
    config = config_module.load_config()
    incoming = payload.model_dump(exclude_none=True)

    for field in ("active", "max_steps", "temperature"):
        if field in incoming:
            config[field] = incoming[field]

    if incoming.get("providers"):
        for key, patch in incoming["providers"].items():
            current = config["providers"].get(key, {})
            # 掩码值说明用户没改这一项，保留原值
            if _is_masked(patch.get("api_key", "")):
                patch = {**patch, "api_key": current.get("api_key", "")}
            config["providers"][key] = {**current, **patch}
            # 记住用过的型号，顶部选择器才有东西可列
            config_module.remember_model(config, key)

    config_module.save_config(config)
    return config_module.mask_config(config)


# ---------------------------------------------------------------------- 技能接口


@app.get("/api/skills")
async def list_skills() -> dict[str, Any]:
    """技能目录。每次都现扫磁盘，改完 SKILL.md 刷新页面就生效。"""
    return {
        "skills": [skills_module.public(skill) for skill in skills_module.scan()],
        "roots": skills_module.roots_info(),
    }


@app.get("/api/skills/{name}")
async def get_skill(name: str) -> dict[str, Any]:
    """单个技能的全文，供前端预览。"""
    for skill in skills_module.scan():
        if skill["name"] == name:
            return skill
    raise HTTPException(status_code=404, detail=f"技能 {name} 不存在")


class SkillSelection(BaseModel):
    skills: list[str] = Field(default_factory=list)


@app.put("/api/sessions/{session_id}/skills")
async def set_session_skills(session_id: str, payload: SkillSelection) -> dict[str, Any]:
    """设置本会话激活的技能。技能是会话级状态：换个会话互不干扰。"""
    catalog = {skill["name"] for skill in skills_module.scan()}
    unknown = [name for name in payload.skills if name not in catalog]
    if unknown:
        raise HTTPException(status_code=400, detail=f"技能不存在：{', '.join(unknown)}")
    # 去重且保序，避免同一条技能被注入两次
    active = list(dict.fromkeys(payload.skills))
    session_module.set_meta(session_id, skills=active)
    return {"id": session_id, "skills": active}


# ---------------------------------------------------------------------- 会话接口


@app.get("/api/sessions")
async def list_sessions() -> list[dict[str, Any]]:
    return session_module.list_all()


@app.post("/api/sessions")
async def new_session() -> dict[str, Any]:
    session_id = session_module.create()
    return {"id": session_id, "title": "新会话", "turns": 0}


@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str) -> dict[str, Any]:
    records = session_module.load(session_id)
    return {
        "id": session_id,
        "title": session_module.title_of(records),
        "messages": records,
        "skills": session_module.get_meta(session_id).get("skills", []),
    }


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str) -> dict[str, Any]:
    session_module.delete(session_id)
    return {"ok": True}


class TestPayload(BaseModel):
    provider: str | None = None


# ---------------------------------------------------------------------- 连通性自测


@app.post("/api/test")
async def test_connection(payload: TestPayload) -> dict[str, Any]:
    """探测当前配置能否正常调用：key 是否有效、base_url 是否写对。

    配置错误最常见的表现是「发消息后什么都没有」，与其猜，不如在这里一次性查明。
    """
    config = config_module.load_config()
    provider_cfg = config_module.get_provider_config(config, payload.provider)
    if not provider_cfg.get("base_url"):
        return {"ok": False, "status": 0, "detail": "未填写 Base URL", "models": []}
    if not provider_cfg.get("api_key"):
        return {"ok": False, "status": 0, "detail": "未填写 API Key", "models": []}
    return await probe(provider_cfg["base_url"], provider_cfg["api_key"])


# ---------------------------------------------------------------------- 对话主接口


@app.post("/api/chat")
async def chat(request: ChatRequest) -> StreamingResponse:
    config = config_module.load_config()
    provider_cfg = config_module.get_provider_config(config, request.provider)

    if not provider_cfg.get("api_key"):
        raise HTTPException(status_code=400, detail="请先在设置里填入该服务商的 API Key")

    model_name = (request.model or provider_cfg.get("model") or "").strip()
    if not model_name:
        raise HTTPException(status_code=400, detail="请先选择或填写模型型号")

    provider = create_provider(config, request.provider, model=model_name)
    session_id = request.session_id or session_module.create()

    # 本会话激活的技能：正文注入 system prompt。技能是会话级状态，不随请求传，
    # 这样刷新页面、换个客户端接上同一个会话，用的还是同一套技能。
    active_skills = session_module.get_meta(session_id).get("skills", [])
    system_prompt = skills_module.compose_system_prompt(SYSTEM_PROMPT, active_skills)

    # 只把参与模型上下文的角色取出来，前端的展示字段在这里被过滤掉
    history = [
        record
        for record in session_module.load(session_id)
        if record.get("role") in ("user", "assistant", "tool", "system")
    ]
    history.append({"role": "user", "content": request.content})
    session_module.append(session_id, {"role": "user", "content": request.content})

    async def event_stream() -> AsyncIterator[str]:
        yield sse({"type": "session", "id": session_id, "skills": active_skills})
        baseline = len(history)  # 记录起始位置，流结束后只落盘新增的消息
        error_message: str | None = None
        try:
            async for event in run_react(
                provider,
                history,
                max_steps=config.get("max_steps", 12),
                system_prompt=system_prompt,
            ):
                if event.get("type") == "error":
                    error_message = str(event.get("message", ""))
                yield sse(event)
        except Exception as exc:  # 兜底：任何异常都要变成一条可读的事件推给前端
            error_message = f"{type(exc).__name__}: {exc}"
            yield sse({"type": "error", "message": error_message})
        finally:
            produced = [m for m in history[baseline:] if m.get("role") in ("assistant", "tool")]
            for message in produced:
                session_module.append(session_id, message)
            # 整轮什么都没产出却报错了（鉴权失败 / 网络不通等），也落盘一条：
            # 否则用户刷新或切换会话回来，连「为什么没回复」的线索都找不到
            has_content = any(m.get("role") == "assistant" and m.get("content") for m in produced)
            if error_message and not has_content:
                session_module.append(session_id, {"role": "assistant", "content": error_message, "error": True})
            await provider.aclose()
        yield sse({"type": "done", "finish_reason": "stop"})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# 静态前端必须最后挂载，否则会吞掉 /api 路由
app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
