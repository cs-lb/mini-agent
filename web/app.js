/* mini-agent 前端：零依赖、零构建。
   职责只有三件：读配置、渲染消息流、把 SSE 事件流拼回界面。 */

const state = {
  config: null, // 后端返回的完整配置（api_key 已掩码）
  presets: {},
  sessions: [],
  currentId: null,
  streaming: false,
  controller: null,
  skills: [], // 技能目录（全量，每次打开面板都是最新的）
  activeSkills: [], // 本会话启用的技能名；随会话切换而变
  skillRoots: [],
};

const $ = (id) => document.getElementById(id);

// ------------------------------------------------------------------ 极简 markdown 渲染

function escapeHtml(text) {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function inline(text) {
  return escapeHtml(text)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\*([^*]+)\*/g, "<em>$1</em>");
}

function renderMarkdown(src) {
  const fences = [];
  // 先抽出代码块占位，避免其中的 #、- 被当成 markdown 语法
  let text = src.replace(/```[\w-]*\n?([\s\S]*?)(?:```|$)/g, (_m, code) => {
    fences.push(`<pre><code>${escapeHtml(code.replace(/\n$/, ""))}</code></pre>`);
    return `\u0000${fences.length - 1}\u0000`; // NUL 占位，避免正文数字被误替换
  });

  const out = [];
  for (const block of text.split(/\n{2,}/)) {
    const lines = block.split("\n").filter((l) => l.trim() !== "");
    if (lines.length === 0) continue;

    const fenceOnly = lines[0].match(/^\u0000(\d+)\u0000$/);
    if (fenceOnly) {
      out.push(fences[Number(fenceOnly[1])]);
      continue;
    }
    if (lines.every((l) => /^\s*[-*]\s+/.test(l))) {
      out.push(`<ul>${lines.map((l) => `<li>${inline(l.replace(/^\s*[-*]\s+/, ""))}</li>`).join("")}</ul>`);
      continue;
    }
    if (lines.every((l) => /^\s*\d+\.\s+/.test(l))) {
      out.push(`<ol>${lines.map((l) => `<li>${inline(l.replace(/^\s*\d+\.\s+/, ""))}</li>`).join("")}</ol>`);
      continue;
    }
    out.push(
      `<p>${lines
        .map((l) => (/^#{1,6}\s+/.test(l) ? `<strong>${inline(l.replace(/^#{1,6}\s+/, ""))}</strong>` : inline(l)))
        .join("<br>")}</p>`
    );
  }
  return out.join("");
}

// ------------------------------------------------------------------ 消息渲染

function scrollToBottom() {
  const box = $("messages");
  box.scrollTop = box.scrollHeight;
}

function appendUser(text) {
  $("empty-state")?.remove();
  const div = document.createElement("div");
  div.className = "msg-user";
  div.textContent = text;
  $("messages").appendChild(div);
  scrollToBottom();
}

function appendAssistant() {
  $("empty-state")?.remove();
  const div = document.createElement("div");
  div.className = "msg-assistant cursor";
  $("messages").appendChild(div);
  scrollToBottom();
  return div;
}

function appendTool(name, args) {
  const wrap = document.createElement("div");
  wrap.className = "tool-block";
  const head = document.createElement("div");
  head.className = "tool-head";
  head.textContent = `⏺ ${name}(${JSON.stringify(args ?? {}).slice(0, 120)})`;
  const body = document.createElement("pre");
  body.className = "tool-body";
  body.hidden = true;
  body.textContent = "执行中…";
  head.onclick = () => (body.hidden = !body.hidden);
  wrap.append(head, body);
  $("messages").appendChild(wrap);
  scrollToBottom();
  return body;
}

function appendError(message) {
  const div = document.createElement("div");
  div.className = "msg-assistant error-line";
  div.textContent = `⚠ ${message}`;
  $("messages").appendChild(div);
  scrollToBottom();
}

function renderHistory(records) {
  const box = $("messages");
  box.innerHTML = "";
  if (records.length === 0) {
    box.innerHTML = `<div class="empty" id="empty-state"><p>发送第一条消息开始。</p></div>`;
    return;
  }
  for (const record of records) {
    if (record.role === "user") {
      appendUser(record.content || "");
    } else if (record.role === "assistant" && record.content) {
      const div = document.createElement("div");
      if (record.error) {
        // 后端在整轮无产出时会把错误落盘，刷新/切换会话后依然能看到失败原因
        div.className = "msg-assistant error-line";
        div.textContent = `⚠ ${record.content}`;
      } else {
        div.className = "msg-assistant";
        div.innerHTML = renderMarkdown(record.content);
      }
      box.appendChild(div);
    }
  }
  scrollToBottom();
}

// ------------------------------------------------------------------ 会话列表

async function loadSessions(selectId) {
  state.sessions = await (await fetch("/api/sessions")).json();
  const list = $("session-list");
  list.innerHTML = "";
  for (const item of state.sessions) {
    const row = document.createElement("div");
    row.className = "session-item" + (item.id === state.currentId ? " active" : "");
    row.onclick = () => openSession(item.id);

    const title = document.createElement("span");
    title.className = "session-title";
    title.textContent = item.title;

    const del = document.createElement("button");
    del.className = "session-del";
    del.textContent = "×";
    del.onclick = async (event) => {
      event.stopPropagation();
      await fetch(`/api/sessions/${item.id}`, { method: "DELETE" });
      if (item.id === state.currentId) state.currentId = null;
      loadSessions();
      renderHistory([]);
    };

    row.append(title, del);
    list.appendChild(row);
  }
  if (selectId) openSession(selectId);
}

async function openSession(id) {
  state.currentId = id;
  localStorage.setItem("mini-agent:session", id);
  const data = await (await fetch(`/api/sessions/${id}`)).json();
  renderHistory(data.messages);
  state.activeSkills = data.skills || []; // 技能跟会话走，切会话就换一套
  renderSkillPicker();
  loadSessions();
}

async function newSession() {
  const data = await (await fetch("/api/sessions", { method: "POST" })).json();
  state.currentId = data.id;
  localStorage.setItem("mini-agent:session", data.id);
  state.activeSkills = [];
  renderHistory([]);
  renderSkillPicker();
  await loadSessions();
}

// ------------------------------------------------------------------ 配置与模型选择

async function loadConfig() {
  state.config = await (await fetch("/api/config")).json();
  state.presets = await (await fetch("/api/presets")).json();
  renderModelPicker();
}

/** 当前选中的 {provider, model}。唯一事实来源是配置里的 active + providers[active].model，
 *  这样刷新、换标签页、重启进程后选中的型号都不会漂。 */
function currentSelection() {
  const key = state.config?.active;
  const provider = state.config?.providers?.[key];
  return { provider: key, model: provider?.model || "" };
}

/** 把配置里所有可用型号拍平成一维列表：一个条目 = 一个可直接点选的「服务商 × 型号」对。
 *  型号才是主键，服务商退化成行尾的一个小标签 —— 这正是「按型号选」的意思。 */
function modelEntries() {
  const activeKey = state.config.active;
  const groups = [];

  for (const [key, provider] of Object.entries(state.config.providers)) {
    if (!provider.api_key_set) continue; // 没 key 的连不通，不给它出现在列表里
    const models = [...new Set([provider.model, ...(provider.models || [])].filter(Boolean))];
    if (models.length === 0) continue;
    groups.push({ key, label: provider.label, models });
  }
  // 当前正在用的服务商排最前，它的型号就是最可能被点的
  groups.sort((a, b) => (a.key === activeKey ? -1 : b.key === activeKey ? 1 : 0));

  return groups.flatMap((g) => g.models.map((model) => ({ key: g.key, label: g.label, model })));
}

function renderModelPicker() {
  const entries = modelEntries();
  const { provider: activeKey, model: activeModel } = currentSelection();
  const activeProvider = state.config.providers[activeKey];

  // 触发器上型号是主角，服务商只作浅色后缀
  $("model-trigger-label").textContent = activeModel || "选择模型";
  $("model-trigger-hint").textContent = activeModel && activeProvider ? activeProvider.label : "";
  $("model-trigger").classList.toggle("empty", !activeModel);

  const list = $("model-list");
  list.innerHTML = "";
  if (entries.length === 0) {
    const hint = document.createElement("div");
    hint.className = "picker-empty";
    hint.textContent = "还没有可用的型号。先配置一个服务商（Base URL + API Key），型号就会出现在这里。";
    list.appendChild(hint);
  }

  for (const entry of entries) {
    const selected = entry.key === activeKey && entry.model === activeModel;
    const row = document.createElement("button");
    row.type = "button";
    row.className = "model-item" + (selected ? " selected" : "");
    row.innerHTML =
      `<span class="model-badge">${escapeHtml(entry.label.slice(0, 1))}</span>` +
      `<span class="model-name">${escapeHtml(entry.model)}</span>` +
      `<span class="model-from">${escapeHtml(entry.label)}</span>` +
      `<span class="model-check">${selected ? "✓" : ""}</span>`;
    row.onclick = () => selectModel(entry.key, entry.model);
    list.appendChild(row);
  }

  const ready = entries.length > 0;
  $("status").textContent = ready
    ? `就绪${activeModel ? ` · ${activeModel}` : ""}`
    : "尚未配置 API Key，点击左下角「模型与设置」";
}

/** 点选某个型号：立即落盘为当前选择，下一轮对话就生效 */
async function selectModel(providerKey, model) {
  closePanels();
  const now = currentSelection();
  if (providerKey === now.provider && model === now.model) return; // 点的就是当前这个，不白跑一次请求

  $("status").textContent = "切换中…";
  state.config = await postConfig({
    active: providerKey,
    providers: { [providerKey]: { model } },
  });
  renderModelPicker();
}

// ------------------------------------------------------------------ 技能（会话级）

const PANELS = {
  model: { wrap: "model-picker", trigger: "model-trigger", panel: "model-panel" },
  skill: { wrap: "skill-picker", trigger: "skill-trigger", panel: "skill-panel" },
};

/** 打开/关闭某个面板，同时保证另一个是关的（两个面板位置重叠，不能同时开） */
function togglePanel(name, open) {
  for (const [key, spec] of Object.entries(PANELS)) {
    const shouldOpen = key === name && (open === undefined ? $(spec.panel).hidden : open);
    $(spec.panel).hidden = !shouldOpen;
    $(spec.trigger).classList.toggle("open", shouldOpen);
  }
}

function closePanels() {
  togglePanel(null);
}

async function loadSkills() {
  const data = await (await fetch("/api/skills")).json();
  state.skills = data.skills || [];
  state.skillRoots = data.roots || [];
  renderSkillPicker();
}

function renderSkillPicker() {
  const active = state.activeSkills || [];
  $("skill-trigger-hint").textContent = active.length ? `${active.length} 个已启用` : "";
  $("skill-trigger").classList.toggle("empty", active.length === 0);

  const list = $("skill-list");
  list.innerHTML = "";

  if (state.skills.length === 0) {
    const hint = document.createElement("div");
    hint.className = "picker-empty";
    hint.textContent = "没扫到技能。在下面任一目录里放一个 <名称>/SKILL.md，刷新即可。";
    list.appendChild(hint);
  }

  for (const skill of state.skills) {
    const on = active.includes(skill.name);
    const row = document.createElement("button");
    row.type = "button";
    row.className = "skill-item" + (on ? " on" : "");
    row.innerHTML =
      `<span class="skill-box">✓</span>` +
      `<span class="skill-main">` +
      `<span class="skill-head">` +
      `<span class="skill-name">${escapeHtml(skill.name)}</span>` +
      `<span class="skill-tag">${escapeHtml(skill.source)}</span>` +
      `</span>` +
      `<span class="skill-desc">${escapeHtml(skill.description || "（无描述）")}</span>` +
      `</span>`;
    row.onclick = () => toggleSkill(skill.name);
    list.appendChild(row);
  }

  $("skill-roots").innerHTML =
    "技能目录（放 <code>&lt;名称&gt;/SKILL.md</code>）：<br>" +
    state.skillRoots
      .map((root) => `· <code>${escapeHtml(root.path)}</code>${root.exists ? "" : " — 目录不存在"}`)
      .join("<br>");
}

/** 勾选/取消一个技能。技能是会话级状态，改完立刻写回后端，换会话互不影响。 */
async function toggleSkill(name) {
  if (!state.currentId) await newSession();
  const chosen = new Set(state.activeSkills || []);
  if (chosen.has(name)) chosen.delete(name);
  else chosen.add(name);
  // 按目录顺序输出，保证每次提交的数组顺序稳定（便于 diff 与阅读）
  const next = state.skills.map((skill) => skill.name).filter((n) => chosen.has(n));

  $("skill-trigger-hint").textContent = "保存中…";
  try {
    const resp = await fetch(`/api/sessions/${state.currentId}/skills`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ skills: next }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || `HTTP ${resp.status}`);
    state.activeSkills = data.skills || [];
  } catch (error) {
    // 保存失败就把界面拉回服务端的真实状态，不能让它显示一个没生效的勾
    state.activeSkills = [];
    $("status").textContent = `技能保存失败：${error.message}`;
  }
  renderSkillPicker();
}

function openSettings() {
  const active = state.config.active;
  const provider = state.config.providers[active] || {};
  $("cfg-provider").innerHTML = Object.entries(state.config.providers)
    .map(([key, p]) => `<option value="${key}"${key === active ? " selected" : ""}>${p.label}</option>`)
    .join("");
  $("cfg-base-url").value = provider.base_url || "";
  $("cfg-model").value = provider.model || "";
  $("cfg-api-key").value = ""; // 掩码值不下发到输入框，留空表示不修改
  $("cfg-max-steps").value = state.config.max_steps ?? 12;
  fillModelOptions(modelCandidates(active), false);
  $("cfg-hint").textContent = "";
  $("cfg-hint").style.color = "";
  $("settings").hidden = false;
}

/** 某个服务商的候选型号：当前选中 + 记忆列表 + 出厂建议 */
function modelCandidates(key) {
  const saved = state.config.providers[key] || {};
  const preset = state.presets[key] || {};
  return [...new Set([saved.model, ...(saved.models || []), preset.model, ...(preset.models || [])].filter(Boolean))];
}

/** 填充 datalist：输入框既能手打，也能点箭头从候选里挑 */
function fillModelOptions(models, keepExisting = true) {
  const list = $("model-options");
  const existing = keepExisting ? [...list.options].map((o) => o.value) : [];
  const merged = [...new Set([...models, ...existing].filter(Boolean))];
  list.innerHTML = merged.map((m) => `<option value="${m}"></option>`).join("");
}

/** 切换服务商时，把该服务商已保存的配置填回表单（而不是只在空值时补预设） */
function onProviderChange() {
  const key = $("cfg-provider").value;
  const saved = state.config.providers[key] || {};
  const preset = state.presets[key] || {};
  $("cfg-base-url").value = saved.base_url || preset.base_url || "";
  $("cfg-model").value = saved.model || preset.model || "";
  $("cfg-api-key").value = ""; // 掩码值不下发到输入框，留空表示不修改
  fillModelOptions(modelCandidates(key), false);
  $("cfg-hint").textContent = "";
  $("cfg-hint").style.color = "";
}

/** 向服务商请求可用型号列表，填进输入框的候选里 */
async function fetchModels() {
  await saveSettings(true); // 先落盘，保证拉取用的是刚填的 base_url / key
  const hint = $("cfg-hint");
  hint.style.color = "";
  hint.textContent = "正在拉取型号…";
  try {
    const data = await (
      await fetch("/api/test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ provider: $("cfg-provider").value }),
      })
    ).json();
    if (data.ok) {
      fillModelOptions(data.models);
      hint.textContent = `拉到 ${data.models.length} 个型号，点输入框右侧箭头选择`;
    } else {
      hint.style.color = "var(--danger)";
      hint.textContent = `拉取失败（HTTP ${data.status}）：${String(data.detail).slice(0, 120)}`;
    }
  } catch (error) {
    hint.style.color = "var(--danger)";
    hint.textContent = `拉取失败：${error.message}`;
  }
}

/** 统一的配置写入入口：所有改配置的地方都走这里，保证返回值同步回 state */
async function postConfig(payload) {
  const resp = await fetch("/api/config", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  return resp.json();
}

async function saveSettings(stayOpen = false) {
  const key = $("cfg-provider").value;
  const model = $("cfg-model").value.trim();
  state.config = await postConfig({
    active: key,
    max_steps: Number($("cfg-max-steps").value) || 12,
    providers: {
      [key]: {
        base_url: $("cfg-base-url").value.trim(),
        model,
        api_key: $("cfg-api-key").value.trim() || "••••", // 空 = 保持原值
      },
    },
  });
  renderModelPicker();
  fillModelOptions(modelCandidates(key), false); // 刚保存的型号立刻进入候选
  $("cfg-hint").textContent = model ? `已保存，当前型号 ${model}` : "已保存";
  if (!stayOpen) setTimeout(() => ($("settings").hidden = true), 500);
}

async function testConnection() {
  await saveSettings(true); // 先落盘再测，测的就是即将生效的配置
  const hint = $("cfg-hint");
  hint.style.color = "";
  hint.textContent = "测试中…";
  try {
    const resp = await fetch("/api/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ provider: $("cfg-provider").value }),
    });
    const data = await resp.json();
    if (data.ok) {
      hint.style.color = "";
      hint.textContent = `连接正常，可见模型 ${data.models.length} 个`;
    } else {
      hint.style.color = "var(--danger)";
      hint.textContent = `失败（HTTP ${data.status}）：${String(data.detail).slice(0, 120)}`;
    }
  } catch (error) {
    hint.style.color = "var(--danger)";
    hint.textContent = `失败：${error.message}`;
  }
}

// ------------------------------------------------------------------ 发送与 SSE 消费

async function send() {
  if (state.streaming) return;
  const input = $("input");
  const text = input.value.trim();
  if (!text) return;

  const { provider: providerKey, model: modelName } = currentSelection();
  if (!providerKey || !modelName) {
    openSettings();
    return;
  }

  input.value = "";
  autoGrow();
  appendUser(text);

  state.streaming = true;
  state.controller = new AbortController();
  $("send").disabled = true;
  $("stop").hidden = false;
  $("status").textContent = "生成中…";

  const bubble = appendAssistant();
  let raw = "";
  let pendingRaf = null;

  const flush = () => {
    pendingRaf = null;
    bubble.innerHTML = renderMarkdown(raw);
    scrollToBottom();
  };

  try {
    const resp = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: state.currentId, content: text, provider: providerKey, model: modelName }),
      signal: state.controller.signal,
    });

    if (!resp.ok) {
      const detail = await resp.json().catch(() => ({}));
      throw new Error(detail.detail || `HTTP ${resp.status}`);
    }

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      let split;
      while ((split = buffer.indexOf("\n\n")) !== -1) {
        const chunk = buffer.slice(0, split);
        buffer = buffer.slice(split + 2);
        for (const line of chunk.split("\n")) {
          if (!line.startsWith("data:")) continue;
          let event;
          try {
            event = JSON.parse(line.slice(5).trim());
          } catch {
            continue;
          }
          handleEvent(event, bubble, (t) => {
            raw += t;
            if (!pendingRaf) pendingRaf = requestAnimationFrame(flush);
          });
        }
      }
    }
    flush();
  } catch (error) {
    if (error.name !== "AbortError") appendError(error.message);
  } finally {
    bubble.classList.remove("cursor");
    if (!bubble.textContent.trim()) bubble.remove(); // 整轮没内容（如鉴权失败）就别留个空壳
    state.streaming = false;
    $("send").disabled = false;
    $("stop").hidden = true;
    if ($("status").textContent === "生成中…") renderModelPicker(); // 复原成「就绪 · 型号」
    // 只刷新左侧标题。这里千万不能带 id：loadSessions(id) 会触发 openSession → renderHistory
    // 整体重绘消息区，刚渲染出来的错误提示会当场消失（曾经就是这条 bug 让人以为「没有任何回复」）
    loadSessions();
  }
}

function handleEvent(event, bubble, onText) {
  switch (event.type) {
    case "session":
      state.currentId = event.id;
      localStorage.setItem("mini-agent:session", event.id);
      // 会话是后端建的时，技能以后端返回的为准
      if (Array.isArray(event.skills)) {
        state.activeSkills = event.skills;
        renderSkillPicker();
      }
      break;
    case "delta":
      onText(event.text);
      break;
    case "tool_start":
      bubble.dataset.tool = event.id;
      break;
    case "tool_end":
      break;
    case "error": {
      const line = document.createElement("div");
      line.className = "msg-assistant error-line";
      line.textContent = `⚠ ${event.message}`;
      $("messages").appendChild(line);
      scrollToBottom();
      $("status").textContent = "请求出错";
      break;
    }
    case "done":
      break;
  }
}

// ------------------------------------------------------------------ 绑定与启动

function autoGrow() {
  const input = $("input");
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 180) + "px";
}

function bind() {
  $("send").onclick = send;
  $("new-chat").onclick = newSession;
  $("open-settings").onclick = openSettings;
  $("close-settings").onclick = () => ($("settings").hidden = true);
  $("save-settings").onclick = saveSettings;
  $("test-connection").onclick = testConnection;
  $("fetch-models").onclick = fetchModels;
  $("model-trigger").onclick = (event) => {
    event.stopPropagation(); // 别让下面的 document 监听立刻又把它关掉
    togglePanel("model");
  };
  $("skill-trigger").onclick = (event) => {
    event.stopPropagation();
    togglePanel("skill");
  };
  $("open-settings-from-picker").onclick = () => {
    closePanels();
    openSettings();
  };
  document.addEventListener("click", (event) => {
    for (const spec of Object.values(PANELS)) {
      if ($(spec.wrap).contains(event.target)) return; // 点在某个选择器内部，交给它自己处理
    }
    closePanels();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closePanels();
  });
  $("cfg-provider").onchange = onProviderChange;
  $("stop").onclick = () => state.controller?.abort();
  $("input").addEventListener("input", autoGrow);
  $("input").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      send();
    }
  });
}

async function boot() {
  bind();
  await loadConfig();
  await loadSkills();
  const remembered = localStorage.getItem("mini-agent:session");
  await loadSessions(remembered);
  if (!state.currentId && state.sessions.length > 0) openSession(state.sessions[0].id);
  if (!state.currentId) await newSession();
  autoGrow();
  $("input").focus();
}

boot();
