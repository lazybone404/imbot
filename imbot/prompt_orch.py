"""Prompt 构建编排器。负责群聊/私聊 prompt 的组装。"""
from astrbot.api import logger

FALLBACK_TIME_CTX = {"date": "未知", "period": "未知", "time_feel": "..."}

TRIGGER_DESCRIPTIONS = {
    "long_silence":         lambda t: f"你已经{t.get('gap_hours','?')}小时没和用户说话了",
    "user_returned":        "用户刚刚回到电脑前",
    "late_night":           lambda t: f"现在是深夜，用户还在电脑前（连续熬夜第{t.get('streak','?')}天）",
    "game_ended":           lambda t: f"用户刚打完游戏（玩了{round(t.get('duration_minutes',0)/60,1)}小时）",
    "late_night_music":     "深夜，用户在听歌",
    "long_session":         lambda t: f"用户已经在电脑前待了{t.get('hours','?')}小时。" + (f"他在{t.get('process','')}，" if t.get('process') else "") + (f"在听{t.get('media','')}，" if t.get('media') else "") + f"现在是{t.get('period','')}。这只是一个观察。",
    "self_lonely":          lambda t: f"今天用户话很少（{t.get('daily_messages','?')}条），你已经运行了{t.get('uptime_hours','?')}小时",
    "self_mood_swing":      lambda t: f"你的情绪从{t.get('from_mood','?')}变成了{t.get('to_mood','?')}",
    "steam_game_ended":     lambda t: f"用户刚退出Steam游戏：{t.get('game_name','') or '游戏'}",
    "steam_achievement":    lambda t: f"用户解锁了Steam成就：{t.get('achievement_name','') or '新成就'}",
    "relationship_milestone": lambda t: f"{t.get('display_name','') or '某人'}和你的关系变得更近了",
    "memory_anchor":        lambda t: f"你忽然想起了一件事：{t.get('summary','') or t.get('seed','')}" + (f"（那时也是{t.get('time_feel','')}）" if t.get('time_feel') else ""),
    "quiet_lifted":         "安静够了。你可以说话了，但你也不知道该说什么",
    "group_mentioned":      lambda t: f"群里有人@了你（群活跃度：{t.get('activity','?')}）",
    "interest_discovery":   lambda t: f"你最近对「{t.get('keyword','')}」有点好奇，自己去查了一下" + (f"，发现：{t.get('summary','')}" if t.get('summary') else ""),
}


