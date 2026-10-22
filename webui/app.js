/* ============================================================
   JMComic 桌面端前端逻辑
   ============================================================ */
'use strict';

const $ = (s) => document.querySelector(s);
const api = () => window.pywebview.api;

const S = {
  view: 'local',        // local | history | results
  kind: 'all',          // all | work | author
  items: [],
  selected: null,
  theme: 'dark',
  busy: false,
  lastDone: 0,
  logOpen: false,
};

const THEMES = [
  ['dark', '深色'], ['light', '浅色'], ['neon', '霓虹'],
  ['sakura', '樱花'], ['follow', '跟随系统'],
];

/* ---------------- 小工具 ---------------- */
function toast(msg, kind = 'ok') {
  const el = document.createElement('div');
  el.className = 'toast ' + kind;
  el.textContent = msg;
  $('#toasts').appendChild(el);
  setTimeout(() => { el.classList.add('out'); setTimeout(() => el.remove(), 300); }, 2400);
}

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function applyTheme(name) {
  S.theme = name;
  document.body.dataset.theme = name;
}

/* ---------------- 视图渲染 ---------------- */
function setView(v) {
  S.view = v;
  document.querySelectorAll('.nav-item').forEach((b) =>
    b.classList.toggle('is-active', b.dataset.view === v));
  loadView();
}

async function loadView() {
  if (S.view === 'local') {
    renderSkeleton(8);
    const items = (await api().list_local()).items || [];
    S.items = items || [];
    renderGrid();
  } else if (S.view === 'history') {
    S.items = (await api().list_history()).items || [];
    renderGrid();
  } else {
    renderGrid();
  }
}

function renderSkeleton(n) {
  const g = $('#grid');
  g.innerHTML = '';
  for (let i = 0; i < n; i++) {
    const d = document.createElement('div');
    d.className = 'skeleton';
    d.style.aspectRatio = '3 / 4';
    g.appendChild(d);
  }
  $('#empty').style.display = 'none';
}

function renderGrid() {
  const g = $('#grid');
  g.innerHTML = '';
  const items = S.items || [];
  $('#empty').style.display = items.length ? 'none' : 'grid';
  items.forEach((it, i) => {
    const card = document.createElement('button');
    card.className = 'card';
    card.style.animationDelay = Math.min(i, 20) * 18 + 'ms';
    const badge = it.badge ? `<span class="badge">${esc(it.badge)}</span>` : '';
    const cover = it.cover
      ? `<img src="${esc(it.cover)}" loading="lazy" alt="" onerror="this.style.display='none'">`
      : '';
    card.innerHTML = `
      <div class="cover">${cover}${badge}</div>
      <div class="title">${esc(it.title || '未命名')}</div>
      <div class="meta">${esc(it.meta || '')}</div>`;
    card.onclick = () => openSheet(it);
    g.appendChild(card);
  });
}

/* ---------------- 详情抽屉 ---------------- */
function openSheet(it) {
  S.selected = it;
  $('#sheetTitle').textContent = it.title || '—';
  const chips = [];
  if (it.id) chips.push(['JM' + it.id, true]);
  if (it.authors) chips.push([it.authors, false]);
  if (it.pages && /\d/.test(String(it.pages))) chips.push([it.pages + ' 页', false]);
  if (it.chapters && /\d/.test(String(it.chapters))) chips.push([it.chapters + ' 话', false]);
  $('#sheetChips').innerHTML = chips
    .map(([t, a]) => `<span class="chip${a ? ' accent' : ''}">${esc(t)}</span>`).join('');
  $('#sheetTags').textContent = it.tags && it.tags !== '—' ? '标签 · ' + it.tags : '';

  const img = $('#sheetImg');
  if (it.cover) { img.src = it.cover; img.style.display = 'block'; }
  else { img.removeAttribute('src'); img.style.display = 'none'; }

  const hasDir = !!it.dir;
  $('#actRead').style.display = hasDir ? '' : 'none';
  $('#actFolder').style.display = hasDir ? '' : 'none';
  $('#actPdf').style.display = hasDir ? '' : 'none';
  $('#actZip').style.display = hasDir ? '' : 'none';
  $('#actDel').style.display = hasDir ? '' : 'none';

  $('#sheet').classList.add('on');
  $('#sheetMask').classList.add('on');
}

function closeSheet() {
  $('#sheet').classList.remove('on');
  $('#sheetMask').classList.remove('on');
}

/* ---------------- 动作 ---------------- */
function parseTarget() {
  const v = $('#q').value.trim();
  if (!v) { toast('先填个车号或关键词', 'err'); return null; }
  return v;
}

async function doDownload() {
  const v = parseTarget();
  if (!v) return;
  const r = await api().download(v);
  if (!r.ok) toast(r.msg || '下载失败', 'err');
}

async function doQuery() {
  const v = parseTarget();
  if (!v) return;
  $('#statusText').textContent = '查询中 …';
  const r = await api().query(v);
  if (!r.ok) { toast(r.msg || '没查到', 'err'); $('#statusText').textContent = '就绪'; return; }
  if (r.searched) {
    LAST_SEARCH = v;
    S.items = r.items || [];
    S.view = 'results';
    document.querySelectorAll('.nav-item').forEach((b) =>
      b.classList.toggle('is-active', b.dataset.view === 'results'));
    S.items = (r.items || []).map((it) => it);
    if (typeof renderGrid === 'function') renderGrid();
    $('#statusText').textContent = `按关键词搜到 ${(r.items || []).length} 条`;
    toast('这是搜索结果，点开卡片用「下载 / 更新」下载', '');
    return;
  }
  S.view = 'results';
  document.querySelectorAll('.nav-item').forEach((b) =>
    b.classList.toggle('is-active', b.dataset.view === 'results'));
  S.items = r.items || [];
  renderGrid();
  $('#statusText').textContent = `查到 ${S.items.length} 条`;
  if (S.items.length === 1) openSheet(S.items[0]);
}

async function doSearch() {
  const v = parseTarget();
  if (!v) return;
  $('#statusText').textContent = '搜索中 …';
  const r = await api().search(v, S.kind);
  if (!r.ok) { toast(r.msg || '搜索失败', 'err'); return; }
  S.view = 'results';
  S.items = r.items || [];
  renderGrid();
  $('#statusText').textContent = `搜索到 ${S.items.length} 条`;
}

async function doCancel() {
  await api().cancel();
  toast('已发出取消', '');
}

/* ---------------- 进度轮询 ---------------- */
async function poll() {
  try {
    const st = await api().poll();
    if (!st) return;
    S.busy = st.busy;
    $('#downloadBtn').disabled = st.busy;
    $('#queryBtn').disabled = st.busy;
    $('#cancelBtn').disabled = !st.busy;
    $('#statusText').textContent = st.status || '就绪';

    const total = st.total || 0, done = st.done || 0;
    const pct = total > 0 ? Math.min(100, Math.round(done * 100 / total)) : 0;
    $('#progressFill').style.width = pct + '%';
    $('#pctText').textContent = total > 0 ? `${done}/${total} · ${pct}%` : '';

    if (st.logs && st.logs.length) {
      const box = $('#logList');
      box.innerHTML = st.logs.slice(-60).map((l) =>
        `<div><span class="t">${esc(l.t)}</span><span class="${esc(l.k || '')}">${esc(l.m)}</span></div>`
      ).join('');
      box.scrollTop = box.scrollHeight;
    }

    try { renderQueue(st); } catch (e) {}
    if (S.lastDone && !st.busy && st.task === 'download' && S.lastDone !== st.finishedAt) {
      loadView();
    }
    S.lastDone = st.finishedAt || 0;
  } catch (e) { /* 桥还没就绪 */ }
}

/* ---------------- 事件绑定 ---------------- */
function bind() {
  $('#downloadBtn').onclick = doDownload;
  $('#queryBtn').onclick = doQuery;
  $('#cancelBtn').onclick = doCancel;

  $('#q').addEventListener('keydown', (e) => {
    if (e.key !== 'Enter') return;
    const v = $('#q').value.trim();
    // 纯车号 → 下载；带文字 → 搜索
    if (/^[pP]?\d{3,8}(\s+[pP]?\d{3,8})*$/.test(v)) doDownload();
    else if (v) { S.kind === 'all' ? doSearch() : doQuery(); }
  });

  $('#kindSeg').addEventListener('click', (e) => {
    const b = e.target.closest('.seg-btn');
    if (!b) return;
    S.kind = b.dataset.kind;
    $('#kindSeg').querySelectorAll('.seg-btn').forEach((x) =>
      x.classList.toggle('is-active', x === b));
    if ($('#q').value.trim()) doSearch();
  });

  document.querySelectorAll('.nav-item').forEach((b) => {
    b.onclick = () => setView(b.dataset.view);
  });

  $('#sheetMask').onclick = closeSheet;
  $('#sheetClose').onclick = closeSheet;
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeSheet(); });

  $('#logToggle').onclick = () => {
    S.logOpen = !S.logOpen;
    $('#logPanel').classList.toggle('on', S.logOpen);
    $('#logToggle').textContent = S.logOpen ? '日志 ▴' : '日志 ▾';
  };

  $('#themeBtn').onclick = async () => {
    const idx = THEMES.findIndex(([k]) => k === S.theme);
    const [key, label] = THEMES[(idx + 1) % THEMES.length];
    applyTheme(key);
    await api().set_theme(key);
    toast('主题：' + label);
  };

  $('#folderBtn').onclick = () => api().open_base();

  // 详情抽屉动作
  $('#actRead').onclick = () => { if (S.selected) openReader(S.selected); };
  $('#actFolder').onclick = $('#actRead').onclick;
  $('#actPdf').onclick = () => exportIt('pdf');
  $('#actZip').onclick = () => exportIt('zip');
  $('#actDel').onclick = async () => {
    if (!S.selected) return;
    const ok = confirm(`把这个文件夹移到回收站吗？\n\n${S.selected.dir || ''}`);
    if (!ok) return;
    const r = await api().trash(S.selected.key || S.selected.dir);
    if (r.ok) { toast('已移到回收站'); closeSheet(); loadView(); }
    else toast(r.msg || '删除失败', 'err');
  };
}

