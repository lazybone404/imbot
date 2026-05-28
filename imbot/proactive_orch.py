"""主动互动编排器。负责主动层循环和消息执行。"""
import asyncio
import time
import random
from imbot.utils import parse_llm_json, call_lightweight_llm, parse_time_window
from imbot.prompt_orch import TRIGGER_DESCRIPTIONS


class ProactiveOrchestrator:
    def __init__(self, engine):
        self._e = engine

    async def _proactive_loop(self):
        await asyncio.sleep(90)  # 等感知层积累初始数据
        while True:
            await asyncio.sleep(30)
            # 后台：为待生成 seed 的记忆调用 LLM 摘要 + 计算 embedding（独立于主动层）
            if self._e.memory:
                await self._e.memory.generate_pending_seeds() if self._e.memory else None
                await self._e.memory.compute_embeddings_pending()
            if not self._e.proactive or not self._e.perception:
                continue

            # 忽略检测：上次主动发送后用户完全没回应 → 计一次忽略
            now = time.time()
            if self._e.state._last_proactive_sent > 0:
                gap = now - self._e.state._last_proactive_sent
                if gap > 600:  # 10 分钟
                    oid = self._e.config.owner.qq_id
                    last_user_msg = self._e.state._last_interaction.get(oid, 0)
                    if last_user_msg < self._e.state._last_proactive_sent:
                        self._e.proactive.on_proactive_ignored()
                    self._e.state._last_proactive_sent = 0  # 标记已处理

            # 兴趣捕获（感知 → 兴趣池）
            if self._e.interests and self._e.perception:
                media = self._e.perception.media
                steam = self._e.perception.steam
                enriched = {
                    "player_name": media.player_name if (media and media.is_playing) else "",
                    "is_playing": media.is_playing if media else False,
                    "game_name": steam.current_game if (steam and steam.is_playing()) else "",
                }
                self._e.interests.tick(enriched)
                captured = await self._e.interests.maybe_capture()
                for kw in captured:
                    from astrbot.api import logger
                    logger.info(f"兴趣捕获: {kw}")

            # 私聊主动
            ctx = self._e.proactive.check_triggers()
            if ctx:
                await self._execute_proactive(ctx)
            # 熟人主动（亲近及以上）
            kctx = self._e.proactive.check_known_targets()
            if kctx:
                await self._execute_proactive(kctx)
            # 兴趣探索
            if self._e.interests:
                ictx = self._e.proactive.check_interest_triggers()
                if ictx:
                    await self._execute_interest_explore(ictx)

            # ── 主动层心跳保底 ──
            hb = self._e.config.proactive.heartbeat
            if self._e.proactive and self._e.proactive.cfg.enabled and hb.enabled:
                now = time.time()
                today = time.strftime("%Y-%m-%d")
                if getattr(self._e, "_hb_date", "") != today:
                    self._e._hb_today_count = 0
                    self._e._hb_date = today
                if self._e._hb_today_count < hb.daily_min:
                    if parse_time_window(hb.window_start, hb.window_end):
                        saved_quiet = self._e.proactive._quiet_until
                        saved_ignore = self._e.proactive._ignore_streak
                        self._e.proactive._quiet_until = 0
                        self._e.proactive._ignore_streak = 0
                        ctx = self._e.proactive.check_triggers()
                        self._e.proactive._quiet_until = saved_quiet
                        self._e.proactive._ignore_streak = saved_ignore
                        if ctx:
                            await self._execute_proactive(ctx)
                            self._e._hb_today_count += 1

    async def _execute_proactive(self, trigger_ctx: dict):
        from astrbot.api import logger
        try:
            prompt = self._build_proactive_prompt(trigger_ctx)
            if not prompt:
                return

            self._e._is_proactive = True
            self._e._resolve_platform_id()  # 每次发送前刷新平台 ID

            # 使用 trigger 指定的目标用户（默认 owner）；群聊用 group_id
            is_group = trigger_ctx.get("is_group", False)
            first = trigger_ctx.get("triggers", [{}])[0]
            if is_group:
                target_id = first.get("group_id", "")
                umo = f"{self._e._platform_id}:GroupMessage:{target_id}"
            else:
                target_id = first.get("target_user_id", self._e.config.owner.qq_id)
                umo = f"{self._e._platform_id}:FriendMessage:{target_id}"
                logger.debug(f"主动消息 UMO: {umo} (platform_id={self._e._platform_id})")
            if not target_id:
                logger.warning("主动消息: 无目标用户")
                return

            # 调用 LLM
            try:
                response_text = await self._call_llm(umo, prompt)
            except Exception:
                logger.error("主动消息: LLM 调用失败", exc_info=True)
                return

            if not response_text:
                return

            # 清洗 + 分段 + 发送
            if self._e.segmentation and hasattr(self._e.segmentation, "clean"):
                response_text = self._e.segmentation.clean(response_text)

            from astrbot.api.event import MessageChain
            try:
                from astrbot.core.message.components import Plain
            except ImportError:
                class Plain:
                    def __init__(self, text): self.text = text

            seg_result = self._e.segmentation.segment(response_text, {}) if self._e.segmentation else None
            if seg_result:
                segments = seg_result["segments"]
                delays = seg_result["delays"]
                fillers = seg_result["fillers"]
                for i, seg in enumerate(segments):
                    prefix = fillers[i] + " " if fillers[i] else ""
                    text = prefix + seg
                    if i > 0 and delays[i] > 0:
                        await asyncio.sleep(delays[i])
                    try:
                        await self._e.plugin_context.send_message(umo, MessageChain([Plain(text)]))
                    except Exception:
                        logger.error("主动消息发送失败", exc_info=True)
            else:
                try:
                    await self._e.plugin_context.send_message(
                        umo, MessageChain([Plain(response_text)])
                    )
                except Exception:
                    logger.error("主动消息发送失败", exc_info=True)

            self._e.proactive.execute(trigger_ctx)
            self._e.state._last_proactive_sent = time.time()

            desc = ", ".join(t["type"] for t in trigger_ctx.get("triggers", []))
            scene = "群聊" if is_group else "私聊"
            action = "主动互动" if not is_group else "主动回复"
            logger.info(f"{action}触发: [{scene}] {desc} → {target_id}")
        except Exception:
            logger.error("主动消息执行失败", exc_info=True)
        finally:
            self._e._is_proactive = False
            self._e._last_was_proactive = True

    async def _get_owner_umo(self) -> str | None:
        """获取 owner 的 unified_msg_origin。
        格式: {platform_id}:{MessageType}:{session_id}
        如: aiocqhttp:FriendMessage:123456
        """
        owner_id = self._e.config.owner.qq_id
        if not owner_id:
            return None
        return f"aiocqhttp:FriendMessage:{owner_id}"

    async def _call_llm(self, umo: str, prompt: str) -> str:
        """通过 AstrBot 官方 Context.llm_generate() API 调用 LLM (v4.5.7+)"""
        from astrbot.api import logger
        try:
            chat_provider_id = await self._e.plugin_context.get_current_chat_provider_id(umo)
            resp = await self._e.plugin_context.llm_generate(
                chat_provider_id=chat_provider_id,
                prompt=prompt,
            )
            if resp and resp.completion_text:
                return resp.completion_text.strip()
            if resp and resp.role == "err":
                logger.error(f"LLM 返回错误")
        except Exception:
            logger.error("LLM 调用失败", exc_info=True)
        return ""

    async def _execute_interest_explore(self, trigger_ctx: dict):
        """兴趣探索：调用轻量 LLM，让它用原生搜索工具查 + 判断趣味性"""
        from astrbot.api import logger
        try:
            keyword = trigger_ctx.get("keyword", "")
            if not keyword:
                return

            # 一次 LLM 调用：搜索 + 趣味性判断
            prompt = (
                f"你对「{keyword}」很好奇。先调用搜索工具查一下相关的新鲜事，"
                "再基于搜索结果判断是否值得跟朋友分享。\n"
                "回复 JSON：{\"interesting\": true/false, \"one_line\": \"一句话总结\"}\n"
                "只输出 JSON。"
            )
            interesting = False
            one_line = ""
            result = await call_lightweight_llm(self, prompt)
            if result:
                data = parse_llm_json(result)
                if data:
                    interesting = data.get("interesting", False)
                    one_line = data.get("one_line", "")

            self._e.interests.record_exploration(keyword, interesting, one_line)

            if not interesting:
                return

            trigger_ctx["is_group"] = False
            for t in trigger_ctx["triggers"]:
                if t["type"] == "interest_discovery":
                    t["summary"] = one_line
                    break
            else:
                trigger_ctx["triggers"].append({
                    "type": "interest_discovery",
                    "keyword": keyword,
                    "summary": one_line,
                    "category": "sharing",
                    "target_user_id": self._e.config.owner.qq_id,
                })
            await self._execute_proactive(trigger_ctx)
            self._e.interests.record_share(keyword)

        except Exception:
            logger.error("兴趣探索执行失败", exc_info=True)

    async def _llm_generate_lightweight(self, prompt: str) -> str:
        """轻量 LLM 调用——用于兴趣语义提取和趣味性判断"""
        from astrbot.api import logger
        try:
            owner_id = self._e.config.owner.qq_id or "unknown"
            configured = self._e.config.models.summarization
            if configured:
                chat_provider_id = configured
            else:
                umo = f"aiocqhttp:FriendMessage:{owner_id}"
                chat_provider_id = await self._e.plugin_context.get_current_chat_provider_id(umo)
            resp = await self._e.plugin_context.llm_generate(
                chat_provider_id=chat_provider_id,
                prompt=prompt,
                system_prompt="只输出请求的内容，不加任何说明。",
            )
            if resp and resp.completion_text:
                return resp.completion_text.strip()
        except Exception:
            logger.warning("轻量 LLM 调用失败", exc_info=True)
        return ""

    def _build_proactive_prompt(self, trigger_ctx: dict) -> str:
        triggers = trigger_ctx["triggers"]
        tone = trigger_ctx.get("tone", "安静")
        is_group = trigger_ctx.get("is_group", False)

        def _describe(t: dict) -> str:
            ttype = t["type"]
            desc = TRIGGER_DESCRIPTIONS.get(ttype)
            if callable(desc):
                return desc(t)
            return desc or ttype

        reasons = [_describe(t) for t in triggers]
        categories = list({t.get("category", "") for t in triggers if t.get("category")})

        # 类别专属语气引导
        guidance = {
            "care": "你有点在意，但不是为了对方——你只是自己想说。不说\"早点睡\"，不说\"注意身体\"。可以带点不耐烦，也可以只是陈述一个观察。",
            "curiosity": "随口一问，不在意对方答不答。不问\"去哪了\"，不问\"在干嘛\"。更像是对空气说了一句，碰巧被听到了。",
            "sharing": "你不是在跟对方说话——你是在跟自己说话。有感而发，不期待回应。可以只说半句，可以让句子断掉。",
        }
        primary = "care"
        for cat in ("sharing", "care", "curiosity"):
            if cat in categories:
                primary = cat
                break
        guidance_text = guidance.get(primary, "")
        if len(triggers) > 1:
            guidance_text += " 多个原因叠加在一起，试着自然地都带到——不用刻意，像随口提到几件事。"

        # 注入核心规则（和正常聊天一致，确保说话方式不违反规则）
        rules_text = ""
        if self._e.config.core.inject_rules:
            rules_text = self._e.rules.format_for_prompt(is_group=is_group, speaker_type="owner")

        # 可选话题：从兴趣池随机取 1-2 个，LLM 可自然融入
        topic_hint = ""
        interests = getattr(self._e, "interests", None)
        if interests:
            active = interests.list_active()
            if active:
                kws = [i["keyword"] for i in random.sample(active, min(2, len(active)))]
                topic_hint = f"\n你最近对{'、'.join(kws)}有点好奇。如果自然的话可以带一句，不用勉强。"

        # 用户自定义的语气倾向
        tendency_map = {
            "natural": "", "随性": "语气随意，想到什么说什么。",
            "关心": "语气温和一点，稍微在意。", "吐槽": "可以损人，可以无语。",
            "好奇": "像发现了有意思的事，随口一问。", "安静": "少说几句，只说半句也行。",
        }
        user_hint = tendency_map.get(self._e.config.proactive.tendency, "")
        if self._e.config.proactive.custom_hint.strip():
            user_hint += "\n" + self._e.config.proactive.custom_hint.strip()

        return (
            rules_text
            + "\n\n现在你因为以下原因想主动说点什么：\n\n"
            + "\n".join(f"- {r}" for r in reasons)
            + f"\n\n当前语气倾向：{tone}\n\n"
            + guidance_text
            + topic_hint
            + ("\n" + user_hint if user_hint else "")
        )
