# -*- coding: utf-8 -*-
"""JMComic 工具箱 —— 自用 QQ 机器人（OneBot 11 / NapCatQQ，反向 HTTP 上报）

=========== 安全边界（写在代码里，不是文档口头约定）===========
1. 只处理 **私聊** 消息（message_type == 'private'），群里发的直接丢弃；
2. 只处理 **白名单里的 QQ 号**（bot_config.yml 的 owners，默认就是你自己的号）；
3. 本文件**没有任何"发到群"的代码路径** —— 只实现了 send_private_msg /
   upload_private_file 两个接口，想加群发必须改代码。

=========== 功能 ===========
    帮助                      命令列表
    查询 350234               标题 / 作者 / 页数 / 话数
    下载 350234               下载整本（后台队列，完成回消息）
    导出 350234               已下载的本子 → PDF + ZIP，直接把文件发给你
    搜索 关键词               站内搜索，回前 10 条（标题 + 车号）
    状态                      保存位置 / 导出目录 / 队列

=========== 安装（详见 bot/README-机器人.md）==========
    1. 装 NapCatQQ，登录 QQ 小号
    2. NapCat → 网络配置：
         · HTTP 服务端：http://127.0.0.1:3000   （机器人调它发消息）
         · HTTP 上报（反向）：http://127.0.0.1:8099/report?token=你的token
    3. 编辑 bot/bot_config.yml：owners 填你的 QQ 号、token 两边一致
    4. 双击 bot/启动机器人.cmd
"""
import json
import os
import queue
import re
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src'))

import jmapp  # noqa: E402

CONFIG_PATH = os.path.join(ROOT, 'bot', 'bot_config.yml')
LOG_PATH = os.path.join(ROOT, 'bot', 'bot.log')

DEFAULT_CONFIG = {
    'owners': [],
    'api': 'http://127.0.0.1:3000',
    'api_token': '',
    'report_host': '127.0.0.1',
    'report_port': 8099,
    'report_token': '',
    'send_zip': True,
    'max_send_mb': 120,
    'max_download_pages': 400,
}


# ---------------------------------------------------------------- 基础设施

