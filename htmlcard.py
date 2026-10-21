# -*- coding: utf-8 -*-
# ============================================================
#  HTML 渲染器  htmlcard.py
#  跟 card.render() 平级的另一套出图方式：拼 HTML，交给 Edge 无头截图
#  配色、字号、板块顺序全部读 theme，跟 PIL 那套共用一套样式 JSON
# ============================================================

import datetime
import re
import glob
import os
import shutil
import time
import subprocess
import tempfile
import uuid

from astrbot.api import logger

from . import theme as _theme

GB = 1024 ** 3

# 页面尺寸跟 PIL 版一样，换渲染器不用改别的地方
SETTLE_MS = 400          # 图和字体就绪后再等这么久才截，防裂图
W = 1600
H = 1200


# ------------------------------------------------------------
# 小工具
# ------------------------------------------------------------
def _rgba(col, alpha=1.0):
    """(r,g,b) 或 (r,g,b,a) -> css 颜色"""
    try:
        if len(col) >= 4:
            return "rgba(%d,%d,%d,%.3f)" % (col[0], col[1], col[2], col[3] / 255.0 * alpha)
        return "rgba(%d,%d,%d,%.3f)" % (col[0], col[1], col[2], alpha)
    except Exception:
        return "rgba(255,255,255,%.3f)" % alpha


def _txt(col):
    """纯色用，不带透明度"""
    try:
        return "rgb(%d,%d,%d)" % (col[0], col[1], col[2])
    except Exception:
        return "#ffffff"


def _pick_color(val, T):
    if val >= 70:
        return _txt(T.mood_good)
    if val >= 45:
        return _txt(T.mood_mid)
    return _txt(T.mood_bad)


def _fmt_time():
    return datetime.datetime.now().strftime("%Y.%m.%d  %H:%M:%S")


_SZ_RE = re.compile(r"(\d{3,4})\s*[x×*×]\s*(\d{3,4})")

def _ok_size(w, h):
    return 200 <= w <= 4000 and 200 <= h <= 4000


def detect_size(*texts):
    """从用户话里认画布：显式 1080x1920 优先，其次竖版/横版关键词"""
    blob = " ".join([str(t or "") for t in texts if t])
    if not blob:
        return None
    low = blob.lower()
    for m in _SZ_RE.finditer(blob):
        w, h = int(m.group(1)), int(m.group(2))
        if _ok_size(w, h):
            return (w, h)
    vert = any(k in low for k in ("竖版", "竖屏", "竖向", "竖图", "纵向", "portrait", "9:16"))
    horiz = any(k in low for k in ("横版", "横屏", "横向", "横图", "landscape", "16:9"))
    if vert and not horiz:
        return (1080, 1920)
    if horiz and not vert:
        return (1600, 1200)
    return None


def sniff_size(tpl):
    """模板里自己写死画布尺寸就按它的来，免得窗口和内容对不上"""
    if not isinstance(tpl, str):
        return None
    head = tpl[:4000]
    mw = re.search(r"(?:html\s*,\s*body|body|html)[^{}]*\{[^}]*?width\s*:\s*(\d{3,4})px", head, re.S)
    mh = re.search(r"(?:html\s*,\s*body|body|html)[^{}]*\{[^}]*?height\s*:\s*(\d{3,4})px", head, re.S)
    if mw and mh:
        w, h = int(mw.group(1)), int(mh.group(1))
        if _ok_size(w, h):
            return (w, h)
    return None


def pick_size(prefer=None, tpl=None):
    """模板声明的 > 外部指定的 > 默认"""
    s = sniff_size(tpl)
    if s:
        return s
    if prefer:
        try:
            w, h = int(prefer[0]), int(prefer[1])
            if _ok_size(w, h):
                return (w, h)
        except Exception:
            pass
    return (W, H)


