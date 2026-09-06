/* 共享前端工具：API 请求封装、Token、Toast、主题、弹层辅助 */
'use strict';

const TOKEN_KEY = 'api_token';

function $(id) { return document.getElementById(id); }

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
  const opts = Object.assign({headers: authHeaders()}, options || {});
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
    const message = (detail && detail.message) || ('请求失败（HTTP ' + resp.status + '）');
    throw Object.assign(new Error(message), {kind: 'api', status: resp.status, detail: detail});
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
