"""
能力层入口。WillingnessEvaluator + CapabilityManager。
"""
from enum import IntEnum


class CapabilityWillingness(IntEnum):
    REFUSE = 0
    RELUCTANT = 1
    MODERATE = 2
    WILLING = 3
    EAGER = 4

    @property
    def label(self) -> str:
        return ["拒绝", "勉强", "就事论事", "愿意", "主动"][self.value]

    @property
    def tone_hint(self) -> str:
        return {
            0: "直接拒绝,一两字", 1: "帮了但态度差", 2: "帮了不多说",
            3: "帮了追问一句", 4: "主动包揽",
        }[self.value]


class WillingnessEvaluator:
    def evaluate(self, state, motivation: dict, speaker_type: str,
                 skill_name: str, is_group: bool = False,
                 was_mentioned: bool = False) -> CapabilityWillingness:
        """
        评估能力意愿。调用时机: @on_using_llm_tool 拦截后。
        """
        base = 2  # 从 MODERATE 开始

        # ── 场景 ──
        if is_group:
            base -= 1
            if was_mentioned:
                base += 1  # 被 @ 恢复
            # 操作 Skill 群聊完全禁止
            if skill_name in ("volume_control", "media_control", "open_program"):
                return CapabilityWillingness.REFUSE

        # ── 身份 ──
        if speaker_type == "owner":
            base += 1
        elif speaker_type == "stranger":
            base -= 2

        # ── 自身状态 ──
        tone = motivation.get("tone", "安静")
        if tone in ("冷淡", "烦躁"):
            base -= 1
        if state.energy < 0.3:
            base -= 1

        # ── 请求性质 ──
        simple_skills = ("volume_control", "media_control", "translate", "calculate")
        if skill_name in simple_skills:
            base += 1

        # ── 动机 ──
        if motivation.get("should_respond") and motivation.get("initiative", 0) > 0.6:
            base += 1

        return CapabilityWillingness(max(0, min(4, base)))


class CapabilityManager:
    def __init__(self, config):
        self.config = config
        self.evaluator = WillingnessEvaluator()
        self._help_count: dict[str, int] = {}  # {user_id: 近期帮助次数}
        self._last_help_time: dict[str, float] = {}

    def _prune_stale_users(self, now: float):
        """清理 1 小时未活跃的用户记录，防止内存泄漏"""
        stale = [uid for uid, t in self._last_help_time.items() if now - t > 3600]
        for uid in stale:
            self._help_count.pop(uid, None)
            self._last_help_time.pop(uid, None)

    def evaluate(self, event, skill_name: str) -> tuple[CapabilityWillingness, str]:
        """供 main.py 拦截钩子调用。返回 (意愿等级, 自然语言拒绝消息)"""
        # 延迟导入避免循环
        engine = getattr(event, "_imbot_engine", None)
        if not engine:
            return CapabilityWillingness.MODERATE, ""

        try:
            speaker_id = event.get_sender_id()
        except Exception:
            speaker_id = ""
        speaker_type = engine.state.classify_speaker(speaker_id)
        is_group = bool(event.get_group_id()) if event.get_group_id else False

        mot = engine._last_motivation_result or {"tone": "安静", "initiative": 0.5}

        # 群聊 @ 检测
        was_mentioned = False
        if is_group and hasattr(engine, "group_perception"):
            mention = engine.group_perception.detect_mention(event)
            was_mentioned = mention.get("mentioned_imbot", False)

        level = self.evaluator.evaluate(
            engine.state, mot, speaker_type, skill_name, is_group, was_mentioned
        )

        # 疲劳惩罚
        if speaker_id:
            import time
            now = time.time()
            self._prune_stale_users(now)
            cnt = self._help_count.get(speaker_id, 0)
            last = self._last_help_time.get(speaker_id, 0)
            if now - last < 300:  # 5 分钟内
                cnt += 1
            else:
                cnt = 1
            self._help_count[speaker_id] = cnt
            self._last_help_time[speaker_id] = now
            if cnt > 3:
                level = CapabilityWillingness(max(0, level.value - 1))

        refusals = {
            CapabilityWillingness.REFUSE: "不想。",
            CapabilityWillingness.RELUCTANT: "啧。",
        }
        result = refusals.get(level, "")
        if level in (CapabilityWillingness.REFUSE, CapabilityWillingness.RELUCTANT):
            from astrbot.api import logger
            logger.warning(f"能力拒绝: {skill_name} → {level.name} ({speaker_type or '?'})")
        return level, result
