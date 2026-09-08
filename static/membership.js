/* 会员状态、购买弹窗与模拟支付流程 */
(function () {
  "use strict";

  const TOKEN_KEY = "member_token";
  let token = localStorage.getItem(TOKEN_KEY) || "";
  let currentStatus = { member: false };

  function formatPrice(cents) {
    return (Number(cents || 0) / 100).toFixed(2);
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll(String.fromCharCode(34), "&quot;")
      .replaceAll(String.fromCharCode(39), "&#039;");
  }

  async function api(path, options = {}) {
    const { useMemberToken = true, ...rest } = options;
    const authHeaders = window.Auth && window.Auth.getHeaders ? window.Auth.getHeaders() : {};
    const headers = { "Content-Type": "application/json", ...authHeaders, ...(rest.headers || {}) };
    if (token && useMemberToken) headers["X-Member-Token"] = token;
    const response = await fetch(path, { ...rest, headers });
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

  async function ensureToken() {
    if (token) return token;
    const data = await api("/api/member/init", { method: "POST" });
    token = data.token;
    localStorage.setItem(TOKEN_KEY, token);
    return token;
  }

  async function getToken() {
    await ensureToken();
    return token;
  }

  function renderNav() {
    const btn = document.getElementById("member-nav-btn");
    if (!btn) return;
    const loggedIn = window.Auth && window.Auth.getToken && window.Auth.getToken();
    if (!loggedIn) {
      btn.textContent = "会员未开通";
      btn.classList.remove("is-member");
      return;
    }
    if (currentStatus.member) {
      const days = Math.max(0, Math.ceil((Number(currentStatus.expires_at || 0) * 1000 - Date.now()) / 86400000));
      btn.textContent = `会员已开通 · 剩余 ${days} 天`;
    } else {
      btn.textContent = "开通会员";
    }
    btn.classList.toggle("is-member", Boolean(currentStatus.member));
  }

  function closeModal() {
    const old = document.getElementById("member-modal-backdrop");
    if (old) old.remove();
  }

  function openModal() {
    closeModal();
    const price = formatPrice(currentStatus.price_cents);
    const modal = document.createElement("div");
    modal.id = "member-modal-backdrop";
    modal.className = "member-modal-backdrop";
    modal.innerHTML = `
      <div class="member-modal" role="dialog" aria-modal="true" aria-labelledby="member-modal-title">
        <div class="member-modal-head">
          <h2 id="member-modal-title">开通会员</h2>
          <button class="member-modal-close" id="member-modal-close" type="button" aria-label="关闭">×</button>
        </div>
        <div class="member-plan">
          <p class="member-plan-name">${escapeHtml(currentStatus.plan_name || "30天会员")}</p>
          <p class="member-price">¥ ${price}<small> / 30天</small></p>
          <ul class="member-benefits">
            <li>无限次 AI 视频总结</li>
            <li>视频下载与 MP3 提取继续免费</li>
            <li>支付成功即时开通</li>
          </ul>
        </div>
        <div class="member-pay-actions">
          <button class="member-pay-btn alipay" data-channel="alipay" type="button">支付宝支付</button>
          <button class="member-pay-btn wechat" data-channel="wechat" type="button">微信支付</button>
        </div>
        <a class="member-pay-link is-hidden" id="member-pay-link" href="#" target="_blank" rel="noopener noreferrer">如果未自动打开，点这里继续支付</a>
        <p class="member-modal-note">当前为模拟支付模式，不会产生真实扣款。</p>
      </div>`;
    document.body.appendChild(modal);
    modal.querySelector("#member-modal-close").addEventListener("click", closeModal);
    modal.addEventListener("click", (event) => {
      if (event.target === modal) closeModal();
    });
    modal.querySelectorAll(".member-pay-btn").forEach((button) => {
      button.addEventListener("click", () => handlePay(button));
    });
  }

  async function handlePay(button) {
    const channel = button.dataset.channel;
    const payLink = document.getElementById("member-pay-link");
    button.disabled = true;
    button.textContent = "创建订单中...";
    try {
      await ensureToken();
      const order = await api("/api/member/orders", {
        method: "POST",
        body: JSON.stringify({ channel }),
      });
      payLink.href = order.pay_url;
      payLink.classList.remove("is-hidden");
      window.open(order.pay_url, "_blank", "width=420,height=560");
      pollOrder(order.id);
    } catch (error) {
      button.textContent = channel === "alipay" ? "支付宝支付" : "微信支付";
      button.disabled = false;
      if (window.showToast) window.showToast(error.message || "创建订单失败", "error");
    }
  }

  function pollOrder(orderId) {
    let attempts = 0;
    const timer = window.setInterval(async () => {
      attempts += 1;
      try {
        const order = await api(`/api/member/orders/${encodeURIComponent(orderId)}`);
        if (order.status === "paid") {
          window.clearInterval(timer);
          closeModal();
          await refreshStatus();
          if (window.showToast) window.showToast("会员已开通", "success");
        } else if (attempts > 120) {
          window.clearInterval(timer);
        }
      } catch (error) {
        window.clearInterval(timer);
      }
    }, 1500);
  }

  async function refreshStatus() {
    const loggedIn = window.Auth && window.Auth.getToken && window.Auth.getToken();
    if (!loggedIn) {
      currentStatus = await api("/api/member/status", { useMemberToken: false });
      currentStatus.member = false;
      renderNav();
      return currentStatus;
    }
    await ensureToken();
    currentStatus = await api("/api/member/status");
    renderNav();
    return currentStatus;
  }

  async function ensureMember() {
    if (window.Auth && !(await window.Auth.ensureLoggedIn())) return { ok: false, token };
    await refreshStatus();
    if (currentStatus.member) return { ok: true, token };
    openModal();
    return { ok: false, token };
  }

  function openPurchaseModal() {
    if (window.Auth) {
      window.Auth.ensureLoggedIn().then((ok) => {
        if (ok) openModal();
      });
    } else {
      openModal();
    }
  }

  const navBtn = document.getElementById("member-nav-btn");
  if (navBtn) navBtn.addEventListener("click", openPurchaseModal);

  window.Member = {
    getToken: getToken,
    ensureMember: ensureMember,
    refreshStatus: refreshStatus,
    openModal: openModal,
  };

  refreshStatus().catch(() => {});
})();
