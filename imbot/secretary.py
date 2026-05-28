"""
群聊秘书模型 — 读空气，判断该不该插话。
不代替 imbot 决策，只提供简短建议。最终决定权在动机系统。
"""
import asyncio

from astrbot.api import logger
from imbot.utils import parse_llm_json


class Secretary:
    def __init__(self, config):
        self.cfg = config

    # ── 第一层：前置过滤 ──
    def prefilter(self, mention: dict, activity: str) -> bool:
        """免费硬规则过滤。返回 True 表示值得继续处理。"""
        # 被@或直呼 — 永远通过
        if mention.get("mentioned_imbot"):
            return True

        # 活跃度门槛
        activity_levels = {"安静": 0, "低频": 1, "正常": 2, "活跃": 3, "很吵": 4}
        threshold = activity_levels.get(self.cfg.min_activity, 3)
        current = activity_levels.get(activity, 2)
        if current >= threshold:
            return True

        return False

    # ── 第二层：秘书分析 ──
    async def analyze(self, context: dict) -> dict | None:
        """调用秘书模型分析群聊上下文。返回建议 dict 或 None（失败/降级）。"""
        if not self.cfg.enabled:
            return None

        # 构建 prompt
        messages = context.get("messages", "")
        speakers = context.get("speakers", "")
        topic = context.get("topic", "")
        activity = context.get("activity", "")

        rules_block = ""
        if self.cfg.rules.strip():
            rules_block = f"## 用户规则\n{self.cfg.rules.strip()}"
        if self.cfg.silence_rules.strip():
            rules_block += f"\n\n## 强制闭嘴规则（以下场景 must_respond=false）\n{self.cfg.silence_rules.strip()}"

        prompt = (
            "你是群聊社交秘书。分析群聊上下文，判断是否应该插话。\n"
            "你不是群助手或客服，是有脾气的存在。不该说话时保持沉默。\n"
            "你只输出 JSON，不要生成回复内容。\n\n"
            f"## 群聊上下文\n{messages}\n\n"
            f"## 发言人\n{speakers}\n\n"
            f"## 话题\n{topic or '未检测到明确话题'}\n\n"
            f"## 活跃度\n{activity}\n\n"
            f"{rules_block}\n\n"
            "## 氛围判断\n如果群友在开玩笑或轻松聊天，倾向于建议她用「随大流/凑热闹/接梗」语气。"
            "如果群友在认真讨论，倾向于建议她用「认真接话」语气。\n\n"
            '输出 JSON：{"should_respond": bool, "tone": "随大流/凑热闹/接梗/短促回应/认真接话/关心/吐槽/冷淡", "reason": "一句话理由"}'
        )

        task = asyncio.ensure_future(context["_llm_call"](prompt))
        try:
            result = await asyncio.wait_for(task, timeout=5.0)
            if not result:
                return None
            return self._parse_response(result)
        except asyncio.TimeoutError:
            task.cancel()
            logger.warning("秘书模型超时，降级到纯动机判断")
            return None
        except Exception:
            logger.warning("秘书模型调用失败，降级", exc_info=True)
            return None

    @staticmethod
    def _parse_response(text: str) -> dict | None:
        data = parse_llm_json(text)
        if not data:
            return None
        should = data.get("should_respond", False)
        if not isinstance(should, bool):
            should = False
        return {
            "should_respond": should,
            "tone": data.get("tone", "安静"),
            "reason": data.get("reason", ""),
        }

    # ── 强制闭嘴 ──
    def check_silence_rules(self, context: dict) -> str | None:
        """检查强制闭嘴规则。命中返回规则文本，否则 None。始终生效。"""
        silence = self.cfg.silence_rules.strip()
        if not silence:
            return None
        topic = (context.get("topic") or "").lower()
        messages = (context.get("messages") or "").lower()
        for rule in silence.split("\n"):
            rule = rule.strip()
            if not rule:
                continue
            for kw in self._extract_keywords(rule):
                if kw in topic or kw in messages:
                    return rule
        return None

    # ── 规则匹配 ──
    def check_rules(self, secretary_advice: dict | None, context: dict) -> str | None:
        """检查用户规则是否禁止回应。返回禁止原因或 None（不禁止）。"""
        apply_at = self.cfg.rules_apply_at
        if apply_at == "secretary":
            return None  # 规则只提示秘书，动机不做硬约束

        rules = self.cfg.rules.strip()
        if not rules:
            return None

        topic = (context.get("topic") or "").lower()
        messages = (context.get("messages") or "").lower()

        for rule in rules.split("\n"):
            rule = rule.strip()
            if not rule:
                continue
            for kw in self._extract_keywords(rule):
                if kw in topic or kw in messages:
                    return rule
        return None

    @staticmethod
    def _extract_keywords(rule: str) -> list[str]:
        """从规则文本提取关键词"""
        import re
        quoted = re.findall(r'[""]([^""]+)[""]', rule)
        if quoted:
            return [q.lower() for q in quoted]
        # 按停用词分割（长词优先，避免"不"误切"不要"）
        stops = ["不要", "禁止", "应该", "可以", "直接", "不", "时", "的", "了", "在"]
        parts = rule
        for s in stops:
            parts = parts.replace(s, " ")
        keywords = [p.strip().lower() for p in parts.split() if len(p.strip()) >= 2]
        return keywords
