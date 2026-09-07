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
// 活跃任务 = 未落定状态。awaiting_confirmation 需要用户操作，必须参与
// 活跃过滤与轮询，否则会被其他排队任务挤出可见区，用户永远无法确认发布。
const ACTIVE_STATUSES = ['queued', 'generating', 'reviewing', 'awaiting_confirmation'];
const ACTIVE_SORT = {awaiting_confirmation: 0, generating: 1, reviewing: 2, queued: 3};

let projects = [];
let currentProject = null;
let chapters = [];
let jobs = [];
let editingChapterVersion = null;
let autoTimer = null;
let pollDelay = 5000;
let searchActive = false;
let jobSubmitKey = null;
let projectModalMode = 'create';
let editingProjectId = null;

// ── DOM 快捷方式（$ / clearNode 由 app.js 提供） ──────────
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = String(text);
  return node;
}

function makeBadge(map, status) {
  const info = map[status] || {label: status || '未知', cls: 'dim'};
  return el('span', 'badge ' + info.cls, info.label);
}

function fmtWords(n) {
  return n ? Number(n).toLocaleString() + ' 字' : '—';
}

// 列表接口可能只带 word_count/preview（瘦身模式），旧字段做兼容回退
function chapterWords(c) {
  return c.word_count != null ? c.word_count : (c.content || '').length;
}

function chapterPreview(c) {
  if (c.preview != null) return c.preview;
  return (c.content || '').slice(0, 40).replace(/\s+/g, ' ');
}

function findChapter(no) {
  return chapters.find((c) => String(c.chapter_no) === String(no));
}

// ── 错误态用 app.js 的 renderBoxError ─────────────────────

// ── 项目 ──────────────────────────────────────────────────
async function loadProjects() {
  const data = await apiFetch('/api/novel/projects');
  projects = data.projects || [];
  if (currentProject) {
    currentProject = projects.find((p) => p.project_id === currentProject.project_id) || null;
  }
  renderProjectList();
  renderProjectSummary();
}

function renderProjectSummary() {
  const name = $('current-project-name');
  const slug = $('current-project-slug');
  const rename = $('rename-project-btn');
  const remove = $('delete-project-btn');
  if (!currentProject) {
    name.textContent = '未选择项目';
    slug.textContent = '';
    rename.disabled = true;
    remove.disabled = true;
    return;
  }
  name.textContent = currentProject.name || '(未命名)';
  slug.textContent = currentProject.slug ? '项目标识：' + currentProject.slug : '';
  rename.disabled = false;
  remove.disabled = false;
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

function clearProjectWorkspace() {
  chapters = [];
  jobs = [];
  searchActive = false;
  $('search-input').value = '';
  $('chapter-count').textContent = '';
  $('job-count').textContent = '';
  clearNode($('overview'));
  renderEmptyChapters();
  renderJobs();
  renderProjectSummary();
}

async function selectProject(projectId) {
  currentProject = projects.find((p) => p.project_id === projectId) || null;
  searchActive = false;
  $('search-input').value = '';
  renderProjectList();
  renderProjectSummary();
  stopAutoRefresh();
  if (!currentProject) {
    clearProjectWorkspace();
    return;
  }
  try {
    await refreshProjectData();
    startAutoRefresh();
  } catch (e) {
    toast(e.message, 'err');
    if (e.kind === 'unauthorized') openTokenModal();
  }
}

// ── 章节列表 ──────────────────────────────────────────────
async function loadChapters() {
  const box = $('chapters');
  if (!currentProject) { clearNode(box); renderEmptyChapters(); return; }
  box.textContent = '加载中…';
  try {
    const data = await apiFetch('/api/novel/projects/' + currentProject.project_id + '/chapters');
    chapters = data.chapters || [];
    $('chapter-count').textContent = chapters.length ? '共 ' + chapters.length + ' 章' : '';
    renderChapters();
  } catch (e) {
    renderBoxError(box, e, loadChapters);
    throw e;
  }
}

function renderEmptyChapters() {
  const box = $('chapters');
  clearNode(box);
  const empty = el('div', 'empty');
  empty.appendChild(el('div', 'icon', '🖋'));
  empty.appendChild(el('div', '', currentProject ? '这本书还没有章节，点「+ 新建章节」开始写作' : '选择或创建一个项目开始'));
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
    row.tabIndex = 0;
    row.setAttribute('role', 'button');
    row.appendChild(el('td', 'ch-no', '第' + c.chapter_no + '章'));
    const titleCell = el('td', 'ch-title', c.title || '（未命名）');
    const preview = chapterPreview(c);
    if (preview) {
      titleCell.appendChild(el('span', 'ch-title-preview', '　' + preview));
    }
    row.appendChild(titleCell);
    const statusCell = el('td');
    statusCell.appendChild(makeBadge(CHAPTER_STATUS, c.status));
    row.appendChild(statusCell);
    row.appendChild(el('td', 'ch-words', fmtWords(chapterWords(c))));
    row.addEventListener('click', () => openChapterByNo(c.chapter_no));
    row.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        openChapterByNo(c.chapter_no);
      }
    });
    tbody.appendChild(row);
  });
  table.appendChild(tbody);
  box.appendChild(table);
}

