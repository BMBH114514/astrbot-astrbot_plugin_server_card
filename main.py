# -*- coding: utf-8 -*-
"""服务器状态卡 —— 把主机状态画成一张图，底部带 AI 点评。"""

import asyncio
import datetime
import glob
import json
import math
import shutil
import os
import random
import re
import time
import traceback

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .card import apply_config, collect, render
from . import freedraw

# 改子模块（card/freedraw/htmlcard）后重载插件，AstrBot 只会重跑 main，
# 子模块还在内存里躺着，这里手动 reload 一遍，免得改了不生效
def reload_submodules():
    import importlib
    for name in ("theme", "card", "freedraw", "htmlcard", "persona"):
        try:
            mod = importlib.import_module("." + name, __package__)
            importlib.reload(mod)
        except Exception as _e:
            pass


try:
    reload_submodules()
except Exception:
    pass


BLOCK_NAMES = ("header", "kpi", "chart", "config", "disk", "comment")


def _extract_code(text):
    """把模型回的代码从 markdown 围栏里扒出来"""
    t = (text or "").strip()
    t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
    t = re.sub(r"\s*```$", "", t).strip()
    i = t.find("def draw")
    if i > 0:
        t = t[i:]
    return t.strip()


def _extract_html_code(text):
    """从模型回复里抠出带 TEMPLATE 的代码块

    HTML 模板跟绘图代码不一样，没有 def draw 可以切，
    所以先按 ``` 围栏分段找含 TEMPLATE 的那段，再退回全文截取。
    """
    t = (text or "").strip()
    pat = re.compile(r"^\s*(TEMPLATE|HTML|TPL|template)\s*=", re.M)
    blocks = re.findall(r"```[a-zA-Z]*\s*(.*?)```", t, re.S)
    for b in (blocks or []):
        if pat.search(b):
            return b.strip()
    m = pat.search(t)
    if m:
        return t[m.start():].strip()
    # 模型直接甩了一坨 HTML，就替它套上 TEMPLATE
    h = t.find("<html")
    if h < 0:
        h = t.find("<!doctype")
    if h >= 0:
        body = t[h:]
        body = re.sub(r"```\s*$", "", body).strip()
        return "TEMPLATE = r\'\'\'%s\'\'\'" % body
    return ""


def _wants_multi(req):
    """用户有没有明说要多个角色/元素，没说要就别让素材凑数"""
    t = str(req or "")
    keys = ("两个", "三个", "多个", "两张", "三张", "双人", "对比", "并列", "配合")
    return any(k in t for k in keys)


def _ok_hex(v):
    """只认六位 hex，别的一律当没有"""
    t = str(v or "").strip()
    if re.fullmatch(r"#[0-9A-Fa-f]{6}", t):
        return t.upper()
    return ""


# 固定立绘：留空就按正常流程从素材文件夹挑，想临时钉死某一张再填相对路径
FIXED_HERO = ""

# 用户说这五个字连在一起，这张卡就一张素材都不许用
NO_ASSET = "不使用素材"

def _extract_json(text):
    """从模型回复里抠出 JSON，带 ``` 包裹也能认"""
    if not text:
        return None
    t = text.strip()
    t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        return json.loads(t[i:j + 1])
    except Exception:
        return None


PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(PLUGIN_DIR, "out")
STYLES_DIR = os.path.join(PLUGIN_DIR, "styles")
ERROR_DIR = os.path.join(OUT_DIR, "errors")
BACKUP_DIR = os.path.join(STYLES_DIR, "_backup")
FAV_DIR = os.path.join(STYLES_DIR, "_favorites")

# livingmemory 的 register 名和目录名都试一遍
PLUGIN_VERSION = "1.8.7"

# 每次重做随机抽一个方向，逼模型换一套版面，别老是那副样子
FREE_MOODS = [
    "极简黑白海报，大量留白，只靠粗细文字和大色块撑版面",
    "赛博朋克霓虹，紫粉青撞色，扫描线，发光描边",
    "复古报纸，分栏排版，压字大标题，细线分隔",
    "蒸汽波，粉紫渐变，网格地平线，故障色块",
    "科幻 HUD 仪表盘，环形刻度，准星，细密网格",
    "工业暗红，铁锈色配警示黄，粗黑大写",
    "日式侘寂，米白宣纸底，细字，大留白",
    "像素复古游戏，硬边方块，8bit 配色",
    "玻璃拟态，半透明层叠卡片，柔和投影",
    "杂志封面，超大标题压版，图文穿插",
    "工程蓝图，深蓝底白线，标注引线",
    "故障艺术，错位色带，撕裂文字",
    "粗野主义，超粗无衬线，黑底白字配撞色",
    "数据可视化海报，大数字当视觉主体",
    "终端窗口，等宽字，命令行味道",
    "暗黑哥特，深色底细金线，装饰纹样",
    "糖果色，马卡龙撞色，圆润卡片",
    "水彩纸质，柔和晕染，手写感",
    "军事简报，深绿卡其，表格化",
    "太空站，深空黑配冰蓝，星点背景",
]
PLUGIN_AUTHOR = "FULL（B站 @白木不华）"

# livingmemory 的 register 名和目录名都试一遍
LMEM_NAMES = ("astrbot_plugin_livingmemory", "LivingMemory")


# ---- 画布尺寸：认 1080x1920、9:16、竖版/横版/方形这几种写法 ----
_SIZE_PRESETS = {
    "9:16": (1080, 1920), "16:9": (1920, 1080),
    "3:4": (1200, 1600), "4:3": (1600, 1200),
    "2:3": (1200, 1800), "3:2": (1800, 1200),
    "1:1": (1440, 1440), "4:5": (1200, 1500), "5:4": (1500, 1200),
}
_SIZE_WORDS = re.compile(r"\d{3,5}\s*[x×*]\s*\d{3,5}|\d{1,2}\s*[:：]\s*\d{1,2}|竖版|横版|竖屏|横屏|方形|方版|portrait|landscape|square", re.I)

# ---- 画布随机模式：配置里写这几个词，就不用管具体数字了 ----
_CANVAS_BUILTIN_MODE = ("随机内置", "内置随机", "随机六种", "内置六种", "random-builtin")
_CANVAS_POOL_MODE = ("随机池", "自定义池", "池里随机", "random-pool")
_CANVAS_WEIRD_MODE = ("奇葩", "离谱", "怪比例", "歪比例", "weird", "absurd")

# 内置六档，跟 rand_canvas_list 留空时的默认一致
_BUILTIN_CANVAS = [(1600, 1200), (1200, 1800), (1080, 1920), (1440, 1440),
                   (1920, 1080), (1200, 1600)]

# 摇奇葩比例用的底料，怎么排都不像正常海报
_WEIRD_RATIOS = [(21, 9), (9, 21), (1, 3), (3, 1), (5, 2), (2, 5), (16, 7), (7, 16),
                 (13, 5), (5, 13), (11, 4), (4, 11), (7, 3), (3, 7), (17, 5), (5, 17)]


def meta_text(w, h):
    """分辨率 + 比例，比例能约成整数比就约，约不出来给小数"""
    w, h = int(w), int(h)
    g = math.gcd(w, h) or 1
    a, b = w // g, h // g
    if g > 1 and max(a, b) <= 32:
        ratio = "%d:%d" % (a, b)
    else:
        ratio = ("%.2f:1" % (w / float(h))) if w >= h else ("1:%.2f" % (h / float(w)))
    return "%d×%d · %s" % (w, h, ratio)