async function exportIt(fmt) {
  if (!S.selected) return;
  toast(`正在导出 ${fmt.toUpperCase()} …`, '');
  const r = await api().export_it(S.selected.key || S.selected.dir, fmt);
  if (r.ok) toast(`已导出：${r.msg}`);
  else toast(r.msg || '导出失败', 'err');
}

/* ============================================================
   内置阅读器：单页 / 滚动、缩放、翻页、拖拽平移
   ============================================================ */
const R = {
  open: false, chapters: [], ci: 0, idx: 0,
  zoom: 1, px: 0, py: 0, mode: 'single', drag: null, lastWheel: 0,
};

async function openReader(item) {
  const r = await api().chapters(item.key || item.dir);
  if (!r.ok) { toast(r.msg || '打不开', 'err'); return; }
  R.chapters = r.chapters || [];
  R.ci = 0; R.idx = 0; R.zoom = 1; R.px = 0; R.py = 0; R.mode = 'single';
  R.open = true;
  $('#reader').classList.add('on');
  $('#rdTitle').textContent = r.title || item.title || '';
  const sel = $('#rdChapter');
  sel.innerHTML = R.chapters.map((c, i) =>
    `<option value="${i}">${esc(c.name)}</option>`).join('');
  sel.style.display = R.chapters.length > 1 ? '' : 'none';
  sel.value = '0';
  setMode('single');
  renderPage();
}

function closeReader() {
  R.open = false;
  $('#reader').classList.remove('on');
}

function setMode(mode) {
  R.mode = mode;
  $('#reader').classList.toggle('scrollmode', mode === 'scroll');
  $('#rdScroll').classList.toggle('on', mode === 'scroll');
  $('#rdMode').textContent = mode === 'scroll' ? '滚动' : '单页';
  $('#rdMode').classList.toggle('is-active', mode === 'scroll');
  if (mode === 'scroll') buildScroll();
  else renderPage();
}

function renderPage() {
  const ch = R.chapters[R.ci];
  if (!ch) return;
  R.idx = Math.max(0, Math.min(R.idx, ch.images.length - 1));
  const img = $('#rdImg');
  img.classList.remove('fade');
  void img.offsetWidth;
  img.src = ch.images[R.idx];
  img.classList.add('fade');
  R.px = 0; R.py = 0;
  applyZoom();
  $('#rdPage').textContent = `${R.idx + 1} / ${ch.images.length}`;
  if (R.chapters.length > 1) $('#rdChapter').value = String(R.ci);
}

function applyZoom() {
  const img = $('#rdImg');
  img.style.transform = `translate(${R.px}px, ${R.py}px) scale(${R.zoom})`;
  $('#rdZoomTxt').textContent = Math.round(R.zoom * 100) + '%';
}

function setZoom(z) {
  R.zoom = Math.min(6, Math.max(0.4, z));
  applyZoom();
}

function flip(d) {
  const ch = R.chapters[R.ci];
  if (!ch) return;
  if (R.mode === 'scroll') {
    R.idx = Math.max(0, Math.min(R.idx + d, ch.images.length - 1));
    buildScroll();
    return;
  }
  let n = R.idx + d;
  if (n < 0) {
    if (R.ci > 0) { R.ci--; R.idx = R.chapters[R.ci].images.length - 1; renderPage(); }
    return;
  }
  if (n >= ch.images.length) {
    if (R.ci < R.chapters.length - 1) { R.ci++; R.idx = 0; renderPage(); }
    return;
  }
  R.idx = n;
  renderPage();
}

function buildScroll() {
  const ch = R.chapters[R.ci];
  const box = $('#rdScroll');
  box.innerHTML = ch.images.map((u, i) =>
    `<img src="${u}" loading="lazy" data-i="${i}" alt="">`).join('');
  const t = box.querySelector(`img[data-i="${R.idx}"]`);
  if (t) t.scrollIntoView({ block: 'start' });
}

function bindReader() {
  $('#rdClose').onclick = closeReader;
  $('#rdMode').onclick = () => setMode(R.mode === 'scroll' ? 'single' : 'scroll');
  $('#rdZoomIn').onclick = () => setZoom(R.zoom * 1.2);
  $('#rdZoomOut').onclick = () => setZoom(R.zoom / 1.2);
  $('#rdFit').onclick = () => { R.zoom = 1; R.px = 0; R.py = 0; applyZoom(); };
  $('#rdChapter').onchange = (e) => {
    R.ci = parseInt(e.target.value, 10) || 0;
    R.idx = 0;
    if (R.mode === 'scroll') buildScroll(); else renderPage();
  };

  const stage = $('#rdStage');
  stage.addEventListener('wheel', (e) => {
    if (!R.open) return;
    e.preventDefault();
    if (e.ctrlKey) { setZoom(R.zoom * (e.deltaY > 0 ? 0.9 : 1.1)); return; }
    const now = Date.now();
    if (now - R.lastWheel < 220) return;
    R.lastWheel = now;
    flip(e.deltaY > 0 ? 1 : -1);
  }, { passive: false });

  stage.addEventListener('mousedown', (e) => {
    if (R.zoom <= 1) return;
    R.drag = { x: e.clientX, y: e.clientY, px: R.px, py: R.py };
    stage.classList.add('dragging');
  });
  window.addEventListener('mousemove', (e) => {
    if (!R.drag) return;
    R.px = R.drag.px + (e.clientX - R.drag.x);
    R.py = R.drag.py + (e.clientY - R.drag.y);
    applyZoom();
  });
  window.addEventListener('mouseup', () => {
    R.drag = null;
    stage.classList.remove('dragging');
  });

  // 滚动模式：滚到哪页就记哪页
  $('#rdScroll').addEventListener('scroll', () => {
    if (R.mode !== 'scroll') return;
    const imgs = [...$('#rdScroll').querySelectorAll('img')];
    const y = $('#rdScroll').scrollTop + 40;
    for (const im of imgs) {
      if (im.offsetTop + im.offsetHeight > y) { R.idx = parseInt(im.dataset.i, 10) || 0; break; }
    }
    const ch = R.chapters[R.ci];
    if (ch) $('#rdPage').textContent = `${R.idx + 1} / ${ch.images.length}`;
  });

  document.addEventListener('keydown', (e) => {
    if (!R.open) return;
    if (e.key === 'Escape') { closeReader(); return; }
    if (e.key === 'ArrowRight' || e.key === 'PageDown' || e.key === ' ') { e.preventDefault(); flip(1); }
    else if (e.key === 'ArrowLeft' || e.key === 'PageUp') { e.preventDefault(); flip(-1); }
    else if (e.key === '+' || e.key === '=') setZoom(R.zoom * 1.15);
    else if (e.key === '-' || e.key === '_') setZoom(R.zoom / 1.15);
    else if (e.key === '0') { R.zoom = 1; R.px = 0; R.py = 0; applyZoom(); }
    else if (e.key === 'Home') { R.idx = 0; R.mode === 'scroll' ? buildScroll() : renderPage(); }
    else if (e.key === 'End') { R.idx = 1e9; R.mode === 'scroll' ? buildScroll() : renderPage(); }
  });
}

/* ---------------- 自测入口（截图用，不参与正常流程） ---------------- */
window.__demoReader = async () => {
  if (!S.items.length) { toast('书库空，无法演示', 'err'); return; }
  S.selected = S.items[0];
  await openReader(S.items[0]);
  closeSheet();
};

/* ============================================================
   阅读器 2（覆盖上面的旧实现）
   · 内联在内容区，返回即恢复列表，不开新窗口
   · 图片尺寸 = 自然尺寸 × 适应比例 × 缩放，滚动容器平移 → 位置/缩放准确
   · 进度按本子记在 localStorage，重新打开自动回到上次页码
   · 预加载后两页
   ============================================================ */
const RD = { open: false, chapters: [], ci: 0, idx: 0, zoom: 1, base: 1,
             mode: 'single', key: '', wheelT: 0, drag: null };

function _contentEl() { return document.querySelector('.content'); }

