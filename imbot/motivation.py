import random

TONES = ("冷淡", "安静", "温和", "好奇", "烦躁", "随性")

MODIFIERS = {
    "owner": {
        "concern": 0.3,
        "withdrawal": -0.3,
    },
    "known": {
        "concern": 0.1,
        "withdrawal": 0.0,
    },
    "stranger": {
        "concern": -0.2,
        "withdrawal": 0.2,
    },
}


class MotivationEngine:
    def evaluate(self, state, time_ctx: dict, speaker_type: str,
                 is_group: bool = False, social_context: dict = None,
                 secretary_advice: dict = None,
                 group_proactive_level: str = "克制") -> dict:
        mood = state.mood
        intensity = state.mood_intensity
        energy = state.energy
        period = time_ctx.get("period", "深夜")

        # 基础语气映射
        tone = self._mood_to_tone(mood, intensity)

        # 微量随机噪声（5%，先于时间因子，确保时间语境优先）
        if random.random() < 0.05:
            tone = random.choice(TONES)

        # 时间因子
        if period in ("深夜", "凌晨"):
            if energy < 0.4:
                tone = random.choice(["安静", "冷淡"])
            elif random.random() < 0.3:
                tone = "随性"

        # 身份调节 — 影响 should_respond 和 initiative
        mod = MODIFIERS.get(speaker_type, MODIFIERS["stranger"])
        withdrawal = mod.get("withdrawal", 0)

        # 群聊额外调节
        if is_group:
            withdrawal += {"克制": 0.2, "适度": 0.1, "活跃": 0.0}.get(group_proactive_level, 0.2)

        # 决定是否回复
        base_willingness = 0.6 + mod.get("concern", 0) - withdrawal
        if energy < 0.3:
            base_willingness -= 0.3
        if intensity < 0.3:
            base_willingness -= 0.2

        # 秘书建议（提升意愿 + 影响语气）
        if secretary_advice and secretary_advice.get("should_respond"):
            base_willingness += 0.25
            sec_tone = secretary_advice.get("tone", "")
            if sec_tone and tone in ("安静", ""):
                tone = sec_tone

        # 社交世界调节
        if social_context:
            base_willingness += social_context.get("willingness_mod", 0)
            if social_context.get("trigger_curiosity"):
                tone = "好奇"
            if social_context.get("trigger_displeasure") and random.random() < 0.3:
                tone = "烦躁"

        should_respond = base_willingness > 0.4

        initiative = max(0.1, min(1.0, base_willingness))

        return {
            "tone": tone,
            "should_respond": should_respond,
            "initiative": initiative,
        }

    def _mood_to_tone(self, mood: str, intensity: float) -> str:
        if mood == "低落" and intensity > 0.6:
            return "冷淡"
        if mood == "安静" and intensity < 0.4:
            return "安静"
        if mood == "开心":
            return random.choice(["温和", "随性"])
        if mood == "烦躁":
            return "烦躁"
        if mood == "好奇":
            return "好奇"
        if mood == "疲惫":
            return random.choice(["安静", "冷淡"])
        return "安静"