def log(msg):
    line = '[%s] %s' % (datetime.now().strftime('%m-%d %H:%M:%S'), msg)
    print(line, flush=True)
    try:
        with open(LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass


def load_config(path=None):
    path = path or CONFIG_PATH
    cfg = dict(DEFAULT_CONFIG)
    try:
        import yaml
        with open(path, encoding='utf-8') as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            raise ValueError('配置格式不对，应该是 key: value')
        cfg.update({k: v for k, v in data.items() if v is not None})
    except FileNotFoundError:
        log('配置文件不存在：%s（先用默认值，白名单为空 → 不会响应任何人）' % path)
    except Exception as e:
        log('读配置失败：%s（先用默认值）' % e)
    cfg['owners'] = [str(x) for x in (cfg.get('owners') or [])]
    return cfg


def extract_text(message):
    """OneBot 消息（字符串或消息段数组）→ 纯文本"""
    if isinstance(message, str):
        return message.strip()
    parts = []
    if isinstance(message, list):
        for seg in message:
            if not isinstance(seg, dict):
                continue
            if seg.get('type') == 'text':
                parts.append(str((seg.get('data') or {}).get('text') or ''))
    return ''.join(parts).strip()


def split_command(text):
    """'下载 123' / '/下载 123' / '123' → (命令, 参数)"""
    t = text.strip().lstrip('/').strip()
    if not t:
        return '', ''
    head, _, rest = t.partition(' ')
    if head in ('帮助', 'help', '?', '？'):
        return '帮助', ''
    if head in ('查询', '查', 'cx'):
        return '查询', rest.strip()
    if head in ('下载', '下', 'xz'):
        return '下载', rest.strip()
    if head in ('导出', '打包', 'dc'):
        return '导出', rest.strip()
    if head in ('搜索', '搜', 'ss'):
        return '搜索', rest.strip()
    if head in ('状态', 'status'):
        return '状态', ''
    if re.fullmatch(r'p?\d{3,8}', t):          # 只发个车号 = 查询
        return '查询', t
    return head, rest.strip()


class OneBot:
    """只实现私聊相关接口 —— 没有群发能力"""

    def __init__(self, api_base, token='', timeout=60):
        self.base = (api_base or '').rstrip('/')
        self.token = token or ''
        self.timeout = timeout

    def _post(self, action, payload):
        url = '%s/%s' % (self.base, action)
        data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        req = urllib.request.Request(url, data=data, method='POST',
                                     headers={'Content-Type': 'application/json'})
        if self.token:
            req.add_header('Authorization', 'Bearer ' + self.token)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            body = resp.read().decode('utf-8', 'replace')
        try:
            return json.loads(body)
        except Exception:
            return {'raw': body}

    def send_text(self, user_id, text):
        return self._post('send_private_msg',
                          {'user_id': int(user_id), 'message': str(text)})

    def send_file(self, user_id, path, name=None):
        name = name or os.path.basename(path)
        return self._post('upload_private_file', {
            'user_id': int(user_id), 'file': path, 'name': name})


# ---------------------------------------------------------------- 核心逻辑

class Bot:
    def __init__(self, cfg, api=None):
        self.cfg = cfg
        self.api = api or OneBot(cfg.get('api'), cfg.get('api_token'))
        self.q = queue.Queue()
        self.worker = threading.Thread(target=self._work_loop, daemon=True)
        self.worker.start()
        self.doing = '空闲'

    # ---- 权限：只有白名单里的号、只有私聊
    def is_owner(self, event):
        try:
            if event.get('post_type') != 'message':
                return False
            if event.get('message_type') != 'private':
                return False
            return str(event.get('user_id')) in self.cfg['owners']
        except Exception:
            return False

    def submit(self, event):
        uid = str(event.get('user_id'))
        text = extract_text(event.get('message'))
        log('收到私聊 %s: %s' % (uid, text[:80]))
        self.q.put((uid, text))

    # ---- 队列：一次只干一件事，别把禁漫服务器打爆
    def _work_loop(self):
        while True:
            uid, text = self.q.get()
            try:
                self.handle(uid, text)
            except Exception:
                log('处理出错：\n' + traceback.format_exc(limit=4))
                try:
                    self.api.send_text(uid, '出错了，详情看 bot.log')
                except Exception:
                    pass
            finally:
                self.doing = '空闲'
                self.q.task_done()

    def reply(self, uid, text):
        try:
            self.api.send_text(uid, text)
        except Exception as e:
            log('发消息失败：%s' % e)

    def handle(self, uid, text):
        cmd, arg = split_command(text)
        log('命令 %s / %s' % (cmd, arg[:40]))
        if cmd in ('', None):
            return
        if cmd in ('帮助',):
            return self.reply(uid, HELP_TEXT)
        if cmd == '查询':
            return self.cmd_query(uid, arg)
        if cmd == '下载':
            return self.cmd_download(uid, arg)
        if cmd == '导出':
            return self.cmd_export(uid, arg)
        if cmd == '搜索':
            return self.cmd_search(uid, arg)
        if cmd == '状态':
            return self.cmd_status(uid)
        self.reply(uid, '不认识的命令：%s\n发「帮助」看用法。' % cmd)

    # ---- 公共：把参数解析成车号
    def _parse_spec(self, arg):
        arg = (arg or '').strip()
        ids = jmapp.parse_ids(arg)
        if not ids:
            self.reply('请带车号，例如：查询 350234')
            return None
        return ids[0]

    def _option(self):
        opt, base = jmapp.build_option()
        return opt, base

    # ---- 命令实现
    def cmd_query(self, uid, arg):
        spec = self._parse_spec(arg)
        if not spec:
            return
        self.doing = '查询 %s' % spec[1]
        try:
            opt, _base = self._option()
            client = opt.new_jm_client()
            kind, detail, hit = jmapp.resolve_item(client, spec[0], spec[1])
        except Exception as e:
            return self.reply(uid, '查询失败：%s' % e)

        info = jmapp.detail_info(detail)
        lines = ['【%s】%s' % (info['kind'], info['title']),
                 '车号：JM%s' % info['id'],
                 '作者：%s' % info['authors'],
                 '页数：%s    章节：%s' % (info['pages'], info['chapters'])]
        if hit:
            lines.append('命中章节：%s（已自动关联整本）' % hit)
        if info.get('tags') and info['tags'] != '—':
            lines.append('标签：%s' % info['tags'][:120])
        lines.append('')
        lines.append('发「下载 %s」下载整本' % info['id'])
        self.reply(uid, '\n'.join(lines))

    def cmd_download(self, uid, arg):
        spec = self._parse_spec(arg)
        if not spec:
            return
        self.doing = '下载 %s' % spec[1]
        try:
            opt, base = self._option()
            client = opt.new_jm_client()
            kind, detail, hit = jmapp.resolve_item(client, spec[0], spec[1])
        except Exception as e:
            return self.reply(uid, '解析失败：%s' % e)

        info = jmapp.detail_info(detail)
        pages = int(info.get('pages') or 0)
        cap = int(self.cfg.get('max_download_pages') or 0)
        if cap and pages > cap:
            return self.reply(uid, '这本 %d 页，超过机器人上限 %d 页，先不下。' % (pages, cap))

        self.reply(uid, '开始下载：%s（%d 页 / %d 话）\n下完再回你路径。'
                   % (info['title'], pages, info['chapters']))
        t0 = time.time()
        try:
            opt.download_album(info['id'])
        except Exception as e:
            return self.reply(uid, '下载失败：%s' % e)

        root = ''
        try:
            obj = detail if kind == 'album' else getattr(detail, 'from_album', None)
            if obj is not None:
                root = str(opt.dir_rule.decide_album_root_dir(obj))
        except Exception:
            pass
        chapters = jmapp.discover_chapters(root) if (root and os.path.isdir(root)) else []
        n_pages = sum(len(p) for _n, p in chapters) if chapters else 0
        self.reply(uid, '下载完成：%s\n%d 页 / %d 话，用时 %.0f 秒\n目录：%s\n\n发「导出 %s」打包成 PDF 发给你'
                   % (info['title'], n_pages, len(chapters), time.time() - t0,
                      root or '（用「打开下载文件夹」看）', info['id']))

    def cmd_export(self, uid, arg):
        spec = self._parse_spec(arg)
        if not spec:
            return
        self.doing = '导出 %s' % spec[1]
        try:
            opt, base = self._option()
            client = opt.new_jm_client()
            kind, detail, _hit = jmapp.resolve_item(client, spec[0], spec[1])
        except Exception as e:
            return self.reply(uid, '解析失败：%s' % e)

        info = jmapp.detail_info(detail)
        root = ''
        try:
            obj = detail if kind == 'album' else getattr(detail, 'from_album', None)
            if obj is not None:
                root = str(opt.dir_rule.decide_album_root_dir(obj))
        except Exception:
            pass
        if not (root and os.path.isdir(root)):
            return self.reply(uid, '还没下载这本，先发「下载 %s」' % info['id'])

        out_dir = jmapp.export_dir(base)
        results = []
        for fmt in ('pdf', 'zip'):
            if fmt == 'zip' and not self.cfg.get('send_zip'):
                continue
            ok, n, path = jmapp.export_album(root, fmt, out_dir=out_dir, name=info['title'])
            if not ok:
                results.append((fmt, False, str(path)))
                continue
            mb = os.path.getsize(path) / 1024.0 / 1024.0
            limit = float(self.cfg.get('max_send_mb') or 0)
            if limit and mb > limit:
                results.append((fmt, True, '%s（%.0f MB，超过 %s MB 上限，没发文件）' % (path, mb, limit)))
                continue
            results.append((fmt, True, path))

        ok_paths = [p for _f, ok, p in results if ok and os.path.isfile(p)]
        for p in ok_paths:
            try:
                self.api.send_file(uid, p)
            except Exception as e:
                self.reply(uid, '发送 %s 失败：%s' % (os.path.basename(p), e))

        lines = ['导出完成：%s' % info['title']]
        for fmt, ok, p in results:
            if ok and os.path.isfile(p):
                lines.append('%s：%s（%.1f MB）' % (fmt.upper(), os.path.basename(p),
                                                    os.path.getsize(p) / 1024.0 / 1024.0))
            else:
                lines.append('%s：%s' % (fmt.upper(), p))
        lines.append('')
        lines.append('导出目录：%s' % out_dir)
        self.reply(uid, '\n'.join(lines))

    def cmd_search(self, uid, arg):
        word = (arg or '').strip()
        if not word:
            return self.reply(uid, '用法：搜索 关键词')
        self.doing = '搜索 %s' % word
        try:
            opt, _base = self._option()
            client = opt.new_jm_client()
            page = jmapp.search_page(client, word, 'site')
        except Exception as e:
            return self.reply(uid, '搜索失败：%s' % e)

        infos = jmapp.search_infos(page) or []
        total = getattr(page, 'total', '?')
        if not infos:
            return self.reply(uid, '没搜到「%s」（共 %s 个结果）' % (word, total))
        lines = ['搜索「%s」：共 %s 个，本页 %d 条' % (word, total, len(infos))]
        for key, info in infos[:10]:
            lines.append('%s  %s%s' % (info['id'], info['title'][:40],
                                       ('  / ' + info['authors']) if info.get('authors') else ''))
        lines.append('')
        lines.append('发「查询 车号」看详情，「下载 车号」下载')
        self.reply(uid, '\n'.join(lines))

    def cmd_status(self, uid):
        try:
            opt, base = self._option()
        except Exception as e:
            return self.reply(uid, '读配置失败：%s' % e)
        lines = ['保存位置：%s' % base,
                 '导出目录：%s' % jmapp.export_dir(base),
                 '队列：%d 个待处理   当前：%s' % (self.q.qsize(), self.doing),
                 '白名单：%s' % ', '.join(self.cfg['owners'] or ['（空 → 谁都不理）'])]
        self.reply(uid, '\n'.join(lines))


HELP_TEXT = """JMComic 自用机器人
———————————————
查询 车号        看标题/作者/页数/话数
下载 车号        下载整本（下完回路径）
导出 车号        已下载的本子 → PDF + ZIP 发给你
搜索 关键词      站内搜索前 10 条
状态             保存位置/导出目录/队列

直接发车号也行（等于查询）。
章节号带 p，如 p350240，会自动关联到整本。

仅限私聊 + 白名单，群里不发消息。"""


# ---------------------------------------------------------------- 上报服务

class ReportHandler(BaseHTTPRequestHandler):
    server_version = 'jmqq/1.0'

    def log_message(self, fmt, *args):
        pass

    def _deny(self, code=403):
        self.send_response(code)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_GET(self):
        self._deny(405)

    def do_POST(self):
        cfg = self.server.cfg
        bot = self.server.bot
        parsed = urlparse(self.path)
        if parsed.path not in ('/report', '/'):
            return self._deny(404)

        token = cfg.get('report_token') or ''
        if token:
            given = (parse_qs(parsed.query).get('token') or [''])[0]
            if not given:
                auth = self.headers.get('Authorization') or ''
                given = auth[7:] if auth.lower().startswith('bearer ') else ''
            if given != token:
                log('上报 token 不对，已拒绝（%s）' % self.client_address[0])
                return self._deny(403)

        try:
            length = int(self.headers.get('Content-Length') or 0)
            raw = self.rfile.read(length) if length else b''
            event = json.loads(raw.decode('utf-8', 'replace') or '{}')
        except Exception as e:
            log('上报解析失败：%s' % e)
            return self._deny(400)

        self.send_response(204)
        self.send_header('Content-Length', '0')
        self.end_headers()

        try:
            if bot.is_owner(event):
                bot.submit(event)
            else:
                if event.get('post_type') == 'message':
                    log('忽略非白名单/非私聊消息：type=%s user=%s'
                        % (event.get('message_type'), event.get('user_id')))
        except Exception:
            log('事件处理出错：\n' + traceback.format_exc(limit=3))


def serve(cfg, bot, port=None, host=None):
    host = host or cfg.get('report_host') or '127.0.0.1'
    port = int(port or cfg.get('report_port') or 8099)
    srv = ThreadingHTTPServer((host, port), ReportHandler)
    srv.cfg = cfg
    srv.bot = bot
    log('上报服务已启动：http://%s:%d/report?token=%s'
        % (host, port, cfg.get('report_token') or '（未设置）'))
    return srv


# ---------------------------------------------------------------- 自检（离线）

def _start_fake_api(port):
    """模拟 NapCat 的 HTTP API，记录机器人发出去的东西"""
    import http.server

    calls = []

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get('Content-Length') or 0)
            body = self.rfile.read(n).decode('utf-8', 'replace')
            try:
                payload = json.loads(body)
            except Exception:
                payload = {}
            calls.append((self.path.strip('/'), payload))
            out = json.dumps({'status': 'ok', 'retcode': 0,
                              'data': {'message_id': len(calls)}}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(out)))
            self.end_headers()
            self.wfile.write(out)

    srv = http.server.ThreadingHTTPServer(('127.0.0.1', port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, calls


def _post(url, obj, headers=None):
    data = json.dumps(obj, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(url, data=data, method='POST',
                                 headers=headers or {'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:
        return 'ERR:%s' % e


def selftest():
    import tempfile

    api_port, report_port = 3011, 8111
    api_srv, calls = _start_fake_api(api_port)
    cfg = load_config(os.path.join(tempfile.gettempdir(), '__no_such_config__.yml'))
    cfg.update({'owners': ['10001'], 'api': 'http://127.0.0.1:%d' % api_port,
                'report_port': report_port, 'report_token': 'tk',
                'send_zip': True})
    bot = Bot(cfg)
    srv = serve(cfg, bot, port=report_port)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = 'http://127.0.0.1:%d/report' % report_port

    def msg(uid, text, mtype='private'):
        return {'post_type': 'message', 'message_type': mtype,
                'user_id': uid, 'message': text, 'self_id': 1}

    def wait_calls(n, timeout=6.0):
        t = time.time() + timeout
        while time.time() < t and len(calls) < n:
            time.sleep(0.05)
        return list(calls)

    # 1) token 不对 → 403 且无任何调用
    code = _post(base + '?token=bad', msg('10001', '帮助'))
    print('坏 token     -> HTTP %s（期望 403）' % code)
    assert code == 403, code

    # 2) 陌生人私聊 → 不响应
    _post(base + '?token=tk', msg('99999', '帮助'))
    # 3) 群消息 → 不响应
    _post(base + '?token=tk', msg('10001', '帮助', 'group'))
    time.sleep(0.6)
    print('陌生人/群消息 -> 调用数 %d（期望 0）' % len(calls))
    assert len(calls) == 0, calls

    # 4) 主人私聊「帮助」→ 回一条文本
    _post(base + '?token=tk', msg('10001', '帮助'))
    c = wait_calls(1)
    print('主人 帮助     -> %s' % (c[0][0] if c else None))
    assert c and c[0][0] == 'send_private_msg'
    assert 'JMComic 自用机器人' in c[0][1]['message']
    assert c[0][1]['user_id'] == 10001

    # 5) 命令解析
    for raw, want in (('下载 350234', ('下载', '350234')), ('/查询 p350240', ('查询', 'p350240')),
                      ('1234567', ('查询', '1234567')), ('状态', ('状态', '')),
                      ('搜索 狐狐', ('搜索', '狐狐'))):
        got = split_command(raw)
        print('  解析 %-14s -> %s' % (raw, got))
        assert got == want, (raw, got, want)

    # 6) 导出：把临时本子打成 PDF/ZIP 并发文件
    base_dir = os.path.join(tempfile.gettempdir(), 'jmcomic_bot_test')
    album = os.path.join(base_dir, '[JM1]机器人测试')
    d = os.path.join(album, '001-第1話')
    os.makedirs(d, exist_ok=True)
    from PIL import Image
    for p in range(1, 4):
        Image.new('RGB', (240, 340), (30 * p, 70, 110)).save(os.path.join(d, '%05d.jpg' % p))

    n0 = len(calls)
    pdf_ok, pdf_n, pdf_path = jmapp.export_album(album, 'pdf', out_dir=base_dir, name='机器人测试')
    zip_ok, zip_n, zip_path = jmapp.export_album(album, 'zip', out_dir=base_dir, name='机器人测试')
    print('导出 PDF/ZIP  -> %s %d 页 / %s %d 张' % (pdf_ok, pdf_n, zip_ok, zip_n))
    assert pdf_ok and zip_ok and pdf_n == 3 and zip_n == 3

    for p in (pdf_path, zip_path):
        bot.api.send_file(10001, p)
    c = wait_calls(n0 + 2)
    sent = [x for x in c[n0:] if x[0] == 'upload_private_file']
    print('文件发送     -> %d 次：%s' % (len(sent), [os.path.basename(x[1]['file']) for x in sent]))
    assert len(sent) == 2
    assert sent[0][1]['user_id'] == 10001

    print('白名单判定   -> 主人 %s / 陌生人 %s / 群 %s'
          % (bot.is_owner(msg('10001', 'x')), bot.is_owner(msg('99999', 'x')),
             bot.is_owner(msg('10001', 'x', 'group'))))
    assert bot.is_owner(msg('10001', 'x')) is True
    assert bot.is_owner(msg('99999', 'x')) is False
    assert bot.is_owner(msg('10001', 'x', 'group')) is False

    print('SELFTEST-BOT OK')
    srv.shutdown()
    api_srv.shutdown()


def main(argv):
    if '--selftest' in argv:
        selftest()
        return 0

    cfg = load_config()
    if not cfg['owners']:
        log('白名单为空！在 bot_config.yml 的 owners 里填上你自己的 QQ 号，否则机器人不会理任何人。')
    bot = Bot(cfg)
    srv = serve(cfg, bot)
    log('机器人已就绪（只响应私聊 + 白名单）。Ctrl+C 退出。')
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log('收到 Ctrl+C，退出')
    finally:
        srv.shutdown()
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
