/* ============================================================
   在线代码评测与竞赛系统 — 公共前端运行时
   提供：认证、API 封装、导航渲染、工具函数、Toast 提示
   ============================================================ */
(function (global) {
  "use strict";

  const TOKEN_KEY = "oj_token";
  const USER_KEY = "oj_user";

  /* ---------- 存储 ---------- */
  const store = {
    get token() { return localStorage.getItem(TOKEN_KEY) || ""; },
    set token(v) { v ? localStorage.setItem(TOKEN_KEY, v) : localStorage.removeItem(TOKEN_KEY); },
    get user() {
      try { return JSON.parse(localStorage.getItem(USER_KEY) || "null"); }
      catch (e) { return null; }
    },
    set user(v) { v ? localStorage.setItem(USER_KEY, JSON.stringify(v)) : localStorage.removeItem(USER_KEY); },
  };

  /* ---------- API ---------- */
  async function api(path, opts) {
    opts = opts || {};
    const headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
    if (store.token) headers["Authorization"] = "Bearer " + store.token;
    let res;
    try {
      res = await fetch("/api" + path, {
        method: opts.method || "GET",
        headers,
        body: opts.body ? JSON.stringify(opts.body) : undefined,
      });
    } catch (e) {
      throw new Error("网络错误，无法连接服务器");
    }
    let data = {};
    try { data = await res.json(); } catch (e) { /* 非 JSON */ }
    if (res.status === 401) {
      logout();
      throw new Error(data.message || "未登录或登录已过期");
    }
    if (data.code !== 0) {
      throw new Error(data.message || ("请求失败 (" + res.status + ")"));
    }
    return data.data;
  }

  /* ---------- 认证 ---------- */
  function setSession(token, user) { store.token = token; store.user = user; }
  function logout() { store.token = ""; store.user = null; }
  function currentUser() { return store.user; }
  function isAdmin() { const u = store.user; return u && u.role === "admin"; }
  async function requireAuth() {
    if (!store.token) { goLogin(); throw new Error("请先登录"); }
    try { const u = await api("/auth/me"); store.user = u; return u; }
    catch (e) { goLogin(); throw e; }
  }
  function goLogin() {
    if (location.pathname.endsWith("index.html") || location.pathname === "/") return;
    location.href = "/index.html?login=1";
  }

  /* ---------- 导航 ---------- */
  const NAV = [
    ["/index.html", "题目列表", "index"],
    ["/submissions.html", "提交记录", "submissions"],
    ["/leaderboard.html", "排行榜", "leaderboard"],
    ["/contest.html", "竞赛", "contest"],
    ["/forum.html", "讨论区", "forum"],
    ["/stats.html", "统计报表", "stats"],
    ["/users.html", "用户管理", "users", true],
    ["/settings.html", "系统设置", "settings", true],
  ];

  function renderNav(active) {
    const el = document.getElementById("nav");
    if (!el) return;
    const user = currentUser();
    const admin = isAdmin();
    let links = "";
    for (const [href, label, key, adminOnly] of NAV) {
      if (adminOnly && !admin) continue;
      links += `<a href="${href}" class="${active === key ? "active" : ""}">${label}</a>`;
    }
    el.innerHTML = `
      <div class="nav">
        <div class="nav-inner">
          <a class="nav-brand" href="/index.html"><span class="logo">OJ</span><span>在线评测与竞赛</span></a>
          <div class="nav-links">${links}</div>
          <div class="nav-user">
            ${user
              ? `<span class="uname">${esc(user.nickname || user.username)}</span>` +
                (admin ? `<span class="role-tag">管理员</span>` : "") +
                `<button class="btn btn-outline btn-sm" onclick="OJ.logout();location.href='/index.html'">退出</button>`
              : `<button class="btn btn-primary btn-sm" onclick="location.href='/index.html?login=1'">登录</button>`}
          </div>
        </div>
      </div>`;
  }

  /* ---------- 工具 ---------- */
  function esc(s) {
    if (s === null || s === undefined) return "";
    return String(s).replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
    ));
  }

  function fmtTime(s) {
    if (!s) return "-";
    // 兼容 ISO 与 "YYYY-MM-DDTHH:MM:SS"
    return String(s).replace("T", " ");
  }

  function fmtDuration(ms) {
    if (!ms && ms !== 0) return "-";
    if (ms < 1000) return ms + " ms";
    if (ms < 60000) return (ms / 1000).toFixed(2) + " s";
    return (ms / 60000).toFixed(2) + " min";
  }

  function fmtMem(kb) {
    if (!kb && kb !== 0) return "-";
    if (kb < 1024) return kb + " KB";
    return (kb / 1024).toFixed(2) + " MB";
  }

  function verdictBadge(status) {
    const s = status || "PENDING";
    return `<span class="badge ${s}">${s}</span>`;
  }

  function difficultyLabel(d) {
    const names = ["", "入门", "简单", "中等", "较难", "困难"];
    const n = Number(d) || 1;
    return `<span class="diff diff-${n}">${names[n] || "困难"}</span>`;
  }

  function toast(message, type) {
    let wrap = document.querySelector(".toast-wrap");
    if (!wrap) { wrap = document.createElement("div"); wrap.className = "toast-wrap"; document.body.appendChild(wrap); }
    const t = document.createElement("div");
    t.className = "toast " + (type || "");
    t.textContent = message;
    wrap.appendChild(t);
    setTimeout(() => { t.style.opacity = "0"; t.style.transition = "opacity .3s"; setTimeout(() => t.remove(), 320); }, 3200);
  }

  function el(id) { return document.getElementById(id); }

  function openModal(title, bodyHTML) {
    const mask = document.createElement("div");
    mask.className = "modal-mask";
    mask.innerHTML = `<div class="modal"><h3>${esc(title)}</h3><div class="modal-body">${bodyHTML}</div>
      <div class="modal-actions"><button class="btn btn-outline" onclick="this.closest('.modal-mask').remove()">关闭</button></div></div>`;
    mask.addEventListener("click", (e) => { if (e.target === mask) mask.remove(); });
    document.body.appendChild(mask);
    return mask;
  }

  function confirmDialog(message) {
    return new Promise((resolve) => {
      const mask = document.createElement("div");
      mask.className = "modal-mask";
      mask.innerHTML = `<div class="modal" style="max-width:420px"><h3>确认操作</h3>
        <p>${esc(message)}</p>
        <div class="modal-actions">
          <button class="btn btn-outline" data-a="cancel">取消</button>
          <button class="btn btn-danger" data-a="ok">确定</button>
        </div></div>`;
      mask.addEventListener("click", (e) => {
        if (e.target === mask) { mask.remove(); resolve(false); }
        const a = e.target.getAttribute && e.target.getAttribute("data-a");
        if (a === "ok") { mask.remove(); resolve(true); }
        if (a === "cancel") { mask.remove(); resolve(false); }
      });
      document.body.appendChild(mask);
    });
  }

  /* ---------- 页面引导 ---------- */
  async function boot(activeKey) {
    renderNav(activeKey);
    // 若 URL 带 login=1 且未登录，跳登录
    if (new URLSearchParams(location.search).get("login") === "1" && !currentUser()) {
      // 登录在题目列表页内联处理
    }
  }

  // 倒计时组件
  function countdown(targetIso, elId) {
    const target = new Date(targetIso.replace(" ", "T")).getTime();
    const box = el(elId);
    if (!box) return;
    const tick = () => {
      const diff = target - Date.now();
      if (diff <= 0) { box.innerHTML = '<span class="badge AC">已到时间</span>'; return; }
      const d = Math.floor(diff / 86400000);
      const h = Math.floor((diff % 86400000) / 3600000);
      const m = Math.floor((diff % 3600000) / 60000);
      const s = Math.floor((diff % 60000) / 1000);
      const pad = (n) => String(n).padStart(2, "0");
      box.innerHTML = `<span class="countdown">${d > 0 ? d + "d " : ""}${pad(h)}:${pad(m)}:${pad(s)}</span>`;
    };
    tick();
    return setInterval(tick, 1000);
  }

  global.OJ = {
    api, esc, fmtTime, fmtDuration, fmtMem, verdictBadge, difficultyLabel,
    toast, el, openModal, confirmDialog, boot, countdown,
    setSession, logout, currentUser, isAdmin, requireAuth, goLogin,
    store,
  };
})(window);
