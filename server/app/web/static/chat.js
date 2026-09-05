/* 聊天页逻辑（从旧 index.html 内联脚本迁移） */
'use strict';

const chat = document.getElementById('chat');
const input = document.getElementById('input');
const hint = document.getElementById('hint');
const tokenInput = document.getElementById('token');

tokenInput.value = getToken();
function onSaveToken() {
  saveToken(tokenInput.value);
  hint.textContent = 'Token 已保存';
}
tokenInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') onSaveToken(); });
document.getElementById('save-token').addEventListener('click', onSaveToken);

function addMsg(role, text) {
  const div = document.createElement('div');
  div.className = 'msg ' + role;
  div.textContent = text;
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
}

async function send() {
  const msg = input.value.trim();
  if (!msg) return;
  input.value = '';
  addMsg('user', msg);
  hint.textContent = '思考中…';
  try {
    const data = await apiFetch('/api/chat', {
      method: 'POST',
      body: JSON.stringify({message: msg}),
    });
    addMsg('assistant', data.reply);
    hint.textContent = data.memories_used > 0 ? '召回了 ' + data.memories_used + ' 条记忆' : '';
  } catch (e) {
    addMsg('assistant', e.message);
    hint.textContent = '';
  }
}

document.getElementById('send').addEventListener('click', send);
input.addEventListener('keydown', (e) => { if (e.key === 'Enter') send(); });

async function showStats() {
  try {
    const d = await apiFetch('/api/stats/summary?days=7');
    addMsg('assistant', '📊 近7天统计：\n' +
      '对话 ' + d.messages + ' 条，git 提交 ' + d.git_commits + ' 次，工作日志 ' + d.work_logs + ' 条\n\n' +
      '应用时长 Top：\n' + (d.top_apps.map((a) => '· ' + a.name + ': ' + a.hours + 'h').join('\n') || '暂无数据'));
  } catch (e) {
    toast(e.message, 'err');
  }
}

async function showReports() {
  try {
    const d = await apiFetch('/api/reports');
    if (!d.reports.length) { addMsg('assistant', '暂无周报，可稍后查看。'); return; }
    const wk = d.reports[0].week;
    const rd = await apiFetch('/api/reports/' + wk);
    addMsg('assistant', '📋 周报 ' + wk + '：\n' + rd.content);
  } catch (e) {
    toast(e.message, 'err');
  }
}