// ── 章节抽屉（按需拉取全文） ───────────────────────────────
async function openChapterByNo(chapterNo) {
  if (!currentProject) return;
  try {
    const chapter = await apiFetch(
      '/api/novel/projects/' + currentProject.project_id + '/chapters/' + encodeURIComponent(chapterNo)
    );
    openChapterDrawer(chapter);
  } catch (e) {
    toast(e.message, 'err');
  }
}

function openChapterDrawer(chapter) {
  const meta = $('drawer-meta');
  clearNode(meta);
  meta.appendChild(makeBadge(CHAPTER_STATUS, chapter.status));
  meta.appendChild(el('span', '', '　' + fmtWords(chapterWords(chapter))));
  $('drawer-body').textContent = chapter.content || '（暂无正文）';
  const editBtn = $('drawer-edit');
  editBtn.onclick = () => openChapterModal(chapter.chapter_no, chapter.title, chapter.content, chapter.version);
  openDrawer('第' + chapter.chapter_no + '章 ' + (chapter.title || ''));
}

// ── 生成任务 ──────────────────────────────────────────────
async function loadJobs() {
  if (!currentProject) { jobs = []; renderJobs(); return; }
  try {
    const data = await apiFetch('/api/novel/projects/' + currentProject.project_id + '/jobs');
    jobs = data.jobs || [];
    $('job-count').textContent = jobs.length ? jobs.length + ' 个' : '';
    renderJobs();
  } catch (e) {
    renderBoxError($('jobs'), e, loadJobs);
    throw e;
  }
}

function renderJobs() {
  const box = $('jobs');
  clearNode(box);
  // 待确认任务需要用户操作，置顶展示，避免被排队任务挤掉
  const act = jobs
    .filter((j) => ACTIVE_STATUSES.includes(j.status))
    .sort((a, b) => (ACTIVE_SORT[a.status] || 9) - (ACTIVE_SORT[b.status] || 9));
  const visible = act.length ? act : jobs.slice(0, 8);
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
    publish.addEventListener('click', () => jobAction(publish, j, 'confirm', '已发布'));
    actions.appendChild(publish);
  }
  if (ACTIVE_STATUSES.includes(j.status)) {
    const cancel = el('button', 'small', '取消');
    cancel.addEventListener('click', () => {
      if (!window.confirm('确定取消该生成任务？此操作不可撤销。')) return;
      jobAction(cancel, j, 'cancel', '已取消');
    });
    actions.appendChild(cancel);
  }
  if (j.status === 'failed') {
    const retry = el('button', 'small', '重试');
    retry.addEventListener('click', () => jobAction(retry, j, 'retry', '已重新排队'));
    actions.appendChild(retry);
  }
  if (j.status === 'published') {
    const sync = el('button', 'small', '同步文件');
    sync.addEventListener('click', () => jobAction(sync, j, 'file-sync', '文件已同步'));
    actions.appendChild(sync);
  }
  card.appendChild(actions);
  return card;
}

async function jobAction(btn, job, action, okMsg) {
  if (btn) btn.disabled = true; // 防连点：请求期间禁用，列表刷新后按钮重建
  try {
    await apiFetch('/api/novel/projects/' + currentProject.project_id + '/jobs/' + job.job_id + '/' + action, {method: 'POST'});
    toast(okMsg, 'ok');
    await refreshProjectData();
  } catch (e) {
    toast(e.message, 'err');
    if (btn) btn.disabled = false;
  }
}

// 自动刷新：setTimeout 链 + 失败指数退避（5s→10s→20s 封顶），
// 后台标签页跳过请求，回到前台立即补一次刷新。
function startAutoRefresh() {
  stopAutoRefresh();
  pollDelay = 5000;
  const loop = async () => {
    await pollTick();
    autoTimer = setTimeout(loop, pollDelay);
  };
  autoTimer = setTimeout(loop, pollDelay);
}

