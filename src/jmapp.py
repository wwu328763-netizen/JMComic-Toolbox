# -*- coding: utf-8 -*-
"""
JMComic 工具箱 —— 禁漫天堂下载器的图形界面

内核直接调用 jmcomic 的 Python API（不依赖命令行）：
  * 查询车号详情
  * 下载本子 / 章节，按图片张数显示真实进度
  * 实时日志（挂 JmModuleConfig.EXECUTOR_LOG）

界面：Tkinter 原生窗口，深色主题，按系统 DPI 缩放。
"""
import os
import json
import queue
import re
import sys
import tempfile
import threading
import time
import traceback

APP_NAME = 'JMComic 工具箱'
VERSION = '1.1'
HISTORY_MAX = 200


class Cancelled(Exception):
    """用户主动终止下载（在 jmcomic 的日志钩子里抛出）"""


# ---------------------------------------------------------------- 基础

def enable_dpi_awareness():
    """让窗口在高 DPI 屏上清晰（必须在 Tk 创建前调用）"""
    if sys.platform != 'win32':
        return
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)   # Win8.1+
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()        # 老系统
    except Exception:
        pass


def _app_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


APP_DIR = _app_dir()
FALLBACK_DIR = APP_DIR          # 便携：配置就在程序自己所在目录找/生成
COVER_DIR = os.path.join(
    os.environ.get('LOCALAPPDATA') or os.environ.get('TEMP') or APP_DIR,
    'JMComic工具箱', 'covers')

# 默认目录规则：
#   [JM车号] 作者 - 标题 / 001-章节名 / 00001.jpg …
# 带 JM 车号和作者名，章节序号补零（文件夹也能按话序排列），
# 带本子层 → 不同本子章节重名也不会互相覆盖。
# 纯声明式规则，所以命令行版和 GUI 完全一致。
DEFAULT_DIR_RULE = 'Bd/[JM{Aid}] {Aauthor} - {Atitle}/{Pindex:03d}-{Pname}'
IMAGE_EXT = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp'}

OPTION_TEMPLATE = """\
# JMComic 工具箱配置文件（自动生成，可自行修改）
dir_rule:
  base_dir: '%(base)s'
  rule: '%(rule)s'
download:
  threading:
    image: 8
    photo: 4
"""


def _config_file():
    # 依次找：程序目录、回退目录、以及上一级（便于 bot/ 或 tools/ 复用同一份配置）
    candidates = [APP_DIR, FALLBACK_DIR]
    try:
        candidates.append(os.path.dirname(APP_DIR.rstrip('\\/')))
    except Exception:
        pass
    for d in candidates:
        if not d:
            continue
        p = os.path.join(d, 'option.yml')
        if os.path.isfile(p):
            return p
    return None


def build_option():
    """创建 JmOption，返回 (option, 下载目录)"""
    import jmcomic

    cfg = _config_file()
    if cfg is None:
        target_dir = os.path.join(APP_DIR if os.path.isdir(APP_DIR) else FALLBACK_DIR, 'downloads')
        os.makedirs(target_dir, exist_ok=True)
        cfg = os.path.join(os.path.dirname(target_dir), 'option.yml')
        with open(cfg, 'w', encoding='utf-8') as f:
            f.write(OPTION_TEMPLATE % {'base': target_dir, 'rule': DEFAULT_DIR_RULE})

    opt = jmcomic.create_option_by_file(cfg)
    # 图片文件名加前缀：JM车号_页码（纯 ASCII，跨本子不重名，页序不变）
    try:
        opt.decide_image_filename = _prefixed_image_filename
    except Exception:
        pass
    base = str(opt.dir_rule.base_dir)
    if not os.path.isabs(base):
        base = os.path.normpath(os.path.join(APP_DIR, base))
    return opt, base


def _quote_yaml_path(path):
    """YAML 单引号字符串里反斜杠是原义的，直接写即可"""
    return "'" + path + "'"


def _rewrite_base_dir(text, path):
    """把 option.yml 里的 base_dir 改成 path（纯函数，便于测试）

    三种情况都覆盖：已有 base_dir / 有 dir_rule 但没 base_dir / 完全没有 dir_rule
    """
    quoted = _quote_yaml_path(path)

    new, n = re.subn(r'(?m)^([ \t]*base_dir[ \t]*:[ \t]*).*$',
                     lambda m: m.group(1) + quoted, text, count=1)
    if n:
        return new

    if re.search(r'(?m)^dir_rule[ \t]*:[ \t]*$', text):
        return re.sub(r'(?m)^(dir_rule[ \t]*:[ \t]*)$',
                      lambda m: m.group(1) + f'\n  base_dir: {quoted}', text, count=1)

    sep = '' if (not text or text.endswith('\n')) else '\n'
    return text + sep + f'dir_rule:\n  base_dir: {quoted}\n'


def _prefixed_image_filename(image):
    """下载时的图片文件名：JM车号_页码（纯 ASCII 前缀，不含中文）

    例：JM350234_00001
    * 前缀只有 JM 号 —— 不同本子之间不会重名
    * 话序/作者/标题等信息都在文件夹名里，不放文件名
    * 页码在后面 → 页序仍然正确
    """
    base = image.filename_without_suffix
    try:
        aid = str(getattr(image, 'aid', '') or '')
        if not aid:
            return base
        return f'JM{aid}_{base}'
    except Exception:
        return base


def update_option_file(path):
    """把新的保存位置写进 option.yml，返回 (成功?, 文件路径)"""
    cfg = _config_file()
    if cfg is None:
        target_dir = os.path.join(APP_DIR if os.path.isdir(APP_DIR) else FALLBACK_DIR, 'downloads')
        cfg = os.path.join(os.path.dirname(target_dir), 'option.yml')
    try:
        text = ''
        if os.path.isfile(cfg):
            with open(cfg, encoding='utf-8') as f:
                text = f.read()
        with open(cfg, 'w', encoding='utf-8') as f:
            f.write(_rewrite_base_dir(text, path))
        return True, cfg
    except Exception:
        return False, cfg


# ---------------------------------------------------------------- 历史记录

HISTORY_FILE = os.path.join(APP_DIR, 'history.json')


def load_history():
    """读历史记录（失败就当空的，绝不让它影响启动）"""
    try:
        with open(HISTORY_FILE, encoding='utf-8') as f:
            data = json.load(f)
        items = data.get('items') if isinstance(data, dict) else data
        return [it for it in (items or []) if isinstance(it, dict) and it.get('id')]
    except Exception:
        return []


def save_history(items):
    """原子写入，避免中途崩溃把历史写坏"""
    try:
        tmp = HISTORY_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'version': 1, 'items': items}, f, ensure_ascii=False, indent=1)
        os.replace(tmp, HISTORY_FILE)
        return True
    except Exception:
        return False


def fmt_time(ts):
    try:
        dt = time.localtime(float(ts))
        now = time.localtime()
    except Exception:
        return ''
    if (dt.tm_year, dt.tm_yday) == (now.tm_year, now.tm_yday):
        return time.strftime('%H:%M', dt)
    if dt.tm_year == now.tm_year:
        return time.strftime('%m-%d %H:%M', dt)
    return time.strftime('%Y-%m-%d', dt)


def natural_key(name):
    """自然排序：让 001 / 002 / 010 和 2 / 10 都按数字大小排"""
    return [int(t) if t.isdigit() else str(t).lower() for t in re.split(r'(\d+)', str(name))]


def list_images(folder):
    try:
        names = [f for f in os.listdir(folder)
                 if os.path.splitext(f)[1].lower() in IMAGE_EXT]
    except Exception:
        return []
    return [os.path.join(folder, f) for f in sorted(names, key=natural_key)]


def discover_chapters(root_dir):
    """返回 [(章节名, [图片路径, ...]), ...]，按名称自然排序（补零后即为话序）"""
    if not root_dir or not os.path.isdir(root_dir):
        return []
    try:
        subs = [d for d in sorted(os.listdir(root_dir), key=natural_key)
                if os.path.isdir(os.path.join(root_dir, d))]
    except Exception:
        subs = []
    chapters = []
    for d in subs:
        imgs = list_images(os.path.join(root_dir, d))
        if imgs:
            chapters.append((d, imgs))
    if chapters:
        return chapters
    imgs = list_images(root_dir)
    if imgs:
        return [(os.path.basename(os.path.normpath(root_dir)) or '全部', imgs)]
    return []


# ---------------------------------------------------------------- 搜索

SEARCH_KINDS = {'全部': 'site', '本子名称': 'work', '作者': 'author'}
SEARCH_KIND_NAMES = {v: k for k, v in SEARCH_KINDS.items()}


def search_page(client, query, kind='site', page=1, order='', time='', category='all'):
    """按类型调用 jmcomic 的搜索接口

    order: mr 最新 / mv 最多观看 / mp 最多图片 / tf 最多点赞 / tr 最高评分 / md 最多评论
    time : a 全部 / t 今天 / w 本周 / m 本月
    """
    from jmcomic.jm_config import JmMagicConstants as C

    order_by = {
        'mr': C.ORDER_BY_LATEST, 'mv': C.ORDER_BY_VIEW,
        'mp': C.ORDER_BY_PICTURE, 'tf': C.ORDER_BY_LIKE,
        'tr': C.ORDER_BY_SCORE, 'md': C.ORDER_BY_COMMENT,
    }.get(order or 'mr', C.ORDER_BY_LATEST)
    tk = {'a': C.TIME_ALL, 't': C.TIME_TODAY,
          'w': C.TIME_WEEK, 'm': C.TIME_MONTH}.get(time or 'a', C.TIME_ALL)

    fn = {'author': client.search_author,
          'work': client.search_work}.get(kind, client.search_site)
    try:
        return fn(query, page, order_by, tk)
    except TypeError:
        return fn(query, page)


def search_infos(result_page):
    """把 JmSearchPage 转成 [(key, info)]，直接复用左侧结果列表"""
    def join_authors(v):
        if isinstance(v, (list, tuple)):
            return '、'.join(str(x) for x in v if x)
        return str(v or '')

    def cat_title(v):
        if isinstance(v, dict):
            return str(v.get('title') or '')
        return str(v or '')

    out = []
    for item in (getattr(result_page, 'content', None) or []):
        try:
            aid, ainfo = item
        except Exception:
            continue
        if not aid:
            continue
        ainfo = ainfo or {}
        tags = ainfo.get('tags') or []
        if isinstance(tags, str):
            tags = [tags]
        cats = [cat_title(ainfo.get('category')), cat_title(ainfo.get('category_sub'))]
        tagtext = ' / '.join([str(t) for t in list(tags)[:4] + cats if t])
        out.append((f'album:{aid}', {
            'id': str(aid),
            'kind': '本子',
            'title': str(ainfo.get('name') or '').strip(),
            'authors': join_authors(ainfo.get('author')),
            'pages': '—',
            'chapters': '—',
            'tags': tagtext or '—',
        }))
    return out


# ---------------------------------------------------------------- 删除到回收站

def trash_path(path):
    """把文件/文件夹移到回收站（可恢复），返回 (成功?, 说明)"""
    if sys.platform != 'win32':
        return False, '只支持 Windows'
    path = os.path.abspath(path)
    if not os.path.exists(path):
        return False, '路径不存在'
    path = path.rstrip('\\/')
    try:
        import ctypes
        from ctypes import wintypes

        class SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [
                ('hwnd', wintypes.HWND),
                ('wFunc', wintypes.UINT),
                ('pFrom', wintypes.LPCWSTR),
                ('pTo', wintypes.LPCWSTR),
                ('fFlags', ctypes.c_ushort),
                ('fAnyOperationsAborted', wintypes.BOOL),
                ('hNameMappings', ctypes.c_void_p),
                ('lpszProgressTitle', wintypes.LPCWSTR),
            ]

        FO_DELETE = 3
        FOF_SILENT = 0x0004
        FOF_NOCONFIRMATION = 0x0010
        FOF_ALLOWUNDO = 0x0040
        FOF_NOCONFIRMMKDIR = 0x0200
        FOF_NOERRORUI = 0x0400

        op = SHFILEOPSTRUCTW()
        op.hwnd = None
        op.wFunc = FO_DELETE
        op.pFrom = path + '\0'          # ctypes 会再补一个 \0 → 双 null 结尾
        op.pTo = None
        op.fFlags = (FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT
                     | FOF_NOERRORUI | FOF_NOCONFIRMMKDIR)
        rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
        if rc == 0 and not op.fAnyOperationsAborted and not os.path.exists(path):
            return True, '已移到回收站'
        if rc == 0 and os.path.exists(path):
            return False, '操作被取消'
        return False, f'系统返回码 {rc}'
    except Exception as e:
        return False, str(e)


# ---------------------------------------------------------------- 导出 PDF / ZIP
EXPORT_DIRNAME = 'exports'


def _safe_filename(text, fallback='本子'):
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', '_', str(text or '')).strip(' .')
    return (name or fallback)[:80]


def export_dir(base_dir=None):
    """导出目录：保存位置下的 exports/（没有就建）"""
    root = base_dir or APP_DIR
    path = os.path.join(root, EXPORT_DIRNAME)
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        path = os.path.join(tempfile.gettempdir(), EXPORT_DIRNAME)
        os.makedirs(path, exist_ok=True)
    return path


def _album_images(root_dir):
    """按章节顺序收集要导出的图片"""
    out = []
    try:
        for _name, pages in discover_chapters(root_dir):
            out.extend(pages)
    except Exception:
        pass
    return out


def export_pdf(root_dir, out_path):
    """把本子/章节目录导出成 PDF，返回 (成功?, 页数, 输出路径或错误)"""
    try:
        from PIL import Image
        files = _album_images(root_dir)
        if not files:
            return False, 0, '这个目录里没有图片'
        pages = []
        for p in files:
            try:
                pages.append(Image.open(p).convert('RGB'))
            except Exception:
                continue
        if not pages:
            return False, 0, '图片解码失败'
        d = os.path.dirname(out_path)
        if d:
            os.makedirs(d, exist_ok=True)
        pages[0].save(out_path, 'PDF', resolution=100.0,
                      save_all=True, append_images=pages[1:])
        return True, len(pages), out_path
    except Exception as e:
        return False, 0, str(e)


