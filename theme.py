# -*- coding: utf-8 -*-
# ============================================================
#  主题配置  theme.py
#  换画风只要动这个文件，别的地方不用管
#  颜色统一用 (R, G, B) 元组
# ============================================================

import os


def _font(*names):
    """按顺序在系统字体目录里找第一个存在的字体文件

    找的位置依次是:
      系统字体目录、当前用户装的字体目录
    都找不到就返回空字符串，渲染时会退回默认字体
    """
    windir = os.environ.get("WINDIR") or r"C:\Windows"
    local = os.environ.get("LOCALAPPDATA") or ""
    home = os.path.expanduser("~")

    dirs = [os.path.join(windir, "Fonts")]
    if local:
        dirs.append(os.path.join(local, "Microsoft", "Windows", "Fonts"))
    if home and home != "~":
        dirs.append(os.path.join(home, "AppData", "Local",
                                 "Microsoft", "Windows", "Fonts"))

    for n in names:
        for d in dirs:
            if not os.path.isdir(d):
                continue
            p = os.path.join(d, n)
            if os.path.isfile(p):
                return p
    return ""


class Theme:

    # ---------- 背景 ----------
    bg_top    = (8, 11, 20)      # 渐变顶部
    bg_bottom = (18, 24, 42)     # 渐变底部

    # 光斑: (x比例, y比例, 半径, 颜色, 强度)
    glows = [
        (0.11, 0.06, 380, (37, 99, 235), 0.42),
        (0.94, 0.85, 420, (13, 148, 190), 0.26),
        (0.56, 0.19, 560, (109, 40, 217), 0.14),
    ]
    vignette   = 0.42            # 暗角强度 0~1
    grid_step  = 48              # 网格间距，设 0 关闭
    grid_alpha = 5               # 网格透明度 0~255

    # ---------- 文字 ----------
    title    = (255, 255, 255)   # 大标题
    subtitle = (255, 255, 255)   # 副标题 / 时间
    label    = (255, 255, 255)   # 各板块小标题
    text  = (240, 244, 252)
    sub   = (142, 156, 180)
    faint = (98, 112, 138)
    split_alpha = 24             # 分隔线透明度

    # ---------- 强调色 ----------
    cpu        = (56, 189, 248)
    mem        = (52, 211, 153)
    proc       = (167, 139, 250)
    cfg        = (167, 139, 250)
    disk_c     = (96, 165, 250)
    disk_d     = (52, 211, 153)
    comment    = (56, 189, 248)
    bar_top    = (34, 211, 238)  # 柱子顶部
    bar_bottom = (37, 99, 235)   # 柱子底部

    # ---------- 心情值配色（按分数自动选） ----------
    mood_good = (52, 211, 153)   # >= 70
    mood_mid  = (245, 158, 11)   # >= 45
    mood_bad  = (248, 113, 113)  # 其余

    # ---------- 字体 ----------
    # 大标题用标小智无界黑，正文和数字用鸿蒙，都找不到就退回雅黑
    font_title = _font("LogoSCUnboundedSans.otf",
                       "HarmonyOS_Sans_SC_Bold.ttf", "msyhbd.ttc")
    font_main = _font("HarmonyOS_Sans_SC_Regular.ttf", "msyh.ttc")
    font_bold = _font("HarmonyOS_Sans_SC_Bold.ttf", "msyhbd.ttc")

    # ---------- 版式 ----------
    width           = 1600
    bar_count       = 12         # 柱子数量
    bar_width_ratio = 0.58       # 柱子粗细比例
    card_radius     = 16         # 卡片圆角
    bar_radius      = 9          # 柱子圆角

    # 版式：整张卡是「页眉 / 三指标 / 柱状图 / 硬件 / 磁盘 / 点评」六块拼起来，
    # blocks 决定顺序，gaps 决定每块下面空多少，想推翻重排就动这两个
    layout = {
        "margin_x": 100,         # 左右留白
        "right_x": 1500,         # 右边界
        "top_margin": 56,        # 顶部留白
        "sub_gap": 22,           # 副标题离标题底部的距离
        "kpi_gap": 24,           # 大数字离标签的距离
        "chart_max_h": 286,      # 柱子最高高度
        "mood_x": 940,           # 状态值那一排的起点
        "mood_slot": 180,        # 每个状态值占多宽
        "cfg_h": 126,            # 硬件配置卡高度
        "disk_h": 92,            # 磁盘卡高度
        "comment_h": 104,        # 点评卡高度
        "block_gap": 20,         # 默认块间距
        "gaps": {"kpi": 24, "chart": 143, "config": 32, "disk": 20, "comment": 20},
        "blocks": ["header", "kpi", "chart", "config", "disk", "comment"],
    }

    # 各处字号，想调大小改这里
    fonts = {
        "title": 46,             # 大标题
        "subtitle": 16,          # 标题下面那行
        "time": 15,              # 右上角时间
        "kpi_label": 15,         # CPU 占用 / 内存占用 / 进程总数
        "kpi": 52,               # 那三个大数字
        "axis": 13,              # 柱状图左侧刻度
        "bar_val": 15,           # 柱子顶上的 MB
        "proc": 14,              # 柱子下面的进程名
        "section": 13,           # 各卡片的小标题
        "cfg_label": 12,         # 硬件配置的项名
        "cfg_value": 17,         # 硬件配置的值
        "disk": 23,              # 磁盘容量
        "mood_label": 12,        # 心情值 / 摸鱼值 这几个字
        "mood_num": 19,          # 状态值数字
        "mood_unit": 11,         # /100
        "mood_word": 12,         # 状态词
        "comment": 19,           # 点评正文
    }

    # 三个状态值的名字，想叫什么都行
    metrics = [
        {"name": "心情值"},
        {"name": "摸鱼值"},
        {"name": "赛博寿命"},
    ]

    show_config  = True          # 是否显示硬件配置栏
    show_disk    = True          # 是否显示磁盘 + 心情
    show_mood    = True          # 磁盘栏右半边的心情值
    show_comment = True          # 是否显示 AI 点评

