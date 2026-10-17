# -*- coding: utf-8 -*-
"""服务器状态卡 —— 把主机状态画成一张图，底部带 AI 点评。"""

import asyncio
import os
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .card import apply_config, collect, render

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(PLUGIN_DIR, "out")

# livingmemory 的 register 名和目录名都试一遍
LMEM_NAMES = ("astrbot_plugin_livingmemory", "LivingMemory")


@register(
    "astrbot_plugin_server_card",
    "Full",
    "把主机状态画成一张深色卡片，底部带 AI 点评",
    "1.1.1",
)
class ServerCard(Star):

    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config or {}
        os.makedirs(OUT_DIR, exist_ok=True)

    # ---------------- 配置 ----------------
    def _cfg(self, key, default=None):
        try:
            v = self.config.get(key, default)
            return default if v is None else v
        except Exception:
            return default

    def _text(self, key, default=""):
        return str(self._cfg(key, default) or "").strip()

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
            logger.warning("[server_card] 没找到 livingmemory，上下文跳过")
            return None
        handler = getattr(inst, "event_handler", None)
        cm = getattr(handler, "conversation_manager", None)
        if cm is None:
            logger.warning("[server_card] livingmemory 还没初始化好，上下文跳过")
            return None
        try:
            sid = event.unified_msg_origin
            msgs = await cm.get_context(session_id=sid, max_messages=limit)
            return msgs or None
        except Exception as e:
            logger.warning("[server_card] 取 livingmemory 上下文失败: %s" % e)
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
            logger.warning("[server_card] 取 AstrBot 上下文失败: %s" % e)
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
            logger.info("[server_card] 上下文为空，按无上下文点评")
        else:
            logger.info("[server_card] 取到上下文 %d 条" % len(ctx))
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
                logger.warning("[server_card] 没有可用模型，退回规则点评")
                return None
            resp = await provider.text_chat(
                prompt=prompt,
                contexts=ctx or [],
                system_prompt="" if name else "",
            )
            text = (getattr(resp, "completion_text", "") or "").strip()
            return text or None
        except Exception as e:
            logger.warning("[server_card] LLM 点评失败(%s)，退回规则点评" % e)
            return None

    # ---------------- 指令 ----------------
    @filter.command("服务器状态卡", alias={"状态卡", "出状态卡"})
    async def make_card(self, event: AstrMessageEvent):
        """服务器状态卡：出一张主机状态图"""
        t0 = time.time()
        try:
            apply_config(self.config)
            data = await asyncio.to_thread(collect)
            comment = await self._comment(event, data)
            who = self._text("persona_name") or None

            out = os.path.join(OUT_DIR, "card_%d.png" % int(time.time() * 1000))
            await asyncio.to_thread(render, data, out, comment, who)

            logger.info("[server_card] 出图 %.2fs -> %s" % (time.time() - t0, out))
            yield event.image_result(out)
        except Exception as e:
            logger.error("[server_card] 出图失败: %s" % e)
            yield event.plain_result("状态卡出图失败了：%s" % e)