async function openReader(item) {
  const r = await api().chapters(item.key || item.dir);
  if (!r.ok) { toast(r.msg || '打不开', 'err'); return; }
  RD.chapters = r.chapters || [];
  RD.key = item.key || item.dir || '';
  RD.ci = 0; RD.idx = 0; RD.zoom = 1; RD.mode = 'single';
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem('rd:' + RD.key) || 'null'); } catch (e) { saved = null; }
  if (saved && saved.ci < RD.chapters.length) {
    RD.ci = saved.ci;
    const n = RD.chapters[RD.ci].images.length;
    RD.idx = Math.max(0, Math.min(saved.idx || 0, n - 1));
  }
  RD.open = true;
  try { closeSheet(); } catch (e) {}
  _contentEl().classList.add('reading');
  $('#reader').classList.add('on');
  $('#rdTitle').textContent = r.title || item.title || '';
  const sel = $('#rdChapter');
  sel.innerHTML = RD.chapters.map((c, i) =>
    `<option value="${i}">${esc(c.name)}</option>`).join('');
  sel.style.display = RD.chapters.length > 1 ? '' : 'none';
  sel.value = String(RD.ci);
  setMode('single');
  if (saved) toast(`已回到上次位置：第 ${RD.idx + 1} 页`);
}

function closeReader() {
  if (!RD.open) return;
  RD.open = false;
  saveProgress();
  $('#reader').classList.remove('on');
  _contentEl().classList.remove('reading');
}

function saveProgress() {
  if (!RD.key || !RD.chapters[RD.ci]) return;
  try {
    localStorage.setItem('rd:' + RD.key,
      JSON.stringify({ ci: RD.ci, idx: RD.idx, ts: Date.now() }));
  } catch (e) { /* 忽略 */ }
}

function setMode(mode) {
  RD.mode = mode;
  $('#reader').classList.toggle('scrollmode', mode === 'scroll');
  $('#rdScroll').classList.toggle('on', mode === 'scroll');
  $('#rdMode').textContent = mode === 'scroll' ? '滚动' : '单页';
  applyScale();
  if (mode === 'scroll') buildScroll(); else renderPage();
}

function renderPage() {
  const ch = RD.chapters[RD.ci];
  if (!ch) return;
  RD.idx = Math.max(0, Math.min(RD.idx, ch.images.length - 1));
  const img = $('#rdImg');
  img.onload = () => { fitImage(); preloadNext(); };
  img.src = ch.images[RD.idx];
  if (img.complete && img.naturalWidth) { fitImage(); preloadNext(); }
  $('#rdPage').textContent = `${RD.idx + 1} / ${ch.images.length}`;
  $('#rdChapter').value = String(RD.ci);
  saveProgress();
}

function fitImage() {
  const img = $('#rdImg'), view = $('#rdView');
  if (!img.naturalWidth || !view.clientHeight) return;
  const vw = Math.max(80, view.clientWidth - 16);
  const vh = Math.max(80, view.clientHeight - 16);
  RD.base = Math.min(vw / img.naturalWidth, vh / img.naturalHeight);
  RD.zoom = 1;
  applyScale();
  view.scrollTop = 0;
  view.scrollLeft = Math.max(0, (view.scrollWidth - view.clientWidth) / 2);
}

function applyScale() {
  const img = $('#rdImg');
  if (!img.naturalWidth) return;
  const w = Math.round(img.naturalWidth * RD.base * RD.zoom);
  const h = Math.round(img.naturalHeight * RD.base * RD.zoom);
  img.style.width = w + 'px';
  img.style.height = h + 'px';
  img.classList.toggle('zoomed', RD.zoom > 1.001);
  const sc = $('#rdScroll');
  if (sc) sc.style.setProperty('--rz', RD.zoom);   // 滚动模式同步缩放
  $('#rdZoomTxt').textContent = Math.round(RD.zoom * 100) + '%';
}

function imageUnder(e) {
  if (RD.mode === 'single') return $('#rdImg');
  const imgs = $('#rdScroll').querySelectorAll('img');
  for (const im of imgs) {
    const r = im.getBoundingClientRect();
    if (e.clientX >= r.left && e.clientX <= r.right &&
        e.clientY >= r.top && e.clientY <= r.bottom) return im;
  }
  return null;
}

/* 以鼠标位置为中心的缩放：单页 / 滚动通用 */
function setZoomAt(z, e) {
  const view = $('#rdView'), sc = $('#rdScroll');
  const container = (RD.mode === 'scroll') ? sc : view;
  const img = imageUnder(e);
  const before = img ? img.getBoundingClientRect() : null;
  const fx = before ? (e.clientX - before.left) / Math.max(1, before.width) : 0.5;
  const fy = before ? (e.clientY - before.top) / Math.max(1, before.height) : 0.5;
  RD.zoom = Math.min(6, Math.max(0.2, z));
  applyScale();
  const fix = () => {
    const after = img ? img.getBoundingClientRect() : null;
    if (!after) return;
    container.scrollLeft += (after.left + fx * after.width) - e.clientX;
    container.scrollTop += (after.top + fy * after.height) - e.clientY;
  };
  fix();
  requestAnimationFrame(fix);      // 布局稳定后再补一次，锚点更准
}

function setZoom(z) {
  const view = $('#rdView'), img = $('#rdImg');
  const oldW = img.clientWidth || 1, oldH = img.clientHeight || 1;
  const cx = (view.scrollLeft + view.clientWidth / 2) / oldW;
  const cy = (view.scrollTop + view.clientHeight / 2) / oldH;
  RD.zoom = Math.min(6, Math.max(0.2, z));
  applyScale();
  view.scrollLeft = cx * img.clientWidth - view.clientWidth / 2;
  view.scrollTop = cy * img.clientHeight - view.clientHeight / 2;
}

function preloadNext() {
  const ch = RD.chapters[RD.ci];
  if (!ch) return;
  [RD.idx + 1, RD.idx + 2].forEach((i) => {
    if (ch.images[i]) { const im = new Image(); im.src = ch.images[i]; }
  });
}

function flip(d) {
  const ch = RD.chapters[RD.ci];
  if (!ch) return;
  if (RD.mode === 'scroll') {
    RD.idx = Math.max(0, Math.min(RD.idx + d, ch.images.length - 1));
    buildScroll();
    return;
  }
  const n = RD.idx + d;
  if (n < 0) {
    if (RD.ci > 0) { RD.ci--; RD.idx = RD.chapters[RD.ci].images.length - 1; renderPage(); }
    return;
  }
  if (n >= ch.images.length) {
    if (RD.ci < RD.chapters.length - 1) { RD.ci++; RD.idx = 0; renderPage(); }
    return;
  }
  RD.idx = n;
  renderPage();
}

function buildScroll() {
  const ch = RD.chapters[RD.ci];
  const box = $('#rdScroll');
  box.innerHTML = ch.images.map((u, i) =>
    `<img src="${u}" loading="lazy" data-i="${i}" alt="">`).join('');
  const t = box.querySelector(`img[data-i="${RD.idx}"]`);
  if (t) t.scrollIntoView({ block: 'start' });
  $('#rdPage').textContent = `${RD.idx + 1} / ${ch.images.length}`;
  applyScale();
}

