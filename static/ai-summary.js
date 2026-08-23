/* AI 视频总结区块逻辑（新增文件，不改动 app.js 现有逻辑） */
(function () {
  const $ = (selector) => document.querySelector(selector);

  const section = $("#ai-summary-section");
  const idle = $("#ai-summary-idle");
  const loading = $("#ai-summary-loading");
  const loadingText = $("#ai-loading-text");
  const resultBox = $("#ai-summary-result");
  const errorBox = $("#ai-summary-error");
  const btn = $("#ai-summary-btn");
  const badge = $("#ai-source-badge");

  let pollTimer = null;

  function show(element) {
    if (element) element.classList.remove("is-hidden");
  }

  function hide(element) {
    if (element) element.classList.add("is-hidden");
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function resetPanel() {
    if (pollTimer) {
      window.clearInterval(pollTimer);
      pollTimer = null;
    }
    hide(loading);
    hide(resultBox);
    hide(errorBox);
    show(idle);
    show(section);
    if (badge) {
      badge.textContent = "";
      badge.classList.add("is-hidden");
    }
    if (btn) btn.disabled = false;
  }

  // 每次解析出新的视频结果时，自动重置 AI 区块
  const resultCard = $("#result-card");
  if (resultCard && "MutationObserver" in window) {
    new MutationObserver(() => {
      if (!resultCard.classList.contains("is-hidden")) resetPanel();
    }).observe(resultCard, { attributes: true, attributeFilter: ["class"] });
  }

  async function api(path, options = {}) {
    const response = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
    let data = null;
    try {
      data = await response.json();
    } catch {
      data = null;
    }
    if (!response.ok) {
      const detail = (data && data.detail) || `请求失败（${response.status}）`;
      throw new Error(detail);
    }
    return data;
  }

  function renderResult(task) {
    const result = task.result || {};
    const sourceMap = {
      bilibili_api: "B站平台字幕",
      yt_dlp: "yt-dlp 字幕",
    };
    const sourceText = task.source ? sourceMap[task.source] || task.source : "";
    badge.textContent = sourceText ? `字幕来源：${sourceText}` : "";
    badge.classList.toggle("is-hidden", !sourceText);

    const parts = [];

    if (result.summary) {
      parts.push(
        `<div class="ai-block ai-summary-main"><span class="ai-label">一句话总结</span><p>${escapeHtml(result.summary)}</p></div>`,
      );
    }

    if (Array.isArray(result.key_points) && result.key_points.length) {
      parts.push(
        `<div class="ai-block"><span class="ai-label">核心观点</span><ul>${result.key_points
          .map((point) => `<li>${escapeHtml(point)}</li>`)
          .join("")}</ul></div>`,
      );
    }

    if (Array.isArray(result.chapters) && result.chapters.length) {
      parts.push(
        `<div class="ai-block"><span class="ai-label">章节要点</span>${result.chapters
          .map(
            (chapter) =>
              `<div class="ai-chapter"><h4>${escapeHtml(chapter.title || "")}</h4><p>${escapeHtml(chapter.content || "")}</p></div>`,
          )
          .join("")}</div>`,
      );
    }

    if (Array.isArray(result.highlights) && result.highlights.length) {
      parts.push(
        `<div class="ai-block"><span class="ai-label">金句与亮点</span><ul class="ai-quotes">${result.highlights
          .map((highlight) => `<li>${escapeHtml(highlight)}</li>`)
          .join("")}</ul></div>`,
      );
    }

    if (Array.isArray(result.action_items) && result.action_items.length) {
      parts.push(
        `<div class="ai-block"><span class="ai-label">行动建议</span><ul>${result.action_items
          .map((item) => `<li>${escapeHtml(item)}</li>`)
          .join("")}</ul></div>`,
      );
    }

    parts.push(
      `<div class="ai-export-row"><a class="ai-export-link" href="/api/ai/export/${encodeURIComponent(task.id)}?fmt=md">导出 Markdown 总结</a></div>`,
    );

    resultBox.innerHTML = parts.join("");
    show(resultBox);
  }

  function showError(message) {
    hide(loading);
    hide(resultBox);
    errorBox.textContent = message || "总结失败，请稍后重试";
    show(errorBox);
    if (btn) btn.disabled = false;
  }

  function pollTask(taskId) {
    if (pollTimer) window.clearInterval(pollTimer);
    pollTimer = window.setInterval(async () => {
      try {
        const task = await api(`/api/ai/task/${encodeURIComponent(taskId)}`);
        if (task.status === "fetching_subtitle") {
          loadingText.textContent = "正在获取视频字幕";
        } else if (task.status === "summarizing") {
          loadingText.textContent = "AI 正在生成总结";
        } else if (task.status === "completed") {
          window.clearInterval(pollTimer);
          pollTimer = null;
          hide(loading);
          renderResult(task);
          if (btn) btn.disabled = false;
        } else if (task.status === "failed") {
          window.clearInterval(pollTimer);
          pollTimer = null;
          showError(task.error || "总结失败，请稍后重试");
        }
      } catch (error) {
        window.clearInterval(pollTimer);
        pollTimer = null;
        showError(error.message || "查询总结任务失败");
      }
    }, 1500);
  }

  function startSummary() {
    // state 由 app.js 定义（经典脚本顶层 const，可跨脚本访问）
    if (!state || !state.parsed) return;

    const urlInput = $("#url-input");
    const url = state.parsed.webpage_url || (urlInput ? urlInput.value.trim() : "");
    if (!url) return;

    if (btn) btn.disabled = true;
    hide(idle);
    hide(resultBox);
    hide(errorBox);
    show(section);
    show(loading);
    loadingText.textContent = "正在获取视频字幕";

    api("/api/ai/summarize", {
      method: "POST",
      body: JSON.stringify({ url }),
    })
      .then((task) => {
        loadingText.textContent = "正在获取视频字幕";
        pollTask(task.id);
      })
      .catch((error) => {
        showError(error.message || "创建总结任务失败");
      });
  }

  if (btn) btn.addEventListener("click", startSummary);
})();