async function pollTick() {
  if (!$('auto-refresh').checked) return;
  if (document.hidden) return;
  if (!jobs.some((j) => ACTIVE_STATUSES.includes(j.status))) return;
  try {
    await loadJobs();
    pollDelay = 5000;
  } catch (e) {
    pollDelay = Math.min(pollDelay * 2, 20000);
  }
}

function stopAutoRefresh() {
  if (autoTimer) { clearTimeout(autoTimer); autoTimer = null; }
}

document.addEventListener('visibilitychange', () => {
  if (!document.hidden && currentProject && jobs.some((j) => ACTIVE_STATUSES.includes(j.status))) {
    refreshProjectData().catch(() => {});
  }
});

// ── 概览统计 ──────────────────────────────────────────────
async function loadOverview() {
  const box = $('overview');
  clearNode(box);
  if (!currentProject) return;
  const published = chapters.filter((c) => c.status === 'published').length;
  const activeCount = jobs.filter((j) => ACTIVE_STATUSES.includes(j.status)).length;
  const totalWords = chapters.reduce((sum, c) => sum + chapterWords(c), 0);
  let files = '—';
  try {
    const idx = await apiFetch('/api/novel/projects/' + currentProject.project_id + '/index/status');
    files = idx.files && idx.files.files != null ? idx.files.files + ' 个' : '—';
  } catch (e) { /* 概览中的索引状态失败不阻塞 */ }
  [['章节数', chapters.length], ['已发布', published], ['进行中任务', activeCount], ['总字数', Number(totalWords).toLocaleString()], ['文件索引', files]]
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
    searchActive = true;
    const box = $('chapters');
    clearNode(box);
    const results = data.results || [];
    $('chapter-count').textContent = '搜索到 ' + results.length + ' 条';
    if (!results.length) {
      renderEmptyChapters();
      return;
    }
    const back = el('button', 'small search-back', '‹ 返回全部章节');
    back.addEventListener('click', clearSearchView);
    box.appendChild(back);
    results.forEach((x) => {
      const row = el('button', 'project-item');
      row.appendChild(el('span', 'p-name', '第' + x.chapter_no + '章 ' + (x.title || '')));
      const snippet = el('span', 'p-meta');
      snippet.textContent = x.snippet || '';
      row.appendChild(snippet);
      row.addEventListener('click', () => openChapterByNo(x.chapter_no));
      box.appendChild(row);
    });
  } catch (e) {
    toast(e.message, 'err');
  }
}

function clearSearchView() {
  searchActive = false;
  $('search-input').value = '';
  $('chapter-count').textContent = chapters.length ? '共 ' + chapters.length + ' 章' : '';
  renderChapters();
}

async function rebuildNovelIndex() {
  if (!currentProject) return;
  if (!window.confirm('重建全文索引可能需要一些时间，确定继续？')) return;
  const btn = $('rebuild-btn');
  btn.disabled = true;
  try {
    await apiFetch('/api/novel/projects/' + currentProject.project_id + '/index/rebuild', {method: 'POST'});
    toast('索引已重建', 'ok');
    await loadOverview();
  } catch (e) {
    toast(e.message, 'err');
  } finally {
    btn.disabled = false;
  }
}

// ── 弹层（Esc 关闭 + 焦点归还由 openOverlay/closeOverlay 支持） ──
const modalEsc = {};
function showModal(id) {
  const node = $(id);
  if (!node.classList.contains('hidden')) return;
  openOverlay(node, node.querySelector('input, textarea, button'));
  modalEsc[id] = registerEscClose(() => hideModal(id));
}
function hideModal(id) {
  const node = $(id);
  if (node.classList.contains('hidden')) return;
  closeOverlay(node);
  if (modalEsc[id]) { modalEsc[id](); delete modalEsc[id]; }
}

function clearProjectValidation() {
  ['project-name', 'project-slug'].forEach((id) => $(id).removeAttribute('aria-invalid'));
}

function focusProjectField(id) {
  const field = $(id);
  field.setAttribute('aria-invalid', 'true');
  field.focus();
  if (typeof field.select === 'function') field.select();
}

function openProjectCreateModal() {
  projectModalMode = 'create';
  editingProjectId = null;
  $('project-modal-title').textContent = '新建小说项目';
  $('project-modal-hint').classList.add('hidden');
  $('project-name').value = '';
  $('project-slug').value = '';
  $('project-slug').disabled = false;
  $('project-save').textContent = '创建';
  clearProjectValidation();
  showModal('project-modal');
}

