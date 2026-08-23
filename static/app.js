const state = {
  parsed: null,
  selectedKind: "video",
  selectedFormat: "bestvideo+bestaudio/best",
  currentTaskId: null,
  pollTimer: null,
};

const platformExamples = [
  { name: "B站", placeholder: "粘贴 bilibili.com 视频链接" },
  { name: "YouTube", placeholder: "粘贴 youtube.com/watch?v= 链接" },
  { name: "抖音", placeholder: "粘贴 douyin.com 视频链接" },
  { name: "小红书", placeholder: "粘贴 xiaohongshu.com 视频链接" },
  { name: "X / Twitter", placeholder: "粘贴 x.com 或 twitter.com 链接" },
  { name: "Instagram", placeholder: "粘贴 instagram.com 视频链接" },
];

const $ = (selector) => document.querySelector(selector);

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function extractFirstUrl(value) {
  const text = String(value || "");
  const match = text.match(/https?:\/\/[^\s"'<>]+/i);
  return match ? match[0].replace(/[),.;，。；]+$/, "") : "";
}

function formatBytes(bytes) {
  if (!bytes && bytes !== 0) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = Number(bytes);
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return `${value.toFixed(value >= 10 || index === 0 ? 0 : 1)} ${units[index]}`;
}

function formatEta(eta) {
  if (eta === null || eta === undefined || eta < 0) return "";
  const total = Math.max(0, Math.ceil(eta));
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  if (minutes) return `预计剩余 ${minutes} 分 ${seconds} 秒`;
  return `预计剩余 ${seconds} 秒`;
}

function formatSpeed(speed) {
  if (!speed) return "";
  return `${formatBytes(speed)}/s`;
}

function formatTime(timestamp) {
  if (!timestamp) return "";
  const date = new Date(timestamp * 1000);
  return date.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function showToast(message, type = "info") {
  const region = $("#toast-region");
  const toast = document.createElement("div");
  toast.className = `toast ${type}`;
  toast.textContent = message;
  region.appendChild(toast);
  window.setTimeout(() => {
    toast.style.opacity = "0";
    toast.style.transform = "translateY(8px)";
    window.setTimeout(() => toast.remove(), 250);
  }, 3600);
}

function setButtonLoading(button, loading) {
  if (!button) return;
  const label = button.querySelector(".btn-label");
  if (label) {
    if (!label.dataset.default) label.dataset.default = label.textContent;
    label.textContent = loading ? "处理中..." : label.dataset.default;
  }
  button.disabled = loading;
}

function setParseLoading(loading) {
  $("#parse-loading").classList.toggle("is-hidden", !loading);
  if (loading) {
    $("#result-card").classList.add("is-hidden");
  }
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
    const detail = data?.detail || `请求失败（${response.status}）`;
    throw new Error(detail);
  }
  return data;
}

function renderPlatformChips() {
  const container = $("#platform-chips");
  container.innerHTML = platformExamples
    .map(
      (item) =>
        `<button class="platform-chip" type="button" data-placeholder="${escapeHtml(item.placeholder)}">${escapeHtml(item.name)}</button>`,
    )
    .join("");
}

function renderResult(data) {
  state.parsed = data;
  state.selectedKind = "video";
  state.selectedFormat = data.videoOptions?.[0]?.id || "bestvideo+bestaudio/best";

  $("#result-title").textContent = data.title || "未知视频";
  $("#result-platform").textContent = data.extractor || "视频";
  $("#result-meta").textContent = [
    data.uploader ? `作者：${data.uploader}` : "",
    data.duration ? `时长：${data.duration}` : "",
  ]
    .filter(Boolean)
    .join(" · ");

  const thumb = $("#result-thumb");
  const fallback = $("#preview-fallback");
  fallback.classList.remove("is-hidden");
  if (data.thumbnail) {
    thumb.onload = () => fallback.classList.add("is-hidden");
    thumb.onerror = () => fallback.classList.remove("is-hidden");
    thumb.src = data.thumbnail;
  } else {
    thumb.removeAttribute("src");
  }

  renderQualityOptions();
  $("#result-card").classList.remove("is-hidden");
  $("#progress-card").classList.add("is-hidden");
  window.setTimeout(() => {
    $("#result-card").scrollIntoView({ behavior: "smooth", block: "center" });
  }, 80);
}

function renderQualityOptions() {
  const container = $("#quality-options");
  const options =
    state.selectedKind === "audio"
      ? [state.parsed.audioOption]
      : state.parsed.videoOptions || [];

  container.innerHTML = options
    .map((option) => {
      const active = option.id === state.selectedFormat ? " active" : "";
      return `<button class="quality-chip${active}" type="button" data-format="${escapeHtml(option.id)}" title="${escapeHtml(option.hint || "")}">${escapeHtml(option.label)}</button>`;
    })
    .join("");
}

function handleParseSubmit(event) {
  event.preventDefault();
  const rawValue = $("#url-input").value.trim();
  const url = extractFirstUrl(rawValue);
  if (!url) {
    showToast("没有找到有效链接，请粘贴分享文本或完整网址", "error");
    return;
  }

  const button = $("#parse-btn");
  setButtonLoading(button, true);
  setParseLoading(true);

  api("/api/parse", {
    method: "POST",
    body: JSON.stringify({ url }),
  })
    .then((data) => {
      renderResult(data);
      showToast("解析成功，请选择格式后下载", "success");
    })
    .catch((error) => {
      showToast(error.message || "解析失败，请稍后重试", "error");
    })
    .finally(() => {
      setButtonLoading(button, false);
      setParseLoading(false);
    });
}

function handleFormatTabClick(event) {
  const button = event.target.closest(".segment");
  if (!button) return;

  state.selectedKind = button.dataset.kind;
  document.querySelectorAll("#format-tabs .segment").forEach((item) => {
    item.classList.toggle("active", item === button);
  });

  const options =
    state.selectedKind === "audio"
      ? [state.parsed.audioOption]
      : state.parsed.videoOptions || [];
  state.selectedFormat = options[0]?.id || "bestaudio/best";
  renderQualityOptions();
}

function handleQualityClick(event) {
  const button = event.target.closest(".quality-chip");
  if (!button) return;
  state.selectedFormat = button.dataset.format;
  document.querySelectorAll("#quality-options .quality-chip").forEach((item) => {
    item.classList.toggle("active", item === button);
  });
}

function startDownload(button) {
  if (!state.parsed || !button) return;

  setButtonLoading(button, true);
  api("/api/download", {
    method: "POST",
    body: JSON.stringify({
      url: state.parsed.webpage_url || $("#url-input").value.trim(),
      kind: state.selectedKind,
      format_id: state.selectedFormat,
    }),
  })
    .then((task) => {
      state.currentTaskId = task.id;
      $("#progress-card").classList.remove("is-hidden");
      $("#progress-title").textContent = task.title || state.parsed.title || "正在准备下载";
      renderProgress(task);
      startPolling(task.id);
    })
    .catch((error) => {
      showToast(error.message || "创建下载任务失败", "error");
    })
    .finally(() => {
      setButtonLoading(button, false);
    });
}

function handleDownloadClick() {
  if (!state.parsed) return;

  state.selectedKind = "video";
  const videoOptions = state.parsed.videoOptions || [];
  if (!videoOptions.some((option) => option.id === state.selectedFormat)) {
    state.selectedFormat = videoOptions[0]?.id || "bestvideo+bestaudio/best";
  }
  document.querySelectorAll("#format-tabs .segment").forEach((item) => {
    item.classList.toggle("active", item.dataset.kind === "video");
  });
  renderQualityOptions();
  startDownload($("#download-btn"));
}

function handleAudioDownloadClick() {
  if (!state.parsed) return;

  state.selectedKind = "audio";
  state.selectedFormat = state.parsed.audioOption?.id || "bestaudio/best";
  document.querySelectorAll("#format-tabs .segment").forEach((item) => {
    item.classList.toggle("active", item.dataset.kind === "audio");
  });
  renderQualityOptions();
  startDownload($("#audio-download-btn"));
}

function renderProgress(task) {
  const status = task.status || "queued";
  const statusPill = $("#task-status");
  statusPill.className = "status-pill";
  statusPill.textContent = {
    queued: "排队中",
    downloading: "下载中",
    completed: "已完成",
    failed: "失败",
  }[status] || status;

  if (status === "failed") statusPill.classList.add("failed");
  if (status === "completed") statusPill.classList.add("completed");

  const percent = Math.min(100, Math.max(0, Number(task.progress || 0)));
  $("#progress-percent").textContent = `${Math.round(percent)}%`;
  $("#progress-fill").style.width = `${percent}%`;

  const stats = [
    task.speed ? `速度 ${formatSpeed(task.speed)}` : "",
    formatEta(task.eta),
    task.file_size ? `大小 ${formatBytes(task.file_size)}` : "",
    task.error ? `错误：${task.error}` : "",
  ].filter(Boolean);
  $("#progress-stats").textContent = stats.join(" · ");

  const link = $("#download-link");
  if (status === "completed") {
    link.href = `/api/file/${task.id}`;
    link.classList.remove("is-hidden");
  } else {
    link.classList.add("is-hidden");
  }
}

function startPolling(taskId) {
  stopPolling();
  state.currentTaskId = taskId;

  const poll = async () => {
    try {
      const task = await api(`/api/task/${taskId}`);
      renderProgress(task);
      if (task.status === "completed") {
        stopPolling();
        showToast("下载完成，点击“保存到本地”即可保存", "success");
        loadHistory();
      } else if (task.status === "failed") {
        stopPolling();
        showToast(task.error || "下载失败，请稍后重试", "error");
      }
    } catch (error) {
      stopPolling();
      showToast(error.message || "获取任务状态失败", "error");
    }
  };

  poll();
  state.pollTimer = window.setInterval(poll, 1200);
}

function stopPolling() {
  if (state.pollTimer) {
    window.clearInterval(state.pollTimer);
    state.pollTimer = null;
  }
}

function renderHistory(tasks) {
  const container = $("#history-list");
  const empty = $("#empty-history");
  container.innerHTML = "";

  if (!tasks.length) {
    empty.classList.remove("is-hidden");
    return;
  }

  empty.classList.add("is-hidden");
  const statusText = {
    queued: "排队中",
    downloading: "下载中",
    completed: "已完成",
    failed: "失败",
  };

  tasks.forEach((task) => {
    const item = document.createElement("article");
    item.className = "history-item";

    const isCompleted = task.status === "completed";
    const title = task.title || task.url || "下载任务";
    const meta = [
      statusText[task.status] || task.status,
      task.kind === "audio" ? "MP3" : "MP4",
      task.file_size ? formatBytes(task.file_size) : "",
      formatTime(task.finished_at || task.created_at),
    ]
      .filter(Boolean)
      .join(" · ");

    item.innerHTML = `
      <div class="history-icon">
        <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path d="M12 4v12m0 0 5-5m-5 5-5-5M5 19h14" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
      </div>
      <div class="history-main">
        <h3>${escapeHtml(title)}</h3>
        <p>${escapeHtml(meta)}</p>
      </div>
      ${
        isCompleted
          ? `<a class="history-action" href="/api/file/${task.id}">
              <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
                <path d="M12 4v12m0 0 5-5m-5 5-5-5M5 19h14" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
              </svg>
              下载
            </a>`
          : '<span class="history-action">查看</span>'
      }
    `;
    container.appendChild(item);
  });
}

async function loadHistory() {
  try {
    const data = await api("/api/history?limit=20");
    renderHistory(data.tasks || []);
  } catch (error) {
    showToast(error.message || "获取下载记录失败", "error");
  }
}

async function pasteFromClipboard() {
  try {
    const text = await navigator.clipboard.readText();
    if (text) {
      const url = extractFirstUrl(text);
      $("#url-input").value = url || text.trim();
      $("#url-input").focus();
      showToast(url ? "已粘贴链接" : "已粘贴内容，请确认后解析", "success");
    }
  } catch {
    showToast("浏览器没有授权读取剪贴板，请手动粘贴", "error");
  }
}

function init() {
  renderPlatformChips();
  loadHistory();

  $("#parse-form").addEventListener("submit", handleParseSubmit);
  $("#paste-btn").addEventListener("click", pasteFromClipboard);
  $("#format-tabs").addEventListener("click", handleFormatTabClick);
  $("#quality-options").addEventListener("click", handleQualityClick);
  $("#download-btn").addEventListener("click", handleDownloadClick);
  $("#audio-download-btn").addEventListener("click", handleAudioDownloadClick);
  $("#refresh-history").addEventListener("click", loadHistory);

  $("#platform-chips").addEventListener("click", (event) => {
    const chip = event.target.closest(".platform-chip");
    if (!chip) return;
    $("#url-input").placeholder = chip.dataset.placeholder;
    $("#url-input").focus();
  });
}

init();
