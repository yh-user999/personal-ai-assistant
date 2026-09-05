/* 小说工作台逻辑：全部 DOM 写入走 createElement/textContent 等安全 API */
'use strict';

// ── 状态映射 ──────────────────────────────────────────────
const JOB_STATUS = {
  queued: {label: '排队中', cls: 'dim'},
  generating: {label: '生成中', cls: 'info'},
  reviewing: {label: '审阅中', cls: 'info'},
  awaiting_confirmation: {label: '待确认', cls: 'warn'},
  published: {label: '已发布', cls: 'ok'},
  failed: {label: '失败', cls: 'err'},
  cancelled: {label: '已取消', cls: 'dim'},
};
const CHAPTER_STATUS = {
  draft: {label: '草稿', cls: 'dim'},
  published: {label: '已发布', cls: 'ok'},
  archived: {label: '存档', cls: 'info'},
};

let projects = [];
let currentProject = null;
let chapters = [];
let jobs = [];
let autoTimer = null;

// ── DOM 快捷方式 ──────────────────────────────────────────
function $(id) { return document.getElementById(id); }

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = String(text);
  return node;
}

function clearNode(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

function makeBadge(map, status) {
  const info = map[status] || {label: status || '未知', cls: 'dim'};
  return el('span', 'badge ' + info.cls, info.label);
}

function fmtWords(text) {
  const n = (text || '').length;
  return n ? n.toLocaleString() + ' 字' : '—';
}

// ── 项目 ──────────────────────────────────────────────────
async function loadProjects() {
  const data = await apiFetch('/api/novel/projects');
  projects = data.projects || [];
  renderProjectList();
}

function renderProjectList() {
  const box = $('project-list');
  clearNode(box);
  if (!projects.length) {
    box.appendChild(el('div', 'empty', '还没有项目，点右上角「+ 新建」开始'));
    return;
  }
  projects.forEach((p) => {
    const item = el('button', 'project-item' + (currentProject && p.project_id === currentProject.project_id ? ' active' : ''));
    item.appendChild(el('span', 'p-name', p.name || '(未命名)'));
    item.appendChild(el('span', 'p-meta', '更新于 ' + fmtDate(p.updated_at)));
    item.addEventListener('click', () => selectProject(p.project_id));
    box.appendChild(item);
  });
}

async function selectProject(projectId) {
  currentProject = projects.find((p) => p.project_id === projectId) || null;
  renderProjectList();
  stopAutoRefresh();
  await Promise.all([loadChapters(), loadJobs(), loadOverview()]);
  startAutoRefresh();
}

// ── 章节列表 ──────────────────────────────────────────────
async function loadChapters() {
  const box = $('chapters');
  box.textContent = '加载中…';
  if (!currentProject) { clearNode(box); renderEmptyChapters(); return; }
  const data = await apiFetch('/api/novel/projects/' + currentProject.project_id + '/chapters');
  chapters = data.chapters || [];
  $('chapter-count').textContent = chapters.length ? '共 ' + chapters.length + ' 章' : '';
  renderChapters();
}

function renderEmptyChapters() {
  const box = $('chapters');
  clearNode(box);
  const empty = el('div', 'empty');
  empty.appendChild(el('div', 'icon', '🖋'));
  empty.appendChild(el('div', '', currentProject ? '本章还没有章节，点「+ 新建章节」开始写作' : '选择或创建一个项目开始'));
  box.appendChild(empty);
}

function renderChapters() {
  const box = $('chapters');
  clearNode(box);
  const filter = $('chapter-filter').value;
  const visible = chapters.filter((c) => !filter || c.status === filter);
  if (!visible.length) { renderEmptyChapters(); return; }

  const table = el('table', 'table');
  const thead = el('thead');
  const headRow = el('tr');
  ['章', '标题', '状态', '字数'].forEach((h) => headRow.appendChild(el('th', '', h)));
  thead.appendChild(headRow);
  table.appendChild(thead);
  const tbody = el('tbody');
  visible.forEach((c) => {
    const row = el('tr');
    row.appendChild(el('td', 'ch-no', '第' + c.chapter_no + '章'));
    const titleCell = el('td', 'ch-title', c.title || '（未命名）');
    if (c.content) {
      const preview = el('span', 'ch-title-preview', '　' + c.content.slice(0, 40).replace(/\s+/g, ' '));
      titleCell.appendChild(preview);
    }
    row.appendChild(titleCell);
    const statusCell = el('td');
    statusCell.appendChild(makeBadge(CHAPTER_STATUS, c.status));
    row.appendChild(statusCell);
    row.appendChild(el('td', 'ch-words', fmtWords(c.content)));
    row.addEventListener('click', () => openChapterDrawer(c));
    tbody.appendChild(row);
  });
  table.appendChild(tbody);
  box.appendChild(table);
}

// ── 章节抽屉 ──────────────────────────────────────────────
function openChapterDrawer(chapter) {
  $('drawer-title').textContent = '第' + chapter.chapter_no + '章 ' + (chapter.title || '');
  const meta = $('drawer-meta');
  clearNode(meta);
  meta.appendChild(makeBadge(CHAPTER_STATUS, chapter.status));
  meta.appendChild(el('span', '', '　' + fmtWords(chapter.content)));
  $('drawer-body').textContent = chapter.content || '（暂无正文）';
  const editBtn = $('drawer-edit');
  editBtn.onclick = () => openChapterModal(chapter.chapter_no, chapter.title, chapter.content);
  $('drawer').classList.remove('hidden');
  $('drawer-mask').classList.remove('hidden');
}

function closeDrawer() {
  $('drawer').classList.add('hidden');
  $('drawer-mask').classList.add('hidden');
}

// ── 生成任务 ──────────────────────────────────────────────
async function loadJobs() {
  if (!currentProject) { renderJobs(); return; }
  const data = await apiFetch('/api/novel/projects/' + currentProject.project_id + '/jobs');
  jobs = data.jobs || [];
  $('job-count').textContent = jobs.length ? jobs.length + ' 个' : '';
  renderJobs();
}

function renderJobs() {
  const box = $('jobs');
  clearNode(box);
  const active = jobs.filter((j) => ['queued', 'generating', 'reviewing'].includes(j.status));
  const visible = active.length ? active : jobs.slice(0, 8);
  if (!visible.length) {
    const empty = el('div', 'empty');
    empty.appendChild(el('div', 'icon', '✨'));
    empty.appendChild(el('div', '', '暂无生成任务，点「+ 创建任务」让 AI 帮你写'));
    box.appendChild(empty);
    return;
  }
  visible.forEach((j) => box.appendChild(renderJobCard(j)));
}

function renderJobCard(j) {
  const card = el('div', 'job card');
  card.appendChild(el('span', 'j-chapter', '第' + j.chapter_no + '章'));
  card.appendChild(makeBadge(JOB_STATUS, j.status));

  if (j.status === 'generating' && typeof j.progress === 'number') {
    const wrap = el('div', 'j-progress');
    const track = el('div', 'progress-track');
    const fill = el('div', 'progress-fill');
    fill.style.width = Math.max(0, Math.min(100, j.progress)) + '%';
    track.appendChild(fill);
    wrap.appendChild(track);
    card.appendChild(wrap);
    card.appendChild(el('span', 'muted', j.progress + '%'));
  }

  if (j.prompt) card.appendChild(el('span', 'j-prompt', j.prompt));
  if (j.attempts > 0) card.appendChild(el('span', 'muted', '尝试 ' + j.attempts));
  if (j.error) card.appendChild(el('div', 'j-err', j.error));

  const actions = el('div', 'j-actions');
  if (j.status === 'awaiting_confirmation') {
    const view = el('button', 'small', '查看草稿');
    view.addEventListener('click', () => openChapterDrawer({chapter_no: j.chapter_no, title: '', content: j.draft_content, status: 'draft'}));
    actions.appendChild(view);
    const publish = el('button', 'primary small', '确认发布');
    publish.addEventListener('click', () => jobAction(j, 'confirm', '已发布'));
    actions.appendChild(publish);
  }
  if (['queued', 'generating', 'reviewing', 'awaiting_confirmation'].includes(j.status)) {
    const cancel = el('button', 'small', '取消');
    cancel.addEventListener('click', () => jobAction(j, 'cancel', '已取消'));
    actions.appendChild(cancel);
  }
  if (j.status === 'failed') {
    const retry = el('button', 'small', '重试');
    retry.addEventListener('click', () => jobAction(j, 'retry', '已重新排队'));
    actions.appendChild(retry);
  }
  if (j.status === 'published') {
    const sync = el('button', 'small', '同步文件');
    sync.addEventListener('click', () => jobAction(j, 'file-sync', '文件已同步'));
    actions.appendChild(sync);
  }
  card.appendChild(actions);
  return card;
}

async function jobAction(job, action, okMsg) {
  try {
    await apiFetch('/api/novel/projects/' + currentProject.project_id + '/jobs/' + job.job_id + '/' + action, {method: 'POST'});
    toast(okMsg, 'ok');
    await refreshProjectData();
  } catch (e) {
    toast(e.message, 'err');
  }
}

// 自动刷新：仅存在活跃任务时轮询
function startAutoRefresh() {
  stopAutoRefresh();
  autoTimer = setInterval(async () => {
    if (!$('auto-refresh').checked) return;
    if (!jobs.some((j) => ['queued', 'generating', 'reviewing'].includes(j.status))) return;
    try { await loadJobs(); } catch (e) { /* 静默，下次重试 */ }
  }, 5000);
}

function stopAutoRefresh() {
  if (autoTimer) { clearInterval(autoTimer); autoTimer = null; }
}

// ── 概览统计 ──────────────────────────────────────────────
async function loadOverview() {
  const box = $('overview');
  clearNode(box);
  if (!currentProject) return;
  const published = chapters.filter((c) => c.status === 'published').length;
  const activeJobs = jobs.filter((j) => ['queued', 'generating', 'reviewing'].includes(j.status)).length;
  const totalWords = chapters.reduce((sum, c) => sum + (c.content || '').length, 0);
  let files = '—';
  try {
    const idx = await apiFetch('/api/novel/projects/' + currentProject.project_id + '/index/status');
    files = idx.files && idx.files.files != null ? idx.files.files + ' 个' : '—';
  } catch (e) { /* 概览中的索引状态失败不阻塞 */ }
  [['章节数', chapters.length], ['已发布', published], ['进行中任务', activeJobs], ['总字数', totalWords.toLocaleString()], ['文件索引', files]]
    .forEach(([label, num]) => {
      const card = el('div', 'stat card');
      card.appendChild(el('div', 's-num', num));
      card.appendChild(el('div', 's-label', label));
      box.appendChild(card);
    });
}

// ── 搜索 ──────────────────────────────────────────────────
async function searchNovel() {
  const q = $('search-input').value.trim();
  if (!currentProject || !q) { toast('先选择项目，再输入搜索词'); return; }
  try {
    const data = await apiFetch('/api/novel/projects/' + currentProject.project_id + '/chapters/search?q=' + encodeURIComponent(q));
    const box = $('chapters');
    clearNode(box);
    const results = data.results || [];
    $('chapter-count').textContent = '搜索到 ' + results.length + ' 条';
    if (!results.length) {
      renderEmptyChapters();
      return;
    }
    results.forEach((x) => {
      const row = el('button', 'project-item');
      row.appendChild(el('span', 'p-name', '第' + x.chapter_no + '章 ' + (x.title || '')));
      const snippet = el('span', 'p-meta');
      snippet.textContent = x.snippet || '';
      row.appendChild(snippet);
      row.addEventListener('click', () => {
        const chapter = chapters.find((c) => c.chapter_no === x.chapter_no);
        if (chapter) openChapterDrawer(chapter);
      });
      box.appendChild(row);
    });
  } catch (e) {
    toast(e.message, 'err');
  }
}

async function rebuildNovelIndex() {
  if (!currentProject) return;
  try {
    await apiFetch('/api/novel/projects/' + currentProject.project_id + '/index/rebuild', {method: 'POST'});
    toast('索引已重建', 'ok');
    await loadOverview();
  } catch (e) {
    toast(e.message, 'err');
  }
}

// ── 弹层 ──────────────────────────────────────────────────
function showModal(id) { $(id).classList.remove('hidden'); }
function hideModal(id) { $(id).classList.add('hidden'); }

async function createProject() {
  const name = $('project-name').value.trim();
  if (!name) { toast('请填写书名'); return; }
  try {
    const payload = {name: name};
    const slug = $('project-slug').value.trim();
    if (slug) payload.slug = slug;
    const p = await apiFetch('/api/novel/projects', {method: 'POST', body: JSON.stringify(payload)});
    hideModal('project-modal');
    $('project-name').value = '';
    $('project-slug').value = '';
    toast('项目已创建', 'ok');
    await loadProjects();
    await selectProject(p.project_id);
  } catch (e) {
    toast(e.message, 'err');
  }
}

function openChapterModal(no, title, content) {
  $('chapter-modal-title').textContent = no ? '编辑章节' : '新建章节';
  $('chapter-no').value = no || '';
  $('chapter-title').value = title || '';
  $('chapter-content').value = content || '';
  $('chapter-no').disabled = Boolean(no);
  showModal('chapter-modal');
}

async function saveChapter() {
  if (!currentProject) { toast('先选择项目'); return; }
  const no = $('chapter-no').value.trim();
  if (!no) { toast('请填写章节号'); return; }
  try {
    await apiFetch('/api/novel/projects/' + currentProject.project_id + '/chapters', {
      method: 'PUT',
      body: JSON.stringify({
        chapter_no: no,
        title: $('chapter-title').value.trim(),
        content: $('chapter-content').value,
      }),
    });
    hideModal('chapter-modal');
    toast('章节已保存', 'ok');
    await Promise.all([loadChapters(), loadOverview()]);
  } catch (e) {
    toast(e.message, 'err');
  }
}

async function createJob() {
  if (!currentProject) { toast('先选择项目'); return; }
  const chapterNo = $('job-chapter').value.trim();
  if (!chapterNo) { toast('请填写章节号'); return; }
  try {
    await apiFetch('/api/novel/projects/' + currentProject.project_id + '/jobs', {
      method: 'POST',
      body: JSON.stringify({
        chapter_no: chapterNo,
        prompt: $('job-prompt').value,
        idempotency_key: 'web-' + currentProject.project_id + '-' + chapterNo + '-' + Date.now(),
      }),
    });
    hideModal('job-modal');
    $('job-chapter').value = '';
    $('job-prompt').value = '';
    toast('任务已创建', 'ok');
    await loadJobs();
  } catch (e) {
    toast(e.message, 'err');
  }
}

// ── Token ─────────────────────────────────────────────────
function openTokenModal() {
  $('token-input').value = getToken();
  showModal('token-modal');
}

async function saveTokenFromModal() {
  saveToken($('token-input').value);
  hideModal('token-modal');
  toast('Token 已保存', 'ok');
  await boot();
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

async function refreshProjectData() {
  await Promise.all([loadChapters(), loadJobs(), loadOverview()]);
}

// ── 启动 ──────────────────────────────────────────────────
async function boot() {
  const box = $('project-list');
  clearNode(box);
  box.appendChild(el('div', 'muted', '加载中…'));
  try {
    await loadProjects();
    if (projects.length) {
      await selectProject(projects[0].project_id);
    } else {
      renderEmptyChapters();
      renderJobs();
    }
  } catch (e) {
    clearNode(box);
    const item = el('div', 'empty', e.message);
    box.appendChild(item);
    if (e.kind === 'unauthorized') openTokenModal();
  }
}

// 事件绑定
$('search-btn').addEventListener('click', searchNovel);
$('search-input').addEventListener('keydown', (e) => { if (e.key === 'Enter') searchNovel(); });
$('rebuild-btn').addEventListener('click', rebuildNovelIndex);
$('chapter-filter').addEventListener('change', renderChapters);
$('new-project-btn').addEventListener('click', () => showModal('project-modal'));
$('project-save').addEventListener('click', createProject);
$('project-cancel').addEventListener('click', () => hideModal('project-modal'));
$('new-chapter-btn').addEventListener('click', () => openChapterModal('', '', ''));
$('chapter-save').addEventListener('click', saveChapter);
$('chapter-cancel').addEventListener('click', () => hideModal('chapter-modal'));
$('new-job-btn').addEventListener('click', () => showModal('job-modal'));
$('job-save').addEventListener('click', createJob);
$('job-cancel').addEventListener('click', () => hideModal('job-modal'));
$('drawer-close').addEventListener('click', closeDrawer);
$('drawer-mask').addEventListener('click', closeDrawer);
$('token-btn').addEventListener('click', openTokenModal);
$('token-save').addEventListener('click', saveTokenFromModal);
$('token-cancel').addEventListener('click', () => hideModal('token-modal'));
$('theme-btn').addEventListener('click', toggleTheme);

boot();
