# 交给执行 AI 的任务说明书（Tkinter → WebView2 界面迁移）

> 用法：把 **A. 初始 Prompt** 整段复制给执行 AI；
> 它需要同时能读到本包里的 `docs/Tkinter转WebView2界面-迁移指南.md` 和 `docs/webui-template/`。
> **B~E** 是给它自己对照的执行规范，也是你验收的依据。

---

## A. 初始 Prompt（整段复制给执行 AI）

```
任务：把下面这个 Python 桌面程序的 Tkinter 界面，改造成 WebView2 界面（pywebview + HTML/CSS/JS），
      业务逻辑/数据格式/配置文件格式一律保持不变。

【必读材料（在本项目目录下）】
1. docs/Tkinter转WebView2界面-迁移指南.md   ← 方法论 + 全部坑，先通读
2. docs/webui-template/                      ← 可直接运行的最小模板，照它的结构抄

【硬性约束】
1. 不改业务逻辑：核心层函数（下载/解析/存储/网络）行为与返回结构保持原样；
   只允许把"更新界面"的代码改成 return 值或 on_progress(done,total) 回调。
2. 新旧并存：保留原 Tk 入口（例如 main_tk.py 或旧 exe），新界面独立入口，验收后再由用户决定删旧版。
3. 不引入前端框架/构建工具：纯 HTML+CSS+原生 JS，零 npm 依赖。
4. 所有桥方法返回可 JSON 序列化的 dict，形状统一 {'ok': bool, 'msg': str, ...}；
   长任务必须异步（线程 + poll 轮询），绝不能阻塞桥调用。
5. 本地图片/文件通过 127.0.0.1 临时 HTTP 服务提供，必须带路径白名单，不能暴露任意磁盘路径。
6. 打包用 PyInstaller --onefile --windowed，必须 --add-data 界面目录、--paths 核心目录、
   --hidden-import webview.platforms.edgechromium / winforms / clr。
7. 每完成一个阶段，必须实际运行程序验证（不能只说"改好了"），并给出证据。

【执行步骤】
阶段0 调研：列出原程序的 ①窗口/页面清单 ②每个页面的动作 ③哪些是业务逻辑、哪些是界面逻辑；
        输出一份"改造范围表"给我确认。
阶段1 抽核心层：确保有一个不 import tkinter 的模块可复用；跑通一个 CLI 冒烟测试。
阶段2 搭骨架：基于 docs/webui-template 生成 host.py + webui/{index.html,style.css,app.js}，
        先只做"列表 + 一个动作 + 进度"，跑起来截图给我看。
阶段3 逐页迁移：按阶段0的表，一页一页搬；每页完成后自测并记录。
阶段4 打包：PyInstaller 出单文件 exe，双击验证资源加载/配置读取/单实例。
阶段5 交付：给出改动文件清单、回滚方法、自测记录、已知问题。

【每阶段的验证要求（缺一不可）】
- 能启动：窗口正常显示，无空白页 / 404
- JS 真执行：用 window.evaluate_js 取一个值回来（不要只看代码），确认脚本无语法错误
- 桥连通：每个按钮点一次，观察状态栏/日志有对应输出
- 长任务：进度条动、可取消、失败有可见提示
- 截图：每个阶段给 1 张关键界面截图

【禁止事项】
- 禁止把图片转 base64 塞进 JSON
- 禁止在桥方法里同步跑耗时操作
- 禁止改动用户数据文件格式（配置/数据库/history 等）
- 禁止删除旧界面代码（只能新增 + 在验收后由用户批准再删）
- 禁止用 --noconsole 之外的方式隐藏错误：打包版也必须能看日志（写文件）

【交付格式】
按阶段汇报，每阶段包含：改了什么文件 / 怎么验证的 / 证据（截图或命令输出）/ 遗留问题。
最后给一份 README_新界面.md（启动方式、打包命令、已知限制）。
```

---

## B. 给执行 AI 的作业指导（对照用）

### 阶段 0：调研表（必须先出这张表，别直接写代码）

| 页面/窗口 | 入口 | 用到的动作 | 数据来源 | 涉及的核心函数 | 迁移难度 |
|---|---|---|---|---|---|
| 例：主窗口 | main.py:120 | 输入车号、下载、查历史 | config.json | core.query / core.download | 中 |
| 例：阅读器窗口 | reader.py | 翻页、缩放、切换章节 | 下载目录 | core.list_pages | 高（建议单独排期） |

**完成标志**：表里每个动作都能指到具体函数；用户确认无遗漏。

### 阶段 1：核心层可用

```powershell
# 核心层不能依赖界面库
python -c "import src.core as c; print(c.list_items()[:1])"
# 最好再跑一个 CLI 冒烟：不启动界面也能完成一次业务动作
```

**完成标志**：`import core` 不触发任何窗口；一个"取数据 + 执行一个动作"的命令行测试通过。

### 阶段 2：骨架跑通

照 `docs/webui-template/` 复制成 `webui/`，只保留：列表渲染 + 一个动作 + 进度 + 日志。
把 `host.py` 里的 `core_list / core_run` 换成真实函数。