function bindReader() {
  $('#rdBack').onclick = closeReader;
  $('#rdMode').onclick = () => setMode(RD.mode === 'scroll' ? 'single' : 'scroll');
  $('#rdZoomIn').onclick = () => setZoom(RD.zoom * 1.25);
  $('#rdZoomOut').onclick = () => setZoom(RD.zoom / 1.25);
  $('#rdZoomFit').onclick = () => fitImage();
  $('#rdTheme').onclick = async () => {
    const i = THEMES.findIndex(([k]) => k === S.theme);
    const [key, label] = THEMES[(i + 1) % THEMES.length];
    applyTheme(key);
    try { await api().set_theme(key); } catch (e) { /* 忽略 */ }
    toast('主题：' + label);
  };
  $('#rdChapter').onchange = (e) => {
    RD.ci = parseInt(e.target.value, 10) || 0;
    RD.idx = 0;
    if (RD.mode === 'scroll') buildScroll(); else renderPage();
  };

  // 双击：适应窗口 / 1:1 像素
  const img = $('#rdImg');
  img.addEventListener('dblclick', () => {
    const view = $('#rdView');
    if (RD.zoom > 1.001) { fitImage(); return; }
    RD.zoom = 1 / Math.max(0.01, RD.base);
    applyScale();
    view.scrollLeft = (view.scrollWidth - view.clientWidth) / 2;
    view.scrollTop = Math.max(0, (view.scrollHeight - view.clientHeight) / 2);
  });

  // 滚轮：Ctrl/Shift/Alt 任一 + 滚轮 = 缩放图片（两种模式都生效）；
  // 单页模式下普通滚轮 = 翻页；滚动模式普通滚轮交给容器自己滚
  const view = $('#rdView');
  const shell = $('#reader');
  shell.addEventListener('wheel', (e) => {
    if (!RD.open) return;
    if (e.ctrlKey || e.shiftKey || e.altKey) {
      e.preventDefault();
      e.stopPropagation();
      setZoomAt(RD.zoom * (e.deltaY > 0 ? 0.9 : 1.1), e);
      return;
    }
    if (RD.mode === 'scroll') return;
    if (!view.contains(e.target)) return;
    e.preventDefault();
    const now = Date.now();
    if (now - RD.wheelT < 200) return;
    RD.wheelT = now;
    flip(e.deltaY > 0 ? 1 : -1);
  }, { passive: false, capture: true });

  // 拖拽平移（缩放后）
  let drag = null;
  view.addEventListener('mousedown', (e) => {
    if (RD.zoom <= 1.001) return;
    drag = { x: e.clientX, y: e.clientY, l: view.scrollLeft, t: view.scrollTop };
    img.classList.add('dragging');
  });
  window.addEventListener('mousemove', (e) => {
    if (!drag) return;
    view.scrollLeft = drag.l - (e.clientX - drag.x);
    view.scrollTop = drag.t - (e.clientY - drag.y);
  });
  window.addEventListener('mouseup', () => {
    drag = null;
    img.classList.remove('dragging');
  });

  // 滚动模式：记录当前页
  $('#rdScroll').addEventListener('scroll', () => {
    if (RD.mode !== 'scroll') return;
    const imgs = [...$('#rdScroll').querySelectorAll('img')];
    const y = $('#rdScroll').scrollTop + 40;
    for (const im of imgs) {
      if (im.offsetTop + im.offsetHeight > y) { RD.idx = parseInt(im.dataset.i, 10) || 0; break; }
    }
    const ch = RD.chapters[RD.ci];
    if (ch) $('#rdPage').textContent = `${RD.idx + 1} / ${ch.images.length}`;
    saveProgress();
  });

  document.addEventListener('keydown', (e) => {
    if (!RD.open) return;
    if (e.key === 'Escape' || e.key === 'Backspace') { e.preventDefault(); closeReader(); return; }
    if (e.key === 'ArrowRight' || e.key === 'PageDown' || e.key === ' ') { e.preventDefault(); flip(1); }
    else if (e.key === 'ArrowLeft' || e.key === 'PageUp') { e.preventDefault(); flip(-1); }
    else if (e.key === '+' || e.key === '=') setZoom(RD.zoom * 1.15);
    else if (e.key === '-' || e.key === '_') setZoom(RD.zoom / 1.15);
    else if (e.key === '0') fitImage();
    else if (e.key === 'Home') { RD.idx = 0; RD.mode === 'scroll' ? buildScroll() : renderPage(); }
    else if (e.key === 'End') { RD.idx = 1e9; RD.mode === 'scroll' ? buildScroll() : renderPage(); }
  });

  // 窗口尺寸变化后重新适应
  window.addEventListener('resize', () => {
    if (RD.open && RD.mode === 'single') fitImage();
  });
}

/* 自测入口：可指定模式与缩放（截图/验证用） */
window.__demoReader = async (mode, zoom) => {
  if (!S.items.length) { toast('书库空', 'err'); return; }
  S.selected = S.items[0];
  await openReader(S.items[0]);
  if (mode === 'scroll') setMode('scroll');
  if (zoom) setZoom(zoom);
};

/* 自测：量化"以鼠标为中心缩放"的锚点漂移，结果写到标题 */
window.__anchorTest = () => {
  const box = (RD.mode === 'scroll') ? $('#rdScroll') : $('#rdView');
  const img = (RD.mode === 'scroll')
    ? $('#rdScroll').querySelector('img') : $('#rdImg');
  if (!img) return -1;
  try { img.scrollIntoView({ block: 'center' }); } catch (e) {}
  const r0 = img.getBoundingClientRect();
  const cx = r0.left + r0.width * 0.25;
  const cy = r0.top + r0.height * 0.25;
  const fx = (cx - r0.left) / r0.width;
  const fy = (cy - r0.top) / r0.height;
  document.querySelector('#reader').dispatchEvent(new WheelEvent('wheel', {
    deltaY: -120, ctrlKey: true, bubbles: true, cancelable: true,
    clientX: cx, clientY: cy,
  }));
  const r1 = img.getBoundingClientRect();
  const drift = Math.hypot(r1.left + fx * r1.width - cx, r1.top + fy * r1.height - cy);
  document.title = 'JMComic 锚点漂移 ' + drift.toFixed(1) + 'px zoom=' +
                   Math.round(RD.zoom * 100) + '%';
  return drift;
};

/* 自测：合成一次滚轮事件（验证 Ctrl+滚轮是否走到缩放逻辑） */
window.__testWheel = (ctrl, dy, times) => {
  const el = document.querySelector('#reader');
  let n = times || 1;
  for (let i = 0; i < n; i++) {
    el.dispatchEvent(new WheelEvent('wheel', {
      deltaY: dy || -120, ctrlKey: !!ctrl, bubbles: true, cancelable: true,
    }));
  }
  return RD.zoom;
};

/* ---------------- 启动 ---------------- */
window.addEventListener('pywebviewready', async () => {
  bind();
  bindReader();
  const cfg = await api().settings();
  applyTheme(cfg.theme || 'dark');
  toast('就绪 · ' + (cfg.base || ''));
  await loadView();
  setInterval(poll, 260);
});


/* ============================================================
   追更（关注列表）+ 已下载库增强（筛选 / 排序 / 阅读进度 / 双击续读）
   ============================================================ */
let SORT = 'recent';

function watchBadgeCount() {
  return (S.items || []).filter((x) => x.watching && (x.newCount || 0) > 0).length;
}

function progressOf(key) {
  try { return JSON.parse(localStorage.getItem('rd:' + key) || 'null'); }
  catch (e) { return null; }
}

/* 覆盖：视图加载（新增 watch；local 走筛选） */
async function loadView() {
  const v = S.view;
  document.querySelectorAll('.nav-item').forEach((b) =>
    b.classList.toggle('is-active', b.dataset.view === v));
  syncTopTools();
  if (v === 'watch') {
    renderSkeleton(4);
    const list = (await api().watch_list()).authors || [];
    S.items = list.map((w) => ({
      key: 'album:' + w.id, id: w.id, title: w.title,
      authors: w.authors || '', pages: w.pages || 0, chapters: w.chapters || 0,
      cover: w.cover || '', dir: w.dir || '',
      newCount: w.newCount || 0, watching: true,
      meta: (w.newCount > 0 ? `${w.newCount} 话有更新` : '已是最新') +
            (w.lastCheck ? ' · ' + w.lastCheck : ''),
      badge: w.newCount > 0 ? `+${w.newCount}` : '',
    }));
    renderGrid();
    const upd = watchBadgeCount();
    $('#statusText').textContent = `关注 ${S.items.length} 本` +
      (upd ? `，${upd} 本有更新` : '');
    return;
  }
  if (v === 'local') {
    renderSkeleton(8);
    S.allLocal = (await api().list_local()).items || [];
    applyFilter();
    return;
  }
  if (v === 'history') {
    S.items = (await api().list_history()).items || [];
    renderGrid();
    return;
  }
  renderGrid();
}

function applyFilter() {
  const q = ($('#q').value || '').trim().toLowerCase();
  let arr = (S.allLocal || []).slice();
  if (q) {
    arr = arr.filter((it) =>
      ((it.title || '') + ' ' + (it.authors || '') + ' ' + (it.tags || ''))
        .toLowerCase().includes(q));
  }
  if (SORT === 'name') arr.sort((x, y) => String(x.title).localeCompare(String(y.title), 'zh'));
  else if (SORT === 'pages') arr.sort((x, y) => (y.pages || 0) - (x.pages || 0));
  else if (SORT === 'chapters') arr.sort((x, y) => (y.chapters || 0) - (x.chapters || 0));
  S.items = arr;
  renderGrid();
  $('#statusText').textContent = `已下载 ${arr.length}` +
    (q ? ` / ${(S.allLocal || []).length}（筛选：${q}）` : ' 项');
}

/* 覆盖：卡片渲染（加阅读进度、双击续读、更新徽章） */
function renderGrid() {
  const g = $('#grid');
  g.innerHTML = '';
  const items = S.items || [];
  $('#empty').style.display = items.length ? 'none' : 'grid';
  items.forEach((it, i) => {
    const card = document.createElement('button');
    card.className = 'card';
    card.style.animationDelay = Math.min(i, 20) * 16 + 'ms';
    const p = progressOf(it.key);
    const pct = (p && it.pages) ? Math.min(100, Math.round((p.idx + 1) * 100 / it.pages)) : 0;
    const badge = it.badge ? `<span class="badge">${esc(it.badge)}</span>` : '';
    let prog = '';
    if (p) {
      prog = `<span class="badge prog" title="阅读进度">${p.idx + 1}` +
             (it.pages ? '/' + it.pages : '') + '</span>';
    }
    const cover = it.cover
      ? `<img src="${esc(it.cover)}" loading="lazy" alt="" onerror="this.style.display='none'">`
      : '';
    card.innerHTML = `<div class="cover">${cover}${badge}${prog}</div>
      <div class="title">${esc(it.title || '未命名')}</div>
      <div class="meta">${esc(it.meta || '')}</div>`;
    card.onclick = () => openSheet(it);
    if (it.dir || it.watching) {
      card.ondblclick = () => openReader(it);
    }
    g.appendChild(card);
  });
}

/* 顶栏工具按视图显隐 */
function syncTopTools() {
  $('#sortSel').style.display = (S.view === 'local') ? '' : 'none';
  $('#watchCheck').style.display = (S.view === 'watch') ? '' : 'none';
  const sel = $('#sortSel');
  if (sel && sel.value !== SORT) sel.value = SORT;
}

