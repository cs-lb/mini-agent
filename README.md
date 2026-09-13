# mini-agent

一个不依赖任何 agent 框架（langchain 之类）的 ReAct Agent，**核心循环手写**，模型由你自己选、key 你自己填，能力通过 MCP 与 Skill 插件式扩展。前端是仿 Codex 的极简界面，零构建。

## 快速开始

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh            # 或： .venv/bin/python -m uvicorn server.main:app --port 8000
# 打开 http://localhost:8000 → 左下角「模型与设置」填 Base URL / 模型名 / API Key
```

凭据只写在本机 `~/.mini-agent/config.json`，不会随任何请求上行，也不会写进仓库。

## 选择模型型号

**型号是主键，服务商只是它的一个属性**——所以选择器按型号平铺，不按服务商分两级。

**顶部选择器**：点开后是一张型号列表，每行一个型号，行尾浅色小字标出它属于哪个服务商，当前生效的那行打勾。点一下即切换并落盘，下一轮对话立刻用新型号。列表底部是「✎ 配置自定义模型…」，直接进设置面板。

一个型号要出现在列表里，需要它所属的服务商已经填好 API Key。候选型号来自三处：

1. 每个服务商的**出厂建议列表**（如 DeepSeek 的 `deepseek-chat` / `deepseek-reasoner`）；
2. 你**用过的型号**——每次保存或切换都会去重置顶（上限 30 个）；
3. **「拉取型号」按钮**——向服务商请求 `/models`，把真实可用的型号灌进候选，适合 OpenRouter、硅基流动这类有几百个型号的中转站。拉回来的型号同样可以直接在顶部选择器里点。

设置面板里也能直接手打型号（原生 datalist，输入即过滤），适合预设里没有的新型号。

对话请求本身带 `model` 字段，所以切换型号无需等配置落盘即对下一轮生效。

## 技能（Skill）

技能是**一段写给模型的指令**，用来改变它在某一类任务上的行为：审查代码的清单、写文档的规矩、某个领域的口径。
一个技能就是一个目录 + 一份 `SKILL.md`：

```
skills/code-review/SKILL.md
---
name: code-review
description: 代码审查。按「正确性 → 边界 → 可维护性」三层过一遍改动，只报真问题并给可落地的修法
---
（正文：写给模型的指令，会原文进 system prompt）
```

**技能是会话级的**。顶部「技能」按钮里勾选，只对**当前这条会话**生效，换个会话互不影响——
写代码的会话开着 `code-review`，写文档的会话开着 `tech-doc`，两者的 system prompt 各是各的。

**成本模型**：未激活的技能只贡献 name + description（约 30 token/个）；激活的才把正文全文注入。
勾选状态写在会话的边车文件 `~/.mini-agent/sessions/<id>.meta.json` 里，刷新页面、重启服务都还在。

**两个扫描目录**，同一个名字时用户版覆盖内置版：

| 目录 | 用途 |
|---|---|
| `mini-agent/skills/` | 仓库自带，跟项目走 |
| `~/.mini-agent/skills/` | 你自己的，跨项目复用，不进仓库 |

放进去就生效，不需要重启——每次打开面板都现扫磁盘。目录、优先级、正文长度都能在面板底部看到。

> 目前只支持「用户显式激活」。第④步工具注册表就位后会补上 `load_skill` 工具，
> 让模型也能按需读取未激活的技能（渐进披露），两者共存。

## 架构

```
Web UI（原生 HTML/JS，仿 Codex）
   │  SSE
FastAPI（main.py：配置 / 会话 / 对话接口）
   │
ReAct 循环（core/react.py）── 手写，核心不到 100 行
   │                    │
模型适配层               工具注册表（registry.py）
providers/               ├── 内置工具（builtin.py）
  ├── openai_compat.py   ├── MCP 工具（mcp_client.py）
  └── （anthropic 待补）  └── Skill（skills.py，渐进披露）
   │
持久化：~/.mini-agent/sessions/*.jsonl
```

ReAct 循环本体：

```python
for step in range(max_steps):
    resp = await llm.stream(messages, tools=schemas)   # Act：拿到思考文本 + tool_calls
    messages.append(resp.message)                      # 回写历史
    if not resp.tool_calls:
        return resp.text                               # 没有工具调用就是最终答案
    for call in resp.tool_calls:
        result = await registry.invoke(call)           # Execute
        messages.append(tool_message(result))          # Observe
```

第①步工具列表为空，循环退化为单轮流式生成；第②步接上工具后同一份代码直接跑多轮。

## 目录

| 路径 | 作用 |
|---|---|
| `server/main.py` | FastAPI 入口，SSE 对话接口 |
| `server/config.py` | `~/.mini-agent/config.json` 读写，api_key 掩码 |
| `server/core/react.py` | ReAct 主循环 |
| `server/core/skills.py` | 技能扫描、frontmatter 解析、按会话注入 system prompt |
| `server/core/registry.py` | 工具注册表（第②步） |
| `server/core/builtin.py` | 内置工具（第②步） |
| `server/core/mcp_client.py` | MCP 客户端，官方 SDK（第③步） |
| `server/providers/` | 模型适配，目前只有 OpenAI 兼容 |
| `server/session.py` | JSONL 会话持久化 + 会话级元信息（激活的技能） |
| `web/` | 前端三件套，零依赖 |
| `skills/` | 内置技能示例（code-review / tech-doc） |

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 存活检查 |
| GET/POST | `/api/config` | 读写配置（GET 返回的 api_key 是掩码） |
| GET | `/api/presets` | 内置服务商预设 |
| GET | `/api/skills` | 技能目录（含扫描根目录与是否存在） |
| GET | `/api/skills/{name}` | 单个技能全文 |
| PUT | `/api/sessions/{id}/skills` | 设置本会话激活的技能，body `{"skills": [...]}` |
| POST | `/api/test` | 自测连通性：探测 `/models`，验证 key 与 base_url |
| GET/POST/DELETE | `/api/sessions[/id]` | 会话列表 / 新建 / 详情（含 `skills`）/ 删除 |
| POST | `/api/chat` | 发起对话，返回 SSE 事件流 |

SSE 事件类型：`session`（会话 id + 本会话激活的技能）、`delta`（文本增量）、`tool_start` / `tool_end`（第②步）、`error`、`done`。

## 排查问题

**发消息后什么都没有？** 点设置面板里的「保存并测试」，它会直接探测服务商的 `/models`：

- 返回 401 → key 无效或已过期，换一个再试；
- 返回 404 → base_url 结尾多了或少了 `/v1`；
- 连接超时 → 网络不通，或需要给 httpx 配代理。

整轮没有任何产出却报错时，后端会把错误写进会话历史（`error: true` 的 assistant 消息），
刷新或切换会话回来仍能看到失败原因，不会再出现「静默无回复」。

## 路线图

- [x] ① 骨架：配置 + 模型选择 + 流式对话 + 会话持久化 + 前端
- [x] ①b 技能：SKILL.md 扫描 + 会话级勾选 + 注入 system prompt
- [ ] ② 内置工具（bash / read / write / glob / grep）与工具注册表，ReAct 真正跑多轮
- [ ] ③ MCP 接入（官方 Python SDK，stdio transport，前端管理 server）
- [ ] ④ 危险命令审批闸门 + `load_skill` 工具（技能的渐进披露）