def fit_to_canvas(path, size):
    """把出好的图等比塞进目标尺寸，多出来的地方用底色补，不变形"""
    try:
        from PIL import Image
        tw, th = int(size[0]), int(size[1])
        im = Image.open(path).convert("RGB")
        if (im.width, im.height) == (tw, th):
            return path
        scale = min(tw / float(im.width), th / float(im.height))
        nw, nh = max(1, int(im.width * scale)), max(1, int(im.height * scale))
        im2 = im.resize((nw, nh), Image.LANCZOS)
        bg = Image.new("RGB", (tw, th), im.getpixel((2, 2)))
        bg.paste(im2, ((tw - nw) // 2, (th - nh) // 2))
        bg.save(path)
    except Exception as e:
        logger.warning("[状态卡·FULL] 套目标尺寸失败: %s" % e)
    return path


def parse_canvas_text(text, default=None):
    """从一句话里抠画布尺寸，抠不到就用 default"""
    t = str(text or "").strip().lower().replace("：", ":")
    if not t:
        return default
    m = re.search(r"(\d{3,5})\s*[x×*]\s*(\d{3,5})", t)
    if m:
        w, h = int(m.group(1)), int(m.group(2))
        if 200 <= w <= 4000 and 200 <= h <= 4000:
            return (w - w % 2, h - h % 2)
    m = re.search(r"(\d{1,2})\s*:\s*(\d{1,2})", t)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        key = "%d:%d" % (a, b)
        if key in _SIZE_PRESETS:
            return _SIZE_PRESETS[key]
        if 0 < a < 40 and 0 < b < 40:
            base = 1200
            if a <= b:
                w, h = base, int(round(base * b / float(a)))
            else:
                w, h = int(round(base * a / float(b))), base
            w, h = min(w, 4000), min(h, 4000)
            return (w - w % 2, h - h % 2)
    if "竖" in t or "portrait" in t:
        return (1200, 1800)
    if "横" in t or "landscape" in t:
        return (1600, 1200)
    if "方" in t or "square" in t:
        return (1440, 1440)
    return default


def weird_canvas():
    """摇一个不讲道理的比例：长边随机，短边跟着算，再抖两下"""
    a, b = random.choice(_WEIRD_RATIOS)
    long_side = random.randint(1500, 2600)
    if a >= b:
        w, h = long_side, int(round(long_side * b / float(a)))
    else:
        w, h = int(round(long_side * a / float(b))), long_side
    w = max(400, min(4000, w + random.randint(-70, 70)))
    h = max(400, min(4000, h + random.randint(-70, 70)))
    return (w - w % 2, h - h % 2)


def strip_canvas_text(text):
    """把尺寸那截从话里拿掉，剩下的才是模板关键字 / 风格描述"""
    t = _SIZE_WORDS.sub(" ", str(text or ""))
    return " ".join(t.replace(",", " ").split())


PLAN_HINT = r"""你在给一张服务器状态卡打底稿：先定尺寸、字段、名字，再把绘图的骨架代码搭出来。

{REQ}

可用的数据（只能用这些，没有别的）：
  cpu            CPU 占用百分比
  vm             .total .used .free .percent（字节 / 百分比）
  C / D          磁盘，同上
  proc_total     进程总数
  top            内存占用前 12 名，[(占用MB, 进程名), ...]
  up_text        开机时长，现成的一句话
  up_days up_h up_m up_s
  cfg            硬件 dict：cpu cores threads gpu os ram disk
  mood slack life  三个 0-100 的分

输出两段，第一段是规划 JSON，第二段是代码骨架，中间用 <<<BASE>>> 单独一行隔开。

第一段：

{
  "width": 1200,
  "height": 1800,
  "title": "顶部主标题",
  "blocks": [
    {"name": "给人看的名字", "field": "cpu", "unit": "%", "kind": "big", "note": "想怎么表现"}
  ],
  "palette": "具体配色，写色值",
  "layout": "版面怎么分，两三句"
}

第二段（要不要写看下面的规矩）：

<<<BASE>>>
```python
def draw(d, out_path, comment=None, who=None):
    img = Image.new("RGB", (W, H), (12, 14, 22))
    # 背景、大色块、分区块都在这层定死
    img.save(out_path)
```

规则：
  - 尺寸：{SIZE_RULE}
  - blocks 挑 4 到 7 块，别把数据全塞满，挑对这台机器最有意思的
  - name 可以换叫法，比如 vm.percent 就叫「内存压力」
  - unit 没单位就留空字符串
  - kind 只能填 big / bar / list / text
  - 版面最底下一块要留给点评（comment）：水平横条，高度占整张的 10%~18%，先空着别塞数据
{BASE_RULE}
"""


DEF_COMMENT_HINT = """你正在给一台电脑出状态卡，用一两句话点评，不超过 70 个字，口语，别用括号别用表情，别复述数字。
{NAME}{STYLE}{CTX}
这台机器：{BODY}
只输出这句话。"""



@register(
    "astrbot_plugin_server_card",
    "Full",
    "把主机状态画成一张深色卡片，底部带 AI 点评",
    PLUGIN_VERSION,
)

class ServerCard(Star):

    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config or {}
        os.makedirs(OUT_DIR, exist_ok=True)
        os.makedirs(FAV_DIR, exist_ok=True)
        logger.info(
            "[状态卡·FULL] 服务器状态卡 v%s 已加载 · 作者 %s" % (PLUGIN_VERSION, PLUGIN_AUTHOR)
        )
        logger.info("[状态卡·FULL] 风格目录 %s" % STYLES_DIR)
        self._sync_style_options()

    # ---------------- 配置 ----------------
    def _canvas(self):
        """代码绘图的画布尺寸：默认读配置，指令里传了就以指令为准"""
        return parse_canvas_text(self._text("canvas") or "", (1600, 1200)) or (1600, 1200)

    def _meta_on(self):
        """「显示图片分辨率与比例」那个开关"""
        return bool(self._cfg("show_meta", True))

    def _meta_line(self, size):
        """知道尺寸就给那行字，不知道给空"""
        if not self._meta_on():
            return ""
        try:
            w, h = int(size[0]), int(size[1])
        except Exception:
            return ""
        if w <= 0 or h <= 0:
            return ""
        return meta_text(w, h)

    def _deco_comment(self, comment, size):
        """点评说完，另起一行补本次的分辨率和比例"""
        line = self._meta_line(size)
        if not line:
            return comment
        c = (comment or "").rstrip()
        return (c + "\n" + line) if c else line

    def _canvas_mode(self):
        """配置里那句画布怎么理解：fixed 固定 / builtin 内置六档 / pool 自定义池 / weird 奇葩比例"""
        raw = self._text("canvas").lower()
        if any(w in raw for w in _CANVAS_WEIRD_MODE):
            return "weird", None
        if any(w in raw for w in _CANVAS_BUILTIN_MODE):
            return "builtin", None
        if any(w in raw for w in _CANVAS_POOL_MODE):
            return "pool", None
        return "fixed", self._canvas()

    def _roll_canvas(self):
        """这一轮用多大：随机模式当场摇一个，固定模式直接给"""
        mode, size = self._canvas_mode()
        if mode == "weird":
            size = weird_canvas()
        elif mode == "builtin":
            size = random.choice(_BUILTIN_CANVAS)
        elif mode == "pool":
            size = random.choice(self._canvas_pool())
        return size

    def _cfg(self, key, default=None):
        try:
            v = self.config.get(key, default)
            return default if v is None else v
        except Exception:
            return default

    @staticmethod
    def _whoami(event):
        """日志里认人用"""
        if event is None:
            return "system"
        try:
            return "%s(%s)" % (event.get_sender_name() or "?",
                               event.get_sender_id() or "?")
        except Exception:
            return "?"

    def _text(self, key, default=""):
        return str(self._cfg(key, default) or "").strip()

    def _renderer(self):
        """这个版本两套渲染器：pil 直接画，html 交给浏览器"""
        v = self._text("renderer", "pil").lower()
        return "html" if v in ("html", "浏览器", "web") else "pil"

    def _tpl_kind(self, path):
        """按文件名前缀判渲染器：HTML_xxx 走浏览器，PIL_xxx 走代码绘图，
        没前缀就退回老规矩——.html 走浏览器、.py 走代码、.json 算样式"""
        b = os.path.basename(str(path or "")).lower()
        stem = os.path.splitext(b)[0]
        if stem.startswith("html_"):
            return "html"
        if stem.startswith("pil_"):
            return "pil"
        if b.endswith(".html"):
            return "html"
        if b.endswith(".py"):
            return "pil"
        return "json"

    def _int(self, key, default):
        try:
            v = int(float(self._cfg(key, default)))
            return v if v > 0 else default
        except Exception:
            return default

    def _moods(self):
        """随机方向池，设置里改，留空用内置"""
        raw = self._text("rand_mood_list", "")
        if (raw or "").strip():
            items = [x.strip() for x in re.split(r"[\n\r]+", raw) if x.strip()]
            if items:
                return items
        return FREE_MOODS

    def _canvas_pool(self):
        """随机画布候选，设置里改，留空用内置"""
        raw = self._text("rand_canvas_list", "")
        if (raw or "").strip():
            out = []
            for seg in re.split(r"[,，\s]+", raw):
                sz = parse_canvas_text(seg)
                if sz and sz not in out:
                    out.append(sz)
            if out:
                return out
        return [(1600, 1200), (1200, 1800), (1080, 1920), (1440, 1440),
                (1920, 1080), (1200, 1600)]

    def _prov(self, key, label=""):
        """按环节取模型：这个环节没配就退回通用生成模型，再退回当前对话模型"""
        for k in (key, "generator_provider_id"):
            pid = self._text(k)
            if not pid:
                continue
            p = None
            try:
                p = self.context.get_provider_by_id(pid)
            except Exception as e:
                logger.warning("[状态卡·FULL] 取模型 %s 出错: %s" % (pid, e))
            if p is not None:
                if k != "generator_provider_id":
                    logger.info("[状态卡·FULL] %s 用模型 %s" % (label or key, pid))
                return p
        try:
            return self.context.get_using_provider()
        except Exception:
            return None

    def _code_provider(self):
        """单独给生成用的模型，配置留空就退回当前对话模型"""
        pid = self._text("generator_provider_id")
        if pid:
            p = None
            try:
                p = self.context.get_provider_by_id(pid)
            except Exception as e:
                logger.warning("[状态卡·FULL] 取绘图模型 %s 出错: %s" % (pid, e))
            if p is not None:
                logger.info("[状态卡·FULL] 本次生成使用独立模型 %s（%s）"
                            % (pid, getattr(p, "get_model", lambda: "?")()))
                return p
            logger.warning("[状态卡·FULL] 配置的绘图模型 %s 找不到，退回当前对话模型" % pid)
        try:
            return self.context.get_using_provider()
        except Exception:
            return None

    def _dump_exc(self, tag, exc=None, extra=""):
        """完整堆栈：进后台日志，同时落一份到 out/errors"""
        tb = traceback.format_exc()
        if not tb or tb.strip() in ("NoneType: None", ""):
            if exc is not None:
                tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            else:
                tb = "无堆栈信息"
        ts = datetime.datetime.now()
        head = "[状态卡·FULL] %s · %s" % (tag, ts.strftime("%Y-%m-%d %H:%M:%S"))
        body = head + ("\n" + extra if extra else "") + "\n" + tb
        logger.error(body)
        path = ""
        try:
            os.makedirs(ERROR_DIR, exist_ok=True)
            safe = re.sub(r"[^\w\-\u4e00-\u9fa5]", "_", tag)[:40]
            path = os.path.join(ERROR_DIR, ts.strftime("%Y%m%d_%H%M%S_") + safe + ".log")
            with open(path, "w", encoding="utf-8") as f:
                f.write(body)
        except Exception:
            path = ""
        return path

    def _err_report(self, tag, exc=None, brief=""):
        """按配置决定聊天里回多详细，完整堆栈永远写日志"""
        mode = self._text("error_report", "brief").lower()
        path = self._dump_exc(tag, exc, brief)
        if mode == "off":
            return "出错了，详细看后台日志"
        lines = ["出错：%s" % (brief or (str(exc) if exc else "未知"))]
        if mode == "full":
            detail = traceback.format_exc()
            if not detail or detail.strip() in ("NoneType: None", ""):
                detail = ("%s: %s" % (type(exc).__name__, exc)) if exc else "无堆栈信息"
            detail = detail.strip()
            if len(detail) > 1500:
                detail = detail[:1500] + "\n...(太长截断，完整见文件)"
            lines.append("```\n%s\n```" % detail)
        if path:
            lines.append("完整日志：%s" % path)
        return "\n".join(lines)

    # ---------------- 风格 ----------------
    # ---------------- 收藏夹 ----------------
    def _fav_files(self):
        """styles/_favorites 里的模板，用户自己塞的，插件只读不删"""
        try:
            os.makedirs(FAV_DIR, exist_ok=True)
        except Exception:
            return []
        out = []
        try:
            for f in sorted(os.listdir(FAV_DIR)):
                fp = os.path.join(FAV_DIR, f)
                if os.path.isfile(fp) and not f.startswith(".") \
                        and f.lower().endswith((".py", ".json", ".html")):
                    out.append(fp)
        except Exception as e:
            logger.warning("[状态卡·FULL] 读收藏夹失败: %s" % e)
        return out

    def _style_templates(self):
        """styles 根目录下的模板，下划线开头的目录不扫"""
        out = []
        try:
            for f in sorted(os.listdir(STYLES_DIR)):
                fp = os.path.join(STYLES_DIR, f)
                if os.path.isfile(fp) and not f.startswith(".") \
                        and f.lower().endswith((".py", ".json")):
                    out.append(fp)
        except Exception:
            pass
        return out

    def _load_style_abs(self, path):
        """按路径直接套一份样式，收藏夹用的"""
        from . import theme as _theme
        _theme.reset()
        try:
            with open(path, encoding="utf-8") as f:
                _theme.apply_dict(json.load(f))
            logger.info("[状态卡·FULL] 收藏样式 %s" % os.path.basename(path))
            return True
        except Exception as e:
            logger.warning("[状态卡·FULL] 收藏样式 %s 读不了: %s"
                           % (os.path.basename(path), e))
            return False

    @staticmethod
    def _fuzzy_hit(key, name):
        """子串匹配不上时兜底：关键字的字符大多数都出现在名字里就算中"""
        import re as _re
        k = _re.sub(r"[^\u4e00-\u9fa5a-z0-9]", "", (key or "").lower())
        if not k:
            return False
        n = _re.sub(r"^(html|pil)[_\- ]+", "", (name or ""), flags=_re.I).lower()
        if k in n:
            return True
        hit = sum(1 for c in k if c in n)
        need = len(k) if len(k) <= 2 else max(2, int(len(k) * 0.7))
        return hit >= need

    def _pick_template(self, key):
        """按关键词抽模板，收藏夹优先，返回 (路径, 来源)"""
        key = (key or "").strip().lower()
        for pool, tag in ((self._fav_files(), "收藏"),
                          (self._style_templates(), "风格")):
            if key:
                cands = [p for p in pool if key in os.path.basename(p).lower()]
                if not cands:
                    cands = [p for p in pool
                             if self._fuzzy_hit(key, os.path.basename(p))]
            else:
                cands = list(pool)
            if cands:
                return random.choice(cands), tag
        return None, ""

    _CMD_NAMES = ("服务器状态卡", "状态卡", "出状态卡",
                  "状态卡自由生成", "自由生成", "代码生成卡片", "状态卡代码",
                  "状态卡HTML生成", "HTML生成", "网页状态卡", "状态卡网页生成", "网页生成",
                  "状态卡重做风格", "重做卡片风格", "随机风格", "换个风格")

    def _arg_text(self, event, arg=""):
        """取指令后面那截字

        AstrBot 只把第一个词塞进 arg，后面几个词会丢，
        所以能拿到 message_str 就整串用，别被截成半截。
        """
        a = str(arg or "").strip()

        def _clean(t):
            t = re.sub(r"^(\[[^\]]{0,40}\]\s*)+", "", str(t or ""))  # 开头的 CQ 码 / @
            return re.sub(r"^[/#\uff0f!！]\s*", "", t).strip()

        s = _clean(a)
        try:
            full = str(getattr(event, "message_str", "") or "").strip()
        except Exception:
            full = ""
        if full:
            full = _clean(full)
            rest, hit = full, None
            for n in sorted(self._CMD_NAMES, key=len, reverse=True):
                if full.startswith(n):
                    hit = full[len(n):].strip()
                    break
            if hit is None:
                hit = rest
            if len(hit) >= len(a):
                s = hit
        if not s:
            return ""
        s = re.sub(r"^[/#\uff0f!！]\s*", "", s)
        for n in sorted(self._CMD_NAMES, key=len, reverse=True):
            if s.startswith(n):
                return s[len(n):].strip()
        return s

    def _fav_key(self, event, arg=""):
        """None 表示没带参数，照常出图
        「随机 霓虹」-> 霓虹，「随机」-> 空串（整个收藏夹随机）"""
        s = self._arg_text(event, arg)
        if not s:
            return None
        m = re.match(r"^(?:随机抽|收藏夹|随机|抽取|随便|收藏)\s*[:：,，]?\s*(.*)$", s)
        if m:
            return m.group(1).strip()
        return s

    def _fav_list(self):
        favs = self._fav_files()
        if not favs:
            return ("收藏夹空着\n路径：styles/_favorites\n"
                    "把 .py 绘图代码、.json 样式或 .html 模板丢进去，就能被抽到")
        lines = ["收藏夹 %d 个：%s" % (len(favs), FAV_DIR)]
        for fp in favs[:20]:
            lines.append("· " + os.path.basename(fp))
        if len(favs) > 20:
            lines.append("…还有 %d 个" % (len(favs) - 20))
        lines.append("用 /服务器状态卡 随机 关键词 抽")
        return "\n".join(lines)

    def _fav_miss(self, key):
        names = [os.path.splitext(os.path.basename(x))[0]
                 for x in self._fav_files() + self._style_templates()]
        tail = ""
        if names:
            tail = "\n现有：" + "、".join(names[:12])
            if len(names) > 12:
                tail += "…"
        if key:
            return ("没找到名字带「%s」的模板%s\n"
                    "把 .py / .json / .html 丢进 styles/_favorites 再试" % (key, tail))
        return ("收藏夹里没有能用的模板%s\n"
                "把 .py / .json / .html 丢进 styles/_favorites 再试" % tail)

    def _style_file(self, name):
        safe = re.sub(r"[^\w\-\u4e00-\u9fa5]", "", str(name or ""))
        return os.path.join(STYLES_DIR, (safe or "default") + ".json")

    def _load_style(self, name=None):
        """读 styles/<名字>.json 套到 Theme 上，返回最终用的名字"""
        from . import theme as _theme
        _theme.reset()
        if not name:
            name = self._text("style_name", "default") or "default"
        path = self._style_file(name)

        os.makedirs(STYLES_DIR, exist_ok=True)
        if not os.path.exists(path):
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(_theme.to_dict(), f, ensure_ascii=False, indent=2)
                logger.info("[状态卡·FULL] 没有 %s，已生成一份模板" % path)
            except Exception as e:
                logger.warning("[状态卡·FULL] 写风格模板失败: %s" % e)
            return name

        try:
            with open(path, encoding="utf-8") as f:
                _theme.apply_dict(json.load(f))
            logger.info("[状态卡·FULL] 风格 %s" % name)
        except Exception as e:
            logger.warning("[状态卡·FULL] 风格 %s 读取失败: %s" % (name, e))
        return name

    def _persona_style_path(self):
        """跟随人格时，风格存成 styles/persona_<人格>.json"""
        pid = self._text("comment_persona_id", "")
        if not pid:
            return None, ""
        who = self._text("persona_name", "") or ""
        safe = re.sub(r"[^\w\-\u4e00-\u9fa5]", "", str(who)) or \
            re.sub(r"[^\w\-\u4e00-\u9fa5]", "", str(pid)) or "persona"
        path = os.path.join(STYLES_DIR, "persona_" + safe + ".json")
        old = os.path.join(STYLES_DIR, "persona_" +
                           (re.sub(r"[^\w\-\u4e00-\u9fa5]", "", str(pid)) or "persona") + ".json")
        if old != path and os.path.exists(old) and not os.path.exists(path):
            try:
                shutil.copyfile(old, path)
                logger.info("[状态卡·FULL] 人格风格改名 %s -> %s"
                            % (os.path.basename(old), os.path.basename(path)))
            except Exception:
                return old, pid
        return path, pid

    def _persona_text(self, pid):
        """拿这个人格的名字和设定"""
        try:
            pm = getattr(self.context, "persona_manager", None)
            p = pm.get_persona_v3_by_id(pid) if pm else None
            if p is None:
                return pid, ""
            if isinstance(p, dict):
                return (p.get("name") or pid,
                        p.get("prompt") or p.get("system_prompt") or "")
            return getattr(p, "name", pid) or pid, getattr(p, "prompt", "") or ""
        except Exception as e:
            logger.warning("[状态卡·FULL] 取人格失败: %s" % e)
            return pid, ""

    async def _build_persona_style(self, path, pid):
        """让这个人格照自己的性格，把整张卡重做一遍"""
        from . import theme as _theme

        provider = self.context.get_using_provider()
        if provider is None:
            logger.warning("[状态卡·FULL] 没有可用模型，人格风格做不了")
            return self._load_style("default")

        name, prompt = self._persona_text(pid)
        _theme.reset()
        current = _theme.to_dict()
        full = (
            "你是「%s」。\n%s\n\n"
            "现在给自己设计一张服务器状态卡的视觉风格，数据由程序填，你只管设计。\n"
            "完全按你的性格和审美来：配色、字号、圆角、板块顺序、间距都从你的人设出发。\n"
            "温和柔和的性格就用浅色、低对比、大圆角；硬朗粗犷的性格就用深色、高对比、大字号、直角。\n"
            "不要套默认模板，让这张卡一看就是你的。\n\n"
            "当前配置（可以整套推翻）：\n%s\n\n%s\n只输出 JSON。"
        ) % (name, (prompt or "").strip()[:1500],
             json.dumps(current, ensure_ascii=False), self._tpl("prompt_style", self._STYLE_HINT))

        try:
            resp = await provider.text_chat(
                prompt=full,
                system_prompt="你是%s，正在给自己的状态卡做设计，只输出 JSON。" % name,
            )
            text = getattr(resp, "completion_text", "") or ""
            data = _extract_json(text)
        except Exception as e:
            logger.warning("[状态卡·FULL] 人格风格生成失败: %s" % e)
            data = None

        if not data:
            logger.warning("[状态卡·FULL] 人格风格没拿到合法 JSON，先用默认")
            return self._load_style("default")

        clean, notes = self._clean_style(data)
        if not clean:
            return self._load_style("default")

        try:
            os.makedirs(STYLES_DIR, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(clean, f, ensure_ascii=False, indent=2)
            self._archive_style(path)
            self._sync_style_options()
            logger.info("[状态卡·FULL] 人格 %s 的风格已存到 %s%s" % (
                pid, os.path.basename(path),
                ("（%s）" % "；".join(notes[:3])) if notes else ""))
        except Exception as e:
            logger.warning("[状态卡·FULL] 人格风格写入失败: %s" % e)
            return self._load_style("default")

        return self._load_style(os.path.basename(path)[:-5])

    async def _resolve_style(self):
        """这次用哪套风格：跟随人格就用他那份，否则用配置里的风格名"""
        if not self._cfg("style_from_persona", False):
            return self._load_style()

        path, pid = self._persona_style_path()
        if not path:
            logger.warning("[状态卡·FULL] 开了跟随人格但没选人格，先用默认风格")
            return self._load_style("default")
        if os.path.exists(path):
            return self._load_style(os.path.basename(path)[:-5])
        return await self._build_persona_style(path, pid)

    def _code_style_path(self):
        """挑一份绘图代码出来

        code_file 四种写法：
          空           -> 不走代码，用 styles 里的 JSON
          code_xxx     -> 固定用这份
          random       -> 所有 code_*.py 里随机抽，每次都换
          a, b, c      -> 在这几份里随机抽
        """
        raw = self._text("code_file", "")
        if not raw:
            return None

        if raw.lower() in ("random", "*", "随机", "all"):
            files = sorted(glob.glob(os.path.join(STYLES_DIR, "*.py")))
            return random.choice(files) if files else None

        names = [x.strip() for x in re.split(r"[,，\s]+", raw) if x.strip()]
        cands = []
        for n in names:
            if not n.endswith(".py"):
                n += ".py"
            p = os.path.join(STYLES_DIR, n)
            if os.path.exists(p):
                cands.append(p)
        return random.choice(cands) if cands else None


    def _sync_style_options(self):
        """风格名是手填的，这里只更新提示，把现有文件名列出来"""
        try:
            names = sorted(os.path.basename(f)[:-5]
                           for f in glob.glob(os.path.join(STYLES_DIR, "*.json")))
        except Exception:
            return
        if not names:
            return

        path = os.path.join(PLUGIN_DIR, "_conf_schema.json")
        try:
            with open(path, encoding="utf-8") as f:
                sc = json.load(f)
        except Exception as e:
            logger.warning("[状态卡·FULL] 读 schema 失败: %s" % e)
            return

        node = sc.get("style_name")
        if not isinstance(node, dict):
            return

        hint = "填 styles/ 里的文件名，不用带 .json，留空就用 default。"

        changed = False
        if node.get("hint") != hint:
            node["hint"] = hint
            changed = True
        for k in ("options", "labels"):
            if k in node:
                node.pop(k)
                changed = True
        sc["style_name"] = node
        if not changed:
            return

        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(sc, f, ensure_ascii=False, indent=2)
            logger.info("[状态卡·FULL] 风格名改成手填，现有：%s" % "、".join(names))
            self._sync_code_options()
        except Exception as e:
            logger.warning("[状态卡·FULL] 写 schema 失败: %s" % e)

    def _sync_code_options(self):
        """代码风格也是手填，这里只更新提示"""
        try:
            names = sorted(os.path.basename(f)[:-3]
                           for f in glob.glob(os.path.join(STYLES_DIR, "*.py")))
        except Exception:
            return

        path = os.path.join(PLUGIN_DIR, "_conf_schema.json")
        try:
            with open(path, encoding="utf-8") as f:
                sc = json.load(f)
        except Exception:
            return

        node = sc.get("code_file")
        if not isinstance(node, dict):
            return

        hint = ("留空走 JSON 样式；填 random 每次随机一套代码；"
                "填文件名（逗号隔开几个）就在这几个里随机。")

        changed = False
        if node.get("hint") != hint:
            node["hint"] = hint
            changed = True
        for k in ("options", "labels"):
            if k in node:
                node.pop(k)
                changed = True
        sc["code_file"] = node
        if not changed:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(sc, f, ensure_ascii=False, indent=2)
            logger.info("[状态卡·FULL] 代码风格改成可随机：%s" % ("、".join(names) or "还没有"))
        except Exception as e:
            logger.warning("[状态卡·FULL] 写 schema 失败: %s" % e)


    def _archive_style(self, path):
        """每做出一版就留个存档，只保留最近几份"""
        try:
            keep = int(self._cfg("style_versions_keep", 8) or 8)
        except Exception:
            keep = 8
        vdir = os.path.join(STYLES_DIR, "_versions")
        try:
            os.makedirs(vdir, exist_ok=True)
            name = os.path.basename(path)[:-5]
            base = "%s_%s" % (name, datetime.datetime.now().strftime("%m%d_%H%M%S"))
            dst = os.path.join(vdir, base + ".json")
            n = 2
            while os.path.exists(dst):
                dst = os.path.join(vdir, "%s_%d.json" % (base, n))
                n += 1
            shutil.copyfile(path, dst)

            files = sorted(glob.glob(os.path.join(vdir, "*.json")), key=os.path.getmtime)
            if keep > 0:
                for f in files[:-keep]:
                    os.remove(f)
                left = min(len(files), keep)
            else:
                left = len(files)
            logger.info("[状态卡·FULL] 版本已存档 %s，%s 里留了 %d 份"
                        % (os.path.basename(dst), "_versions", left))
        except Exception as e:
            logger.warning("[状态卡·FULL] 版本存档失败: %s" % e)

    def _backup_style(self, name):
        """把当前这套风格备份一份到 styles/_backup"""
        if not self._cfg("style_auto_backup", True):
            return
        try:
            from . import theme as _theme
            os.makedirs(BACKUP_DIR, exist_ok=True)
            day = datetime.date.today().isoformat()
            path = os.path.join(BACKUP_DIR, "%s_%s.json" % (name, day))
            if not os.path.exists(path):
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(_theme.to_dict(), f, ensure_ascii=False, indent=2)
                logger.info("[状态卡·FULL] 风格已备份 %s" % os.path.basename(path))

            keep = int(self._cfg("style_backup_keep", 10) or 10)
            files = sorted(glob.glob(os.path.join(BACKUP_DIR, "*.json")))
            for old in files[:-max(1, keep)]:
                os.remove(old)
        except Exception as e:
            logger.warning("[状态卡·FULL] 备份风格失败: %s" % e)

    # ---------------- 人格 ----------------
    async def _persona_prompt(self):
        """按下拉选的人格取 system_prompt，没选就返回空"""
        pid = self._text("comment_persona_id")
        if not pid:
            return ""
        try:
            pm = getattr(self.context, "persona_manager", None)
            if pm is None:
                return ""
            p = await pm.get_persona(pid)
            sp = getattr(p, "system_prompt", "") or ""
            if sp:
                logger.info("[状态卡·FULL] 点评使用人格 %s" % pid)
            return sp
        except Exception as e:
            logger.warning("[状态卡·FULL] 取人格失败: %s" % e)
            return ""

    # ---------------- 上下文 ----------------
    def _lmem(self):
        """拿到 livingmemory 插件实例，拿不到返回 None"""
        for name in LMEM_NAMES:
            try:
                star = self.context.get_registered_star(name)
            except Exception:
                star = None
            if not star:
                continue
            inst = getattr(star, "star_cls", None)
            if inst is not None:
                return inst
        return None

    async def _context_from_livingmemory(self, event, limit):
        inst = self._lmem()
        if inst is None:
            logger.warning("[状态卡·FULL] 没找到 livingmemory，上下文跳过")
            return None
        handler = getattr(inst, "event_handler", None)
        cm = getattr(handler, "conversation_manager", None)
        if cm is None:
            logger.warning("[状态卡·FULL] livingmemory 还没初始化好，上下文跳过")
            return None
        try:
            sid = event.unified_msg_origin
            msgs = await cm.get_context(session_id=sid, max_messages=limit)
            return msgs or None
        except Exception as e:
            logger.warning("[状态卡·FULL] 取 livingmemory 上下文失败: %s" % e)
            return None

    async def _context_from_astrbot(self, event, limit):
        try:
            import json as _json
            cm = self.context.conversation_manager
            umo = event.unified_msg_origin
            cid = await cm.get_curr_conversation_id(umo)
            if not cid:
                return None
            conv = await cm.get_conversation(umo, cid)
            if conv is None or not getattr(conv, "history", None):
                return None
            history = _json.loads(conv.history)
            return history[-limit:] or None
        except Exception as e:
            logger.warning("[状态卡·FULL] 取 AstrBot 上下文失败: %s" % e)
            return None

    async def _context(self, event):
        mode = self._text("context_mode", "off").lower()
        if mode == "off":
            return None
        try:
            limit = int(self._cfg("context_messages", 12))
        except Exception:
            limit = 12
        limit = max(1, min(limit, 60))

        if mode == "livingmemory":
            ctx = await self._context_from_livingmemory(event, limit)
        else:
            ctx = await self._context_from_astrbot(event, limit)

        if ctx is None:
            logger.info("[状态卡·FULL] 上下文为空，按无上下文点评")
        else:
            logger.info("[状态卡·FULL] 取到上下文 %d 条" % len(ctx))
        return ctx

    # ---------------- 点评 ----------------
    def _prompt(self, d, name, has_ctx, style_name=""):
        cfg = d["cfg"]
        body = (
            "CPU %d%%，内存 %d%%，进程 %d 个，已开机 %d 天，"
            "C 盘剩余 %.1fG，显卡 %s，心情值 %d，摸鱼值 %d。"
            % (d["cpu"], d["mem_percent"], d["proc_total"], d["up_days"],
               d["free_c"], cfg.get("gpu", "未知"), d["mood"], d["slack"])
        )
        tpl = self._tpl("prompt_comment", DEF_COMMENT_HINT)
        out = (tpl.replace("{NAME}", ("你现在扮演「%s」。" % name) if name else "")
                  .replace("{STYLE}", ("\n这张卡现在的风格方向是「%s」，顺口点一句这风格的味道，别写成设计分析。" % style_name) if style_name else "")
                  .replace("{CTX}", "\n下面是你们刚才的聊天，接着那个话头说下去，别复述原话。" if has_ctx else "")
                  .replace("{BODY}", body))
        if "{BODY}" not in tpl:
            out += "\n这台机器：%s" % body
        if "只输出" not in out:
            out += "\n只输出这句话。"
        return out

    async def _comment(self, event, d, style_name=""):
        """None = 走 persona.py 本地规则"""
        mode = self._text("comment_mode", "rules").lower()

        if mode == "off":
            return " "
        if mode == "fixed":
            return self._text("fixed_comment") or None
        if mode != "llm":
            return None

        name = self._text("persona_name")
        ctx = await self._context(event)
        prompt = self._prompt(d, name, bool(ctx), style_name)

        try:
            provider = self._prov("provider_comment", "点评")
            if provider is None:
                logger.warning("[状态卡·FULL] 没有可用模型，退回规则点评")
                return None
            sys_prompt = await self._persona_prompt()
            resp = await provider.text_chat(
                prompt=prompt,
                contexts=ctx or [],
                system_prompt=sys_prompt,
            )
            text = (getattr(resp, "completion_text", "") or "").strip()
            return text or None
        except Exception as e:
            logger.warning("[状态卡·FULL] LLM 点评失败(%s)，退回规则点评" % e)
            self._dump_exc("LLM点评", e, "已退回本地规则点评")
            return None

    async def _hero_assets(self, desc, tpl=None, limit=4):
        """模板里要立绘就按描述去素材里找，转成 data URI 交过去"""
        if not self._cfg("flow_palette", True):
            return []
        if NO_ASSET in (desc or ""):
            logger.info("[状态卡·FULL] 说了不使用素材，这张卡的立绘直接不给")
            return []
        if isinstance(tpl, str):
            # 模板可能写成 src="{{ hero }}"，也可能先 {% set img = hero %} 再用 {{ img }}，
            # 只认 "{{ hero" 这种字面串会漏掉后者，结果就是永远不给立绘
            _need = ("heroes" in tpl
                     or bool(re.search(r"\{\{[^}]*\bhero", tpl))
                     or bool(re.search(r"\{%[^%]*\bhero", tpl))
                     or bool(re.search(r"\bhero\b", tpl)))
            if not _need:
                return []
        desc = (desc or "").strip()
        try:
            from . import palette as _pal
            rows = []
            if FIXED_HERO:
                # 填了固定立绘就只认这一张，别人别想抢
                _r = await asyncio.to_thread(_pal.extract, FIXED_HERO)
                rows = [_r] if _r else []
                logger.info("[状态卡·FULL] 立绘固定为 %s" % FIXED_HERO)
            elif desc and self._cfg("flow_pick", True):
                # 复用收藏模板时也得让模型自己挑，别靠名字模糊匹配瞎撞
                from . import freedraw as _fdmod
                _fdmod.set_asset_dir(self._cfg("asset_dir", ""))
                _brief = _fdmod.asset_brief()
                if "一张图都没有" not in _brief:
                    _picks = await self._pick_assets(
                        self._prov("provider_pick", "挑图"), desc, "", (1020, 2019), _brief)
                    for _p in (_picks or []):
                        _r = await asyncio.to_thread(_pal.extract, str(_p.get("file") or ""))
                        if _r:
                            rows.append(_r)
                if not rows:
                    rows = await asyncio.to_thread(_pal.scan, desc, limit)
            elif desc:
                rows = await asyncio.to_thread(_pal.scan, desc, limit)
        except Exception as e:
            logger.warning("[状态卡·FULL] 立绘没找着，照常出图: %s" % e)
            return []
        assets = []
        for r in rows[:limit]:
            f = r.get("file")
            if not f:
                continue
            uri = await asyncio.to_thread(_pal.to_data_uri, f)
            if uri:
                assets.append({
                    "file": f, "uri": uri,
                    "name": os.path.splitext(os.path.basename(str(f)))[0].split(" (")[0]})
        if assets:
            logger.info("[状态卡·FULL] 网页立绘已内嵌 %d 张 · 共 %.0fKB"
                        % (len(assets), sum(len(a["uri"]) for a in assets) / 1024.0))
        return assets

    # ---------------- 指令 ----------------
    @filter.command("服务器状态卡", alias={"状态卡", "出状态卡"})
    async def make_card(self, event: AstrMessageEvent, arg: str = ""):
        """服务器状态卡：出一张主机状态图
        /服务器状态卡 随机 霓虹  从收藏夹里抽一个名字带霓虹的模板"""
        t0 = time.time()
        logger.info("[状态卡·FULL] %s 调用了 /状态卡" % self._whoami(event))
        try:
            raw = self._arg_text(event, arg)
            if raw in ("收藏夹", "收藏", "收藏列表"):
                yield event.plain_result(self._fav_list())
                return

            # 指令后面可能带了尺寸：/状态卡 9:16、/状态卡 随机 霓虹 1080x1920
            want = parse_canvas_text(raw)
            if want:
                raw = strip_canvas_text(raw)

            key = self._fav_key(event, arg) if raw else None
            fav = None
            if key is not None:
                fav, where = self._pick_template(key)
                if not fav:
                    yield event.plain_result(self._fav_miss(key))
                    return
                logger.info("[状态卡·FULL] 抽到%s模板 %s"
                            % (where, os.path.basename(fav)))

            from . import htmlcard
            use_html = self._renderer() == "html"
            html_tpl = None
            style, code_path, tag = None, None, ""
            if fav:
                tag = os.path.splitext(os.path.basename(fav))[0]
                kind = self._tpl_kind(fav)
                if kind == "html":
                    html_tpl = htmlcard.load_template(fav)
                    if html_tpl is None:
                        fav, tag = None, ""
                    else:
                        use_html = True
                elif kind == "pil":
                    code_path = fav
                    use_html = False
                elif self._load_style_abs(fav):
                    style = tag
                else:
                    fav, tag = None, ""
            if not fav:
                style = await self._resolve_style()
                code_path = self._code_style_path()
                if code_path:
                    tag = os.path.splitext(os.path.basename(code_path))[0]
                    # 文件名前缀说了算：HTML_xxx.py 走浏览器，PIL_xxx.py 走代码
                    if self._tpl_kind(code_path) == "html":
                        html_tpl = htmlcard.load_template(code_path)
                        if html_tpl is not None:
                            use_html = True
                            code_path = None
                        else:
                            use_html = False
                    else:
                        use_html = False
                else:
                    tag = str(style or "")

            apply_config(self.config)
            data = await asyncio.to_thread(collect)
            await self._fill_metric_names(data, (arg or tag or "").strip())
            style_tag = re.sub(r"_\d{4}_\d{4}(_\d+)?$", "", tag) or "默认"
            # 前缀是给插件看的，点评里别念出来
            style_tag = re.sub(r"^(html|pil)[_\- ]+", "", style_tag, flags=re.I) or "默认"
            comment = await self._comment(event, data, style_name=style_tag)
            who = self._text("persona_name") or None

            out = os.path.join(OUT_DIR, "card_%d.png" % int(time.time() * 1000))
            if use_html:
                size = htmlcard.pick_size(want, html_tpl)
                # 模板自己写死了画布，就别再拿嘴里的「竖版」去套一遍
                if want and htmlcard.sniff_size(html_tpl):
                    logger.info("[状态卡·FULL] 模板固定 %dx%d，不用 %dx%d 套"
                                % (size[0], size[1], want[0], want[1]))
                    want = None
                hero_assets = await self._hero_assets(raw or tag, html_tpl, 1)
                cm = self._deco_comment(comment, size)
                await asyncio.to_thread(htmlcard.render, data, out,
                                        cm, who, html_tpl, size[1], size[0],
                                        hero_assets,
                                        settle=self._int("flow_settle", 400))
                logger.info("[状态卡·FULL] HTML 渲染 %s"
                            % (os.path.basename(fav) if fav else "内置版式"))
            elif code_path:
                with open(code_path, encoding="utf-8") as f:
                    code = f.read()
                freedraw.set_asset_dir(self._cfg("asset_dir", ""))
                freedraw.set_no_asset(NO_ASSET in (raw or ""))
                canvas = want or self._roll_canvas()
                on = self._meta_on()
                try:
                    await asyncio.to_thread(freedraw.run, code, data, out, comment,
                                            who, canvas, False, on)
                except Exception:
                    if not on:
                        raise
                    # 这个模板不认换行，去掉那行再跑，别为了尺寸把图搞没了
                    logger.warning("[状态卡·FULL] 模板吃不下换行，这版不带尺寸信息")
                    await asyncio.to_thread(freedraw.run, code, data, out, comment,
                                            who, canvas, False, False)
                logger.info("[状态卡·FULL] 用代码风格 %s 画完" % os.path.basename(code_path))
            else:
                await asyncio.to_thread(render, data, out, comment, who,
                                        meta_in_comment=self._meta_on())
                if not fav:
                    self._backup_style(style)

            logger.info("[状态卡·FULL] 出图 %.2fs -> %s" % (time.time() - t0, out))
            if want:
                await asyncio.to_thread(fit_to_canvas, out, want)

            yield event.image_result(out)
        except Exception as e:
            logger.error("[状态卡·FULL] 出图失败: %s" % e)
            yield event.plain_result(self._err_report("出图失败", e, "状态卡出图失败了：%s" % e))

    # ---------------- 让 AI 现写一版风格 ----------------
    _STYLE_HINT = """你在给一张服务器状态卡做整体设计，可以推翻重排，只输出 JSON，不要解释。

颜色都是 [r,g,b]，0-255 整数。

【背景】
bg_top / bg_bottom  ／ 渐变上下两个颜色
glows               光斑，最多 5 个，每项 [x比例, y比例, 半径, [r,g,b], 强度]
vignette            暗角强度 0~1
grid_step / grid_alpha  网格间距(像素，0 就关掉)和透明度(0-255)

【配色】
title / subtitle / label / text / sub / faint   各级文字颜色
cpu / mem / proc / cfg / disk_c / disk_d / comment   各板块主色
bar_top / bar_bottom   柱子渐变色
mood_good / mood_mid / mood_bad   状态值按分数自动换色
split_alpha            分隔线透明度
card_radius / bar_radius  卡片和柱子圆角

【字号】fonts 里 18 项，按需改：
title subtitle time kpi_label kpi axis bar_val proc section cfg_label cfg_value disk mood_label mood_num mood_unit mood_word comment

【版式】layout 是整个框架，布局由下面这些决定：
blocks   六块的顺序，可任意重排，可选值：header(页眉) kpi(三指标) chart(柱状图) config(硬件配置) disk(磁盘和状态值) comment(AI点评)
         例：["comment","header","kpi","disk","config","chart"] 就是把点评提到最上面
gaps     每块下面空多少像素，键同上
block_gap 没单独指定时的默认间距
top_margin 顶部留白
margin_x / right_x 左右留白
chart_max_h 柱子最高高度，压矮一点更紧凑
cfg_h / disk_h / comment_h 这三张卡的高度
mood_x / mood_slot 状态值那一排的起点和间距
sub_gap / kpi_gap 标题和数字跟上一行的距离

【状态值名字】metrics 形如 [{"name":"心情值"},{"name":"摸鱼值"},{"name":"赛博寿命"}]

要推翻重做就直接改 blocks 顺序、gaps、卡高、字号和配色，不要只换颜色。
"""

    def _clean_style(self, data):
        """把模型给的风格洗一遍，只认认识的键，返回 (干净数据, 提示)"""
        from . import theme as _theme
        base = _theme.snapshot()
        clean, notes = {}, []

        if not isinstance(data, dict):
            return {}, ["模型返回的不是对象"]

        for k, v in data.items():
            if k not in base:
                else_note = "忽略不认识的字段 %s" % k
                if len(notes) < 5:
                    notes.append(else_note)
                continue
            ref = base[k]

            # 颜色：三元素数字列表
            if (isinstance(ref, list) and len(ref) == 3
                    and all(isinstance(x, (int, float)) for x in ref)):
                if isinstance(v, (list, tuple)) and len(v) >= 3:
                    try:
                        clean[k] = [max(0, min(255, int(x))) for x in v[:3]]
                    except Exception:
                        notes.append("%s 颜色值不合法，已跳过" % k)
                continue

            # 子表：fonts / layout
            if isinstance(ref, dict):
                if not isinstance(v, dict):
                    notes.append("%s 不是对象，已跳过" % k)
                    continue
                sub = {}
                for kk, vv in v.items():
                    if kk not in ref:
                        continue
                    # 板块顺序
                    if kk == "blocks":
                        if isinstance(vv, list):
                            bs = [str(x) for x in vv if str(x) in BLOCK_NAMES]
                            if bs:
                                sub["blocks"] = bs
                        continue
                    # 板块间距表
                    if kk == "gaps":
                        if isinstance(vv, dict):
                            gs = {}
                            for a, b in vv.items():
                                if str(a) not in BLOCK_NAMES:
                                    continue
                                if isinstance(b, bool) or not isinstance(b, (int, float)):
                                    continue
                                gs[str(a)] = max(0, min(400, int(b)))
                            if gs:
                                sub["gaps"] = gs
                        continue
                    if isinstance(vv, bool) or not isinstance(vv, (int, float)):
                        continue
                    if kk in ("margin_x", "right_x", "header_y", "sub_gap",
                              "kpi_label_y", "kpi_gap", "chart_base_y",
                              "chart_max_h", "mood_x", "mood_slot", "width"):
                        vv = max(0, min(4000, int(vv)))
                    else:
                        vv = max(6, min(200, int(vv)))
                    if kk == "right_x" and kk in sub:
                        pass
                    sub[kk] = vv
                if sub:
                    clean[k] = sub
                continue

            # 列表：glows / metrics
            if isinstance(ref, list):
                if k == "metrics" and isinstance(v, list):
                    ms = []
                    for m in v[:3]:
                        if isinstance(m, dict) and str(m.get("name", "")).strip():
                            ms.append({"name": str(m["name"]).strip()[:10]})
                    if ms:
                        clean[k] = ms
                elif k == "glows" and isinstance(v, list):
                    gs = []
                    for g in v[:5]:
                        if isinstance(g, list) and len(g) >= 4:
                            try:
                                gs.append([
                                    float(g[0]), float(g[1]), float(g[2]),
                                    [max(0, min(255, int(x))) for x in g[3]][:3],
                                    float(g[4]) if len(g) > 4 else 0.2,
                                ])
                            except Exception:
                                continue
                    if gs:
                        clean[k] = gs
                continue

            # 数字
            if isinstance(ref, (int, float)) and not isinstance(v, bool):
                try:
                    clean[k] = float(v) if isinstance(ref, float) else int(v)
                except Exception:
                    notes.append("%s 不是数字，已跳过" % k)

        return clean, notes

    @filter.command("状态卡主题", alias={"生成卡片风格", "卡片风格"})
    async def gen_style(self, event: AstrMessageEvent):
        """状态卡主题 <描述>：让模型单独生成一版卡片风格"""
        logger.info("[状态卡·FULL] %s 调用了 /状态卡主题 · %s"
                    % (self._whoami(event), (desc or "无描述")[:40]))
        from . import theme as _theme

        if not self._cfg("style_ai_enabled", True):
            yield event.plain_result("配置里把「允许用指令让 AI 生成风格」关掉了")
            return

        raw = (event.message_str or "").strip()
        parts = raw.split(maxsplit=1)
        desc = parts[1].strip() if len(parts) > 1 else ""
        if not desc:
            yield event.plain_result("用法：/状态卡主题 赛博朋克紫，标题大一点")
            return

        provider = self.context.get_using_provider()
        if provider is None:
            yield event.plain_result("现在没有可用模型，先去配一个再试")
            return

        _theme.reset()
        current = _theme.to_dict()
        prompt = "%s\n当前配置：\n%s\n\n按这个要求改，没提到的保持原样：%s\n只输出完整 JSON。" % (
            self._tpl("prompt_style", self._STYLE_HINT),
            json.dumps(current, ensure_ascii=False),
            desc,
        )

        try:
            resp = await provider.text_chat(
                prompt=prompt,
                system_prompt="你是界面配色助手，只输出 JSON。",
            )
            text = (getattr(resp, "completion_text", "") or "").strip()
        except Exception as e:
            logger.warning("[状态卡·FULL] 生成风格调用失败: %s" % e)
            yield event.plain_result(self._err_report("风格生成失败", e, "模型调用失败了：%s" % e))
            return

        data = _extract_json(text)
        if not data:
            logger.warning("[状态卡·FULL] 模型没给合法 JSON: %s" % text[:200])
            yield event.plain_result("模型这次没返回合法 JSON，风格没保存，换个说法再试一次")
            return

        clean, notes = self._clean_style(data)
        if not clean:
            yield event.plain_result("模型给的内容一个能用的字段都没有，没保存")
            return

        name = "ai_" + datetime.datetime.now().strftime("%m%d_%H%M%S")
        os.makedirs(STYLES_DIR, exist_ok=True)
        path = os.path.join(STYLES_DIR, name + ".json")
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(clean, f, ensure_ascii=False, indent=2)
            self._archive_style(path)
            self._sync_style_options()
        except Exception as e:
            yield event.plain_result(self._err_report("风格写入失败", e, "风格文件写不进去：%s" % e))
            return

        # 用新风格出一张预览
        try:
            _theme.reset()
            _theme.apply_dict(clean)
            apply_config(self.config)
            data2 = await asyncio.to_thread(collect)
            out = os.path.join(OUT_DIR, "preview_%s.png" % name)
            await asyncio.to_thread(render, data2, out, None, None)
        except Exception as e:
            _theme.reset()
            logger.warning("[状态卡·FULL] 预览出图失败: %s" % e)
            yield event.plain_result("风格存好了（%s）\n%s"
                                     % (name, self._err_report("预览出图失败", e, "预览出图失败")))
            return
        finally:
            _theme.reset()

        tip = "已存成风格 %s，去配置里把「卡片风格名」填成 %s 就生效" % (name, name)
        if notes:
            tip += "（%s）" % "；".join(notes[:3])
        yield event.plain_result(tip)
        yield event.image_result(out)

    def _tpl(self, key, default):
        """设置面板里填了就用它，留空用内置的那份"""
        v = self._text(key, "")
        return v if (v or "").strip() else default

    _last_size = None

    async def _plan_card(self, provider, desc, mood, size):
        """第一步：让模型定尺寸、挑字段、改显示名，返回 dict 或 None"""
        req = []
        if (desc or "").strip():
            req.append("用户要求：%s" % desc.strip())
        if mood:
            req.append("视觉方向：%s" % mood)
        if not req:
            req.append("用户没提要求，随你发挥")
        if size:
            rule = "固定 %d x %d，不许改" % (int(size[0]), int(size[1]))
        else:
            pool = self._canvas_pool()
            rule = ("横版竖版都行，从这几个里挑最搭的一个："
                    + "、".join("%dx%d" % (w, h) for w, h in pool))

        if self._cfg("flow_base", True):
            base_rule = (
                "  - 骨架规矩：紧跟 <<<BASE>>> 之后给一段能直接跑通的 Python，"
                "用 Pillow 和现成的 _grad/_rr/_txt/_bar/_fit/_hex，不许 import，"
                "不许调不存在的函数\n"
                "  - 骨架里把背景、版面分区、每块的位置和留白定死，数字用占位就行，"
                "别写具体数值内容\n"
                "  - 骨架必须有 def draw(d, out_path, comment=None, who=None) 和 img.save(out_path)\n"
                "  - 代码里直接用 W H，它就是 JSON 里那个尺寸，两边别打架")
        else:
            base_rule = "  - 这次只要 JSON，不要写代码，不要给 <<<BASE>>>"
        ask = (self._tpl("prompt_plan", PLAN_HINT)
               .replace("{REQ}", "\n".join(req))
               .replace("{SIZE_RULE}", rule)
               .replace("{BASE_RULE}", base_rule))
        t = time.time()
        try:
            resp = await asyncio.wait_for(
                provider.text_chat(
                    prompt=ask,
                    system_prompt="你是卡片排版规划师，只输出 JSON，不要解释。",
                ), timeout=90)
        except Exception as e:
            logger.warning("[状态卡·FULL] 规划这步没拿到，直接写代码: %s" % e)
            return None

        text = getattr(resp, "completion_text", "") or ""
        head, sep, tail = text.partition("<<<BASE>>>")
        plan = _extract_json(head) or _extract_json(text)
        if not isinstance(plan, dict):
            logger.warning("[状态卡·FULL] 规划返回的不是 JSON，跳过")
            return None
        base_code = ""
        if sep and tail.strip():
            base_code = _extract_code(tail)
            if base_code and "def draw" in base_code:
                plan["base_code"] = base_code
            else:
                base_code = ""

        try:
            w, h = int(plan.get("width") or 0), int(plan.get("height") or 0)
            if 200 <= w <= 4000 and 200 <= h <= 4000:
                plan["width"], plan["height"] = w - w % 2, h - h % 2
            else:
                plan.pop("width", None)
                plan.pop("height", None)
        except Exception:
            plan.pop("width", None)
            plan.pop("height", None)

        logger.info("[状态卡·FULL] 规划完成 %.1fs · %sx%s · 骨架 %d 行 · %s"
                    % (time.time() - t, plan.get("width"), plan.get("height"),
                       len(base_code.split("\n")) if base_code else 0,
                       (str(plan.get("layout") or plan.get("palette") or ""))[:40]))
        return plan

    PICK_HINT = """你在给一张服务器状态卡挑素材图，只输出 JSON，不要解释，不要 markdown 围栏。

用户要求：{REQ}
{MOOD}
画布：{W}x{H}

素材清单（路径 尺寸 大小）：
{BRIEF}

规则（挑不到就交空数组，空数组是正常答案，不是失败）：
  - 每挑一张，都必须能一句话说出它跟用户输入的直接关系；说不出关系的，一张都不许拿
  - 默认只挑 1 张。只有用户明确要多个角色或多个元素时才挑 2 张以上，最多 3 张
  - 绝对不许为了「画面丰富」「构图平衡」「配色好看」「有总比没有强」去补图
  - 用户给的是抽象题材（世界和平、夏天、赛博朋克、期末考试）而素材里没有对得上的具体对象，就交空数组
  - 用户点名要某个具体的东西（某张贴纸、某个角色、某场比赛），就只挑那一个，别顺手把同文件夹的其它图也拿走
  - 同一个人的好几张图（换衣服、换角度）只挑一张最贴用户描述的
  - 挑的时候只看文件名和文件夹名判断，不需要看图；素材按人名/角色名建文件夹时，文件夹名算线索
  - 每张说清楚放哪、多大、怎么处理：横图别硬拉成竖版整幅，讲裁剪从哪切

输出：
{
  "picks": [
    {"file": "清单里一字不差的路径", "size": "6000x4000",
     "where": "放在画面上部 1080x1400", "how": "等比缩到宽 1080，顶部对齐，下部渐变淡出"}
  ],
  "note": "一句话说挑图思路，挑不到就写「没有对得上的素材」"
}"""

    async def _pick_assets(self, provider, desc, mood, size, brief):
        """单独一次调用：从清单里挑真正要用的图，返回 list，失败返回 None"""
        if provider is None:
            return None
        req = (desc or "").strip() or "用户没提具体要求，按机器状态卡的一般审美来"
        ask = (self.PICK_HINT
               .replace("{REQ}", req)
               .replace("{MOOD}", ("视觉方向：%s" % mood) if mood else "")
               .replace("{W}", str(size[0])).replace("{H}", str(size[1]))
               .replace("{BRIEF}", brief))
        t = time.time()
        try:
            resp = await asyncio.wait_for(
                provider.text_chat(prompt=ask,
                                   system_prompt="你是素材挑选助手，只输出 JSON。"),
                timeout=90)
        except Exception as e:
            logger.warning("[状态卡·FULL] 挑图这步没拿到: %s" % e)
            return None
        data = _extract_json(getattr(resp, "completion_text", "") or "")
        if not isinstance(data, dict):
            logger.warning("[状态卡·FULL] 挑图返回的不是 JSON，跳过")
            return None
        picks = data.get("picks")
        if not isinstance(picks, list):
            picks = []
        # 只认清单里真有的文件，防它编名字
        valid = set()
        for line in (brief or "").split("\n"):
            line = line.strip()
            if line and not line.startswith("（"):
                valid.add(line.split("    ")[0].strip())
        out = []
        for p in picks[:3]:
            if not isinstance(p, dict):
                continue
            fn = str(p.get("file") or "").strip()
            if fn and fn in valid:
                out.append(p)
        # 没明确要多个角色时，多出来的第二三张基本都是凑数的
        if len(out) > 1 and not _wants_multi(req):
            logger.info("[状态卡·FULL] 没要多个元素，%d 张砍到 1 张" % len(out))
            out = out[:1]
        logger.info("[状态卡·FULL] 挑图完成 %.1fs · 挑了 %d 张 %s"
                    % (time.time() - t, len(out),
                       ("| " + ", ".join(os.path.basename(str(p.get("file"))) for p in out))[:80] if out else ""))
        return out

    METRIC_HINT = """给一张服务器状态卡起三个状态条的名字，只输出 JSON，不要解释。

这张卡的主题 / 场景：{REQ}
风格：{MOOD}

要求：
  - 三个名字必须贴这次的题材和聊天氛围，不许用「心情值」「摸鱼值」「赛博寿命」这种默认词
  - 举例：通缉题材 → 通缉程度 / 逃亡进度 / 招供意愿
          群聊开撕 → 累麻指数 / 辱骂浓度 / 心累值
          科技公司 → 压榨指数 / 画饼浓度 / 秃头进度
          末日题材 → 苟活天数 / 变异风险 / 物资余量
  - 每个 3~5 个汉字，不带标点，不解释，三个之间风格统一
  - 一个都想不出来就交空数组

输出：
{"names": ["第一个", "第二个", "第三个"]}"""

    PALETTE_HINT = """你在给一张服务器状态卡定配色，只输出 JSON，不要解释，不要 markdown 围栏。

用户要求：{REQ}
{MOOD}

素材色板（已经从立绘的实心像素里抽好了，是原色，不许改色值）：
{BRIEF}

规则：
  - 只用上面确实对得上的角色，能用一张就别用两张，别为了画面热闹把不相干的角色拉进来
  - 一张都对不上就别编，roles 交空数组
  - 每个角色：主色调 1 个（hex 带占比）、副色调 2 个（hex 带占比）、点缀色 3 个（只写 hex，不写占比）
  - 占比照抄上面的数据，别自己编，也不要求凑满 100%
  - hex 只能从这张图自己的色板里挑，不许凭空造色，也不许自动提饱和度
  - 同一个角色有多张立绘（_1 / skin1 / skin2）时只选一张，别把同一个角色当成两个人
  - 多个角色各用各的色系。一冷一暖就写成对比配色，布局分左右或上下，别硬凑成一个色系
  - bg 看整体明度：亮的走 light（浅底），暗的走 dark（深底）
  - layout 只能填 left_right / top_bottom / mixed

输出：
{
  "roles": [
    {"file": "上面一字不差的路径", "name": "角色名",
     "main": {"hex": "#RRGGBB", "share": 50},
     "sub": [{"hex": "#RRGGBB", "share": 20}, {"hex": "#RRGGBB", "share": 20}],
     "accent": ["#RRGGBB", "#RRGGBB", "#RRGGBB"]}
  ],
  "bg": "light",
  "layout": "left_right",
  "note": "一句话说配色思路"
}"""

    async def _metric_names(self, provider, desc, mood=""):
        """多调一次模型：按主题给三个状态条起名，失败返回空"""
        if provider is None:
            return []
        ask = (self.METRIC_HINT
               .replace("{REQ}", (desc or "").strip() or "服务器状态卡，没给主题")
               .replace("{MOOD}", ("视觉方向：%s" % mood) if mood else ""))
        try:
            resp = await asyncio.wait_for(
                provider.text_chat(prompt=ask, system_prompt="你只输出 JSON。"),
                timeout=45)
        except Exception as e:
            logger.warning("[状态卡·FULL] 指标名这步没拿到: %s" % e)
            return []
        data = _extract_json(getattr(resp, "completion_text", "") or "")
        if not isinstance(data, dict):
            return []
        names = []
        for x in (data.get("names") or [])[:3]:
            t = str(x or "").strip()
            if t and len(t) <= 8:
                names.append(t)
        if len(names) == 3:
            logger.info("[状态卡·FULL] 这轮指标名：%s" % " / ".join(names))
            return names
        return []

    async def _fill_metric_names(self, data, desc, mood=""):
        """给上一层的 data 补三个指标名，失败就算了，不影响出图"""
        if not self._cfg("flow_metric", True):
            return
        try:
            names = await self._metric_names(
                self._prov("provider_metric", "指标名") or self._prov("provider_pick", "挑图"),
                desc, mood)
            if names:
                data["metric_names"] = names
        except Exception as e:
            logger.warning("[状态卡·FULL] 指标名没填上: %s" % e)

    async def _palette_plan(self, provider, desc, mood, rows):
        """单独一次调用：拿抽好的色板定每个角色的配色，失败返回 None"""
        if provider is None or not rows:
            return None
        from . import palette as _pal
        req = (desc or "").strip() or "用户没提具体要求，按素材本身的气色配"
        ask = (self.PALETTE_HINT
               .replace("{REQ}", req)
               .replace("{MOOD}", ("视觉方向：%s" % mood) if mood else "")
               .replace("{BRIEF}", _pal.brief(rows)))
        t = time.time()
        try:
            resp = await asyncio.wait_for(
                provider.text_chat(prompt=ask,
                                   system_prompt="你是状态卡的配色助手，只输出 JSON。"),
                timeout=90)
        except Exception as e:
            logger.warning("[状态卡·FULL] 取色判断这步没拿到: %s" % e)
            return None
        data = _extract_json(getattr(resp, "completion_text", "") or "")
        if not isinstance(data, dict):
            logger.warning("[状态卡·FULL] 取色返回的不是 JSON，这轮不上配色骨架")
            return None

        # 只认抽到色的那几张图，颜色必须是六位 hex，别的一律丢掉
        valid = {r.get("file"): r for r in rows}
        roles = []
        for r in (data.get("roles") or []):
            if not isinstance(r, dict):
                continue
            fn = str(r.get("file") or "").strip().replace("\\", "/")
            if fn not in valid:
                continue
            main = r.get("main") or {}
            mh = _ok_hex(main.get("hex") if isinstance(main, dict) else main)
            if not mh:
                continue
            try:
                mshare = float(main.get("share")) if isinstance(main, dict) else 0
            except Exception:
                mshare = 0
            subs = []
            for s in (r.get("sub") or [])[:2]:
                sh = _ok_hex(s.get("hex") if isinstance(s, dict) else s)
                if not sh:
                    continue
                try:
                    ss = float(s.get("share")) if isinstance(s, dict) else 0
                except Exception:
                    ss = 0
                subs.append({"hex": sh, "share": ss})
            acc = [_ok_hex(a) for a in (r.get("accent") or [])[:3]]
            acc = [a for a in acc if a]
            if not subs and not acc:
                continue
            roles.append({"file": fn, "name": str(r.get("name") or "")[:24],
                          "main": {"hex": mh, "share": mshare},
                          "sub": subs, "accent": acc,
                          "light": valid[fn].get("light"),
                          "colors": valid[fn].get("colors")})
        if not roles:
            logger.warning("[状态卡·FULL] 取色结果里没有一个能用的角色，跳过配色骨架")
            return None
        plan = {
            "roles": roles,
            "bg": ("dark" if str(data.get("bg") or "").lower().startswith("d") else "light"),
            "layout": str(data.get("layout") or "").strip() or "left_right",
            "note": str(data.get("note") or "")[:80],
        }
        logger.info("[状态卡·FULL] 取色完成 %.1fs · %d 个角色 · %s · %s"
                    % (time.time() - t, len(roles), plan["bg"], plan["layout"]))
        for r in roles:
            logger.info("[状态卡·FULL]   配色 %s 主 %s %s%% 副 %s 点缀 %s"
                        % (os.path.basename(r["file"])[:24], r["main"]["hex"],
                           r["main"]["share"],
                           ",".join("%s %s%%" % (s["hex"], s["share"]) for s in r["sub"]),
                           ",".join(r["accent"])))
        return plan

    def _palette_vars(self, plan):
        """整理成模板能直接取的结构：PALETTE['main'] / pal('accent', 0, 1)"""
        if not plan:
            return None
        roles = []
        for r in plan.get("roles") or []:
            roles.append({
                "name": r.get("name") or "",
                "file": r.get("file") or "",
                "main": (r.get("main") or {}).get("hex"),
                "sub": [s.get("hex") for s in (r.get("sub") or []) if s.get("hex")],
                "accent": list(r.get("accent") or []),
            })
        if not roles:
            return None
        first = roles[0]
        return {
            "roles": roles,
            "main": first.get("main"),
            "sub": first.get("sub") or [],
            "accent": first.get("accent") or [],
            "bg": plan.get("bg") or "light",
            "layout": plan.get("layout") or "left_right",
            "note": plan.get("note") or "",
        }

    def _palette_text(self, plan):
        """写进提示词的那段配色骨架"""
        if not plan:
            return ""
        rows = []
        for i, r in enumerate(plan.get("roles") or [], 1):
            main = r.get("main") or {}
            seg = "  角色%d %s：主色 %s %s%%" % (
                i, (r.get("name") or "").strip() or "未命名",
                main.get("hex"), ("%.0f" % float(main.get("share") or 0)))
            subs = " / ".join("%s %s%%" % (x["hex"], "%.0f" % float(x.get("share") or 0))
                              for x in (r.get("sub") or []))
            if subs:
                seg += "，副色 " + subs
            if r.get("accent"):
                seg += "，点缀 " + " ".join(r["accent"])
            seg += "\n      素材：%s" % r.get("file")
            rows.append(seg)
        head = ("\n配色骨架（从立绘里抽出来的，主色副色点缀只能从里面取，"
                "别自己编色，也别改饱和度）：\n")
        tail = ("  这几张立绘必须真的出现在画面里，不能只拿来取色\n"
                "  底色走%s（%s），多角色分区 %s\n"
                % ("深色" if plan.get("bg") == "dark" else "浅色",
                   plan.get("bg"), plan.get("layout")))
        note = ("  配色思路：%s\n" % plan.get("note")) if plan.get("note") else ""
        return head + "\n".join(rows) + "\n" + tail + note

    async def _make_code(self, event, desc="", mood=""):
        """让模型写一版绘图代码，跑通就返回 (code, 图片路径, 错误)

        点评和写代码互不依赖，直接同时跑，省掉一半等待。
        """
        t0 = time.time()
        # 描述里带了尺寸（9:16、1080x1920、竖版）就用它，剩下的才是风格描述
        want = parse_canvas_text(desc)
        if want:
            desc = strip_canvas_text(desc)
        # 指令里点 奇葩比例 / 随机内置 也认，认完从描述里抠掉，别让它污染风格
        force = None
        for w in _CANVAS_WEIRD_MODE:
            if w in desc:
                force = "weird"
                desc = re.sub(r"(奇葩|离谱|怪|歪)(的)?(比例|尺寸|画布)?|weird|absurd", " ", desc, flags=re.I)
                break
        if force is None:
            for w in _CANVAS_BUILTIN_MODE:
                if w in desc:
                    force = "builtin"
                    desc = desc.replace(w, " ")
                    break
        if force:
            desc = " ".join(desc.split())

        # 画布：指令里写了听指令；配置写的是随机模式就当场摇；没设就交给①去挑
        mode, cfg_size = self._canvas_mode()
        if want:
            size = want
        elif force == "weird":
            size, mode = weird_canvas(), "weird"
        elif force == "builtin":
            size, mode = random.choice(_BUILTIN_CANVAS), "builtin"
        elif mode == "fixed" and cfg_size == (1600, 1200):
            size = None
        else:
            size = self._roll_canvas()
        provider = self._prov("provider_code", "绘图代码") or self._code_provider()
        if provider is None:
            return None, "", "没有可用模型，检查配置里的「生成用模型」"

        # 第一步：先让模型规划尺寸、要显示的字段、字段改叫什么
        plan = await self._plan_card(self._prov("provider_plan", "规划") or provider, desc, mood, size)
        if size is None and plan and plan.get("width") and plan.get("height"):
            size = (plan["width"], plan["height"])
        if not size:
            size = (1600, 1200)
        logger.info("[状态卡·FULL] 本轮画布 %dx%d（%s）" % (size[0], size[1], mode))
        self._last_size = size

        apply_config(self.config)
        data = await asyncio.to_thread(collect)
        await self._fill_metric_names(data, desc, mood)
        t_collect = time.time() - t0

        async def _get_comment():
            try:
                return await asyncio.wait_for(
                    self._comment(event, data, style_name=(mood or desc or "").strip()),
                    timeout=45)
            except Exception as e:
                logger.warning("[状态卡·FULL] 点评没拿到，先画图: %s" % e)
                return None

        comment_task = asyncio.create_task(_get_comment())
        who = self._text("persona_name") or None

        freedraw.set_asset_dir(self._cfg("asset_dir", ""))
        freedraw.set_prelude(self._text("flow_prelude", ""))
        # 第二步：单独一次调用，从素材清单里挑真正要用的图（配置里可以关掉省一次）
        brief = freedraw.asset_brief()
        picks = None
        no_asset = NO_ASSET in (desc or "")
        if no_asset:
            desc = (desc or "").replace(NO_ASSET, "").strip()
            logger.info("[状态卡·FULL] 说了不使用素材，代码这条路也不挑图")
        freedraw.set_no_asset(no_asset)
        if self._cfg("flow_pick", True) and not no_asset and "一张图都没有" not in brief:
            picks = await self._pick_assets(self._prov("provider_pick", "挑图"),
                                            desc, mood, size, brief)
        elif "一张图都没有" not in brief:
            logger.info("[状态卡·FULL] 配置里关掉了挑图，这轮不调第二次")

        # 第三步：拿立绘抽色，定一份配色骨架（配置里关掉就一次都不调）
        pal_plan = None
        pal_rows = []
        if self._cfg("flow_palette", True):
            from . import palette as _palmod
            try:
                # 模型挑到了就用它挑的，别再拿名字去模糊匹配，不然「通缉令」这种
                # 里面的单字能把完全不相干的立绘（令）拽进来
                if picks:
                    for _p in picks:
                        _r = _palmod.extract(str(_p.get("file") or ""))
                        if _r:
                            pal_rows.append(_r)
                elif (desc or "").strip() and not no_asset:
                    pal_rows = _palmod.scan(desc, limit=8)
                if pal_rows:
                    pal_plan = await self._palette_plan(
                        self._prov("provider_palette", "取色判断"), desc, mood, pal_rows[:8])
            except Exception as _e:
                logger.warning("[状态卡·FULL] 取色这步出了问题，继续出图: %s" % _e)

        # 取色点到的立绘必须真的画进画面，不然模型只拿了配色不画人
        if pal_rows:
            need = [x.get("file") for x in (pal_plan or {}).get("roles") or [] if x.get("file")]
            if not need:
                need = [pal_rows[0].get("file")]
            meta_by_file = {r.get("file"): r for r in pal_rows}
            have = {str(_x.get("file") or "") for _x in (picks or [])}
            for _f in need:
                if not _f or _f in have:
                    continue
                _m = meta_by_file.get(_f) or {}
                picks = (picks or []) + [{
                    "file": _f,
                    "w": _m.get("w"), "h": _m.get("h"),
                    "size": "%dx%d" % (_m.get("w") or 0, _m.get("h") or 0),
                    "where": "画面主体，竖版占上半身到腰，横版贴一侧",
                    "how": "透明底立绘，等比缩放后直接叠上去，别拉变形，别裁脸",
                }]
                have.add(_f)
            logger.info("[状态卡·FULL] 取色点到的立绘已并入必用素材：%s"
                        % ", ".join(os.path.basename(str(x)) for x in need)[:80])

        prompt = freedraw.hint_for(*size,
                                   template=self._tpl("prompt_code", freedraw.HINT),
                                   assets=picks)
        if no_asset:
            # 说了不使用素材，连素材清单都不给它看，免得它手痒调 asset
            prompt = ("这张卡不使用任何图片素材：不要调用 asset / asset_list，"
                      "不要画任何照片、立绘或者图标位，整张图全靠色块和文字。\n" + prompt)
        if pal_plan:
            prompt += self._palette_text(pal_plan)
        if picks:
            prompt += ("\n这次就这几张成品，按下面的位置处理直接摆，别再翻别的文件：\n")
            for p in picks:
                prompt += ("  %s → %s；%s\n"
                           % (p.get("file"), p.get("where") or "", p.get("how") or ""))
        prompt_core = prompt
        base_code = ((plan or {}).get("base_code") or "").strip()
        if plan:
            show = {k: v for k, v in plan.items() if k != "base_code"}
            prompt += ("\n这次的排版规划，板块、字段名、显示名、单位都按它来，别自己另起一套：\n%s\n"
                       % json.dumps(show, ensure_ascii=False, indent=1))
        if base_code:
            prompt += ("\n底稿已经搭好了，在它上面把内容和细节补完。版面结构、分区块的位置、"
                       "留白都别推翻，只填东西和修毛病：\n```python\n%s\n```\n" % base_code)
        if mood:
            prompt += "\n这次的视觉方向：%s\n排版、位置、留白全按这个方向来，别套默认模板。\n" % mood
        if (desc or "").strip():
            prompt += "\n额外要求：%s\n" % desc.strip()

        # 超时之后再拿同样大的提示去撞，大概率还是超时，备一份不放底稿的精简版
        prompt_slim = prompt_core + "\n这次不给底稿，从零写一份完整、但别太长的 draw。"
        if (desc or "").strip():
            prompt_slim += "\n额外要求：%s\n" % desc.strip()

        pal_vars = self._palette_vars(pal_plan)
        if pal_vars:
            logger.info("[状态卡·FULL] 配色骨架已注入模板：%s 个角色，底色 %s"
                        % (len(pal_vars["roles"]), pal_vars["bg"]))

        code, last_err, out = None, "", ""
        slim = False
        last_exc = None
        gen_timeout = self._int("gen_timeout", 300)
        t_code = 0.0
        tries = max(1, min(4, self._int("flow_retry", 2)))
        for attempt in range(tries):
            try:
                body = prompt_slim if slim else prompt
                ask = body if not last_err else (
                    body + "\n上次这版跑挂了：%s\n按报错改好，发一版完整代码。" % last_err)
                tc = time.time()
                resp = await asyncio.wait_for(
                    provider.text_chat(
                        prompt=ask,
                        system_prompt="你是 Python + Pillow 绘图工程师，只输出代码，不要解释。",
                    ), timeout=gen_timeout)
                t_code += time.time() - tc
                code = _extract_code(getattr(resp, "completion_text", "") or "")

                # 点评还没回来就再等一小会，最多 6 秒，不拖住出图
                comment = None
                if comment_task.done():
                    comment = comment_task.result()
                else:
                    try:
                        comment = await asyncio.wait_for(asyncio.shield(comment_task), timeout=6)
                    except Exception:
                        comment = None

                out = os.path.join(OUT_DIR, "free_%d.png" % int(time.time() * 1000))
                freedraw.set_asset_dir(self._cfg("asset_dir", ""))
                freedraw.set_no_asset(NO_ASSET in (desc or ""))
                on = self._meta_on()
                try:
                    await asyncio.to_thread(freedraw.run, code, data, out, comment, who,
                                            size, True, on, pal_vars)
                except Exception:
                    if not on:
                        raise
                    logger.warning("[状态卡·FULL] 这版模板吃不下换行，不带尺寸信息重跑")
                    await asyncio.to_thread(freedraw.run, code, data, out, comment, who,
                                            size, True, False, pal_vars)
                logger.info("[状态卡·FULL] 第 %d 版跑通 · 采集 %.1fs · 模型 %.1fs · 总 %.1fs"
                            % (attempt + 1, t_collect, t_code, time.time() - t0))
                return code, out, ""
            except Exception as e:
                last_exc = e
                last_err = ("%s: %s" % (type(e).__name__, e)).strip()
                if last_err.endswith(":") or last_err == "TimeoutError:":
                    last_err = "%s（等了 %d 秒没出结果，去配置里把生成时限调大）" % (
                        type(e).__name__, gen_timeout)
                if isinstance(e, asyncio.TimeoutError) and attempt + 1 < tries:
                    slim = True
                    logger.warning("[状态卡·FULL] 这版超时了，下一版换精简提示，不带底稿")
                code = None
                logger.warning("[状态卡·FULL] 第 %d 版没跑通（已用 %.1fs）: %s"
                               % (attempt + 1, time.time() - t0, last_err))
                self._dump_exc("代码生成_第%d版" % (attempt + 1), e,
                               "第 %d 版 · 模型 %.1fs" % (attempt + 1, t_code))

        if not comment_task.done():
            comment_task.cancel()
        logger.warning("[状态卡·FULL] %d 版都没跑通，总耗时 %.1fs" % (tries, time.time() - t0))
        detail = (("%d 版都没跑通，最后一个是 %s" % (tries, last_err)) if last_err
                  else "%d 版都没跑通" % tries)
        return None, "", self._err_report("自由生成失败", last_exc, detail)

    async def _make_html_code(self, event, desc="", mood=""):
        """让模型写一版 HTML 模板，截出图就返回 (code, 图片路径, 错误)"""
        from . import htmlcard
        t0 = time.time()
        provider = self._prov("provider_html", "HTML") or self._code_provider()
        if provider is None:
            return None, "", "没有可用模型，检查配置里的「生成用模型」"

        apply_config(self.config)
        data = await asyncio.to_thread(collect)
        await self._fill_metric_names(data, desc, mood)

        async def _get_comment():
            try:
                return await asyncio.wait_for(
                    self._comment(event, data, style_name=(mood or desc or "网页").strip()),
                    timeout=45)
            except Exception as e:
                logger.warning("[状态卡·FULL] 点评没拿到，先截图: %s" % e)
                return None

        comment_task = asyncio.create_task(_get_comment())
        who = self._text("persona_name") or None

        # 「不使用素材」这五个字连在一起，这张卡就一张图都不许上
        no_asset = NO_ASSET in (desc or "")
        if no_asset:
            desc = re.sub(r"[（(]\s*[)）]", "", (desc or "").replace(NO_ASSET, "")).strip()
            logger.info("[状态卡·FULL] 用户说了不使用素材，这轮不挑图不内嵌")

        # 没给方向就自己抽一个，不然模型每次画出来都一个模子
        if not (mood or "").strip() and not (desc or "").strip():
            mood = random.choice(self._moods())

        sw, sh = self._roll_canvas()
        # 用户嘴里说了竖版/横版就按他说的来，别再随机
        forced = htmlcard.detect_size(desc, mood, self._text("style_desc", ""))
        if forced:
            sw, sh = forced
        # 基底（尺寸、排版、整个框架的技术规格）最后再拼进去，免得被前面的内容压掉
        base_prompt = (self._tpl("prompt_html", htmlcard.HINT)
                       .replace("{W}", str(sw)).replace("{H}", str(sh)))
        # 拼装顺序：用户输入 → 素材配色 → 人格 → 基底
        prompt = ""
        if (desc or "").strip():
            prompt += "这次要做的主题：%s\n" % desc.strip()
        if no_asset:
            prompt += ("这张卡不使用任何图片素材：不要写 img、不要用 hero 变量、"
                       "也不许留一块空的立绘区，全靠排版和图形撑起来\n")
        if mood:
            prompt += ("这次的视觉方向：%s\n排版、配色、留白都按这个方向来，"
                       "别套默认那套深色仪表盘\n" % mood)
        prompt += "\n"
        # 立绘：抽色定配色骨架 + 转成 data URI 交给模板（关掉就整段跳过）
        hero_assets = []
        if self._cfg("flow_palette", True) and not no_asset and (desc or "").strip():
            try:
                from . import palette as _palmod
                # 先让模型在素材清单里挑（多调一次），挑不到才退回名字匹配
                _targets = []
                if FIXED_HERO:
                    _targets = [FIXED_HERO]
                    logger.info("[状态卡·FULL] 立绘固定为 %s" % FIXED_HERO)
                elif self._cfg("flow_pick", True):
                    from . import freedraw as _fdmod
                    _brief = _fdmod.asset_brief()
                    if "一张图都没有" not in _brief:
                        _picks = await self._pick_assets(
                            self._prov("provider_pick", "挑图"), desc, mood, (sw, sh), _brief)
                        _targets = [str(_p.get("file") or "") for _p in (_picks or [])]
                if not _targets and FIXED_HERO:
                    _targets = [FIXED_HERO]
                    logger.info("[状态卡·FULL] 用代码里写死的立绘：%s" % FIXED_HERO)
                if not _targets:
                    _targets = [x[0] for x in _palmod.hits(desc, limit=8)]
                    if _targets:
                        logger.info("[状态卡·FULL] 模型没挑，退回名字匹配：%s"
                                    % "、".join(os.path.basename(t) for t in _targets[:3]))
                _rows = []
                for _f in _targets[:4]:
                    _r = _palmod.extract(str(_f))
                    if _r:
                        _rows.append(_r)
                _plan = None
                if _rows:
                    _plan = await self._palette_plan(
                        self._prov("provider_palette", "取色判断"), desc, mood, _rows[:8])
                    if _plan:
                        prompt += self._palette_text(_plan)
                files = [x.get("file") for x in (_plan or {}).get("roles") or [] if x.get("file")]
                if not files and _rows:
                    files = [_rows[0].get("file")]
                names = [x.get("name") for x in (_plan or {}).get("roles") or []]
                for _i, _f in enumerate(files[:4]):
                    _uri = await asyncio.to_thread(_palmod.to_data_uri, _f)
                    if _uri:
                        hero_assets.append({
                            "file": _f, "uri": _uri,
                            "name": (names[_i] if _i < len(names) and names[_i] else
                                     os.path.splitext(os.path.basename(str(_f)))[0])})
                if hero_assets:
                    logger.info("[状态卡·FULL] 网页立绘已内嵌 %d 张 · 共 %.0fKB"
                                % (len(hero_assets),
                                   sum(len(a["uri"]) for a in hero_assets) / 1024.0))
                    prompt += "\n这次真的有立绘，图已经转好了，直接用：\n"
                    prompt += "  第一张写 <img src=\"{{ hero }}\">\n"
                    for _i, _a in enumerate(hero_assets):
                        prompt += ("  heroes[%d] → %s（%s）\n"
                                   % (_i, _a["name"], os.path.basename(str(_a["file"]))))
                    prompt += ("  立绘必须真的出现在画面里，别只当配色参考；"
                               "要前后关系就叠两层，后层放大加 blur 当投影，前层清晰\n")
            except Exception as _e:
                logger.warning("[状态卡·FULL] 网页这版立绘没取成，照常出图: %s" % _e)
        # 人格：配色、字体气质、装饰元素、留白节奏都按这个人的审美，不是让他扮演说话
        try:
            _ptxt = (await self._persona_prompt()) or ""
        except Exception as _e:
            logger.warning("[状态卡·FULL] 取人格给网页这版用失败: %s" % _e)
            _ptxt = ""
        if _ptxt.strip():
            _ptxt = _ptxt.strip()[:2500]
            prompt += ("\n【下面这段人设只用来给这张卡定美术调性：配色、字体气质、装饰元素、"
                       "留白节奏都照这个人的审美。不是让你扮演他说话，里面关于聊天怎么说的规矩不用执行】\n"
                       "%s\n【人设到此为止，继续做图】\n" % _ptxt)
            logger.info("[状态卡·FULL] 网页提示词已带上人格，%d 字" % len(_ptxt))
        prompt += "\n以下是最低技术要求，主题和人格调性优先于任何默认审美：\n" + base_prompt

        code, last_err, out = None, "", ""
        last_exc = None
        gen_timeout = self._int("gen_timeout", 300)
        t_code = 0.0
        for attempt in range(2):
            try:
                ask = prompt if not last_err else (
                    prompt + "\n上一版没跑通：%s\n按这个错误改好，发一版完整的。" % last_err)
                tc = time.time()
                resp = await asyncio.wait_for(
                    provider.text_chat(
                        prompt=ask,
                        system_prompt="你是 HTML + CSS 界面工程师，只输出一个 Python 文件的内容，不要解释。",
                    ), timeout=gen_timeout)
                t_code += time.time() - tc
                code = _extract_html_code(getattr(resp, "completion_text", "") or "")
                if not code:
                    raise ValueError("模型没给出带 TEMPLATE 的代码")

                tpl = htmlcard.template_from_code(code)
                if not tpl:
                    raise ValueError("代码里没找到 TEMPLATE 字符串")

                comment = None
                if comment_task.done():
                    comment = comment_task.result()
                else:
                    try:
                        comment = await asyncio.wait_for(asyncio.shield(comment_task), timeout=6)
                    except Exception:
                        comment = None

                out = os.path.join(OUT_DIR, "freehtml_%d.png" % int(time.time() * 1000))
                await asyncio.to_thread(htmlcard.render, data, out, comment, who, tpl,
                                        sh, sw, hero_assets,
                                        settle=self._int("flow_settle", 400))
                if htmlcard.looks_blank(out):
                    raise RuntimeError("截出来整张是空的，内容大概跑到画布外面了")
                logger.info("[状态卡·FULL] 第 %d 版网页跑通 · 模型 %.1fs · 总 %.1fs"
                            % (attempt + 1, t_code, time.time() - t0))
                return code, out, ""
            except Exception as e:
                last_exc = e
                last_err = ("%s: %s" % (type(e).__name__, e)).strip()
                if last_err.endswith(":") or last_err == "TimeoutError:":
                    last_err = "%s（等了 %d 秒没出结果，去配置里把生成时限调大）" % (
                        type(e).__name__, gen_timeout)
                code = None
                logger.warning("[状态卡·FULL] 第 %d 版网页没跑通（已用 %.1fs）: %s"
                               % (attempt + 1, time.time() - t0, last_err))
                self._dump_exc("网页生成_第%d版" % (attempt + 1), e, last_err)

        if not comment_task.done():
            comment_task.cancel()
        logger.warning("[状态卡·FULL] 网页两版都没跑通，总耗时 %.1fs" % (time.time() - t0))
        detail = ("两版都没跑通，最后一个是 %s" % last_err) if last_err else "两版都没跑通"
        return None, "", self._err_report("网页生成失败", last_exc, detail)

    def _save_code(self, code, prefix="code", label="", kind="pil", size=None):
        """存成人类看得懂的名字：渲染器前缀 + 风格名 + 短时间戳

        插件靠文件名前缀认渲染器，所以存下来就得带上：
        PIL_xxx.py 走代码绘图，HTML_xxx.py 走浏览器
        """
        tag = re.sub(r"[^\w\-\u4e00-\u9fa5]", "", str(label or ""))[:12]
        stamp = datetime.datetime.now().strftime("%m%d_%H%M")
        pre = "HTML_" if str(kind).lower() == "html" else "PIL_"
        base = "%s_%s" % (tag or prefix, stamp)
        if not base.upper().startswith(("PIL_", "HTML_")):
            base = pre + base
        name = base
        path = os.path.join(STYLES_DIR, name + ".py")
        n = 2
        while os.path.exists(path):
            name = "%s_%d" % (base, n)
            path = os.path.join(STYLES_DIR, name + ".py")
            n += 1

        # 图片留在素材文件夹，模板里只留相对路径，换台机器也能找到图
        try:
            code = re.sub(r'"([A-Za-z]:[^"\n]*?)(?:素材|assets)[\\/]([^"\n]*)"', r'"\2"', code)
            code = re.sub(r"'([A-Za-z]:[^'\n]*?)(?:素材|assets)[\\/]([^'\n]*)'", r"'\2'", code)
        except Exception:
            pass

        # 把画布尺寸写进模板本身，收藏后换配置也不会变形
        if str(kind).lower() != "html" and size:
            has_size = re.search(r"^\s*(SIZE|CANVAS|SIZE_PX)\s*=", code, re.M) or re.search(r"^\s*W\s*=", code, re.M)
            if not has_size:
                code = "SIZE = (%d, %d)\n" % (int(size[0]), int(size[1])) + code

        try:
            os.makedirs(STYLES_DIR, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(code)
            self._archive_style(path)
            self._sync_style_options()
        except Exception as e:
            logger.warning("[状态卡·FULL] 代码保存失败: %s" % e)
        return name

    @filter.command("状态卡重做风格", alias={"重做卡片风格", "随机风格", "换个风格"})
    async def regen_style(self, event: AstrMessageEvent, desc: str = ""):
        """状态卡重做风格：随机抽个方向，让 AI 重新画一张"""
        desc = self._arg_text(event, desc)
        mood = random.choice(self._moods())
        t0 = time.time()
        logger.info("[状态卡·FULL] %s 调用了 /状态卡重做风格 · 抽到方向：%s"
                    % (self._whoami(event), mood))

        code, out, err = await self._make_code(event, desc, mood)
        if not code:
            yield event.plain_result("两版都没跑通：%s" % err)
            return

        try:
            name = self._save_code(code, "code", label=mood, size=getattr(self, "_last_size", None))
        except Exception as e:
            logger.warning("[状态卡·FULL] 模板存档挂了，图照发: %s" % e)
            name = "未存档"
        yield event.plain_result(
            "方向：%s\n跑通了，存成 %s.py，用了 %.1fs"
            % (mood, name, time.time() - t0))
        yield event.image_result(out)

    @filter.command("状态卡自由生成", alias={"自由生成", "代码生成卡片", "状态卡代码"})
    async def gen_code(self, event: AstrMessageEvent, desc: str = ""):
        """状态卡自由生成：让 AI 直接写绘图代码，想画什么画什么"""
        t0 = time.time()
        desc = self._arg_text(event, desc)
        logger.info("[状态卡·FULL] %s 调用了 /状态卡自由生成 · %s"
                    % (self._whoami(event), (desc or "无要求")[:40]))
        code, out, err = await self._make_code(event, desc, "")
        if not code:
            yield event.plain_result("两版都没跑通：%s" % err)
            return

        try:
            name = self._save_code(code, "code", label=desc, size=getattr(self, "_last_size", None))
        except Exception as e:
            logger.warning("[状态卡·FULL] 模板存档挂了，图照发: %s" % e)
            name = "未存档"
        logger.info("[状态卡·FULL] 代码风格 %s 跑通，用时 %.1fs" % (name, time.time() - t0))
        yield event.plain_result(
            "跑通了，存成 %s.py，用了 %.1fs\n想固定用它去配置的「代码风格文件」选上"
            % (name, time.time() - t0))
        yield event.image_result(out)

    @filter.command("状态卡HTML生成", alias={"HTML生成", "网页状态卡", "状态卡网页生成", "网页生成"})
    async def gen_html_code(self, event: AstrMessageEvent, desc: str = ""):
        """状态卡HTML生成：让 AI 写一版网页模板，用浏览器截出图"""
        t0 = time.time()
        desc = self._arg_text(event, desc)
        logger.info("[状态卡·FULL] %s 调用了 /状态卡HTML生成 · %s"
                    % (self._whoami(event), (desc or "无要求")[:40]))
        code, out, err = await self._make_html_code(event, desc, "")
        if not code:
            yield event.plain_result("两版都没跑通：%s" % err)
            return

        name = self._save_code(code, "html", label=desc, kind="html")
        logger.info("[状态卡·FULL] 网页模板 %s 跑通，用时 %.1fs" % (name, time.time() - t0))
        yield event.plain_result(
            "跑通了，存成 %s.py，用了 %.1fs\n想固定用它就填到「代码风格文件」，或者丢收藏夹"
            % (name, time.time() - t0))
        yield event.image_result(out)

