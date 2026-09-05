/* 共享前端工具：API 请求封装、Token、Toast、主题 */
'use strict';

const TOKEN_KEY = 'api_token';

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
    document.body.appendChild(box);
  }
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

applyTheme();
