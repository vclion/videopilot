/* AI 视频总结区块逻辑（解析成功后自动触发） */
(function () {
  "use strict";

  const $ = (selector) => document.querySelector(selector);

  const section = $("#ai-summary-section");
  const idle = $("#ai-summary-idle");
  const loading = $("#ai-summary-loading");
  const loadingText = $("#ai-loading-text");
  const resultBox = $("#ai-summary-result");
  const errorBox = $("#ai-summary-error");
  const btn = $("#ai-summary-btn");
  const retryBtn = $("#ai-summary-retry");
  const badge = $("#ai-source-badge");
  const subtitleBtn = $("#subtitle-btn");
  const subtitleLoading = $("#subtitle-loading");
  const subtitleLoadingText = $("#subtitle-loading-text");
  const subtitleResult = $("#subtitle-result");
  const subtitleFilePath = $("#subtitle-file-path");
  const subtitleFileLink = $("#subtitle-file-link");
  const subtitleError = $("#subtitle-error");
  const subtitleBtnLabel = subtitleBtn ? subtitleBtn.querySelector(".ai-summary-btn-label") : null;

  let pollTimer = null;
  let subtitlePollTimer = null;

  function show(element) {
    if (element) element.classList.remove("is-hidden");
  }

  function hide(element) {
    if (element) element.classList.add("is-hidden");
  }

  function setSubtitleButtonState(stateName) {
    if (!subtitleBtn) return;
    const labels = {
      idle: "提取字幕",
      loading: "提取中...",
      success: "提取成功",
    };
    subtitleBtn.disabled = stateName !== "idle";
    subtitleBtn.classList.toggle("is-success", stateName === "success");
    if (subtitleBtnLabel) subtitleBtnLabel.textContent = labels[stateName] || labels.idle;
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll(String.fromCharCode(34), "&quot;")
      .replaceAll(String.fromCharCode(39), "&#039;");
  }

  function resetPanel() {
    if (pollTimer) {
      window.clearInterval(pollTimer);
      pollTimer = null;
    }
    if (subtitlePollTimer) {
      window.clearInterval(subtitlePollTimer);
      subtitlePollTimer = null;
    }
    hide(loading);
    hide(resultBox);
    hide(errorBox);
    hide(retryBtn);
    hide(subtitleLoading);
    hide(subtitleResult);
    hide(subtitleError);
    hide(subtitleFilePath);
    if (subtitleFileLink) {
      subtitleFileLink.removeAttribute("href");
      subtitleFileLink.textContent = "字幕文件地址";
    }
    show(idle);
    show(section);
    if (badge) {
      badge.textContent = "";
      badge.classList.add("is-hidden");
    }
    if (btn) btn.disabled = false;
    if (retryBtn) retryBtn.disabled = false;
    setSubtitleButtonState("idle");
  }

  async function api(path, options = {}) {
    const token = window.Member && window.Member.getToken ? await window.Member.getToken() : null;
    const authHeaders = window.Auth && window.Auth.getHeaders ? window.Auth.getHeaders() : {};
    const headers = { "Content-Type": "application/json", ...authHeaders };
    if (token) headers["X-Member-Token"] = token;
    const response = await fetch(path, { headers, ...options });
    let data = null;
    try {
      data = await response.json();
    } catch {
      data = null;
    }
    if (!response.ok) {
      const error = new Error((data && data.detail) || `请求失败（${response.status}）`);
      error.status = response.status;
      throw error;
    }
    return data;
  }

  async function restoreCachedHistory(url) {
    if (!url) return;
    try {
      const cached = await api(`/api/ai/history?url=${encodeURIComponent(url)}`);
      if (cached.subtitle) {
        renderSubtitles({
          source: cached.subtitle.source,
          result: cached.subtitle.result || {},
        });
        setSubtitleButtonState("success");
      }
      if (cached.summary) {
        hide(idle);
        hide(loading);
        hide(errorBox);
        hide(retryBtn);
        renderResult({
          cached: true,
          url,
          source: cached.summary.source,
          result: cached.summary.result || {},
        });
      }
    } catch {
      // 历史缓存读取失败不应阻断正常的字幕提取和总结流程。
    }
  }

  function renderResult(task) {
    const result = task.result || {};
    const sourceMap = {
      bilibili_api: "B站平台字幕",
      yt_dlp: "yt-dlp 字幕",
      faster_whisper: "本地语音转写",
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

    const exportUrl = task.cached
      ? `/api/ai/export-cache?url=${encodeURIComponent(task.url)}&fmt=md`
      : `/api/ai/export/${encodeURIComponent(task.id)}?fmt=md`;
    parts.push(`<div class="ai-export-row"><a class="ai-export-link" href="${exportUrl}">导出 Markdown 总结</a></div>`);

    resultBox.innerHTML = parts.join("");
    show(resultBox);
  }

  function showError(message) {
    if (pollTimer) {
      window.clearInterval(pollTimer);
      pollTimer = null;
    }
    hide(loading);
    hide(resultBox);
    errorBox.textContent = message || "总结失败，请稍后重试";
    show(errorBox);
    show(retryBtn);
    if (btn) btn.disabled = false;
    if (retryBtn) retryBtn.disabled = false;
  }

  function renderSubtitles(task) {
    const result = task.result || {};
    if (result.transcript_url && subtitleFileLink) {
      subtitleFileLink.href = result.transcript_url;
      subtitleFileLink.textContent = result.transcript_path || result.transcript_file || "字幕文件地址";
      show(subtitleFilePath);
    } else {
      hide(subtitleFilePath);
    }
    show(subtitleResult);
  }

  function showSubtitleError(message) {
    if (subtitlePollTimer) {
      window.clearInterval(subtitlePollTimer);
      subtitlePollTimer = null;
    }
    hide(subtitleLoading);
    subtitleError.textContent = message || "字幕提取失败，请稍后重试";
    show(subtitleError);
    setSubtitleButtonState("idle");
  }

  function pollSubtitleTask(taskId) {
    if (subtitlePollTimer) window.clearInterval(subtitlePollTimer);
    subtitlePollTimer = window.setInterval(async () => {
      try {
        const task = await api(`/api/subtitles/task/${encodeURIComponent(taskId)}`);
        if (task.status === "fetching_subtitle") {
          subtitleLoadingText.textContent = "正在提取视频字幕";
        } else if (task.status === "cache_hit") {
          subtitleLoadingText.textContent = "正在读取历史字幕";
        } else if (task.status === "transcribing_audio") {
          subtitleLoadingText.textContent = "正在下载音频并进行语音转写";
        } else if (task.status === "completed") {
          window.clearInterval(subtitlePollTimer);
          subtitlePollTimer = null;
          hide(subtitleLoading);
          hide(subtitleError);
          renderSubtitles(task);
          setSubtitleButtonState("success");
        } else if (task.status === "failed") {
          showSubtitleError(task.error);
        }
      } catch (error) {
        showSubtitleError(error.message || "查询字幕任务失败");
      }
    }, 1200);
  }

  async function startSubtitleExtraction() {
    if (!state || !state.parsed) return;

    const urlInput = $("#url-input");
    const url = state.parsed.webpage_url || (urlInput ? urlInput.value.trim() : "");
    if (!url) return;

    setSubtitleButtonState("loading");
    hide(subtitleResult);
    hide(subtitleError);
    show(subtitleLoading);
    subtitleLoadingText.textContent = "正在提取视频字幕";

    try {
      const task = await api("/api/subtitles", {
        method: "POST",
        body: JSON.stringify({ url }),
      });
      pollSubtitleTask(task.id);
    } catch (error) {
      showSubtitleError(error.message || "创建字幕任务失败");
    }
  }

  function pollTask(taskId) {
    if (pollTimer) window.clearInterval(pollTimer);
    pollTimer = window.setInterval(async () => {
      try {
        const task = await api(`/api/ai/task/${encodeURIComponent(taskId)}`);
        if (task.status === "fetching_subtitle") {
          loadingText.textContent = "正在获取视频字幕";
        } else if (task.status === "transcribing_audio") {
          loadingText.textContent = "正在下载音频并进行语音转写";
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

  async function startSummary() {
    if (!state || !state.parsed) return;

    const member = window.Member && window.Member.ensureMember ? await window.Member.ensureMember() : { ok: true };
    if (!member.ok) return;

    const urlInput = $("#url-input");
    const url = state.parsed.webpage_url || (urlInput ? urlInput.value.trim() : "");
    if (!url) return;

    if (btn) btn.disabled = true;
    if (retryBtn) retryBtn.disabled = true;
    hide(idle);
    hide(resultBox);
    hide(errorBox);
    hide(retryBtn);
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
        if (error.status === 403 && window.Member && window.Member.ensureMember) {
          resetPanel();
          window.Member.ensureMember();
        } else {
          showError(error.message || "创建总结任务失败");
        }
      });
  }

  function handleParsed(event) {
    resetPanel();
    const url = event.detail?.webpage_url || $("#url-input")?.value.trim() || "";
    restoreCachedHistory(url);
  }

  window.addEventListener("video:parsed", handleParsed);

  if (btn) btn.addEventListener("click", startSummary);
  if (retryBtn) retryBtn.addEventListener("click", startSummary);
  if (subtitleBtn) subtitleBtn.addEventListener("click", startSubtitleExtraction);

  window.startAiSummary = startSummary;
})();
