# -*- coding: utf-8 -*-
# ============================================================
#  服务器状态卡  server_status.py
#  用法:
#      python server_status.py                 输出到同目录 server_status.png
#      python server_status.py out.png         指定输出文件名
#  依赖: psutil  pillow  numpy
#  --------------------------------------------------------
#  想换画风  -> 改 theme.py
#  想换口吻  -> 改 persona.py
# ============================================================
import os
import sys
import json
import random
import datetime
import subprocess
import time

import numpy as np
import psutil
from PIL import Image, ImageDraw, ImageFont

from .theme import Theme
from .persona import Persona

GB = 1024 ** 3

# 硬件型号基本不变，缓存起来，省得每次出图都等 PowerShell 启动
HW_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".hw_cache.json")
HW_CACHE_HOURS = 12
HW_CACHE_VER = 2        # 采集字段一变就加一，老缓存自动作废

RAM_BRAND = {
    "unknown": "", "default string": "", "not specified": "", "000000": "",
    "0x0000": "", "empty": "", "": "",
    "sk hynix": "Hynix", "hynix": "Hynix",
}
RAM_DISK_BRAND = [
    ("skhynix", "SK Hynix"), ("hynix", "Hynix"), ("samsung", "Samsung"),
    ("western digital", "WD"), ("wd", "WD"), ("seagate", "Seagate"),
    ("kingston", "Kingston"), ("crucial", "Crucial"), ("micron", "Micron"),
    ("netac", "Netac"), ("intel", "Intel"), ("toshiba", "Toshiba"),
    ("kioxia", "Kioxia"), ("sandisk", "SanDisk"), ("adata", "ADATA"),
    ("hikvision", "Hikvision"), ("aigo", "aigo"), ("fanxiang", "Fanxiang"),
    ("ramsta", "Ramsta"), ("lenovo", "Lenovo"), ("colorful", "Colorful"),
]
RAM_TYPE = {20: "DDR", 21: "DDR2", 24: "DDR3", 26: "DDR4",
            27: "LPDDR", 28: "LPDDR2", 29: "LPDDR3", 30: "LPDDR4", 34: "DDR5"}


# ------------------------------------------------------------
# 采集
# ------------------------------------------------------------
def collect():
    d = {}
    d["vm"] = psutil.virtual_memory()
    # 1 秒采样太浪费，0.2 秒够用了，省下来的时间够画半张图
    d["cpu"] = psutil.cpu_percent(interval=0.2)
    d["C"] = psutil.disk_usage("C:\\")
    d["D"] = psutil.disk_usage("D:\\")
    d["proc_total"] = len(psutil.pids())

    agg = {}
    for p in psutil.process_iter(["name", "memory_info", "cpu_times"]):
        try:
            if p.pid in (0, 4):
                continue
            n = p.info["name"] or "unknown"
            mi = p.info["memory_info"]
            ct = p.info["cpu_times"]
            if mi is None or ct is None:
                continue
            if n.lower() == "system idle process":
                continue
            e = agg.setdefault(n, [0, 0.0])
            e[0] += mi.rss
            e[1] += ct.user + ct.system
        except Exception:
            pass

    d["top"] = sorted(((v[0] / 1024 / 1024, k) for k, v in agg.items()),
                      reverse=True)[:Theme.bar_count]

    # 开机时长：全部由总秒数一次算出来，避免小时数不取模出现 287h 这种
    boot = datetime.datetime.fromtimestamp(psutil.boot_time())
    _up_total = max(0, int((datetime.datetime.now() - boot).total_seconds()))

    d["up_days"] = _up_total // 86400                 # 天数
    d["up_h"] = _up_total // 3600 % 24                # 小时 0-23，展示用
    d["up_m"] = _up_total // 60 % 60                  # 分钟 0-59
    d["up_s"] = _up_total % 60                        # 秒 0-59
    d["up_h24"] = d["up_h"]                           # 老字段，保持兼容
    d["up_total_h"] = _up_total // 3600               # 总小时，只给文案判断用
    d["up_text"] = "%d 天 %02d 时 %02d 分" % (d["up_days"], d["up_h"], d["up_m"])
    d["up_text_full"] = "%d 天 %02d 时 %02d 分 %02d 秒" % (
        d["up_days"], d["up_h"], d["up_m"], d["up_s"])
    d["host"] = os.environ.get("COMPUTERNAME", "DESKTOP")
    d["cfg"] = _hardware()

    d["mem_percent"] = d["vm"].percent
    d["mem_total_gb"] = d["vm"].total / GB
    d["free_c"] = d["C"].free / GB
    d["free_d"] = d["D"].free / GB
    d["mood"], d["mood_word"] = calc_mood(d)
    d["slack"] = calc_slack(d)
    d["life"] = calc_life(d)
    return d