class PromptOrchestrator:
    def __init__(self, engine):
        self._e = engine

    async def _build_group_prompt(self, event, speaker_id, speaker_type, time_ctx) -> dict | None:
        from astrbot.api import logger

        mention = self._e.group_perception.detect_mention(event)
        activity = self._e.group_perception.assess_activity(event)

        # 群聊主动回复关闭 + 没被@ → 沉默
        if not self._e.config.group_chat.proactive_in_group and not mention.get("mentioned_imbot"):
            self._e._last_should_silence = True
            self._e.state.record_silence(speaker_id or "unknown")
            return None

        # ── 第一层：前置过滤（仅秘书启用时）──
        secretary_advice = None
        if self._e.secretary and self._e.secretary.cfg.enabled:
            if not self._e.secretary.prefilter(mention, activity):
                # 前置过滤不通过 → 直接沉默
                result = {"tone": "安静", "should_respond": False, "initiative": 0.1}
                self._e._last_motivation_result = result
                self._e._last_should_silence = True
                self._e.state.record_silence(speaker_id or "unknown")
                return None

            # ── 第二层：秘书模型 ──
            try:
                # 获取最近 N 条消息作为上下文（不是当前单条）
                topic = self._e.group_perception.extract_topic(event)
                recent_msgs = ""
                try:
                    uid = event.unified_msg_origin
                    conv_mgr = self._e.plugin_context.conversation_manager
                    curr_cid = await conv_mgr.get_curr_conversation_id(uid)
                    if curr_cid:
                        conv = await conv_mgr.get_conversation(uid, curr_cid)
                        if conv and conv.history:
                            lines = conv.history.strip().split("\n")
                            n = self._e.config.secretary.message_window
                            recent_msgs = "\n".join(lines[-n:])
                except Exception:
                    pass  # 取不到历史就降级到当前消息
                if not recent_msgs:
                    recent_msgs = getattr(event, "message_str", "")[:1000]

                speaker_label = f"{speaker_type}"
                if speaker_id == self._e.config.owner.qq_id:
                    speaker_label = "owner（所有者）"
                ctx = {
                    "messages": recent_msgs[:2000],
                    "speakers": f"当前发言人: {speaker_label}",
                    "topic": topic or "",
                    "activity": activity,
                    "_llm_call": self._e.proactive_orch._llm_generate_lightweight,
                }
                secretary_advice = await self._e.secretary.analyze(ctx)
                if secretary_advice:
                    logger.info(f"秘书建议: respond={secretary_advice['should_respond']}, tone={secretary_advice['tone']}, reason={secretary_advice['reason']}")
            except Exception:
                logger.warning("秘书模型调用异常，降级到纯动机", exc_info=True)

        # ── 第三层：动机系统 ──
        # 强制闭嘴规则（始终生效，命中后直接沉默）
        context_for_rules = {
            "topic": self._e.group_perception.extract_topic(event),
            "messages": getattr(event, "message_str", ""),
        }
        if self._e.secretary:
            blocked = self._e.secretary.check_silence_rules(context_for_rules)
            if blocked:
                logger.debug(f"群聊闭嘴规则命中: {blocked}")
                self._e._last_should_silence = True
                self._e.state.record_silence(speaker_id or "unknown")
                return None

        # 秘书规则硬约束
        if secretary_advice and secretary_advice["should_respond"]:
            if self._e.secretary:
                blocked = self._e.secretary.check_rules(secretary_advice, context_for_rules)
                if blocked:
                    logger.debug(f"群聊秘书规则禁止: {blocked}")
                    secretary_advice = None  # 规则禁止，忽略秘书建议

        result = self._e.motivation.evaluate(
            self._e.state, time_ctx, speaker_type, is_group=True,
            secretary_advice=secretary_advice,
            group_proactive_level=self._e.config.group_chat.proactive_level,
        )
        self._e._last_motivation_result = result

        should_respond = result["should_respond"]

        # 秘书建议的 tone 覆盖（如果动机没给出更明确的语气）
        if secretary_advice and secretary_advice.get("tone") and result.get("tone", "") in ("安静", ""):
            result["tone"] = secretary_advice["tone"]

        # 秘书强制模式
        if secretary_advice and secretary_advice["should_respond"]:
            if self._e.secretary and not self._e.secretary.cfg.allow_motivation_override:
                should_respond = True
                result["should_respond"] = True
                logger.debug("秘书强制回应（allow_motivation_override=false）")

        # 连续沉默过多 → 强制执行一次回应（安全网）
        if not should_respond and self._check_silence_counter(speaker_id):
            should_respond = True

        owner_present = speaker_type == "owner"
        if mention.get("mentioned_owner") and not owner_present:
            owner_present = True

        self._e._last_should_silence = not should_respond

        if not should_respond:
            self._e.state.record_silence(speaker_id or "unknown")
            logger.debug(f"群聊沉默: activity={activity}, mentioned={mention['mentioned_imbot']}")
            return None

        logger.info(f"群聊回应: tone={result['tone']}, initiative={result['initiative']:.1f}")
        # 群记忆
        group_id = event.get_group_id()
        topic = self._e.group_perception.extract_topic(event)
        try:
            if self._e.group_memory:
                self._e.group_memory.apply_decay(group_id)
                memories = self._e.group_memory.retrieve_relevant(group_id, limit=3, group_topic=topic)
            else:
                memories = []
        except Exception:
            memories = []

        # 社交语境
        social_ctx = self._build_social_context(speaker_id, is_group=True)

        rules_text = self._e.rules.format_for_prompt(is_group=True, speaker_type=speaker_type) if self._e.config.core.inject_rules else ""

        # ── 动态上下文 ──
        group_context = self._format_group_context(activity, mention, memories, result["tone"])
        group_context += (
            "\n\n[群聊风格]\n"
            "你在群里，不是客服。发言要像群友一样随意：\n"
            "- 一般只回几个字到一句话，不写完整段落\n"
            "- 别人用什么语气你就用什么语气\n"
            "- 群友玩梗你就接梗，群友严肃你就认真\n"
            "- 可以只回「草」「确实」「6」这种超短回应\n"
            "- 不要每条都回，挑你感兴趣的\n"
        )
        emotion_residue = self._e.memory.format_emotion_residue() if self._e.memory else ""
        if emotion_residue:
            group_context += "\n" + emotion_residue
        if social_ctx:
            group_context += "\n\n" + social_ctx
        perception_text = self._build_perception_text()
        time_text = f"{time_ctx['date']} {time_ctx['weekday']} {time_ctx['time']}，{time_ctx['time_feel']}"
        self_state_text = f"情绪：{self._e.state.mood}，精力：{'充足' if self._e.state.energy > 0.6 else '一般' if self._e.state.energy > 0.3 else '很累'}"
        if perception_text:
            self_state_text += f"。{perception_text}"

        dynamic = self._e.prompt.build_dynamic(
            speaking_style=self._build_speaking_style_text(),
            time_context=time_text,
            self_state=self_state_text,
            tone=result["tone"],
            context=group_context,
        )

        # ── 静态 Prompt ──
        try:
            static = self._e.prompt.build(
                is_group=True,
                speaker_type=speaker_type,
                owner_present="是" if owner_present else "否",
                rules=rules_text,
            )
            return {"static": static, "dynamic": dynamic}
        except Exception:
            logger.error("模板渲染失败", exc_info=True)
            return {"static": self._fallback_prompt(), "dynamic": ""}

    async def _build_private_prompt(self, event, speaker_id, speaker_type, time_ctx) -> dict | None:
        from astrbot.api import logger

        result = self._e.motivation.evaluate(self._e.state, time_ctx, speaker_type, is_group=False)
        self._e._last_motivation_result = result

        should_respond = result["should_respond"]
        # 连续沉默过多 → 强制执行一次回应
        if not should_respond and self._check_silence_counter(speaker_id):
            should_respond = True

        self._e._last_should_silence = not should_respond

        if not should_respond:
            if speaker_id:
                self._e.state.record_silence(speaker_id)
            logger.debug(f"私聊沉默: tone={result['tone']}, speaker={speaker_type}")
            return None

        logger.debug(f"私聊回应: tone={result['tone']}, stage={self._e.state.get_stage(speaker_id)}")
        # 阶段
        stage = self._e.state.get_stage(speaker_id)

        # 记忆
        try:
            memories = self._e.memory.retrieve_relevant(
                time_period=time_ctx["period"],
                mood=self._e.state.mood,
                user_id=speaker_id,
                limit=self._e.config.prompts.max_memories_in_prompt,
            )
        except Exception:
            memories = []

        # 社交语境
        social_ctx = self._build_social_context(speaker_id, is_group=False)
        owner_self_ctx = self._e.social_world.format_self_context() if (speaker_type == "owner" and self._e.social_world) else ""

        # 情绪残留
        emotion_residue = self._e.memory.format_emotion_residue() if self._e.memory else ""

        # 规则
        rules_text = self._e.rules.format_for_prompt(is_group=False, speaker_type=speaker_type) if self._e.config.core.inject_rules else ""

        # ── 动态上下文 ──
        context = self._format_private_context(stage, memories)
        if emotion_residue:
            context += "\n" + emotion_residue
        if social_ctx:
            context += "\n\n" + social_ctx
        if owner_self_ctx:
            context += "\n\n" + owner_self_ctx
        perception_text = self._build_perception_text()
        time_text = f"{time_ctx['date']} {time_ctx['weekday']} {time_ctx['time']}，{time_ctx['time_feel']}"
        self_state_text = f"情绪：{self._e.state.mood}，精力：{'充足' if self._e.state.energy > 0.6 else '一般' if self._e.state.energy > 0.3 else '很累'}"
        if perception_text:
            self_state_text += f"。{perception_text}"

        dynamic = self._e.prompt.build_dynamic(
            speaking_style=self._build_speaking_style_text(),
            time_context=time_text,
            self_state=self_state_text,
            tone=result["tone"],
            context=context,
        )

        # ── 静态 Prompt ──
        try:
            static = self._e.prompt.build(
                speaker_type=speaker_type,
                is_group=False,
                rules=rules_text,
            )
            return {"static": static, "dynamic": dynamic}
        except Exception:
            logger.error("模板渲染失败", exc_info=True)
            return {"static": self._fallback_prompt(), "dynamic": ""}

    def _format_group_context(self, activity: str, mention: dict,
                              memories: list[dict], tone: str) -> str:
        parts = [f"群活跃度: {activity}"]
        if mention.get("mentioned_imbot"):
            parts.append("有人@了你")
        if mention.get("mentioned_owner"):
            parts.append("所有者在群里")
        if memories:
            formatted = self._e.memory.format_memories(memories, is_group=True)
            parts.append(formatted)
        return "\n".join(parts)

    def _format_private_context(self, stage: str, memories: list[dict]) -> str:
        parts = [f"你们的关系阶段: {stage}"]
        if memories:
            formatted = self._e.memory.format_memories(memories, is_group=False)
            parts.append(formatted)
        return "\n".join(parts)

    def _build_perception_text(self) -> str:
        if not self._e._current_perception:
            return ""
        p = self._e._current_perception
        parts = []
        if p.get("primary_context"):
            parts.append(p["primary_context"])
        if p.get("jun_attention"):
            parts.append(p["jun_attention"])
        return "。".join(parts) if parts else ""

    def _build_social_context(self, speaker_id: str, is_group: bool) -> str:
        if not self._e.social_world:
            return ""
        return self._e.social_world.format_context_for(speaker_id, is_group)

    def _build_speaking_style_text(self) -> str:
        """格式化说话方式为注入文本"""
        ss = self._e.config.speaking_style
        if not ss.enabled:
            return ""
        parts = []
        if ss.verbosity == "terse":
            parts.append("说话极简短，尽量只回一两个字")
        elif ss.verbosity == "verbose":
            parts.append("可以多说几句，完整表达你的想法")
        if ss.formality == "casual":
            parts.append("像朋友闲聊，可以用语气词")
        elif ss.formality == "formal":
            parts.append("保持一定距离感，不用太亲密")
        if ss.emoji_usage == "never":
            parts.append("不要使用任何 emoji 表情符号")
        elif ss.emoji_usage == "rarely":
            parts.append("偶尔可以用 emoji")
        elif ss.emoji_usage == "sometimes":
            parts.append("可以用 emoji 表达情绪")
        if ss.sentence_style == "short":
            parts.append("使用短句，像随意聊天")
        elif ss.sentence_style == "long":
            parts.append("可以使用长句表达完整想法")
        return "。".join(parts) if parts else ""

    @staticmethod
    def _fallback_prompt() -> str:
        return """简短随性地说话。禁止讨好、客服语气、长篇分析。"""

    def _check_silence_counter(self, user_id: str) -> bool:
        """返回 True 表示应该强制回应（连续沉默过多 → 覆盖动机沉默）"""
        if not user_id:
            return False
        return self._e.state.should_force_respond(user_id)

    @staticmethod
    def _extract_mood(text: str) -> str:
        keywords = {
            "烦躁": ["烦", "吵", "别", "不想"],
            "低落": ["算了", "没意思", "唉"],
            "好奇": ["什么", "为什么", "怎么"],
            "开心": ["哈", "笑", "好"],
            "疲惫": ["困", "累", "睡"],
        }
        for mood, words in keywords.items():
            if any(w in text for w in words):
                return mood
        return "安静"
