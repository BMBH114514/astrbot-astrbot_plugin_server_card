# -*- coding: utf-8 -*-
"""从立绘里抽出配色骨架，喂给生成模型

规则：
  - 只统计实心像素（alpha > 200），透明区域和半透明发光边不参与，
    不然一张透明底立绘会被抽出一堆画布白边
  - 色值不动它，不自动提饱和度，抽出来什么就是什么
  - 量化后把相近色合并，避免五个色全是一个色系的深浅
"""
from __future__ import annotations

import base64
import io
import os
import re

from PIL import Image

try:
    import numpy as np
except Exception:  # 没装 numpy 也能跑，只是慢一点
    np = None

EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp")
# 这些词几乎每张图都沾，命中它们等于没命中
STOP = {"立绘", "卡片", "图片", "素材", "角色", "背景", "透明", "皮肤", "竖版", "横版",
        "风格", "设计", "展示", "好看", "生成", "状态", "网页", "界面", "排版", "配色",
        "一张", "做个", "做一", "一个", "帮我", "来张", "来一", "那种", "感觉", "主题"}
SEG_BONUS = 50             # 文件名里有一整段等于这个词，权重直接拉满
ALPHA_SOLID = 200          # 高于这个才算实心
MERGE_DIST = 18.0          # 色差小于这个就并成一档
QUANT = 24                 # 先粗量化成这么多档再合，档多了才能抓到点缀色
DEFAULT_COLORS = 8


def _freedraw():
    from . import freedraw
    return freedraw


def _root():
    try:
        return _freedraw()._asset_root()
    except Exception:
        return ""


def abspath(rel):
    """清单里的相对路径 -> 磁盘绝对路径，找不到返回空串"""
    root = _root()
    if not root or not rel:
        return ""
    p = os.path.join(root, str(rel).replace("/", os.sep).replace("\\", os.sep))
    return p if os.path.isfile(p) else ""


def _asset_list():
    try:
        return _freedraw()._asset_list()
    except Exception:
        return []


def _substrings(seg, low=1, high=6):
    out = set()
    n = len(seg)
    for size in range(low, min(high, n) + 1):
        for i in range(n - size + 1):
            out.add(seg[i:i + size])
    return out


def _words(text):
    """把一句话拆成可能的素材名候选（中文按 2-6 字滑窗，英文按词）"""
    segs = re.findall(r"[\u4e00-\u9fa5]+|[A-Za-z][A-Za-z0-9_\-]{1,}", text or "")
    out = set()
    for s in segs:
        if re.match(r"[A-Za-z]", s):
            out.add(s.lower())
        else:
            out |= _substrings(s)
    return out


def _segments(rel):
    """路径按分隔符切段，目录名也算：人物名/图片名 -> ['人物名', '图片名']

    只切文件名的话，素材按人名建文件夹（人物名\某张图.jpg）就永远搜不到
    """
    stem = os.path.splitext(str(rel).replace("\\", "/"))[0]
    return [x for x in re.split(r"[_\/\-\s（）()【】\[\]．.、]+", stem) if x]


def hits(desc, limit=8):
    """按名字去素材目录里找：用户嘴里提到的名字 -> 命中的文件

    单字名（年、令、黍、夕）只在文件名有一整段等于它时才算，
    不然"2024年上海锦标赛"这种也会被拖进来。
    返回 [(相对路径, 命中权重)]，权重高的排前面
    """
    words = _words(desc)
    if not words:
        return []
    found = {}
    for rel in _asset_list():
        low = str(rel).lower()
        stem = os.path.splitext(os.path.basename(low))[0]
        segs = {x.lower() for x in _segments(low)}
        best = 0
        for w in words:
            if w in STOP:
                continue
            if len(w) == 1:
                if w in segs:
                    best = max(best, SEG_BONUS + 1)
            elif len(w) >= 2:
                # 文件名里有，或者文件夹名字里有，都算；按名字建文件夹的素材全靠这行
                if w in stem:
                    best = max(best, len(w) + 2)
                elif w in low:
                    best = max(best, len(w))
        if best:
            found[rel] = max(best, found.get(rel, 0))
    rows = sorted(found.items(), key=lambda kv: (-kv[1], kv[0]))
    return rows[:limit]


def _hex(rgb):
    return "#%02X%02X%02X" % (int(rgb[0]) & 255, int(rgb[1]) & 255, int(rgb[2]) & 255)


def _dist(a, b):
    return sum((float(x) - float(y)) ** 2 for x, y in zip(a, b)) ** 0.5