def _hardware():
    try:
        if HW_CACHE_HOURS > 0 and os.path.exists(HW_CACHE_FILE):
            if time.time() - os.path.getmtime(HW_CACHE_FILE) < HW_CACHE_HOURS * 3600:
                with open(HW_CACHE_FILE, encoding="utf-8") as f:
                    cached = json.load(f)
                if (isinstance(cached, dict) and cached.get("cpu")
                        and cached.get("ver") == HW_CACHE_VER):
                    return cached
    except Exception:
        pass

    out = {"cpu": "Unknown CPU", "cores": "-", "threads": "-",
           "gpu": "未知", "os": "Windows", "res": "-",
           "ram": "-", "build": "-", "disk": "-", "ver": HW_CACHE_VER}
    ps = (
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n"
        "$p  = Get-CimInstance Win32_Processor | Select-Object -First 1\n"
        "$v  = Get-CimInstance Win32_VideoController | "
        "Where-Object { $_.Name -notlike '*Oray*' } | Select-Object -First 1\n"
        "$os = Get-CimInstance Win32_OperatingSystem\n"
        "$dd = @(Get-CimInstance Win32_DiskDrive | Where-Object { $_.Size -gt 0 } "
        "| Select-Object Model, Size, MediaType)\n"
        "$k  = Get-ItemProperty 'HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion'\n"
        "$m  = @(Get-CimInstance Win32_PhysicalMemory)\n"
        "[ordered]@{ cpu=$p.Name; cores=$p.NumberOfCores; "
        "threads=$p.NumberOfLogicalProcessors; gpu=$v.Name; os=$os.Caption; "
        "res=($v.CurrentHorizontalResolution.ToString() + ' x ' + "
        "$v.CurrentVerticalResolution.ToString()); ver=$k.DisplayVersion; "
        "build=$k.CurrentBuild; pn=$k.ProductName; mods=$m; "
        "disks=$dd } | ConvertTo-Json -Depth 5 -Compress\n"
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
            capture_output=True, text=True, encoding="utf-8",
            errors="ignore", timeout=60)
        j = json.loads(r.stdout.strip())

        name = (j.get("cpu") or "").strip()
        name = name.replace("(R)", "").replace("(TM)", "").replace("CPU", "")
        name = " ".join(name.split())
        if " with " in name:
            name = name.split(" with ")[0].strip()
        out["cpu"] = name or out["cpu"]
        out["cores"] = j.get("cores") or "-"
        out["threads"] = j.get("threads") or "-"
        out["gpu"] = (j.get("gpu") or "未知").strip()
        out["res"] = (j.get("res") or "-").replace("null", "-")

        osname = (j.get("pn") or j.get("os") or "Windows").replace("Microsoft ", "").strip()
        ver = (j.get("ver") or "").strip()
        if ver:
            osname = "%s %s" % (osname, ver)
        out["os"] = osname
        out["build"] = (j.get("build") or "-").strip()

        out["ram"] = _mem_str(j.get("mods") or [])
        out["disk"] = _disk_str(j.get("disks") or j.get("disk"))
    except Exception:
        pass

    try:
        if HW_CACHE_HOURS > 0 and out.get("cpu") != "Unknown CPU":
            with open(HW_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False)
    except Exception:
        pass
    return out


def _mem_str(mods):
    if not mods:
        return "%.1f GB" % (psutil.virtual_memory().total / GB)
    if isinstance(mods, dict):
        mods = [mods]
    m = mods[0]
    cap = sum((x.get("Capacity") or 0) for x in mods) / GB
    if cap <= 0:
        cap = psutil.virtual_memory().total / GB

    raw = (m.get("Manufacturer") or "").strip()
    brand = RAM_BRAND.get(raw.lower(), raw)
    if len(brand) > 14:
        brand = ""

    typ = RAM_TYPE.get(m.get("SMBIOSMemoryType"), "DDR")
    speed = m.get("ConfiguredClockSpeed") or m.get("Speed") or 0

    s = ("%s " % brand if brand else "") + "%.0fG %s" % (cap, typ)
    if speed:
        s += "-%d" % speed
    return s


