/* 聊天页逻辑：历史加载、流式对话（SSE，JSON 回退）、统计/周报抽屉。
 * 全部 DOM 写入走 createElement/textContent 等安全 API。 */
'use strict';

(function () {
  const chatEl = $('chat');
  const input = $('input');
  const hint = $('hint');
  const sendBtn = $('send');

  let pending = false;
  let aborter = null;

  // ── 消息渲染 ──────────────────────────────────────────────
  function addMsg(role, text) {
    const div = document.createElement('div');
    div.className = 'msg ' + role;
    div.textContent = text;
    chatEl.appendChild(div);
    chatEl.scrollTop = chatEl.scrollHeight;
    return div;
  }

  function makeRequestId() {
    return (window.crypto && crypto.randomUUID)
      ? crypto.randomUUID()
      : 'web-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 10);
  }

  // ── 流式请求（SSE；非流式响应自动回退一次性 JSON） ─────────
  // 服务端事件：meta / delta / done / error。done 携带清洗后的最终全文。
  async function streamChat(payload, opts) {
    const controller = new AbortController();
    const external = opts.signal;
    const onExternalAbort = () => controller.abort();
    if (external) {
      if (external.aborted) controller.abort();
      else external.addEventListener('abort', onExternalAbort);
    }
    let idleTimer = null;
    let timedOut = false;
    const armIdle = () => {
      if (!opts.idleMs) return;
      clearTimeout(idleTimer);
      idleTimer = setTimeout(() => { timedOut = true; controller.abort(); }, opts.idleMs);
    };

    try {
      armIdle();
      const resp = await fetch('/api/chat/stream', {
        method: 'POST',
        headers: authHeaders(),
        body: JSON.stringify(payload),
        signal: controller.signal,
      });
      const contentType = resp.headers.get('content-type') || '';
      if (!resp.ok || !contentType.includes('text/event-stream')) {
        // 回退：鉴权失败（4xx JSON）或服务端未提供流式端点时一次性读取
        let data = null;
        try { data = await resp.json(); } catch (e) { /* 保持 null */ }
        if (!resp.ok) {
          if (resp.status === 401) throw Object.assign(new Error('未授权：请先填写 API Token'), {kind: 'unauthorized'});
          if (resp.status === 403) throw Object.assign(new Error('没有权限执行该操作'), {kind: 'forbidden'});
          const detail = data && data.detail;
          const message =
            (detail && typeof detail === "object" && detail.message) ||
            (typeof detail === "string" && detail) ||
            ('请求失败（HTTP ' + resp.status + '）');
          throw Object.assign(new Error(message), {kind: 'api', status: resp.status, detail: detail});
        }
        return data;
      }

      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buf = '';
      let final = null;
      const handleFrame = (frame) => {
        let event = 'message';
        let dataStr = '';
        frame.split('\n').forEach((line) => {
          if (line.indexOf('event:') === 0) event = line.slice(6).trim();
          else if (line.indexOf('data:') === 0) dataStr += line.slice(5).trim();
        });
        if (!dataStr) return;
        let data;
        try { data = JSON.parse(dataStr); } catch (e) { return; }
        if (event === 'delta' && opts.onDelta && data.text) opts.onDelta(data.text);
        else if (event === 'done') final = data;
        else if (event === 'error') {
          throw Object.assign(new Error(data.message || '生成失败'), {kind: 'stream'});
        }
      };
      while (true) {
        const chunk = await reader.read();
        if (chunk.done) break;
        armIdle();
        buf += decoder.decode(chunk.value, {stream: true});
        let idx;
        while ((idx = buf.indexOf('\n\n')) >= 0) {
          const frame = buf.slice(0, idx).replace(/\r/g, '');
          buf = buf.slice(idx + 2);
          handleFrame(frame);
        }
      }
      return final;
    } catch (e) {
      if (e.name === 'AbortError' && timedOut) {
        throw new Error('响应超时：服务端长时间没有返回内容，请稍后重试');
      }
      if (!e.name || e.name !== 'AbortError') {
        if (e instanceof TypeError && !e.kind) {
          throw Object.assign(new Error('网络连接失败，请检查服务是否可达'), {kind: 'network'});
        }
      }
      throw e;
    } finally {
      clearTimeout(idleTimer);
      if (external) external.removeEventListener('abort', onExternalAbort);
    }
  }

  // ── 发送 ──────────────────────────────────────────────────
  function setPending(on) {
    pending = on;
    sendBtn.textContent = on ? '停止' : '发送';
    sendBtn.classList.toggle('stopping', on);
  }

  async function send() {
    if (pending) return; // 生成中不允许重复发送；停止走按钮
    const msg = input.value.trim();
    if (!msg) return;
    input.value = '';
    addMsg('user', msg);
    await dispatch(msg);
  }

  async function dispatch(msg) {
    const bubble = addMsg('assistant', '');
    bubble.classList.add('pending');
    hint.textContent = '思考中…';
    setPending(true);
    aborter = new AbortController();
    try {
      const final = await streamChat(
        {message: msg, request_id: makeRequestId()},
        {
          signal: aborter.signal,
          idleMs: 90000,
          onDelta: (t) => {
            bubble.classList.remove('pending');
            bubble.textContent += t;
            chatEl.scrollTop = chatEl.scrollHeight;
          },
        }
      );
      bubble.classList.remove('pending');
      if (final && typeof final.reply === 'string') {
        bubble.textContent = final.reply; // done 的最终文本已含服务端清洗，覆盖累计增量
      }
      hint.textContent = final && final.memories_used > 0
        ? '召回了 ' + final.memories_used + ' 条记忆'
        : '';
    } catch (e) {
      bubble.classList.remove('pending');
      if (e.name === 'AbortError') {
        if (!bubble.textContent) bubble.textContent = '（已停止生成）';
        hint.textContent = '';
      } else {
        renderError(bubble, e, msg);
      }
    } finally {
      setPending(false);
      aborter = null;
    }
  }

  function renderError(bubble, e, msg) {
    bubble.classList.remove('assistant');
    bubble.classList.add('error');
    bubble.textContent = e.kind === 'unauthorized'
      ? '未授权：请点左下角 🔑 填写 API Token'
      : (e.message || '请求失败');
    hint.textContent = '';
    const btn = document.createElement('button');
    btn.className = 'small';
    btn.textContent = '重试';
    btn.addEventListener('click', () => {
      bubble.remove();
      dispatch(msg);
    });
    bubble.appendChild(btn);
    chatEl.scrollTop = chatEl.scrollHeight;
    if (e.kind === 'unauthorized') openTokenModal();
  }

  // ── 历史加载 ──────────────────────────────────────────────
  async function loadHistory() {
    try {
      const data = await apiFetch('/api/messages?limit=50');
      const msgs = (data.messages || []).filter((m) => m.sender === 'user' || m.sender === 'assistant');
      if (!msgs.length) return; // 无历史时保留欢迎语
      clearNode(chatEl);
      msgs.forEach((m) => addMsg(m.sender, m.content));
      chatEl.scrollTop = chatEl.scrollHeight;
    } catch (e) {
      if (e.kind === 'unauthorized') openTokenModal();
      else toast(e.message, 'err');
    }
  }

  // ── 统计 / 周报抽屉（openDrawer/closeDrawer 由 app.js 提供）──
  function drawerError(body, e) {
    clearNode(body);
    body.textContent = e.message || '加载失败';
    if (e.kind === 'unauthorized') openTokenModal();
  }

  async function showStats() {
    openDrawer('📊 近 7 天行为统计');
    const body = $('drawer-body');
    body.textContent = '加载中…';
    try {
      const d = await apiFetch('/api/stats/summary?days=7');
      clearNode(body);
      body.textContent = [
        '对话 ' + d.messages + ' 条',
        'git 提交 ' + d.git_commits + ' 次',
        '工作日志 ' + d.work_logs + ' 条',
        '',
        '应用时长 Top：',
        (d.top_apps || []).map((a) => '· ' + a.name + ': ' + a.hours + 'h').join('\n') || '暂无数据',
      ].join('\n');
    } catch (e) { drawerError(body, e); }
  }

  async function showReports() {
    openDrawer('📋 周报');
    const body = $('drawer-body');
    body.textContent = '加载中…';
    try {
      const d = await apiFetch('/api/reports');
      const reports = d.reports || [];
      clearNode(body);
      if (!reports.length) {
        body.textContent = '暂无周报，每周生成后会出现在这里。';
        return;
      }
      reports.slice(0, 12).forEach((r) => {
        const btn = document.createElement('button');
        btn.className = 'report-item';
        const wk = document.createElement('span');
        wk.className = 'r-week';
        wk.textContent = r.week;
        btn.appendChild(wk);
        btn.appendChild(document.createTextNode(' 查阅 ›'));
        btn.addEventListener('click', () => showReportDetail(r.week));
        body.appendChild(btn);
      });
    } catch (e) { drawerError(body, e); }
  }

  async function showReportDetail(week) {
    const body = $('drawer-body');
    body.textContent = '加载中…';
    try {
      const rd = await apiFetch('/api/reports/' + encodeURIComponent(week));
      clearNode(body);
      const back = document.createElement('button');
      back.className = 'small';
      back.textContent = '‹ 返回列表';
      back.addEventListener('click', showReports);
      body.appendChild(back);
      const content = document.createElement('div');
      content.textContent = rd.content || '（空）';
      body.appendChild(content);
    } catch (e) { drawerError(body, e); }
  }


  // ── 事件绑定与启动 ────────────────────────────────────────
  sendBtn.addEventListener('click', () => { if (pending) { if (aborter) aborter.abort(); } else { send(); } });
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') send(); });
  $('nav-stats').addEventListener('click', (e) => { e.preventDefault(); showStats(); });
  $('nav-reports').addEventListener('click', (e) => { e.preventDefault(); showReports(); });

  setTokenSavedHook(loadHistory);
  loadHistory();
})();
