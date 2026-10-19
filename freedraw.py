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

from PIL import Image, ImageDraw, ImageFont

from .theme import Theme

W, H = 1600, 1200

_ALLOWED_MODULES = {"PIL", "math", "time", "random", "colorsys", "itertools", "datetime"}

_BLOCKED_NAMES = {
    "__import__", "eval", "exec", "compile", "open", "input", "breakpoint",
    "globals", "locals", "vars", "memoryview", "getattr", "setattr", "delattr",
    "exit", "quit", "help",
}

_SAFE_BUILTINS = {
    "abs": abs, "all": all, "any": any, "bool": bool, "chr": chr,
    "dict": dict, "divmod": divmod, "enumerate": enumerate, "filter": filter,
    "float": float, "format": format, "hex": hex, "int": int,
    "isinstance": isinstance, "len": len, "list": list, "map": map,
    "max": max, "min": min, "ord": ord, "pow": pow, "print": print,
    "range": range, "repr": repr, "reversed": reversed, "round": round,
    "set": set, "sorted": sorted, "str": str, "sum": sum, "tuple": tuple,
    "zip": zip, "Exception": Exception, "ValueError": ValueError,
    "TypeError": TypeError, "RuntimeError": RuntimeError,
}


def _safe_import(name, globals=None, locals=None, fromlist=(), level=0):
    """只放行白名单里的模块，模型写 from PIL import Image 这种能过"""
    if name.split(".")[0] not in _ALLOWED_MODULES:
        raise ImportError("不许 import %s" % name)
    return builtins.__import__(name, globals, locals, fromlist, level)


_SAFE_BUILTINS["__import__"] = _safe_import


_ASSET_DIR = ""


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
    root = _asset_root()
    if not root:
        return []
    try:
        names = [n for n in os.listdir(root)
                 if os.path.splitext(n)[1].lower() in (".png", ".jpg", ".jpeg", ".webp", ".bmp")]
        return sorted(names)[:200]
    except Exception:
        return []


def _asset(name):
    """按文件名取素材，返回 RGBA 图，取不到返回 None"""
    if not name or not isinstance(name, str):
        return None
    root = _asset_root()
    if not root:
        return None
    base = os.path.basename(name.strip())
    path = os.path.join(root, base)
    if not os.path.isfile(path):
        return None
    try:
        return Image.open(path).convert("RGBA")
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


def run(code, data, out_path, comment=None, who=None):
    """跑模型写的绘图代码，跑不通就抛异常，调用方自己兜底"""
    ok, why = check(code)
    if not ok:
        raise ValueError(why)

    ns = {
        "__builtins__": dict(_SAFE_BUILTINS),
        "Image": Image, "ImageDraw": ImageDraw, "ImageFont": ImageFont,
        "font": _font, "font_big": _font_big,
        "W": W, "H": H, "GB": 1024 ** 3,
        "asset": _asset, "asset_list": _asset_list,
    }

    exec(compile(code, "<card_code>", "exec"), ns)
    draw = ns.get("draw")
    if not callable(draw):
        raise ValueError("代码里没有 def draw")

    draw(data, out_path, comment, who)
    return out_path


HINT = """写一段 Python 代码，用 Pillow 画一张 1600x1200 的服务器状态卡。

环境里已经准备好的（直接就能用，不用 import）：
  Image / ImageDraw / ImageFont
  font(size, bold=False)          正常字体
  font_big(size)                  标题字体
  W = 1600  H = 1200  GB = 1024**3
  asset_list()                    素材文件夹里的文件名列表，例如 ["flag.png", "bg.jpg"]
  asset("flag.png")               按名字取素材，返回 RGBA 图，取不到返回 None
  也可以 import PIL、math、time、random、colorsys、itertools（只有这几个）
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
  - 想用素材就先 asset_list() 看看有什么，名字必须来自这个列表，别自己编文件名
  - 素材取到的是 RGBA 图，要自己 resize 到合适尺寸再 paste
  - 不许用 while True，不许碰 open / eval / exec / __ 开头的属性
  - 只画图，不写文件
  - 排版自己设计，别套模板，该大就大该空就空
  - 画布 1600x1200，所有内容必须落在里面，底边至少留 40 像素，别画出界
  - 别让两段文字压在一起，排完自己算一下每块占了多高
  - 只要代码，不要解释，不要 markdown 围栏

示例骨架（只示意，别照着写）：

  def draw(d, out_path, comment=None, who=None):
      img = Image.new("RGB", (W, H), (12, 16, 26))
      dr = ImageDraw.Draw(img)
      dr.text((64, 64), "SERVER", font=font_big(64), fill=(255, 255, 255))
      dr.text((64, 180), "CPU %.0f%%" % d["cpu"], font=font(40), fill=(120, 200, 255))
      img.save(out_path)
"""
