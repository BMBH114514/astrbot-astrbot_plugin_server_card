# -*- coding: utf-8 -*-
"""服务器状态卡 —— 把主机状态画成一张图，底部带 AI 点评。"""

import asyncio
import datetime
import glob
import json
import shutil
import os
import random
import re
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .card import apply_config, collect, render
from . import freedraw

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
BACKUP_DIR = os.path.join(STYLES_DIR, "_backup")

# livingmemory 的 register 名和目录名都试一遍
PLUGIN_VERSION = "1.6.0"

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
        logger.info(
            "[状态卡·FULL] 服务器状态卡 v%s 已加载 · 作者 %s" % (PLUGIN_VERSION, PLUGIN_AUTHOR)
        )
        logger.info("[状态卡·FULL] 风格目录 %s" % STYLES_DIR)
        self._sync_style_options()

    # ---------------- 配置 ----------------
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

    # ---------------- 风格 ----------------
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
        safe = re.sub(r"[^\w\-\u4e00-\u9fa5]", "", str(pid)) or "persona"
        return os.path.join(STYLES_DIR, "persona_" + safe + ".json"), pid

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
             json.dumps(current, ensure_ascii=False), self._STYLE_HINT)

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

        hint = ("填 styles/ 里的文件名，不用带 .json。现有的：%s。"
                "自己写一份丢进去也能直接填名字用。") % "、".join(names)

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

        hint = ("三选一：留空就走 JSON 样式；填 random 就从所有 code_*.py 里随机抽，"
                "每次出图都不一样；填文件名（或用逗号隔开几个）就在这几个里随机抽。")
        if names:
            hint += "现有代码：%s。" % "、".join(names)

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
    def _prompt(self, d, name, has_ctx):
        cfg = d["cfg"]
        body = (
            "CPU %d%%，内存 %d%%，进程 %d 个，已开机 %d 天，"
            "C 盘剩余 %.1fG，显卡 %s，心情值 %d，摸鱼值 %d。"
            % (d["cpu"], d["mem_percent"], d["proc_total"], d["up_days"],
               d["free_c"], cfg.get("gpu", "未知"), d["mood"], d["slack"])
        )
        head = "你正在给一台电脑出状态卡，用一句话点评，不超过 55 个字，口语，别用括号别用表情，别复述数字。"
        if name:
            head = "你现在扮演「%s」。%s" % (name, head)
        if has_ctx:
            head += "\n下面是你们最近的聊天，参考这个语气说话，但别直接引用。"
        return "%s\n这台机器：%s\n只输出这句话。" % (head, body)

    async def _comment(self, event, d):
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
        prompt = self._prompt(d, name, bool(ctx))

        try:
            provider = self.context.get_using_provider()
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
            return None

    # ---------------- 指令 ----------------
    @filter.command("服务器状态卡", alias={"状态卡", "出状态卡"})
    async def make_card(self, event: AstrMessageEvent):
        """服务器状态卡：出一张主机状态图"""
        t0 = time.time()
        logger.info("[状态卡·FULL] %s 调用了 /状态卡" % self._whoami(event))
        try:
            style = await self._resolve_style()
            apply_config(self.config)
            data = await asyncio.to_thread(collect)
            comment = await self._comment(event, data)
            who = self._text("persona_name") or None

            out = os.path.join(OUT_DIR, "card_%d.png" % int(time.time() * 1000))
            code_path = self._code_style_path()
            if code_path:
                with open(code_path, encoding="utf-8") as f:
                    code = f.read()
                await asyncio.to_thread(freedraw.run, code, data, out, comment, who)
                logger.info("[状态卡·FULL] 用代码风格 %s 画完" % os.path.basename(code_path))
            else:
                await asyncio.to_thread(render, data, out, comment, who)
                self._backup_style(style)

            logger.info("[状态卡·FULL] 出图 %.2fs -> %s" % (time.time() - t0, out))
            yield event.image_result(out)
        except Exception as e:
            logger.error("[状态卡·FULL] 出图失败: %s" % e)
            yield event.plain_result("状态卡出图失败了：%s" % e)

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
            self._STYLE_HINT,
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
            yield event.plain_result("模型调用失败了：%s" % e)
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
            yield event.plain_result("风格文件写不进去：%s" % e)
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
            yield event.plain_result("风格存好了（%s），但预览出图失败：%s" % (name, e))
            return
        finally:
            _theme.reset()

        tip = "已存成风格 %s，去配置里把「卡片风格名」填成 %s 就生效" % (name, name)
        if notes:
            tip += "（%s）" % "；".join(notes[:3])
        yield event.plain_result(tip)
        yield event.image_result(out)

    async def _make_code(self, event, desc="", mood=""):
        """让模型写一版绘图代码，跑通就返回 (code, 图片路径, 错误)

        点评和写代码互不依赖，直接同时跑，省掉一半等待。
        """
        t0 = time.time()
        provider = self.context.get_using_provider()
        if provider is None:
            return None, "", "没有可用模型"

        apply_config(self.config)
        data = await asyncio.to_thread(collect)
        t_collect = time.time() - t0

        async def _get_comment():
            try:
                return await asyncio.wait_for(self._comment(event, data), timeout=45)
            except Exception as e:
                logger.warning("[状态卡·FULL] 点评没拿到，先画图: %s" % e)
                return None

        comment_task = asyncio.create_task(_get_comment())
        who = self._text("persona_name") or None

        prompt = freedraw.HINT
        if mood:
            prompt += "\n这次的视觉方向：%s\n排版、位置、留白全按这个方向来，别套默认模板。\n" % mood
        if (desc or "").strip():
            prompt += "\n额外要求：%s\n" % desc.strip()

        code, last_err, out = None, "", ""
        t_code = 0.0
        for attempt in range(2):
            try:
                ask = prompt if not last_err else (
                    prompt + "\n上次这版跑挂了：%s\n按报错改好，发一版完整代码。" % last_err)
                tc = time.time()
                resp = await asyncio.wait_for(
                    provider.text_chat(
                        prompt=ask,
                        system_prompt="你是 Python + Pillow 绘图工程师，只输出代码，不要解释。",
                    ), timeout=60)
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
                await asyncio.to_thread(freedraw.run, code, data, out, comment, who)
                logger.info("[状态卡·FULL] 第 %d 版跑通 · 采集 %.1fs · 模型 %.1fs · 总 %.1fs"
                            % (attempt + 1, t_collect, t_code, time.time() - t0))
                return code, out, ""
            except Exception as e:
                last_err = str(e)[:200]
                code = None
                logger.warning("[状态卡·FULL] 第 %d 版没跑通（已用 %.1fs）: %s"
                               % (attempt + 1, time.time() - t0, last_err))

        if not comment_task.done():
            comment_task.cancel()
        logger.warning("[状态卡·FULL] 两版都没跑通，总耗时 %.1fs" % (time.time() - t0))
        return None, "", last_err

    def _save_code(self, code, prefix="code"):
        name = "%s_%s" % (prefix, datetime.datetime.now().strftime("%m%d_%H%M%S"))
        path = os.path.join(STYLES_DIR, name + ".py")
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
        mood = random.choice(FREE_MOODS)
        t0 = time.time()
        logger.info("[状态卡·FULL] %s 调用了 /状态卡重做风格 · 抽到方向：%s"
                    % (self._whoami(event), mood))

        code, out, err = await self._make_code(event, desc, mood)
        if not code:
            yield event.plain_result("两版都没跑通：%s" % err)
            return

        name = self._save_code(code, "code")
        yield event.plain_result(
            "方向：%s\n跑通了，存成 %s.py，用了 %.1fs" % (mood, name, time.time() - t0))
        yield event.image_result(out)

    @filter.command("状态卡自由生成", alias={"自由生成", "代码生成卡片", "状态卡代码"})
    async def gen_code(self, event: AstrMessageEvent, desc: str = ""):
        """状态卡自由生成：让 AI 直接写绘图代码，想画什么画什么"""
        t0 = time.time()
        logger.info("[状态卡·FULL] %s 调用了 /状态卡自由生成 · %s"
                    % (self._whoami(event), (desc or "无要求")[:40]))
        code, out, err = await self._make_code(event, desc, "")
        if not code:
            yield event.plain_result("两版都没跑通：%s" % err)
            return

        name = self._save_code(code, "code")
        logger.info("[状态卡·FULL] 代码风格 %s 跑通，用时 %.1fs" % (name, time.time() - t0))
        yield event.plain_result(
            "跑通了，存成 %s.py，用了 %.1fs\n想固定用它去配置的「代码风格文件」选上"
            % (name, time.time() - t0))
        yield event.image_result(out)