def export_zip(root_dir, out_path):
    """把本子目录压成 zip（保留章节子目录结构），返回 (成功?, 文件数, 输出路径或错误)"""
    try:
        import zipfile
        files = _album_images(root_dir)
        if not files:
            return False, 0, '这个目录里没有图片'
        d = os.path.dirname(out_path)
        if d:
            os.makedirs(d, exist_ok=True)
        with zipfile.ZipFile(out_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for p in files:
                arc = os.path.relpath(p, root_dir).replace('\\', '/')
                zf.write(p, arc)
        return True, len(files), out_path
    except Exception as e:
        return False, 0, str(e)


def export_album(root_dir, fmt='pdf', out_dir=None, name=None):
    """便捷封装：fmt in ('pdf','zip') → (成功?, 数量, 路径或错误)"""
    if not root_dir or not os.path.isdir(root_dir):
        return False, 0, '找不到这个本子的目录'
    out_dir = out_dir or export_dir()
    stem = _safe_filename(name or os.path.basename(os.path.normpath(root_dir)))
    if fmt == 'zip':
        return export_zip(root_dir, os.path.join(out_dir, stem + '.zip'))
    return export_pdf(root_dir, os.path.join(out_dir, stem + '.pdf'))


# ---------------------------------------------------------------- 动画

ANIM_ON = True              # 全局动画开关（界面上可切换）
ANIM_INTERVAL = 16          # 帧间隔(ms)，约 60fps
CARD_BG = (15, 17, 22)      # 卡片底色 RGB，用于图片淡入


def ease_out(t):
    t = 0.0 if t < 0 else (1.0 if t > 1 else t)
    return 1 - (1 - t) ** 3


def lerp(a, b, t):
    return a + (b - a) * t


def lerp_color(c1, c2, t):
    """'#rrggbb' 之间插值"""
    try:
        a = tuple(int(c1[i:i + 2], 16) for i in (1, 3, 5))
        b = tuple(int(c2[i:i + 2], 16) for i in (1, 3, 5))
        return '#%02x%02x%02x' % tuple(int(round(lerp(a[i], b[i], t))) for i in range(3))
    except Exception:
        return c2


def hex_rgb(c, default=(15, 17, 22)):
    """'#rrggbb' → (r, g, b)"""
    try:
        return tuple(int(c[i:i + 2], 16) for i in (1, 3, 5))
    except Exception:
        return default


def scan_downloads(base_dir, history=None):
    """扫描下载目录，列出已下载的本子/章节（兼容没有 [JM车号] 前缀的旧结构）"""
    out = []
    if not base_dir or not os.path.isdir(base_dir):
        return out

    by_dir = {}
    for it in (history or []):
        d = it.get('dir')
        if d:
            by_dir[os.path.normcase(os.path.normpath(d))] = it

    def img_names(folder):
        try:
            return sorted([f for f in os.listdir(folder)
                           if os.path.splitext(f)[1].lower() in IMAGE_EXT], key=natural_key)
        except Exception:
            return []

    try:
        children = [d for d in os.listdir(base_dir) if os.path.isdir(os.path.join(base_dir, d))]
    except Exception:
        children = []

    for name in children:
        full = os.path.join(base_dir, name)
        own = img_names(full)
        subs = []
        try:
            subs = [x for x in os.listdir(full) if os.path.isdir(os.path.join(full, x))]
        except Exception:
            pass
        chapters = []
        for s in sorted(subs, key=natural_key):
            imgs = img_names(os.path.join(full, s))
            if imgs:
                chapters.append((s, imgs))

        total = len(own) + sum(len(v) for _, v in chapters)
        if total == 0:
            continue                      # 没有图片的目录直接跳过
        hist = by_dir.get(os.path.normcase(os.path.normpath(full))) or {}
        first_img = (os.path.join(full, own[0]) if own else
                     os.path.join(full, chapters[0][0], chapters[0][1][0]) if chapters else '')
        try:
            mtime = os.path.getmtime(full)
        except Exception:
            mtime = 0

        out.append({
            'key': 'local:' + full,
            'kind': '本地',
            'id': hist.get('id') or '',
            'title': hist.get('title') or name,
            'authors': hist.get('authors') or '',
            'tags': hist.get('tags') or '',
            'pages': total,
            'chapters': len(chapters) if chapters else 1,
            'cover': hist.get('cover') or first_img,
            'dir': full,
            'action': '已下载',
            'ts': mtime,
            '_thumb': first_img,
        })

    out.sort(key=lambda e: e.get('ts') or 0, reverse=True)
    return out


ID_RE = re.compile(r'(p)?(\d+)', re.IGNORECASE)

def parse_ids(text):
    """把任意文本解析成 [(kind, id)]，kind 为 album/photo，p 前缀表示章节"""
    out, seen = [], set()
    for m in ID_RE.finditer(text or ''):
        kind = 'photo' if m.group(1) else 'album'
        i = m.group(2).lstrip('0') or '0'
        if i != '0' and (kind, i) not in seen:
            seen.add((kind, i))
            out.append((kind, i))
    return out


_NUM_ONLY_RE = re.compile(r'[\s0-9pP]+')
_URL_ID_RE = re.compile(r'/(album|photos?)/(\d{3,9})', re.I)
_P_ID_RE = re.compile(r'(?<![0-9A-Za-z])[pP](\d{3,9})(?![0-9])')
_JM_ID_RE = re.compile(r'(?<![0-9A-Za-z])[Jj][Mm](\d{3,9})(?![0-9])')
_NUM_ID_RE = re.compile(r'(?<![0-9A-Za-z])(\d{5,9})(?![0-9A-Za-z])')
_ANY_ID_RE = re.compile(r'(?<![0-9A-Za-z])([pP]?)(\d{3,9})(?![0-9A-Za-z])')


def extract_ids_smart(text):
    """更聪明的车号提取，返回 [(kind, id)]

    依次尝试：
      1. 站内链接    /album/350234 → 本子；/photos/350240 → 章节
      2. p 前缀      p350240 → 章节
      3. JM 前缀     JM350234 → 本子
      4. 纯数字文本  350234 / 123 p456（3~9 位都认）
      5. 含车号的文字 取其中 5~9 位的数字（避免把年份 2024 当车号）
    都失败才回退到旧的宽松规则。
    """
    t = str(text or '')
    out, seen = [], set()

    def add(kind, i):
        i = str(i).lstrip('0') or '0'
        if i != '0' and (kind, i) not in seen:
            seen.add((kind, i))
            out.append((kind, i))

    # 1) 链接
    for m in _URL_ID_RE.finditer(t):
        add('photo' if m.group(1).lower().startswith('photo') else 'album', m.group(2))
    if out:
        return out
    # 2) 纯数字（可多个、可带 p）
    if _NUM_ONLY_RE.fullmatch(t.strip()):
        for m in _ANY_ID_RE.finditer(t):
            add('photo' if m.group(1) else 'album', m.group(2))
        if out:
            return out
    # 3) p 前缀
    for m in _P_ID_RE.finditer(t):
        add('photo', m.group(1))
    # 3) JM 前缀
    for m in _JM_ID_RE.finditer(t):
        add('album', m.group(1))
    if out:
        return out
    # 5) 文字里夹带的 5~9 位数字
    for m in _NUM_ID_RE.finditer(t):
        add('album', m.group(1))
    if out:
        return out
    return parse_ids(t)


def looks_like_id(text):
    """这段文字里能不能提取出车号"""
    return bool(extract_ids_smart(text))


def resolve_item(client, kind, i):
    """把用户给的 id 解析成 (kind, detail, 命中的章节名)

    规则：
      * 先按本子查 —— 命中就是本子（本来就有全部章节）
      * 本子查不到就按章节查 —— 命中后如果它属于多章节本子，
        自动升到所属本子，于是「搜到其中一话 = 拿到所有话」
      * 写了 p 前缀但其实是本子号，也能反向兜底
    """
    album_err = None
    if kind == 'album':
        try:
            return 'album', client.get_album_detail(i), None
        except Exception as e:
            album_err = e
    try:
        photo = client.get_photo_detail(i)
    except Exception as e:
        if album_err is not None:
            raise album_err
        try:                                  # p 前缀但其实是本子号
            return 'album', client.get_album_detail(i), None
        except Exception:
            raise e

    if not getattr(photo, 'is_single_album', False):
        album_id = str(getattr(photo, 'album_id', '') or '')
        if album_id:
            try:
                return 'album', client.get_album_detail(album_id), photo.name
            except Exception:
                pass
    return 'photo', photo, None


def detail_info(detail):
    """把 album/photo 详情统一成要展示的字段"""
    is_album = hasattr(detail, 'page_count')
    authors = getattr(detail, 'authors', None) or []
    if not authors and hasattr(detail, 'author'):
        authors = [detail.author]
    info = {
        'id': getattr(detail, 'id', '?'),
        'title': str(getattr(detail, 'name', '?')).strip(),
        'authors': '、'.join([a for a in authors if a]) or '未知',
        'tags': ' / '.join(getattr(detail, 'tags', None) or []) or '—',
    }
    if is_album:
        info['pages'] = int(detail.page_count)
        info['chapters'] = len(detail.episode_list)
        info['kind'] = '本子'
    else:
        info['pages'] = len(detail)
        info['chapters'] = 1
        info['kind'] = '章节'
    return info


# ---------------------------------------------------------------- 主题

THEME_FOLLOW = '跟随系统'

THEMES = {
    '深色': {
        'BG': '#14161c', 'CARD': '#1d2028', 'CARD2': '#22262f', 'BORDER': '#2c313d',
        'FG': '#e9ecf3', 'MUTED': '#8a93a6',
        'ACCENT': '#4c8dff', 'ACCENT_HI': '#6ba0ff',
        'GREEN': '#35c46a', 'RED': '#ff6b6b', 'YELLOW': '#e8c05a',
        'FIELD': '#0f1116', 'ON_ACCENT': '#ffffff', 'HOVER': '#262b36',
        'SCROLL': '#3b4353', 'TROUGH': '#262c39', 'VIEWER_BG': '#0b0c10',
        'DANGER_BG': '#3a1f26', 'DANGER_HI': '#5d2b36',
        'DANGER_FG': '#ff8fa0', 'DANGER_FG_HI': '#ffb3bf',
    },
    '浅色': {
        'BG': '#eef1f6', 'CARD': '#ffffff', 'CARD2': '#f3f6fb', 'BORDER': '#d5dbe6',
        'FG': '#1b2230', 'MUTED': '#6b7484',
        'ACCENT': '#2f6fed', 'ACCENT_HI': '#4d86f0',
        'GREEN': '#12935f', 'RED': '#d93b4f', 'YELLOW': '#b8860b',
        'FIELD': '#ffffff', 'ON_ACCENT': '#ffffff', 'HOVER': '#e8eefb',
        'SCROLL': '#c3cad6', 'TROUGH': '#e3e8f0', 'VIEWER_BG': '#e9edf3',
        'DANGER_BG': '#fdecef', 'DANGER_HI': '#fbd9e0',
        'DANGER_FG': '#c0273c', 'DANGER_FG_HI': '#8f1226',
    },
    '霓虹': {
        'BG': '#0a0713', 'CARD': '#150e28', 'CARD2': '#1d1435', 'BORDER': '#3a2a63',
        'FG': '#ece7ff', 'MUTED': '#9c8fc7',
        'ACCENT': '#b06bff', 'ACCENT_HI': '#c98dff',
        'GREEN': '#2ff0c0', 'RED': '#ff5c9d', 'YELLOW': '#ffd45e',
        'FIELD': '#0d0a1c', 'ON_ACCENT': '#ffffff', 'HOVER': '#271b47',
        'SCROLL': '#4a3679', 'TROUGH': '#2a1f4a', 'VIEWER_BG': '#070512',
        'DANGER_BG': '#3a1030', 'DANGER_HI': '#58204a',
        'DANGER_FG': '#ff8fd0', 'DANGER_FG_HI': '#ffc2e6',
    },
    '樱花': {
        'BG': '#fdf4f6', 'CARD': '#fffafb', 'CARD2': '#fbe9ef', 'BORDER': '#f0cfd9',
        'FG': '#3b2a31', 'MUTED': '#9a7281',
        'ACCENT': '#e84f7a', 'ACCENT_HI': '#f06d93',
        'GREEN': '#2fa46e', 'RED': '#d9425c', 'YELLOW': '#c78b1e',
        'FIELD': '#fffdfd', 'ON_ACCENT': '#ffffff', 'HOVER': '#f9dde7',
        'SCROLL': '#e3bcc9', 'TROUGH': '#f7e3ea', 'VIEWER_BG': '#f6e8ed',
        'DANGER_BG': '#fde8ec', 'DANGER_HI': '#f9d2db',
        'DANGER_FG': '#bb2540', 'DANGER_FG_HI': '#8f132a',
    },
}


def theme_names():
    return list(THEMES.keys()) + [THEME_FOLLOW]


def system_dark():
    """Windows 是否处于深色模式（读注册表，失败按深色）"""
    if sys.platform != 'win32':
        return True
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize')
        try:
            v, _ = winreg.QueryValueEx(k, 'AppsUseLightTheme')
        finally:
            winreg.CloseKey(k)
        return int(v) == 0
    except Exception:
        return True


def resolve_theme(name):
    """把「跟随系统」解析成具体主题名"""
    if name == THEME_FOLLOW:
        return '深色' if system_dark() else '浅色'
    return name if name in THEMES else '深色'


def _ui_settings_path():
    return os.path.join(APP_DIR, 'ui.json')


def load_ui_settings():
    try:
        with open(_ui_settings_path(), encoding='utf-8-sig') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_ui_settings(d):
    try:
        p = _ui_settings_path()
        with open(p + '.tmp', 'w', encoding='utf-8') as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.replace(p + '.tmp', p)
    except Exception:
        pass


# ---------------------------------------------------------------- 界面

class JMApp:

    BG = '#14161c'
    CARD = '#1d2028'
    CARD2 = '#22262f'
    BORDER = '#2c313d'
    FG = '#e9ecf3'
    MUTED = '#8a93a6'
    ACCENT = '#4c8dff'
    ACCENT_HI = '#6ba0ff'
    GREEN = '#35c46a'
    RED = '#ff6b6b'
    YELLOW = '#e8c05a'

    def __init__(self):
        import tkinter as tk
        from tkinter import ttk

        enable_dpi_awareness()

        self.tk = tk
        self.ttk = ttk
        self.q = queue.Queue()
        self.busy = False
        self.done_images = 0
        self.total_images = 0
        self.base_dir = os.path.join(FALLBACK_DIR, 'downloads')
        self._base_dirty = False      # 用户是否手动改过保存位置
        self._cover_photo = None      # 必须留引用，否则被 GC 掉图就没了
        self._cover_path = None
        self._cover_gen = 0           # 封面淡入的代次，用于作废过期帧
        self._cancel = threading.Event()   # 终止下载信号
        self.results = {}             # key -> info
        self.result_rows = {}         # key -> (row, thumb_label, btn)
        self.result_covers = {}       # key -> cover 文件路径
        self._row_photos = {}         # key -> 缩略图引用
        self.selected_key = None
        self.history = load_history()  # 查询/下载过的记录，新的在前
        self.local = []               # 已下载（扫描保存目录得到）
        self.view_mode = 'results'
        self._readers = []            # 打开的阅读器窗口
        self.toast_frame = None

        # 主题（记住上次选择）
        self.ui_settings = load_ui_settings()
        self.theme_name = self.ui_settings.get('theme') or '深色'
        self.theme_active = resolve_theme(self.theme_name)

        # 界面状态变量（放在 __init__，切主题重建界面时不会丢）
        self.root = tk.Tk()
        self.root.title(f'{APP_NAME}  v{VERSION}')
        self._apply_palette(self.theme_name)
        self.root.configure(bg=self.BG)

        self.var_title = tk.StringVar(value='标题：—')
        self.var_meta = tk.StringVar(value='车号：—    作者：—    页数：—    章节：—')
        self.var_tags = tk.StringVar(value='标签：—')
        self.var_status = tk.StringVar(value='就绪')
        self.var_pct = tk.StringVar(value='')
        self.var_foot = tk.StringVar(value='保存位置：加载中 ...')
        self.var_anim = tk.StringVar(value='动画 开' if ANIM_ON else '动画 关')
        self.var_theme = tk.StringVar(value=self.theme_name)

        # DPI 缩放：字体用 tk scaling 自动放大，间距/尺寸用 px() 手动对齐
        try:
            dpi = float(self.root.winfo_fpixels('1i'))
        except Exception:
            dpi = 96.0
        self.scale = max(1.0, round(dpi / 96.0, 2))
        try:
            self.root.tk.call('tk', 'scaling', dpi / 72.0)
        except Exception:
            pass

        w, h = self._fit_to_work_area()
        self.root.minsize(self.px(760), self.px(560))
        self._center(w, h)
        self._set_icon()

        self.style = ttk.Style()
        try:
            self.style.theme_use('clam')
        except Exception:
            pass
        self._setup_styles()

        self._build_ui()
        self._install_log_hook()
        self._apply_dark_titlebar()
        # 滚轮：Windows 把 WM_MOUSEWHEEL 发给「获得焦点的窗口」，而左侧列表(Canvas)
        # 和日志(Text) 平时都拿不到焦点，所以统一绑到主窗口，再按光标位置分发
        self.root.bind('<MouseWheel>', self._on_main_wheel)
        self.root.bind('<Delete>', self._on_delete_key)
        self.root.after(80, self._drain)
        self.root.after(60, lambda: self._fade_in(self.root))
        # 后台读一次配置，把保存位置显示出来
        threading.Thread(target=self._load_base_dir, daemon=True).start()

    # -------------------------------------------------- 主题

    def _apply_palette(self, name):
        """把主题配色写到实例属性上（类属性是深色默认值）"""
        self.theme_active = resolve_theme(name)
        pal = dict(THEMES.get(self.theme_active, THEMES['深色']))
        # 派生色：分区块、正文强调底、分隔线
        pal.setdefault('BAND', lerp_color(pal['CARD'], pal['CARD2'], 0.55))
        pal.setdefault('CHIP', lerp_color(pal['FIELD'], pal['ACCENT'], 0.13))
        pal.setdefault('SEP', pal['BORDER'])
        for k, v in pal.items():
            setattr(self, k, v)

    def _setup_styles(self):
        """ttk 样式（进度条 / 滚动条 / 下拉框），切主题时会重新调用"""
        self.style.configure(
            'JM.Horizontal.TProgressbar',
            troughcolor=self.TROUGH, background=self.ACCENT,
            bordercolor=self.BORDER, lightcolor=self.ACCENT,
            darkcolor=self.ACCENT, thickness=self.px(10),
        )
        self.style.configure(
            'JM.Vertical.TScrollbar',
            troughcolor=self.CARD, background=self.SCROLL, bordercolor=self.CARD,
            arrowcolor=self.MUTED, darkcolor=self.SCROLL, lightcolor=self.SCROLL,
            relief='flat', arrowsize=self.px(14),
        )
        self.style.map('JM.Vertical.TScrollbar', background=[('active', self.ACCENT)])
        self.style.configure('JM.TCombobox', fieldbackground=self.FIELD,
                             background=self.CARD2, foreground=self.FG,
                             arrowcolor=self.MUTED, bordercolor=self.BORDER)
        self.style.map('JM.TCombobox',
                       fieldbackground=[('readonly', self.FIELD)],
                       foreground=[('readonly', self.FG)],
                       background=[('readonly', self.CARD2)],
                       selectbackground=[('readonly', self.FIELD)],
                       selectforeground=[('readonly', self.FG)])
        try:
            self.root.option_add('*TCombobox*Listbox.background', self.FIELD)
            self.root.option_add('*TCombobox*Listbox.foreground', self.FG)
            self.root.option_add('*TCombobox*Listbox.selectBackground', self.ACCENT)
            self.root.option_add('*TCombobox*Listbox.selectForeground', self.ON_ACCENT)
        except Exception:
            pass

    def set_theme(self, name):
        """切换主题：写配置 → 换调色板 → 重建界面（带淡入淡出）"""
        if name not in THEMES and name != THEME_FOLLOW:
            return
        resolved = resolve_theme(name)
        if name == self.theme_name and resolved == self.theme_active:
            return
        self.theme_name = name
        self.ui_settings['theme'] = name
        save_ui_settings(self.ui_settings)
        self._apply_palette(name)
        self.log(f'已切换主题：{name}' + (f'（{resolved}）' if name == THEME_FOLLOW else ''),
                 'dim')
        self._rebuild_ui()

    def on_theme_pick(self, _e=None):
        try:
            self.set_theme(self.var_theme.get())
        except Exception as e:
            self.log(f'切换主题失败：{e}', 'err')

    def _rebuild_ui(self):
        """按当前调色板重建整个界面，并恢复状态"""
        log_text = ''
        try:
            log_text = self.text.get('1.0', 'end')
        except Exception:
            pass

        def do():
            # 用「重建这一刻」的状态恢复（重建期间用户可能已经切过页签）
            mode_now = self.view_mode
            selected_now = self.selected_key
            for w in list(self.root.winfo_children()):
                w.destroy()
            self.result_rows.clear()
            self.result_covers.clear()
            self._row_photos.clear()
            self._cover_photo = None
            self.root.configure(bg=self.BG)
            self._setup_styles()
            self._build_ui()
            self._apply_dark_titlebar()
            try:
                self.text.configure(state='normal')
                self.text.insert('1.0', log_text)
                self.text.configure(state='disabled')
            except Exception:
                pass
            self.view_mode = mode_now
            self.selected_key = selected_now
            self._render_view()
            self._refresh_left_header()
            if selected_now:
                self._show_detail(selected_now)
            self._sync_anim_button()
            # 本地书库可能在重建期间正在扫描 → 补一次，避免列表空着
            if self.view_mode == 'local':
                threading.Thread(target=self._load_local, daemon=True).start()

        if not ANIM_ON:
            do()
            return

        def step(t):
            try:
                self.root.attributes('-alpha', max(0.3, 1.0 - 0.75 * t))
            except Exception:
                pass

        def mid():
            do()
            self._fade_in(self.root, frames=8)

        self._anim(4, step, done=mid, interval=12, first_now=False)

    def toggle_log(self):
        """运行日志抽屉：收起 / 展开（默认收起，界面更清爽）"""
        self.log_open = not getattr(self, 'log_open', False)
        try:
            if self.log_open:
                self.logwrap.pack(side='left', fill='both', expand=True,
                                  padx=(self.px(12), 0))
                self.reswrap.pack_configure(fill='y', expand=False)
                self.btn_log.configure(text='日志 ▴')
            else:
                self.logwrap.pack_forget()
                self.reswrap.pack_configure(fill='both', expand=True)
                self.btn_log.configure(text='日志 ▾')
        except Exception:
            pass

    def _sync_anim_button(self):
        try:
            self.var_anim.set('动画 开' if ANIM_ON else '动画 关')
            self.btn_anim.configure(fg=self.FG if ANIM_ON else self.MUTED)
            self.var_theme.set(self.theme_name)
        except Exception:
            pass

    # -------------------------------------------------- 小提示条（toast）

    def toast(self, msg, kind='ok'):
        """右下角滑入的小提示，2.6 秒后自动淡出"""
        tk = self.tk
        if not ANIM_ON:
            self.log(msg, kind)
            return
        try:
            if self.toast_frame is not None:
                self.toast_frame.destroy()
        except Exception:
            pass
        edge = self.ACCENT if kind == 'ok' else (self.RED if kind == 'err' else self.BORDER)
        try:
            f = tk.Frame(self.root, bg=self.CARD, highlightbackground=edge,
                         highlightthickness=1)
            tk.Label(f, text=msg, bg=self.CARD, fg=self.FG,
                     font=self._font(10, True), padx=self.px(14),
                     pady=self.px(9)).pack()
            self.toast_frame = f
            f.place(relx=1.0, rely=1.0, x=-self.px(26), y=self.px(20), anchor='se')

            def step(t):
                try:
                    f.place_configure(y=int(self.px(20) - self.px(20) * t))
                except Exception:
                    pass

            self._anim(5, step, first_now=False)
            self.root.after(2600, self._toast_hide)
        except Exception:
            self.log(msg, kind)

    def _toast_hide(self):
        f = self.toast_frame
        if f is None:
            return

        def step(t):
            try:
                f.place_configure(y=int(self.px(20) * t))
            except Exception:
                pass

        def done():
            try:
                f.destroy()
            except Exception:
                pass
            self.toast_frame = None

        self._anim(4, step, done=done, first_now=False)

    # -------------------------------------------------- 小工具

    def _load_base_dir(self):
        try:
            _opt, base = build_option()
            if not self._base_dirty:      # 用户已经手动改过就别覆盖
                self.q.put(('base', base))
        except Exception as e:
            self.q.put(('log', (f'读取配置失败：{e}', 'err')))

    def _apply_dark_titlebar(self, win=None):
        """Win10/11 深色标题栏，和界面主题统一"""
        if sys.platform != 'win32':
            return
        win = win or self.root
        try:
            import ctypes
            win.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
            value = ctypes.c_int(0 if self.theme_active in ('浅色', '樱花') else 1)
            for attr in (20, 19):   # DWMWA_USE_IMMERSIVE_DARK_MODE（新老系统编号不同）
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(value), ctypes.sizeof(value))
        except Exception:
            pass

    def px(self, n):
        """逻辑像素 → 当前 DPI 下的实际像素"""
        return int(round(n * self.scale))

    def _work_area(self):
        """可用工作区（逻辑坐标，已排除任务栏）"""
        try:
            import ctypes

            class RECT(ctypes.Structure):
                _fields_ = [('left', ctypes.c_long), ('top', ctypes.c_long),
                            ('right', ctypes.c_long), ('bottom', ctypes.c_long)]

            r = RECT()
            if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0):
                s = self.scale
                return (r.left / s, r.top / s, (r.right - r.left) / s, (r.bottom - r.top) / s)
        except Exception:
            pass
        s = self.scale
        return 0.0, 0.0, self.root.winfo_screenwidth() / s, self.root.winfo_screenheight() / s

    def _fit_to_work_area(self):
        """窗口默认尺寸按屏幕可用工作区自适应，不越出任务栏

        注意单位：_work_area() 返回逻辑像素，px() 换算成物理像素，
        两者不能直接相减（否则窗口会比设计的小一圈）。
        """
        wx, wy, ww, wh = self._work_area()
        self._work = (wx, wy, ww, wh)
        w_log = max(760, min(920, int(ww) - 20))
        h_log = max(560, min(760, int(wh) - 36))
        return self.px(w_log), self.px(h_log)

    def _center(self, w, h):
        wx, wy, ww, wh = getattr(self, '_work', (0, 0,
                                                 self.root.winfo_screenwidth() / self.scale,
                                                 self.root.winfo_screenheight() / self.scale))
        x = int(wx + max(0, (ww - w) / 2))
        y = int(wy + max(0, (wh - h) / 2))
        self.root.geometry(f'{int(w)}x{int(h)}+{x}+{y}')

    def _set_icon(self):
        ico = os.path.join(APP_DIR, 'icon.ico')
        try:
            if os.path.isfile(ico):
                self.root.iconbitmap(ico)
        except Exception:
            pass

    def _font(self, size=10, bold=False):
        return ('Microsoft YaHei UI', size, 'bold') if bold else ('Microsoft YaHei UI', size)

    # -------------------------------------------------- 动画驱动

    def _anim(self, frames, step, done=None, interval=ANIM_INTERVAL, first_now=True):
        """跑一段补间动画

        step(t)：t 已经过 ease-out 缓动
        first_now：立刻先画第一帧（保证状态马上可见，测试也不用等）
        """
        if not ANIM_ON or frames <= 1:
            try:
                step(1.0)
            finally:
                if done:
                    done()
            return

        if first_now:
            try:
                step(ease_out(1.0 / frames))
            except Exception:
                pass

        state = {'i': 0}

        def tick():
            state['i'] += 1
            i = state['i']
            if i >= frames:
                try:
                    step(1.0)
                except Exception:
                    pass
                if done:
                    done()
                return
            try:
                step(ease_out(i / frames))
            except Exception:
                if done:
                    done()
                return
            try:
                self.root.after(interval, tick)
            except Exception:
                if done:
                    done()

        self.root.after(interval, tick)

    def toggle_anim(self):
        global ANIM_ON
        ANIM_ON = not ANIM_ON
        self.var_anim.set('动画 开' if ANIM_ON else '动画 关')
        self.btn_anim.configure(fg=self.FG if ANIM_ON else self.MUTED)
        self.log(f'动画已{"开启" if ANIM_ON else "关闭"}', 'dim')

    def _fade_in(self, win=None, frames=9):
        """窗口淡入"""
        win = win or self.root
        if not ANIM_ON:
            return
        try:
            win.attributes('-alpha', 0.05)
        except Exception:
            return

        def step(t):
            try:
                win.attributes('-alpha', max(0.05, t))
            except Exception:
                pass

        def done():
            try:
                win.attributes('-alpha', 1.0)
            except Exception:
                pass

        self._anim(frames, step, done)

    def _button(self, parent, text, command, kind='primary'):
        tk = self.tk
        bg, hi, fg = {
            'primary': (self.ACCENT, self.ACCENT_HI, self.ON_ACCENT),
            'ghost': (self.CARD2, self.BORDER, self.FG),
            'flat': (self.CARD, self.CARD2, self.MUTED),
            'danger': (self.DANGER_BG, self.DANGER_HI, self.DANGER_FG),
        }[kind]
        b = tk.Button(
            parent, text=text, command=command, font=self._font(10, True),
            bg=bg, fg=fg, activebackground=hi, activeforeground=fg,
            relief='flat', bd=0, padx=self.px(15), pady=self.px(7),
            cursor='hand2', highlightthickness=0,
        )

        def fade_bg(target):
            try:
                start = str(b['bg'])
            except Exception:
                return
            if not ANIM_ON or start == target:
                try:
                    b.configure(bg=target)
                except Exception:
                    pass
                return

            def step(t):
                try:
                    b.configure(bg=lerp_color(start, target, t))
                except Exception:
                    pass

            self._anim(5, step, first_now=False)

        b.bind('<Enter>', lambda e: fade_bg(hi if str(b['state']) == 'normal' else bg))
        b.bind('<Leave>', lambda e: fade_bg(bg))
        return b

    def _placeholder(self, entry, text):
        """给输入框加灰色占位文字，返回可读可写的包装"""
        state = {'on': False}

        fg_normal = self.FG
        fg_placeholder = self.MUTED

        def show(*_):
            if not entry.get():
                entry.insert(0, text)
                entry.configure(fg=fg_placeholder)
                state['on'] = True

        def hide(*_):
            if state['on']:
                entry.delete(0, 'end')
                entry.configure(fg=fg_normal)
                state['on'] = False

        entry.bind('<FocusIn>', hide)
        entry.bind('<FocusOut>', show)
        show()

        class Box:
            def get(self):
                return '' if state['on'] else entry.get().strip()

            def set(self, value):
                state['on'] = False
                entry.delete(0, 'end')
                entry.configure(fg=fg_normal)
                if value:
                    entry.insert(0, value)

        return Box()

    def _install_placeholder(self):
        self._entry_box = self._placeholder(self.entry, '输入车号，例如 123456')

    def _entry_text(self):
        return self._entry_box.get()

    def _search_text(self):
        return self._search_box.get()

    # -------------------------------------------------- 布局

    def _build_ui(self):
        tk = self.tk
        ttk = self.ttk
        pad = self.px(18)

        # ---------- 顶部：品牌（左）+ 工具（右侧，低视觉权重）
        head = tk.Frame(self.root, bg=self.BG)
        head.pack(fill='x', padx=pad, pady=(self.px(18), self.px(2)))

        brand = tk.Frame(head, bg=self.BG)
        brand.pack(side='left')
        tk.Frame(brand, bg=self.ACCENT, width=self.px(6),
                 height=self.px(30)).pack(side='left', padx=(0, self.px(12)))
        tk.Label(brand, text=APP_NAME, bg=self.BG, fg=self.FG,
                 font=self._font(20, True)).pack(side='left')
        tk.Label(brand, text=f'v{VERSION}', bg=self.BG, fg=self.ACCENT,
                 font=self._font(9, True)).pack(side='left', padx=(self.px(7), 0),
                                                pady=(self.px(9), 0))

        def flat(parent, text, cmd, var=None):
            return tk.Button(parent, text=text, command=cmd, textvariable=var,
                             font=self._font(9), bg=self.BG, fg=self.MUTED,
                             activebackground=self.CARD, activeforeground=self.FG,
                             relief='flat', bd=0, padx=self.px(9), pady=self.px(5),
                             cursor='hand2', highlightthickness=0)

        tools = tk.Frame(head, bg=self.BG)
        tools.pack(side='right')
        self.theme_combo = ttk.Combobox(
            tools, textvariable=self.var_theme, values=theme_names(),
            state='readonly', width=8, font=self._font(9), style='JM.TCombobox')
        self.theme_combo.pack(side='right')
        self.theme_combo.bind('<<ComboboxSelected>>', self.on_theme_pick)
        self.btn_folder = flat(tools, '打开文件夹', self.open_folder)
        self.btn_folder.pack(side='right', padx=(0, self.px(2)))
        self.btn_dir = flat(tools, '保存位置', self.choose_folder)
        self.btn_dir.pack(side='right', padx=(0, self.px(2)))
        self.btn_anim = flat(tools, None, self.toggle_anim, self.var_anim)
        self.btn_anim.pack(side='right')

        # ---------- 主输入卡（无边框、大留白）
        card = tk.Frame(self.root, bg=self.CARD)
        card.pack(fill='x', padx=pad, pady=(self.px(16), self.px(10)))
        card.columnconfigure(1, weight=1)

        self.entry = tk.Entry(card, font=self._font(14), bg=self.FIELD, fg=self.FG,
                              insertbackground=self.FG, relief='flat',
                              highlightthickness=0)
        self.entry.grid(row=0, column=0, columnspan=3, sticky='ew', ipady=self.px(10),
                        padx=(self.px(18), self.px(12)), pady=(self.px(18), self.px(8)))
        self.entry.bind('<Return>', lambda e: self.start_download())

        self.btn_dl = self._button(card, '下载', self.start_download, 'primary')
        self.btn_dl.configure(font=self._font(11, True), padx=self.px(22), pady=self.px(9))
        self.btn_dl.grid(row=0, column=3, padx=(0, self.px(8)), pady=(self.px(18), self.px(8)))
        self.btn_q = self._button(card, '查询', self.start_query, 'ghost')
        self.btn_q.configure(padx=self.px(16), pady=self.px(9))
        self.btn_q.grid(row=0, column=4, padx=(0, self.px(8)), pady=(self.px(18), self.px(8)))
        self.btn_cancel = self._button(card, '终止', self.start_cancel, 'danger')
        self.btn_cancel.configure(padx=self.px(16), pady=self.px(9))
        self.btn_cancel.grid(row=0, column=5, padx=(0, self.px(18)),
                             pady=(self.px(18), self.px(8)))
        self.btn_cancel.configure(state='disabled')

        tk.Label(card, text='搜索', bg=self.CARD, fg=self.MUTED,
                 font=self._font(9, True)).grid(row=1, column=0, padx=(self.px(18), self.px(8)),
                                                pady=(0, self.px(16)), sticky='w')
        self.search_entry = tk.Entry(card, font=self._font(11), bg=self.FIELD, fg=self.FG,
                                     insertbackground=self.FG, relief='flat',
                                     highlightthickness=0)
        self.search_entry.grid(row=1, column=1, columnspan=2, sticky='ew', ipady=self.px(6),
                               padx=(0, self.px(10)), pady=(0, self.px(16)))
        self.search_entry.bind('<Return>', lambda e: self.start_search())
        self.search_kind = tk.StringVar(value='全部')
        self.search_combo = ttk.Combobox(
            card, textvariable=self.search_kind, values=list(SEARCH_KINDS.keys()),
            state='readonly', width=7, font=self._font(9), style='JM.TCombobox')
        self.search_combo.grid(row=1, column=3, padx=(0, self.px(8)), pady=(0, self.px(16)))
        self.btn_search = self._button(card, '搜索', self.start_search, 'ghost')
        self.btn_search.configure(padx=self.px(18), pady=self.px(6))
        self.btn_search.grid(row=1, column=4, columnspan=2, sticky='w',
                             padx=(0, self.px(10)), pady=(0, self.px(16)))
        self._install_placeholder()
        self._search_box = self._placeholder(
            self.search_entry, '本子名称 / 作者名（支持 +包含 -排除）')

        # ---------- 详情卡（左侧强调条，无边框）
        info = tk.Frame(self.root, bg=self.CARD)
        info.pack(fill='x', padx=pad, pady=(0, self.px(12)))
        tk.Frame(info, bg=self.ACCENT, width=self.px(4)).pack(side='left', fill='y')
        ibody = tk.Frame(info, bg=self.CARD)
        ibody.pack(side='left', fill='both', expand=True)
        ibody.columnconfigure(1, weight=1)

        cover_box = tk.Frame(ibody, bg=self.FIELD, width=self.px(124), height=self.px(166))
        cover_box.grid(row=0, column=0, rowspan=2, padx=(self.px(16), self.px(16)),
                       pady=self.px(16), sticky='n')
        cover_box.grid_propagate(False)
        self.cover_label = tk.Label(cover_box, bg=self.FIELD, fg=self.MUTED,
                                    text='封面', font=self._font(9))
        self.cover_label.place(relx=0.5, rely=0.5, anchor='center')
        self.cover_label.bind('<Button-1>', self.open_cover)

        textbox = tk.Frame(ibody, bg=self.CARD)
        textbox.grid(row=0, column=1, sticky='w', padx=(0, self.px(16)))
        tk.Label(textbox, textvariable=self.var_title, bg=self.CARD, fg=self.FG,
                 font=self._font(13, True), anchor='w', justify='left',
                 wraplength=self.px(700)).pack(anchor='w', pady=(0, self.px(5)))
        tk.Label(textbox, textvariable=self.var_meta, bg=self.CARD, fg=self.MUTED,
                 font=self._font(9), anchor='w', justify='left',
                 wraplength=self.px(700)).pack(anchor='w')
        tk.Label(textbox, textvariable=self.var_tags, bg=self.CARD, fg=self.MUTED,
                 font=self._font(9), anchor='w', justify='left',
                 wraplength=self.px(700)).pack(anchor='w', pady=(self.px(3), 0))

        btns = tk.Frame(textbox, bg=self.CARD)
        btns.pack(anchor='w', pady=(self.px(13), 0))
        self.btn_read = self._button(btns, '▶ 阅读', self._read_selected, 'primary')
        self.btn_read.configure(font=self._font(9, True), padx=self.px(14), pady=self.px(5))
        self.btn_read.pack(side='left')
        self.btn_album_dir = self._button(btns, '文件夹', self._open_album_dir, 'ghost')
        self.btn_album_dir.configure(font=self._font(9), padx=self.px(12), pady=self.px(5))
        self.btn_album_dir.pack(side='left', padx=(self.px(6), 0))
        self.btn_pdf = self._button(btns, 'PDF', lambda: self.export_selected('pdf'), 'ghost')
        self.btn_pdf.configure(font=self._font(9), padx=self.px(12), pady=self.px(5))
        self.btn_pdf.pack(side='left', padx=(self.px(6), 0))
        self.btn_zip = self._button(btns, 'ZIP', lambda: self.export_selected('zip'), 'ghost')
        self.btn_zip.configure(font=self._font(9), padx=self.px(12), pady=self.px(5))
        self.btn_zip.pack(side='left', padx=(self.px(6), 0))
        self.btn_del = self._button(btns, '删除', self.delete_selected, 'danger')
        self.btn_del.configure(font=self._font(9), padx=self.px(12), pady=self.px(5))
        self.btn_del.pack(side='left', padx=(self.px(6), 0))

        # ---------- 进度 + 状态行
        prog = tk.Frame(self.root, bg=self.BG)
        prog.pack(fill='x', padx=pad, pady=(0, self.px(8)))
        self.bar = ttk.Progressbar(prog, style='JM.Horizontal.TProgressbar',
                                   mode='determinate', maximum=100)
        self.bar.pack(fill='x')
        prow = tk.Frame(prog, bg=self.BG)
        prow.pack(fill='x', pady=(self.px(7), 0))
        tk.Label(prow, textvariable=self.var_status, bg=self.BG, fg=self.MUTED,
                 font=self._font(9), anchor='w').pack(side='left')
        self.btn_log = tk.Button(prow, text='日志 ▾', command=self.toggle_log,
                                 font=self._font(8, True), bg=self.BG, fg=self.MUTED,
                                 activebackground=self.CARD, activeforeground=self.FG,
                                 relief='flat', bd=0, padx=self.px(8), pady=self.px(3),
                                 cursor='hand2', highlightthickness=0)
        self.btn_log.pack(side='right')
        tk.Label(prow, textvariable=self.var_pct, bg=self.BG, fg=self.ACCENT,
                 font=self._font(9, True)).pack(side='right', padx=(0, self.px(12)))

        # ---------- 底部：左列表（页签用下划线）+ 右日志（可收起）
        bottom = tk.Frame(self.root, bg=self.BG)
        bottom.pack(fill='both', expand=True, padx=pad, pady=(self.px(2), self.px(10)))

        self.reswrap = reswrap = tk.Frame(bottom, bg=self.CARD, width=self.px(340))
        reswrap.pack(side='left', fill='y')
        reswrap.pack_propagate(False)

        rhead = tk.Frame(reswrap, bg=self.CARD)
        rhead.pack(fill='x', padx=self.px(14), pady=(self.px(14), 0))
        seg = dict(font=self._font(10, True), relief='flat', bd=0, cursor='hand2',
                   highlightthickness=0, padx=self.px(2), pady=self.px(6),
                   bg=self.CARD, activebackground=self.CARD, activeforeground=self.FG)
        self.btn_view_res = tk.Button(rhead, text='结果',
                                      command=lambda: self._switch_view('results'), **seg)
        self.btn_view_res.pack(side='left')
        self.btn_view_hist = tk.Button(rhead, text='历史',
                                       command=lambda: self._switch_view('history'), **seg)
        self.btn_view_hist.pack(side='left', padx=(self.px(18), 0))
        self.btn_view_local = tk.Button(rhead, text='已下载',
                                        command=lambda: self._switch_view('local'), **seg)
        self.btn_view_local.pack(side='left', padx=(self.px(18), 0))
        self.btn_clear_view = tk.Button(
            rhead, text='清空', command=self._clear_active_view, font=self._font(8),
            bg=self.CARD, fg=self.MUTED, activebackground=self.CARD,
            activeforeground=self.RED, relief='flat', bd=0, padx=self.px(6),
            pady=self.px(4), cursor='hand2', highlightthickness=0)
        self.btn_clear_view.pack(side='right', pady=self.px(4))

        # 页签下划线（跟着选中的页签走）
        self.tab_line = tk.Frame(reswrap, bg=self.SEP, height=1)
        self.tab_line.pack(fill='x', padx=self.px(14), pady=(0, self.px(4)))
        self.tab_mark = tk.Frame(self.tab_line, bg=self.ACCENT, height=self.px(2))

        self.res_canvas = tk.Canvas(reswrap, bg=self.CARD, highlightthickness=0, bd=0,
                                    yscrollincrement=self.px(30))
        rscroll = ttk.Scrollbar(reswrap, orient='vertical', style='JM.Vertical.TScrollbar',
                                command=self.res_canvas.yview)
        self.res_canvas.configure(yscrollcommand=rscroll.set)
        rscroll.pack(side='right', fill='y')
        self.res_canvas.pack(side='left', fill='both', expand=True,
                             padx=(self.px(10), 0), pady=(self.px(4), self.px(10)))
        self.res_inner = tk.Frame(self.res_canvas, bg=self.CARD)
        self._res_win = self.res_canvas.create_window((0, 0), window=self.res_inner, anchor='nw')
        self.res_inner.bind(
            '<Configure>',
            lambda e: self.res_canvas.configure(scrollregion=self.res_canvas.bbox('all')))
        self.res_canvas.bind(
            '<Configure>',
            lambda e: self.res_canvas.itemconfigure(self._res_win, width=e.width))
        self.res_canvas.bind('<MouseWheel>', self._on_main_wheel)

        # 日志抽屉
        self.logwrap = tk.Frame(bottom, bg=self.CARD)
        lhead = tk.Frame(self.logwrap, bg=self.CARD)
        lhead.pack(fill='x', padx=self.px(14), pady=(self.px(12), 0))
        tk.Label(lhead, text='运行日志', bg=self.CARD, fg=self.MUTED,
                 font=self._font(9, True)).pack(side='left')
        self._button(lhead, '清空', self.clear_log, 'flat').pack(side='right')
        self._button(lhead, '收起', self.toggle_log, 'flat').pack(side='right',
                                                                 padx=(0, self.px(4)))
        self.text = tk.Text(self.logwrap, bg=self.FIELD, fg=self.FG, relief='flat',
                            font=('Consolas', 9), wrap='word', height=6,
                            insertbackground=self.FG, padx=self.px(12), pady=self.px(10),
                            selectbackground=self.ACCENT, selectforeground=self.ON_ACCENT,
                            highlightthickness=0)
        sb = ttk.Scrollbar(self.logwrap, orient='vertical', style='JM.Vertical.TScrollbar',
                           command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set, state='disabled')
        sb.pack(side='right', fill='y')
        self.text.pack(fill='both', expand=True, padx=(self.px(14), 0),
                       pady=(self.px(6), self.px(14)))
        self.text.tag_configure('dim', foreground=self.MUTED)
        self.text.tag_configure('ok', foreground=self.GREEN)
        self.text.tag_configure('err', foreground=self.RED)
        self.text.tag_configure('warn', foreground=self.YELLOW)

        # 日志默认收起（记住状态）；收起时列表面板铺满整行
        if getattr(self, 'log_open', False):
            self.logwrap.pack(side='left', fill='both', expand=True, padx=(self.px(12), 0))
            self.reswrap.pack_configure(fill='y', expand=False)
            self.btn_log.configure(text='日志 ▴')
        else:
            self.reswrap.pack_configure(fill='both', expand=True)
            self.btn_log.configure(text='日志 ▾')

        # ---------- 底栏
        foot = tk.Frame(self.root, bg=self.BG)
        foot.pack(fill='x', padx=pad, pady=(0, self.px(14)))
        tk.Label(foot, textvariable=self.var_foot, bg=self.BG, fg=self.MUTED,
                 font=self._font(8), anchor='w').pack(side='left')
        tk.Label(foot, text='jmcomic', bg=self.BG, fg=self.BORDER,
                 font=self._font(8)).pack(side='right')

        self.log('界面已就绪，输入车号后回车即可下载。', 'dim')
        self._render_view()

    # -------------------------------------------------- 日志与队列

    def _install_log_hook(self):
        """把 jmcomic 的日志接到界面上（含每张图片的完成事件）"""
        from jmcomic.jm_config import JmModuleConfig

        skip = {'api', 'api.setting', 'api.update_domain.success',
                'image.before', 'image.after', 'image.skip'}

        def hook(topic, msg, e=None):
            if self._cancel.is_set():          # 用户点了「终止下载」→ 让下载立刻失败
                raise Cancelled()
            if topic == 'image.after':
                self.done_images += 1
                self.q.put(('tick', self.done_images))
                return
            if topic in skip:
                return
            bad = e is not None or 'failed' in topic or 'exception' in topic
            self.q.put(('log', (str(msg)[:400], 'err' if bad else '')))

        JmModuleConfig.EXECUTOR_LOG = hook

    def log(self, msg, tag=None):
        stamp = time.strftime('%H:%M:%S')
        self.text.configure(state='normal')
        self.text.insert('end', f'[{stamp}] {msg}\n', tag or '')
        self.text.see('end')
        self.text.configure(state='disabled')

    def clear_log(self):
        self.text.configure(state='normal')
        self.text.delete('1.0', 'end')
        self.text.configure(state='disabled')

    def _log_async(self, msg, tag=''):
        """worker 线程不碰控件，只投队列，由主线程刷新"""
        self.q.put(('log', (msg, tag)))

    def _drain(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == 'log':
                    msg, tag = payload
                    self.log(msg, tag)
                elif kind == 'info':
                    key, info = payload
                    self._add_result(key, info)
                elif kind == 'failed':
                    self._add_failed(payload)
                elif kind == 'tick':
                    self._progress(payload, self.total_images)
                elif kind == 'total':
                    self.total_images = payload
                    self.done_images = 0
                    self._progress(0, payload)
                elif kind == 'status':
                    self.var_status.set(payload)
                elif kind == 'info':
                    self._show_info(payload)
                elif kind == 'base':
                    self.base_dir = payload
                    self.var_foot.set(f'保存位置：{payload}')
                elif kind == 'cover':
                    key, path = payload
                    if path:
                        self.result_covers[key] = path
                        self._set_row_cover(key, path)
                        self._history_update_cover(key, path)
                        if self.selected_key == key:
                            self._show_cover(path)
                    elif self.selected_key == key:
                        self._clear_cover('无封面')
                elif kind == 'readypage':
                    reader, page_idx, img, w, h, y = payload
                    try:
                        reader.place_page(page_idx, img, w, h, y)
                    except Exception:
                        pass
                elif kind == 'local':
                    self.local = payload or []
                    if self.view_mode == 'local':
                        self._render_view()
                        self.var_status.set(f'已下载 {len(self.local)} 项')
                    else:
                        self._refresh_left_header()
                elif kind == 'localthumb':
                    key, img = payload
                    try:
                        from PIL import ImageTk

                        photo = ImageTk.PhotoImage(img)
                        self._row_photos[key] = photo
                        item = self.result_rows.get(key)
                        if item:
                            item[1].configure(image=photo, text='', cursor='hand2')
                    except Exception:
                        pass
                elif kind == 'history':
                    key, action, album_dir = (list(payload) + ['', '', ''])[:3]
                    self._history_record(key, action, album_dir=album_dir)
                elif kind == 'done':
                    self._finish()
                elif kind == 'exported':
                    self._exported(path)
        except queue.Empty:
            pass
        self.root.after(80, self._drain)

    def _exported(self, path):
        """导出完成提示（主线程）"""
        try:
            self.toast(f'已导出：{os.path.basename(path)}', 'ok')
        except Exception:
            pass
        try:
            from tkinter import messagebox
            messagebox.showinfo('导出完成',
                                f'已导出：\n{path}\n\n所在目录：\n{os.path.dirname(path)}')
        except Exception:
            pass

    def _inside(self, w, x, y):
        """屏幕坐标 (x, y) 是否落在控件 w 的范围内"""
        try:
            wx, wy = w.winfo_rootx(), w.winfo_rooty()
            return wx <= x < wx + w.winfo_width() and wy <= y < wy + w.winfo_height()
        except Exception:
            return False

    def _on_delete_key(self, e=None):
        """Delete 键 = 删除选中的已下载条目（在输入框里打字时不触发）"""
        try:
            w = self.root.focus_get()
        except Exception:
            w = None
        if w is not None:
            try:
                if w.winfo_class() in ('Entry', 'Text', 'TEntry', 'TCombobox'):
                    return
            except Exception:
                pass
        self.delete_selected()

    def _on_main_wheel(self, e):
        """主窗口滚轮：按光标所在区域决定滚哪个（左侧列表 / 运行日志）"""
        x, y = e.x_root, e.y_root
        if self._inside(self.res_canvas, x, y):
            self.res_canvas.yview_scroll(-3 if e.delta > 0 else 3, 'units')
            return 'break'
        if self._inside(self.text, x, y):
            if self.root.focus_get() is self.text:
                return          # Text 自带的类绑定已经滚过了，避免滚两次
            self.text.yview_scroll(-3 if e.delta > 0 else 3, 'units')
            return 'break'
        return None

    def _progress(self, done, total):
        if total > 0:
            target = min(done, total)
            try:
                cur = float(self.bar['value'] or 0)
            except Exception:
                cur = 0.0
            self.bar.configure(mode='determinate', maximum=total)
            self.var_status.set(f'已下载 {done}/{total} 张')
            self.var_pct.set(f'{target * 100 // total}%')
            # 进度跳得多时补间一下，避免一格一格硬跳
            if ANIM_ON and (target - cur) >= max(2, total // 20):
                def step(t):
                    try:
                        self.bar.configure(value=lerp(cur, target, t))
                    except Exception:
                        pass
                self._anim(4, step, first_now=False)
            else:
                self.bar.configure(value=target)
        else:
            self.var_status.set(f'已下载 {done} 张')

    def _show_info(self, info):
        self.var_title.set(str(info.get('title') or '—'))
        parts = []
        if info.get('id'):
            parts.append(f"JM{info['id']}")
        if info.get('authors'):
            parts.append(str(info['authors']))
        parts.append(f"{info.get('pages')} 页")
        parts.append(f"{info.get('chapters')} 话")
        if info.get('hit'):
            parts.append(f"命中 {info['hit']}")
        self.var_meta.set('   ·   '.join(parts))
        self.var_tags.set(str(info.get('tags') or ''))

    def _show_cover(self, path):
        """把封面文件渲染成缩略图显示（必须在主线程调用），带淡入"""
        try:
            from PIL import Image, ImageTk

            img = Image.open(path).convert('RGB')
            img.thumbnail((self.px(124), self.px(166)), Image.LANCZOS)
        except Exception as e:
            self._clear_cover('封面加载失败')
            self.log(f'封面加载失败：{e}', 'err')
            return

        self._cover_path = path
        self._cover_gen = (self._cover_gen or 0) + 1        # 作废之前没播完的淡入
        gen = self._cover_gen

        def apply(pil):
            photo = ImageTk.PhotoImage(pil)
            self._cover_photo = photo          # 留引用，否则会被回收成空白
            self.cover_label.configure(image=photo, text='', cursor='hand2')

        if not ANIM_ON:
            apply(img)
            return
        base = Image.new('RGB', img.size, hex_rgb(self.FIELD))

        def step(t):
            if gen != self._cover_gen:         # 期间被清空/换了新封面 → 丢弃过期帧
                return
            apply(Image.blend(base, img, max(0.03, t)))

        self._anim(6, step)

    def _clear_cover(self, text='封面'):
        self._cover_gen = (self._cover_gen or 0) + 1        # 作废未播完的淡入
        self._cover_photo = None
        self._cover_path = None
        try:
            self.cover_label.configure(image='', text=text, cursor='')
        except Exception:
            pass

    def open_cover(self, *_):
        """点封面 = 用系统看图程序打开原图"""
        if self._cover_path and os.path.isfile(self._cover_path):
            try:
                os.startfile(self._cover_path)
            except Exception as e:
                self.log(f'打开封面失败：{e}', 'err')

    def _fetch_cover(self, client, detail, kind, key):
        """worker 线程：下载封面到本地缓存，再把 (key, 路径) 投给主线程"""
        album_id = detail.id if kind == 'album' else getattr(detail, 'album_id', None)
        self._fetch_cover_by_id(client, album_id, key)

    def _fetch_cover_by_id(self, client, album_id, key):
        try:
            if not album_id:
                self.q.put(('cover', (key, None)))
                return
            os.makedirs(COVER_DIR, exist_ok=True)
            path = os.path.join(COVER_DIR, f'{album_id}.jpg')
            if not (os.path.isfile(path) and os.path.getsize(path) > 1024):
                client.download_album_cover(album_id, path)
            if os.path.isfile(path) and os.path.getsize(path) > 1024:
                self.q.put(('cover', (key, path)))
            else:
                self.q.put(('cover', (key, None)))
        except Exception as e:
            self.q.put(('cover', (key, None)))
            self._log_async(f'封面获取失败：{e}', 'dim')

    # -------------------------------------------------- 左侧列表（结果 / 历史）

    def _switch_view(self, mode):
        self.view_mode = mode
        self.selected_key = None
        if mode == 'local':
            self.var_status.set('正在扫描已下载 ...')
            self._render_view()
            threading.Thread(target=self._load_local, daemon=True).start()
        else:
            self._render_view()

    def _load_local(self):
        """worker 线程：扫描保存目录 + 用每个文件夹的第一张图做缩略图"""
        try:
            entries = scan_downloads(self.base_dir, self.history)
        except Exception as e:
            self.q.put(('log', (f'扫描下载目录失败：{e}', 'err')))
            entries = []
        self.q.put(('local', entries))
        for e in entries[:60]:
            p = e.get('_thumb')
            if not (p and os.path.isfile(p)):
                continue
            try:
                from PIL import Image

                img = Image.open(p)
                img.draft('RGB', (self.px(112), self.px(152)))   # 让 JPEG 直接小尺寸解码
                img = img.convert('RGB')
                img.thumbnail((self.px(56), self.px(76)), Image.LANCZOS)
                self.q.put(('localthumb', (e['key'], img)))
            except Exception:
                pass

    def _clear_active_view(self):
        if self.view_mode == 'results':
            self._clear_results()
            return
        if self.view_mode == 'local':
            self.var_status.set('正在刷新 ...')
            self._render_view()
            threading.Thread(target=self._load_local, daemon=True).start()
            return
        if not self.history:
            return
        from tkinter import messagebox

        if messagebox.askyesno('清空历史记录', f'确定要清空全部 {len(self.history)} 条历史记录吗？'):
            self.history = []
            save_history(self.history)
            self.selected_key = None
            self._render_view()
            self.log('历史记录已清空。', 'dim')

    def _render_view(self):
        """按当前视图模式重建左侧列表"""
        for w in self.res_inner.winfo_children():
            w.destroy()
        self.result_rows.clear()
        self._row_photos.clear()

        if self.view_mode == 'results':
            items = list(self.results.items())
        elif self.view_mode == 'history':
            items = [(it.get('key'), it) for it in self.history]
        else:
            items = [(it.get('key'), it) for it in self.local]

        for key, info in items:
            self._row_for(self.res_inner, key, info, self.view_mode)

        self._refresh_left_header()
        self._apply_highlight()
        self.res_canvas.yview_moveto(0.0)

    def _refresh_left_header(self):
        mode = self.view_mode
        pairs = (
            (self.btn_view_res, f'结果 {len(self.results)}', 'results'),
            (self.btn_view_hist, f'历史 {len(self.history)}', 'history'),
            (self.btn_view_local, f'已下载 {len(self.local)}', 'local'),
        )
        active_btn = None
        for btn, text, key in pairs:
            btn.configure(text=text)
            if key == mode:
                active_btn = btn
            target = self.ACCENT if key == mode else self.MUTED
            try:
                now = str(btn['fg'])
            except Exception:
                now = target
            if not ANIM_ON or now.lower() == target.lower():
                btn.configure(fg=target)
            else:
                def step(t, b=btn, c0=now, c1=target):
                    try:
                        b.configure(fg=lerp_color(c0, c1, t))
                    except Exception:
                        pass

                self._anim(5, step, first_now=False)
        # 下划线跟着选中的页签
        if active_btn is not None:
            def place_mark(ab=active_btn):
                try:
                    self.tab_mark.place(x=ab.winfo_x(), y=0,
                                        width=max(self.px(16), ab.winfo_width()),
                                        height=self.px(2))
                except Exception:
                    pass

            try:
                self.root.update_idletasks()
            except Exception:
                pass
            place_mark()
            self.root.after(30, place_mark)
        self.btn_clear_view.configure(
            text='刷新' if mode == 'local' else ('清空历史' if mode == 'history' else '清空'))

    def _row_for(self, parent, key, info, mode):
        """一行：封面缩略图 + 标题 + 基本信息 + 下载（历史里还有删除）"""
        tk = self.tk
        row = tk.Frame(parent, bg=self.CARD2, highlightbackground=self.BORDER,
                       highlightthickness=1, cursor='hand2')
        row.pack(fill='x', pady=(0, self.px(8)))

        thumb_box = tk.Frame(row, bg=self.FIELD, width=self.px(56), height=self.px(76),
                             highlightbackground=self.BORDER, highlightthickness=1)
        thumb_box.pack(side='left', padx=self.px(10), pady=self.px(9))
        thumb_box.pack_propagate(False)
        thumb = tk.Label(thumb_box, bg=self.FIELD, fg=self.MUTED, text='…', font=self._font(8))
        thumb.place(relx=0.5, rely=0.5, anchor='center')

        if mode == 'history':
            x = tk.Button(row, text='×', command=lambda k=key: self._history_remove(k),
                          font=self._font(9, True), bg=self.CARD2, fg=self.MUTED,
                          activebackground=self.BORDER, activeforeground=self.RED,
                          relief='flat', bd=0, padx=self.px(5), pady=0, cursor='hand2',
                          highlightthickness=0)
            x.pack(side='right', padx=(0, self.px(6)), pady=self.px(8))

        if mode == 'local':
            btn = self._button(row, '阅读', lambda k=key: self._read_key(k), 'primary')
            delbtn = tk.Button(row, text='删', command=lambda k=key: self.delete_key(k),
                               font=self._font(8, True), bg=self.CARD2, fg=self.DANGER_FG,
                               activebackground=self.DANGER_HI, activeforeground=self.DANGER_FG_HI,
                               relief='flat', bd=0, padx=self.px(7), pady=0,
                               cursor='hand2', highlightthickness=0)
            delbtn.pack(side='right', padx=(0, self.px(2)), pady=self.px(8))
        else:
            btn = self._button(row, '下载', lambda k=key: self._download_key(k), 'ghost')
        btn.configure(font=self._font(8, True), padx=self.px(8), pady=self.px(3))
        btn.pack(side='right', padx=self.px(6), pady=self.px(8))

        text = tk.Frame(row, bg=self.CARD2)
        text.pack(side='left', fill='both', expand=True, pady=self.px(8))
        title = str(info.get('title') or '')
        if len(title) > 24:
            title = title[:24] + '…'
        tk.Label(text, text=title, bg=self.CARD2, fg=self.FG, font=self._font(9, True),
                 anchor='w', justify='left', wraplength=self.px(132)).pack(anchor='w')

        if info.get('id'):
            line2 = f"JM{info.get('id')} · {info.get('pages')}页 · {info.get('chapters')}章"
        else:
            line2 = f"{info.get('pages')}页 · {info.get('chapters')}章"
        tk.Label(text, text=line2, bg=self.CARD2, fg=self.MUTED, font=self._font(8),
                 anchor='w').pack(anchor='w', pady=(self.px(2), 0))

        if mode == 'history':
            act = info.get('action') or '查询'
            color = self.GREEN if act == '下载' else self.MUTED
            tk.Label(text, text=f"{act} · {fmt_time(info.get('ts'))}", bg=self.CARD2,
                     fg=color, font=self._font(8), anchor='w').pack(anchor='w',
                                                                    pady=(self.px(1), 0))
        elif mode == 'local':
            tk.Label(text, text=f"已下载 · {fmt_time(info.get('ts'))}", bg=self.CARD2,
                     fg=self.GREEN, font=self._font(8), anchor='w').pack(anchor='w',
                                                                        pady=(self.px(1), 0))

        self.result_rows[key] = (row, thumb, btn)
        for w in [row, text, thumb] + list(text.winfo_children()):
            w.bind('<Button-1>', lambda e, k=key: self._select_result(k))

        # 悬停渐变（整行背景柔和过渡）
        hover_widgets = [row, text] + list(text.winfo_children())

        def hov(active):
            target = self.HOVER if active else self.CARD2
            start = str(row['bg'])
            if start.lower() == target.lower():
                return

            def step(t):
                col = lerp_color(start, target, t)
                for w in hover_widgets:
                    try:
                        w.configure(bg=col)
                    except Exception:
                        pass

            self._anim(4, step, first_now=False)

        for w in hover_widgets:
            w.bind('<Enter>', lambda e: hov(True), add='+')
            w.bind('<Leave>', lambda e: hov(False), add='+')

        # 新行"亮一下"再落位（前 12 行，避免一次刷太多动画）
        if ANIM_ON and len(self.result_rows) <= 12:
            def step(t, r=row):
                try:
                    r.configure(highlightbackground=lerp_color(self.ACCENT, self.BORDER, t))
                except Exception:
                    pass
            self._anim(6, step, first_now=False, done=self._apply_highlight)

        # 已有封面缓存就直接画上（历史记录里存了封面路径）
        cover = self.result_covers.get(key) or info.get('cover')
        if cover and os.path.isfile(cover):
            self.result_covers.setdefault(key, cover)
            self._set_row_cover(key, cover)

    def _apply_highlight(self):
        for k, (row, _thumb, _btn) in self.result_rows.items():
            picked = (k == self.selected_key)
            row.configure(highlightbackground=self.ACCENT if picked else self.BORDER,
                          highlightthickness=2 if picked else 1)

    def _clear_results(self):
        self.results.clear()
        self.result_covers.clear()
        self.selected_key = None
        if self.view_mode == 'results':
            self._render_view()
        else:
            self._refresh_left_header()

    def _add_result(self, key, info):
        first = not self.results
        self.results[key] = info
        self.selected_key = key if first else self.selected_key
        if self.view_mode == 'results':
            self._render_view()
            self.res_canvas.yview_moveto(1.0)
        else:
            self._refresh_left_header()
        if first:
            self._show_detail(key)

    def _add_failed(self, key):
        """查询失败的条目也在列表里留一行，方便对照"""
        tk = self.tk
        rid = key.split(':')[-1]
        if self.view_mode != 'results':
            return
        row = tk.Frame(self.res_inner, bg=self.CARD2, highlightbackground=self.RED,
                       highlightthickness=1)
        row.pack(fill='x', pady=(0, self.px(8)))
        tk.Label(row, text=f'JM{rid}  查询失败', bg=self.CARD2, fg=self.RED,
                 font=self._font(9), anchor='w').pack(side='left', padx=self.px(10),
                                                      pady=self.px(8))
        self.res_canvas.yview_moveto(1.0)

    def _find_info(self, key):
        if key in self.results:
            return self.results[key]
        for it in self.history:
            if it.get('key') == key:
                return it
        for it in self.local:
            if it.get('key') == key:
                return it
        return None

    def _select_result(self, key):
        """点某一行 → 上方大卡片显示这一条的封面和详情"""
        self.selected_key = key
        self._show_detail(key)
        self._apply_highlight()

    def _show_detail(self, key):
        info = self._find_info(key)
        if not info:
            return
        self._show_info(info)
        cover = self.result_covers.get(key) or info.get('cover')
        if cover and os.path.isfile(cover):
            self._show_cover(cover)
        else:
            self._clear_cover('载入中 …' if self.view_mode == 'results' else '无封面')

    def _set_row_cover(self, key, path):
        item = self.result_rows.get(key)
        if not item:
            return
        try:
            from PIL import Image, ImageTk

            img = Image.open(path).convert('RGB')
            img.thumbnail((self.px(56), self.px(76)), Image.LANCZOS)
            photo = ImageTk.PhotoImage(img)
            self._row_photos[key] = photo
            _row, thumb, _btn = item
            thumb.configure(image=photo, text='', cursor='hand2')
            thumb.bind('<Button-1>', lambda e, k=key: self._select_result(k))
        except Exception:
            pass

    # -------------------------------------------------- 阅读已下载

    def _locate_album_dir(self, album_id, title=''):
        """在下载目录里找这个本子的文件夹（新结构带 [JM车号]）"""
        base = self.base_dir
        if not (album_id and os.path.isdir(base)):
            return ''
        want = f'jm{album_id}'.lower()
        try:
            subs = [d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d))]
        except Exception:
            subs = []
        for d in subs:
            if want in d.lower().replace(' ', ''):
                return os.path.join(base, d)
        if title:                              # 旧目录结构兜底：按标题前缀找
            head = str(title).strip()[:10].lower()
            if head:
                for d in subs:
                    if head in d.lower():
                        return os.path.join(base, d)
        return ''

    def _album_dir_of(self, info):
        if not info:
            return ''
        d = info.get('dir')
        if d and os.path.isdir(d):
            return d
        return self._locate_album_dir(str(info.get('id') or ''), info.get('title') or '')

    def export_selected(self, fmt='pdf'):
        """把选中的已下载本子导出成 PDF / ZIP（后台线程，完成后提示路径）"""
        from tkinter import messagebox

        if not self.selected_key:
            self.var_status.set('先从列表里选一条')
            return
        info = self._find_info(self.selected_key)
        root = self._album_dir_of(info) if info else ''
        if not root:
            messagebox.showinfo('还没下载',
                                '找不到已下载的文件夹——先下载一次，或用「已下载」列表选一条。')
            return
        if self.busy:
            self.log('还有任务在跑，等它结束再导出。', 'warn')
            return

        title = (info or {}).get('title') or os.path.basename(root)
        out_dir = export_dir(self.base_dir if os.path.isdir(self.base_dir or '') else None)
        self.busy = True
        self._buttons(False)
        self.var_status.set(f'正在导出 {fmt.upper()} …')
        self.log(f'导出 {fmt.upper()}：{title}', 'dim')

        def work():
            ok, n, path = export_album(root, fmt, out_dir=out_dir, name=title)
            if ok:
                self.q.put(('log', (f'导出完成：{n} 页 → {path}', 'ok')))
                self.q.put(('status', f'已导出 {n} 页：{os.path.basename(path)}'))
                self.q.put(('exported', path))
            else:
                self.q.put(('log', (f'导出失败：{path}', 'err')))
                self.q.put(('status', '导出失败'))
            self.q.put(('done', None))

        threading.Thread(target=work, daemon=True).start()

    def _read_selected(self):
        if not self.selected_key:
            self.var_status.set('先从列表里选一条')
            return
        self._read_key(self.selected_key)

    def _read_key(self, key):
        from tkinter import messagebox

        info = self._find_info(key)
        if not info:
            return
        root = self._album_dir_of(info)
        if not root:
            messagebox.showinfo('还没下载',
                                '找不到已下载的文件夹——先下载一次，'
                                '或者用「打开下载文件夹」确认一下保存位置。')
            return
        chapters = discover_chapters(root)
        if not chapters:
            messagebox.showinfo('没有图片', f'这个文件夹里没有图片：\n{root}')
            return
        for r in list(self._readers):
            r.close()
        self._readers.append(
            ReaderWindow(self, info.get('title') or f"JM{info.get('id')}", root, chapters))
        self.log(f'打开阅读器：{root}', 'dim')

    def _open_album_dir(self):
        if not self.selected_key:
            self.var_status.set('先从列表里选一条')
            return
        info = self._find_info(self.selected_key)
        root = self._album_dir_of(info)
        if not root:
            self.open_folder()
            return
        try:
            os.startfile(root)
        except Exception as e:
            self.log(f'打开文件夹失败：{e}', 'err')

    # -------------------------------------------------- 历史记录

    def _history_record(self, key, action, info=None, album_dir=''):
        info = info or self._find_info(key) or {}
        if not info.get('id'):
            return
        entry = {
            'key': key, 'id': info.get('id'), 'kind': info.get('kind'),
            'title': info.get('title'), 'authors': info.get('authors'),
            'pages': info.get('pages'), 'chapters': info.get('chapters'),
            'tags': info.get('tags'),
            'cover': self.result_covers.get(key) or info.get('cover'),
            'dir': album_dir or info.get('dir') or '',
            'hit': info.get('hit') or '',
            'action': action, 'ts': time.time(),
        }
        old = None
        for i, it in enumerate(self.history):
            if it.get('key') == key:
                old = self.history.pop(i)
                break
        if old:
            if old.get('action') == '下载':
                entry['action'] = '下载'            # 下载过就一直算已下载
            if not entry.get('cover'):
                entry['cover'] = old.get('cover')
            if not entry.get('dir'):
                entry['dir'] = old.get('dir') or ''

        self.history.insert(0, entry)
        del self.history[HISTORY_MAX:]
        save_history(self.history)
        if self.view_mode == 'history':
            self._render_view()
        else:
            self._refresh_left_header()

    def _history_update_cover(self, key, path):
        """封面下载完补进历史（不新建条目）"""
        for it in self.history:
            if it.get('key') == key:
                it['cover'] = path
                save_history(self.history)
                return

    def _history_remove(self, key):
        self.history = [it for it in self.history if it.get('key') != key]
        save_history(self.history)
        if self.selected_key == key:
            self.selected_key = None
        self._render_view()

    def _download_key(self, key):
        """列表行上的「下载」：只下这一条"""
        info = self._find_info(key)
        if not info:
            return
        spec = f"p{info['id']}" if info['kind'] == '章节' else str(info['id'])
        self._entry_box.set(spec)
        self.start_download()

    def _finish(self):
        self.busy = False
        self._cancel.clear()
        try:
            self.btn_cancel.configure(state='disabled')
        except Exception:
            pass
        self._buttons(True)
        self.var_status.set('就绪')
        self.var_pct.set('')
        self.log(f'任务结束，文件保存在 {self.base_dir}', 'ok')
        if self._task_name == '_work_download':
            self.toast('下载任务结束', 'ok')
        if self.view_mode == 'local':          # 刚下完 → 刷新书库
            threading.Thread(target=self._load_local, daemon=True).start()

    def _buttons(self, enabled):
        state = 'normal' if enabled else 'disabled'
        for b in (self.btn_dl, self.btn_q):
            b.configure(state=state)
        try:
            self.entry.configure(state='normal' if enabled else 'disabled')
        except Exception:
            pass

    # -------------------------------------------------- 动作

    def choose_folder(self):
        """选择下载保存位置，并写回 option.yml（命令行版也会跟着变）"""
        from tkinter import filedialog, messagebox

        init = self.base_dir if os.path.isdir(self.base_dir) else APP_DIR
        try:
            picked = filedialog.askdirectory(title='选择下载保存位置', initialdir=init)
        except Exception as e:
            self.log(f'打开文件夹选择器失败：{e}', 'err')
            return
        if not picked:
            return

        picked = os.path.normpath(picked)
        if os.path.normcase(picked) == os.path.normcase(self.base_dir):
            self.log('保存位置没有变化。', 'dim')
            return

        self.set_base_dir(picked)
        messagebox.showinfo('已更改保存位置', f'之后的下载都会保存到：\n{picked}')

    def set_base_dir(self, path):
        self._base_dirty = True
        self.base_dir = path
        self.var_foot.set(f'保存位置：{path}')
        self.var_status.set('保存位置已更改')
        ok, cfg = update_option_file(path)
        if ok:
            self.log(f'保存位置已改为 {path}（已写入 {os.path.basename(cfg)}）', 'ok')
        else:
            self.log(f'保存位置已改为 {path}，但配置文件写入失败，重启后可能不生效。', 'warn')

    def open_folder(self):
        try:
            os.makedirs(self.base_dir, exist_ok=True)
            os.startfile(self.base_dir)
        except Exception as e:
            self.log(f'打开文件夹失败：{e}', 'err')

    def start_cancel(self):
        """终止当前下载：置位信号，下载钩子会立刻抛异常停下"""
        if not self.busy:
            self.var_status.set('当前没有任务在跑')
            return
        self._cancel.set()
        try:
            self.btn_cancel.configure(state='disabled')
        except Exception:
            pass
        self.var_status.set('正在终止…')
        self.log('已发出终止请求，正在停止下载（已完成的部分会保留在目录里）…', 'warn')

    def _confirm_delete(self, root, files):
        from tkinter import messagebox
        return messagebox.askyesno(
            '删除已下载',
            f'把这个文件夹移到回收站吗？（可以在回收站里还原）\n\n'
            f'{root}\n\n共 {files} 张图片',
            icon='warning')

    def delete_key(self, key):
        """列表行上的快捷删除：把这一条对应的文件夹移到回收站"""
        info = self._find_info(key)
        root = self._album_dir_of(info) if info else ''
        if not root or not os.path.isdir(root):
            self.log('这一条没有对应的已下载文件夹（可能已经删掉了）。', 'dim')
            self.var_status.set('没有可删除的文件夹')
            return False

        files = 0
        for _n, pages in discover_chapters(root):
            files += len(pages)
        if not self._confirm_delete(root, files):
            return False

        ok, msg = trash_path(root)
        if not ok:
            self.log(f'删除失败：{msg}', 'err')
            self.var_status.set('删除失败')
            return False

        title = (info or {}).get('title') or os.path.basename(root)
        self.log(f'已删除（回收站）：{root}（{files} 张）', 'ok')
        self.var_status.set(f'已删除：{title}')
        self.toast(f'已移到回收站：{files} 张', 'ok')

        # 从当前列表里拿掉这一行
        self.local = [e for e in self.local if e.get('key') != key]
        item = self.result_rows.pop(key, None)
        if item is not None:
            try:
                item[0].destroy()
            except Exception:
                pass
        if self.selected_key == key:
            self.selected_key = None
            self._clear_cover('封面')
            self._show_info({'title': '—', 'kind': '—', 'pages': '—', 'chapters': '—'})
        self._refresh_left_header()
        return True

    def delete_selected(self):
        """详情卡片上的「删除已下载」"""
        if not self.selected_key:
            self.var_status.set('先从列表里选一条')
            return
        self.delete_key(self.selected_key)

    def start_download(self):
        text = self._entry_text()
        if not text:
            self.var_status.set('请先输入车号')
            return
        if not parse_ids(text):
            self.var_status.set('没识别出车号')
            return
        self._run(self._work_download, text)

    def start_query(self):
        text = self._entry_text()
        if not parse_ids(text):
            self.var_status.set('没识别出车号')
            return
        self._run(self._work_query, text, clear_results=True)

    def start_search(self):
        text = self._search_text()
        if not text:
            self.var_status.set('请输入要搜索的内容')
            self.search_entry.focus_set()
            return
        kind = SEARCH_KINDS.get(self.search_kind.get(), 'site')
        self._run(self._work_search, text, kind, clear_results=True)

    def _run(self, fn, *args, clear_results=False):
        if self.busy:
            self.log('上一个任务还没结束，请稍等。', 'warn')
            return
        self.busy = True
        self._cancel.clear()
        self._task_name = getattr(fn, '__name__', '')
        try:
            self.btn_cancel.configure(state='normal')
        except Exception:
            pass
        self._buttons(False)
        self.done_images = 0
        self.total_images = 0
        self.bar.configure(mode='determinate', value=0)
        self.var_pct.set('')
        if clear_results:
            self._clear_results()
            self._clear_cover('载入中 …')
        elif self._cover_photo is None:
            self._clear_cover('载入中 …')
        threading.Thread(target=self._guard, args=(fn,) + args, daemon=True).start()

    def _guard(self, fn, *args):
        try:
            fn(*args)
        except Exception:
            self.q.put(('log', (traceback.format_exc(limit=4), 'err')))
        finally:
            self.q.put(('done', None))

    # -------------------------------------------------- 后台任务

    def _resolve_item(self, client, kind, i):
        return resolve_item(client, kind, i)

    def _album_root(self, opt, detail, kind):
        """这个本子下载后落在哪个文件夹（用于阅读器定位）"""
        obj = detail if kind == 'album' else getattr(detail, 'from_album', None)
        if obj is None:
            return ''
        try:
            return str(opt.dir_rule.decide_album_root_dir(obj))
        except Exception:
            return ''

    def _work_search(self, text, kind='site'):
        opt, base = build_option()
        self.q.put(('base', base))
        client = opt.new_jm_client()
        label = SEARCH_KIND_NAMES.get(kind, kind)
        self.q.put(('status', f'正在搜索「{text}」...'))
        self._log_async(f'搜索「{text}」（{label}）', 'dim')
        try:
            page = search_page(client, text, kind)
        except Exception as e:
            self._log_async(f'搜索失败：{e}', 'err')
            return

        infos = search_infos(page)
        total = getattr(page, 'total', None)
        if infos:
            self._log_async(f'共 {total} 个结果，本页 {len(infos)} 条', 'ok')
        else:
            self._log_async(f'没搜到结果（共 {total} 个）', 'err')

        for key, info in infos:
            self.q.put(('info', (key, info)))

        # 顺带给前 12 个结果抓封面（走缓存，慢也不卡界面）
        for key, info in infos[:12]:
            self._fetch_cover_by_id(client, info.get('id'), key)

    def _work_query(self, text):
        opt, base = build_option()
        self.q.put(('base', base))
        client = opt.new_jm_client()
        for kind, i in parse_ids(text):
            self.q.put(('status', f'正在查询 JM{i} ...'))
            self._log_async(f'查询 JM{i} ...', 'dim')
            try:
                kind2, detail, hit = self._resolve_item(client, kind, i)
            except Exception as e:
                self._log_async(f'JM{i} 查询失败：{e}', 'err')
                self.q.put(('failed', f'{kind}:{i}'))
                continue

            key = f'{kind2}:{detail.id}'
            info = detail_info(detail)
            if hit:
                info['hit'] = hit
                self._log_async(
                    f'JM{i} 是章节「{hit}」→ 自动带出所属本子 {detail.id} 的全部 '
                    f'{info["chapters"]} 话（共 {info["pages"]} 页）', 'ok')
            self.q.put(('info', (key, info)))
            self.q.put(('history', (key, '查询', self._album_root(opt, detail, kind2))))
            self._log_async(f"✔ {detail.name}", 'ok')
            self._fetch_cover(client, detail, kind2, key)

    def _work_download(self, text):
        opt, base = build_option()
        self.q.put(('base', base))
        client = opt.new_jm_client()
        items = parse_ids(text)
        self._log_async(f'共 {len(items)} 个目标，开始处理。', 'dim')

        for n, (kind, i) in enumerate(items, 1):
            if self._cancel.is_set():
                self._log_async('已终止：剩余目标不再下载。', 'warn')
                self.q.put(('status', '已终止'))
                return
            tag = f'({n}/{len(items)})' if len(items) > 1 else ''
            self.q.put(('status', f'读取 JM{i} 详情 ...'))
            try:
                kind2, detail, hit = self._resolve_item(client, kind, i)
            except Exception as e:
                self._log_async(f'JM{i} 详情获取失败：{e}', 'err')
                self.q.put(('failed', f'{kind}:{i}'))
                continue

            key = f'{kind2}:{detail.id}'
            info = detail_info(detail)
            if hit:
                info['hit'] = hit
                self._log_async(
                    f'JM{i} 是章节「{hit}」→ 自动下载整本 {detail.id}（{info["chapters"]} 话）', 'ok')
            self.q.put(('info', (key, info)))
            album_root = self._album_root(opt, detail, kind2)
            total = info['pages']
            self.q.put(('total', total))
            self._fetch_cover(client, detail, kind2, key)
            self.q.put(('status', f'正在下载 {detail.name} ...'))
            self._log_async(f"{tag} {info['title']}  共 {total} 张", 'dim')

            try:
                if kind2 == 'album':
                    opt.download_album(detail.id)      # 整本（含全部章节）
                else:
                    opt.download_photo(detail.id)
            except Cancelled:
                self._log_async(f'已终止：{info["title"]}（已下载的部分保留在目录里）', 'warn')
                self.q.put(('status', '已终止'))
                self.q.put(('cancelled', None))
                return
            except Exception as e:
                if self._cancel.is_set():
                    self._log_async('已终止下载。', 'warn')
                    self.q.put(('status', '已终止'))
                    self.q.put(('cancelled', None))
                    return
                self._log_async(f'JM{i} 下载出错：{e}', 'err')
                continue

            self._log_async(f"✔ 完成：{detail.name}", 'ok')
            self.q.put(('history', (key, '下载', album_root)))

        self.q.put(('status', '全部完成'))

    # -------------------------------------------------- 入口

    def run(self):
        self.root.mainloop()


