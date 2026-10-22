# -*- coding: utf-8 -*-
"""JMComic 工具箱 —— WebView2 界面宿主

界面层用 HTML/CSS/JS（webui/），内核仍复用 jmapp.py 的 jmcomic 能力：
查询 / 下载 / 取消 / 搜索 / 已下载扫描 / 导出 PDF·ZIP / 删除到回收站。

为什么不用 Tkinter：Tk 画不出圆角、阴影、真排版和合成动画，
再调色也只是给传统控件上色。WebView2 渲染层能做出真正的现代界面。
"""
import json
import mimetypes
import os
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

def _dirs():
    """源码运行 / PyInstaller 打包 两种情况的资源路径"""
    if getattr(sys, 'frozen', False):
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        bundle = getattr(sys, '_MEIPASS', exe_dir)
        for base in (bundle, exe_dir):
            ui = os.path.join(base, 'webui')
            if os.path.isfile(os.path.join(ui, 'index.html')):
                return exe_dir, ui
        return exe_dir, os.path.join(bundle, 'webui')
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return root, os.path.join(root, 'webui')


ROOT, UI_DIR = _dirs()
UI_FILE = os.path.join(UI_DIR, 'index.html')
if not getattr(sys, 'frozen', False):
    sys.path.insert(0, os.path.join(ROOT, 'src'))

import jmapp  # noqa: E402

try:
    import webview  # noqa: E402
except Exception:  # pragma: no cover
    webview = None




# ---------------------------------------------------------------- 状态

class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.busy = False
        self.task = ''
        self.status = '就绪'
        self.done = 0
        self.total = 0
        self.logs = []
        self.finished_at = 0

    def log(self, msg, kind=''):
        with self.lock:
            self.logs.append({'t': time.strftime('%H:%M:%S'),
                              'm': str(msg)[:300], 'k': kind or ''})
            if len(self.logs) > 400:
                del self.logs[:200]

    def snap(self):
        with self.lock:
            return {'busy': self.busy, 'task': self.task, 'status': self.status,
                    'done': self.done, 'total': self.total,
                    'logs': list(self.logs[-80:]), 'finishedAt': self.finished_at}

    def start(self, task, status=''):
        with self.lock:
            self.busy, self.task, self.done, self.total = True, task, 0, 0
            self.status = status or '开始 …'

    def finish(self, status='就绪'):
        with self.lock:
            self.busy, self.task = False, ''
            self.status = status
            self.finished_at = time.time()


ST = State()
_DETAIL_CACHE = {}   # 车号 -> (时间戳, detail_info)


# ---- 下载队列（FIFO，单线程串行执行）----
_Q = []
_QLock = threading.Lock()
_QBUSY = {'alive': False, 'thread': None}
QINFO = {'current': ''}


def _q_pending():
    with _QLock:
        return list(_Q)

_CANCEL = threading.Event()
_HOOK_INSTALLED = [False]


def install_log_hook():
    """把 jmcomic 的日志接到界面（含每张图的进度 + 取消）"""
    if _HOOK_INSTALLED[0]:
        return
    from jmcomic.jm_config import JmModuleConfig

    skip = {'api', 'api.setting', 'api.update_domain.success',
            'image.before', 'image.after', 'image.skip'}

    def hook(topic, msg, e=None):
        if _CANCEL.is_set():
            raise jmapp.Cancelled()
        if topic == 'image.after':
            ST.done += 1
            return
        if topic in skip:
            return
        bad = e is not None or 'failed' in topic or 'exception' in topic
        ST.log(msg, 'err' if bad else '')

    JmModuleConfig.EXECUTOR_LOG = hook
    _HOOK_INSTALLED[0] = True


# ---------------------------------------------------------------- 本地图片服务

class _ImgHandler(BaseHTTPRequestHandler):
    server_version = 'jmcomic-img/1.0'

    def log_message(self, *a):
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path not in ('/img', '/cover'):
            self.send_error(404)
            return
        q = parse_qs(parsed.query)
        path = unquote((q.get('p') or [''])[0])
        if not path or not os.path.isfile(path):
            self.send_error(404)
            return
        # 只允许下载目录 / 封面缓存目录下的文件
        try:
            real = os.path.realpath(path)
            allowed = [os.path.realpath(self.server.base_dir or ROOT),
                       os.path.realpath(jmapp.COVER_DIR),
                       os.path.realpath(jmapp.export_dir(self.server.base_dir or ROOT))]
            if not any(real.lower().startswith(a.lower()) for a in allowed):
                self.send_error(403)
                return
            ctype = mimetypes.guess_type(real)[0] or 'application/octet-stream'
            with open(real, 'rb') as f:
                data = f.read()
        except Exception:
            self.send_error(500)
            return
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'max-age=600')
        self.end_headers()
        try:
            self.wfile.write(data)
        except Exception:
            pass