def _disk_brand(model):
    low = (model or "").lower()
    for key, name in RAM_DISK_BRAND:
        if key in low:
            return name
    return ""


def _disk_str(disks):
    """把 Win32_DiskDrive 的数组拼成一行，多块盘用 + 连起来。

    超过 3 块就不再逐个列，直接给块数和总容量，免得把硬件栏撑爆。
    """
    if not disks:
        return "-"
    if isinstance(disks, dict):
        disks = [disks]
    disks = [x for x in disks if isinstance(x, dict) and x.get("Model")]
    if not disks:
        return "-"
    parts = []
    for x in disks:
        model = (x.get("Model") or "").strip()
        cap = int(round((x.get("Size") or 0) / 1e9))
        brand = _disk_brand(model)
        if brand:
            parts.append("%s %dG" % (brand, cap) if cap else brand)
        else:
            short = model if len(model) <= 13 else model[:12] + "…"
            parts.append("%s %dG" % (short, cap) if cap else short)

    # 硬件栏那格只够两行，超过两块就把剩下的折成一句
    if len(parts) > 2:
        total = sum((x.get("Size") or 0) for x in disks) / 1e9
        return "%s\n等 %d 盘 %.0fG" % (parts[0], len(parts), total)
    return "\n".join(parts)


def calc_mood(d):
    s = 100.0
    s -= max(0, d["mem_percent"] - 50) * 0.5
    s -= d["cpu"] * 0.3
    s -= d["up_days"] * 1.2
    c_pct = d["C"].used / d["C"].total * 100
    s -= max(0, c_pct - 70) * 0.3
    s = max(5, min(100, int(round(s))))

    words = [
        (90, ["今天气色不错", "神清气爽", "没什么好挑的"]),
        (75, ["还算舒坦", "凑合过得去", "不难受"]),
        (60, ["有点累但能扛", "稳着", "还能再撑撑"]),
        (40, ["挤得慌", "有点闷", "喘气都费劲"]),
        (20, ["快撑不住了", "看着就累", "该歇了"]),
        (0,  ["想关机", "别用我了", "我要罢机"]),
    ]
    pool = words[-1][1]
    for th, lst in words:
        if s >= th:
            pool = lst
            break
    word = random.choice(pool)
    return s, word


def calc_load(d):
    """负载：内存占大头，CPU 补一点"""
    return max(0, min(100, int(round(d["mem_percent"] * 0.7 + d["cpu"] * 0.3))))


def calc_slack(d):
    """摸鱼值：机器越闲越高，等于 100 减负载"""
    return max(0, min(100, 100 - calc_load(d)))


def calc_life(d):
    """赛博寿命：开机越久、C 盘越满，能撑的日子越少"""
    c_pct = d["C"].used / d["C"].total * 100
    v = 100 - d["up_days"] * 3 - c_pct * 0.3
    return max(0, min(100, int(round(v))))


SLACK_WORDS = [(85, "摸得飞起"), (70, "纯摸鱼"), (50, "半摸半干"),
               (30, "有点忙"), (0, "卷起来了")]
LIFE_WORDS = [(70, "还很硬朗"), (50, "还能撑"), (30, "该保养了"), (0, "快到头了")]


def pick_word(v, table):
    for th, w in table:
        if v >= th:
            return w
    return table[-1][1]


def pick_color(v, T):
    if v >= 70:
        return T.mood_good
    if v >= 40:
        return T.mood_mid
    return T.mood_bad


# ------------------------------------------------------------
# 绘制
# ------------------------------------------------------------
def _bg(W, H):
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    t = np.array(Theme.bg_top, np.float32)
    b = np.array(Theme.bg_bottom, np.float32)
    v = (yy / H)[..., None]
    base = t * (1 - v) + b * v

    for rx, ry, sigma, color, strength in Theme.glows:
        cx, cy = rx * W, ry * H
        dist2 = (xx - cx) ** 2 + (yy - cy) ** 2
        g = (np.exp(-dist2 / (2 * sigma * sigma)) * strength)[..., None]
        base = base + g * np.array(color, np.float32)

    dist = np.sqrt((xx - W / 2) ** 2 + (yy - H / 2) ** 2) / (W * 0.62)
    base = base * np.clip(1.06 - dist * Theme.vignette, 0.55, 1.0)[..., None]
    return np.clip(base, 0, 255).astype(np.uint8)