```powershell
python webui\host.py        # 源码模式
```

**完成标志**：窗口能开、列表有真实数据、点动作按钮进度条会动；截图。

### 阶段 3：逐页迁移

顺序建议：**只读列表页 → 详情页 → 带动作的页 → 复杂自定义控件（阅读器/编辑器）**。
每页完成后立刻自测，别攒到最后。

### 阶段 4：打包

```powershell
python -m PyInstaller --noconfirm --onefile --windowed `
  --name MyApp --icon app.ico `
  --add-data "webui;webui" --paths src `
  --hidden-import webview.platforms.edgechromium `
  --hidden-import webview.platforms.winforms `
  --hidden-import clr `
  --collect-all webview --collect-all pythonnet --collect-all clr_loader `
  --distpath . --workpath "$env:TEMP\b" --specpath "$env:TEMP\b" `
  webui\host.py
```

**完成标志**：把 exe 拷到一个**空目录**（只带配置文件）双击能跑起来——这一步最容易暴露 `_MEIPASS` 路径问题。

### 阶段 5：交付

必须包含：
1. 新增/修改文件清单（含行数概览）
2. 回滚方法（git 或备份目录）
3. 自测记录（每个验收项的通过证据）
4. 已知限制（哪些功能降级、哪些没做）
5. `README_新界面.md`

---

## C. 你（人类）的验收清单

- [ ] 新界面能启动，**没有空白页/404**
- [ ] 旧界面还能启动（未被破坏）
- [ ] 每个按钮点一遍，都有反应（没反应 = JS 语法错或桥没绑上）
- [ ] 长任务：进度在动、能取消、失败时能看到原因（不是静默）
- [ ] 关掉重开，配置/数据都还在（没被写成空/损坏）
- [ ] exe 拷到空目录双击能用（资源打包正确）
- [ ] 主题切换、窗口缩放不破版
- [ ] 数据文件格式与旧版一致（新界面写的，旧界面能读；反之亦然）

---

## D. 常见失败对照表（执行 AI 遇到时按这张查）

| 现象 | 最可能原因 | 立即检查 |
|---|---|---|
| 白屏 / `404 index.html` | 打包后路径没走 `_MEIPASS` | 打印 `UI_FILE` 是否指向真实文件 |
| 窗口出来了，**所有按钮都没反应** | `app.js` 有语法错误，整段脚本没执行 | `evaluate_js("1+1")` 能否返回 2；`window.pywebview` 是否为 undefined |
| 桥方法报 `Promise` 被 reject | 桥方法抛异常 / 返回了不可序列化对象 | 把桥方法整个包 `try/except`，返回 `{'ok':False,'msg':...}` |
| 配置读不到、总回默认值 | JSON 被写成 **UTF-8 BOM** | 读用 `encoding='utf-8-sig'` |
| Ctrl+滚轮把整个界面放大 | WebView2 页面缩放 | `create_window(..., zoomable=False)` |
| 界面卡死不刷新 | 桥方法里同步执行耗时逻辑 | 改线程 + `poll()` |
| 大量请求失败且无提示 | 站点/接口限流，异常被静默吞 | 加重试（2~3 次递增）+ 错误写进日志面板 |
| 两个窗口同时开、数据互相覆盖 | 无单实例保护 | Windows 命名互斥量 |
| 打包后图片/字体加载失败 | 静态资源没 `--add-data`，或路径写死 | 用相对路径 + `_MEIPASS` |

---

## E. 需要执行 AI 回传给我的东西

1. 阶段 0 的**改造范围表**
2. 阶段 2 的**骨架截图**（先确认风格方向，别一路做完才发现方向不对）
3. 每阶段的自测**命令输出**（不要只说"通过了"）
4. 最后：`README_新界面.md` + 改动文件清单 + 已知问题

---

## F. 附：我们这次实际踩坑记录（可直接引用）

1. **打包后 404**：`__file__` 在冻结后不在资源目录 → 用 `sys._MEIPASS`。
2. **JS 语法错导致全部按钮失效**：一次补丁多留了 5 行 `});`，括号不平衡；窗口能开但所有交互死。
   → 每次改完 JS 必须 `evaluate_js` 实测一次。
3. **主题总回默认**：PowerShell `Set-Content -Encoding UTF8` 写了 BOM，`json.load` 抛错被吞。
4. **Ctrl+滚轮 缩放整个界面**：WebView2 自带页面缩放 → `zoomable=False`。
5. **长任务界面假死**：桥方法同步跑 → 线程 + 轮询。
6. **搜索补全很慢**：串行 N 个网络请求 → 并发 2~4 + 分批回填 + 重试。
7. **两个实例互相覆盖配置**：无单实例 → 命名互斥量。
8. **文档标题改了但窗口标题不变**：pywebview 不同步 `document.title` → 用 `window.set_title()` 或写文件回传。

---

> 这份说明书和 `docs/Tkinter转WebView2界面-迁移指南.md` 一起用：
> **指南讲"怎么做"，本文件讲"交给 AI 时怎么约束和验收"。**
