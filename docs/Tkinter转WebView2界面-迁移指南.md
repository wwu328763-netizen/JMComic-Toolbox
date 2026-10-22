# Tkinter → WebView2 界面迁移指南

> 这份文档是「JMComic 工具箱」把 Tkinter 界面重做成 WebView2 界面的完整经验总结，
> 目标是**你拿另一个 Python 程序也能照着一路改完**。
>
> 参考实现就在本包的 `webui/` 目录：`host.py`(宿主/桥) + `index.html` + `style.css` + `app.js`，
> 打包脚本 `webui/build_web.ps1`。

---

## 0. 先判断：要不要换

| 情况 | 建议 |
|---|---|
| 界面只是"能用就行"，重活都在命令行 | 不换，Tk 足够 |
| 被吐槽"丑/工科/不像软件"，且愿意花 1~3 天 | **换**，收益最大 |
| 需要圆角、阴影、渐变、真排版、流畅动画、复杂布局 | **必须换**，Tk 做不到 |
| 程序要在无 WebView2 的老系统（Win7）跑 | 换 Electron/Qt，别用 WebView2 |
| 只有 1~2 个窗口、逻辑简单 | 换的成本 ≈ 重写一遍 UI，值得但要有心理准备 |

**我们换完的收益**：封面墙/圆角/阴影/主题切换/60fps 动画/内置阅读器/热加载 CSS，
以及后续 UI 迭代从"改 Python 布局代码"变成"改 CSS"。

**成本**：新增 4 个文件（约 1000 行），Python 核心代码**一行没改**。

---

## 1. 总体架构（关键：三层分清）

```
┌─────────────────────────────────────────────┐
│  界面层  webui/index.html + style.css + app.js │  ← HTML/CSS/JS，跑在 WebView2
└───────────────▲─────────────────────────────┘
                │  window.pywebview.api.xxx(...)   （JS 调 Python，返回 Promise）
                │  window.evaluate_js("...")        （Python 调 JS，测试/推送用）
┌───────────────┴─────────────────────────────┐
│  桥接层  webui/host.py（Api 类 + 本地小服务）    │  ← 只做"转发 + 线程 + 错误包装"
└───────────────▲─────────────────────────────┘
                │  普通函数调用
┌───────────────┴─────────────────────────────┐
│  核心层  src/core.py（原程序里与界面无关的部分）  │  ← 下载/解析/配置/数据库
└─────────────────────────────────────────────┘
```

**改造的核心动作**：把原程序里"与界面无关的逻辑"抽出来（多数人其实已经分层了），
然后**用 HTML 重画界面**，桥接层做转发。

> 反例：不要试图在桥接层里复刻 Tk 的控件树，也不要让 JS 直接访问 Python 对象。

---

## 2. 前置检查（先确认环境能跑）

```powershell
# 1) WebView2 运行时（Win10/11 基本都自带）
Get-ItemProperty 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}' -Name pv

# 2) 装 pywebview（Windows 会连带装 pythonnet/clr-loader）
python -m pip install pywebview

# 3) 冒烟测试：能弹出窗口即 OK
python -c "import webview; webview.create_window('t','https://example.com'); webview.start()"
```