def trim_bottom(path, tol=12, keep=0.34):
    """内容没铺满、底下留一大片纯底色才裁；少于 28% 的纯色块当设计元素放过"""
    try:
        from PIL import Image
        im = Image.open(path).convert("RGB")
        w, h = im.size
        px = im.load()
        bg = px[w // 2, h - 3]
        step = max(1, w // 70)

        def flat(y):
            for x in range(0, w, step):
                c = px[x, y]
                if (abs(c[0] - bg[0]) + abs(c[1] - bg[1]) + abs(c[2] - bg[2])) > tol:
                    return False
            return True

        y = h - 1
        while y > int(h * keep) and flat(y):
            y -= 1
        if (h - 1 - y) > int(h * 0.28):
            im.crop((0, 0, w, y + 1)).save(path)
            return (w, y + 1)
    except Exception:
        return None


def find_browser():
    """按顺序找系统里的 Edge / Chrome"""
    cands = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    for c in cands:
        if os.path.isfile(c):
            return c
    for name in ("msedge", "chrome"):
        p = shutil.which(name)
        if p:
            return p
    return ""


# ------------------------------------------------------------
# 数据整理成模板能直接用的样子
# ------------------------------------------------------------
# ------------------------------------------------------------
# 让模型写 HTML 模板用的说明
# ------------------------------------------------------------
HINT = r"""写一个 Python 文件，里面放一整页 HTML，用来做 {W}x{H} 的服务器状态卡。

文件就长这样，TEMPLATE 里是完整网页：

TEMPLATE = '''
<!doctype html>
<html><head><meta charset="utf-8"><style>
  html,body{margin:0;padding:0;width:{W}px;height:{H}px;overflow:hidden}
</style></head><body>
  <div style="font-size:80px">{{ host }}</div>
</body></html>
'''

模板变量（只有这些，没有别的，别去翻原始数据）：

  host            计算机名
  up / now        运行时长（现成的一句话） / 当前时间
  cpu             CPU 占用百分比，字符串，不带 % 号
  mem_pct         内存占用百分比，字符串，不带 % 号
  mem_used mem_total   内存已用 / 总量，单位 GB 的数值
  proc_total      进程总数
  bars            进程占用前 12 条，每项 name mb pct bar pct_s
                  mb 是兆字节数（字符串）
                  pct 是占内存总量的真实份额，float，能直接写字也能画条
                  bar 是相对第一名的条形宽度，1~100，画进程条只能用 bar
                  pct_s 跟 pct 同值，是一位小数的字符串，写文字最省事
                  百分比只能出现 0~100 之间的正常数，第一位也不许写成 100%
  cfg             硬件配置，字段 cpu cores threads gpu os ram disk
  disks           磁盘，每项 tag used total pct pct_s color
                  used total 是 GB 数值，pct 是 float 只给条宽，pct_s 是显示用的字符串
                  color 是现成的颜色
  moods           三个状态值，每项 name val word color |                   name 是默认叫法，仅当这次主题实在没得取才用它
                  val 是 0~100，word 是现成的评价词，color 是现成的颜色
  comment / who   点评那段话 / 点评署名
  hero            立绘 data URI（透明底 PNG，已经转好），没有立绘时是空串
  heroes          多角色立绘列表，每项 uri name file，英雄就取 heroes[1].uri
  c               备用配色（都是 CSS 颜色）：bg_top bg_bottom title subtitle label text
                  sub faint cpu mem proc cfg disk_c disk_d comment bar_top bar_bottom split card hair
  fs              备用字号（数字）：title subtitle kpi section comment

硬性要求：

  - 只输出这一个 Python 文件的内容，不要解释，不要 markdown 围栏
  - 文件里只需要 TEMPLATE = '''...''' 这一句，别的语句别写
  - 画布 {W}x{H}，html 和 body 都要 margin:0 padding:0 overflow:hidden
  - 不许引用外部资源：不能有外链图片、外链字体、CDN、http 链接，只能用内联 CSS、渐变、SVG
  - 立绘是唯一例外：hero 已经是内嵌好的图片，直接写 <img src="{{ hero }}">，不许自己拼路径
  - 立绘只能来自用户给的素材文件夹，没有 hero 就没有立绘。不许用 CSS/SVG 画人像顶替，
    不许引用任何外链图片，不许拿图标或占位块假装是立绘
  - 立绘整块必须条件渲染：{% if hero %}<div class="hero">…<img src="{{ hero }}">…</div>{% endif %}
    没有立绘的时候这块整个不出现，剩下的内容自己铺满整张画布，不许留一块给立绘的空位
  - 只在有立绘时才用的装饰（光晕、圆环、投影、立绘底座）也一起放进这个 if 里
  - 要前后层次就叠两层：后一层放大加 blur 或错位当投影，前一层清晰压前面；立绘不许裁脸、不许拉变形
  - 不许写 script
  - 字体写 font-family:"Microsoft YaHei","PingFang SC",sans-serif
  - 数值自己补单位：cpu / mem_pct 后面加 %，mb 后面加 MB，used / total 后面加 GB
  - 进程那行的占比写 {{ b.pct_s }}%，条形图的宽写 {{ b.bar }}%，两个别搞混
  - 底部点评要留够高度，字数多了也得整块装下，不许被裁掉、压到别的块上或者出画布
  - 大号数字和它下面紧贴的小字必须分开：字号越大行高越大，装不下就缩字号或者把块撑高，不许文字叠在数字上
  - 配色必须从主题里长出来，不许随手挑：先读「这次的视觉方向」和「额外要求」写了什么，
    朝鲜、将军、红旗这类就是正红配金；森林、军事用墨绿；赛博、霓虹用紫青；奢侈、高级用黑金
  - c 那套是深蓝底，只是兜底，主题明显不是冷色还把整页刷成蓝的就是跑题
  - 颜色和字号自己调，c 和 fs 只是备用，别拿它当标准答案
  - 版式自己设计，想怎么排怎么排，别套模板，别堆一样的卡片
  - 该有的信息别漏：主机名、CPU、内存、进程、磁盘、点评，怎么摆你定
  - 三根状态条的标签必须按这次主题和上下文自己起名，别用默认那三个词
    心情值 / 摸鱼值 / 赛博寿命 是兜底，主题是通缉令就写「通缉程度」「逃亡进度」「招供意愿」，
    群聊气就写「累麻指数」「辱骂浓度」「心累值」，越贴题越好。标签文字自己写死在模板里，
    别用 {{ moods[0].name }} 去取默认名；数值和评价词还是用 {{ moods[0].val }} / {{ moods[0].word }}
  - 所有内容必须落在画布里面，别画到界外；竖画布就竖着排，别把横版布局硬塞进去
  - 内容必须铺满整块画布，上下左右都不许留大面积空底色，别缩在中间一小块

示例骨架（只示意，别照着写）：

TEMPLATE = '''
<!doctype html>
<html><head><meta charset="utf-8"><style>
  html,body{margin:0;padding:0;width:1600px;height:1200px;overflow:hidden;
    background:#f4f1ea;font-family:"Microsoft YaHei",sans-serif;color:#1c1a17}
  .host{font-size:96px;padding:80px 80px 10px}
</style></head><body>
  <div class="host">{{ host }}</div>
  <div style="padding:0 80px;font-size:32px">CPU {{ cpu }}% · 内存 {{ mem_pct }}%</div>
</body></html>
'''"""


def template_from_code(code):
    """从一份 Python 代码里把 TEMPLATE 字符串取出来，取不到返回 None"""
    code = str(code)
    try:
        ns = {"__name__": "scard_tpl"}
        exec(compile(code, "<template>", "exec"), ns)
        for k in ("TEMPLATE", "HTML", "TPL", "template", "html"):
            v = ns.get(k)
            if isinstance(v, str) and v.strip():
                return v
    except Exception as e:
        _log("代码里取模板失败，改从文本里刮: %s" % e)

    # 兜底：模型的 Python 写崩了（三引号提前闭合之类），直接从文本里抠网页
    m = re.search(r"(<!doctype\s+html.*?</html>|<html[\s>].*?</html>)", code, re.S | re.I)
    if m:
        return m.group(1)
    return None


def _log(msg):
    try:
        import logging
        logging.getLogger("astrbot").warning("[状态卡·HTML] %s" % msg)
    except Exception:
        pass


def looks_blank(path):
    """截图是不是一片纯色（模型把东西全画到屏幕外时会这样）"""
    try:
        from PIL import Image
        im = Image.open(path).convert("RGB").resize((48, 48))
        return len(set(im.getdata())) < 3
    except Exception:
        return False


def build_ctx(d, comment=None, who=None, size=None, assets=None):
    T = _theme.Theme
    L = T.layout
    Fs = T.fonts

    vm = d["vm"]
    cfg = d.get("cfg") or {}

    # 柱状图：数值换算成百分比，最上面留点空
    top = list(d.get("top") or [])
    maxv = top[0][0] if top else 1
    # top 里的值是 MB，vm.total 是字节，统一换成 MB 再算份额
    ram_total = (float(getattr(vm, "total", 0) or 0) / (1024.0 * 1024.0)) or (float(maxv) * 1000.0)
    bars = []
    for val, name in top[:int(L.get("bar_count") or len(top) or 12) if T.bar_count else len(top)]:
        nm = name[:-4] if name.lower().endswith(".exe") else name
        bars.append({
            "name": nm,
            "mb": "%.0f" % val,
            # pct 一律是占内存总量的真实份额（跟任务管理器一致，第一名它也不可能 100%）
            # bar 才是相对第一名的条形宽度，1~100，只准拿来画条子
            "pct": round(max(0.1, min(100.0, val / ram_total * 100.0)), 1),
            "pct_s": "%.1f" % max(0.1, min(100.0, val / ram_total * 100.0)),
            "bar": round(max(1.0, min(100.0, val / maxv * 100.0)), 1),
        })

    # 磁盘
    disks = []
    for tag, key, col in (("C", "C", T.disk_c), ("D", "D", T.disk_d)):
        du = d.get(key)
        if not du:
            continue
        disks.append({
            "tag": tag,
            "used": "%.1f" % (du.used / GB),
            "total": "%.1f" % (du.total / GB),
            "pct": float(getattr(du, "percent", 0) or 0),
            "pct_s": "%.1f" % float(getattr(du, "percent", 0) or 0),
            "color": _txt(col),
        })

    # 三个状态值
    # 三个指标的名字：优先用这张卡自己起的（通缉程度、累麻指数那种），
    # 其次看主题配置里的 metrics，最后才是默认那三个词
    mnames = [str(x).strip() for x in (d.get("metric_names") or []) if str(x).strip()][:3]
    if not mnames:
        mnames = [str(m.get("name", "")).strip() for m in (getattr(T, "metrics", None) or [])]
    mnames += ["心情值", "摸鱼值", "赛博寿命"][len(mnames):3]
    from .card import pick_word, SLACK_WORDS, LIFE_WORDS
    moods = [
        {"name": mnames[0], "val": d.get("mood", 0), "word": d.get("mood_word", "")},
        {"name": mnames[1], "val": d.get("slack", 0), "word": pick_word(d.get("slack", 0), SLACK_WORDS)},
        {"name": mnames[2], "val": d.get("life", 0), "word": pick_word(d.get("life", 0), LIFE_WORDS)},
    ]
    for m in moods:
        m["color"] = _pick_color(m["val"], T)

    blocks = [b for b in (L.get("blocks") or ["header", "kpi", "chart", "config", "disk", "comment"])]
    if not T.show_config:
        blocks = [b for b in blocks if b != "config"]
    if not T.show_disk:
        blocks = [b for b in blocks if b != "disk"]
    if not T.show_comment:
        blocks = [b for b in blocks if b != "comment"]
    blocks = [b for b in blocks if b in ("header", "kpi", "chart", "config", "disk", "comment")]

    # 光斑
    glows = []
    for g in (T.glows or []):
        try:
            x, y, r, col, a = g
            glows.append("radial-gradient(circle %dpx at %.1f%% %.1f%%, %s 0%%, transparent 100%%)"
                         % (r, x * 100, y * 100, _rgba(col, a)))
        except Exception:
            continue

    from .persona import Persona
    p = Persona()
    msg = comment or p.comment(d)
    # 点评可能带一行「2162×964 · 2.24:1」，拆出来换行排小字（模板没开 autoescape）
    try:
        from .card import _split_meta, size_label
        body, meta = _split_meta(msg)
        body = (body or "").replace("\n", "<br>")
        if not meta and comment is not None:
            meta = size_label(*(size or (W, H)))
    except Exception:
        body, meta = msg, ""
    if meta:
        msg = ('%s<br><span style="font-size:0.8em;opacity:.5">%s</span>'
               % (body or "&nbsp;", meta))
    label = who or p.name

    return {
        "W": (size or (W, H))[0], "H": (size or (W, H))[1],
        "host": d.get("host", "Windows"),
        "up": d.get("up_text_full", ""),
        "now": _fmt_time(),
        "cpu": "%.0f" % float(d.get("cpu", 0) or 0),
        "mem_pct": "%.0f" % float(getattr(vm, "percent", 0) or 0),
        "mem_used": "%.1f" % (vm.used / GB),
        "mem_total": "%.1f" % (vm.total / GB),
        "proc_total": d.get("proc_total", 0),
        "bars": bars,
        "cfg": cfg,
        "disks": disks,
        "moods": moods,
        "comment": msg,
        "who": label,
        "hero": ((assets or [{}])[0].get("uri", "") if assets else ""),
        "heroes": assets or [],
        "hero_n": len(assets or []),
        "blocks": blocks,
        "glow_css": ", ".join(glows) if glows else "none",
        "vignette": float(T.vignette or 0),
        "grid_step": int(T.grid_step or 0),
        "grid_alpha": float((T.grid_alpha or 0) / 255.0),
        "margin_x": L.get("margin_x", 100),
        "right_x": L.get("right_x", 1500),
        "top_margin": L.get("top_margin", 56),
        "block_gap": L.get("block_gap", 20),
        "chart_h": L.get("chart_max_h", 286),
        "bar_radius": L.get("bar_radius", 9),
        "gaps": L.get("gaps") or {},
        "radius": L.get("card_radius"),
        "fs": Fs,
        "c": {
            "bg_top": _txt(T.bg_top), "bg_bottom": _txt(T.bg_bottom),
            "title": _txt(T.title), "subtitle": _txt(T.subtitle),
            "label": _txt(T.label), "text": _txt(T.text),
            "sub": _txt(T.sub), "faint": _txt(T.faint),
            "cpu": _txt(T.cpu), "mem": _txt(T.mem), "proc": _txt(T.proc),
            "cfg": _txt(T.cfg), "disk_c": _txt(T.disk_c), "disk_d": _txt(T.disk_d),
            "comment": _txt(T.comment),
            "bar_top": _txt(T.bar_top), "bar_bottom": _txt(T.bar_bottom),
            "split": _rgba((255, 255, 255), (T.split_alpha or 0) / 255.0),
            "card": "rgba(255,255,255,0.035)",
            "card2": "rgba(255,255,255,0.04)",
            "hair": "rgba(255,255,255,0.07)",
        },
    }


# ------------------------------------------------------------
# 内置模板
# ------------------------------------------------------------
DEFAULT_TPL = r"""<!doctype html>
<html><head><meta charset="utf-8">
<style>
*{margin:0;padding:0;box-sizing:border-box}
html,body{width:{{W}}px;min-height:{{H}}px;overflow:hidden}
body{
  font-family:"HarmonyOS Sans SC","Source Han Sans SC","思源黑体","Microsoft YaHei",sans-serif;
  color:{{c.text}};
  background:{{glow_css}}, linear-gradient(180deg, {{c.bg_top}} 0%, {{c.bg_bottom}} 100%);
  position:relative;
}
.grid{position:absolute;inset:0;pointer-events:none;
  background-image:linear-gradient(90deg, rgba(255,255,255,{{grid_alpha}}) 1px, transparent 1px),
                   linear-gradient(0deg, rgba(255,255,255,{{grid_alpha}}) 1px, transparent 1px);
  background-size:{{grid_step}}px {{grid_step}}px;}
.veil{position:absolute;inset:0;pointer-events:none;
  background:radial-gradient(ellipse at center, transparent 40%, rgba(0,0,0,{{vignette}}) 100%);}
.wrap{position:relative;z-index:2;padding:{{top_margin}}px {{margin_x}}px 0 {{margin_x}}px;display:flex;flex-direction:column}

/* 页眉 */
.head{position:relative;padding-bottom:18px;margin-bottom:{{gaps.kpi|default(block_gap)}}px}
.h1{font-size:{{fs.title}}px;font-weight:800;color:{{c.title}};letter-spacing:1px;line-height:1.05}
.hsub{margin-top:{{fs.subtitle}}px;font-size:{{fs.subtitle}}px;color:{{c.subtitle}};opacity:.85}
.hup{margin-top:6px;font-size:13px;color:{{c.sub}}}
.htime{position:absolute;right:0;top:12px;font-size:{{fs.time}}px;color:{{c.subtitle}};opacity:.8;font-variant-numeric:tabular-nums}
.hline{position:absolute;left:0;bottom:0;width:100%;height:1px;background:linear-gradient(90deg, {{c.cpu}} 0 6%, {{c.hair}} 6% 100%)}

/* 三指标 */
.kpi{display:flex;margin-bottom:{{gaps.chart|default(block_gap)}}px}
.kpi .cell{width:300px;position:relative}
.kpi .cell+.cell{border-left:1px solid {{c.hair}};padding-left:36px;margin-left:28px}
.kpi .lab{font-size:{{fs.kpi_label}}px;color:{{c.label}};opacity:.62;letter-spacing:.5px}
.kpi .val{font-size:{{fs.kpi}}px;font-weight:800;line-height:1.15;font-variant-numeric:tabular-nums}
.kpi .sub{font-size:{{(fs.kpi*0.30)|int}}px;color:{{c.sub}};margin-left:8px;font-weight:600}

/* 柱状图 */
.chart{margin-bottom:{{gaps.config|default(block_gap)}}px}
.bars{display:flex;align-items:flex-end;height:{{chart_h}}px;position:relative}
.bars .bgline{position:absolute;left:0;right:0;height:1px;background:rgba(255,255,255,0.04)}
.bar{flex:1;display:flex;flex-direction:column;justify-content:flex-end;align-items:center;height:100%;position:relative;z-index:2}
.bar .num{font-size:{{fs.bar_val}}px;color:{{c.sub}};margin-bottom:6px;font-variant-numeric:tabular-nums}
.bar .col{width:58%;border-radius:{{bar_radius}}px;background:linear-gradient(180deg, {{c.bar_top}} 0%, {{c.bar_bottom}} 100%);box-shadow:0 0 18px rgba(56,189,248,0.18)}
.bar .nm{margin-top:10px;font-size:{{fs.proc}}px;color:{{c.sub}};max-width:100%;text-align:center;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.axis{margin-top:6px;height:1px;background:rgba(255,255,255,0.14)}

/* 卡片 */
.card{position:relative;border-radius:16px;background:{{c.card}};padding:16px 26px;margin-bottom:{{block_gap}}px}
.card .mark{position:absolute;left:0;top:22px;bottom:22px;width:4px;border-radius:2px}
.card .cap{font-size:{{fs.section}}px;color:{{c.label}};opacity:.6;letter-spacing:1px;margin-bottom:12px}

/* 硬件 */
.cfgs{display:flex}
.cfgs .it{flex:1;min-width:0;padding-right:14px}
.cfgs .it+.it{border-left:1px solid rgba(255,255,255,0.06);padding-left:18px}
.cfgs .k{font-size:{{fs.cfg_label}}px;color:{{c.label}};opacity:.55;margin-bottom:8px}
.cfgs .v{font-size:{{fs.cfg_value}}px;font-weight:700;color:{{c.text}};white-space:nowrap;overflow:hidden;text-overflow:ellipsis}

/* 磁盘 + 状态值 */
.diskrow{display:flex;align-items:flex-start}
.diskrow .left{flex:1;min-width:0}
.diskrow .dk{display:flex;gap:56px}
.diskrow .dk .t{font-size:{{fs.disk}}px;font-weight:800;font-variant-numeric:tabular-nums}
.diskrow .dk .u{font-size:{{fs.disk*0.52|int}}px;color:{{c.sub}};margin-left:6px;font-weight:600}
.diskrow .db{height:5px;border-radius:3px;background:rgba(255,255,255,0.09);margin-top:10px;width:330px;overflow:hidden}
.diskrow .db i{display:block;height:100%;border-radius:3px}
.moods{display:flex;gap:{{gaps.mood|default(14)}}px;margin-left:40px;border-left:1px solid rgba(255,255,255,0.08);padding-left:34px}
.mood{width:160px}
.mood .m{display:flex;justify-content:space-between;align-items:baseline}
.mood .mn{font-size:{{fs.mood_label}}px;color:{{c.label}};opacity:.55}
.mood .mv{font-size:{{fs.mood_num}}px;font-weight:800;font-variant-numeric:tabular-nums}
.mood .mv small{font-size:{{fs.mood_unit}}px;color:{{c.faint}};font-weight:600;margin-left:2px}
.mood .mb{height:6px;border-radius:3px;background:rgba(255,255,255,0.09);margin:8px 0 7px;overflow:hidden}
.mood .mb i{display:block;height:100%;border-radius:3px}
.mood .mw{font-size:{{fs.mood_word}}px;color:{{c.sub}}}

/* 点评 */
.talk .q{font-size:{{fs.comment}}px;line-height:1.55;color:{{c.text}};
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
</style></head>
<body>
<div class="grid"></div><div class="veil"></div>
<div class="wrap">
{% for b in blocks %}
{% if b == 'header' %}
  <div class="head">
    <div class="h1">服务器状态</div>
    <div class="hsub">{{host}}  ·  Windows</div>
    <div class="hup">已运行&nbsp;&nbsp;{{up}}</div>
    <div class="htime">{{now}}</div>
    <div class="hline"></div>
  </div>
{% elif b == 'kpi' %}
  <div class="kpi">
    <div class="cell"><div class="lab">CPU 占用</div><div class="val" style="color:{{c.cpu}}">{{cpu}}%</div></div>
    <div class="cell"><div class="lab">内存占用</div><div class="val" style="color:{{c.mem}}">{{mem_pct}}%<span class="sub">{{mem_used}}/{{mem_total}}G</span></div></div>
    <div class="cell"><div class="lab">进程总数</div><div class="val" style="color:{{c.proc}}">{{proc_total}}</div></div>
  </div>
{% elif b == 'chart' %}
  <div class="chart">
    <div class="bars">
      <div class="bgline" style="bottom:25%"></div><div class="bgline" style="bottom:50%"></div>
      <div class="bgline" style="bottom:75%"></div><div class="bgline" style="bottom:100%"></div>
      {% for bar in bars %}
      <div class="bar"><div class="num">{{bar.mb}}</div>
        <div class="col" style="height:{{bar.pct}}%;max-height:calc(100% - 48px)"></div>
        <div class="nm">{{bar.name}}</div></div>
      {% endfor %}
    </div>
    <div class="axis"></div>
  </div>
{% elif b == 'config' %}
  <div class="card">
    <div class="mark" style="background:{{c.cfg}}"></div>
    <div class="cap">硬件配置</div>
    <div class="cfgs">
      <div class="it"><div class="k">CPU · {{cfg.cores}} 核 {{cfg.threads}} 线程</div><div class="v">{{cfg.cpu}}</div></div>
      <div class="it"><div class="k">内存</div><div class="v">{{cfg.ram}}</div></div>
      <div class="it"><div class="k">显卡</div><div class="v">{{cfg.gpu}}</div></div>
      <div class="it"><div class="k">系统</div><div class="v">{{cfg.os}}</div></div>
      <div class="it"><div class="k">硬盘</div><div class="v">{{cfg.disk}}</div></div>
    </div>
  </div>
{% elif b == 'disk' %}
  <div class="card">
    <div class="mark" style="background:{{c.disk_c}}"></div>
    <div class="cap">磁盘占用</div>
    <div class="diskrow">
      <div class="left">
        <div class="dk">
          {% for k in disks %}
          <div><span class="t" style="color:{{k.color}}">{{k.tag}} 盘</span><span class="u">{{k.used}} / {{k.total}} GB</span>
            <div class="db"><i style="width:{{k.pct}}%;background:{{k.color}}"></i></div></div>
          {% endfor %}
        </div>
      </div>
      <div class="moods">
        {% for m in moods %}
        <div class="mood">
          <div class="m"><span class="mn">{{m.name}}</span><span class="mv" style="color:{{m.color}}">{{m.val}}<small>/100</small></span></div>
          <div class="mb"><i style="width:{{m.val}}%;background:{{m.color}}"></i></div>
          <div class="mw">{{m.word}}</div>
        </div>
        {% endfor %}
      </div>
    </div>
  </div>
{% elif b == 'comment' %}
  <div class="card talk">
    <div class="mark" style="background:{{c.comment}}"></div>
    <div class="cap">AI 点评 · {{who}}</div>
    <div class="q">{{comment}}</div>
  </div>
{% endif %}
{% endfor %}
</div>
</body></html>
"""


# ------------------------------------------------------------
# 出图
# ------------------------------------------------------------
def build_html(d, comment=None, who=None, tpl=None, size=None, assets=None):
    """把数据套进模板，返回完整 HTML 字符串"""
    from jinja2 import Environment
    ctx = build_ctx(d, comment, who, size, assets)
    tpl = tpl or DEFAULT_TPL
    if not isinstance(tpl, str):
        tpl = str(tpl)
    # 自定义模板里可能有些变量没有，用宽松模式别炸
    try:
        from jinja2 import ChainableUndefined
        env = Environment(autoescape=False, undefined=ChainableUndefined)
    except Exception:
        env = Environment(autoescape=False)
    return env.from_string(tpl).render(**ctx)



def _free_port():
    import socket
    sk = socket.socket()
    sk.bind(("127.0.0.1", 0))
    port = sk.getsockname()[1]
    sk.close()
    return port


def _wait_devtools(port, timeout=15):
    """等浏览器把调试口开出来，拿到当前标签页的 ws 地址"""
    import json as _json
    import urllib.request
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            u = "http://127.0.0.1:%d/json/list" % port
            with urllib.request.urlopen(u, timeout=1) as r:
                tabs = _json.loads(r.read().decode("utf-8", "ignore"))
            for t in tabs:
                if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                    return t["webSocketDebuggerUrl"]
        except Exception:
            pass
        time.sleep(0.15)
    return None


_JS_READY = """(() => {
  if (document.readyState !== 'complete') return false;
  const ims = Array.from(document.images || []);
  for (const i of ims) { if (!i.complete || !i.naturalWidth) return false; }
  if (document.fonts && document.fonts.status !== 'loaded') return false;
  return true;
})()"""


def shoot(html_text, out_png, width=W, height=H, scale=1, timeout=60, settle_ms=SETTLE_MS):
    """HTML 存临时文件，Edge 无头截成图

    走调试口等图片真的解码完、字体也读完，再停 settle_ms 才截；
    等不到就退回 --screenshot 那条老路，至少保证有图。
    """
    browser = find_browser()
    if not browser:
        raise RuntimeError("找不到 Edge 或 Chrome，HTML 渲染需要浏览器")

    tmpdir = tempfile.mkdtemp(prefix="scard_")
    tmp_html = os.path.join(tmpdir, "card_%s.html" % uuid.uuid4().hex[:8])
    profile = os.path.join(tmpdir, "profile")
    port = _free_port()
    proc = None
    ok = False
    try:
        with open(tmp_html, "w", encoding="utf-8") as f:
            f.write(html_text)
        url = "file:///" + tmp_html.replace("\\", "/")
        argv = [
            browser, "--headless=new", "--disable-gpu", "--hide-scrollbars",
            "--no-first-run", "--no-default-browser-check", "--disable-extensions",
            "--disable-features=Translate,BackForwardCache",
            "--force-device-scale-factor=%d" % int(scale or 1),
            "--user-data-dir=" + profile,
            "--window-size=%d,%d" % (int(width), int(height)),
            "--remote-debugging-port=%d" % port,
            "--remote-allow-origins=*",
            "about:blank",
        ]
        proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        ws_url = _wait_devtools(port)
        if ws_url:
            try:
                import base64
                import json as _json
                import websocket
                ws = websocket.create_connection(ws_url, timeout=30)
                mid = [0]

                def call(method, params=None):
                    mid[0] += 1
                    ws.send(_json.dumps({"id": mid[0], "method": method,
                                         "params": params or {}}))
                    while True:
                        msg = _json.loads(ws.recv())
                        if msg.get("id") == mid[0]:
                            return msg

                call("Page.enable")
                # 视口按模板尺寸来，headless 的窗口高度含边框，不设会矮一截
                call("Emulation.setDeviceMetricsOverride", {
                    "width": int(width), "height": int(height),
                    "deviceScaleFactor": int(scale or 1), "mobile": False,
                })
                call("Page.navigate", {"url": url})
                deadline = time.time() + max(3.0, timeout - 8)
                ready = False
                while time.time() < deadline:
                    r = call("Runtime.evaluate",
                             {"expression": _JS_READY, "returnByValue": True})
                    if ((r or {}).get("result") or {}).get("result", {}).get("value"):
                        ready = True
                        break
                    time.sleep(0.12)
                if not ready:
                    logger.warning("[状态卡·FULL] 图和字体还没齐就截了，可能有裂图")
                # 再等一会儿，让合成器把大图真的画上去，别刚 decode 完就拍
                time.sleep(max(0.0, float(settle_ms or 0) / 1000.0))
                shot = call("Page.captureScreenshot", {
                    "format": "png", "fromSurface": True,
                    "captureBeyondViewport": True,
                    "clip": {"x": 0, "y": 0, "width": int(width),
                             "height": int(height), "scale": 1},
                })
                data = ((shot or {}).get("result") or {}).get("data")
                if data:
                    with open(out_png, "wb") as f:
                        f.write(base64.b64decode(data))
                    ok = os.path.exists(out_png)
                try:
                    ws.close()
                except Exception:
                    pass
            except Exception as e:
                logger.warning("[状态卡·FULL] 调试口截图没走通，换老办法: %s" % e)
                ok = False
        if not ok:
            # 老路要用同一个 profile，先把调试口那个进程收掉
            if proc is not None:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except Exception:
                    pass
                proc = None
            cmd = [
                browser, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                "--no-first-run", "--no-default-browser-check", "--disable-extensions",
                "--disable-features=Translate,BackForwardCache",
                "--force-device-scale-factor=%d" % int(scale or 1),
                "--user-data-dir=" + profile,
                "--window-size=%d,%d" % (int(width), int(height)),
                "--virtual-time-budget=%d" % int(max(2000, settle_ms or 0) + 2000),
                "--screenshot=" + out_png,
                url,
            ]
            r = subprocess.run(cmd, capture_output=True, timeout=timeout)
            if not os.path.exists(out_png):
                err = (r.stderr or b"").decode("utf-8", "ignore")[:300]
                raise RuntimeError("Edge 没截出图：%s" % (err or "无输出"))
    finally:
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        shutil.rmtree(tmpdir, ignore_errors=True)
    return out_png


def render(d, out_path, comment=None, who=None, tpl=None, height=None, width=None,
           assets=None, settle=None):
    """跟 card.render 一个用法，只是走 HTML

    assets 是已经转好 data URI 的立绘：[{file, name, uri}]，模板里用 {{ hero }}
    settle 是图和字体就绪后再等多少毫秒才截，默认 SETTLE_MS
    """
    w, h = pick_size((width, height) if (width and height) else None, tpl)
    html = build_html(d, comment, who, tpl, (w, h), assets)
    shoot(html, out_path, w, h, settle_ms=(SETTLE_MS if settle is None else settle))
    trim_bottom(out_path)
    logger.info("[状态卡·FULL] HTML 渲染完 %s · %dx%d" % (os.path.basename(out_path), w, h))
    return out_path


def load_template(path):
    """读一份 HTML 模板：.html 直接读文本，.py 就找里面的 TEMPLATE 字符串"""
    name = os.path.basename(str(path or ""))
    try:
        if os.path.splitext(name)[1].lower() == ".py":
            ns = {"__name__": "scard_html_tpl", "__file__": path}
            with open(path, encoding="utf-8") as f:
                exec(compile(f.read(), path, "exec"), ns)
            for k in ("TEMPLATE", "HTML", "TPL", "template", "html"):
                v = ns.get(k)
                if isinstance(v, str) and v.strip():
                    return v
            logger.warning("[状态卡·FULL] %s 里没找到 TEMPLATE 字符串" % name)
            return None
        with open(path, encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        logger.warning("[状态卡·FULL] HTML 模板 %s 读不了: %s" % (name, e))
        return None
