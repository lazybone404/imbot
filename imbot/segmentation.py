"""
本地智能分段引擎。

LLM 回复 → 预处理清洗 → 场景检测 → 标点切分 → 语义保护
→ 延迟计算 → 填充词 → 频率保护 → 逐条发送。

详见项目根目录 SEGMENTATION.md。
"""
import re
import random


# ── 语义保护：不可在以下模式处断开 ──
SEMANTIC_GUARDS = [
    re.compile(r"\d+\s*个\s*\w"),            # "3 个人"
    re.compile(r"\d+\s*[分秒时天月年]"),      # "5 分钟"
    re.compile(r'[「「"\'\(（].*?[」」"\'\)）]'),  # 括号/引号内容
    re.compile(r"因为.*?所以"),                # 关联词
    re.compile(r"虽然.*?但是"),
    re.compile(r"https?://\S+"),             # URL
    re.compile(r"\d{5,}"),                   # QQ号
    re.compile(r"`[^`]*`"),                   # 行内代码
]

# 结构化内容检测
STRUCTURED_PATTERNS = [
    re.compile(r"^\s*(?:\d+[.、]|[-*])\s", re.MULTILINE),  # 列表
    re.compile(r"^\s*\|.*\|", re.MULTILINE),                 # 表格
    re.compile(r"```"),                                      # 代码块
]

# 填充词表
FILLERS = {
    "concern": ["嗯…", "怎么说呢"],
    "sharing_desire": ["就是…"],
    "vulnerability": ["……"],
    "default": [],
}

# 预设模板
PRESETS = {
    "natural": {
        "method": "auto", "max_segments": 3, "min_text_length": 50,
        "delay_base_min": 0.3, "delay_base_max": 1.0, "add_fillers": "contextual",
    },
    "fragmented": {
        "method": "punctuation", "max_segments": 5, "min_text_length": 30,
        "split_medium_as_strong": True,
        "delay_base_min": 0.1, "delay_base_max": 0.4, "add_fillers": "on",
    },
    "concise": {
        "method": "punctuation", "max_segments": 2, "min_text_length": 100,
        "delay_base_min": 0.2, "delay_base_max": 0.6, "add_fillers": "off",
    },
    "dramatic": {
        "method": "auto", "max_segments": 4, "min_text_length": 30,
        "delay_base_min": 0.8, "delay_base_max": 2.0,
        "mood_multiplier": 1.5, "add_fillers": "on",
    },
    "instant": {
        "enabled": False,
    },
}


