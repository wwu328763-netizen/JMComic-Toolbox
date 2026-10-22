# -*- coding: utf-8 -*-
"""JMComic 工具箱图标 —— 橙色大眼睛吉祥物（个人自用版）

说明
  · 形象来源：站点 favicon 的"橙色圆团 + 一只大眼睛"，重绘为高分辨率
  · 仅个人使用，不建议随包分发（含对官方形象的重绘）
  · 橙色渐变底（squircle）+ 圆团身体（白描边 + 黑描边）
  · 左侧小耳朵凸起；一只漫画大眼（白眼球 + 黑瞳 + 高光）；腮红
  · ≤32px 自动简化：放大眼睛、加粗描边、去掉腮红

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
ORANGE = (247, 157, 40, 255)
ORANGE_D = (228, 88, 39, 255)
INK = (10, 6, 6, 255)
WHITE = (255, 255, 255, 255)
SIZES = [16, 20, 24, 32, 40, 48, 64, 96, 128, 256]


def squircle(size, n=N):
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)
    c = (size - 1) / 2.0
    x = np.abs((xx - c) / c)
    y = np.abs((yy - c) / c)
    return Image.fromarray((((x ** n + y ** n) <= 1) * 255).astype(np.uint8), 'L')


def vgrad(size, top, bottom):
    t = np.linspace(0, 1, size, dtype=np.float32).reshape(size, 1, 1)
    a = np.array(top[:3], np.float32).reshape(1, 1, 3)
    b = np.array(bottom[:3], np.float32).reshape(1, 1, 3)
    return Image.fromarray(np.repeat(a + (b - a) * t, size, axis=1).astype(np.uint8), 'RGB')


def dilate(mask, w):
    return mask.filter(ImageFilter.MaxFilter(w * 2 + 1))


def render(size):
    small = size <= 32
    ss = 4 if size <= 128 else 2
    S = size * ss

    img = Image.new('RGBA', (S, S), (0, 0, 0, 0))
    img.paste(vgrad(S, ORANGE, ORANGE_D).convert('RGBA'), (0, 0), squircle(S))

    # ---- 身体：圆团 + 左侧小耳 ----
    body = Image.new('L', (S, S), 0)
    d = ImageDraw.Draw(body)
    cx, cy = S * .52, S * .53
    bw, bh = S * .285, S * .255
    d.ellipse([cx - bw, cy - bh, cx + bw, cy + bh], fill=255)
    d.ellipse([cx - bw * 1.30, cy + bh * .10, cx - bw * .70, cy + bh * .58], fill=255)

    # 白描边 → 黑描边 → 橙色身体
    halo_w = max(3, int(S * (.026 if not small else .038)))
    white = Image.new('RGBA', (S, S), (0, 0, 0, 0))
    white.paste(Image.new('RGBA', (S, S), WHITE), (0, 0), dilate(body, halo_w))
    img.alpha_composite(white)
    line_w = max(2, int(S * (.012 if not small else .018)))
    black = Image.new('RGBA', (S, S), (0, 0, 0, 0))
    black.paste(Image.new('RGBA', (S, S), INK), (0, 0), dilate(body, line_w))
    img.alpha_composite(black)
    bodyc = Image.new('RGBA', (S, S), (0, 0, 0, 0))
    bodyc.paste(vgrad(S, (252, 170, 52, 255), ORANGE_D).convert('RGBA'), (0, 0), body)
    img.alpha_composite(bodyc)

    d2 = ImageDraw.Draw(img)
    # ---- 大眼睛 ----
    ex = cx + bw * (.22 if not small else .12)
    ey = cy - bh * (.14 if not small else .06)
    er = S * (.150 if not small else .190)
    d2.ellipse([ex - er, ey - er * 1.08, ex + er, ey + er * 1.08], fill=WHITE,
               outline=INK, width=max(2, int(S * (.012 if not small else .022))))
    pr = er * (.54 if not small else .58)
    d2.ellipse([ex - pr * .55, ey - pr, ex + pr * 1.45, ey + pr], fill=INK)
    hr = er * (.24 if not small else .30)
    hx = ex + er * .12
    hy = ey - er * .60
    d2.ellipse([hx, hy, hx + hr * 2, hy + hr * 2], fill=WHITE)

    if not small:
        # 腮红
        blush = Image.new('RGBA', (S, S), (0, 0, 0, 0))
        bd = ImageDraw.Draw(blush)
        bd.ellipse([cx + bw * .62, cy + bh * .22, cx + bw * .92, cy + bh * .46],
                   fill=(255, 96, 96, 150))
        blush = blush.filter(ImageFilter.GaussianBlur(int(S * .012)))
        img.alpha_composite(blush)

    final = Image.new('RGBA', (S, S), (0, 0, 0, 0))
    final.paste(img, (0, 0), squircle(S))
    return final.resize((size, size), Image.LANCZOS)


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
