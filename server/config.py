"""配置读写。

设计要点：
1. 所有凭据只写本地 ~/.mini-agent/config.json，绝不随请求上行到任何第三方。
2. provider 全部按「OpenAI 兼容协议」建模（base_url + api_key + model），
   覆盖 OpenAI / DeepSeek / Kimi / 智谱 / 通义 / 自建网关，一套适配器通吃。
3. 写入用「先写临时文件再原子替换」，避免半截 JSON 把配置写坏。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

HOME = Path.home() / ".mini-agent"
CONFIG_PATH = HOME / "config.json"
SESSIONS_DIR = HOME / "sessions"
SKILLS_DIR = HOME / "skills"  # 用户级技能目录（仓库自带的内置技能在项目 skills/ 下）

# 常见 OpenAI 兼容服务商预设，省得用户手填 base_url。
# models 是「候选型号」：出厂建议 + 用户用过的，会同步进顶部选择器与输入联想。
PRESETS: dict[str, dict[str, Any]] = {
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "models": ["gpt-4o-mini", "gpt-4o", "gpt-4.1", "gpt-4.1-mini", "o4-mini"],
    },
    "deepseek": {
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "models": ["deepseek-chat", "deepseek-reasoner"],
    },
    "moonshot": {
        "label": "月之暗面 Kimi",
        "base_url": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-8k",
        "models": ["moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"],
    },
    "zhipu": {
        "label": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash",
        "models": ["glm-4-flash", "glm-4-air", "glm-4-plus", "glm-4-long"],
    },
    "qwen": {
        "label": "通义千问",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
        "models": ["qwen-plus", "qwen-turbo", "qwen-max", "qwen-long"],
    },
    "custom": {"label": "自定义（OpenAI 兼容）", "base_url": "", "model": "", "models": []},
}


def _default_config() -> dict[str, Any]:
    return {
        "active": "deepseek",
        "max_steps": 12,
        "temperature": 0.2,
        "providers": {
            key: {
                "label": preset["label"],
                "base_url": preset["base_url"],
                "model": preset["model"],
                "api_key": "",
                "models": list(preset.get("models", [])),
            }
            for key, preset in PRESETS.items()
        },
    }


def load_config() -> dict[str, Any]:
    """读取配置；文件不存在或损坏时回退到默认配置。"""
    if not CONFIG_PATH.exists():
        return _default_config()
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _default_config()
    # 与默认结构做一次浅合并，兼容旧版本配置缺字段的情况
    base = _default_config()
    base.update({k: v for k, v in data.items() if k in ("active", "max_steps", "temperature")})
    providers = base["providers"]
    for key, incoming in (data.get("providers") or {}).items():
        if key in providers:
            providers[key].update(incoming)
        else:
            providers[key] = incoming
    return base


def save_config(config: dict[str, Any]) -> None:
    """原子写入配置，顺带把目录建好。"""
    HOME.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, CONFIG_PATH)


def get_provider_config(config: dict[str, Any], name: str | None = None) -> dict[str, Any]:
    """取当前（或指定）provider 的配置。"""
    key = name or config.get("active", "deepseek")
    return config["providers"].get(key, {})


def remember_model(config: dict[str, Any], provider_key: str) -> None:
    """把该 provider 当前选中的 model 记进候选型号列表（去重、置顶、限长）。

    有了这份记忆，顶部选择器才能把「用过的型号」直接列出来给用户点，
    而不用每次都去手打或重新拉取。
    """
    provider = config["providers"].get(provider_key)
    if not provider:
        return
    model = (provider.get("model") or "").strip()
    if not model:
        return
    rest = [m for m in provider.get("models", []) if m and m != model]
    provider["models"] = [model] + rest[:29]


def mask_config(config: dict[str, Any]) -> dict[str, Any]:
    """返回给前端的副本：api_key 只保留前 6 位 + 掩码，避免明文回显到页面上。"""
    safe = json.loads(json.dumps(config))
    for provider in safe.get("providers", {}).values():
        key = provider.get("api_key") or ""
        provider["api_key_set"] = bool(key)
        provider["api_key"] = (key[:6] + "••••" + key[-4:]) if len(key) > 10 else ("••••" if key else "")
    return safe
