# -*- coding: utf-8 -*-
"""
自由绘图：让模型直接写 PIL 代码画整张卡，不受 styles/*.json 的字段限制。

安全取舍：
- import 放行白名单（PIL / math / time / random / colorsys / itertools），别的直接拒
- 禁 while True（死循环），禁 open / eval / exec / 双下划线属性
- builtins 换白名单，拿不到 os、sys、socket、subprocess

挡得住手滑和提示词乱来，别拿它跑陌生来源的代码。
"""

import ast
import builtins
import os
import re

import math
import random
import colorsys
import itertools
import datetime

from PIL import Image, ImageDraw, ImageFont

from astrbot.api import logger

from .theme import Theme

W, H = 1600, 1200

_ALLOWED_MODULES = {"PIL", "math", "time", "random", "colorsys", "itertools", "datetime"}

_BLOCKED_NAMES = {
    "__import__", "eval", "exec", "compile", "open", "input", "breakpoint",
    "globals", "locals", "vars", "memoryview", "getattr", "setattr", "delattr",
    "exit", "quit", "help",
}

def _build_builtins():
    """白名单反着写：正常内置函数全给，危险的那几个剔掉

    以前是正向白名单，模型写出 hasattr / callable / type 这种常用内置就 NameError，
    跑不通再重试一次白花钱。现在是除黑名单外全放，漏的概率低得多。
    """
    keep = {}
    for name in dir(builtins):
        if name.startswith("_") or name in _BLOCKED_NAMES:
            continue
        val = getattr(builtins, name, None)
        if callable(val) or isinstance(val, (str, int, bool, tuple, type(None))):
            keep[name] = val
    # 异常类统一补齐（dir(builtins) 里本来就有，这里是兜底）
    for name in ("Exception", "ValueError", "TypeError", "RuntimeError",
                 "KeyError", "IndexError", "AttributeError", "ZeroDivisionError",
                 "StopIteration", "NotImplementedError", "OverflowError",
                 "ArithmeticError", "LookupError", "AssertionError", "OSError"):
        obj = getattr(builtins, name, None)
        if obj is not None:
            keep[name] = obj
    return keep


_SAFE_BUILTINS = _build_builtins()


def _safe_import(name, globals=None, locals=None, fromlist=(), level=0):
    """只放行白名单里的模块，模型写 from PIL import Image 这种能过"""
    if name.split(".")[0] not in _ALLOWED_MODULES:
        raise ImportError("不许 import %s" % name)
    return builtins.__import__(name, globals, locals, fromlist, level)


_SAFE_BUILTINS["__import__"] = _safe_import


_ASSET_DIR = ""


_NO_ASSET = False


def set_no_asset(flag):
    """这张卡说了不使用素材，模板代码里自己写 asset(...) 也一律给空"""
    global _NO_ASSET
    _NO_ASSET = bool(flag)


def set_asset_dir(path):
    """配置里指定素材目录，留空就自动找常见位置"""
    global _ASSET_DIR
    p = (path or "").strip().strip('"')
    if p and os.path.isdir(p):
        _ASSET_DIR = p
    else:
        _ASSET_DIR = ""


def _asset_root():
    cands = []
    if _ASSET_DIR:
        cands.append(_ASSET_DIR)
    cands.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets"))
    for c in cands:
        try:
            if os.path.isdir(c):
                return c
        except Exception:
            pass
    return ""


def _asset_list():
    """素材清单，递归子文件夹，返回相对路径（用 / 分隔）"""
    if _NO_ASSET:
        return []
    root = _asset_root()
    if not root:
        return []
    exts = (".png", ".jpg", ".jpeg", ".webp", ".bmp")
    out = []
    try:
        for cur, dirs, files in os.walk(root):
            dirs.sort()
            for n in sorted(files):
                if os.path.splitext(n)[1].lower() in exts:
                    rel = os.path.relpath(os.path.join(cur, n), root)
                    out.append(rel.replace("\\", "/"))
            if len(out) >= 300:
                break
        return out[:300]
    except Exception:
        return []