没有 WebView2 运行时 → 装 [Evergreen Runtime](https://developer.microsoft.com/microsoft-edge/webview2/)，
或改用 PySide6（QSS 也能做圆角，但动画/DOM 能力弱于 WebView2）。

---

## 3. 五步改造

### Step 1：抽核心层（最重要，也最枯燥）

目标：得到一个**不 import tkinter** 的模块。

```python
# src/core.py —— 从原程序里搬出来，去掉所有 tk/控件调用
def build_config(): ...         # 读配置
def list_items(): ...           # 取数据
def download(task_id, on_progress): ...   # 长任务：回调进度，而不是更新控件
def export(path, fmt): ...      # 业务动作，返回 (ok, msg, data)
```

**要点**
- 所有"更新界面"的地方改成**返回值**或 `on_progress(done, total)` 回调
- 所有 `messagebox.askyesno` 改成返回布尔值，由界面层弹确认框（Web 用 `confirm()`）
- 所有路径用 `os.path` 拼好，别在核心层里弹窗

> 我们原程序的 `jmapp.py` 里，`parse_ids / resolve_item / detail_info / search_page /
> export_album / trash_path / scan_downloads` 就是典型的核心层函数，
> 迁移时直接 `import jmapp` 复用，界面层一行不用重写。

### Step 2：写宿主 host.py（模板可直接抄）

```python
# -*- coding: utf-8 -*-
import json, os, sys, threading, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src'))
import core                      # 你的核心层
import webview

# ---------- 路径：源码运行 / PyInstaller 打包 两种都要对 ----------
def _dirs():
    if getattr(sys, 'frozen', False):                 # PyInstaller
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        bundle = getattr(sys, '_MEIPASS', exe_dir)    # 打包进去的资源在这
        for base in (bundle, exe_dir):
            ui = os.path.join(base, 'webui')
            if os.path.isfile(os.path.join(ui, 'index.html')):
                return exe_dir, ui
        return exe_dir, os.path.join(bundle, 'webui')
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return root, os.path.join(root, 'webui')

ROOT, UI_DIR = _dirs()
UI_FILE = os.path.join(UI_DIR, 'index.html')

# ---------- 全局状态：长任务进度靠轮询读它 ----------
ST = {'busy': False, 'status': '就绪', 'done': 0, 'total': 0, 'logs': []}
ST_LOCK = threading.Lock()

def log(msg):
    with ST_LOCK:
        ST['logs'].append({'t': time.strftime('%H:%M:%S'), 'm': str(msg)[:300]})
        del ST['logs'][:max(0, len(ST['logs']) - 300)]

# ---------- 桥接 API：每个方法都是 JS 可直接 await 的 ----------
class Api:
    def poll(self):                       # JS 每 200~300ms 调一次
        with ST_LOCK:
            return dict(ST)

    def list_items(self):
        return core.list_items()

    def start_task(self, arg):
        if ST['busy']:
            return {'ok': False, 'msg': '还有任务在跑'}
        threading.Thread(target=self._work, args=(arg,), daemon=True).start()
        return {'ok': True}

    def _work(self, arg):
        ST.update(busy=True, status='运行中', done=0, total=0)
        def on_progress(done, total):
            ST['done'], ST['total'] = done, total
            ST['status'] = f'{done}/{total}'
        try:
            core.download(arg, on_progress)
            ST['status'] = '完成'
        except Exception as e:
            ST['status'] = '失败'
            log(f'失败：{e}')
        finally:
            ST['busy'] = False

# ---------- 单实例（防止两个进程抢写配置文件） ----------
def _single_instance():
    if sys.platform != 'win32':
        return True
    import ctypes
    global _MUTEX
    _MUTEX = ctypes.windll.kernel32.CreateMutexW(None, False, 'Global\\MyApp_One')
    if ctypes.windll.kernel32.GetLastError() == 183:      # ERROR_ALREADY_EXISTS
        ctypes.windll.user32.MessageBoxW(None, '程序已经在运行了。', '提示', 0x40)
        return False
    return True

def main():
    if not _single_instance():
        return 0
    webview.create_window(
        '我的程序', url=UI_FILE, js_api=Api(),
        width=1200, height=800, min_size=(900, 600),
        background_color='#0f1117',
        text_select=False,
        zoomable=False,        # ★ 关掉 WebView2 的页面缩放（否则 Ctrl+滚轮会把整个界面放大）
    )
    webview.start(debug=False)
    return 0

if __name__ == '__main__':
    sys.exit(main())
```

### Step 3：写界面（HTML + CSS）

三段式结构，先搭骨架再美化作：

```html
<div id="app">
  <aside class="sidebar">…导航…</aside>
  <main class="main">
    <header class="topbar">…搜索/按钮…</header>
    <section class="content" id="content">…列表/卡片…</section>
    <footer class="statusbar"><div class="progress"><i id="bar"></i></div>…状态…</footer>
  </main>
</div>
```

**CSS 先立"设计 token"（这是好不好看的分水岭）**：

```css
:root{
  --bg:#0f1117; --panel:#151922; --card:#171b24; --line:#252b38;
  --text:#e8ecf4; --muted:#8791a5; --accent:#4c8dff;
  --radius:14px; --shadow:0 12px 30px rgba(0,0,0,.45);
  --gap:14px;
}
/* 主题 = 换一组变量，界面零改动 */
body[data-theme="light"]{ --bg:#eef1f6; --panel:#fff; --text:#131a26; --accent:#2f6fed; }
body[data-theme="neon"] { --bg:#0a0713; --panel:#130d24; --text:#ece7ff; --accent:#b06bff; }
```

**把 Tk 的"控件"换成 HTML 对应物**：

| Tkinter | HTML/CSS |
|---|---|
| `tk.Frame` | `<div>` + `display:flex/grid` |
| `tk.Label` | `<span>`/`<div>` |
| `tk.Entry` / `StringVar` | `<input>`，值放 JS `state` 对象里 |
| `tk.Button` | `<button>` + `:hover/:active` |
| `ttk.Treeview` | `<div class="grid">` + 卡片，或 `<table>` |
| `Canvas` + 滚动 | `<div style="overflow:auto">` |
| `ttk.Progressbar` | `<div class="progress"><i style="width:42%">` |
| `messagebox.askyesno` | `confirm()` |
| `PhotoImage` | `<img src="...">` |
| `after(80, loop)` | `setInterval(fn, 260)` |
| `widget.bind('<Button-1>')` | `el.addEventListener('click')` |
| `place/pack/grid` | flex / grid / absolute |
| 主题切换（重建控件） | **改 CSS 变量一行**（`document.body.dataset.theme = 'light'`） |

> 圆角/阴影/渐变全是 CSS 一句话；Tk 里这些要么做不到、要么要画 Canvas。

### Step 4：写 app.js（状态 + 渲染 + 桥）

```js
const $ = (s) => document.querySelector(s);
const api = () => window.pywebview.api;      // ★ 桥必须等 pywebviewready 再调

const S = { view:'home', items:[], theme:'dark' };

function render(list){                        // 只做 DOM 生成，不写业务
  const box = $('#content'); box.innerHTML = '';
  list.forEach(it=>{
    const card = document.createElement('button');
    card.className = 'card';
    card.innerHTML = `<img src="${it.cover}"><b>${it.title}</b><span>${it.meta}</span>`;
    card.onclick = ()=> openDetail(it);
    box.appendChild(card);
  });
}

async function loadView(){
  const data = await api().list_items();      // 调桥
  S.items = data; render(data);
}

async function startTask(){
  const r = await api().start_task($('#q').value);
  if(!r.ok) alert(r.msg);
}

/* 进度：轮询（不要用长连接/回调，最稳） */
setInterval(async ()=>{
  const st = await api().poll();
  $('#bar').style.width = st.total ? (st.done*100/st.total)+'%' : '0%';
  $('#status').textContent = st.status;
  // 日志抽屉
  $('#log').innerHTML = st.logs.slice(-60).map(x=>`<div>${x.t} ${x.m}</div>`).join('');
}, 260);

/* ★ 必须等桥就绪，否则 window.pywebview 是 undefined */
window.addEventListener('pywebviewready', async ()=>{
  bindEvents();
  const cfg = await api().settings();
  document.body.dataset.theme = cfg.theme || 'dark';
  await loadView();
});
```

**桥的约定（很重要，否则以后很难维护）**
1. 所有方法**返回可 JSON 序列化的 dict**，统一形状 `{'ok': bool, 'msg': str, ...}`
2. 长任务**绝不阻塞**：起线程，立即返回 `{'ok':True}`，进度走 `poll()`
3. 不要传 Python 对象给 JS；传 id/基本类型
4. 异常在 Python 侧 catch 成 `{'ok':False,'msg':...}`，别让 Promise reject

### Step 5：打包成单文件 exe

```powershell
python -m PyInstaller --noconfirm --onefile --windowed `
  --name MyApp `
  --icon app.ico `
  --add-data "webui;webui" `                  # ★ 把界面资源打进去
  --paths src `                               # ★ 让核心层模块被打包
  --hidden-import webview.platforms.edgechromium `
  --hidden-import webview.platforms.winforms `
  --hidden-import clr `
  --collect-all webview `
  --collect-all pythonnet `
  --collect-all clr_loader `
  --distpath . --workpath "$env:TEMP\build" --specpath "$env:TEMP\build" `
  webui\host.py
```

产物 ≈ 25~35 MB（不含 WebView2 运行时，用的是系统的）。

---

## 4. 图片/文件怎么给前端看

不要用 base64 塞进 JSON（大图会卡死），起一个**本地只读 HTTP 服务**：

```python
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote
import mimetypes

class ImgHandler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        q = parse_qs(urlparse(self.path).query)
        path = unquote((q.get('p') or [''])[0])
        # ★ 白名单：只允许自己的数据目录
        real = os.path.realpath(path)
        if not any(real.lower().startswith(os.path.realpath(d).lower())
                   for d in self.server.allowed):
            return self.send_error(403)
        data = open(real, 'rb').read()
        self.send_response(200)
        self.send_header('Content-Type', mimetypes.guess_type(real)[0] or 'application/octet-stream')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers(); self.wfile.write(data)

srv = ThreadingHTTPServer(('127.0.0.1', 0), ImgHandler)   # 0 = 随机空闲端口
srv.allowed = [DATA_DIR, CACHE_DIR]
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE_URL = f'http://127.0.0.1:{srv.server_address[1]}'
# JS 里： <img src="{BASE_URL}/img?p={encodeURIComponent(path)}">
```

要点：**只监听 127.0.0.1**、随机端口、路径白名单——不要直接把整个磁盘暴露出去。

---

## 5. 我们踩过的坑（照抄可省几天）

| # | 现象 | 原因 | 修法 |
|---|---|---|---|
| 1 | 打包后窗口一片空白 / 404 | 冻结后 `__file__` 不在资源目录 | 用 `sys._MEIPASS` 找 `webui/`（见 host 模板 `_dirs()`） |
| 2 | 所有按钮都失效 | JS 有语法错误，整段脚本不执行 | 改完 JS 必须**实际跑一次**（`evaluate_js` 取个值验证），别只看代码 |
| 3 | 配置读不到、主题老回默认 | 配置文件被记事本/PowerShell 写成 **UTF-8 BOM**，`json.load` 抛错 | 读写都用 `encoding='utf-8-sig'`；写文件用无 BOM |
| 4 | Ctrl+滚轮 把整个界面放大了 | WebView2 自带页面缩放快捷键 | `create_window(..., zoomable=False)`，并在 JS 里自己实现图片缩放 |
| 5 | 改 `document.title` 窗口标题不变 | pywebview 不同步 document.title | 标题用 `window.set_title()`/创建参数；拿不到就写文件回传 |
| 6 | 长任务让界面"假死" | 在桥方法里同步跑耗时逻辑 | 起线程 + `poll()` 轮询 |
| 7 | 下载/统计请求很慢，UI 像卡住 | 串行 N 个网络请求 | 分批（每批 4~6）+ 并发 2~4 + 每批回来就局部刷新 |
| 8 | 并发请求大量失败且无提示 | 站点限流 + 异常被静默吞掉 | 失败**自动重试 2~3 次**（递增间隔），并把错误写进日志面板 |
| 9 | 两个实例同时写配置文件，数据互相覆盖 | 没有单实例保护 | Windows 命名互斥量（见 `_single_instance()`） |
| 10 | Tk 的 `WM_MOUSEWHEEL` 绑不上 | Windows 把滚轮发给**焦点窗口** | Web 端没这问题；如果在 Tk 里修，绑到 Toplevel 并按光标位置分发 |
| 11 | 中文乱码 | 控制台/脚本编码不一致 | Python 侧 `PYTHONUTF8=1`；`.cmd` 文件里只写 ASCII；`.ps1` 用 UTF-8 **带 BOM** |
| 12 | 用户双击两次开了两个窗口 | 同 9 | 同 9 |

---

## 6. 迁移验收清单

- [ ] 核心层 `import` 时不再依赖 tkinter
- [ ] `host.py` 能在源码模式下跑起来（`python webui/host.py`）
- [ ] 界面：列表/详情/主要动作全部可用
- [ ] 长任务有进度、可取消；失败有可见错误提示
- [ ] 主题切换正常（改 CSS 变量，不重建界面）
- [ ] 打包成 exe 后：资源路径、配置读取、单实例都正常
- [ ] 每个桥方法都有 `try/except → {'ok':False,'msg':…}`
- [ ] 本地 HTTP 服务只监听 127.0.0.1 且有路径白名单
- [ ] 旧界面保留一个版本做备份，确认新界面稳定后再删

---

## 7. 迁移成本参考

| 原程序规模 | 预计工时 |
|---|---|
| 单窗口、5~10 个动作 | 0.5~1 天 |
| 2~3 个窗口、列表+详情+进度 | 1~2 天 |
| 含阅读器/编辑器/复杂自定义控件 | 3~5 天 |

**最耗时的不是写 HTML，而是把"界面逻辑"和"业务逻辑"拆开**——
如果我们这次的 `jmapp.py` 不是一开始就分好了核心函数，迁移会多花一倍时间。

---

## 8. 参考文件

| 文件 | 内容 |
|---|---|
| `webui/host.py` | 宿主 + 桥接 + 本地图片服务 + 单实例 + 测试钩子 |
| `webui/index.html` | 三栏布局、阅读器、详情抽屉、toast |
| `webui/style.css` | 设计 token、5 套主题变量、卡片/按钮/动画 |
| `webui/app.js` | 状态管理、渲染、桥调用、轮询、视图记忆 |
| `webui/build_web.ps1` | PyInstaller 一键打包 |

> 想直接抄一个能跑的最小样例：把 `webui/` 拷走，删掉 `app.js` 里业务相关函数、
> 保留 `poll / render / 主题 / toast` 这几块，再把 `host.py` 里 `Api` 的方法换成你自己的即可。
