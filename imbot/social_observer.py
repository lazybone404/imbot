import re
import time


POSITIVE_SIGNALS = ["帮了大忙", "挺好的", "不错", "靠谱", "谢了", "真厉害", "多亏了"]
NEGATIVE_SIGNALS = ["他又", "烦死了", "别理", "无语", "受不了", "算了", "随便"]
CLOSE_SIGNALS = ["互怼", "开玩笑", "损", "熟", "死党", "哥们", "闺蜜"]

# 信号权重：直接互动 > 参与对话 > 旁观
WEIGHT_DIRECT = 0.8   # 直接@imbot
WEIGHT_ENGAGED = 0.5  # imbot 参与对话但未被直接@
WEIGHT_SIDELINE = 0.2  # 纯旁观


class SocialObserver:
    def __init__(self, social_world):
        self.world = social_world
        self._interaction_cache: dict[tuple, int] = {}
        # 降频计数器：每个 user_id 的消息计数，每 10 条触发一次画像更新
        self._profile_update_counter: dict[str, int] = {}

    def observe(self, event, response_text: str, is_group: bool) -> list[dict]:
        observations = []
        msg = getattr(event, "message_str", "") or ""
        sender_id = event.get_sender_id()
        sender_name = event.get_sender_name() or ""
        group_id = event.get_group_id() if is_group else ""

        # 权重判定：被@ → direct；群内有回复 → engaged；其余旁观
        mentioned_imbot = self._is_mentioned_imbot(event)
        weight = WEIGHT_DIRECT if mentioned_imbot else (WEIGHT_ENGAGED if response_text else WEIGHT_SIDELINE)

        # ① 换名检测
        if sender_id and sender_name:
            obs = self._detect_name_change(sender_id, sender_name)
            if obs:
                observations.append(obs)

        # ② 基础交互（含消息文本用于上下文缓冲）
        if sender_id:
            observations.append({
                "type": "interaction",
                "user_id": sender_id,
                "group_id": group_id,
                "message_text": msg,
            })

        # ③ 群聊：@ 检测
        if is_group:
            observations.extend(self._detect_mentions(event, sender_id))

        # ④ 语气信号（带权重）
        observations.extend(self._detect_tone(msg, sender_id, response_text, weight))

        # ⑤ 画像更新（降频：每 10 条消息触发一次）
        observations.extend(self._detect_self_profile_clues(event, msg, is_group))

        if observations:
            from astrbot.api import logger
            types = set(o["type"] for o in observations)
            logger.debug(f"社交观察: {len(observations)}条信号, 类型={types}")
        return observations

    # ── 内部 ──
    def _is_mentioned_imbot(self, event) -> bool:
        """检测消息是否 @ 了 imbot 自身"""
        msg_obj = getattr(event, "message_obj", None)
        if msg_obj:
            imbot_id = getattr(msg_obj, "self_id", "")
            mentions = getattr(msg_obj, "mentions", None) or []
            for m in mentions:
                uid = str(getattr(m, "user_id", ""))
                if uid and uid == imbot_id:
                    return True
        # fallback: 字符串匹配
        return "imbot" in (getattr(event, "message_str", "") or "").lower()

    def _detect_name_change(self, user_id: str, current_name: str) -> dict | None:
        person = self.world.people.get(user_id)
        if person and person.current_display_name and person.current_display_name != current_name:
            old = person.current_display_name
            return {"type": "rename", "user_id": user_id, "old_name": old, "new_name": current_name}
        return None

    def _detect_mentions(self, event, sender_id: str) -> list[dict]:
        observations = []
        msg_obj = getattr(event, "message_obj", None)
        if not msg_obj:
            return observations
        mentions = getattr(msg_obj, "mentions", None) or []
        for m in mentions:
            target_id = str(getattr(m, "user_id", ""))
            if target_id and target_id != sender_id:
                observations.append({
                    "type": "interaction",
                    "user_id": target_id,
                    "group_id": event.get_group_id(),
                    "message_text": getattr(event, "message_str", ""),
                })
        return observations

    def _detect_tone(self, msg: str, sender_id: str, response_text: str, weight: float) -> list[dict]:
        observations = []
        mentioned = set(re.findall(r"@(\S+)", msg))

        # 上下文增强：检查目标人物是否在 recent_context 中出现
        def _context_has(target_id: str, text: str) -> bool:
            person = self.world.people.get(target_id)
            if not person or not person.recent_context:
                return False
            return any(text[:30] in ctx for ctx in person.recent_context)

        for kw in POSITIVE_SIGNALS:
            if kw in msg or kw in response_text:
                for name in mentioned:
                    target = self.world.find_person_by_label(name)
                    if target:
                        ctx_boost = 0.1 if _context_has(target.id, kw) else 0
                        observations.append({
                            "type": "tone_signal",
                            "user_id": sender_id,
                            "signal": "positive",
                            "target_id": target.id,
                            "weight": min(1.0, weight + ctx_boost),
                        })
                break

        for kw in NEGATIVE_SIGNALS:
            if kw in msg or kw in response_text:
                for name in mentioned:
                    target = self.world.find_person_by_label(name)
                    if target:
                        ctx_boost = 0.1 if _context_has(target.id, kw) else 0
                        observations.append({
                            "type": "tone_signal",
                            "signal": "negative",
                            "user_id": sender_id,
                            "target_id": target.id,
                            "weight": min(1.0, weight + ctx_boost),
                        })
                break

        for kw in CLOSE_SIGNALS:
            if kw in msg or kw in response_text:
                for name in mentioned:
                    target = self.world.find_person_by_label(name)
                    if target:
                        ctx_boost = 0.1 if _context_has(target.id, kw) else 0
                        observations.append({
                            "type": "tone_signal",
                            "signal": "close",
                            "user_id": sender_id,
                            "target_id": target.id,
                            "weight": min(1.0, weight + ctx_boost),
                        })
                break

        return observations

    def _detect_self_profile_clues(self, event, msg: str, is_group: bool) -> list[dict]:
        """降频：每 10 条消息才触发一次画像更新"""
        observations = []
        sender_id = event.get_sender_id()
        if sender_id != self.world._owner_id:
            return observations

        cnt = self._profile_update_counter.get(sender_id, 0) + 1
        self._profile_update_counter[sender_id] = cnt
        if len(self._profile_update_counter) > 500:
            self._profile_update_counter = dict(
                sorted(self._profile_update_counter.items(), key=lambda x: x[1])[-200:]
            )
        if cnt % 10 != 0:
            return observations

        hour = time.localtime().tm_hour

        if 0 <= hour < 5:
            observations.append({
                "type": "self_profile_update",
                "dimension": "rhythm",
                "label": "夜猫子",
                "delta": 1,
            })

        if is_group and len(msg) < 10:
            observations.append({
                "type": "self_profile_update",
                "dimension": "social_pattern",
                "label": "不太主动找人",
                "delta": 1,
            })

        if len(msg) > 50:
            observations.append({
                "type": "self_profile_update",
                "dimension": "emotional_style",
                "label": "话多时情绪外露",
                "delta": 1,
            })

        return observations