function initExtras() {
  $('#actWatch').onclick = async () => {
    if (!S.selected) return;
    const id = String(S.selected.id || '').replace(/^.*?:/, '');
    const r = await api().watch_toggle(id);
    if (r.ok) {
      toast(r.watching ? '已关注（可在「关注」里检查更新）' : '已取消关注');
      if (S.view === 'watch') loadView();
    } else {
      toast(r.msg || '操作失败', 'err');
    }
  };
  $('#actUpdate').onclick = async () => {
    if (!S.selected) return;
    const id = String(S.selected.id || '').replace(/^.*?:/, '');
    const r = await api().download(id);
    toast(r.ok ? '已开始下载 / 更新（已存在的图片会跳过）' : (r.msg || '失败'),
          r.ok ? 'ok' : 'err');
  };
  $('#watchCheck').onclick = async () => {
    $('#statusText').textContent = '正在检查更新（含限速，稍等）…';
    $('#watchCheck').disabled = true;
    const r = await api().watch_check();
    $('#watchCheck').disabled = false;
    if (r.ok) {
      toast(`检查完成：${r.updated} / ${r.total} 本有更新`, 'ok');
      loadView();
    } else {
      toast(r.msg || '检查失败', 'err');
      loadView();
    }
  };
  $('#sortSel').onchange = (e) => { SORT = e.target.value || 'recent'; applyFilter(); };

  // 本地库：输入框实时筛选（Enter 不触发下载/搜索）
  $('#q').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && S.view === 'local') {
      e.preventDefault();
      e.stopImmediatePropagation();
      applyFilter();
    }
  }, true);
  $('#q').addEventListener('input', () => {
    if (S.view === 'local') applyFilter();
  });
}

window.addEventListener('pywebviewready', () => {
  try { initExtras(); } catch (e) { /* 忽略 */ }
});


/* ============================================================
   阅读器 3：双页模式 + 适应宽度/高度（覆盖上面的实现）
   ============================================================ */
RD.fit = RD.fit || 'contain';

function _viewBox() {
  const v = $('#rdView');
  return { vw: Math.max(80, v.clientWidth - 16), vh: Math.max(80, v.clientHeight - 16) };
}

function fitImage() {
  const img = $('#rdImg');
  if (!img.naturalWidth) return;
  const { vw, vh } = _viewBox();
  const nw = img.naturalWidth, nh = img.naturalHeight;
  let s;
  if (RD.mode === 'double') s = Math.min((vw - 10) / 2 / nw, vh / nh);
  else if (RD.fit === 'width') s = vw / nw;
  else if (RD.fit === 'height') s = vh / nh;
  else s = Math.min(vw / nw, vh / nh);
  RD.base = s;
  RD.zoom = 1;
  applyScale();
  const v = $('#rdView');
  v.scrollTop = 0;
  v.scrollLeft = Math.max(0, (v.scrollWidth - v.clientWidth) / 2);
}

function applyScale() {
  [$('#rdImg'), $('#rdImg2')].forEach((img) => {
    if (!img || !img.naturalWidth) return;
    img.style.width = Math.round(img.naturalWidth * RD.base * RD.zoom) + 'px';
    img.style.height = Math.round(img.naturalHeight * RD.base * RD.zoom) + 'px';
  });
  const sc = $('#rdScroll');
  if (sc) sc.style.setProperty('--rz', RD.zoom);
  $('#rdZoomTxt').textContent = Math.round(RD.zoom * 100) + '%';
}

function setMode(mode) {
  RD.mode = mode;
  $('#reader').classList.toggle('scrollmode', mode === 'scroll');
  $('#rdScroll').classList.toggle('on', mode === 'scroll');
  $('#rdCanvas').classList.toggle('double', mode === 'double');
  $('#rdMode').textContent = (mode === 'scroll') ? '滚动'
    : (mode === 'double' ? '双页' : '单页');
  const fm = $('#rdFitMode');
  if (fm) fm.style.display = (mode === 'single') ? '' : 'none';
  if (mode !== 'double') { const i2 = $('#rdImg2'); if (i2) i2.style.display = 'none'; }
  applyScale();
  if (mode === 'scroll') buildScroll(); else renderPage();
}

function renderPage() {
  const ch = RD.chapters[RD.ci];
  if (!ch) return;
  const n = ch.images.length;
  if (RD.mode === 'double' && RD.idx % 2 === 1 && RD.idx > 0) RD.idx -= 1;
  RD.idx = Math.max(0, Math.min(RD.idx, n - 1));
  const img = $('#rdImg');
  img.onload = () => { fitImage(); preloadNext(); };
  img.src = ch.images[RD.idx];
  if (img.complete && img.naturalWidth) { fitImage(); preloadNext(); }
  const img2 = $('#rdImg2');
  if (RD.mode === 'double' && ch.images[RD.idx + 1]) {
    img2.onload = () => fitImage();
    img2.src = ch.images[RD.idx + 1];
    img2.style.display = '';
  } else if (img2) {
    img2.removeAttribute('src');
    img2.style.display = 'none';
  }
  $('#rdPage').textContent = (RD.mode === 'double' && ch.images[RD.idx + 1])
    ? `${RD.idx + 1}-${RD.idx + 2} / ${n}` : `${RD.idx + 1} / ${n}`;
  $('#rdChapter').value = String(RD.ci);
  saveProgress();
}

function flip(d) {
  const ch = RD.chapters[RD.ci];
  if (!ch) return;
  if (RD.mode === 'scroll') {
    RD.idx = Math.max(0, Math.min(RD.idx + d, ch.images.length - 1));
    buildScroll();
    return;
  }
  const step = (RD.mode === 'double') ? 2 : 1;
  const next = RD.idx + d * step;
  if (next < 0) {
    if (RD.ci > 0) { RD.ci--; RD.idx = 1e9; renderPage(); }
    return;
  }
  if (next >= ch.images.length) {
    if (RD.ci < RD.chapters.length - 1) { RD.ci++; RD.idx = 0; renderPage(); }
    return;
  }
  RD.idx = next;
  renderPage();
}

/* 演示入口支持 double / fit */
window.__demoReader = async (mode, zoom, fit) => {
  if (!S.items.length) { toast('书库空', 'err'); return; }
  S.selected = S.items[0];
  await openReader(S.items[0]);
  if (fit) { RD.fit = fit; const fm = $('#rdFitMode'); if (fm) fm.value = fit; }
  if (mode) setMode(mode);
  if (zoom) setZoom(zoom);
};

/* 绑定模式循环 + 适应方式 */
window.addEventListener('pywebviewready', () => {
  const btn = $('#rdMode');
  if (btn) {
    btn.onclick = () => {
      const order = ['single', 'double', 'scroll'];
      setMode(order[(order.indexOf(RD.mode) + 1) % order.length]);
    };
  }
  const fm = $('#rdFitMode');
  if (fm) {
    fm.value = RD.fit;
    fm.onchange = (e) => { RD.fit = e.target.value; fitImage(); };
  }
});


/* ============================================================
   收尾轮：下载队列 UI + 库统计 / 作者排序 / 未读优先
   ============================================================ */

function renderQueue(st) {
  const bar = $('#queueBar');
  if (!bar) return;
  const q = st.queue || [];
  const cur = st.current || '';
  if (!q.length && !cur) { bar.style.display = 'none'; bar.innerHTML = ''; return; }
  let html = '';
  if (cur) html += `<span class="q-item q-now">▶ ${esc(cur)}</span>`;
  q.slice(0, 8).forEach((it) => { html += `<span class="q-item">⏳ ${esc(it.spec)}</span>`; });
  if (q.length > 8) html += `<span class="q-item">…还有 ${q.length - 8} 个</span>`;
  html += '<button class="link-btn" id="qClear">清空队列</button>';
  bar.style.display = '';
  bar.innerHTML = html;
  const c = $('#qClear');
  if (c) c.onclick = async () => { await api().queue_clear(); toast('队列已清空'); };
}

/* 覆盖 loadView：本地库补作者/统计 */
async function loadView() {
  const v = S.view;
  document.querySelectorAll('.nav-item').forEach((b) =>
    b.classList.toggle('is-active', b.dataset.view === v));
  syncTopTools();
  if (v === 'watch') {
    renderSkeleton(4);
    const list = (await api().watch_list()).authors || [];
    S.items = list.map((w) => ({
      key: 'album:' + w.id, id: w.id, title: w.title,
      authors: w.authors || '', pages: w.pages || 0, chapters: w.chapters || 0,
      cover: w.cover || '', dir: w.dir || '',
      newCount: w.newCount || 0, watching: true,
      meta: (w.newCount > 0 ? `${w.newCount} 话有更新` : '已是最新') +
            (w.lastCheck ? ' · ' + w.lastCheck : ''),
      badge: w.newCount > 0 ? `+${w.newCount}` : '',
    }));
    renderGrid();
    const upd = watchBadgeCount();
    $('#statusText').textContent = `关注 ${S.items.length} 本` + (upd ? `，${upd} 本有更新` : '');
    return;
  }
  if (v === 'local') {
    renderSkeleton(8);
    const raw = (await api().list_local()).items || [];
    S.allLocal = raw.map((it) => ({
      ...it,
      meta: [it.authors, `${it.pages} 页`, `${it.chapters} 话`].filter(Boolean).join(' · '),
    }));
    applyFilter();
    try {
      const st = await api().library_stats();
      if (st && st.ok) {
        $('#statusText').textContent += ` · ${st.albums} 本 / ${st.files} 张 / ${st.size_mb} MB`;
      }
    } catch (e) { /* 忽略 */ }
    return;
  }
  if (v === 'history') {
    S.items = (await api().list_history()).items || [];
    renderGrid();
    return;
  }
  renderGrid();
}