def start_image_server(base_dir):
    srv = ThreadingHTTPServer(('127.0.0.1', 0), _ImgHandler)
    srv.base_dir = base_dir
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f'http://127.0.0.1:{srv.server_address[1]}'


# ---------------------------------------------------------------- 数据组装

def _cover_path_for(album_id, dir_path, first_img):
    if album_id:
        p = os.path.join(jmapp.COVER_DIR, f'{album_id}.jpg')
        if os.path.isfile(p) and os.path.getsize(p) > 1024:
            return p
    if first_img and os.path.isfile(first_img):
        return first_img
    return ''


def _img_url(server_url, path):
    if not path:
        return ''
    return f'{server_url}/img?p={__import__("urllib.parse", fromlist=["quote"]).quote(path)}'


def _local_items(server_url, base_dir):
    out = []
    try:
        entries = jmapp.scan_downloads(base_dir, jmapp.load_history())
    except Exception:
        entries = []
    for e in entries:
        cover = _cover_path_for(str(e.get('id') or ''), e.get('dir'), e.get('_thumb'))
        out.append({
            'key': e.get('key'), 'id': e.get('id') or '',
            'title': e.get('title') or os.path.basename(e.get('dir') or ''),
            'authors': e.get('authors') or '',
            'pages': e.get('pages') or 0, 'chapters': e.get('chapters') or 0,
            'tags': e.get('tags') or '',
            'dir': e.get('dir') or '',
            'cover': _img_url(server_url, cover),
            'meta': f"{e.get('pages') or 0} 页 · {e.get('chapters') or 0} 话 · 本地",
            'badge': (f"JM{e.get('id')}" if e.get('id') else ''),
        })
    return out


def _history_items(server_url):
    out = []
    for it in jmapp.load_history():
        cover = _cover_path_for(str(it.get('id') or ''), it.get('dir'), '')
        out.append({
            'key': it.get('key') or f"history:{it.get('id')}",
            'id': it.get('id') or '',
            'title': it.get('title') or '',
            'authors': it.get('authors') or '',
            'pages': it.get('pages') or 0, 'chapters': it.get('chapters') or 0,
            'tags': it.get('tags') or '',
            'dir': it.get('dir') or '',
            'cover': _img_url(server_url, cover),
            'meta': f"{it.get('action') or '查询'} · {time.strftime('%m-%d %H:%M', time.localtime(it.get('ts') or 0))}",
            'badge': (f"JM{it.get('id')}" if it.get('id') else ''),
        })
    return out


def _parse_cookie(text):
    """把浏览器复制的 Cookie 串解析成 dict"""
    out = {}
    if not text:
        return out
    raw = str(text).replace('\n', ';')
    for part in raw.split(';'):
        part = part.strip()
        if not part or '=' not in part:
            continue
        k, v = part.split('=', 1)
        k = k.strip().strip('"').strip("'")
        v = v.strip().strip('"').strip("'")
        if k and len(k) < 64:
            out[k] = v
    return out


def _meta_text(pages, chapters, authors, tags, limit_tags=36):
    bits = []
    if pages:
        bits.append('%d 页' % pages)
    if chapters:
        bits.append('%d 话' % chapters)
    if authors:
        bits.append(str(authors))
    if not pages and not chapters:
        t = str(tags or '').strip()
        if t and t != '—':
            bits.append(t[:limit_tags])
    return ' · '.join(bits)


def _num_or_none(v):
    try:
        n = int(v)
        return n if n > 0 else None
    except Exception:
        return None


def _remote_item(server_url, info, cover_path=''):
    """搜索结果没有页数/话数（是 '—'），卡片信息改用作者/分类，别显示 '—页'"""
    pages = _num_or_none(info.get('pages'))
    chapters = _num_or_none(info.get('chapters'))
    bits = []
    if pages:
        bits.append('%d 页' % pages)
    if chapters:
        bits.append('%d 话' % chapters)
    if not bits:
        if info.get('authors'):
            bits.append(str(info['authors']))
        tags = str(info.get('tags') or '').strip()
        if tags and tags != '—':
            bits.append(tags[:36])
    return {
        'key': f"album:{info.get('id')}",
        'id': info.get('id') or '', 'title': info.get('title') or '',
        'authors': info.get('authors') or '',
        'pages': pages or 0, 'chapters': chapters or 0,
        'tags': info.get('tags') or '', 'dir': '',
        'cover': _img_url(server_url, cover_path),
        'meta': _meta_text(pages, chapters, info.get('authors'),
                           info.get('tags')) or '点击查看详情',
        'badge': f"JM{info.get('id')}",
    }


def _album_root(opt, detail, kind):
    try:
        obj = detail if kind == 'album' else getattr(detail, 'from_album', None)
        if obj is not None:
            return str(opt.dir_rule.decide_album_root_dir(obj))
    except Exception:
        pass
    return ''