# ============================================================
#  风格存档
#  Theme 里除了字体路径，其他属性都能整份导出成 JSON，
#  换风格就是换一份 JSON，改完存进 styles/ 就行
# ============================================================

_FONT_KEYS = ("font_title", "font_main", "font_bold")
_base_cache = None


def _plain(v):
    """tuple 转 list，好在 JSON 里写"""
    if isinstance(v, (tuple, list)):
        return [_plain(x) for x in v]
    return v


def _restore(v):
    """读回来时把颜色转成 tuple"""
    if isinstance(v, list):
        if len(v) == 3 and all(isinstance(x, int) for x in v):
            return tuple(v)
        return [_restore(x) for x in v]
    return v


def to_dict():
    """把当前 Theme 导出成能写进 JSON 的 dict"""
    out = {}
    for k, v in vars(Theme).items():
        if k.startswith("_") or k in _FONT_KEYS:
            continue
        if callable(v) or isinstance(v, type):
            continue
        out[k] = _plain(v)
    return out


def snapshot():
    """记一份出厂样式，换风格前拿它复位"""
    global _base_cache
    if _base_cache is None:
        _base_cache = to_dict()
    return _base_cache


def apply_dict(d):
    """把一份风格套到 Theme 上，不认识的键忽略

    字典类型的（fonts / layout）是合并而不是整体替换，
    这样模型只给几个字号也不会把别的键冲掉
    """
    for k, v in (d or {}).items():
        if k.startswith("_") or k in _FONT_KEYS:
            continue
        if not hasattr(Theme, k):
            continue
        cur = getattr(Theme, k)
        if isinstance(cur, dict) and isinstance(v, dict):
            merged = dict(cur)
            merged.update(_restore(v))
            setattr(Theme, k, merged)
        else:
            setattr(Theme, k, _restore(v))
    return Theme


def reset():
    """复位到出厂样式"""
    apply_dict(snapshot())