def asset_index():
    """给提示词用的素材清单"""
    return _asset_list()


def asset_meta(limit=200):
    """素材清单带尺寸，挑图那步要看这个才知道哪张适合当大图"""
    root = _asset_root()
    if not root:
        return []
    out = []
    for rel in _asset_list()[:limit]:
        full = os.path.join(root, rel.replace("/", os.sep))
        w = h = 0
        try:
            with Image.open(full) as im:
                w, h = im.size
        except Exception:
            pass
        try:
            kb = os.path.getsize(full) // 1024
        except Exception:
            kb = 0
        out.append({"file": rel, "w": w, "h": h, "kb": kb})
    return out


def asset_brief(limit=200):
    """一行一张：路径 宽x高 大小"""
    rows = []
    for m in asset_meta(limit):
        size = ("%dx%d" % (m["w"], m["h"])) if m["w"] else "尺寸未知"
        kb = m["kb"]
        sz = ("%.1fMB" % (kb / 1024.0)) if kb >= 1024 else ("%dKB" % kb)
        rows.append("    %s    %s    %s" % (m["file"], size, sz))
    return "\n".join(rows) if rows else "    （素材目录里一张图都没有）"


def _asset(name):
    """按清单里的名字取素材（支持子文件夹），返回 RGBA 图，取不到返回 None"""
    if _NO_ASSET:
        return None
    if not name or not isinstance(name, str):
        return None
    root = _asset_root()
    if not root:
        return None
    rel = name.strip().strip('"').replace("\\", "/").lstrip("/")
    full = os.path.normpath(os.path.join(root, rel))
    rootn = os.path.normpath(root)
    if not (full == rootn or full.startswith(rootn + os.sep)):
        return None
    if not os.path.isfile(full):
        # 只给了文件名的话，在子文件夹里找一遍
        base = os.path.basename(rel)
        for cur, dirs, files in os.walk(rootn):
            if base in files:
                full = os.path.join(cur, base)
                break
        else:
            return None
    try:
        return Image.open(full).convert("RGBA")
    except Exception:
        return None


def _font(size, bold=False):
    try:
        return ImageFont.truetype(Theme.font_bold if bold else Theme.font_main, int(size))
    except Exception:
        try:
            return ImageFont.load_default()
        except Exception:
            return None


def _font_big(size):
    try:
        return ImageFont.truetype(Theme.font_title, int(size))
    except Exception:
        return _font(size, True)


def check(code):
    """先过一遍语法和安全，返回 (能不能跑, 原因)"""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return False, "语法错误: %s" % e

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] not in _ALLOWED_MODULES:
                    return False, "不许 import %s" % a.name
        elif isinstance(node, ast.ImportFrom):
            mod = (node.module or "").split(".")[0]
            if mod not in _ALLOWED_MODULES:
                return False, "不许 import %s" % (node.module or "?")
        elif isinstance(node, ast.While):
            if isinstance(node.test, ast.Constant) and node.test.value is True:
                return False, "不许 while True"
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            return False, "不许碰双下划线属性"
        elif isinstance(node, ast.Name) and node.id in _BLOCKED_NAMES:
            return False, "不许用 %s" % node.id

    if "def draw" not in code:
        return False, "代码里没有 def draw"
    return True, ""