class SegmentationEngine:
    def __init__(self, imbot_config):
        self.cfg = imbot_config.output_segmentation
        self._recent_sends: list[float] = []  # 频率追踪

    # ── 公开：纯清洗（所有 LLM 输出必经）──
    def clean(self, text: str) -> str:
        """清洗 LLM 输出中的脏换行，不依赖分段配置。无论是否分段都调用。"""
        return self._preprocess(text)

    # ── 入口 ──
    def segment(self, text: str, context: dict) -> dict | None:
        if not self.cfg.enabled:
            return None

        is_group = context.get("is_group", False)
        if is_group and not self.cfg.scene_overrides.group_chat_enabled:
            return None

        # 应用预设
        preset = PRESETS.get(self.cfg.preset, {})
        enabled = preset.get("enabled", True)
        if not enabled:
            return None

        max_seg = preset.get("max_segments", self.cfg.max_segments)
        min_len = preset.get("min_text_length", self.cfg.min_text_length)
        method = preset.get("method", self.cfg.method)

        # 结构化检测 → 不分段（必须在预处理前，保留原始换行用于判断）
        if self._is_structured(text):
            return None

        # 预处理
        text = self._preprocess(text)
        if not text or len(text) < min_len:
            return None

        # scene 检测 + 方法映射
        scene = self._detect_scene(text, context)
        if method == "auto":
            method = self._scene_to_method(scene)

        # 寻找分割点
        split_points = self._find_split_points(text, method, preset)
        if not split_points:
            return None

        # 切分
        segments = self._split(text, split_points, max_seg, scene)
        if len(segments) <= 1:
            return None

        # 语义保护: 重检每段
        if self.cfg.semantic_guard:
            segments = self._apply_semantic_guard(segments, text)

        # 延迟
        delays = self._calc_delays(segments, preset, context)

        # 填充词
        fillers = self._calc_fillers(segments, context)

        # 频率保护
        segments, delays, fillers = self._apply_rate_limit(segments, delays, fillers)

        if len(segments) <= 1:
            return None

        return {"segments": segments, "delays": delays, "fillers": fillers}

    # ── 预处理 ──
    def _preprocess(self, text: str) -> str:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = re.sub(r"\n{3,}", "\n\n", text)    # 3+换行 → 2换行
        # 先拆段（利用 \n\n），再处理行内换行
        parts = text.split("\n\n")
        ENDS_WITH_PUNCT = "。！？…—~，；"
        result = parts[0].replace("\n", " ").strip()
        for seg in parts[1:]:
            seg = seg.replace("\n", " ").strip()
            if not seg:
                continue
            if result and result[-1] not in ENDS_WITH_PUNCT:
                result += "。"
            result += seg
        return re.sub(r" {2,}", " ", result).strip()

    def _is_structured(self, text: str) -> bool:
        if re.search(r"```", text):
            return True
        lines = text.split("\n")
        if len(lines) < 3:
            return False
        matchers = STRUCTURED_PATTERNS[:2]  # 列表+表格正则
        structured_lines = sum(
            1 for l in lines
            if l.strip() and any(p.search(l) for p in matchers)
        )
        return structured_lines > len(lines) * 0.5

    # ── Scene 检测 ──
    def _detect_scene(self, text: str, context: dict) -> str:
        mot = context.get("motivation", {})
        tone = mot.get("tone", "安静")
        text_len = len(text)

        if context.get("is_proactive"):
            return "proactive"

        if tone in ("冷淡", "烦躁", "低落") and text_len < 80:
            return "emotional"

        if any(kw in text for kw in ("查了", "搜了", "步骤", "说明", "注意", "建议")):
            return "helping"
        if "?" in text and text_len > 100:
            return "helping"

        if any(kw in text for kw in ("因为", "所以", "第一", "首先")):
            return "explaining"
        if re.search(r"\d+[.、]", text):
            return "explaining"

        return "chitchat"

    def _scene_to_method(self, scene: str) -> str:
        return {
            "helping": "minimal",
            "chitchat": "normal",
            "emotional": "fragmented",
            "explaining": "logical",
            "proactive": "normal",
        }.get(scene, "normal")

    # ── 分割点查找 ──
    def _find_split_points(self, text: str, method: str, preset: dict) -> list[int]:
        strong = list(self.cfg.split_strong)
        medium = list(self.cfg.split_medium)

        if preset.get("split_medium_as_strong"):
            strong.extend(medium)
            medium = []

        chars = self.cfg.min_segment_chars
        positions = []

        if method == "minimal":
            # 只在长段落处分，一个 strong 分割点最多选一个
            for ch in strong:
                idx = text.find(ch, chars)
                if idx != -1 and idx < len(text) - chars:
                    positions.append(idx + 1)
        elif method == "fragmented":
            # 强+中 都用
            for i, ch in enumerate(text):
                if ch in strong or ch in medium:
                    positions.append(i + 1)
        elif method == "logical":
            # 优先在强分割处断，然后中
            for i, ch in enumerate(text):
                if ch in strong:
                    positions.append(i + 1)
            if not positions:
                for i, ch in enumerate(text):
                    if ch in medium:
                        positions.append(i + 1)
        else:  # normal
            for i, ch in enumerate(text):
                if ch in strong:
                    positions.append(i + 1)

        # 过滤过近的分割点
        filtered = []
        last = -chars
        for p in positions:
            if p - last >= chars:
                filtered.append(p)
                last = p
        return filtered

    def _split(self, text: str, points: list[int], max_seg: int, scene: str) -> list[str]:
        segments = []
        prev = 0
        for p in points[:max_seg - 1]:
            seg = text[prev:p].strip()
            if seg:
                segments.append(seg)
            prev = p
        tail = text[prev:].strip()
        if tail:
            segments.append(tail)
        return segments[:max_seg]

    def _apply_semantic_guard(self, segments: list[str], original: str) -> list[str]:
        """检查断开点是否在保护区间内，若是则合并相邻段"""
        protected_ranges = []
        for pattern in SEMANTIC_GUARDS:
            for m in pattern.finditer(original):
                protected_ranges.append((m.start(), m.end()))
        if not protected_ranges:
            return segments

        # 简单策略：如果某段末尾/开头在保护区间内，合并
        merged = []
        i = 0
        while i < len(segments):
            if i < len(segments) - 1:
                boundary = len("".join(segments[:i + 1]))
                for start, end in protected_ranges:
                    if start < boundary < end:
                        segments[i] = segments[i] + segments[i + 1]
                        segments.pop(i + 1)
                        break
                else:
                    i += 1
            else:
                i += 1
        return segments

    # ── 延迟计算 ──
    def _calc_delays(self, segments: list[str], preset: dict, context: dict) -> list[float]:
        if not self.cfg.delay_enabled:
            return [0] * len(segments)

        base_min = preset.get("delay_base_min", self.cfg.delay_base_min)
        base_max = preset.get("delay_base_max", self.cfg.delay_base_max)
        mood_mult = preset.get("mood_multiplier", 1.0)

        state = context.get("state")
        if state:
            mood_mult *= {"疲惫": 1.5, "低落": 1.2, "烦躁": 1.3}.get(state.mood, 1.0)
            if state.energy < 0.3:
                mood_mult *= 1.3

        gap = context.get("last_interaction_gap", 0)
        rhythm_mult = 1.0
        if gap > 300:
            rhythm_mult = 1.3
        elif gap < 30:
            rhythm_mult = 0.8

        delays = []
        for seg in segments:
            base = random.uniform(base_min, base_max)
            if len(seg) < 20:
                base *= random.uniform(0.5, 0.8)
            elif len(seg) > 50:
                base *= random.uniform(1.0, 1.5)
            base *= mood_mult * rhythm_mult
            delays.append(round(base, 2))
        return delays

    # ── 填充词 ──
    def _calc_fillers(self, segments: list[str], context: dict) -> list:
        mode = self.cfg.add_fillers
        if mode == "off":
            return [None] * len(segments)
        mot = context.get("motivation", {})
        tone = mot.get("tone", "安静")
        fillers = []
        for i, seg in enumerate(segments):
            if mode == "on" and i == 0:
                filler = self._pick_filler(tone)
                fillers.append(filler)
            elif mode == "contextual" and i == 0:
                filler = self._pick_filler(tone)
                fillers.append(filler)
            else:
                fillers.append(None)
        return fillers

    def _pick_filler(self, tone: str) -> str | None:
        mapping = {
            "冷淡": None,
            "安静": None,
            "温和": FILLERS["concern"],
            "好奇": FILLERS["sharing_desire"],
            "烦躁": None,
            "随性": None,
        }
        candidates = mapping.get(tone) or FILLERS["default"]
        if not candidates:
            return None
        return random.choice(candidates)

    # ── 频率保护 ──
    def _apply_rate_limit(self, segments, delays, fillers):
        now = time.time()
        self._recent_sends = [t for t in self._recent_sends if now - t < 60]
        allowed = self.cfg.rate_limit_max_per_min - len(self._recent_sends)

        if len(segments) > allowed + self.cfg.rate_limit_burst:
            # 合并超出部分
            keep = max(1, allowed)
            merged = segments[:keep]
            merged.append("".join(segments[len(merged):]))
            segments = merged
            delays = delays[:len(merged)]
            fillers = fillers[:len(merged)]

        return segments, delays, fillers