def _wrap(draw, text, font, maxw):
    lines, cur = [], ""
    for ch in text:
        if draw.textlength(cur + ch, font=font) > maxw:
            lines.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        lines.append(cur)
    return lines


def _mood_color(score, T):
    if score >= 70:
        return T.mood_good
    if score >= 45:
        return T.mood_mid
    return T.mood_bad


def render(d, out_path, comment=None, who=None):
    """按 layout["blocks"] 的顺序一块一块往下画，块高自己算"""
    T = Theme
    L = T.layout
    Fs = T.fonts
    W = T.width
    MX, RX = L["margin_x"], L["right_x"]
    vm, C, Dd = d["vm"], d["C"], d["D"]
    sub_gap = L.get("sub_gap", 22)
    kpi_gap = L.get("kpi_gap", 24)

    def F(size, bold=False):
        try:
            return ImageFont.truetype(T.font_bold if bold else T.font_main, size)
        except Exception:
            return ImageFont.load_default()

    def FT(size):
        """大标题专用字体，没配就退回粗体"""
        try:
            return ImageFont.truetype(T.font_title, size)
        except Exception:
            return F(size, True)

    # ---------- 每块的高度 ----------
    H_BLK = {
        "header": Fs["title"] + sub_gap + Fs["subtitle"] + 46,
        "kpi": Fs["kpi_label"] + kpi_gap + int(Fs["kpi"] * 1.12),
        "chart": L["chart_max_h"] + Fs["proc"] + 28,
        "config": L.get("cfg_h", 126) + max(0, Fs["cfg_value"] - 17) * 2,
        "disk": L.get("disk_h", 92) + max(0, Fs["disk"] - 23) * 2,
        "comment": L.get("comment_h", 104) + max(0, Fs["comment"] - 19) * 2,
    }

    order = list(L.get("blocks") or ["header", "kpi", "chart", "config", "disk", "comment"])
    if not T.show_config:
        order = [b for b in order if b != "config"]
    if not T.show_disk:
        order = [b for b in order if b != "disk"]
    if not T.show_comment:
        order = [b for b in order if b != "comment"]
    order = [b for b in order if b in H_BLK]

    gaps = L.get("gaps") or {}
    default_gap = L.get("block_gap", 20)
    top = L.get("top_margin", 56)
    H = int(top + sum(H_BLK[b] + gaps.get(b, default_gap) for b in order)) + 20

    img = Image.fromarray(_bg(W, H), "RGB")
    d0 = ImageDraw.Draw(img, "RGBA")

    def TX(x, y, s, font, fill=None, anchor="la"):
        d0.text((x, y), s, font=font, fill=fill or T.text, anchor=anchor)

    if T.grid_step:
        for gx in range(0, W, T.grid_step):
            d0.line([gx, 0, gx, H], fill=(255, 255, 255, T.grid_alpha), width=1)
        for gy in range(0, H, T.grid_step):
            d0.line([0, gy, W, gy], fill=(255, 255, 255, T.grid_alpha), width=1)

    # ---------- 页眉 ----------
    def draw_header(y):
        TX(MX, y, "服务器状态", FT(Fs["title"]), T.title)
        _sub_y = y + Fs["title"] + sub_gap
        TX(MX + 2, _sub_y, "%s  ·  Windows" % d["host"],
           F(Fs["subtitle"]), T.subtitle)
        TX(MX + 2, _sub_y + Fs["subtitle"] + 7,
           "已运行   " + d["up_text_full"],
           F(max(12, Fs["subtitle"] - 2)), T.sub)
        TX(RX, y + 14, datetime.datetime.now().strftime("%Y.%m.%d  %H:%M:%S"),
           F(Fs["time"]), T.subtitle, "ra")
        ly = y + H_BLK["header"] - 8
        d0.rectangle([MX, ly, MX + 90, ly + 4], fill=T.cpu)
        d0.line([MX + 90, ly + 2, RX, ly + 2], fill=(255, 255, 255, 20), width=1)

    # ---------- 三指标 ----------
    def draw_kpi(y):
        mem_sub = "%.1f/%.1fG" % (vm.used / GB, vm.total / GB)
        met = [("CPU 占用", "%d%%" % d["cpu"], T.cpu, ""),
               ("内存占用", "%d%%" % vm.percent, T.mem, mem_sub),
               ("进程总数", "%d" % d["proc_total"], T.proc, "")]
        vy = y + Fs["kpi_label"] + kpi_gap
        for i, (lab, val, col, sub) in enumerate(met):
            x = MX + i * 300
            TX(x, y, lab, F(Fs["kpi_label"]), T.label)
            kf = F(Fs["kpi"], True)
            TX(x, vy, val, kf, col)
            if sub:
                # 数字右边补一句 已用/总量，像 76% 6.1/7.7G
                wv = d0.textlength(val, font=kf)
                TX(x + wv + 14, vy + Fs["kpi"] * 0.60,
                   sub, F(max(12, int(Fs["kpi"] * 0.32))), T.sub)
            if i:
                d0.line([x - 64, y + 6, x - 64, vy + Fs["kpi"] - 6],
                        fill=(255, 255, 255, T.split_alpha), width=1)

    # ---------- 柱状图 ----------
    def draw_chart(y):
        X0, X1 = MX, RX
        MAX_H = L["chart_max_h"]
        BASE_Y = y + MAX_H
        items = d["top"]
        N = max(1, len(items))
        slot = (X1 - X0) / N
        bw = slot * T.bar_width_ratio
        maxv = items[0][0] if items else 1

        for i in range(5):
            gy = BASE_Y - MAX_H * i / 4
            d0.line([X0, gy, X1, gy], fill=(255, 255, 255, 10 if i else 26), width=1)
            TX(X0 - 14, gy, "%.0f" % (maxv * i / 4), F(Fs["axis"]), T.faint, "ra")

        gt = np.array(T.bar_top, np.float32)
        gb = np.array(T.bar_bottom, np.float32)

        for i, (val, name) in enumerate(items):
            cx = X0 + slot * i + slot / 2
            bh = max(8, MAX_H * val / maxv)
            bwi, bhi = int(bw), int(bh)
            x0, yy = int(cx - bw / 2), int(BASE_Y - bh)
            cols = np.linspace(1, 0, bhi)[:, None]
            arr = np.repeat((gb * cols + gt * (1 - cols))[:, None, :],
                            bwi, axis=1).astype(np.uint8)
            mask = Image.new("L", (bwi, bhi), 0)
            ImageDraw.Draw(mask).rounded_rectangle(
                [0, 0, bwi - 1, bhi - 1], radius=T.bar_radius, fill=255)
            img.paste(Image.fromarray(arr, "RGB"), (x0, yy), mask)

            # 柱子太高时把数值压回本块里，别顶到上面那块去
            ly = max(y + 2, yy - 26)
            TX(cx, ly, "%.0f MB" % val, F(Fs["bar_val"], True), T.text, "ma")
            nm = name[:-4] if name.lower().endswith(".exe") else name
            if len(nm) > 13:
                nm = nm[:12] + "…"
            TX(cx, BASE_Y + 14, nm, F(Fs["proc"]), T.sub, "ma")

        d0.line([X0, BASE_Y, X1, BASE_Y], fill=(255, 255, 255, 56), width=2)

    # ---------- 硬件配置 ----------
    def draw_config(y):
        cfg = d["cfg"]
        y1 = y + H_BLK["config"]
        d0.rounded_rectangle([MX, y, RX, y1], radius=T.card_radius,
                             fill=(255, 255, 255, 9))
        d0.rectangle([MX, y + 24, MX + 4, y1 - 24], fill=T.cfg)
        TX(MX + 28, y + 16, "硬件配置", F(Fs["section"]), T.label)

        cells = [
            ("CPU  ·  %s 核 %s 线程" % (cfg["cores"], cfg["threads"]), cfg["cpu"], 320),
            ("内存", cfg["ram"], 265),
            ("显卡", cfg["gpu"], 285),
            ("系统", cfg["os"], 230),
            ("硬盘", cfg["disk"], 200),
        ]
        x = MX + 28
        for i, (lab, val, wid) in enumerate(cells):
            if i:
                d0.line([x - 22, y + 22, x - 22, y1 - 22],
                        fill=(255, 255, 255, 18), width=1)
            TX(x, y + 40, lab, F(Fs["cfg_label"]), T.label)
            fval = F(Fs["cfg_value"], True)
            for li, one in enumerate(str(val).split("\n")[:2]):
                s = one
                while len(s) > 2 and d0.textlength(s, font=fval) > wid - 26:
                    s = s[:-2] + "…"
                TX(x, y + 62 + li * (Fs["cfg_value"] + 2), s, fval, T.text)
            x += wid

    # ---------- 磁盘 + 三个状态值 ----------
    def draw_disk(y):
        y1 = y + H_BLK["disk"]
        d0.rounded_rectangle([MX, y, RX, y1], radius=T.card_radius,
                             fill=(255, 255, 255, 10))
        d0.rectangle([MX, y + 24, MX + 4, y1 - 24], fill=T.disk_c)
        TX(MX + 28, y + 20, "磁盘占用", F(Fs["section"]), T.label)

        fdisk = F(Fs["disk"], True)
        s_c = "C 盘   %.1f / %.1f GB" % (C.used / GB, C.total / GB)
        s_d = "D 盘   %.1f / %.1f GB" % (Dd.used / GB, Dd.total / GB)
        TX(MX + 28, y + 46, s_c, fdisk, T.disk_c)
        TX(MX + 28 + d0.textlength(s_c, font=fdisk) + 60, y + 46, s_d, fdisk, T.disk_d)

        if T.show_mood:
            mx = L.get("mood_x", 940)
            d0.line([mx - 20, y + 18, mx - 20, y1 - 18],
                    fill=(255, 255, 255, 20), width=1)
            mnames = [str(m.get("name", "")).strip() for m in getattr(T, "metrics", [])]
            mnames += ["心情值", "摸鱼值", "赛博寿命"][len(mnames):3]
            items = [
                (mnames[0], d["mood"], d["mood_word"]),
                (mnames[1], d["slack"], pick_word(d["slack"], SLACK_WORDS)),
                (mnames[2], d["life"], pick_word(d["life"], LIFE_WORDS)),
            ]
            fnum, funit = F(Fs["mood_num"], True), F(Fs["mood_unit"])
            for i, (lab, val, word) in enumerate(items):
                x = mx + i * L.get("mood_slot", 180)
                col = pick_color(val, T)
                TX(x, y + 10, lab, F(Fs["mood_label"]), T.label)
                snum = "%d" % val
                nw = d0.textlength(snum, font=fnum)
                uw = d0.textlength("/100", font=funit)
                sx = x + 160 - nw - uw
                TX(sx, y + 4, snum, fnum, col)
                TX(sx + nw, y + 14, "/100", funit, T.faint)
                d0.rounded_rectangle([x, y + 36, x + 160, y + 42], radius=3,
                                     fill=(255, 255, 255, 20))
                fw = max(6, 160 * val / 100.0)
                d0.rounded_rectangle([x, y + 36, x + fw, y + 42], radius=3, fill=col)
                TX(x, y + 50, word, F(Fs["mood_word"]), T.sub)

    # ---------- AI 点评 ----------
    def draw_comment(y):
        persona = Persona()
        msg = comment or persona.comment(d)
        label = who or persona.name
        y1 = y + H_BLK["comment"]
        d0.rounded_rectangle([MX, y, RX, y1], radius=T.card_radius,
                             fill=(255, 255, 255, 10))
        d0.rectangle([MX, y + 24, MX + 4, y1 - 24], fill=T.comment)
        TX(MX + 28, y + 14, "AI 点评  ·  %s" % label, F(Fs["section"]), T.label)
        fnt = F(Fs["comment"])
        lines = _wrap(d0, msg, fnt, 1330)[:2]
        for i, ln in enumerate(lines):
            TX(MX + 28, y + 40 + i * 30, ln, fnt, T.text)

    DRAW = {"header": draw_header, "kpi": draw_kpi, "chart": draw_chart,
            "config": draw_config, "disk": draw_disk, "comment": draw_comment}

    y = top
    for b in order:
        DRAW[b](y)
        y += H_BLK[b] + gaps.get(b, default_gap)

    img.save(out_path, "PNG")
    return out_path


# ------------------------------------------------------------
# 配置覆盖
# ------------------------------------------------------------
def apply_config(cfg):
    """把插件配置里的开关写回 Theme"""
    global HW_CACHE_HOURS

    def get(k, d=None):
        try:
            v = cfg.get(k, d)
            return d if v is None else v
        except Exception:
            return d

    try:
        HW_CACHE_HOURS = int(get("hw_cache_hours", HW_CACHE_HOURS))
    except Exception:
        pass

    if str(get("comment_mode", "")).lower() == "off":
        Theme.show_comment = False
    else:
        Theme.show_comment = bool(get("show_comment", Theme.show_comment))
    Theme.show_mood = bool(get("show_mood", Theme.show_mood))
    Theme.show_config = bool(get("show_config", Theme.show_config))
    Theme.show_disk = bool(get("show_disk", Theme.show_disk))