PRELUDE = """
def _hex(s, a=255):
    s = str(s).strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16), a)

def _grad(img, box, c1, c2, horizontal=False):
    x1, y1, x2, y2 = [int(v) for v in box]
    w, h = max(1, x2 - x1), max(1, y2 - y1)
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    dd = ImageDraw.Draw(layer)
    c1 = tuple(c1)[:3]
    c2 = tuple(c2)[:3]
    if horizontal:
        for i in range(w):
            t = i / float(max(1, w - 1))
            col = tuple(int(c1[k] + (c2[k] - c1[k]) * t) for k in range(3))
            dd.line([(i, 0), (i, h)], fill=col)
    else:
        for i in range(h):
            t = i / float(max(1, h - 1))
            col = tuple(int(c1[k] + (c2[k] - c1[k]) * t) for k in range(3))
            dd.line([(0, i), (w, i)], fill=col)
    img.paste(layer, (x1, y1))
    return img

def _rr(d, box, r, fill=None, outline=None, width=1):
    return d.rounded_rectangle(tuple(int(v) for v in box), radius=int(r),
                               fill=fill, outline=outline, width=int(width))

def _txt(d, xy, s, size=28, color=(255, 255, 255), bold=False, anchor="la"):
    f = font(size, bold)
    if f is None:
        return 0
    d.text(tuple(int(v) for v in xy), str(s), font=f, fill=color, anchor=anchor)
    return d.textlength(str(s), font=f)

def _fit(img, src, box, pos="center"):
    if src is None:
        return None
    x1, y1, x2, y2 = [int(v) for v in box]
    bw, bh = max(1, x2 - x1), max(1, y2 - y1)
    k = min(bw / float(src.width), bh / float(src.height))
    nw, nh = max(1, int(src.width * k)), max(1, int(src.height * k))
    im2 = src.resize((nw, nh), Image.LANCZOS)
    x = x1 + (bw - nw) // 2
    if pos in ("top", "t"):
        y = y1
    elif pos in ("bottom", "b"):
        y = y2 - nh
    else:
        y = y1 + (bh - nh) // 2
    img.paste(im2, (x, y), im2)
    return (x, y, nw, nh)

def _bar(d, box, pct, fill, bg=(255, 255, 255, 30), r=None):
    x1, y1, x2, y2 = [int(v) for v in box]
    r = int((y2 - y1) / 2) if r is None else int(r)
    _rr(d, box, r, fill=bg)
    pct = max(0.0, min(100.0, float(pct or 0)))
    w = int((x2 - x1) * pct / 100.0)
    if w > 2:
        _rr(d, (x1, y1, x1 + max(w, r * 2), y2), r, fill=fill)
    return pct
"""

_PRELUDE_OVERRIDE = None


def set_prelude(text):
    """设置面板里那份自定义骨架工具，留空就用内置"""
    global _PRELUDE_OVERRIDE
    _PRELUDE_OVERRIDE = (text or "").strip() or None


def get_prelude():
    return _PRELUDE_OVERRIDE if _PRELUDE_OVERRIDE is not None else PRELUDE


def _wrap_text(txt, size, maxw, bold=False):
    """按字号和可用宽度把一句话拆成几行，中文按字断"""
    f = _font(size, bold)
    out, line = [], ""
    for ch in str(txt or ""):
        if ch == "\n":
            out.append(line)
            line = ""
            continue
        t = line + ch
        if line and f.getlength(t) > maxw:
            out.append(line)
            line = ch
        else:
            line = t
    if line:
        out.append(line)
    return out or [""]


def _used_comment(code):
    """代码里到底有没有用 comment，光看 draw 的签名不算"""
    body = re.sub(r"def\s+draw\s*\([^)]*\)\s*:", "", code, count=1)
    return bool(re.search(r"\bcomment\b", body))