function openProjectRenameModal() {
  if (!currentProject) return;
  projectModalMode = 'rename';
  editingProjectId = currentProject.project_id;
  $('project-modal-title').textContent = '改名小说项目';
  $('project-modal-hint').classList.remove('hidden');
  $('project-name').value = currentProject.name || '';
  $('project-slug').value = currentProject.slug || '';
  $('project-slug').disabled = true;
  $('project-save').textContent = '保存';
  clearProjectValidation();
  showModal('project-modal');
}

function resetProjectModal() {
  projectModalMode = 'create';
  editingProjectId = null;
  $('project-modal-title').textContent = '新建小说项目';
  $('project-modal-hint').classList.add('hidden');
  $('project-slug').disabled = false;
  $('project-save').textContent = '创建';
  clearProjectValidation();
}

async function saveProject() {
  const name = $('project-name').value.trim();
  if (!name) {
    focusProjectField('project-name');
    toast('请填写书名');
    return;
  }
  const btn = $('project-save');
  btn.disabled = true;
  clearProjectValidation();
  const isRename = projectModalMode === 'rename' && editingProjectId && currentProject;
  try {
    let p;
    if (isRename) {
      p = await apiFetch('/api/novel/projects/' + encodeURIComponent(editingProjectId), {
        method: 'PATCH',
        body: JSON.stringify({name: name, expected_version: currentProject.version}),
      });
    } else {
      const payload = {name: name};
      const slug = $('project-slug').value.trim();
      if (slug) payload.slug = slug;
      p = await apiFetch('/api/novel/projects', {method: 'POST', body: JSON.stringify(payload)});
    }
    hideModal('project-modal');
    $('project-name').value = '';
    $('project-slug').value = '';
    resetProjectModal();
    // 改名/创建成功后立即刷新列表并选中目标项目。
    await loadProjects();
    await selectProject(p.project_id);
    toast(isRename ? '项目已改名' : '项目已创建', 'ok');
  } catch (e) {
    if (e.code === 'project_slug_conflict') {
      focusProjectField('project-slug');
      toast('项目标识已存在，请换一个', 'err');
    } else if (e.code === 'project_version_conflict') {
      toast('项目已被其他操作更新，请关闭弹窗后重新打开', 'err');
    } else if (e.code === 'project_root_invalid') {
      toast(e.message || '项目目录不可用', 'err');
    } else {
      toast(e.message, 'err');
    }
  } finally {
    btn.disabled = false;
  }
}

async function requestProjectDelete(project) {
  const baseUrl = '/api/novel/projects/' + encodeURIComponent(project.project_id);
  const params = '?expected_version=' + encodeURIComponent(project.version);
  try {
    // 某些反向代理默认拒绝 DELETE；POST 兼容入口优先保证工作台可用。
    return await apiFetch(baseUrl + '/delete' + params, {method: 'POST'});
  } catch (e) {
    if (e.status !== 405) throw e;
    return apiFetch(baseUrl + params, {method: 'DELETE'});
  }
}

async function deleteCurrentProject() {
  if (!currentProject) return;
  const project = currentProject;
  const confirmed = window.confirm(
    '确定删除「' + (project.name || '未命名项目') + '」？\n\n'
    + '该项目的章节、生成任务、成员和服务器项目目录都会永久删除，此操作不可撤销。'
  );
  if (!confirmed) return;
  const btn = $('delete-project-btn');
  btn.disabled = true;
  try {
    const result = await requestProjectDelete(project);
    stopAutoRefresh();
    currentProject = null;
    clearProjectWorkspace();
    await loadProjects();
    if (projects.length) {
      await selectProject(projects[0].project_id);
    } else {
      clearProjectWorkspace();
    }
    if (result && result.files_deleted === false) {
      toast('项目已删除，但项目文件清理失败，请联系管理员', 'err');
    } else {
      toast('项目已删除', 'ok');
    }
  } catch (e) {
    if (e.code === 'project_protected') {
      toast('该项目受保护，不能删除', 'err');
    } else if (e.code === 'project_version_conflict') {
      toast('项目已被其他操作更新，请刷新后重试', 'err');
      await loadProjects().catch(() => {});
    } else if (e.code === 'project_delete_blocked') {
      toast(e.message || '项目文件正在被占用，暂时无法删除', 'err');
    } else if (e.code === 'project_root_invalid') {
      toast(e.message || '项目目录不安全，无法删除', 'err');
    } else {
      toast(e.message, 'err');
    }
  } finally {
    btn.disabled = false;
    renderProjectSummary();
  }
}

function openChapterModal(no, title, content, version) {
  $('chapter-modal-title').textContent = no ? '编辑章节' : '新建章节';
  $('chapter-no').value = no || '';
  $('chapter-title').value = title || '';
  $('chapter-content').value = content || '';
  editingChapterVersion = Number.isInteger(version) ? version : null;
  $('chapter-no').disabled = Boolean(no);
  showModal('chapter-modal');
}

