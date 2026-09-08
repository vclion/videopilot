/* 用户注册登录与 JWT 无状态认证 */
(function () {
  "use strict";

  const TOKEN_KEY = "access_token";
  let token = localStorage.getItem(TOKEN_KEY) || "";
  let currentUser = null;
  let loginResolve = null;

  function getToken() {
    return token;
  }

  function getHeaders() {
    return token ? { "Authorization": `Bearer ${token}` } : {};
  }

  async function api(path, options = {}) {
    const headers = { "Content-Type": "application/json", ...getHeaders(), ...(options.headers || {}) };
    const response = await fetch(path, { ...options, headers });
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

  function closeModal() {
    const old = document.getElementById("auth-modal-backdrop");
    if (old) old.remove();
    if (loginResolve) {
      loginResolve(false);
      loginResolve = null;
    }
  }

  function openModal(mode) {
    closeModal();
    const modal = document.createElement("div");
    modal.id = "auth-modal-backdrop";
    modal.className = "auth-modal-backdrop";
    modal.innerHTML = `
      <div class="auth-modal" role="dialog" aria-modal="true" aria-labelledby="auth-modal-title">
        <div class="auth-modal-head">
          <h2 id="auth-modal-title">登录</h2>
          <button class="auth-modal-close" id="auth-modal-close" type="button" aria-label="关闭">×</button>
        </div>
        <div class="auth-tabs">
          <button class="auth-tab active" data-mode="login" type="button">登录</button>
          <button class="auth-tab" data-mode="register" type="button">注册</button>
        </div>
        <form id="auth-form" class="auth-form">
          <div class="auth-field">
            <label for="auth-account">邮箱或账号</label>
            <input id="auth-account" name="account" type="text" autocomplete="username" placeholder="admin 或 admin@example.com" />
          </div>
          <div class="auth-field is-hidden" id="auth-email-field">
            <label for="auth-email">邮箱</label>
            <input id="auth-email" name="email" type="email" autocomplete="email" placeholder="you@example.com" />
          </div>
          <div class="auth-field">
            <label for="auth-password">密码</label>
            <input id="auth-password" name="password" type="password" autocomplete="current-password" placeholder="至少 8 位" />
          </div>
          <button class="auth-submit" type="submit">登录</button>
        </form>
      </div>`;
    document.body.appendChild(modal);
    modal.querySelector("#auth-modal-close").addEventListener("click", closeModal);
    modal.addEventListener("click", (event) => {
      if (event.target === modal) closeModal();
    });
    modal.querySelectorAll(".auth-tab").forEach((tab) => {
      tab.addEventListener("click", () => switchMode(tab.dataset.mode));
    });
    modal.querySelector("#auth-form").addEventListener("submit", handleSubmit);
    switchMode(mode || "login");
  }

  function switchMode(mode) {
    const modal = document.getElementById("auth-modal-backdrop");
    if (!modal) return;
    modal.querySelectorAll(".auth-tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.mode === mode));
    modal.querySelector("#auth-modal-title").textContent = mode === "register" ? "注册账号" : "登录";
    modal.querySelector("#auth-submit-text")?.remove();
    const emailField = modal.querySelector("#auth-email-field");
    emailField.classList.toggle("is-hidden", mode !== "register");
    const accountLabel = modal.querySelector("label[for=\"auth-account\"]");
    if (accountLabel) accountLabel.textContent = mode === "register" ? "登录账号（可选）" : "邮箱或账号";
  }

  async function handleSubmit(event) {
    event.preventDefault();
    const modal = document.getElementById("auth-modal-backdrop");
    const account = modal.querySelector("#auth-account").value.trim();
    const email = modal.querySelector("#auth-email").value.trim();
    const password = modal.querySelector("#auth-password").value;
    const submit = modal.querySelector(".auth-submit");
    const mode = modal.querySelector(".auth-tab.active").dataset.mode;
    submit.disabled = true;
    submit.textContent = "处理中...";
    try {
      const body = mode === "register" ? { email, password } : { account, password };
      const data = await api(mode === "register" ? "/api/auth/register" : "/api/auth/login", {
        method: "POST",
        body: JSON.stringify(body),
      });
      setLoggedIn(data.token, data.user);
    } catch (error) {
      if (window.showToast) window.showToast(error.message || "操作失败", "error");
      submit.disabled = false;
      submit.textContent = mode === "register" ? "注册" : "登录";
    }
  }

  function setLoggedIn(newToken, user) {
    token = newToken;
    currentUser = user;
    localStorage.setItem(TOKEN_KEY, token);
    renderNav();
    if (loginResolve) {
      const resolve = loginResolve;
      loginResolve = null;
      resolve(true);
    }
    closeModal();
    if (window.Member && window.Member.refreshStatus) window.Member.refreshStatus();
  }

  function logout() {
    token = "";
    currentUser = null;
    localStorage.removeItem(TOKEN_KEY);
    renderNav();
    if (window.Member && window.Member.refreshStatus) window.Member.refreshStatus();
  }

  function renderNav() {
    const loginBtn = document.getElementById("auth-nav-btn");
    const logoutBtn = document.getElementById("auth-logout-btn");
    const label = document.getElementById("auth-user-label");
    if (!loginBtn) return;
    if (currentUser) {
      loginBtn.classList.add("is-hidden");
      logoutBtn.classList.remove("is-hidden");
      if (label) {
        label.textContent = currentUser.email || currentUser.username || "";
        label.classList.remove("is-hidden");
      }
    } else {
      loginBtn.classList.remove("is-hidden");
      logoutBtn.classList.add("is-hidden");
      if (label) label.classList.add("is-hidden");
    }
  }

  async function refreshUser() {
    if (!token) return null;
    try {
      const data = await api("/api/auth/me");
      currentUser = data.user;
      renderNav();
      return currentUser;
    } catch {
      token = "";
      currentUser = null;
      localStorage.removeItem(TOKEN_KEY);
      renderNav();
      return null;
    }
  }

  function ensureLoggedIn() {
    if (currentUser) return Promise.resolve(true);
    openModal("login");
    return new Promise((resolve) => {
      loginResolve = resolve;
    });
  }

  const loginBtn = document.getElementById("auth-nav-btn");
  const logoutBtn = document.getElementById("auth-logout-btn");
  if (loginBtn) loginBtn.addEventListener("click", () => openModal("login"));
  if (logoutBtn) logoutBtn.addEventListener("click", logout);

  window.Auth = {
    getToken: getToken,
    getHeaders: getHeaders,
    ensureLoggedIn: ensureLoggedIn,
    refreshUser: refreshUser,
    logout: logout,
  };

  refreshUser();
})();