# ---------------------------------------------------------------- 内置阅读器

class ReaderWindow:
    """看已下载的漫画：翻页 / 换章节 / 键盘鼠标操作"""

    def __init__(self, app, title, root_dir, chapters=None):
        self.app = app
        tk = app.tk
        self.tk = tk
        self.px = app.px
        self.root_dir = root_dir
        self.chapters = chapters if chapters is not None else discover_chapters(root_dir)
        self.ci = 0
        self.idx = 0
        self.pages = []
        self._photo = None
        self._after = None
        # 滚动模式的状态
        self.layout = []            # [(页号, 路径, 宽, 高, y偏移)]
        self._items = {}            # 已画到画布上的页 -> (item id, photo)
        self._pending = set()       # 正在解码的页
        self._queue = queue.Queue()
        self._worker = None
        self._total_h = 1
        self.zoom = 1.0             # 1.0 = 适应窗口
        self._scroll_w = 1
        self._src = None            # 当前页解码缓存（翻页动画/缩放复用）
        self._src_path = None
        self._prev_img = None       # 上一帧图像，用于交叉淡入
        self._page_item = None
        self._dragged = False
        self._drag_from = (0, 0)
        self._mouse = None          # 画布内最后的鼠标位置，缩放锚点
        self._cur_img_size = (1, 1)  # 单页模式当前渲染出的图片尺寸

        self.win = tk.Toplevel(app.root)
        self.win.title(f'阅读 - {title}')
        self.win.configure(bg=app.BG)
        wx, wy, ww, wh = app._work_area()
        w = min(self.px(900), int(ww) - self.px(40))
        h = min(self.px(940), int(wh) - self.px(40))
        self.win.geometry(f'{int(w)}x{int(h)}+{int(wx + (ww - w) / 2)}+{int(wy + (wh - h) / 2)}')
        self.win.minsize(self.px(520), self.px(420))
        try:
            ico = os.path.join(APP_DIR, 'icon.ico')
            if os.path.isfile(ico):
                self.win.iconbitmap(ico)
        except Exception:
            pass

        # ---- 顶部控制条
        bar = tk.Frame(self.win, bg=app.CARD)
        bar.pack(fill='x')
        self._btn = lambda text, cmd, kind='ghost': app._button(bar, text, cmd, kind)
        self._btn('‹ 上一页', lambda: self.flip(-1)).pack(side='left', padx=(self.px(10), 0),
                                                         pady=self.px(8))
        self._btn('下一页 ›', lambda: self.flip(1)).pack(side='left', padx=self.px(6),
                                                         pady=self.px(8))
        self.var_page = tk.StringVar(value='0 / 0')
        tk.Label(bar, textvariable=self.var_page, bg=app.CARD, fg=app.FG,
                 font=app._font(10, True)).pack(side='left', padx=self.px(12))

        # 阅读模式切换
        self.mode = 'single'
        mseg = dict(font=app._font(8, True), relief='flat', bd=0, cursor='hand2',
                    highlightthickness=0, padx=self.px(9), pady=self.px(4))
        self.btn_single = tk.Button(bar, text='单页', command=lambda: self.set_mode('single'), **mseg)
        self.btn_single.pack(side='left', padx=(self.px(4), 0), pady=self.px(8))
        self.btn_scroll = tk.Button(bar, text='滚动', command=lambda: self.set_mode('scroll'), **mseg)
        self.btn_scroll.pack(side='left', padx=(self.px(3), 0), pady=self.px(8))

        # 缩放
        self.var_zoom = tk.StringVar(value='适应')
        tk.Button(bar, text='－', command=lambda: self.zoom_by(1 / 1.25), **mseg).pack(
            side='left', padx=(self.px(8), 0), pady=self.px(8))
        self.btn_zoom_fit = tk.Button(bar, textvariable=self.var_zoom, command=self.zoom_fit, **mseg)
        self.btn_zoom_fit.configure(width=5)
        self.btn_zoom_fit.pack(side='left', padx=self.px(2), pady=self.px(8))
        tk.Button(bar, text='＋', command=lambda: self.zoom_by(1.25), **mseg).pack(
            side='left', padx=(0, 0), pady=self.px(8))

        self._btn('打开文件夹', self.open_folder).pack(side='right', padx=(0, self.px(10)),
                                                       pady=self.px(8))

        self.chapter_combo = None
        if len(self.chapters) > 1:
            tk.Label(bar, text='章节', bg=app.CARD, fg=app.MUTED,
                     font=app._font(9)).pack(side='right', padx=(self.px(6), self.px(4)))
            app.style.configure('JM.TCombobox', fieldbackground=app.FIELD,
                                background=app.CARD2, foreground=app.FG,
                                arrowcolor=app.MUTED, bordercolor=app.BORDER)
            app.style.map('JM.TCombobox',
                          fieldbackground=[('readonly', app.FIELD)],
                          foreground=[('readonly', app.FG)],
                          background=[('readonly', app.CARD2)],
                          selectbackground=[('readonly', app.FIELD)],
                          selectforeground=[('readonly', app.FG)])
            for opt, val in (('*TCombobox*Listbox.background', app.FIELD),
                             ('*TCombobox*Listbox.foreground', app.FG),
                             ('*TCombobox*Listbox.selectBackground', app.ACCENT),
                             ('*TCombobox*Listbox.selectForeground', app.ON_ACCENT)):
                self.win.option_add(opt, val)
            self.chapter_combo = app.ttk.Combobox(
                bar, values=[c[0] for c in self.chapters], state='readonly',
                width=22, font=app._font(9), style='JM.TCombobox')
            self.chapter_combo.pack(side='right', pady=self.px(8))
            self.chapter_combo.bind('<<ComboboxSelected>>',
                                    lambda e: self.load_chapter(self.chapter_combo.current()))

        # ---- 画布（单页 / 滚动 两种模式共用）
        body = tk.Frame(self.win, bg=app.VIEWER_BG)
        body.pack(fill='both', expand=True)
        self.vsb = app.ttk.Scrollbar(body, orient='vertical', style='JM.Vertical.TScrollbar',
                                     command=self._on_scrollbar)
        self.canvas = tk.Canvas(body, bg=app.VIEWER_BG, highlightthickness=0, bd=0,
                                yscrollincrement=self.px(40), xscrollincrement=self.px(40))
        self.canvas.pack(side='left', fill='both', expand=True)
        self.canvas.bind('<Configure>', self._on_resize)
        self.canvas.bind('<Motion>', self._on_motion)
        self.canvas.bind('<Enter>', self._on_motion)
        self.canvas.bind('<ButtonPress-1>', self._on_press)
        self.canvas.bind('<B1-Motion>', self._on_drag)
        self.canvas.bind('<ButtonRelease-1>', self._on_release)

        # ---- 底部状态
        self.var_name = tk.StringVar(value='')
        foot = tk.Frame(self.win, bg=app.CARD)
        foot.pack(fill='x')
        tk.Label(foot, textvariable=self.var_name, bg=app.CARD, fg=app.MUTED,
                 font=app._font(8), anchor='w').pack(side='left', padx=self.px(10),
                                                     pady=self.px(6))

        self.win.bind('<Escape>', lambda e: self.close())
        for seq in ('<Left>', '<Prior>'):
            self.win.bind(seq, lambda e: self.flip(-1))
        for seq in ('<Right>', '<Next>', '<space>'):
            self.win.bind(seq, lambda e: self.flip(1))
        self.win.bind('<Home>', lambda e: self.goto(0))
        self.win.bind('<End>', lambda e: self.goto(len(self.pages) - 1))
        self.win.protocol('WM_DELETE_WINDOW', self.close)
        app._apply_dark_titlebar(self.win)

        # 滚轮：Windows 的 WM_MOUSEWHEEL 是发给"当前焦点窗口"的，
        # 只绑在 Canvas 上永远不会触发（Canvas 默认拿不到焦点），所以绑到 Toplevel，
        # 事件从子控件冒泡上来；同时让 Canvas 拿到焦点。
        for w in (self.win, self.canvas):
            w.bind('<MouseWheel>', self._on_wheel)
        if self.chapter_combo is not None:
            self.chapter_combo.bind('<MouseWheel>', self._on_wheel)   # 别让下拉框吃掉滚轮
        self.canvas.configure(takefocus=1)
        self.win.after(120, lambda: (self.canvas.focus_set(), None)[1])

        if self.chapters:
            self.load_chapter(0)
        else:
            self.var_page.set('没有图片')
        self._refresh_mode_buttons()
        app._fade_in(self.win)

    # ---------------- 行为

    def _on_scrollbar(self, *args):
        self.canvas.yview(*args)
        self._update_visible()

    def _on_wheel(self, e):
        self._mouse = (e.x, e.y)
        # Ctrl+滚轮 = 缩放；Shift+滚轮 = 横向滚动；其余按模式
        if e.state & 0x0004:
            self._zoom_to(self.zoom * (1.25 if e.delta > 0 else 1 / 1.25), (e.x, e.y))
            return 'break'
        if e.state & 0x0001:
            self.canvas.xview_scroll(-2 if e.delta > 0 else 2, 'units')
            return 'break'
        if self.mode == 'scroll':
            self.canvas.yview_scroll(-3 if e.delta > 0 else 3, 'units')
            self._update_visible()
        elif self.zoom > 1.0:
            self.canvas.yview_scroll(-2 if e.delta > 0 else 2, 'units')   # 放大后滚轮=上下平移
        else:
            self.flip(-1 if e.delta > 0 else 1)
        return 'break'

    # ---------------- 拖动平移 / 点击翻页

    def _on_press(self, e):
        self._drag_from = (e.x, e.y)
        self._dragged = False
        try:
            self.canvas.scan_mark(e.x, e.y)
        except Exception:
            pass
        try:
            self.canvas.focus_set()
        except Exception:
            pass

    def _on_drag(self, e):
        if abs(e.x - self._drag_from[0]) + abs(e.y - self._drag_from[1]) > self.px(6):
            self._dragged = True
        if self._dragged:
            try:
                self.canvas.scan_dragto(e.x, e.y, gain=1)
            except Exception:
                pass
            if self.mode == 'scroll':
                self._update_visible()

    def _on_release(self, e):
        if not self._dragged and self.mode == 'single':
            self.flip(-1 if e.x < self.canvas.winfo_width() / 2 else 1)
        self._dragged = False

    # ---------------- 缩放（以鼠标所指位置为中心）

    def _on_motion(self, e):
        self._mouse = (e.x, e.y)

    def _canvas_center(self):
        return (max(1, self.canvas.winfo_width()) // 2,
                max(1, self.canvas.winfo_height()) // 2)

    def zoom_by(self, factor):
        self._zoom_to(self.zoom * factor)

    def set_zoom(self, z):
        self._zoom_to(z)

    def zoom_fit(self):
        self._zoom_to(1.0)

    def _zoom_to(self, new_zoom, anchor=None):
        """缩放，并让 anchor（默认=鼠标位置）下的那一点保持在原地"""
        z = max(0.5, min(4.0, round(float(new_zoom), 3)))
        if abs(z - self.zoom) < 1e-6:
            return
        anchor = anchor or self._mouse or self._canvas_center()
        rel = self._capture_anchor(anchor)
        self.zoom = z
        self._refresh_zoom_label()
        if self.mode == 'scroll':
            self._build_scroll(keep=self.idx)
        else:
            self._render()
        if rel:
            self._restore_anchor(rel, anchor)

    def _capture_anchor(self, anchor):
        """记录鼠标所指那一点在当前画面里的相对位置"""
        cx, cy = anchor
        try:
            X, Y = self.canvas.canvasx(cx), self.canvas.canvasy(cy)
        except Exception:
            return None
        if self.mode == 'single':
            w, h = self._cur_img_size
            if w <= 1 or h <= 1:
                return None
            W = max(self.canvas.winfo_width(), w)
            H = max(self.canvas.winfo_height(), h)
            ox, oy = (W - w) / 2.0, (H - h) / 2.0
            return ('single', None, (X - ox) / w, (Y - oy) / h)
        for i, _p, w, h, y in self.layout:
            if y <= Y <= y + h:
                ox = (self._scroll_w - w) / 2.0
                return ('scroll', i, (X - ox) / max(1, w), (Y - y) / max(1, h))
        return None

    def _restore_anchor(self, rel, anchor):
        kind, idx, rx, ry = rel
        if kind == 'single':
            w, h = self._cur_img_size
            if w <= 1 or h <= 1:
                return
            W = max(self.canvas.winfo_width(), w)
            H = max(self.canvas.winfo_height(), h)
            X = (W - w) / 2.0 + rx * w
            Y = (H - h) / 2.0 + ry * h
        else:
            page = next((x for x in self.layout if x[0] == idx), None)
            if page is None:
                return
            _i, _p, w, h, y = page
            X = (self._scroll_w - w) / 2.0 + rx * w
            Y = y + ry * h
        self._scroll_to_point(X, Y, anchor)

    def _scroll_to_point(self, X, Y, anchor):
        """让画面坐标 (X,Y) 落在窗口里的 anchor 位置"""
        cx, cy = anchor
        cw = max(1, self.canvas.winfo_width())
        ch = max(1, self.canvas.winfo_height())
        if self.mode == 'single':
            W = max(cw, self._cur_img_size[0])
            H = max(ch, self._cur_img_size[1])
        else:
            W = max(cw, self._scroll_w)
            H = max(ch, self._total_h + self.px(56))
        try:
            self.canvas.xview_moveto(min(1.0, max(0.0, (X - cx) / W)) if W > cw else 0.0)
            self.canvas.yview_moveto(min(1.0, max(0.0, (Y - cy) / H)) if H > ch else 0.0)
        except Exception:
            pass
        if self.mode == 'scroll':
            self._update_visible()

    def _refresh_zoom_label(self):
        self.var_zoom.set('适应' if abs(self.zoom - 1.0) < 1e-6
                          else f'{int(round(self.zoom * 100))}%')

    def _on_resize(self, _e=None):
        if self._after:
            try:
                self.win.after_cancel(self._after)
            except Exception:
                pass
        self._after = self.win.after(120, self._redraw)

    def _redraw(self):
        self._after = None
        if self.mode == 'scroll':
            self._build_scroll(keep=self.idx)
        else:
            self._render()

    # ---------------- 模式

    def set_mode(self, mode):
        if mode == self.mode:
            return
        cur = self.idx
        self.mode = mode
        if mode == 'scroll':
            self.vsb.pack(side='right', fill='y')
            self.canvas.delete('all')
            self._build_scroll(keep=cur)
        else:
            self.vsb.pack_forget()
            self._items.clear()
            self._pending.clear()
            self.layout = []
            self.canvas.configure(scrollregion=(0, 0, 0, 0))
            self.canvas.yview_moveto(0)
            self.goto(cur)
        self._refresh_mode_buttons()

    def _refresh_mode_buttons(self):
        for btn, active in ((self.btn_single, self.mode == 'single'),
                            (self.btn_scroll, self.mode == 'scroll')):
            btn.configure(bg=self.app.ACCENT if active else self.app.CARD2,
                          fg=self.app.ON_ACCENT if active else self.app.MUTED,
                          activebackground=self.app.ACCENT_HI if active else self.app.BORDER)

    # ---------------- 滚动模式

    def _scaled_size(self, path, target_w):
        """只读图片头取尺寸；缩放 100% 时若太高就按高度适配，避免一屏只看到半页"""
        try:
            from PIL import Image

            with Image.open(path) as im:
                w, h = im.size
        except Exception:
            return target_w, int(target_w * 1.45)

        if self.zoom > 1.0:
            pw = max(1, int(target_w * self.zoom))
            return pw, max(1, int(h * pw / max(1, w))) + self.px(6)

        cap_h = max(self.px(240), self.canvas.winfo_height() - self.px(6))
        pw = target_w
        ph = int(h * pw / max(1, w))
        if ph > cap_h:
            pw = max(1, int(w * cap_h / max(1, h)))
            ph = cap_h
        return pw, ph + self.px(6)

    def _max_page_w(self):
        return max([x[2] for x in self.layout] + [1])

    def _build_scroll(self, keep=None):
        self.canvas.delete('all')
        self._items.clear()
        self._pending.clear()
        self.layout = []
        if not self.pages:
            return
        tw = max(self.px(200), self.canvas.winfo_width() - self.px(8))
        y = 0
        for i, p in enumerate(self.pages):
            w, h = self._scaled_size(p, tw)
            self.layout.append((i, p, w, h, y))
            y += h
        self._total_h = max(1, y)
        self._scroll_w = max(self.canvas.winfo_width(), self._max_page_w())
        self.canvas.configure(scrollregion=(0, 0, self._scroll_w, y + self.px(56)))
        self.canvas.create_text(self._scroll_w // 2, y + self.px(24), text='—— 本话结束 ——',
                                fill=self.app.MUTED, font=self.app._font(9))
        if keep is not None and 0 <= keep < len(self.layout):
            self.canvas.yview_moveto(self.layout[keep][4] / self._total_h)
        self._update_visible()

    def _update_visible(self):
        """只渲染视口附近的页，其它页释放掉，避免几十页全解码占内存"""
        if self.mode != 'scroll' or not self.layout:
            return
        vh = max(1, self.canvas.winfo_height())
        top = self.canvas.canvasy(0)
        bottom = top + vh
        keep = set()
        for i, p, w, h, y in self.layout:
            if y + h >= top - vh and y <= bottom + vh:
                keep.add(i)
                if i not in self._items and i not in self._pending:
                    self._pending.add(i)
                    self._queue.put((i, p, w, h, y))
        for i in list(self._items):
            if i not in keep:
                item, _photo = self._items.pop(i)
                self.canvas.delete(item)
        for i, p, w, h, y in self.layout:
            if y + h > top + self.px(20):
                self.idx = i
                self.var_page.set(f'{i + 1} / {len(self.pages)}')
                self.var_name.set(
                    f'{os.path.basename(p)}    （第 {self.ci + 1} 话 / 共 {len(self.chapters)} 话）')
                break
        self._ensure_worker()

    def _ensure_worker(self):
        if self._worker is not None and self._worker.is_alive():
            return
        self._worker = threading.Thread(target=self._decode_worker, daemon=True)
        self._worker.start()

    def _decode_worker(self):
        while True:
            try:
                i, p, w, h, y = self._queue.get_nowait()
            except queue.Empty:
                return
            try:
                from PIL import Image

                im = Image.open(p).convert('RGB')
                im = im.resize((max(1, w), max(1, h - self.px(6))), Image.LANCZOS)
            except Exception:
                im = None
            self.app.q.put(('readypage', (self, i, im, w, h, y)))

    def place_page(self, i, img, w, h, y):
        """主线程：把解码好的页画到画布上"""
        self._pending.discard(i)
        if img is None or self.mode != 'scroll' or i in self._items:
            return
        try:
            from PIL import ImageTk

            photo = ImageTk.PhotoImage(img)
            cx = max(w, self._scroll_w) // 2
            item = self.canvas.create_image(cx, y + self.px(3), image=photo, anchor='n')
            self._items[i] = (item, photo)
        except Exception:
            pass

    # ---------------- 翻页 / 换话

    def load_chapter(self, ci, idx=0):
        if not self.chapters:
            return
        ci = max(0, min(int(ci), len(self.chapters) - 1))
        self.ci = ci
        name, pages = self.chapters[ci]
        self.pages = pages
        self.idx = idx if idx >= 0 else max(0, len(pages) + idx)
        if self.chapter_combo is not None:
            self.chapter_combo.set(name)
        if self.mode == 'scroll':
            self.canvas.yview_moveto(0)
            self._build_scroll(keep=self.idx)
        else:
            self._render()

    def goto(self, idx):
        if not self.pages:
            return
        self.idx = max(0, min(int(idx), len(self.pages) - 1))
        if self.mode == 'scroll' and self.layout:
            self.canvas.yview_moveto(self.layout[self.idx][4] / max(1, self._total_h))
            self._update_visible()
        else:
            self._render()

    def flip(self, delta):
        if not self.pages:
            return
        if self.mode == 'scroll':
            self.goto(self.idx + delta)                # 滚动模式：直接跳到上/下一页
            return
        n = self.idx + delta
        if 0 <= n < len(self.pages):
            self.goto(n)
        elif n >= len(self.pages) and self.ci + 1 < len(self.chapters):
            self.load_chapter(self.ci + 1, 0)          # 翻到下一话
        elif n < 0 and self.ci > 0:
            self.load_chapter(self.ci - 1, -1)         # 回到上一话最后一页

    def _render(self):
        self._after = None
        if not self.pages or self.mode != 'single':
            return
        path = self.pages[self.idx]
        try:
            from PIL import Image, ImageTk

            if self._src_path == path and self._src is not None:
                img = self._src                     # 同一页复用解码结果
            else:
                img = Image.open(path).convert('RGB')
                self._src, self._src_path = img, path
        except Exception as e:
            self.app.log(f'读取图片失败：{e}', 'err')
            return

        cw = max(64, self.canvas.winfo_width())
        ch = max(64, self.canvas.winfo_height())
        iw, ih = img.size
        fit = min(cw / iw, ch / ih)
        scale = fit * self.zoom
        nw, nh = max(1, int(iw * scale)), max(1, int(ih * scale))
        if (nw, nh) != (iw, ih):
            img = img.resize((nw, nh), Image.LANCZOS)
        self._cur_img_size = (nw, nh)
        W, H = max(cw, nw), max(ch, nh)
        self.canvas.configure(scrollregion=(0, 0, W, H))
        self.canvas.delete('all')
        prev = self._prev_img
        self._photo = ImageTk.PhotoImage(img)      # 留引用
        self._page_item = self.canvas.create_image(W // 2, H // 2, image=self._photo,
                                                   anchor='center')
        # 翻页交叉淡入（同尺寸才做，缩放时尺寸变了就直出）
        if ANIM_ON and prev is not None and prev.size == img.size:
            def step(t):
                try:
                    blended = Image.blend(prev, img, max(0.03, t))
                    photo = ImageTk.PhotoImage(blended)
                    self._photo = photo
                    self.canvas.itemconfigure(self._page_item, image=photo)
                except Exception:
                    pass
            self.app._anim(6, step, first_now=False)
        self._prev_img = img
        # 放大后从顶部开始看
        if W > cw:
            self.canvas.xview_moveto((W - cw) / (2.0 * W))
        if H > ch:
            self.canvas.yview_moveto(0)
        self.var_page.set(f'{self.idx + 1} / {len(self.pages)}')
        self.var_name.set(f'{os.path.basename(path)}    （第 {self.ci + 1} 话 / 共 {len(self.chapters)} 话）')

    def open_folder(self):
        try:
            os.startfile(self.root_dir)
        except Exception as e:
            self.app.log(f'打开文件夹失败：{e}', 'err')

    def close(self):
        try:
            self.win.destroy()
        except Exception:
            pass
        if self in self.app._readers:
            self.app._readers.remove(self)


def main():
    app = JMApp()
    if '--selftest' in sys.argv:
        app.root.withdraw()
        app.root.update()
        app.log('selftest 日志', 'dim')
        app.q.put(('log', ('队列事件', '')))
        app._drain()
        assert 'selftest 日志' in app.text.get('1.0', 'end')
        app.q.put(('total', 10))
        app.q.put(('tick', 3))
        app._drain()
        assert '3/10' in app.var_status.get(), app.var_status.get()
        assert parse_ids('JM123 p456 x') == [('album', '123'), ('photo', '456')]
        # 保存位置改写（三种 YAML 形态）
        out1 = _rewrite_base_dir("dir_rule:\n  base_dir: 'C:\\old'\n", r'C:\new')
        assert "base_dir: 'C:\\new'" in out1 and out1.count('base_dir') == 1, out1
        out2 = _rewrite_base_dir('dir_rule:\n  rule: Bd_Pname\n', r'D:\jm')
        assert out2.count('dir_rule') == 1 and 'base_dir' in out2, out2
        out3 = _rewrite_base_dir('download:\n  threading:\n    image: 8\n', r'D:\jm')
        assert out3.startswith('download:') and 'dir_rule:' in out3, out3
        opt, base = build_option()
        assert os.path.isabs(base), base
        print('selftest OK')
        print('scale =', app.scale)
        print('base_dir =', base)
        print('option_file =', _config_file())
        print('parse_ids =', parse_ids('JM123 p456 x'))
        print('progress =', app.var_status.get(), app.var_pct.get())
        app.root.destroy()
        return

    if '--selftest-dir' in sys.argv:
        # 真实地改一次保存位置，验证「界面 → option.yml → 下载目录」整条链路，然后还原
        app.root.withdraw()
        cfg = _config_file()
        original = None
        if cfg and os.path.isfile(cfg):
            with open(cfg, encoding='utf-8') as f:
                original = f.read()
        tmp = os.path.join(tempfile.gettempdir(), 'jmcomic_gui_dir_test')
        os.makedirs(tmp, exist_ok=True)
        try:
            app.set_base_dir(tmp)
            opt, base = build_option()
            print('footer        =', app.var_foot.get())
            print('option.yml    =', [l.strip() for l in
                                     open(cfg, encoding='utf-8') if 'base_dir' in l])
            print('option object =', opt.dir_rule.base_dir)
            print('build_option  =', base)
            assert os.path.normcase(base) == os.path.normcase(tmp), (base, tmp)
            assert os.path.normcase(str(opt.dir_rule.base_dir)) == os.path.normcase(tmp)
            print('SELFTEST-DIR OK')
        finally:
            if original is not None:
                with open(cfg, 'w', encoding='utf-8') as f:
                    f.write(original)
                print('option.yml restored')
            app.root.destroy()
        return

    if '--selftest-cover' in sys.argv:
        # 用合成图验证封面渲染链路；真实封面走的是同一段渲染代码
        app.root.withdraw()
        app.root.update()
        from PIL import Image, ImageDraw

        tmp = os.path.join(tempfile.gettempdir(), 'jmcomic_cover_test.jpg')
        im = Image.new('RGB', (600, 800), (34, 38, 50))
        d = ImageDraw.Draw(im)
        for y in range(0, 800, 40):
            d.line([(0, y), (600, y)], fill=(62, 70, 92))
        d.rectangle([50, 50, 550, 750], outline=(76, 141, 255), width=10)
        im.save(tmp, quality=90)

        app._show_cover(tmp)
        app.root.update()
        assert app._cover_photo is not None, '缩略图没生成'
        tw, th = app._cover_photo.width(), app._cover_photo.height()
        assert tw <= app.px(124) and th <= app.px(166), (tw, th)
        assert bool(app.cover_label.cget('image')), 'Label 上没有图'
        print('cover thumb =', tw, 'x', th, '(上限', app.px(124), 'x', app.px(166), ')')
        print('cover path  =', app._cover_path)

        app._clear_cover('无封面')
        app.root.update()
        assert app._cover_photo is None
        print('clear ok, text =', app.cover_label.cget('text'))

        app._show_cover(os.path.join(tempfile.gettempdir(), '不存在.jpg'))
        app.root.update()
        assert app._cover_photo is None, '坏图没被兜住'
        print('坏图兜底 ok, text =', app.cover_label.cget('text'))

        # 真实网络分支（不存在的车号，只验证不崩）
        opt, _base = build_option()
        client = opt.new_jm_client()

        class _Fake:
            id = '999999999'

        app._fetch_cover(client, _Fake(), 'album', 'album:999999999')
        got = None
        deadline = time.time() + 90
        while time.time() < deadline:
            k, p = app.q.get()
            if k == 'cover':
                got = (k, p)
                break
        print('真实下载分支(坏id) ->', got)
        assert got is not None and got[0] == 'cover'
        print('SELFTEST-COVER OK')
        app.root.destroy()
        return

    if '--selftest-multi' in sys.argv:
        # 回归：查多个车号时，每条都要在列表里出现（而不是只显示最后一个）
        app.root.withdraw()
        app.root.update()
        from PIL import Image, ImageDraw

        def make_cover(name, color):
            p = os.path.join(tempfile.gettempdir(), name)
            im = Image.new('RGB', (400, 533), color)
            ImageDraw.Draw(im).text((30, 250), name, fill=(255, 255, 255))
            im.save(p, quality=85)
            return p

        covers = {
            'album:111': make_cover('c111.jpg', (60, 70, 110)),
            'album:222': make_cover('c222.jpg', (110, 70, 60)),
            'album:333': make_cover('c333.jpg', (70, 110, 80)),
        }
        infos = [
            ('album:111', '第一个本子 First Album', 50, 2),
            ('album:222', '第二个本子 Second Album', 80, 3),
            ('album:333', '第三个本子 Third Album', 12, 1),
        ]
        for key, title, pages, chapters in infos:
            app.q.put(('info', (key, {'id': key.split(':')[1], 'kind': '本子',
                                      'title': title, 'authors': 'Author', 'pages': pages,
                                      'chapters': chapters, 'tags': 'A / B'})))
        app._drain()
        app.root.update()

        rows = [w for w in app.res_inner.winfo_children()]
        print('结果行数     =', len(rows), '(期望 3)')
        assert len(rows) == 3, len(rows)
        print('列表标题     =', app.btn_view_res.cget('text'))
        assert '3' in app.btn_view_res.cget('text')
        print('默认选中     =', app.var_title.get())
        assert app.var_title.get().endswith('First Album'), app.var_title.get()

        for key, path in covers.items():
            app.q.put(('cover', (key, path)))
        app._drain()
        app.root.update()
        print('行缩略图数量 =', len(app._row_photos), '(期望 3)')
        assert len(app._row_photos) == 3, list(app._row_photos)
        assert app._cover_photo is not None, '大卡片封面没显示'

        app._select_result('album:222')
        app.root.update()
        print('切换选中后   =', app.var_title.get())
        assert 'Second Album' in app.var_title.get(), app.var_title.get()
        assert app.selected_key == 'album:222'

        app.q.put(('failed', 'album:999'))
        app._drain()
        app.root.update()
        rows2 = app.res_inner.winfo_children()
        print('加一行失败后 =', len(rows2), '(期望 4)')
        assert len(rows2) == 4

        app._clear_results()
        app.root.update()
        assert not app.res_inner.winfo_children()
        print('清空结果 ok')
        print('SELFTEST-MULTI OK')
        app.root.destroy()
        return

    if '--selftest-history' in sys.argv:
        # 历史记录：去重、保留"已下载"标记、上限、落盘重载、视图渲染、删除、清空
        app.root.withdraw()
        app.root.update()
        global HISTORY_FILE
        real_file = HISTORY_FILE
        tmp = os.path.join(tempfile.gettempdir(), 'jmcomic_history_test.json')
        HISTORY_FILE = tmp
        if os.path.isfile(tmp):
            os.remove(tmp)
        app.history = []
        try:
            for i in (101, 202, 303):
                app._history_record(f'album:{i}', '查询',
                                    {'id': str(i), 'kind': '本子', 'title': f'历史本子 {i}',
                                     'authors': 'A', 'pages': 10, 'chapters': 1, 'tags': 'T'})
            ids = [it['id'] for it in app.history]
            print('三条之后     =', len(app.history), ids)
            assert len(app.history) == 3 and ids == ['303', '202', '101'], ids

            app._history_record('album:202', '下载', app._find_info('album:202'))
            assert len(app.history) == 3
            hit = [it for it in app.history if it['key'] == 'album:202'][0]
            print('重复加不新增 =', len(app.history), '| 动作 =', hit['action'])
            assert hit['action'] == '下载', hit

            app._history_record('album:202', '查询', {'id': '202', 'kind': '本子', 'title': '再查一次'})
            hit = [it for it in app.history if it['key'] == 'album:202'][0]
            print('下载标记保留 =', hit['action'], '| 标题已更新 =', hit['title'])
            assert hit['action'] == '下载' and hit['title'] == '再查一次'

            for i in range(HISTORY_MAX + 40):
                app._history_record(f'album:8{i:04d}', '查询',
                                    {'id': str(i), 'kind': '本子', 'title': 'x'})
            print('超上限后     =', len(app.history), '(上限', HISTORY_MAX, ')')
            assert len(app.history) == HISTORY_MAX

            saved = load_history()
            print('落盘并重载   =', len(saved), '| 最新 =', saved[0]['id'])
            assert len(saved) == HISTORY_MAX

            app._switch_view('history')
            app.root.update()
            rows = len(app.res_inner.winfo_children())
            print('历史视图行数 =', rows, '| 按钮 =', app.btn_view_hist.cget('text'))
            assert rows == HISTORY_MAX, rows

            before = len(app.history)
            app._history_remove(app.history[0]['key'])
            print('删除一条     =', len(app.history), '| 文件里 =', len(load_history()))
            assert len(app.history) == before - 1 and len(load_history()) == before - 1

            app.history = []
            save_history(app.history)
            app._render_view()
            app.root.update()
            assert not app.res_inner.winfo_children()
            print('清空 ok')

            # 真实事件链路：worker 投队列 → 主线程落历史
            app.q.put(('info', ('album:777', {'id': '777', 'kind': '本子', 'title': '走队列写的',
                                              'authors': 'A', 'pages': 1, 'chapters': 1, 'tags': 'T'})))
            app.q.put(('history', ('album:777', '查询', r'C:\tmp\fake_album')))
            app._drain()
            app.root.update()
            print('经队列写入   =', len(app.history),
                  '| 标题 =', app.history[0]['title'] if app.history else '')
            assert len(app.history) == 1 and app.history[0]['title'] == '走队列写的'
            assert app.history[0].get('dir') == r'C:\tmp\fake_album', app.history[0]
            print('目录也记住了 =', app.history[0]['dir'])
            assert len(load_history()) == 1
            print('SELFTEST-HISTORY OK')
        finally:
            HISTORY_FILE = real_file
            if os.path.isfile(tmp):
                os.remove(tmp)
            app.root.destroy()
        return

    if '--selftest-rule' in sys.argv:
        # 章节目录补零 + 本子层
        app.root.withdraw()
        from jmcomic.jm_entity import JmAlbumDetail, JmPhotoDetail
        from jmcomic.jm_option import DirRule

        album = JmAlbumDetail('350234', '220980', '某本子标题',
                              [('350240', '1', '第1話'), ('350241', '2', '第2話')],
                              50, '', '', '', '', 0, [], [], ['作者'], ['标签'])
        # 多话本子：series_id = 本子id，章节序号取 sort 字段
        p1 = JmPhotoDetail('350240', '第1話', 350234, 1, from_album=album)
        p2 = JmPhotoDetail('350241', '第2話', 350234, 2, from_album=album)
        d = DirRule(DEFAULT_DIR_RULE, base_dir=r'C:\out')
        out1 = d.apply_rule_to_path(album, p1)
        out2 = d.apply_rule_to_path(album, p2)
        print('第1話 ->', out1)
        print('第2話 ->', out2)
        assert out1.startswith('C:/out/[JM350234]'), out1
        assert out1.endswith('/001-第1話'), out1
        assert '作者' in out1 and '某本子标题' in out1, out1
        assert out2.endswith('/002-第2話'), out2

        # 单章本子（series_id=0）序号归一为 1
        solo = JmAlbumDetail('350234', '220980', '单章本子', [], 50,
                             '', '', '', '', 0, [], [], ['作者'], ['标签'])
        psolo = JmPhotoDetail('350234', '单章本子', 0, 2, from_album=solo)
        print('单章   ->', d.apply_rule_to_path(solo, psolo))

        # 第 10 话要能排在 第 2 话 后面（补零的意义）
        many = [('35024%d' % i, str(i), '第%d話' % i) for i in range(1, 11)]
        album_many = JmAlbumDetail('999', '1', '十话本子', many, 200,
                                   '', '', '', '', 0, [], [], [], [])
        p10 = JmPhotoDetail('3502410', '第10話', 999, 10, from_album=album_many)
        out10 = d.apply_rule_to_path(album_many, p10)
        print('第10話 ->', out10)
        assert out10.endswith('/010-第10話'), out10
        assert 'default_author' in out10        # 站点没给作者时的兜底值
        print('排序检查 ->', sorted([out2.rsplit('/', 1)[1], out10.rsplit('/', 1)[1]]))

        opt, base = build_option()
        cur = opt.dir_rule.apply_rule_to_path(album, p2)
        print('配置里的规则 =', cur)
        print('保存位置     =', base)
        assert '002-第2話' in cur, cur
        assert cur.startswith(str(base).replace('\\', '/')), cur
        print('SELFTEST-RULE OK')
        app.root.destroy()
        return

    if '--selftest-reader' in sys.argv:
        # 阅读器：章节顺序、翻页、跨话翻页
        app.root.withdraw()
        app.root.update()
        from PIL import Image as PILImage
        from PIL import ImageDraw

        base = os.path.join(tempfile.gettempdir(), 'jmcomic_reader_test')
        album_dir = os.path.join(base, '[JM350234]测试本子')
        names = ['001-第1話', '002-第2話', '010-第10話']
        for ci, cname in enumerate(names):
            d = os.path.join(album_dir, cname)
            os.makedirs(d, exist_ok=True)
            for pi in (1, 2, 3):
                p = os.path.join(d, f'{pi:05d}.jpg')
                im = PILImage.new('RGB', (400, 560), (30 + ci * 45, 40, 60))
                ImageDraw.Draw(im).text((40, 260), f'{cname} #{pi}', fill=(255, 255, 255))
                im.save(p, quality=80)

        chs = discover_chapters(album_dir)
        print('章节顺序   =', [c[0] for c in chs])
        assert [c[0] for c in chs] == names, [c[0] for c in chs]
        print('每话页数   =', [len(c[1]) for c in chs])
        print('图片顺序   =', [os.path.basename(p) for p in chs[0][1]])
        assert [os.path.basename(p) for p in chs[0][1]] == ['00001.jpg', '00002.jpg', '00003.jpg']
        assert [len(c[1]) for c in chs] == [3, 3, 3]

        r = ReaderWindow(app, '测试本子', album_dir, chs)
        app._readers.append(r)
        app.root.update()
        print('初始       =', r.var_page.get(), '|', r.chapters[r.ci][0])
        assert r.var_page.get() == '1 / 3'
        r.flip(1)
        app.root.update()
        assert r.var_page.get() == '2 / 3', r.var_page.get()
        r.flip(1); r.flip(1)
        app.root.update()
        print('翻到下一话 =', r.var_page.get(), '|', r.chapters[r.ci][0])
        assert r.chapters[r.ci][0] == '002-第2話' and r.var_page.get() == '1 / 3'
        r.flip(-1)
        app.root.update()
        print('退回上一话 =', r.var_page.get(), '|', r.chapters[r.ci][0])
        assert r.chapters[r.ci][0] == '001-第1話' and r.var_page.get() == '3 / 3'
        r.load_chapter(2)
        app.root.update()
        print('切到第10话 =', r.chapters[r.ci][0], r.var_page.get())
        assert r.chapters[r.ci][0] == '010-第10話' and r._photo is not None

        # ---- 滚动模式
        r.load_chapter(0)
        app.root.update()
        r.set_mode('scroll')
        app.root.update()
        print('滚动模式   =', r.mode, '| 布局页数 =', len(r.layout), '| 总高 =', r._total_h)
        assert r.mode == 'scroll' and len(r.layout) == 3
        deadline = time.time() + 20
        while time.time() < deadline and not r._items:
            app.root.update()
            time.sleep(0.05)
        print('已渲染页数 =', len(r._items), '| 待解码 =', len(r._pending))
        assert r._items, '滚动模式一张都没渲染出来'
        r.goto(2)
        app.root.update()
        print('滚到第3页  =', r.var_page.get())
        assert r.var_page.get() == '3 / 3', r.var_page.get()
        r.set_mode('single')
        app.root.update()
        print('切回单页   =', r.mode, r.var_page.get(), '| 有图 =', r._photo is not None)
        assert r.mode == 'single' and r._photo is not None

        # ---- 缩放
        z0 = r.zoom
        r.zoom_by(1.25)
        app.root.update()
        print('放大       =', z0, '->', r.zoom, '|', r.var_zoom.get(), '| 有图 =', r._photo is not None)
        assert r.zoom > z0 and r._photo is not None
        r.set_zoom(4.0)
        app.root.update()
        print('放到 400%  =', r.var_zoom.get())
        assert abs(r.zoom - 4.0) < 1e-6
        r.set_zoom(9.0)
        app.root.update()
        print('上限裁剪   =', r.var_zoom.get())
        assert r.zoom <= 4.0
        r.zoom_fit()
        app.root.update()
        print('回到适应   =', r.var_zoom.get())
        assert abs(r.zoom - 1.0) < 1e-6

        # ---- 滚轮（合成事件，验证绑定真的生效）
        r.set_mode('scroll')
        app.root.update()
        for _ in range(10):
            app.root.update()
            time.sleep(0.03)
        y0 = r.canvas.yview()[0]
        r.win.event_generate('<MouseWheel>', delta=-120)
        app.root.update()
        y1 = r.canvas.yview()[0]
        print('滚轮滚动   =', round(y0, 4), '->', round(y1, 4))
        assert y1 > y0, '滚轮没生效'
        z1 = r.zoom
        r.win.event_generate('<MouseWheel>', delta=120, state=0x0004)
        app.root.update()
        print('Ctrl+滚轮  =', z1, '->', r.zoom, '|', r.var_zoom.get())
        assert r.zoom > z1, 'Ctrl+滚轮没缩放'

        # ---- 缩放锚点：鼠标所指那一点要保持不动
        r.set_zoom(1.0)
        app.root.update()
        for _ in range(10):
            app.root.update()
            time.sleep(0.03)
        page = r.layout[0]
        cx = int(r._scroll_w // 2)
        cy = int(page[4] + page[3] * 0.45)
        r._mouse = (cx, cy)
        rel0 = r._capture_anchor((cx, cy))
        r.set_zoom(1.5)
        app.root.update()
        rel1 = r._capture_anchor((cx, cy))
        print('锚点        = (%s) 缩放前后' % ', '.join(f'{v:.3f}' for v in rel0[2:]),
              '-> (%s)' % ', '.join(f'{v:.3f}' for v in rel1[2:]))
        assert rel0 and rel1, '取不到锚点'
        assert abs(rel0[2] - rel1[2]) < 0.06 and abs(rel0[3] - rel1[3]) < 0.06, (rel0, rel1)
        # 再放大一次，锚点仍应保持
        rel2 = r._capture_anchor((cx, cy))
        r.set_zoom(2.0)
        app.root.update()
        rel3 = r._capture_anchor((cx, cy))
        print('再放大      = (%s) -> (%s)' % (', '.join(f'{v:.3f}' for v in rel2[2:]),
                                            ', '.join(f'{v:.3f}' for v in rel3[2:])))
        assert abs(rel2[2] - rel3[2]) < 0.06 and abs(rel2[3] - rel3[3]) < 0.06, (rel2, rel3)
        r.set_zoom(1.0)
        app.root.update()

        x0 = r.canvas.xview()[0]
        r.win.event_generate('<MouseWheel>', delta=-120, state=0x0001)
        app.root.update()
        print('Shift+滚轮 =', round(x0, 4), '->', round(r.canvas.xview()[0], 4), '(横向)')

        r.close()
        app.root.update()
        assert not app._readers
        print('阅读器关闭 ok')
        print('SELFTEST-READER OK')
        app.root.destroy()
        return

    if '--selftest-local' in sys.argv:
        # 书库：扫描真实保存目录（只读），并用第一项真的打开阅读器
        app.root.withdraw()
        app.root.update()
        # 等后台线程把真实保存位置读出来（jmcomic 导入本身要 1~2 秒）
        fallback = os.path.join(FALLBACK_DIR, 'downloads')
        deadline = time.time() + 30
        while time.time() < deadline and os.path.normcase(app.base_dir) == os.path.normcase(fallback):
            app.root.update()
            time.sleep(0.1)
        app._drain()
        app.root.update()
        entries = scan_downloads(app.base_dir, app.history)
        print('保存位置   =', app.base_dir)
        print('识别到     =', len(entries), '项')
        for e in entries[:10]:
            print('   %-38s %5d页 %2d章  缩略图=%s' % (str(e['title'])[:36], e['pages'],
                                                     e['chapters'], bool(e['_thumb'])))
        assert entries, '没扫到任何已下载内容'
        assert all(e['pages'] > 0 for e in entries)

        skipped = []
        try:
            for d in os.listdir(app.base_dir):
                fp = os.path.join(app.base_dir, d)
                if os.path.isdir(fp) and not any(e['dir'] == fp for e in entries):
                    skipped.append(d)
        except Exception:
            pass
        print('跳过的目录 =', skipped, '（没有图片的目录会被跳过）')

        first = entries[0]
        chs = discover_chapters(first['dir'])
        print('打开第一项 =', first['dir'])
        print('           ', len(chs), '话 | 第一话', chs[0][0], len(chs[0][1]), '页')
        r = ReaderWindow(app, first['title'], first['dir'], chs)
        app._readers.append(r)
        app.root.update()
        print('阅读器页码 =', r.var_page.get(), '| 图片已解码 =', r._photo is not None)
        assert r._photo is not None
        r.flip(1)
        app.root.update()
        print('翻页后     =', r.var_page.get())
        r.close()
        print('SELFTEST-LOCAL OK')
        app.root.destroy()
        return

    if '--selftest-wheel' in sys.argv:
        # 用真实的系统滚轮事件验证（Windows 的滚轮是发给焦点窗口的，
        # 合成事件测不出这个链路的真伪）。会让本窗口短暂抢一下焦点。
        app.root.withdraw()
        app.root.update()
        from PIL import Image as PILImage
        from PIL import ImageDraw

        base = os.path.join(tempfile.gettempdir(), 'jmcomic_wheel_test')
        album_dir = os.path.join(base, '[JM1]滚轮测试')
        d = os.path.join(album_dir, '001-第1話')
        os.makedirs(d, exist_ok=True)
        for p in range(1, 9):
            im = PILImage.new('RGB', (800, 1200), (40, 56, 88))
            ImageDraw.Draw(im).text((40, 40), 'page %d' % p, fill=(255, 255, 255))
            im.save(os.path.join(d, '%05d.jpg' % p), quality=80)

        r = ReaderWindow(app, '滚轮测试', album_dir)
        app._readers.append(r)
        r.set_mode('scroll')
        app.root.update()
        for _ in range(40):
            app.root.update()
            time.sleep(0.05)

        import ctypes
        hwnd = r.win.winfo_id()
        user32 = ctypes.windll.user32
        parent = user32.GetParent(hwnd)
        target = parent if parent else hwnd

        # 后台进程直接 SetForegroundWindow 常被系统拒绝，先 AttachThreadInput 再抢
        try:
            fg = user32.GetForegroundWindow()
            t_fg = user32.GetWindowThreadProcessId(fg, None)
            t_me = ctypes.windll.kernel32.GetCurrentThreadId()
            user32.AttachThreadInput(t_fg, t_me, True)
            user32.ShowWindow(target, 9)
            user32.SetForegroundWindow(target)
            user32.SetFocus(target)
            user32.AttachThreadInput(t_fg, t_me, False)
        except Exception as e:
            print('抢焦点出错:', e)
        time.sleep(0.5)
        app.root.update()

        cx = r.canvas.winfo_rootx() + max(1, r.canvas.winfo_width()) // 2
        cy = r.canvas.winfo_rooty() + max(1, r.canvas.winfo_height()) // 2
        is_fg = (user32.GetForegroundWindow() == target)
        print('拿到前台焦点 =', is_fg)

        # Tk 在 Windows 上按"光标下的控件"派发滚轮事件，所以要把光标移进窗口
        # （测完还原到原来的位置）
        class POINT(ctypes.Structure):
            _fields_ = [('x', ctypes.c_long), ('y', ctypes.c_long)]

        saved = POINT()
        user32.GetCursorPos(ctypes.byref(saved))
        print('光标移入窗口 =', (saved.x, saved.y), '->', (cx, cy))

        def wheel(delta, times=3):
            user32.SetCursorPos(cx, cy)
            time.sleep(0.25)
            for _ in range(times):
                if is_fg:
                    user32.mouse_event(0x0800, 0, 0, delta, 0)     # MOUSEEVENTF_WHEEL
                else:
                    lparam = ((cy & 0xFFFF) << 16) | (cx & 0xFFFF)
                    user32.PostMessageW(target, 0x020A,
                                        (delta << 16) & 0xFFFFFFFF, lparam)
                time.sleep(0.2)
            for _ in range(10):
                app.root.update()
                time.sleep(0.05)

        try:
            y0 = r.canvas.yview()[0]
            wheel(-120)
            y1 = r.canvas.yview()[0]
            print('滚轮向下   =', round(y0, 5), '->', round(y1, 5), '| 当前页', r.var_page.get())
            assert y1 > y0, f'滚轮没生效（yview {y0} -> {y1}）'

            wheel(120, times=2)
            y2 = r.canvas.yview()[0]
            print('滚轮向上   =', round(y1, 5), '->', round(y2, 5))
            assert y2 < y1, '向上滚没生效'

            # Ctrl+滚轮 = 缩放
            z0 = r.zoom
            user32.SetCursorPos(cx, cy)
            ctypes.windll.user32.keybd_event(0x11, 0, 0, 0)        # Ctrl 按下
            wheel(-120, times=2)
            ctypes.windll.user32.keybd_event(0x11, 0, 2, 0)        # Ctrl 抬起
            print('Ctrl+滚轮  =', z0, '->', r.zoom, '|', r.var_zoom.get())
        finally:
            user32.SetCursorPos(saved.x, saved.y)
        r.close()
        print('SELFTEST-WHEEL OK')
        app.root.destroy()
        return

    if '--selftest-resolve' in sys.argv:
        # 「搜到其中一话 → 自动给出所有话」的解析规则（用假 client，离线可测）
        app.root.withdraw()
        from jmcomic.jm_entity import JmAlbumDetail, JmPhotoDetail

        album = JmAlbumDetail('350234', '1', '多章本子',
                              [('350240', '1', '第1話'), ('350241', '2', '第2話')],
                              50, '', '', '', '', 0, [], [], ['作者'], ['标签'])
        ch1 = JmPhotoDetail('350240', '第1話', 350234, 1, from_album=album)
        ch2 = JmPhotoDetail('350241', '第2話', 350234, 2, from_album=album)
        solo = JmPhotoDetail('999', '单章本子', 0, 2)

        class FakeClient:
            def __init__(self):
                self.calls = []

            def get_album_detail(self, i):
                self.calls.append(('album', str(i)))
                if str(i) == '350234':
                    return album
                raise Exception('请求的本子不存在！')

            def get_photo_detail(self, i):
                self.calls.append(('photo', str(i)))
                for p in (ch1, ch2, solo):
                    if p.photo_id == str(i):
                        return p
                raise Exception('请求的章节不存在！')

        c = FakeClient()
        cases = [
            ('album', '350234', 'album', '350234', None),     # 正常本子号
            ('album', '350240', 'album', '350234', '第1話'),   # 拿了章节号当本子查 → 升到整本
            ('photo', '350241', 'album', '350234', '第2話'),   # 显式 p 前缀 → 也升到整本
            ('photo', '999', 'photo', '999', None),           # 单章节本子 → 保持单章
        ]
        for kind, i, wk, wid, whit in cases:
            k, d, hit = app._resolve_item(c, kind, i)
            print('%-6s %-8s -> %-6s %-8s 命中=%s' % (kind, i, k, d.id, hit))
            assert (k, d.id, hit) == (wk, wid, whit), (k, d.id, hit)

        k, d, hit = app._resolve_item(c, 'photo', '350234')    # p + 本子号 → 反向兜底
        print('photo  350234   -> %s %s' % (k, d.id))
        assert (k, d.id) == ('album', '350234')

        try:
            app._resolve_item(c, 'album', '1234567')
            raise AssertionError('不存在的 id 应当抛错')
        except AssertionError:
            raise
        except Exception as e:
            print('album  1234567  -> 正确抛错:', str(e)[:24])

        # 整本的章节信息确实是「所有话」
        info = detail_info(album)
        print('整本信息     =', info['chapters'], '话 /', info['pages'], '页')
        assert info['chapters'] == 2 and info['pages'] == 50
        print('SELFTEST-RESOLVE OK')
        app.root.destroy()
        return

    if '--selftest-wheel-main' in sys.argv:
        # 主窗口左侧列表的滚轮（真实系统事件：Windows 只把滚轮发给焦点窗口，
        # 所以必须按光标位置分发才滚得动）
        app.root.deiconify()
        app.root.update()
        from PIL import Image as PILImage

        base = os.path.join(tempfile.gettempdir(), 'jmcomic_wheel_lib')
        for n in range(8):
            d = os.path.join(base, '[JM%d]测试本子%d' % (1000 + n, n))
            os.makedirs(d, exist_ok=True)
            for p in range(1, 4):
                PILImage.new('RGB', (400, 560), (40 + n * 12, 60, 96)).save(
                    os.path.join(d, '%05d.jpg' % p))

        app.base_dir = base
        app._base_dirty = True
        app._switch_view('local')
        deadline = time.time() + 30
        while time.time() < deadline and len(app.local) < 8:
            app.root.update()
            time.sleep(0.05)
        for _ in range(20):
            app.root.update()
            time.sleep(0.03)
        print('列表项数   =', len(app.local))
        assert len(app.local) >= 8, len(app.local)

        import ctypes
        user32 = ctypes.windll.user32
        hwnd = user32.GetParent(app.root.winfo_id()) or app.root.winfo_id()
        try:
            fg = user32.GetForegroundWindow()
            t_fg = user32.GetWindowThreadProcessId(fg, None)
            t_me = ctypes.windll.kernel32.GetCurrentThreadId()
            user32.AttachThreadInput(t_fg, t_me, True)
            user32.ShowWindow(hwnd, 9)
            user32.SetForegroundWindow(hwnd)
            user32.SetFocus(hwnd)
            user32.AttachThreadInput(t_fg, t_me, False)
        except Exception:
            pass
        time.sleep(0.5)
        app.root.update()
        print('前台焦点   =', user32.GetForegroundWindow() == hwnd)

        class POINT(ctypes.Structure):
            _fields_ = [('x', ctypes.c_long), ('y', ctypes.c_long)]

        saved = POINT()
        user32.GetCursorPos(ctypes.byref(saved))
        cx = app.res_canvas.winfo_rootx() + max(1, app.res_canvas.winfo_width()) // 2
        cy = app.res_canvas.winfo_rooty() + max(1, app.res_canvas.winfo_height()) // 2
        y0 = app.res_canvas.yview()[0]
        try:
            user32.SetCursorPos(cx, cy)
            time.sleep(0.3)
            for _ in range(3):
                user32.mouse_event(0x0800, 0, 0, -120, 0)      # 向下
                time.sleep(0.2)
            for _ in range(10):
                app.root.update()
                time.sleep(0.05)
            y1 = app.res_canvas.yview()[0]
            print('列表滚轮   =', round(y0, 5), '->', round(y1, 5))
            assert y1 > y0, f'列表滚轮没生效（{y0} -> {y1}）'

            for _ in range(2):
                user32.mouse_event(0x0800, 0, 0, 120, 0)       # 向上
                time.sleep(0.2)
            for _ in range(10):
                app.root.update()
                time.sleep(0.05)
            print('向上滚     =', round(app.res_canvas.yview()[0], 5))
            assert app.res_canvas.yview()[0] < y1
        finally:
            user32.SetCursorPos(saved.x, saved.y)
        print('SELFTEST-WHEEL-MAIN OK')
        app.root.destroy()
        return

    if '--selftest-filename' in sys.argv:
        # 下载文件名前缀：JM车号_话序_原文件名
        app.root.withdraw()
        from jmcomic.jm_entity import JmAlbumDetail, JmPhotoDetail, JmImageDetail

        album = JmAlbumDetail('350234', '1', '多章本子',
                              [('350240', '1', '第1話'), ('350241', '2', '第2話')],
                              50, '', '', '', '', 0, [], [], ['作者'], ['标签'])
        p1 = JmPhotoDetail('350240', '第1話', 350234, 1, from_album=album)
        p2 = JmPhotoDetail('350241', '第2話', 350234, 2, from_album=album)

        def img(photo, page):
            name = '%05d' % page
            return JmImageDetail(str(photo.album_id), '220980',
                                 'http://x/%s.webp' % name, name, '.webp',
                                 from_photo=photo, index=page)

        i1, i2 = img(p1, 1), img(p2, 1)
        print('第1話第1页 ->', _prefixed_image_filename(i1))
        print('第2話第1页 ->', _prefixed_image_filename(i2))
        assert _prefixed_image_filename(i1) == 'JM350234_00001'
        assert _prefixed_image_filename(i2) == 'JM350234_00001'   # 话序在文件夹名上
        assert not re.search(r'[\u4e00-\u9fff]', _prefixed_image_filename(i1)), '文件名不能有中文'

        names = sorted(_prefixed_image_filename(img(p1, p)) for p in range(1, 13))
        print('排序抽样   =', names[:2], '...', names[-1])
        assert names[0] == 'JM350234_00001' and names[-1] == 'JM350234_00012'

        opt, _base = build_option()
        got = opt.decide_image_filename(i1)
        print('option 上生效 ->', got)
        assert got == 'JM350234_00001'

        class Bare:
            filename_without_suffix = '00001'

        print('拿不到字段兜底 ->', _prefixed_image_filename(Bare()))
        assert _prefixed_image_filename(Bare()) == '00001'
        print('SELFTEST-FILENAME OK')
        app.root.destroy()
        return

    if '--selftest-search' in sys.argv:
        # 搜索：结果解析 + 接口分派 + 界面控件
        app.root.withdraw()
        from jmcomic.jm_entity import JmSearchPage

        sp = JmSearchPage([
            ('350234', {'name': '本子A', 'author': '作者A', 'category': {'title': '同人'}}),
            ('350235', {'name': '本子B', 'author': ['作者B1', '作者B2'], 'tags': ['标签1']}),
        ], 12)
        infos = search_infos(sp)
        print('解析条数   =', len(infos))
        for k, i in infos:
            print('   %-14s %-10s 作者=%-12s 标签=%s' % (k, i['title'], i['authors'], i['tags']))
        assert len(infos) == 2
        assert infos[0][0] == 'album:350234' and infos[0][1]['title'] == '本子A'
        assert infos[0][1]['authors'] == '作者A'
        assert infos[1][1]['authors'] == '作者B1、作者B2'
        assert '同人' in infos[0][1]['tags'] and '标签1' in infos[1][1]['tags']
        assert infos[0][1]['id'] == '350234'

        class FakeClient:
            def __init__(self):
                self.calls = []

            def search_site(self, q, page=1):
                self.calls.append(('site', q, page))
                return sp

            def search_work(self, q, page=1):
                self.calls.append(('work', q, page))
                return sp

            def search_author(self, q, page=1):
                self.calls.append(('author', q, page))
                return sp

        c = FakeClient()
        for kind in ('site', 'work', 'author'):
            assert search_page(c, '测试词', kind) is sp
        print('接口分派   =', c.calls)
        assert [x[0] for x in c.calls] == ['site', 'work', 'author']

        print('界面控件   = 输入框 %s | 类型选项 %s | 按钮 %s' % (
            bool(app.search_entry), app.search_kind.get(), bool(app.btn_search)))
        assert app.search_entry and app.btn_search
        assert list(SEARCH_KINDS.keys()) == ['全部', '本子名称', '作者']
        print('SELFTEST-SEARCH OK')
        app.root.destroy()
        return

    if '--selftest-search-net' in sys.argv:
        # 真实搜索链路：用无意义关键词，只验证能拿到结果页（不显示任何内容）
        app.root.withdraw()
        app.root.update()
        opt, _base = build_option()
        client = opt.new_jm_client()
        for kind in ('site', 'work', 'author'):
            page = search_page(client, 'zzzznotfound12345', kind)
            total = getattr(page, 'total', None)
            cnt = len(getattr(page, 'content', []) or [])
            print('%-6s -> total=%s 本页=%d' % (kind, total, cnt))
            assert str(total).isdigit()
            assert cnt == 0
        print('SELFTEST-SEARCH-NET OK')
        app.root.destroy()
        return

    if '--selftest-anim' in sys.argv:
        # 动画：缓动、颜色插值、帧驱动、总开关、阅读器交叉淡入
        app.root.withdraw()
        app.root.update()
        print('ease_out    =', [round(ease_out(x), 3) for x in (0, 0.25, 0.5, 0.75, 1)])
        assert ease_out(0) == 0 and ease_out(1) == 1 and ease_out(0.5) > 0.5
        print('lerp_color  =', lerp_color('#000000', '#ffffff', 0),
              lerp_color('#000000', '#ffffff', 0.5), lerp_color('#000000', '#ffffff', 1))
        assert lerp_color('#000000', '#ffffff', 0) == '#000000'
        assert lerp_color('#000000', '#ffffff', 1) == '#ffffff'

        vals = []
        app._anim(5, lambda t: vals.append(round(t, 3)), interval=1)
        deadline = time.time() + 5
        while time.time() < deadline and (not vals or vals[-1] < 1.0):
            app.root.update()
            time.sleep(0.01)
        print('帧序列      =', vals)
        assert vals and vals[-1] == 1.0
        assert all(vals[i] <= vals[i + 1] for i in range(len(vals) - 1)), vals

        # 关掉动画 → 只落位一帧
        globals()['ANIM_ON'] = False
        got = []
        app._anim(5, lambda t: got.append(t), interval=1)
        app.root.update()
        print('关闭动画    =', got)
        assert got == [1.0], got
        globals()['ANIM_ON'] = True

        # 阅读器翻页交叉淡入
        from PIL import Image as PILImage
        from PIL import ImageDraw

        base = os.path.join(tempfile.gettempdir(), 'jmcomic_anim_test')
        album = os.path.join(base, '[JM1]动画测试')
        os.makedirs(album, exist_ok=True)
        for p in range(1, 4):
            im = PILImage.new('RGB', (400, 560), (35 * p, 60, 90))
            ImageDraw.Draw(im).text((30, 30), 'PAGE %d' % p, fill=(255, 255, 255))
            im.save(os.path.join(album, '%05d.jpg' % p))

        r = ReaderWindow(app, '动画测试', album)
        app._readers.append(r)
        app.root.update()
        r.flip(1)
        for _ in range(9):
            app.root.update()
            time.sleep(0.02)
        print('阅读器翻页  =', r.var_page.get(), '| 有图 =', r._photo is not None,
              '| 上一帧缓存 =', r._prev_img is not None)
        assert r._photo is not None and r._prev_img is not None
        r.close()
        print('SELFTEST-ANIM OK')
        app.root.destroy()
        return

    if '--selftest-export' in sys.argv:
        # 导出 PDF / ZIP
        app.root.withdraw()
        from PIL import Image as PILImage

        base = os.path.join(tempfile.gettempdir(), 'jmcomic_export_test')
        album = os.path.join(base, '[JM1]导出测试')
        d = os.path.join(album, '001-第1話')
        os.makedirs(d, exist_ok=True)
        for p in range(1, 4):
            PILImage.new('RGB', (300, 420), (30 * p, 70, 110)).save(
                os.path.join(d, '%05d.jpg' % p))
        empty = os.path.join(base, '[JM2]空目录')
        os.makedirs(empty, exist_ok=True)

        ok0, n0, msg0 = export_album(empty, 'pdf')
        print('空目录    =', ok0, n0, msg0)
        assert not ok0

        for fmt, ext in (('pdf', '.pdf'), ('zip', '.zip')):
            ok, n, path = export_album(album, fmt, out_dir=base, name='导出测试')
            print('%-3s -> ok=%s 页数=%d 文件=%s' % (fmt.upper(), ok, n, os.path.basename(path)))
            assert ok and n == 3, (ok, n, path)
            assert path.endswith(ext) and os.path.getsize(path) > 500
            if fmt == 'pdf':
                data = open(path, 'rb').read()
                print('    PDF 页对象 =', data.count(b'/Type /Page') - data.count(b'/Type /Pages'))
            else:
                import zipfile
                with zipfile.ZipFile(path) as zf:
                    names = zf.namelist()
                print('    ZIP 条目 =', names)
                assert len(names) == 3

        print('界面按钮  = PDF %s | ZIP %s' % (bool(app.btn_pdf), bool(app.btn_zip)))
        assert app.btn_pdf is not None and app.btn_zip is not None
        print('SELFTEST-EXPORT OK')
        app.root.destroy()
        return

    if '--selftest-cancel' in sys.argv:
        # 终止下载：钩子置位后必须抛 Cancelled；按钮状态联动
        app.root.withdraw()
        from jmcomic.jm_config import JmModuleConfig

        app._install_log_hook()
        hook = JmModuleConfig.EXECUTOR_LOG
        print('初始信号     =', app._cancel.is_set())
        assert not app._cancel.is_set()

        hook('image.after', 'ok')
        print('正常事件     = 未抛异常 ✓')

        app._cancel.set()
        try:
            hook('image.after', 'ok')
            raise AssertionError('置位后钩子应当抛 Cancelled')
        except Cancelled:
            print('置位后钩子   = 抛 Cancelled ✓')

        print('按钮初始状态 =', str(app.btn_cancel['state']))
        assert str(app.btn_cancel['state']) == 'disabled'

        app.busy = True
        app.start_cancel()
        print('start_cancel -> %s | 按钮 %s' % (app.var_status.get(), str(app.btn_cancel['state'])))
        assert str(app.btn_cancel['state']) == 'disabled'

        # 模拟一次"下载"：每张图都会触发钩子，第 5 张时点终止 → 必须立刻中断
        app._cancel.clear()
        step = {'n': 0}

        def fake_download():
            for _ in range(100):
                step['n'] += 1
                hook('image.after', 'ok')          # 真实下载里每张图都会走这里
                if step['n'] == 5:
                    app._cancel.set()              # 模拟用户此刻点了「终止下载」

        try:
            fake_download()
            raise AssertionError('置位后应当中断')
        except Cancelled:
            print('模拟下载     = 第 %d 步被中断（共 100 步）' % step['n'])
            assert step['n'] < 20, step

        app.busy = False
        app._cancel.clear()
        print('SELFTEST-CANCEL OK')
        app.root.destroy()
        return

    if '--selftest-delete' in sys.argv:
        # 删除已下载 → 回收站
        app.root.withdraw()
        from PIL import Image as PILImage

        base = os.path.join(tempfile.gettempdir(), 'jmcomic_del_test')
        d = os.path.join(base, '[JM1]删除测试', '001-第1話')
        os.makedirs(d, exist_ok=True)
        for p in range(1, 3):
            PILImage.new('RGB', (120, 160), (40, 60, 90)).save(
                os.path.join(d, '%05d.jpg' % p))
        target = os.path.dirname(d)
        print('删除前存在   =', os.path.isdir(target))
        assert os.path.isdir(target)

        ok, msg = trash_path(target)
        print('trash_path   ->', ok, msg)
        assert ok and not os.path.exists(target), (ok, msg)

        ok2, msg2 = trash_path(target)
        print('重复删除     =', ok2, msg2)
        assert not ok2

        print('按钮就位     =', bool(app.btn_del), '| 危险配色 =', str(app.btn_del['bg']))
        assert app.btn_del is not None
        print('SELFTEST-DELETE OK')
        app.root.destroy()
        return

    if '--selftest-quickdel' in sys.argv:
        # 已下载列表里的快捷删除（行内「删」按钮 + Delete 键）
        app.root.withdraw()
        from PIL import Image as PILImage

        base = os.path.join(tempfile.gettempdir(), 'jmcomic_quickdel')
        for n in (1, 2, 3):
            d = os.path.join(base, '[JM%d]快捷删除%d' % (n, n), '001-第1話')
            os.makedirs(d, exist_ok=True)
            for p in range(1, 3):
                PILImage.new('RGB', (120, 160), (30 * n, 60, 90)).save(
                    os.path.join(d, '%05d.jpg' % p))

        app.base_dir = base
        app._base_dirty = True
        app._switch_view('local')
        deadline = time.time() + 30
        while time.time() < deadline and len(app.local) < 3:
            app.root.update()
            time.sleep(0.05)
        for _ in range(20):
            app.root.update()
            time.sleep(0.03)
        print('列表项数   =', len(app.local))
        assert len(app.local) >= 3, len(app.local)

        key0 = app.local[0]['key']
        row = app.result_rows.get(key0)
        assert row is not None, '没有这一行'
        texts = []

        def walk(w):
            for c in w.winfo_children():
                try:
                    if c.winfo_class() == 'Button':
                        texts.append(str(c['text']))
                except Exception:
                    pass
                walk(c)

        walk(row[0])
        print('行内按钮   =', texts)
        assert any('删' in t for t in texts), texts

        # 确认 → 删除成功
        app._confirm_delete = lambda root, files: True
        target = app.local[0].get('dir')
        before = len(app.local)
        ok = app.delete_key(key0)
        app.root.update()
        print('删除结果   =', ok, '| 目录还在?', os.path.exists(target or ''),
              '| 列表', before, '->', len(app.local))
        assert ok and not os.path.exists(target)
        assert len(app.local) == before - 1

        # 取消确认 → 什么都没动
        app._confirm_delete = lambda root, files: False
        key1 = app.local[0]['key']
        d1 = app.local[0].get('dir')
        ok2 = app.delete_key(key1)
        print('取消确认   =', ok2, '| 目录还在?', os.path.exists(d1 or ''))
        assert not ok2 and os.path.exists(d1)

        # Delete 键：焦点在输入框时不应该触发删除
        app.selected_key = key1
        app.entry.focus_set()
        app.root.update()
        app._on_delete_key()
        print('输入框里按 Delete -> 目录还在?', os.path.exists(d1 or ''))
        assert os.path.exists(d1)

        print('SELFTEST-QUICKDEL OK')
        app.root.destroy()
        return

    if '--selftest-theme' in sys.argv:
        # 主题：调色板完整、跟随系统解析、持久化、切换重建
        app.root.withdraw()
        app.root.update()
        need = {'BG', 'CARD', 'CARD2', 'BORDER', 'FG', 'MUTED', 'ACCENT', 'ACCENT_HI',
                'GREEN', 'RED', 'YELLOW', 'FIELD', 'ON_ACCENT', 'HOVER',
                'SCROLL', 'TROUGH', 'VIEWER_BG', 'DANGER_BG', 'DANGER_HI',
                'DANGER_FG', 'DANGER_FG_HI'}
        for name, pal in THEMES.items():
            miss = need - set(pal)
            print('  %-4s 颜色 %2d 个 | 缺 %s' % (name, len(pal), miss or '无'))
            assert not miss, (name, miss)
            for k, v in pal.items():
                assert re.fullmatch(r'#[0-9a-fA-F]{6}', v), (name, k, v)

        print('主题列表     =', theme_names())
        assert THEME_FOLLOW in theme_names()
        print('系统深色?    =', system_dark())
        print('跟随系统解析 =', resolve_theme(THEME_FOLLOW))
        assert resolve_theme(THEME_FOLLOW) in ('深色', '浅色')
        assert resolve_theme('霓虹') == '霓虹'
        assert resolve_theme('不存在的主题') == '深色'

        # 持久化往返
        tmp = os.path.join(tempfile.gettempdir(), 'jmcomic_ui_test.json')
        old_path = _ui_settings_path
        globals()['_ui_settings_path'] = lambda: tmp
        try:
            save_ui_settings({'theme': '樱花', 'x': 1})
            got = load_ui_settings()
            print('持久化往返   =', got)
            assert got.get('theme') == '樱花' and got.get('x') == 1
        finally:
            globals()['_ui_settings_path'] = old_path
            try:
                os.remove(tmp)
            except Exception:
                pass

        # 真正切换每个主题（会重建界面）
        for name in ('浅色', '樱花', '霓虹', '深色', '跟随系统'):
            app.set_theme(name)
            for _ in range(30):
                app.root.update()
                time.sleep(0.02)
            print('  %-4s -> 生效 %-4s | 窗口底色 %s | 卡片 %s'
                  % (name, app.theme_active, app.root['bg'], app.CARD))
            assert app.root['bg'].lower() == THEMES[app.theme_active]['BG'].lower()
            assert app.theme_name == name
            # 界面重建后关键控件都在
            for attr in ('entry', 'search_entry', 'text', 'res_canvas', 'btn_dl',
                         'btn_cancel', 'btn_del', 'btn_pdf', 'theme_combo',
                         'btn_view_local', 'bar'):
                assert getattr(app, attr, None) is not None, (name, attr)

        # 日志抽屉：默认收起，能展开能收回
        app.toggle_log()
        app.root.update()
        opened = app.logwrap.winfo_manager() != ''
        app.toggle_log()
        app.root.update()
        closed = app.logwrap.winfo_manager() == ''
        print('日志抽屉     = 展开 %s / 收起 %s' % (opened, closed))
        assert opened and closed

        # toast 能出现也能收掉
        app.toast('测试提示', 'ok')
        for _ in range(20):
            app.root.update()
            time.sleep(0.02)
        print('toast        =', app.toast_frame is not None)
        assert app.toast_frame is not None
        app._toast_hide()
        for _ in range(20):
            app.root.update()
            time.sleep(0.02)
        print('toast 收掉   =', app.toast_frame is None)
        assert app.toast_frame is None

        app.set_theme('深色')
        for _ in range(20):
            app.root.update()
            time.sleep(0.02)
        print('SELFTEST-THEME OK')
        app.root.destroy()
        return

    if '--selftest-net' in sys.argv:
        idx = sys.argv.index('--selftest-net')
        album = sys.argv[idx + 1] if len(sys.argv) > idx + 1 else '999999999'
        app.root.withdraw()
        app._entry_box.set(album)
        app.start_download()
        deadline = time.time() + 90
        while app.busy and time.time() < deadline:
            app.root.update()
            time.sleep(0.05)
        tail = time.time() + 1.0
        while time.time() < tail:
            app.root.update()
            time.sleep(0.05)
        print('=== 界面日志 ===')
        print(app.text.get('1.0', 'end').strip())
        print('=== 状态 ===')
        print(app.var_status.get())
        app.root.destroy()
        return

    app.run()


if __name__ == '__main__':
    main()