async function saveChapter(force) {
  if (!currentProject) { toast('先选择项目'); return; }
  const no = $('chapter-no').value.trim();
  if (!no) { toast('请填写章节号'); return; }
  const payload = {
    chapter_no: no,
    title: $('chapter-title').value.trim(),
    content: $('chapter-content').value,
  };
  // 强制覆盖时不带 expected_version（服务端语义 = 无条件覆盖）
  if (!force && editingChapterVersion != null) payload.expected_version = editingChapterVersion;
  const btn = $('chapter-save');
  btn.disabled = true;
  try {
    await apiFetch('/api/novel/projects/' + currentProject.project_id + '/chapters', {
      method: 'PUT',
      body: JSON.stringify(payload),
    });
    hideModal('chapter-modal');
    editingChapterVersion = null;
    toast('章节已保存', 'ok');
    await Promise.all([loadChapters(), loadOverview()]);
  } catch (e) {
    if (e.status === 409 && !force) {
      const overwrite = window.confirm(
        '章节已被其他客户端修改。\n\n「确定」= 用你正在编辑的草稿强制覆盖服务端版本；\n「取消」= 放弃修改，加载最新版本。'
      );
      if (overwrite) {
        await saveChapter(true);
        return;
      }
      editingChapterVersion = null;
      hideModal('chapter-modal');
      await Promise.all([loadChapters(), loadOverview()]);
      toast('已加载最新版本，你的修改未保存', 'err');
      return;
    }
    toast(e.message, 'err');
  } finally {
    btn.disabled = false;
  }
}

async function createJob() {
  if (!currentProject) { toast('先选择项目'); return; }
  const chapterNo = $('job-chapter').value.trim();
  if (!chapterNo) { toast('请填写章节号'); return; }
  const btn = $('job-save');
  btn.disabled = true;
  try {
    await apiFetch('/api/novel/projects/' + currentProject.project_id + '/jobs', {
      method: 'POST',
      body: JSON.stringify({
        chapter_no: chapterNo,
        prompt: $('job-prompt').value,
        idempotency_key: jobSubmitKey || ('web-' + currentProject.project_id + '-' + Date.now().toString(36)),
      }),
    });
    hideModal('job-modal');
    $('job-chapter').value = '';
    $('job-prompt').value = '';
    toast('任务已创建', 'ok');
    await loadJobs();
  } catch (e) {
    toast(e.message, 'err');
  } finally {
    btn.disabled = false;
  }
}

function openJobModal() {
  // 幂等键每次打开弹层时重新生成：同一次弹层内的重试复用同键，
  // 服务端去重生效；关闭重开则换新键，允许再次创建相同内容任务。
  jobSubmitKey = 'web-' + currentProject.project_id + '-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 8);
  showModal('job-modal');
}

// ── Token（openTokenModal 由 app.js 提供，保存后重跑 boot）──

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
      clearProjectWorkspace();
    }
  } catch (e) {
    renderBoxError(box, e, boot);
    if (e.kind === 'unauthorized') openTokenModal();
  }
}

// 事件绑定
$('search-btn').addEventListener('click', searchNovel);
$('search-input').addEventListener('keydown', (e) => { if (e.key === 'Enter') searchNovel(); });
$('rebuild-btn').addEventListener('click', rebuildNovelIndex);
$('chapter-filter').addEventListener('change', () => {
  // 搜索结果没有状态语义，切筛选即视为退出搜索
  if (searchActive) clearSearchView();
  else renderChapters();
});
$('new-project-btn').addEventListener('click', openProjectCreateModal);
$('project-save').addEventListener('click', saveProject);
$('project-cancel').addEventListener('click', () => { hideModal('project-modal'); resetProjectModal(); });
$('rename-project-btn').addEventListener('click', openProjectRenameModal);
$('delete-project-btn').addEventListener('click', deleteCurrentProject);
$('new-chapter-btn').addEventListener('click', () => openChapterModal('', '', ''));
$('chapter-save').addEventListener('click', () => saveChapter(false));
$('chapter-cancel').addEventListener('click', () => hideModal('chapter-modal'));
$('new-job-btn').addEventListener('click', () => {
  if (!currentProject) { toast('先选择项目'); return; }
  openJobModal();
});
$('job-save').addEventListener('click', createJob);
$('job-cancel').addEventListener('click', () => hideModal('job-modal'));
// token/theme/drawer 的事件绑定由 app.js bindShared 提供
setTokenSavedHook(() => boot());
boot();
