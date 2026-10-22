# -*- coding: utf-8 -*-
"""WebView2 界面最小模板（可复制改成你自己的程序）

演示了迁移指南里的全部关键点：
  · 三层架构：本文件（宿主/桥）→ core_ops（你的业务）→ webui/（界面）
  · _MEIPASS 路径处理（源码运行 / PyInstaller 打包都能用）
  · js_api 桥：返回 {'ok': bool, 'msg': str, ...}
  · 长任务：起线程 + poll() 轮询进度（界面不卡）
  · 单实例互斥
  · zoomable=False（否则 Ctrl+滚轮会缩放整个界面）

运行：
  python docs\webui-template\host.py
打包：
  见 迁移指南 Step 5，把 --add-data 指到本目录的 webui
"""
import os
import sys
import threading
import time

import webview


# ---------------------------------------------------------------- 路径
def _dirs():
    if getattr(sys, 'frozen', False):
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        bundle = getattr(sys, '_MEIPASS', exe_dir)
        for base in (bundle, exe_dir):
            ui = os.path.join(base, 'webui')
            if os.path.isfile(os.path.join(ui, 'index.html')):
                return exe_dir, ui
        return exe_dir, os.path.join(bundle, 'webui')
    here = os.path.dirname(os.path.abspath(__file__))
    return here, os.path.join(here, 'webui')


ROOT, UI_DIR = _dirs()
UI_FILE = os.path.join(UI_DIR, 'index.html')


# ---------------------------------------------------------------- 业务层（你的代码放这里）
ITEMS = [
    {'id': 1, 'title': '示例任务一', 'done': False},
    {'id': 2, 'title': '示例任务二', 'done': True},
]


def core_list():
    return list(ITEMS)


def core_run(task_id, on_progress):
    """模拟耗时任务：每 0.15 秒报一次进度"""
    total = 20
    for i in range(1, total + 1):
        time.sleep(0.15)
        on_progress(i, total)
    return f'任务 {task_id} 完成'


# ---------------------------------------------------------------- 状态
LOCK = threading.Lock()
ST = {'busy': False, 'status': '就绪', 'done': 0, 'total': 0, 'logs': []}


def log(msg):
    with LOCK:
        ST['logs'].append({'t': time.strftime('%H:%M:%S'), 'm': str(msg)[:300]})
        del ST['logs'][:max(0, len(ST['logs']) - 200)]


# ---------------------------------------------------------------- 桥
class Api:
    def settings(self):
        return {'theme': 'dark', 'title': '迁移模板示例', 'root': ROOT}

    def poll(self):
        with LOCK:
            return dict(ST)

    def list_items(self):
        return core_list()

    def add_item(self, title):
        title = (title or '').strip()
        if not title:
            return {'ok': False, 'msg': '内容不能为空'}
        ITEMS.append({'id': len(ITEMS) + 1, 'title': title, 'done': False})
        log(f'新增：{title}')
        return {'ok': True}

    def run_task(self, task_id):
        with LOCK:
            if ST['busy']:
                return {'ok': False, 'msg': '还有任务在跑'}
            ST.update(busy=True, status='运行中', done=0, total=0)
        threading.Thread(target=self._work, args=(task_id,), daemon=True).start()
        return {'ok': True}

    def _work(self, task_id):
        def on_progress(done, total):
            ST['done'], ST['total'] = done, total
            ST['status'] = f'运行中 {done}/{total}'

        try:
            msg = core_run(task_id, on_progress)
            ST['status'] = '完成'
            log(msg)
        except Exception as e:
            ST['status'] = '失败'
            log(f'失败：{e}')
        finally:
            ST['busy'] = False


# ---------------------------------------------------------------- 单实例
_MUTEX = None


def _single_instance():
    global _MUTEX
    if sys.platform != 'win32':
        return True
    import ctypes
    _MUTEX = ctypes.windll.kernel32.CreateMutexW(None, False, 'Global\\WebTemplateDemo_One')
    if ctypes.windll.kernel32.GetLastError() == 183:
        ctypes.windll.user32.MessageBoxW(None, '程序已经在运行了。', '提示', 0x40)
        return False
    return True


def main():
    if not _single_instance():
        return 0
    webview.create_window(
        '迁移模板示例', url=UI_FILE, js_api=Api(),
        width=1000, height=700, min_size=(760, 520),
        background_color='#0f1117',
        text_select=False,
        zoomable=False,          # ★ 关掉 WebView2 页面缩放
    )
    webview.start(debug=False)
    return 0


if __name__ == '__main__':
    sys.exit(main())
