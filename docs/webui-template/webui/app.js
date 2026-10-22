/* 最小模板：状态 + 渲染 + 桥调用 + 轮询 + toast
   改造成你的程序时，只需要动 loadList()/addItem()/runTask() 里调用的桥方法名 */
'use strict';

const $ = (s) => document.querySelector(s);
const api = () => window.pywebview.api;

const S = { items: [], theme: 'dark' };

/* ---------------- 渲染 ---------------- */
function render() {
  const box = $('#list');
  box.innerHTML = '';
  const items = S.items || [];
  $('#empty').style.display = items.length ? 'none' : 'block';
  items.forEach((it, i) => {
    const el = document.createElement('div');
    el.className = 'item';
    el.style.animationDelay = Math.min(i, 12) * 20 + 'ms';
    el.innerHTML = `
      <span class="dot" style="${it.done ? 'background:#35c46a' : ''}"></span>
      <span class="t">${esc(it.title)}</span>
      <span class="s">${it.done ? '已完成' : '待处理'}</span>`;
    el.onclick = () => runTask(it.id);
    box.appendChild(el);
  });
}

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function toast(msg, kind) {
  const el = document.createElement('div');
  el.className = 'toast' + (kind === 'err' ? ' err' : '');
  el.textContent = msg;
  $('#toasts').appendChild(el);
  setTimeout(() => { el.classList.add('out'); setTimeout(() => el.remove(), 260); }, 2200);
}

/* ---------------- 桥调用 ---------------- */
async function loadList() {
  S.items = (await api().list_items()) || [];
  render();
}

async function addItem() {
  const q = $('#q');
  const v = (q.value || '').trim();
  if (!v) { toast('先输入内容', 'err'); return; }
  const r = await api().add_item(v);
  if (!r.ok) { toast(r.msg || '失败', 'err'); return; }
  q.value = '';
  toast('已新增');
  await loadList();
}

async function runTask(id) {
  const r = await api().run_task(id || 1);
  if (!r.ok) toast(r.msg || '失败', 'err');
  else toast('任务已开始');
}

/* ---------------- 轮询进度（关键：不阻塞界面） ---------------- */
async function poll() {
  try {
    const st = await api().poll();
    const pct = st.total ? Math.round(st.done * 100 / st.total) : 0;
    $('#bar').style.width = pct + '%';
    $('#status').textContent = st.status || '就绪';
    if (st.logs && st.logs.length) {
      const box = $('#logList');
      box.innerHTML = st.logs.slice(-50)
        .map((l) => `<div>${esc(l.t)} ${esc(l.m)}</div>`).join('');
      box.scrollTop = box.scrollHeight;
    }
  } catch (e) { /* 桥未就绪 */ }
}

/* ---------------- 事件 ---------------- */
function bind() {
  $('#addBtn').onclick = addItem;
  $('#runBtn').onclick = () => runTask(1);
  $('#q').addEventListener('keydown', (e) => { if (e.key === 'Enter') addItem(); });
  $('#logBtn').onclick = () => {
    const on = $('#logPanel').classList.toggle('on');
    $('#logBtn').textContent = on ? '日志 ▴' : '日志 ▾';
  };
  $('#themeBtn').onclick = () => {
    S.theme = S.theme === 'dark' ? 'light' : 'dark';
    document.body.dataset.theme = S.theme;
    toast('主题：' + (S.theme === 'dark' ? '深色' : '浅色'));
  };
}

/* ---------------- 启动（必须等桥就绪） ---------------- */
window.addEventListener('pywebviewready', async () => {
  bind();
  try {
    const cfg = await api().settings();
    S.theme = cfg.theme || 'dark';
    document.body.dataset.theme = S.theme;
  } catch (e) { /* 忽略 */ }
  await loadList();
  setInterval(poll, 250);
});