/* 覆盖 applyFilter：作者排序 / 未读优先 */
function applyFilter() {
  const q = ($('#q').value || '').trim().toLowerCase();
  let arr = (S.allLocal || []).slice();
  if (q) {
    arr = arr.filter((it) =>
      ((it.title || '') + ' ' + (it.authors || '') + ' ' + (it.tags || ''))
        .toLowerCase().includes(q));
  }
  if (SORT === 'name') arr.sort((x, y) => String(x.title).localeCompare(String(y.title), 'zh'));
  else if (SORT === 'pages') arr.sort((x, y) => (y.pages || 0) - (x.pages || 0));
  else if (SORT === 'chapters') arr.sort((x, y) => (y.chapters || 0) - (x.chapters || 0));
  else if (SORT === 'author') {
    arr.sort((x, y) => String(x.authors || 'zzz').localeCompare(String(y.authors || 'zzz'), 'zh'));
  } else if (SORT === 'unread') {
    arr.sort((x, y) => {
      const px = progressOf(x.key), py = progressOf(y.key);
      return (px ? 1 : 0) - (py ? 1 : 0);       // 没进度的排前面
    });
  }
  S.items = arr;
  renderGrid();
  $('#statusText').textContent = `已下载 ${arr.length}` +
    (q ? ` / ${(S.allLocal || []).length}（筛选：${q}）` : ' 项');
}


/* ============================================================
   回车智能判断：能提取到车号 → 下载；否则 → 搜索
   ============================================================ */
window.addEventListener('pywebviewready', () => {
  const q = $('#q');
  if (!q) return;
  q.addEventListener('keydown', async (e) => {
    if (e.key !== 'Enter' || S.view === 'local') return;
    e.preventDefault();
    e.stopImmediatePropagation();
    const v = (q.value || '').trim();
    if (!v) return;
    let hasId = false;
    try { hasId = (await api().looks_like_id(v)).ok; } catch (err) { hasId = false; }
    if (hasId) doDownload();
    else if (S.kind === 'all') doSearch();
    else doQuery();
  }, true);

  // 下载按钮：车号 → 入队下载；不是车号 → 按关键词搜索，选一本再下载
  const dl = $('#downloadBtn');
  if (dl) {
    dl.onclick = async () => {
      const v = (q.value || '').trim();
      if (!v) { toast('先填车号，或输入本子名 / 作者名', 'err'); return; }
      let hasId = false;
      try { hasId = (await api().looks_like_id(v)).ok; } catch (e) { hasId = false; }
      if (!hasId) {
        S.kind = 'all';
        const seg = $('#kindSeg');
        if (seg) seg.querySelectorAll('.seg-btn').forEach((b) =>
          b.classList.toggle('is-active', b.dataset.kind === 'all'));
        toast('不是车号，已按关键词搜索 —— 点开结果卡片用「下载 / 更新」下载', '');
        await doSearch();
        return;
      }
      const r = await api().download(v);
      if (!r.ok) toast(r.msg || '下载失败', 'err');
      else toast('已加入下载队列');
    };
  }
});


/* ============================================================
   关注改为「作者级」
   ============================================================ */
async function loadAuthorWorks(name) {
  $('#statusText').textContent = `搜索作者 ${name} 的作品…`;
  const r = await api().search(name, 'author');
  if (!r.ok) { toast(r.msg || '搜索失败', 'err'); return; }
  S.items = r.items || [];
  S.view = 'results';
  document.querySelectorAll('.nav-item').forEach((b) =>
    b.classList.toggle('is-active', b.dataset.view === 'results'));
  renderGrid();
  $('#statusText').textContent = `作者 ${name}：${S.items.length} 部作品`;
  toast('点开卡片下载；作者可在详情里「关注作者 / 取消关注」');
}

/* 覆盖 loadView：watch 分支改成作者列表 */
async function loadView() {
  const v = S.view;
  document.querySelectorAll('.nav-item').forEach((b) =>
    b.classList.toggle('is-active', b.dataset.view === v));
  syncTopTools();
  if (v === 'watch') {
    renderSkeleton(6);
    const list = (await api().watch_list()).authors || [];
    S.items = list.map((w) => ({
      key: 'author:' + w.name, id: '', title: w.name,
      authors: w.name, authorName: w.name,
      pages: 0, chapters: 0, dir: '', cover: w.cover || '',
      newCount: w.newCount || 0, watching: true,
      meta: `${w.works || 0} 部作品 · ` +
            (w.newCount > 0 ? `${w.newCount} 部新作` : '已是最新') +
            (w.lastCheck ? ` · ${w.lastCheck}` : ''),
      badge: w.newCount > 0 ? `+${w.newCount}` : '',
    }));
    renderGrid();
    const upd = (S.items || []).filter((x) => (x.newCount || 0) > 0).length;
    $('#statusText').textContent = `关注 ${S.items.length} 位作者` +
      (upd ? `，${upd} 位有新作品` : '');
    return;
  }
  if (v === 'local') {
    renderSkeleton(8);
    const raw = (await api().list_local()).items || [];
    S.allLocal = raw.map((it) => ({
      ...it,
      meta: [it.authors, `${it.pages} 页`, `${it.chapters} 话`].filter(Boolean).join(' · '),
    }));
    applyFilter();
    try {
      const st = await api().library_stats();
      if (st && st.ok) {
        $('#statusText').textContent += ` · ${st.albums} 本 / ${st.files} 张 / ${st.size_mb} MB`;
      }
    } catch (e) { /* 忽略 */ }
    return;
  }
  if (v === 'history') {
    S.items = (await api().list_history()).items || [];
    renderGrid();
    return;
  }
  renderGrid();
}

/* 覆盖 renderGrid：作者卡片点击 = 看TA的作品 */
function renderGrid() {
  const g = $('#grid');
  g.innerHTML = '';
  const items = S.items || [];
  $('#empty').style.display = items.length ? 'none' : 'grid';
  items.forEach((it, i) => {
    const card = document.createElement('button');
    card.className = 'card';
    card.style.animationDelay = Math.min(i, 20) * 16 + 'ms';
    const p = it.authorName ? null : progressOf(it.key);
    const badge = it.badge ? `<span class="badge">${esc(it.badge)}</span>` : '';
    let prog = '';
    if (p) {
      prog = `<span class="badge prog">${p.idx + 1}` +
             (it.pages ? '/' + it.pages : '') + '</span>';
    }
    const cover = it.cover
      ? `<img src="${esc(it.cover)}" loading="lazy" alt="" onerror="this.style.display='none'">`
      : '';
    card.innerHTML = `<div class="cover">${cover}${badge}${prog}</div>
      <div class="title">${esc(it.title || '未命名')}</div>
      <div class="meta">${esc(it.meta || '')}</div>`;
    if (it.authorName) {
      card.onclick = () => loadAuthorWorks(it.authorName);
    } else {
      card.onclick = () => openSheet(it);
      if (it.dir || it.watching) card.ondblclick = () => openReader(it);
    }
    g.appendChild(card);
  });
}

/* 详情里的「关注」= 关注作者 */
window.addEventListener('pywebviewready', () => {
  const w = $('#actWatch');
  if (w) {
    w.textContent = '关注作者';
    w.onclick = async () => {
      const name = (S.selected && (S.selected.authors || S.selected.authorName)) || '';
      if (!name) { toast('这条没有作者信息', 'err'); return; }
      const r = await api().watch_toggle_name(name);
      if (r.ok) {
        toast(r.watching ? `已关注作者：${name}` : `已取消关注：${name}`);
        if (S.view === 'watch') loadView();
      } else {
        toast(r.msg || '操作失败', 'err');
      }
    };
  }
});


/* ============================================================
   关注：按钮状态 + 新作品提醒
   ============================================================ */
function watchNavBtn() {
  return document.querySelector('.nav-item[data-view="watch"]');
}

function setWatchDot(n) {
  const btn = watchNavBtn();
  if (!btn) return;
  let dot = btn.querySelector('.nav-dot');
  if (n > 0) {
    if (!dot) {
      dot = document.createElement('span');
      dot.className = 'nav-dot';
      btn.appendChild(dot);
    }
    dot.title = `${n} 位关注作者有新作品`;
  } else if (dot) {
    dot.remove();
  }
}

async function refreshWatchDot(showToast) {
  try {
    const list = (await api().watch_list()).authors || [];
    const upd = list.filter((x) => (x.newCount || 0) > 0).length;
    setWatchDot(upd);
    if (showToast && upd > 0) {
      toast(`关注里有 ${upd} 位作者有新作品`, 'ok');
    }
    return upd;
  } catch (e) {
    return 0;
  }
}