def _hist_add(info, action='查询', cover='', dir_path=''):
    """写一条历史记录（按 key 去重；下载优先保留）"""
    try:
        hist = jmapp.load_history()
        key = 'album:%s' % info.get('id')
        old = None
        for it in hist:
            if it.get('key') == key:
                old = it
                break
        if old is not None:
            hist.remove(old)
            entry = dict(old)
        else:
            entry = {}
        entry.update({
            'key': key, 'id': info.get('id'), 'kind': '本子',
            'title': info.get('title') or entry.get('title') or '',
            'authors': info.get('authors') or entry.get('authors') or '',
            'pages': info.get('pages') or entry.get('pages') or 0,
            'chapters': info.get('chapters') or entry.get('chapters') or 0,
            'tags': info.get('tags') or entry.get('tags') or '',
            'action': entry.get('action') if entry.get('action') == '下载' else action,
            'cover': cover or entry.get('cover') or '',
            'dir': dir_path or entry.get('dir') or '',
            'ts': time.time(),
        })
        hist.insert(0, entry)
        del hist[jmapp.HISTORY_MAX:]
        jmapp.save_history(hist)
    except Exception as e:
        ST.log('写历史失败：%s' % e, 'err')


def _trim_history(info, root):
    hist = jmapp.load_history()
    key = f"album:{info.get('id')}"
    for it in hist:
        if it.get('key') == key:
            it.update({'action': '下载', 'dir': root or it.get('dir') or ''})
            jmapp.save_history(hist)
            return
    hist.insert(0, {
        'key': key, 'id': info.get('id'), 'kind': '本子',
        'title': info.get('title'), 'authors': info.get('authors'),
        'pages': info.get('pages'), 'chapters': info.get('chapters'),
        'tags': info.get('tags'), 'dir': root or '', 'action': '下载',
        'ts': time.time(),
    })
    del hist[jmapp.HISTORY_MAX:]
    jmapp.save_history(hist)


# ---------------------------------------------------------------- API