def extract(rel, k=DEFAULT_COLORS):
    """抽一张图的配色骨架，抽不到返回 None"""
    path = abspath(rel) or (rel if os.path.isfile(str(rel)) else "")
    if not path:
        return None
    try:
        im = Image.open(path)
        w0, h0 = im.size
        im = im.convert("RGBA")
        im.thumbnail((300, 300))
    except Exception:
        return None

    if np is not None:
        arr = np.asarray(im, dtype=np.uint8)
        solid = arr[:, :, 3] > ALPHA_SOLID
        px = arr[solid][:, :3]
        if len(px) < 64:                     # 整张全透明就当没有
            return None
        if len(px) > 120000:
            px = px[:: max(1, len(px) // 120000)]
        flat = Image.fromarray(np.ascontiguousarray(px.reshape(1, -1, 3)), "RGB")
    else:
        px = [p[:3] for p in im.getdata() if p[3] > ALPHA_SOLID]
        if len(px) < 64:
            return None
        flat = Image.new("RGB", (len(px), 1))
        flat.putdata(px)
        px = list(px)

    # 先粗量化到 9 档，再合并相近色
    try:
        q = flat.quantize(colors=QUANT, method=Image.MEDIANCUT)
    except Exception:
        q = flat.convert("RGB").quantize(colors=QUANT)
    pal = q.getpalette() or []
    counts = q.getcolors() or []
    if not counts:
        return None
    total = float(sum(c for c, _ in counts)) or 1.0

    buckets = []
    for cnt, idx in sorted(counts, reverse=True):
        rgb = tuple(pal[idx * 3: idx * 3 + 3]) if len(pal) > idx * 3 + 2 else (128, 128, 128)
        if len(rgb) < 3:
            rgb = (128, 128, 128)
        for b in buckets:
            if _dist(b["rgb"], rgb) <= MERGE_DIST:
                b["w"] += cnt
                b["rgb"] = tuple(
                    (b["rgb"][i] * (b["w"] - cnt) + rgb[i] * cnt) / b["w"] for i in range(3))
                break
        else:
            buckets.append({"rgb": tuple(float(v) for v in rgb), "w": float(cnt)})

    buckets.sort(key=lambda b: -b["w"])
    buckets = buckets[:k]
    colors = [{"hex": _hex(b["rgb"]), "share": round(b["w"] * 100.0 / total, 1)}
              for b in buckets]

    # 整体明暗、饱和度，决定走浅底还是深底
    try:
        if np is not None:
            mean = px.astype("float32").mean(axis=0)
            mx = px.astype("float32").max(axis=1)
            mn = px.astype("float32").min(axis=1)
            sat = float(((mx - mn) / (mx + 1e-6)).mean())
        else:
            mean = [sum(p[i] for p in px) / float(len(px)) for i in range(3)]
            sats = [(max(p) - min(p)) / (max(p) + 1e-6) for p in px]
            sat = sum(sats) / float(len(sats))
        light = (0.299 * mean[0] + 0.587 * mean[1] + 0.114 * mean[2]) / 255.0
    except Exception:
        light, sat = 0.5, 0.3

    return {
        "file": str(rel).replace("\\", "/"),
        "w": w0, "h": h0,
        "colors": colors,
        "light": round(float(light), 3),
        "sat": round(float(sat), 3),
    }


def brief(rows):
    """给模型看的候选清单：每张图一行色板"""
    lines = []
    for r in rows:
        cols = "  ".join("%s %s%%" % (c["hex"], c["share"]) for c in r.get("colors") or [])
        lines.append("  %s    %dx%d    明度 %.2f 饱和 %.2f\n        %s"
                     % (r["file"], r.get("w", 0), r.get("h", 0),
                        r.get("light", 0), r.get("sat", 0), cols))
    return "\n".join(lines)


def scan(desc, limit=8, k=DEFAULT_COLORS):
    """描述里点名的素材 -> 抽色，返回抽到色的行"""
    out = []
    for rel, _n in hits(desc, limit=limit):
        r = extract(rel, k=k)
        if r:
            out.append(r)
    return out


def to_data_uri(rel, max_side=1200, budget_kb=350):
    """立绘转成 data URI 塞进网页：长边压到 max_side，超预算再量化到 256 色

    透明底原样保留，只动尺寸和色深，不动颜色
    """
    path = abspath(rel) or (rel if os.path.isfile(str(rel)) else "")
    if not path:
        return ""
    try:
        im = Image.open(path).convert("RGBA")
        w, h = im.size
        scale = min(1.0, float(max_side) / float(max(w, h) or 1))
        if scale < 1.0:
            im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "PNG", optimize=True)
        if buf.tell() > budget_kb * 1024:
            try:
                q = im.quantize(colors=256, method=Image.FASTOCTREE)
                buf = io.BytesIO()
                q.save(buf, "PNG", optimize=True)
            except Exception:
                pass
        data = base64.b64encode(buf.getvalue()).decode("ascii")
        return "data:image/png;base64," + data
    except Exception:
        return ""

def inventory(limit=80):
    """素材清单（只有路径和尺寸，不抽色），给挑图那一步的 AI 看"""
    out = []
    root = _root()
    for rel in _asset_list()[:limit]:
        w = h = 0
        try:
            with Image.open(os.path.join(root, rel.replace("/", os.sep))) as im:
                w, h = im.size
        except Exception:
            pass
        out.append({"file": rel, "w": w, "h": h})
    return out