/* 打开详情时刷新「关注/取消关注」按钮文案 */
(function patchOpenSheet() {
  if (typeof openSheet !== 'function') return;
  const old = openSheet;
  openSheet = function (it) {
    old(it);
    refreshWatchBtn(it);
  };
})();

async function refreshWatchBtn(it) {
  const btn = $('#actWatch');
  if (!btn) return;
  const name = (it && (it.authors || it.authorName)) || '';
  btn.dataset.author = name;
  if (!name) {
    btn.textContent = '关注作者';
    btn.classList.remove('btn-danger');
    btn.classList.add('btn-ghost');
    btn.disabled = true;
    return;
  }
  btn.disabled = false;
  let following = false;
  try { following = (await api().watch_has(name)).ok; } catch (e) { following = false; }
  btn.textContent = following ? '取消关注作者' : '关注作者';
  btn.classList.toggle('btn-danger', following);
  btn.classList.toggle('btn-ghost', !following);
}

/* 覆盖点击：已关注 → 确认后取消；未关注 → 直接关注 */
window.addEventListener('pywebviewready', () => {
  const btn = $('#actWatch');
  if (btn) {
    btn.onclick = async () => {
      const name = btn.dataset.author || (S.selected && (S.selected.authors || S.selected.authorName)) || '';
      if (!name) { toast('这条没有作者信息', 'err'); return; }
      let following = false;
      try { following = (await api().watch_has(name)).ok; } catch (e) { following = false; }
      if (following) {
        if (!confirm(`确定取消关注作者「${name}」吗？`)) return;
        const r = await api().watch_toggle_name(name);
        toast(r.ok ? `已取消关注：${name}` : (r.msg || '操作失败'), r.ok ? '' : 'err');
      } else {
        const r = await api().watch_toggle_name(name);
        toast(r.ok ? `已关注作者：${name}（可在「关注」里检查新作品）` : (r.msg || '操作失败'),
              r.ok ? 'ok' : 'err');
      }
      refreshWatchBtn(S.selected);
      refreshWatchDot(false);
      if (S.view === 'watch') loadView();
    };
  }

  // 启动 4 秒后：加载红点；若上次检查超过 2 小时则后台自动查一次
  setTimeout(async () => {
    await refreshWatchDot(false);
    let last = 0;
    try { last = parseInt(localStorage.getItem('watchCheckTs') || '0', 10) || 0; } catch (e) { last = 0; }
    if (Date.now() - last > 2 * 3600 * 1000) {
      try {
        const list = (await api().watch_list()).authors || [];
        if (!list.length) return;
        $('#statusText').textContent = '后台检查关注更新…';
        const r = await api().watch_check();
        localStorage.setItem('watchCheckTs', String(Date.now()));
        if (r.ok) {
          $('#statusText').textContent = '就绪';
          await refreshWatchDot(false);
          if (r.updated > 0) toast(`有 ${r.updated} 位关注作者发布了新作品`, 'ok');
        }
        if (S.view === 'watch') loadView();
      } catch (e) { /* 忽略 */ }
    }
  }, 4000);
});


/* ============================================================
   搜索结果按官方排序 / 时间范围
   ============================================================ */
let SEARCH_ORDER = 'mr';
let SEARCH_TIME = 'a';
let LAST_SEARCH = '';

function orderTime() {
  return { order: SEARCH_ORDER, time: SEARCH_TIME };
}

/* 覆盖 doSearch：带上排序参数 */
async function doSearch() {
  const v = ($('#q').value || '').trim();
  if (!v) { toast('先输入关键词', 'err'); return; }
  LAST_SEARCH = v;
  $('#statusText').textContent = '搜索中 …';
  const r = await api().search(v, S.kind, SEARCH_ORDER, SEARCH_TIME);
  if (!r.ok) { toast(r.msg || '搜索失败', 'err'); return; }
  S.view = 'results';
  document.querySelectorAll('.nav-item').forEach((b) =>
    b.classList.toggle('is-active', b.dataset.view === 'results'));
  S.items = r.items || [];
  renderGrid();
  const name = { mr: '最新', mv: '最多观看', tf: '最多点赞',
                 mp: '最多图片', tr: '最高评分', md: '最多评论' }[SEARCH_ORDER] || '最新';
  const tn = { a: '全部时间', t: '今天', w: '本周', m: '本月' }[SEARCH_TIME] || '';
  $('#statusText').textContent = `搜到 ${S.items.length} 条 · ${name} · ${tn}`;
}

window.addEventListener('pywebviewready', () => {
  const orderSel = $('#orderSel');
  const timeSel = $('#timeSel');
  if (orderSel) {
    orderSel.value = SEARCH_ORDER;
    orderSel.onchange = (e) => {
      SEARCH_ORDER = e.target.value || 'mr';
      if (LAST_SEARCH) { $('#q').value = LAST_SEARCH; doSearch(); }
    };
  }
  if (timeSel) {
    timeSel.value = SEARCH_TIME;
    timeSel.onchange = (e) => {
      SEARCH_TIME = e.target.value || 'a';
      if (LAST_SEARCH) { $('#q').value = LAST_SEARCH; doSearch(); }
    };
  }
});


/* ============================================================
   搜索结果卡片显示标签行 + 详情卡按需补全页数/章节
   ============================================================ */
/* 在卡片里补一行标签（有 tags 且不是已下载项时） */
(function patchRenderGridTags() {
  if (typeof renderGrid !== 'function') return;
  const prev = renderGrid;
  renderGrid = function () {
    prev();
    try {
      const cards = document.querySelectorAll('#grid .card');
      (S.items || []).forEach((it, i) => {
        const c = cards[i];
        if (!c || it.dir) return;                   // 已下载的不用加
        const t = String(it.tags || '').trim();
        if (!t || t === '—') return;
        const d = document.createElement('div');
        d.className = 'meta2';
        d.textContent = t;
        c.appendChild(d);
      });
    } catch (e) { /* 忽略 */ }
  };
})();

/* 点开搜索结果时，异步用详情接口补全（页数/章节/标签） */
(function patchOpenSheetInfo() {
  if (typeof openSheet !== 'function') return;
  const prev = openSheet;
  openSheet = function (it) {
    prev(it);
    if (!it || !it.id || it.dir || it.pages) return;
    api().album_info(it.id).then((r) => {
      if (!r || !r.ok || !r.info) return;
      const i = r.info;
      it.pages = i.pages || 0;
      it.chapters = i.chapters || 0;
      it.authors = i.authors || it.authors || '';
      it.tags = i.tags || it.tags || '';
      it.meta = [i.pages ? i.pages + ' 页' : '', i.chapters ? i.chapters + ' 话' : '',
                 it.authors].filter(Boolean).join(' · ');
      if (S.selected === it) prev(it);
      try { renderGrid(); } catch (e) { /* 忽略 */ }
    }).catch(() => {});
  };
})();


/* ============================================================
   卡片信息统一 + 详情面板显示全部信息
   ============================================================ */
function cardMeta(it) {
  const p = (it.pages && /\d/.test(String(it.pages))) ? it.pages : 0;
  const c = (it.chapters && /\d/.test(String(it.chapters))) ? it.chapters : 0;
  const bits = [];
  if (p) bits.push(p + ' 页');
  if (c) bits.push(c + ' 话');
  if (it.authors) bits.push(it.authors);
  return bits.join(' · ') || (it.dir ? '' : '…');
}

/* 覆盖 renderGrid：卡片只留统一 meta，不显示标签行 */
(function patchCardMeta() {
  const prev = renderGrid;
  renderGrid = function () {
    prev();
    try {
      const cards = document.querySelectorAll('#grid .card');
      (S.items || []).forEach((it, i) => {
        const c = cards[i];
        if (!c) return;
        c.querySelectorAll('.meta2').forEach((n) => n.remove());
        const m = c.querySelector('.meta');
        if (m) m.textContent = cardMeta(it);
      });
    } catch (e) { /* 忽略 */ }
  };
})();

