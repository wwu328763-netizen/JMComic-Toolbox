# -*- coding: utf-8 -*-
"""JMComic 工具箱图标 —— 原创构图（借用官方配色，不复刻官方吉祥物/字标）

设计
  · 橙色渐变底（官方配色 #F9A22E → #E45827）+ 圆角超椭圆
  · 一张微旋 -4° 的漫画分格：米白纸面 + 粗黑边框
  · 分格内一支黑色墨箭头（下载语义），箭头上方留白
  · 左上角网点（screentone）做漫画阴影，底部一条细速度线
  · ≤32px 简化：去网点、放大箭头与边框、加大留白

用法：python tools/make_icon.py   -> icon.ico + icon-preview.png
"""
import io
import os
import struct

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ICO = os.path.join(ROOT, 'icon.ico')
PREVIEW = os.path.join(ROOT, 'icon-preview.png')

N = 4.6
ORANGE = (249, 162, 46)
ORANGE_D = (228, 88, 39)
INK = (12, 8, 8)
CREAM = (250, 245, 234)
SIZES = [16, 20, 24, 32, 40, 48, 64, 96, 128, 256]


def squircle(size, n=N):
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)
    c = (size - 1) / 2.0
    x = np.abs((xx - c) / c)
    y = np.abs((yy - c) / c)
    return Image.fromarray((((x ** n + y ** n) <= 1) * 255).astype(np.uint8), 'L')


def vgrad(size, top, bottom):
    t = np.linspace(0, 1, size, dtype=np.float32).reshape(size, 1, 1)
    a = np.array(top, np.float32).reshape(1, 1, 3)
    b = np.array(bottom, np.float32).reshape(1, 1, 3)
    return Image.fromarray(np.repeat(a + (b - a) * t, size, axis=1).astype(np.uint8), 'RGB')


def halftone(size, step, cx, cy, radius, maxr=.28, alpha=170):
    layer = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    s = max(4, int(step))
    for y in range(0, size, s):
        for x in range(0, size, s):
            k = max(0.0, 1 - np.hypot((x - cx * size) / size, (y - cy * size) / size) / radius)
            r = int(s * maxr * k ** 1.25)
            if r >= 1:
                d.ellipse([x - r, y - r, x + r, y + r], fill=INK + (alpha,))
    return layer


def arrow_mask(size, total_h, stem_w, cx=.5, cy=.5):
    m = Image.new('L', (size, size), 0)
    d = ImageDraw.Draw(m)
    Cx, Cy = cx * size, cy * size
    stem_h = total_h * .52
    head_w = stem_w * 1.75
    top = Cy - total_h / 2
    rad = stem_w / 2
    d.rounded_rectangle([Cx - stem_w / 2, top, Cx + stem_w / 2, top + stem_h + rad],
                        radius=rad, fill=255)
    d.polygon([(Cx - head_w / 2, top + stem_h), (Cx + head_w / 2, top + stem_h),
               (Cx, top + total_h)], fill=255)
    return m


def render(size):
    small = size <= 32
    ss = 4 if size <= 128 else 2
    S = size * ss

    img = Image.new('RGBA', (S, S), (0, 0, 0, 0))
    img.paste(vgrad(S, ORANGE, ORANGE_D).convert('RGBA'), (0, 0), squircle(S))

    page = Image.new('RGBA', (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(page)
    inset = .085 if small else .115
    x0, y0 = int(S * inset), int(S * inset)
    x1, y1 = int(S * (1 - inset)), int(S * (1 - inset))
    lw = max(3, int(S * (.040 if small else .024)))
    radius = int(S * (.020 if not small else .030))
    d.rounded_rectangle([x0, y0, x1, y1], radius=radius, fill=CREAM + (255,))
    d.rectangle([x0, y0, x1, y1], outline=INK + (255,), width=lw)

    if not small:
        # 左上角网点
        page.alpha_composite(halftone(S, S // 34, cx=.20, cy=.17, radius=.30))
        # 底部速度线
        d.line([(x0 + int(S * .06), y1 - int(S * .085)),
                (x1 - int(S * .20), y1 - int(S * .100))],
               fill=INK + (200,), width=max(2, int(S * .012)))

    # 墨箭头
    ah = .50 if small else .46
    aw = .115 if small else .098
    m = arrow_mask(S, S * ah, S * aw, cx=.52, cy=.50)
    blk = Image.new('RGBA', (S, S), (0, 0, 0, 0))
    blk.paste(Image.new('RGBA', (S, S), INK + (255,)), (0, 0), m)
    page.alpha_composite(blk)

    if small:
        page = page.rotate(-3, resample=Image.BICUBIC, center=(S // 2, S // 2))
    else:
        page = page.rotate(-4, resample=Image.BICUBIC, center=(S // 2, S // 2))
    img.alpha_composite(page)

    out = Image.new('RGBA', (S, S), (0, 0, 0, 0))
    out.paste(img, (0, 0), squircle(S))
    return out.resize((size, size), Image.LANCZOS)


def write_ico(path, images):
    items = sorted(images, key=lambda x: x[0])
    blobs = []
    for s, im in items:
        buf = io.BytesIO()
        im.save(buf, format='PNG')
        blobs.append(buf.getvalue())
    n = len(items)
    header = struct.pack('<HHH', 0, 1, n)
    off = 6 + 16 * n
    entries = b''
    for (s, _), blob in zip(items, blobs):
        w = 0 if s >= 256 else s
        entries += struct.pack('<BBBBHHII', w, w, 0, 0, 1, 32, len(blob), off)
        off += len(blob)
    with open(path, 'wb') as f:
        f.write(header + entries + b''.join(blobs))


def main():
    imgs = [(s, render(s)) for s in SIZES]
    write_ico(ICO, imgs)
    render(512).save(PREVIEW, format='PNG')
    print('icon.ico ->', ICO, '%.1f KB' % (os.path.getsize(ICO) / 1024))
    print('sizes    ->', sorted(Image.open(ICO).info.get('sizes', [])))


if __name__ == '__main__':
    main()
