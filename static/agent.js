/* Video Intelligence Agent：按视频持久化问答聊天记录。 */
(function () {
  "use strict";

  const $ = (selector) => document.querySelector(selector);
  const section = $("#agent-section");
  const questionInput = $("#agent-question");
  const askButton = $("#agent-ask-btn");
  const status = $("#agent-status");
  const historyBox = $("#agent-history");
  const sessionsBox = $("#agent-sessions");
  const sessionCount = $("#agent-session-count");
  const stepsBox = $("#agent-steps");
  const resultBox = $("#agent-result");
  const errorBox = $("#agent-error");
  const newSessionButton = $("#agent-new-session");
  const clearSessionButton = $("#agent-clear-session");
  let pollTimer = null;
  let currentUrl = "";
  let conversationId = null;
  let sessionItems = [];
  let displayedRuns = [];
  let historyRequestId = 0;

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function hide(element) {
    if (element) element.classList.add("is-hidden");
  }

  function show(element) {
    if (element) element.classList.remove("is-hidden");
  }

  function currentVideoUrl() {
    const parsedState = typeof state !== "undefined" ? state : null;
    return parsedState?.parsed?.webpage_url || $("#url-input")?.value.trim() || currentUrl;
  }

  async function api(path, options = {}) {
    const memberToken = window.Member && window.Member.getToken ? await window.Member.getToken() : null;
    const authHeaders = window.Auth && window.Auth.getHeaders ? window.Auth.getHeaders() : {};
    const headers = { "Content-Type": "application/json", ...authHeaders };
    if (memberToken) headers["X-Member-Token"] = memberToken;
    const response = await fetch(path, { headers, ...options });
    let data = null;
    try {
      data = await response.json();
    } catch {
      data = null;
    }
    if (!response.ok) {
      const error = new Error(data?.detail || `请求失败（${response.status}）`);
      error.status = response.status;
      throw error;
    }
    return data;
  }

  function formatTime(timestamp) {
    if (!timestamp) return "刚刚";
    const date = new Date(Number(timestamp) * 1000);
    return Number.isNaN(date.getTime())
      ? "刚刚"
      : date.toLocaleString("zh-CN", { dateStyle: "short", timeStyle: "short" });
  }

  function reset() {
    if (pollTimer) window.clearInterval(pollTimer);
    pollTimer = null;
    currentUrl = currentVideoUrl();
    conversationId = null;
    show(section);
    hide(stepsBox);
    hide(resultBox);
    hide(errorBox);
    status.textContent = "可提问";
    askButton.disabled = false;
    loadSession();
  }

  function renderSessions(sessions) {
    if (!sessionsBox) return;
    const items = Array.isArray(sessions) ? sessions : [];
    sessionItems = items;
    if (sessionCount) sessionCount.textContent = items.length ? `${items.length} 个话题` : "暂无话题";
    if (!items.length) {
      sessionsBox.innerHTML = '<span class="agent-sessions-empty">还没有历史话题，点击“新建话题”开始。</span>';
      return;
    }
    sessionsBox.innerHTML = items
      .map((session, index) => {
        const summary = String(session.rolling_summary || "")
          .replace(/^[-\s]+/, "")
          .split("\n")[0]
          .trim();
        const title = summary || `话题 ${items.length - index}`;
        const count = Number(session.run_count || 0);
        return `<button class="agent-session-item${session.id === conversationId ? " active" : ""}" type="button" data-session-id="${escapeHtml(session.id)}">
          <strong>${escapeHtml(title)}</strong>
          <span>${count ? `${count} 轮问答 · ` : ""}${escapeHtml(formatTime(session.updated_at))}</span>
        </button>`;
      })
      .join("");
  }

  function renderSteps(steps) {
    const latest = new Map();
    (steps || []).forEach((step) => latest.set(step.tool, step));
    stepsBox.innerHTML = Array.from(latest.values())
      .map((step) => {
        const label = {
          fetch_subtitle: "获取字幕",
          rewrite_question: "理解追问",
          retrieve_evidence: "检索证据",
          prepare_video: "准备视频证据",
          index_video: "建立画面索引",
          retrieve_video: "Qwen 选择相关片段",
          analyze_video: "分析候选画面",
          answer_question: "生成回答",
          answer_video_question: "Qwen 视频问答",
        }[step.tool] || step.tool;
        const progress = step.tool === "index_video" && step.output?.total_chunks
          ? ` ${step.output.indexed_chunks || 0}/${step.output.total_chunks}` : "";
        return `<span class="agent-step ${escapeHtml(step.status)}">${step.status === "completed" ? "✓ " : ""}${escapeHtml(label + progress)}</span>`;
      })
      .join("");
    show(stepsBox);
  }

  function renderAnswer(result) {
    const parts = [];
    if (result.video_analysis) {
      const info = result.video_analysis;
      const label = {
        ready: "本次回答由 Qwen 直接结合视频片段生成。",
        partial: "本次画面分析尚未完整覆盖视频。",
        unavailable: "本次未取得画面证据。",
        no_match: "本次未定位到相关画面片段。",
        global_limited: "本次分析无法确认全片次数、首次或最后一次出现。",
      }[info.status] || "";
      parts.push(`<div class="agent-answer"><p>${escapeHtml(label + (info.reason ? " " + info.reason : ""))}</p></div>`);
    }
    if (result.answer) {
      parts.push(`<div class="agent-bubble agent-answer-bubble"><span class="agent-bubble-label">AI 回答</span><p>${escapeHtml(result.answer)}</p></div>`);
    }
    if (Array.isArray(result.key_points) && result.key_points.length) {
      parts.push(`<div class="agent-answer agent-key-points"><strong>视频中的关键依据</strong><ul>${result.key_points
        .map((point) => `<li>${escapeHtml(point)}</li>`)
        .join("")}</ul></div>`);
    }
    if (Array.isArray(result.ai_extensions) && result.ai_extensions.length) {
      parts.push(`<div class="agent-answer agent-extensions"><strong>AI 拓展（非视频原文）</strong><ul>${result.ai_extensions
        .map((item) => `<li>${escapeHtml(item)}</li>`)
        .join("")}</ul></div>`);
    }
    if (Array.isArray(result.follow_up_questions) && result.follow_up_questions.length) {
      parts.push(`<div class="agent-followups"><strong>你还可以继续问</strong><p>${result.follow_up_questions
        .map(escapeHtml)
        .join(" · ")}</p></div>`);
    }
    return parts.join("") || `<div class="agent-answer"><p>Agent 没有返回可展示的结果。</p></div>`;
  }

  function renderEvidence(evidence) {
    if (!Array.isArray(evidence) || !evidence.length) return "";
    return `<details class="agent-evidence"><summary>查看检索片段（${evidence.length}）</summary><div class="agent-evidence-list">${evidence
      .map((item) => {
      const label = item.source === "video" ? "视频片段" : item.source === "visual" ? "画面观察" : item.source === "subtitle" ? "字幕" : "";
        const time = Number.isFinite(item.start_seconds) && Number.isFinite(item.end_seconds)
          ? `${videoTime(item.start_seconds)}–${videoTime(item.end_seconds)}` : "";
        return `<p><b>${escapeHtml([item.id || item.chunk_id, label, time].filter(Boolean).join(" · "))}</b>：${escapeHtml(item.excerpt || item.quote)}</p>`;
      })
      .join("")}</div></details>`;
  }

  function videoTime(seconds) {
    const whole = Math.floor(seconds);
    return `${Math.floor(whole / 60).toString().padStart(2, "0")}:${(whole % 60).toString().padStart(2, "0")}`;
  }

  function processingMessage(run) {
    if (run.status === "queued") return "等待执行…";
    const step = (run.steps || []).at(-1);
    if (!step) return "正在准备…";
    const messages = {
      fetch_subtitle: "正在获取字幕…",
      rewrite_question: "正在理解问题…",
      retrieve_evidence: "正在准备字幕证据…",
      prepare_video: "正在下载和准备视频…",
      index_video: "正在分析视频画面…",
      retrieve_video: "Qwen 正在阅读画面记录、选择相关片段…",
      answer_question: "正在生成回答…",
      answer_video_question: "Qwen 正在观看所选视频片段并生成回答…",
    };
    if (step.tool === "index_video" && step.output?.total_chunks) {
      return `正在分析视频画面：${step.output.indexed_chunks || 0}/${step.output.total_chunks}，首次处理需要一些时间…`;
    }
    return messages[step.tool] || "正在处理…";
  }

  function renderTurn(run) {
    const result = run.result?.result || {};
    const question = run.question || "未记录问题";
    if (run.status !== "completed") {
      return `<article class="agent-turn" data-run-id="${escapeHtml(run.id)}">
        <div class="agent-turn-head"><span>${escapeHtml(formatTime(run.created_at))}</span><button class="agent-delete" type="button" data-delete-run="${escapeHtml(run.id)}">删除</button></div>
        <div class="agent-bubble agent-question-bubble"><span class="agent-bubble-label">你</span><p>${escapeHtml(question)}</p></div>
        <div class="agent-answer agent-turn-error"><strong>${run.status === "failed" ? "回答失败" : "处理中"}</strong><p>${escapeHtml(run.error || processingMessage(run))}</p></div>
      </article>`;
    }
    return `<article class="agent-turn" data-run-id="${escapeHtml(run.id)}">
      <div class="agent-turn-head"><span>${escapeHtml(formatTime(run.created_at))}</span><button class="agent-delete" type="button" data-delete-run="${escapeHtml(run.id)}">删除</button></div>
      <div class="agent-bubble agent-question-bubble"><span class="agent-bubble-label">你</span><p>${escapeHtml(question)}</p></div>
      ${renderAnswer(result)}
      ${renderEvidence(result.evidence)}
    </article>`;
  }

  function renderHistory(runs, highlightRunId = null) {
    if (!historyBox) return;
    displayedRuns = Array.isArray(runs) ? runs : [];
    if (!displayedRuns.length) {
      historyBox.innerHTML = '<div class="agent-history-empty">还没有问答记录，输入问题开始对话。</div>';
      return;
    }
    historyBox.innerHTML = displayedRuns.map(renderTurn).join("");
    if (highlightRunId) {
      const item = Array.from(historyBox.querySelectorAll("[data-run-id]")).find(
        (candidate) => candidate.dataset.runId === String(highlightRunId)
      );
      if (item) {
        item.classList.add("is-new");
        window.setTimeout(() => item.classList.remove("is-new"), 900);
      }
    }
  }

  function upsertRun(run, highlight = false) {
    const next = displayedRuns.filter((item) => item.id !== run.id);
    next.push(run);
    renderHistory(next, highlight ? run.id : null);
  }

  async function loadHistory(showError = true, highlightRunId = null) {
    const url = currentVideoUrl();
    if (!url || !historyBox) return;
    currentUrl = url;
    const requestId = ++historyRequestId;
    try {
      const conversationQuery = conversationId
        ? `&conversation_id=${encodeURIComponent(conversationId)}`
        : "";
      const data = await api(`/api/agent/history?url=${encodeURIComponent(url)}${conversationQuery}`);
      if (requestId === historyRequestId) {
        const scrollY = window.scrollY;
        renderHistory(data.runs, highlightRunId);
        window.requestAnimationFrame(() => window.scrollTo(0, scrollY));
      }
    } catch (error) {
      if (showError) {
        historyBox.innerHTML = `<div class="agent-history-empty">问答记录暂时无法加载：${escapeHtml(error.message)}</div>`;
      }
      return false;
    }
    return true;
  }

  async function loadSessions(selectLatest = false) {
    const url = currentVideoUrl();
    if (!url || !sessionsBox) return false;
    try {
      const data = await api(`/api/agent/conversations?url=${encodeURIComponent(url)}`);
      const sessions = Array.isArray(data.conversations) ? data.conversations : [];
      if (selectLatest || !conversationId || !sessions.some((item) => item.id === conversationId)) {
        conversationId = sessions[0]?.id || null;
      }
      renderSessions(sessions);
      return true;
    } catch {
      renderSessions([]);
      return false;
    }
  }

  async function loadSession() {
    const url = currentVideoUrl();
    if (!url || !historyBox) return;
    currentUrl = url;
    try {
      await loadSessions(true);
      await loadHistory();
    } catch (error) {
      conversationId = null;
      const historyLoaded = await loadHistory(false);
      if (!historyLoaded && historyBox) {
        historyBox.innerHTML = `<div class="agent-history-empty">会话暂时无法加载：${escapeHtml(error.message)}</div>`;
      }
    }
  }

  async function createNewSession() {
    const url = currentVideoUrl();
    if (!url) return;
    newSessionButton.disabled = true;
    try {
      const conversation = await api(`/api/agent/conversations?url=${encodeURIComponent(url)}`, { method: "POST" });
      conversationId = conversation.id;
      await loadSessions(false);
      renderHistory([]);
      status.textContent = "新话题已创建";
    } catch (error) {
      showError(error.message);
    } finally {
      newSessionButton.disabled = false;
    }
  }

  async function clearSession() {
    if (!conversationId) return;
    if (!window.confirm("确定清空当前话题的全部问答和记忆吗？此操作不可恢复。")) return;
    clearSessionButton.disabled = true;
    try {
      await api(`/api/agent/conversations/${encodeURIComponent(conversationId)}`, { method: "DELETE" });
      conversationId = null;
      await loadSessions(true);
      await loadHistory();
      status.textContent = "记忆已清空";
    } catch (error) {
      showError(error.message);
    } finally {
      clearSessionButton.disabled = false;
    }
  }

  async function switchSession(nextConversationId) {
    if (!nextConversationId || nextConversationId === conversationId) return;
    if (pollTimer) window.clearInterval(pollTimer);
    pollTimer = null;
    conversationId = nextConversationId;
    renderSessions(sessionItems);
    hide(errorBox);
    hide(stepsBox);
    status.textContent = "加载历史话题";
    await loadHistory();
    status.textContent = "可提问";
  }

  async function deleteRun(runId, button) {
    if (!window.confirm("确定删除这条问答记录吗？删除后数据库中的记录也会同步清除。")) return;
    button.disabled = true;
    try {
      await api(`/api/agent/runs/${encodeURIComponent(runId)}`, { method: "DELETE" });
      await loadHistory();
    } catch (error) {
      showError(error.message);
      button.disabled = false;
    }
  }

  function showError(message) {
    if (pollTimer) window.clearInterval(pollTimer);
    pollTimer = null;
    askButton.disabled = false;
    status.textContent = "执行失败";
    errorBox.textContent = message || "Agent 执行失败，请稍后重试";
    show(errorBox);
  }

  function pollRun(runId, runConversationId = conversationId) {
    if (pollTimer) window.clearInterval(pollTimer);
    pollTimer = window.setInterval(async () => {
      try {
        const run = await api(`/api/agent/runs/${encodeURIComponent(runId)}`);
        if (conversationId !== runConversationId) {
          window.clearInterval(pollTimer);
          pollTimer = null;
          return;
        }
        renderSteps(run.steps);
        if (run.status === "running" || run.status === "queued") upsertRun(run);
        if (run.status === "running" || run.status === "queued") {
          status.textContent = run.status === "queued" ? "等待执行" : "执行中";
        } else if (run.status === "completed") {
          window.clearInterval(pollTimer);
          pollTimer = null;
          status.textContent = "已完成";
          askButton.disabled = false;
          hide(stepsBox);
          await loadSessions(false);
          await loadHistory(true, run.id);
        } else if (run.status === "failed") {
          showError(run.error);
          await loadHistory();
        }
      } catch (error) {
        showError(error.message);
      }
    }, 1200);
  }

  async function askAgent() {
    const question = questionInput.value.trim();
    if (!question) {
      showError("请先输入问题");
      return;
    }
    const member = window.Member && window.Member.ensureMember ? await window.Member.ensureMember() : { ok: true };
    if (!member.ok) return;
    const url = currentVideoUrl();
    if (!url) return;

    askButton.disabled = true;
    hide(errorBox);
    status.textContent = "创建任务";
    try {
      const run = await api("/api/agent/runs", {
        method: "POST",
        body: JSON.stringify({ url, mode: "question", question, conversation_id: conversationId }),
      });
      questionInput.value = "";
      renderSteps(run.steps);
      upsertRun(run, true);
      status.textContent = "执行中";
      pollRun(run.id, conversationId);
    } catch (error) {
      showError(error.message);
    }
  }

  window.addEventListener("video:parsed", (event) => {
    currentUrl = event.detail?.webpage_url || "";
    reset();
  });

  if (historyBox) {
    historyBox.addEventListener("click", (event) => {
      const button = event.target.closest("[data-delete-run]");
      if (button) deleteRun(button.dataset.deleteRun, button);
    });
  }
  if (sessionsBox) {
    sessionsBox.addEventListener("click", (event) => {
      const button = event.target.closest("[data-session-id]");
      if (button) switchSession(button.dataset.sessionId);
    });
  }
  if (askButton) askButton.addEventListener("click", askAgent);
  if (newSessionButton) newSessionButton.addEventListener("click", createNewSession);
  if (clearSessionButton) clearSessionButton.addEventListener("click", clearSession);
  if (questionInput) {
    questionInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter") askAgent();
    });
  }
})();