/* 覆盖 doSearch：先秒出结果，再后台补全详情 */
async function doSearch() {
  const v = ($('#q').value || '').trim();
  if (!v) { toast('先输入关键词', 'err'); return; }
  LAST_SEARCH = v;
  $('#statusText').textContent = '搜索中 …';
  const r = await api().search(v, S.kind, SEARCH_ORDER, SEARCH_TIME);
  if (!r.ok) { toast(r.msg || '搜索失败', 'err'); return; }
  S.view = 'results';
  document.querySelectorAll('.nav-item').forEach((b) =>
    b.classList.toggle('is-active', b.dataset.view === 'results'));
  S.items = r.items || [];
  renderGrid();
  const name = { mr: '最新', mv: '最多观看', tf: '最多点赞',
                 mp: '最多图片', tr: '最高评分', md: '最多评论' }[SEARCH_ORDER] || '最新';
  const tn = { a: '全部时间', t: '今天', w: '本周', m: '本月' }[SEARCH_TIME] || '';
  $('#statusText').textContent = `搜到 ${S.items.length} 条 · ${name} · ${tn} · 补全详情中…`;

  // 分批补全（每批 6 条），每批回来就刷新卡片，让页数/话数尽快出现
  const ids = S.items.map((x) => x.id).filter(Boolean);
  if (ids.length) {
    (async () => {
      let done = 0;
      for (let k = 0; k < ids.length; k += 6) {
        const chunk = ids.slice(k, k + 6);
        let rr = null;
        for (let attempt = 0; attempt < 2; attempt++) {
          try { rr = await api().search_enrich(chunk); } catch (e) { rr = null; }
          // 返回条数不够就当失败，稍等再试一次
          if (rr && rr.ok && (rr.items || []).length >= chunk.length) break;
          await new Promise((res) => setTimeout(res, 1200));
        }
        if (rr && rr.ok) {
          const map = {};
          (rr.items || []).forEach((x) => { map[String(x.id)] = x; });
          (S.items || []).forEach((it) => {
            const x = map[String(it.id)];
            if (!x) return;
            it.pages = x.pages || 0;
            it.chapters = x.chapters || 0;
            it.authors = x.authors || it.authors || '';
            it.tags = x.tags || it.tags || '';
            it.tags_list = x.tags_list || [];
            it.desc = x.desc || '';
            it.meta = [x.pages ? x.pages + ' 页' : '',
                       x.chapters ? x.chapters + ' 话' : '',
                       it.authors].filter(Boolean).join(' · ');
          });
          renderGrid();
        }
        done += chunk.length;
        $('#statusText').textContent =
          `搜到 ${ids.length} 条 · ${name} · ${tn} · 补全 ${Math.min(done, ids.length)}/${ids.length}`;
      }
      $('#statusText').textContent = `搜到 ${ids.length} 条 · ${name} · ${tn}`;
    })();
  }
}

/* 覆盖 openSheet：详情里显示全部信息（基础字段 + 全部标签 + 简介） */
(function patchSheetFull() {
  const prev = openSheet;
  openSheet = function (it) {
    prev(it);
    if (!it) return;
    try {
      const chips = [];
      if (it.id) chips.push(['JM' + it.id, true]);
      if (it.authors) chips.push([it.authors, false]);
      const p = (it.pages && /\d/.test(String(it.pages))) ? it.pages : 0;
      const c = (it.chapters && /\d/.test(String(it.chapters))) ? it.chapters : 0;
      if (p) chips.push([p + ' 页', false]);
      if (c) chips.push([c + ' 话', false]);
      $('#sheetChips').innerHTML = chips
        .map(([t, acc]) => `<span class="chip${acc ? ' accent' : ''}">${esc(t)}</span>`)
        .join('');

      let tags = it.tags_list || [];
      if (!tags.length) {
        tags = String(it.tags || '').split(' / ').filter((t) => t && t !== '—');
      }
      let html = '';
      if (tags.length) {
        html += '<div class="chip-row">' + tags
          .map((t) => `<span class="chip">${esc(t)}</span>`).join('') + '</div>';
      }
      if (it.desc) html += `<div class="desc">${esc(it.desc)}</div>`;
      if (!html) html = '<span class="muted">暂无标签信息</span>';
      $('#sheetTags').innerHTML = html;

      // 还没补全过 → 打开时补一次
      if (!p && it.id && !it.dir) {
        api().album_info(it.id).then((r) => {
          if (!r || !r.ok || !r.info) return;
          const i = r.info;
          it.pages = i.pages || 0;
          it.chapters = i.chapters || 0;
          it.authors = i.authors || it.authors || '';
          it.tags = i.tags || it.tags || '';
          it.tags_list = i.tags_list || [];
          it.desc = i.desc || it.desc || '';
          if (S.selected === it) openSheet(it);
        }).catch(() => {});
      }
    } catch (e) { /* 忽略 */ }
  };
})();


/* 空列表时按视图给不同提示 */
function setEmptyHint(view) {
  const title = $('.empty-title');
  const sub = $('.empty-sub');
  if (!title || !sub) return;
  if (view === 'history') {
    title.textContent = '还没有历史记录';
    sub.textContent = '查询或下载过的本子会自动记在这里（最多 200 条）';
  } else if (view === 'watch') {
    title.textContent = '还没有关注作者';
    sub.textContent = '打开任意作品详情，点「关注作者」，之后就能在这里检查新作品';
  } else if (view === 'results') {
    title.textContent = '还没有搜索结果';
    sub.textContent = '在上面输入车号，或本子名 / 作者名后回车';
  } else {
    title.textContent = '这里还是空的';
    sub.textContent = '在上面的输入框填车号（如 350234），或搜本子名 / 作者名';
  }
}

/* 包装 renderGrid：空列表时套用视图提示 */
(function patchEmptyHint() {
  const prev = renderGrid;
  renderGrid = function () {
    prev();
    try {
      const emptyShown = $('#empty') && $('#empty').style.display !== 'none';
      if (emptyShown) setEmptyHint(S.view);
    } catch (e) { /* 忽略 */ }
  };
})();


/* ============================================================
   每个视图独立记住搜索框内容（互不干扰）
   ============================================================ */
S.qByView = S.qByView || {};

function setView(v) {
  try {
    // 1) 先把当前视图输入框内容存起来
    S.qByView = S.qByView || {};
    S.qByView[S.view] = ($('#q') || {}).value || '';
    // 2) 切视图
    S.view = v;
    document.querySelectorAll('.nav-item').forEach((b) =>
      b.classList.toggle('is-active', b.dataset.view === v));
    // 3) 恢复目标视图自己的关键词（没有就清空）
    const q = $('#q');
    if (q) q.value = S.qByView[v] || '';
    syncTopTools();
    loadView();
  } catch (e) {
    try { loadView(); } catch (e2) { /* 忽略 */ }
  }
}

/* 程序内切到某视图时（搜索/查询结果）也同步记录关键词 */
window.addEventListener('pywebviewready', () => {
  S.qByView = S.qByView || {};
  // 离开「已下载」时把筛选词留在它自己那一格里
  const q = $('#q');
  if (q) {
    q.addEventListener('blur', () => {
      S.qByView[S.view] = q.value || '';
    });
  }
});


/* 封面缺失时用"首字 + 渐变"占位（关注页/作者无本地作品时） */
(function patchCoverPlaceholder() {
  const prev = renderGrid;
  renderGrid = function () {
    prev();
    try {
      const cards = document.querySelectorAll('#grid .card');
      (S.items || []).forEach((it, i) => {
        const c = cards[i];
        if (!c || it.cover) return;
        const box = c.querySelector('.cover');
        if (!box || box.querySelector('.cover-ph')) return;
        const ph = document.createElement('div');
        ph.className = 'cover-ph';
        const label = (it.title || it.authorName || '?').trim().charAt(0);
        ph.textContent = label;
        box.appendChild(ph);
      });
    } catch (e) { /* 忽略 */ }
  };
})();


/* ============================================================
   网络与登录设置
   ============================================================ */
async function openNet() {
  const m = $('#netModal');
  if (!m) return;
  m.classList.add('on');
  $('#netStatus').className = 'net-status';
  $('#netStatus').textContent = '加载中…';
  try {
    const s = await api().net_get();
    $('#netProxy').value = s.proxy || '';
    $('#netDomain').value = s.domain || '';
    $('#netUser').value = s.username || '';
    $('#netPass').value = '';
    $('#netCookie').value = '';
    $('#netCookie').placeholder = s.cookie_count
      ? ('已保存 ' + s.cookie_count + ' 项：' + (s.cookie_preview || '') + '（粘贴新串可覆盖）')
      : 'AVS=...; yp_uid=...';
    const st = $('#netStatus');
    st.className = 'net-status' + (s.logged_in ? ' ok' : '');
    st.textContent = s.logged_in
      ? ('已登录（Cookie ' + s.cookie_count + ' 项）')
      : '未登录（未登录时，仅部分内容可见）';
  } catch (e) {
    $('#netStatus').textContent = '读取失败：' + e;
  }
}

function closeNet() {
  const m = $('#netModal');
  if (m) m.classList.remove('on');
}

async function saveNet() {
  const st = $('#netStatus');
  st.className = 'net-status';
  st.textContent = '保存中…（登录可能需要几秒）';
  const r = await api().net_save(
    $('#netProxy').value, $('#netDomain').value,
    $('#netUser').value, $('#netPass').value, $('#netCookie').value);
  st.className = 'net-status' + (r.ok ? ' ok' : ' err');
  st.textContent = r.msg || (r.ok ? '已保存' : '保存失败');
  toast(st.textContent, r.ok ? 'ok' : 'err');
  if (r.ok) { $('#netPass').value = ''; $('#netCookie').value = ''; }
}

async function testNet() {
  const st = $('#netStatus');
  st.className = 'net-status';
  st.textContent = '测试中…';
  const r = await api().net_test();
  st.className = 'net-status' + (r.ok ? ' ok' : ' err');
  st.textContent = r.msg || (r.ok ? '连接正常' : '连接失败');
  toast(st.textContent, r.ok ? 'ok' : 'err');
}

window.addEventListener('pywebviewready', () => {
  const btn = $('#netBtn');
  if (btn) btn.onclick = () => openNet();
  const c = $('#netClose');
  if (c) c.onclick = closeNet;
  const s = $('#netSave');
  if (s) s.onclick = saveNet;
  const t = $('#netTest');
  if (t) t.onclick = testNet;
  const m = $('#netModal');
  if (m) m.addEventListener('click', (e) => { if (e.target === m) closeNet(); });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeNet();
  });
});