class Api:
    def __init__(self):
        self.server = None
        self.server_url = ''
        self.base_dir = ''
        self._opt = None

    # ---- 基础
    def _ensure(self):
        if self._opt is None:
            install_log_hook()
            opt, base = jmapp.build_option()
            self._net_apply(opt)
            override = os.environ.get('JMCOMIC_BASE') or ''
            if override and os.path.isdir(override):
                base = override
            self._opt, self.base_dir = opt, base
            if self.server is None:
                self.server, self.server_url = start_image_server(base)
        return self._opt

    # ---------------------------------------------------------- 网络与登录设置
    def _net_cfg(self):
        n = jmapp.load_ui_settings().get('net')
        return n if isinstance(n, dict) else {}

    def _net_write(self, d):
        s = jmapp.load_ui_settings()
        s['net'] = d
        jmapp.save_ui_settings(s)
        self._opt = None            # 下次重建 JmOption 时套用

    def _net_apply(self, opt):
        n = self._net_cfg()
        if not n:
            return
        try:
            if n.get('proxy'):
                opt.client['proxies'] = str(n['proxy']).strip()
            if n.get('domain'):
                opt.client['domain'] = str(n['domain']).strip()
            ck = n.get('cookies')
            if isinstance(ck, dict) and ck:
                cur = dict(opt.client.get('cookies') or {})
                cur.update(ck)
                opt.client['cookies'] = cur
            if n.get('proxy'):
                for k in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy'):
                    os.environ.setdefault(k, str(n['proxy']).strip())
        except Exception as e:
            ST.log('应用网络设置失败：%s' % e, 'err')

    def net_get(self):
        n = self._net_cfg()
        ck = n.get('cookies') if isinstance(n.get('cookies'), dict) else {}
        preview = '; '.join('%s=%s' % (k, (str(v)[:5] + '…') if len(str(v)) > 7 else v)
                            for k, v in list(ck.items())[:4])
        return {'ok': True,
                'proxy': n.get('proxy') or '',
                'domain': n.get('domain') or '',
                'username': n.get('username') or '',
                'has_password': bool(n.get('password')),
                'cookie_count': len(ck),
                'cookie_preview': preview,
                'logged_in': bool(ck.get('AVS'))}

    def net_save(self, proxy='', domain='', username='', password='', cookie=''):
        n = self._net_cfg()
        n['proxy'] = (proxy or '').strip()
        n['domain'] = (domain or '').strip()
        if (username or '').strip():
            n['username'] = username.strip()
        if password:
            n['password'] = password
        if (cookie or '').strip():
            ck = _parse_cookie(cookie)
            if ck:
                cur = dict(n.get('cookies') or {})
                cur.update(ck)
                n['cookies'] = cur
                ST.log('已保存 Cookie（%d 项）' % len(ck), 'ok')
        self._net_write(n)
        msg = '已保存'
        u, p = n.get('username') or '', n.get('password') or ''
        if u and p:
            ok, m = self._try_login(u, p)
            msg = '已保存并登录成功' if ok else ('已保存，但登录失败：%s' % m)
        ST.log('网络设置已更新：%s' % msg, 'ok')
        return {'ok': True, 'msg': msg}

    def _try_login(self, username, password):
        try:
            opt = self._ensure()
            client = opt.new_jm_client()
            client.login(username, password)
            ck = dict(opt.client.get('cookies') or {})
            if ck:
                n = self._net_cfg()
                cur = dict(n.get('cookies') or {})
                cur.update(ck)
                n['cookies'] = cur
                self._net_write(n)
            return True, 'OK'
        except Exception as e:
            return False, str(e)[:160]

    def net_test(self):
        import time as _t
        t0 = _t.time()
        try:
            opt = self._ensure()
            client = opt.new_jm_client()
            page = client.search_site('zzz-not-exist-999', 1)
            cost = _t.time() - t0
            ck = opt.client.get('cookies') or {}
            login = '已登录' if ck.get('AVS') else '未登录'
            return {'ok': True, 'msg': '连接正常 · %.1fs · %s' % (cost, login)}
        except Exception as e:
            return {'ok': False, 'msg': '连接失败：%s' % str(e)[:180]}

    def settings(self):
        try:
            opt = self._ensure()
        except Exception as e:
            return {'ok': False, 'msg': str(e), 'theme': 'dark', 'base': ''}
        theme = jmapp.load_ui_settings().get('theme') or 'dark'
        name_map = {'深色': 'dark', '浅色': 'light', '霓虹': 'neon',
                    '樱花': 'sakura', '跟随系统': 'follow'}
        return {'ok': True, 'theme': name_map.get(theme, theme), 'base': self.base_dir}

    def set_theme(self, key):
        name_map = {'dark': '深色', 'light': '浅色', 'neon': '霓虹',
                    'sakura': '樱花', 'follow': '跟随系统'}
        try:
            d = jmapp.load_ui_settings()
            d['theme'] = name_map.get(key, '深色')
            jmapp.save_ui_settings(d)
            return {'ok': True}
        except Exception as e:
            return {'ok': False, 'msg': str(e)}

    def poll(self):
        snap = ST.snap()
        pend = _q_pending()
        snap['queue'] = [{'spec': s} for s in pend]
        snap['current'] = QINFO.get('current', '')
        snap['queueLen'] = len(pend)
        snap['ok'] = True
        return snap

    # ---- 列表（桥约定：一律返回 dict，形状 {'ok': bool, 'items': [...], 'msg': str}）
    def list_local(self):
        try:
            self._ensure()
            return {'ok': True, 'items': _local_items(self.server_url, self.base_dir)}
        except Exception as e:
            ST.log(f'读配置失败：{e}', 'err')
            return {'ok': False, 'msg': str(e), 'items': []}

    def list_history(self):
        try:
            self._ensure()
            return {'ok': True, 'items': _history_items(self.server_url)}
        except Exception as e:
            ST.log(f'读历史失败：{e}', 'err')
            return {'ok': False, 'msg': str(e), 'items': []}

    # ---- 查询 / 搜索
    def query(self, spec):
        try:
            opt = self._ensure()
            ids = jmapp.extract_ids_smart(spec)
            if not ids:
                # 不是车号 → 当关键词搜索，返回结果让用户挑
                r = self.search(str(spec), 'all')
                if r.get('ok'):
                    return {'ok': True, 'searched': True, 'items': r.get('items') or []}
                return {'ok': False, 'msg': r.get('msg') or '没找到'}
            client = opt.new_jm_client()
            items = []
            for kind, i in ids:
                kind2, detail, hit = jmapp.resolve_item(client, kind, i)
                info = jmapp.detail_info(detail)
                cover = os.path.join(jmapp.COVER_DIR, f"{info['id']}.jpg")
                if not (os.path.isfile(cover) and os.path.getsize(cover) > 1024):
                    try:
                        os.makedirs(jmapp.COVER_DIR, exist_ok=True)
                        client.download_album_cover(str(info['id']), cover)
                    except Exception:
                        pass
                _hist_add(info, '查询', cover)
                it = _remote_item(self.server_url, info, cover)
                if hit:
                    it['meta'] += f' · 命中 {hit}'
                it['chapters'] = info.get('chapters')
                items.append(it)
            return {'ok': True, 'items': items}
        except Exception as e:
            ST.log(f'查询失败：{e}', 'err')
            return {'ok': False, 'msg': str(e)}

    def search(self, word, kind='all', order='mr', time='a'):
        try:
            opt = self._ensure()
            client = opt.new_jm_client()
            kind_map = {'all': 'site', 'work': 'work', 'author': 'author'}
            page = jmapp.search_page(client, word, kind_map.get(kind, 'site'),
                                     order=order, time=time)
            infos = jmapp.search_infos(page) or []
            # 先用搜索接口的轻量信息
            items = []
            for _key, info in infos[:24]:
                cover = os.path.join(jmapp.COVER_DIR, f"{info['id']}.jpg")
                if not (os.path.isfile(cover) and os.path.getsize(cover) > 1024):
                    try:
                        client.download_album_cover(str(info['id']), cover)
                    except Exception:
                        cover = ''
                items.append(_remote_item(self.server_url, info, cover))
            return {'ok': True, 'items': items}
        except Exception as e:
            ST.log(f'搜索失败：{e}', 'err')
            return {'ok': False, 'msg': str(e)}

    # ---- 下载
    def download(self, spec):
        ids = jmapp.extract_ids_smart(spec)
        if not ids:
            return {'ok': False, 'msg': '没识别出车号（支持 JM号 / p章节号 / 站内链接 / 含车号的文字）'}
        with _QLock:
            _Q.append(str(spec))
            start = not _QBUSY['alive']
            if start:
                _QBUSY['alive'] = True
                _QBUSY['thread'] = threading.Thread(target=self._queue_worker, daemon=True)
                worker = _QBUSY['thread']
        if start:
            worker.start()
        ST.log('加入队列：%s（队列 %d 个）' % (spec, len(_q_pending())), '')
        return {'ok': True, 'queued': True}

    def _queue_worker(self):
        """串行消费队列"""
        try:
            while True:
                with _QLock:
                    if not _Q:
                        _QBUSY['alive'] = False
                        return
                    spec = _Q.pop(0)
                QINFO['current'] = spec
                try:
                    self._work_download(spec)
                except Exception as e:
                    ST.log('队列任务失败：%s' % e, 'err')
                QINFO['current'] = ''
        finally:
            QINFO['current'] = ''
            with _QLock:
                _QBUSY['alive'] = False

    def queue_clear(self):
        with _QLock:
            n = len(_Q)
            _Q.clear()
        return {'ok': True, 'cleared': n}

    def library_stats(self):
        """已下载库：本子数 / 图片数 / 占用"""
        try:
            self._ensure()
        except Exception as e:
            return {'ok': False, 'msg': str(e)}
        albums = files = 0
        size = 0
        try:
            for name in os.listdir(self.base_dir):
                d = os.path.join(self.base_dir, name)
                if not os.path.isdir(d):
                    continue
                albums += 1
                for root, _dirs, names in os.walk(d):
                    for f in names:
                        if os.path.splitext(f)[1].lower() in jmapp.IMAGE_EXT:
                            files += 1
                            try:
                                size += os.path.getsize(os.path.join(root, f))
                            except Exception:
                                pass
        except Exception as e:
            return {'ok': False, 'msg': str(e)}
        return {'ok': True, 'albums': albums, 'files': files,
                'size_mb': round(size / 1048576.0, 1)}

    def _work_download(self, spec):
        _CANCEL.clear()
        ST.start('download', '准备中 …')
        try:
            opt = self._ensure()
            client = opt.new_jm_client()
            for kind, i in jmapp.parse_ids(spec):
                if _CANCEL.is_set():
                    raise jmapp.Cancelled()
                kind2, detail, hit = jmapp.resolve_item(client, kind, i)
                info = jmapp.detail_info(detail)
                ST.total = int(info.get('pages') or 0)
                ST.done = 0
                ST.status = f"下载中：{info.get('title')}（{ST.total} 页）"
                ST.log(f"开始 {info.get('title')}" + (f"（命中 {hit}）" if hit else ''))
                root = _album_root(opt, detail, kind2)
                _trim_history(info, root)
                if kind2 == 'album':
                    opt.download_album(detail.id)
                else:
                    opt.download_photo(detail.id)
                ST.log(f"完成：{info.get('title')}", 'ok')
            ST.finish('下载完成')
        except jmapp.Cancelled:
            ST.log('已取消（已下载的图片保留在目录里）', 'warn')
            ST.finish('已取消')
        except Exception as e:
            ST.log(f'下载失败：{e}', 'err')
            ST.finish('下载失败')
        finally:
            _CANCEL.clear()

    def cancel(self):
        _CANCEL.set()
        ST.log('收到取消请求 …', 'warn')
        return {'ok': True}

    # ---- 导出 / 删除 / 打开
    def _dir_of(self, key_or_dir):
        """key 或路径 → 本子文件夹（local:<路径> / album:<车号> / 直接路径）"""
        if not key_or_dir:
            return ''
        key = str(key_or_dir)
        if os.path.isdir(key):
            return key
        if key.startswith('local:'):
            p = key[len('local:'):]
            return p if os.path.isdir(p) else ''
        aid = key.split(':', 1)[1] if ':' in key else ''
        if aid and self.base_dir:
            for e in _local_items('', self.base_dir):
                if str(e.get('id')) == aid and e.get('dir'):
                    return e['dir']
        return ''

    def export_it(self, key_or_dir, fmt='pdf'):
        try:
            self._ensure()
            root = self._dir_of(key_or_dir)
            if not root or not os.path.isdir(root):
                return {'ok': False, 'msg': '找不到已下载的文件夹'}
            out_dir = jmapp.export_dir(self.base_dir)
            name = os.path.basename(os.path.normpath(root))
            ok, n, path = jmapp.export_album(root, fmt, out_dir=out_dir, name=name)
            if ok:
                ST.log(f'导出 {fmt.upper()}：{path}（{n} 页）', 'ok')
                return {'ok': True, 'msg': os.path.basename(path)}
            return {'ok': False, 'msg': str(path)}
        except Exception as e:
            return {'ok': False, 'msg': str(e)}

    def trash(self, key_or_dir):
        try:
            root = self._dir_of(key_or_dir)
            if not root or not os.path.isdir(root):
                return {'ok': False, 'msg': '找不到已下载的文件夹'}
            ok, msg = jmapp.trash_path(root)
            if ok:
                ST.log(f'已移到回收站：{root}', 'ok')
            return {'ok': ok, 'msg': msg}
        except Exception as e:
            return {'ok': False, 'msg': str(e)}

    def search_enrich(self, ids):
        """并发补全搜索结果详情（4 路），比串行快很多"""
        try:
            self._ensure()
            opt, _b = jmapp.build_option()
            ids = [''.join(c for c in str(x) if c.isdigit()) for x in (ids or [])][:24]
            ids = [x for x in ids if x]
            if not ids:
                return {'ok': True, 'items': []}

            def one(aid):
                cached = _DETAIL_CACHE.get(aid)
                if cached and (time.time() - cached[0] < 3600):
                    info = cached[1]
                else:
                    info = None
                    last_err = None
                    for attempt in range(3):        # 失败自动重试 3 次
                        try:
                            info = jmapp.detail_info(
                                opt.new_jm_client().get_album_detail(aid))
                            _DETAIL_CACHE[aid] = (time.time(), info)
                            break
                        except Exception as e:
                            last_err = e
                            time.sleep(0.4 * (attempt + 1))
                    if info is None:
                        ST.log('补全 JM%s 失败：%s' % (aid, last_err), 'err')
                        raise last_err
                tags = str(info.get('tags') or '')
                return {
                    'id': aid,
                    'pages': info.get('pages') or 0,
                    'chapters': info.get('chapters') or 0,
                    'authors': info.get('authors') or '',
                    'tags': tags,
                    'tags_list': [t for t in tags.split(' / ') if t and t != '—'],
                    'desc': info.get('desc') or '',
                }

            from concurrent.futures import ThreadPoolExecutor, as_completed
            out = []
            with ThreadPoolExecutor(max_workers=2) as pool:
                futs = {pool.submit(one, aid): aid for aid in ids}
                for fut in as_completed(futs):
                    try:
                        out.append(fut.result())
                    except Exception:
                        pass
            out.sort(key=lambda x: ids.index(x['id']) if x['id'] in ids else 999)
            return {'ok': True, 'items': out}
        except Exception as e:
            return {'ok': False, 'msg': str(e)}

    def album_info(self, aid):
        """单个车号的详情（带缓存），供搜索结果点开时补全"""
        try:
            self._ensure()
            aid = ''.join(c for c in str(aid) if c.isdigit())
            if not aid:
                return {'ok': False}
            cached = _DETAIL_CACHE.get(aid)
            if cached and (time.time() - cached[0] < 3600):
                return {'ok': True, 'info': cached[1]}
            opt, _b = jmapp.build_option()
            detail = opt.new_jm_client().get_album_detail(aid)
            info = jmapp.detail_info(detail)
            tags = str(info.get('tags') or '')
            info['tags_list'] = [t for t in tags.split(' / ') if t and t != '—']
            info.setdefault('desc', '')
            _DETAIL_CACHE[aid] = (time.time(), info)
            return {'ok': True, 'info': info}
        except Exception as e:
            return {'ok': False, 'msg': str(e)}

    def chapters(self, key_or_dir):
        """阅读器用：返回 [{name, images:[url...]}]"""
        try:
            self._ensure()
            root = self._dir_of(key_or_dir)
            if not root or not os.path.isdir(root):
                return {'ok': False, 'msg': '找不到这个本子的文件夹'}
            out = []
            for name, pages in jmapp.discover_chapters(root):
                out.append({
                    'name': name,
                    'images': [_img_url(self.server_url, p) for p in pages],
                })
            if not out:
                return {'ok': False, 'msg': '这个目录里没有图片'}
            return {'ok': True, 'chapters': out,
                    'title': os.path.basename(os.path.normpath(root))}
        except Exception as e:
            return {'ok': False, 'msg': str(e)}

    def open_folder_of(self, key_or_dir):
        try:
            root = self._dir_of(key_or_dir)
            if not root or not os.path.isdir(root):
                return {'ok': False, 'msg': '找不到文件夹'}
            os.startfile(root)
            return {'ok': True}
        except Exception as e:
            return {'ok': False, 'msg': str(e)}

    # ---------------------------------------------------------- 追更（作者）
    def _watch_path(self):
        return os.path.join(ROOT, 'watch.json')

    def _watch_load(self):
        """v2：{'authors': [...]}；旧的 {'items': [...]}（作品级）自动迁移成作者"""
        d = None
        try:
            with open(self._watch_path(), encoding='utf-8-sig') as f:
                d = json.load(f)
        except Exception:
            pass
        if isinstance(d, dict):
            if isinstance(d.get('authors'), list):
                return d
            items = d.get('items') or []
            if items:
                authors = []
                for it in items:
                    try:
                        m = self._album_meta(it.get('id'))
                    except Exception:
                        m = {}
                    a = (m.get('authors') or '').strip()
                    if a and not any(x['name'] == a for x in authors):
                        authors.append({'name': a, 'known': [], 'newCount': 0,
                                        'lastCheck': '', 'works': 0})
                return {'authors': authors}
        return {'authors': []}

    def _watch_save(self, data):
        try:
            p = self._watch_path()
            with open(p + '.tmp', 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            os.replace(p + '.tmp', p)
        except Exception:
            pass

    def _album_meta(self, aid):
        aid = str(aid)
        try:
            for e in _local_items('', self.base_dir or ''):
                if str(e.get('id')) == aid:
                    return e
        except Exception:
            pass
        for it in jmapp.load_history():
            if str(it.get('id')) == aid:
                return {'id': aid, 'title': it.get('title') or '',
                        'authors': it.get('authors') or '',
                        'pages': it.get('pages') or 0,
                        'chapters': it.get('chapters') or 0,
                        'dir': it.get('dir') or ''}
        return {'id': aid, 'title': '', 'authors': '', 'pages': 0,
                'chapters': 0, 'dir': ''}

    def watch_toggle_name(self, name):
        """按作者名 关注 / 取消关注"""
        name = (name or '').strip()
        if not name:
            return {'ok': False, 'msg': '没有作者信息'}
        data = self._watch_load()
        arr = data['authors']
        cur = next((x for x in arr if x.get('name') == name), None)
        if cur is not None:
            arr.remove(cur)
            self._watch_save(data)
            ST.log('取消关注作者：%s' % name, '')
            return {'ok': True, 'watching': False, 'name': name}
        arr.append({'name': name, 'known': [], 'newCount': 0,
                    'lastCheck': '', 'works': 0})
        self._watch_save(data)
        ST.log('关注作者：%s' % name, 'ok')
        return {'ok': True, 'watching': True, 'name': name}

    def watch_toggle(self, key_or_aid):
        """兼容入口：给车号 → 找到作者再关注"""
        aid = ''.join(c for c in str(key_or_aid) if c.isdigit())
        name = ''
        if aid:
            m = self._album_meta(aid)
            name = (m.get('authors') or '').strip()
            if not name:
                try:
                    self._ensure()
                    opt, _b = jmapp.build_option()
                    info = jmapp.detail_info(opt.new_jm_client().get_album_detail(aid))
                    name = (info.get('authors') or '').strip()
                except Exception:
                    name = ''
        return self.watch_toggle_name(name)

    def watch_has(self, name):
        """是否已关注该作者"""
        name = (name or '').strip()
        if not name:
            return {'ok': False}
        for it in self._watch_load()['authors']:
            if it.get('name') == name:
                return {'ok': True, 'name': name, 'newCount': int(it.get('newCount') or 0)}
        return {'ok': False}

    def watch_list(self):
        try:
            self._ensure()
        except Exception:
            pass
        out = []
        for it in self._watch_load()['authors']:
            cover = ''
            for aid in (it.get('known') or [])[:8]:
                c = _cover_path_for(str(aid), '', '')
                if c:
                    cover = c
                    break
            out.append({
                'name': it.get('name'),
                'works': int(it.get('works') or 0),
                'newCount': int(it.get('newCount') or 0),
                'lastCheck': it.get('lastCheck') or '',
                'cover': _img_url(self.server_url, cover),
                'newItems': it.get('newItems') or [],
            })
        out.sort(key=lambda x: (-x['newCount'], x['name'] or ''))
        return {'ok': True, 'authors': out}

    def watch_check(self):
        """逐个作者查其作品，跟已知集合对比出新作品数"""
        try:
            opt = self._ensure()
            client = opt.new_jm_client()
        except Exception as e:
            return {'ok': False, 'msg': str(e)}
        data = self._watch_load()
        updated = 0
        for it in data['authors']:
            name = it.get('name')
            try:
                page = jmapp.search_page(client, name, 'author')
                infos = jmapp.search_infos(page) or []
                ids = [str(i['id']) for _k, i in infos]
                known = set(str(x) for x in (it.get('known') or []))
                new = ([i for _k, i in infos if str(i['id']) not in known]
                       if known else [])
                it['newCount'] = len(new)
                it['newItems'] = [{'id': i['id'], 'title': i.get('title')}
                                  for i in new[:12]]
                merged = list(dict.fromkeys(list(known) + ids))
                it['known'] = merged[-300:]
                it['works'] = len(ids)
                it['lastCheck'] = time.strftime('%m-%d %H:%M')
                if new:
                    updated += 1
                ST.log('检查作者 %s：%d 部作品%s' % (
                    name, len(ids), ('（新 %d 部）' % len(new)) if new else ''), '')
            except Exception as e:
                it['lastCheck'] = '检查失败'
                ST.log('检查作者 %s 失败：%s' % (name, e), 'err')
            time.sleep(0.4)          # 别给站点压力
        self._watch_save(data)
        ST.log('更新检查完成：%d 位作者有新作品' % updated, 'ok')
        return {'ok': True, 'updated': updated, 'total': len(data['authors'])}

    def looks_like_id(self, text):
        try:
            return {'ok': bool(jmapp.looks_like_id(text))}
        except Exception:
            return {'ok': False}

    def watch_download(self, aid):
        return self.download(str(aid))

    def open_base(self):
        try:
            self._ensure()
            os.makedirs(self.base_dir, exist_ok=True)
            os.startfile(self.base_dir)
            return {'ok': True}
        except Exception as e:
            return {'ok': False, 'msg': str(e)}


# ---------------------------------------------------------------- 入口

def _demo_hook(window):
    """仅用于截图/自测：JMCOMIC_AUTOOPEN=1 时自动打开第一本 + 阅读器"""
    import time as _t
    _t.sleep(3.2)
    mode = os.environ.get('JMCOMIC_AUTOMODE') or 'single'
    zoom = os.environ.get('JMCOMIC_AUTOZOOM') or ''
    try:
        fit = os.environ.get('JMCOMIC_AUTOFIT') or ''
        extra = (', ' + zoom) if zoom else (', 0' if fit else '')
        js = "window.__demoReader && window.__demoReader('%s'%s%s)" % (
            mode, extra, (", '%s'" % fit) if fit else '')
        window.evaluate_js(js)
        test = os.environ.get('JMCOMIC_AUTOTEST') or ''
        if test == 'search':
            _t.sleep(1.2)
            window.evaluate_js("document.querySelector('#q').value='deepseek2'")
            window.evaluate_js("doSearch()")
            _t.sleep(9)
            val = window.evaluate_js(
                "JSON.stringify({items:(S.items||[]).slice(0,2).map(function(i){return {p:i.pages,m:i.meta}}),dom:Array.prototype.slice.call(document.querySelectorAll('#grid .card .meta')).slice(0,2).map(function(d){return d.textContent}),cards:document.querySelectorAll('#grid .card').length})")
            try:
                import tempfile
                with open(os.path.join(tempfile.gettempdir(), 'rd_search.txt'), 'w',
                          encoding='utf-8') as f:
                    f.write(str(val))
            except Exception:
                pass

        if test == 'wheel':
            _t.sleep(1.2)
            window.evaluate_js("window.__testWheel && window.__testWheel(true, -120, 3)")
        elif test == 'anchor':
            _t.sleep(1.2)
            val = window.evaluate_js("window.__anchorTest && window.__anchorTest()")
            try:
                import tempfile
                with open(os.path.join(tempfile.gettempdir(), 'rd_anchor.txt'), 'w',
                          encoding='utf-8') as f:
                    f.write(str(val))
            except Exception:
                pass
    except Exception:
        pass


def main():
    if webview is None:
        print('缺少 pywebview：python -m pip install pywebview')
        return 1
    if sys.platform == 'win32':
        try:
            import ctypes
            # use_last_error=True + get_last_error() 才可靠：
            # 直接 windll.kernel32.GetLastError() 依赖"中间没有别的 Win32 调用"，
            # 冻结后加载 pythonnet 等库时可能被重置。
            _k32 = ctypes.WinDLL('kernel32', use_last_error=True)
            _mutex = _k32.CreateMutexW(None, False, 'Global\\JMComicWeb_SingleInstance')
            if ctypes.get_last_error() == 183:   # ERROR_ALREADY_EXISTS
                ctypes.windll.user32.MessageBoxW(
                    None, 'JMComic 已经在运行了。', 'JMComic', 0x40)
                return 0
        except Exception:
            pass

    api = Api()
    try:
        api._ensure()
    except Exception as e:
        print('初始化失败：', e)

    window = webview.create_window(
        'JMComic', url=UI_FILE, js_api=api,
        width=1240, height=840, min_size=(940, 640),
        background_color='#0b0e13', text_select=False,
        zoomable=False,          # 关键：别让 WebView2 把 Ctrl+滚轮 当成整页缩放
    )
    func = _demo_hook if os.environ.get('JMCOMIC_AUTOOPEN') else None
    webview.start(func, window, debug=False)
    return 0


if __name__ == '__main__':
    sys.exit(main())
