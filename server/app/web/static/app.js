/* 共享前端工具：API 请求封装、Token、Toast、主题、弹层辅助 */
'use strict';

const TOKEN_KEY = 'api_token';

function $(id) { return document.getElementById(id); }

function clearNode(node) {
  if (!node) return;
  while (node.firstChild) node.removeChild(node.firstChild);
}

function getToken() {
  return localStorage.getItem(TOKEN_KEY) || '';
}

function saveToken(value) {
  localStorage.setItem(TOKEN_KEY, (value || '').trim());
}

function authHeaders(extra) {
  const headers = Object.assign({'Content-Type': 'application/json'}, extra || {});
  const t = getToken();
  if (t) headers['Authorization'] = 'Bearer ' + t;
  return headers;
}

/**
 * 统一 API 请求：401 时抛出带标记的错误并触发 token 输入提示。
 * 返回解析后的 JSON。
 */
async function apiFetch(url, options) {
  const opts = Object.assign({}, options || {});
  // 调用方可补充自定义头，但不能意外覆盖 Token 鉴权头。
  opts.headers = authHeaders(opts.headers || {});
  let resp;
  try {
    resp = await fetch(url, opts);
  } catch (e) {
    if (e && e.name === 'AbortError') throw e;
    throw Object.assign(new Error('网络连接失败，请检查服务是否可达'), {kind: 'network'});
  }
  if (resp.status === 401) {
    const err = new Error('未授权：请先在设置中填写 API Token');
    err.kind = 'unauthorized';
    throw err;
  }
  if (resp.status === 403) {
    const err = new Error('没有权限执行该操作');
    err.kind = 'forbidden';
    throw err;
  }
  let data = null;
  try {
    data = await resp.json();
  } catch (e) { /* 非 JSON 响应保持 null */ }
  if (!resp.ok) {
    const detail = data && data.detail;
    // detail 兼容两种形状：结构化 {code, message}（novel/errors.py）与裸字符串（历史端点）
    const message =
      (detail && typeof detail === "object" && detail.message) ||
      (typeof detail === "string" && detail) ||
      ('请求失败（HTTP ' + resp.status + '）');
    const code = detail && typeof detail === 'object' ? (detail.code || '') : '';
    throw Object.assign(new Error(message), {kind: 'api', status: resp.status, code: code, detail: detail});
  }
  return data;
}

function toast(message, kind) {
  let box = document.getElementById('toast-box');
  if (!box) {
    box = document.createElement('div');
    box.id = 'toast-box';
    box.setAttribute('role', 'status');
    box.setAttribute('aria-live', 'polite');
    document.body.appendChild(box);
  }
  // 最多同时 4 条，防止异常轮询刷屏
  while (box.children.length >= 4) box.removeChild(box.firstChild);
  const item = document.createElement('div');
  item.className = 'toast' + (kind ? ' ' + kind : '');
  item.textContent = message;
  box.appendChild(item);
  setTimeout(() => item.remove(), 3200);
}

function applyTheme() {
  const saved = localStorage.getItem('ui_theme');
  const theme = saved || (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  document.documentElement.setAttribute('data-theme', theme);
}

function toggleTheme() {
  const next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
  localStorage.setItem('ui_theme', next);
  applyTheme();
}

/* ── 弹层/抽屉辅助：Esc 关闭 + 焦点管理 ─────────────────────
 * HTML <head> 里的内联脚本已预设 data-theme（防暗色白闪），
 * 这里再执行一次以同步动态创建的节点。 */
applyTheme();

const _escClosers = [];
function registerEscClose(fn) {
  _escClosers.push(fn);
  return () => {
    const i = _escClosers.indexOf(fn);
    if (i >= 0) _escClosers.splice(i, 1);
  };
}
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape' || !_escClosers.length) return;
  e.preventDefault();
  _escClosers[_escClosers.length - 1]();
});

const _overlayReturnFocus = {};
function openOverlay(node, focusTarget) {
  _overlayReturnFocus[node.id] = document.activeElement;
  node.classList.remove('hidden');
  if (focusTarget) focusTarget.focus();
}
function closeOverlay(node) {
  node.classList.add('hidden');
  const prev = _overlayReturnFocus[node.id];
  if (prev && typeof prev.focus === 'function') prev.focus();
  delete _overlayReturnFocus[node.id];
}

/* ── Token 弹层（两页共用）─────────────────────────────────
 * HTML 需含 token-modal / token-input / token-save / token-cancel；
 * onSaved 回调用于保存后的页面自举（聊天页重载历史、小说页重跑 boot）。 */
let _tokenEsc = null;
let _tokenOnSaved = null;
function setTokenSavedHook(fn) {
  /* 页面级保存回调：🔑 打开与代码触发都走它（聊天页重载历史/小说页重跑 boot）*/
  _tokenOnSaved = fn || null;
}
function openTokenModal(onSaved) {
  if (onSaved) _tokenOnSaved = onSaved;
  $('token-input').value = getToken();
  openOverlay($('token-modal'), $('token-input'));
  _tokenEsc = registerEscClose(closeTokenModal);
}
function closeTokenModal() {
  closeOverlay($('token-modal'));
  if (_tokenEsc) { _tokenEsc(); _tokenEsc = null; }
}
function saveTokenFromModal() {
  saveToken($('token-input').value);
  closeTokenModal();
  toast('Token 已保存', 'ok');
  if (_tokenOnSaved) _tokenOnSaved();
}

/* ── 右侧抽屉（两页共用：drawer / drawer-title / drawer-close / drawer-mask）── */
let _drawerEsc = null;
function openDrawer(title) {
  $('drawer-title').textContent = title;
  openOverlay($('drawer'), $('drawer-close'));
  _drawerEsc = registerEscClose(closeDrawer);
}
function closeDrawer() {
  closeOverlay($('drawer'));
  if (_drawerEsc) { _drawerEsc(); _drawerEsc = null; }
}

/* ── 通用错误态（加载失败的容器占位 + 重试按钮）───────────── */
function renderBoxError(box, e, retry) {
  while (box.firstChild) box.removeChild(box.firstChild);
  const empty = document.createElement('div');
  empty.className = 'empty';
  const icon = document.createElement('div');
  icon.className = 'icon';
  icon.textContent = '⚠️';
  const text = document.createElement('div');
  text.textContent = (e && e.message) || '加载失败';
  const btn = document.createElement('button');
  btn.className = 'small';
  btn.textContent = '重试';
  btn.addEventListener('click', () => { const r = retry(); if (r && r.catch) r.catch(() => {}); });
  empty.appendChild(icon);
  empty.appendChild(text);
  empty.appendChild(btn);
  box.appendChild(empty);
}

function fmtDate(iso) {
  if (!iso) return '—';
  try {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return '—';
    const pad = (n) => String(n).padStart(2, '0');
    return (d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes());
  } catch (e) {
    return '—';
  }
}

/* 两页共用的固定事件绑定：元素存在才挂（按页面装配）*/
(function bindShared() {
  const on = (id, fn) => { const el = $(id); if (el) el.addEventListener('click', fn); };
  on('token-btn', () => openTokenModal());
  on('token-save', saveTokenFromModal);
  on('token-cancel', closeTokenModal);
  on('drawer-close', closeDrawer);
  on('drawer-mask', closeDrawer);
  on('theme-btn', toggleTheme);
})();