def _footer_comment(path, comment, who=None, accent=(235, 235, 235)):
    """模型忘了画点评时，底部补一条横条，不至于整张卡没话"""
    im = Image.open(path).convert("RGBA")
    w0, h0 = im.size
    fs = max(16, int(h0 * 0.024))
    pad = max(16, int(w0 * 0.032))
    lh = int(fs * 1.6)
    lines = _wrap_text(comment, fs, w0 - pad * 2)
    limit = max(1, int(h0 * 0.42) // lh)
    if len(lines) > limit:
        lines = lines[:limit]
        lines[-1] = lines[-1][:-1] + "…"
    band = pad // 2 + lh * len(lines) + (int(fs * 1.5) if who else 0) + pad // 2
    band = max(band, lh + pad)
    band = min(band, h0)
    layer = Image.new("RGBA", (w0, band), (0, 0, 0, 0))
    dd = ImageDraw.Draw(layer)
    dd.rectangle((0, 0, w0, band), fill=(0, 0, 0, 155))
    dd.rectangle((0, 0, w0, max(2, int(fs * 0.1))), fill=tuple(accent) + (210,))
    y = pad // 2
    for ln in lines:
        dd.text((pad, y), ln, font=_font(fs), fill=(246, 246, 246))
        y += lh
    if who:
        dd.text((w0 - pad, y + int(fs * 0.15)), str(who), font=_font(max(14, int(fs * 0.78))),
                fill=tuple(accent), anchor="ra")
    im.alpha_composite(layer, (0, h0 - band))
    im.convert("RGB").save(path)
    return path


def _rgb3(v, default=(128, 128, 128)):
    """#RRGGBB / #RGB -> (r, g, b)，坏了就给默认色"""
    t = str(v or "").strip().lstrip("#")
    if len(t) == 3:
        t = "".join(c * 2 for c in t)
    if len(t) != 6:
        return default
    try:
        return (int(t[0:2], 16), int(t[2:4], 16), int(t[4:6], 16))
    except Exception:
        return default


def _mk_pal(palette):
    """给模板用的取色器：pal('main') / pal('accent', 0, 1) / pal('main', role=1)

    palette 是插件按立绘算出来的配色骨架，模板只管取值，不许自己编色值
    """
    d = palette if isinstance(palette, dict) else {}
    roles = d.get("roles") or []

    def _one(v):
        if isinstance(v, dict):
            v = v.get("hex")
        return v

    def pal(key="main", idx=0, sub=0, default="#808080"):
        r = None
        if roles:
            i = idx if isinstance(idx, int) and 0 <= idx < len(roles) else 0
            r = roles[i] if isinstance(roles[i], dict) else None
        v = None
        if isinstance(r, dict):
            v = r.get(key)
        if v is None:
            v = d.get(key)
        if isinstance(v, list):
            j = sub if isinstance(sub, int) and 0 <= sub < len(v) else 0
            v = v[j] if v else None
        v = _one(v) or default
        return _rgb3(v)

    return pal


def run(code, data, out_path, comment=None, who=None, size=None, auto_comment=False,
        meta=False, palette=None):
    """跑模型写的绘图代码，跑不通就抛异常，调用方自己兜底"""
    ok, why = check(code)
    if not ok:
        raise ValueError(why)

    w, h = size or (W, H)
    ns = {
        "__builtins__": dict(_SAFE_BUILTINS),
        "Image": Image, "ImageDraw": ImageDraw, "ImageFont": ImageFont,
        "math": math, "random": random, "colorsys": colorsys,
        "itertools": itertools, "datetime": datetime, "time": __import__("time"),
        "font": _font, "font_big": _font_big,
        "W": w, "H": h, "GB": 1024 ** 3,
        "asset": _asset, "asset_list": _asset_list,
        "PALETTE": palette if isinstance(palette, dict) else {},
        "pal": _mk_pal(palette),
    }

    pre = get_prelude()
    if pre:
        exec(compile(pre, "<card_prelude>", "exec"), ns)
    exec(compile(code, "<card_code>", "exec"), ns)

    # 模板自己声明了尺寸就听模板的（收藏夹里的竖版模板不会因为改了全局配置就变形）
    for key in ("SIZE", "CANVAS", "SIZE_PX"):
        sz = ns.get(key)
        if isinstance(sz, (tuple, list)) and len(sz) == 2:
            try:
                w, h = int(sz[0]), int(sz[1])
                ns["W"], ns["H"] = w, h
            except Exception:
                pass
            break

    # 尺寸要等模板自己声明完才算得准，所以这行挪到这儿拼
    if meta and comment and str(comment).strip():
        try:
            from .card import size_label
            line = size_label(w, h)
            comment = str(comment).rstrip() + "\n" + line
            ns["COMMENT"] = comment
        except Exception:
            pass

    draw = ns.get("draw")
    if not callable(draw):
        raise ValueError("代码里没有 def draw")

    draw(data, out_path, comment, who)

    # 模型经常只顾排版，把点评漏了，这里兜一层
    if auto_comment and comment and str(comment).strip() and not _used_comment(code):
        try:
            _footer_comment(out_path, comment, who)
            logger.warning("[状态卡·FULL] 代码没把点评画进去，已在底部自动补一条")
        except Exception as e:
            logger.warning("[状态卡·FULL] 补点评失败: %s" % e)
    return out_path


HINT = """写一段 Python 代码，用 Pillow 画一张 {W}x{H} 的服务器状态卡。

环境里已经准备好的（直接就能用，不用 import）：
  Image / ImageDraw / ImageFont
  font(size, bold=False)          正常字体
  font_big(size)                  标题字体
  W = {W}  H = {H}  GB = 1024**3
  asset_list()                    素材清单（相对路径），例如 ["人物甲/a.jpg", "bg.png"]
  asset("人物甲/a.jpg")             按清单里的名字取素材，返回 RGBA 图，取不到返回 None
  PALETTE                         这次按立绘算好的配色（dict：main/sub/accent/roles/bg/layout）
  pal("main")                     取主色 RGB；pal("sub", 0, 1) 取第 1 个角色的第 2 个副色
                                  pal("main", role=1) 取第二个角色的主色。有 PALETTE 就按它上色

现成的画图工具，直接调，别再自己造同名函数：
  _grad(img, (x1,y1,x2,y2), 色A, 色B, horizontal=False)     往 img 上贴一段渐变
  _rr(draw, (x1,y1,x2,y2), r, fill=色, outline=色, width=1)  圆角矩形
  _txt(draw, (x,y), 文字, size=28, color=色, bold=False, anchor="la")  写字，返回文字宽度
  _bar(draw, (x1,y1,x2,y2), 百分比, 前景色, bg=(255,255,255,30))       进度条
  _fit(img, 素材图, (x1,y1,x2,y2), pos="center")            等比塞进框，不拉变形
  _hex("#ff8800")                                           十六进制转 (r,g,b,a)

素材清单（全部就这些，名字必须一字不差，不许自己编）：
{ASSETS}
  math、random、time、colorsys、itertools、datetime 已经在环境里，直接调，不用 import
  内置：len range min max sum round int float str list dict tuple sorted enumerate zip abs isinstance print
  颜色写 (r, g, b) 或 (r, g, b, a)，0-255

必须定义这个函数，签名固定：

  def draw(d, out_path, comment=None, who=None):
      ...

最后一定要 img.save(out_path)。

d 的结构（内存和磁盘是字节，top 里已经是 MB）：

  d["cpu"]          CPU 占用百分比，float，例如 2.9
  d["vm"]           内存，namedtuple：.total .used .free .percent（字节 / 百分比）
  d["C"], d["D"]    磁盘 C 盘 / D 盘，namedtuple：.total .used .free .percent
  d["proc_total"]   进程总数，int
  d["top"]          [(占用MB, 进程名), ...] 按内存降序，一共 12 个
  d["up_days"]      开机天数（整数）
  d["up_h"]         开机小时 0-23，已经去掉整天，直接用
  d["up_total_h"]   开机总小时，只有需要说「总共开了多少小时」时才用
  d["up_m"]         开机分钟 0-59
  d["up_s"]         开机秒 0-59
  d["up_text"]      现成的一句：「3 天 05 时 12 分」，直接贴最省事
  d["up_text_full"] 带秒的版本
  d["host"]         计算机名
  d["cfg"]          硬件配置 dict：
                      "cpu" CPU型号   "cores" 核数   "threads" 线程数
                      "gpu" 显卡      "os" 系统        "ram" 内存条品牌容量
                      "disk" 硬盘型号 "build" 版本号
  d["mood"] d["mood_word"]     综合状态分（0-100）和对应词
  d["slack"] d["life"]         另外两个 0-100 的分
  comment            AI 点评那段话，string 或 None
  who                点评署名，string 或 None

硬性要求：
  - 上面素材清单里有的图，能用就用；用户提到的名字（人、队、作品、地区、角色）如果在清单里出现，必须把那几张图真的取出来当主视觉，不许只在文字里写这个名字就算数
  - 素材图很大，取到先按目标区域等比裁切或缩放再 paste，别整张糊上去
  - 素材取到的是 RGBA 图，要自己 resize 到合适尺寸再 paste
  - 不许用 while True，不许碰 open / eval / exec / __ 开头的属性
  - 只画图，不写文件
  - 排版自己设计，别套模板，该大就大该空就空
  - comment 是这张卡的灵魂，必须真的画在图里，找个位置写上去，旁边或下面标上 who 的署名；漏了会被自动补一条黑条，很难看
  - 画布 {W}x{H}，所有内容必须落在里面，底边至少留 40 像素，别画出界
  - 别让两段文字压在一起，排完自己算一下每块占了多高；大号数字下面紧跟着的小字必须另起一行，
    字号大就缩字号或者把块撑高，绝不许字压在数字上
  - 配色必须从主题长出来，别默认黑绿蓝：朝鲜、将军、红旗、政治这类就是正红配金，
    森林、军事用墨绿，赛博、霓虹用紫青，奢侈、高级用黑金，日系用米白配朱红
  - PALETTE 空的时候，就按上面这条自己定色，别用那种一眼模板感的深灰配荧光绿
  - 只要代码，不要解释，不要 markdown 围栏

示例骨架（只示意，别照着写）：

  def draw(d, out_path, comment=None, who=None):
      img = Image.new("RGB", (W, H), (12, 16, 26))
      dr = ImageDraw.Draw(img)
      dr.text((64, 64), "SERVER", font=font_big(64), fill=(255, 255, 255))
      dr.text((64, 180), "CPU %.0f%%" % d["cpu"], font=font(40), fill=(120, 200, 255))
      img.save(out_path)
"""


def hint_for(w=None, h=None, template=None, assets=None):
    """按画布尺寸出提示词，竖版横版分开引导

    template 是设置面板里填的提示词，占位符 {W} {H} {ASSETS}
    assets 是素材清单，留空就自己去扫
    """
    w = int(w or W)
    h = int(h or H)
    if assets is None:
        alist = asset_brief()
    else:
        rows = []
        for a in list(assets)[:120]:
            if isinstance(a, dict):
                sz = ("%dx%d" % (a.get("w"), a.get("h"))) if a.get("w") else "尺寸未知"
                rows.append("    %s    %s" % (a.get("file"), sz))
            else:
                rows.append("    " + str(a))
        alist = "\n".join(rows) if rows else "    （素材目录里一张图都没有，别去调 asset，老老实实用图形画）"

    tpl = str(template or HINT)
    txt = tpl.replace("{W}", str(w)).replace("{H}", str(h))
    if "{ASSETS}" in tpl:
        txt = txt.replace("{ASSETS}", alist)
    else:
        txt = txt + "\n素材清单（名字必须一字不差）：\n" + alist + "\n"
    if h > w:
        txt += ("\n这次是竖版 %dx%d：宽度窄、高度长，从上往下分层排，别照横版那套左右分栏。\n"
                "标题压在顶上，主数据放中段放大，进程列表和点评沉到底部，中间别留大片空。\n") % (w, h)
    elif w > h:
        txt += "\n这次是横版 %dx%d，可以左右分栏。\n" % (w, h)
    return txt
