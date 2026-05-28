import json
import os
import time
from dataclasses import dataclass, field, asdict

from imbot.utils import atomic_write_json


RELATIONSHIP_TYPES = (
    "close_friend", "casual_friend", "authority", "subordinate",
    "teasing", "admires", "avoids", "unknown",
)

POWER_DYNAMICS = ("equal", "user_above", "them_above", "unclear")

EVOLVING_STATES = ("improving", "stable", "cooling", "unstable")


@dataclass
class ProfileDimension:
    label: str = "待观察"
    confidence: int = 1  # 1-5


@dataclass
class OwnerProfile:
    rhythm: ProfileDimension = field(default_factory=ProfileDimension)
    emotional_style: ProfileDimension = field(default_factory=ProfileDimension)
    social_pattern: ProfileDimension = field(default_factory=ProfileDimension)
    reliability: ProfileDimension = field(default_factory=ProfileDimension)
    vulnerability: ProfileDimension = field(default_factory=ProfileDimension)
    attitude_to_imbot: ProfileDimension = field(default_factory=ProfileDimension)
    relational_pattern: ProfileDimension = field(default_factory=ProfileDimension)

    DIMENSIONS = (
        "rhythm", "emotional_style", "social_pattern",
        "reliability", "vulnerability", "attitude_to_imbot",
        "relational_pattern",
    )

    def update(self, dim: str, label: str, delta: int = 1):
        if dim not in self.DIMENSIONS:
            return
        current = getattr(self, dim)
        if current.label == label:
            current.confidence = min(5, current.confidence + delta)
        else:
            current.confidence = max(1, current.confidence - 1)
            if current.confidence <= 1:
                current.label = label
                current.confidence = 1

    def to_dict(self) -> dict:
        return {d: asdict(getattr(self, d)) for d in self.DIMENSIONS}

    @classmethod
    def from_dict(cls, data: dict) -> "OwnerProfile":
        profile = cls()
        for dim in cls.DIMENSIONS:
            if dim in data:
                profile.__setattr__(dim, ProfileDimension(**data[dim]))
        return profile


@dataclass
class CallName:
    name: str
    context: str  # "私聊" / "群里"


@dataclass
class Relationship:
    primary: str = "unknown"
    secondary: list = field(default_factory=list)
    confidence: float = 1.0  # 0-5
    asymmetry: dict = field(default_factory=dict)
    power_dynamic: str = "unclear"
    user_calls_them: list = field(default_factory=list)
    they_call_user: list = field(default_factory=list)
    mutual_topics: list = field(default_factory=list)
    impression_source: str = ""
    first_observed: str = ""
    last_observed: str = ""
    evolving: str = "stable"
    history: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "type": {"primary": self.primary, "secondary": self.secondary},
            "confidence": self.confidence,
            "asymmetry": self.asymmetry,
            "power_dynamic": self.power_dynamic,
            "user_calls_them": [asdict(n) if hasattr(n, '__dataclass_fields__') else n for n in self.user_calls_them],
            "they_call_user": [asdict(n) if hasattr(n, '__dataclass_fields__') else n for n in self.they_call_user],
            "mutual_topics": self.mutual_topics,
            "impression_source": self.impression_source,
            "first_observed": self.first_observed,
            "last_observed": self.last_observed,
            "evolving": self.evolving,
            "history": self.history,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Relationship":
        r = cls()
        r.primary = data.get("type", {}).get("primary", "unknown")
        r.secondary = data.get("type", {}).get("secondary", [])
        r.confidence = data.get("confidence", 1.0)
        r.asymmetry = data.get("asymmetry", {})
        r.power_dynamic = data.get("power_dynamic", "unclear")
        r.user_calls_them = [CallName(**n) if isinstance(n, dict) else n for n in data.get("user_calls_them", [])]
        r.they_call_user = [CallName(**n) if isinstance(n, dict) else n for n in data.get("they_call_user", [])]
        r.mutual_topics = data.get("mutual_topics", [])
        r.impression_source = data.get("impression_source", "")
        r.first_observed = data.get("first_observed", "")
        r.last_observed = data.get("last_observed", "")
        r.evolving = data.get("evolving", "stable")
        r.history = data.get("history", [])
        return r


@dataclass
class Impression:
    personality: str = ""
    attitude_to_user: str = ""
    attitude_to_imbot: str = ""
    speech_style: str = ""
    confidence: float = 1.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Impression":
        return cls(**{k: data.get(k, "") if k != "confidence" else data.get(k, 1.0)
                      for k in ("personality", "attitude_to_user", "attitude_to_imbot", "speech_style", "confidence")})


@dataclass
class Person:
    id: str = ""
    current_display_name: str = ""
    past_names: list = field(default_factory=list)
    labels_from_imbot: list = field(default_factory=list)
    relationship_to_user: Relationship = field(default_factory=Relationship)
    imbots_own_impression: Impression = field(default_factory=Impression)
    observed_in: list = field(default_factory=list)
    first_seen: str = ""
    last_seen: str = ""
    observation_count: int = 0
    recent_context: list = field(default_factory=list)  # FIFO，最近 5 条消息摘要

    def add_context(self, text: str, max_len: int = 5):
        """追加消息摘要到上下文缓冲区"""
        summary = text[:80]
        self.recent_context.append(summary)
        if len(self.recent_context) > max_len:
            self.recent_context.pop(0)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "current_display_name": self.current_display_name,
            "past_names": self.past_names,
            "labels_from_imbot": self.labels_from_imbot,
            "relationship_to_user": self.relationship_to_user.to_dict(),
            "imbots_own_impression": self.imbots_own_impression.to_dict(),
            "observed_in": self.observed_in,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "observation_count": self.observation_count,
            "recent_context": self.recent_context,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Person":
        return cls(
            id=data.get("id", ""),
            current_display_name=data.get("current_display_name", ""),
            past_names=data.get("past_names", []),
            labels_from_imbot=data.get("labels_from_imbot", []),
            relationship_to_user=Relationship.from_dict(data.get("relationship_to_user", {})),
            imbots_own_impression=Impression.from_dict(data.get("imbots_own_impression", {})),
            observed_in=data.get("observed_in", []),
            first_seen=data.get("first_seen", ""),
            last_seen=data.get("last_seen", ""),
            observation_count=data.get("observation_count", 0),
            recent_context=data.get("recent_context", []),
        )


class SocialWorld:
    MIN_OBS_FOR_TYPE = 3       # 至少观察 N 次才允许改 primary 类型
    MIN_OBS_FOR_CONFIDENT = 5  # 少于 N 次 → tentative

    # 差异化衰减系数
    DECAY_MULTIPLIERS = {
        "close_friend": 0.3,
        "casual_friend": 0.7,
        "authority": 0.5,
        "admires": 0.5,
        "teasing": 0.8,
        "avoids": 1.0,
        "subordinate": 1.0,
        "unknown": 1.5,
    }

    def __init__(self, owner_id: str):
        self._owner_id = owner_id
        self.self_profile = OwnerProfile()
        self.people: dict[str, Person] = {}
        # 自我信号追踪：{user_id: [最近5次的mood]}
        self._self_signal_counter: dict[str, list[str]] = {}

    @classmethod
    def load(cls, path: str, owner_id: str) -> "SocialWorld":
        world = cls(owner_id)
        world._path = path
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            world.self_profile = OwnerProfile.from_dict(data.get("self", {}).get("profile", {}))
            for pid, pdata in data.get("people", {}).items():
                world.people[pid] = Person.from_dict(pdata)
        except FileNotFoundError:
            world._init_default()
        except json.JSONDecodeError:
            backup = path + ".corrupted"
            try:
                os.rename(path, backup)
            except OSError:
                pass
            world._init_default()
        return world

    def _init_default(self):
        self.self_profile = OwnerProfile()
        self.people = {}

    def save(self):
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            atomic_write_json(self._path, self.to_dict())
        except OSError:
            pass

    def to_dict(self) -> dict:
        return {
            "version": 2,
            "self": {"id": self._owner_id, "profile": self.self_profile.to_dict()},
            "people": {pid: p.to_dict() for pid, p in self.people.items()},
        }

    # ── 人员查询 ──
    def get_person(self, user_id: str) -> Person:
        if user_id not in self.people:
            self.people[user_id] = Person(
                id=user_id,
                first_seen=time.strftime("%Y-%m-%d"),
            )
        return self.people[user_id]

    def classify_person(self, user_id: str) -> str:
        if not user_id:
            return "stranger"
        if user_id == self._owner_id:
            return "owner"
        person = self.people.get(user_id)
        if person and person.relationship_to_user.confidence >= 2:
            return "known"
        return "stranger"

    def find_person_by_label(self, label: str) -> Person | None:
        for p in self.people.values():
            if label in p.labels_from_imbot:
                return p
            if label in p.current_display_name:
                return p
            for n in p.past_names:
                if label in n:
                    return p
        return None

    # ── 称呼 ──
    def observe_nickname(self, user_id: str, name: str, context: str):
        person = self.get_person(user_id)
        if name and name != person.current_display_name:
            if person.current_display_name:
                person.past_names.append(person.current_display_name)
            person.current_display_name = name
        if user_id == self._owner_id:
            self.self_profile.social_pattern.confidence = min(5, self.self_profile.social_pattern.confidence + 1)

    def add_call_name(self, caller_id: str, target_id: str, name: str, context: str):
        """记录 A 怎么叫 B"""
        caller = self.get_person(caller_id)
        target = self.get_person(target_id)
        cn = CallName(name=name, context=context)
        if caller_id == self._owner_id:
            target.relationship_to_user.user_calls_them.append(cn)
        if target_id == self._owner_id:
            caller.relationship_to_user.they_call_user.append(cn)

    # ── 关系更新 ──
    def apply_observation(self, obs):
        from astrbot.api import logger
        try:
            if obs["type"] == "nickname":
                self.observe_nickname(obs["user_id"], obs["name"], obs.get("context", "私聊"))
            elif obs["type"] == "call_name":
                self.add_call_name(obs["caller_id"], obs["target_id"], obs["name"], obs.get("context", ""))
            elif obs["type"] == "rename":
                self._handle_rename(obs["user_id"], obs["old_name"], obs["new_name"])
            elif obs["type"] == "tone_signal":
                self._handle_tone_signal(obs)
            elif obs["type"] == "interaction":
                person = self.get_person(obs["user_id"])
                person.last_seen = time.strftime("%Y-%m-%d")
                person.observation_count += 1
                # 上下文缓冲
                if obs.get("message_text"):
                    person.add_context(obs["message_text"])
                if obs.get("group_id"):
                    if obs["group_id"] not in person.observed_in:
                        person.observed_in.append(obs["group_id"])
            elif obs["type"] == "self_signal":
                self._apply_self_signal(obs["target_id"], obs["mood"])
            elif obs["type"] == "self_profile_update":
                self.update_self_profile(obs["dimension"], obs["label"], obs.get("delta", 1))
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            logger.error(f"社交观察失败 [{obs.get('type', '?')}]: {e}", exc_info=True)

    def _handle_rename(self, user_id: str, old_name: str, new_name: str):
        person = self.get_person(user_id)
        if old_name and old_name not in person.past_names:
            person.past_names.append(old_name)
        person.current_display_name = new_name

    def _handle_tone_signal(self, obs):
        target_id = obs.get("target_id", "")
        if not target_id:
            return
        signal = obs.get("signal", "")
        weight = obs.get("weight", 0.5)  # observer 分配的权重
        target = self.get_person(target_id)
        rel = target.relationship_to_user

        # 观察计数 +1
        target.observation_count += 1

        # 矛盾检测：记录上次极性
        polarity = "positive" if signal in ("positive", "close") else "negative"
        prev = getattr(rel, "_last_polarity", None)
        rel._last_polarity = polarity  # 非持久化属性，临时存
        if prev and prev != polarity:
            rel.evolving = "unstable"
            rel.last_observed = time.strftime("%Y-%m-%d")
            if not rel.first_observed:
                rel.first_observed = rel.last_observed
            return  # 矛盾时不更新 confidence
        elif prev and prev == polarity:
            rel.evolving = "stable"

        # 降权后的 delta
        base_deltas = {"positive": 0.4, "negative": -0.4, "close": 0.6}
        delta = base_deltas.get(signal, 0) * weight

        rel.confidence = max(0.5, min(5.0, rel.confidence + delta))

        # 类型变更：需满足观察次数门槛
        if target.observation_count >= self.MIN_OBS_FOR_TYPE:
            if signal == "positive" and rel.primary == "unknown" and rel.confidence >= 2:
                rel.primary = "casual_friend"
            elif signal == "negative" and rel.primary == "unknown" and rel.confidence <= 1.5:
                rel.primary = "avoids"
            elif signal == "close" and rel.confidence >= 3:
                rel.primary = "close_friend"

        rel.last_observed = time.strftime("%Y-%m-%d")
        if not rel.first_observed:
            rel.first_observed = rel.last_observed

    def _apply_self_signal(self, user_id: str, mood: str):
        """追踪 imbot 自己对某人的语气，连续同方向 → 印象更新"""
        if not user_id:
            return
        history = self._self_signal_counter.setdefault(user_id, [])
        history.append(mood)
        if len(history) > 5:
            history.pop(0)

        # 冷淡的语气
        cold_moods = {"冷淡", "烦躁"}
        warm_moods = {"温和", "好奇", "随性"}

        recent_cold = sum(1 for m in history[-3:] if m in cold_moods)
        recent_warm = sum(1 for m in history[-3:] if m in warm_moods)

        person = self.get_person(user_id)
        imp = person.imbots_own_impression
        if recent_cold >= 3:
            imp.attitude_to_imbot = "好像不太想理"
            imp.confidence = min(5.0, imp.confidence + 0.5)
        elif recent_warm >= 3:
            imp.attitude_to_imbot = "感觉能说上话"
            imp.confidence = min(5.0, imp.confidence + 0.5)

    def update_self_profile(self, dim: str, label: str, delta: int = 1):
        self.self_profile.update(dim, label, delta)

    def update_relationship(self, user_id: str, primary: str = "", delta: float = 0):
        person = self.get_person(user_id)
        rel = person.relationship_to_user
        if primary:
            if primary != rel.primary:
                rel.secondary.append(rel.primary)
                rel.primary = primary
                rel.evolving = "unstable"
        rel.confidence = max(0.5, min(5.0, rel.confidence + delta))
        rel.last_observed = time.strftime("%Y-%m-%d")
        if not rel.first_observed:
            rel.first_observed = rel.last_observed

    def update_impression(self, user_id: str, trait: str, value: str):
        person = self.get_person(user_id)
        imp = person.imbots_own_impression
        if trait == "personality":
            imp.personality = value
        elif trait == "attitude_to_user":
            imp.attitude_to_user = value
        elif trait == "attitude_to_imbot":
            imp.attitude_to_imbot = value
        elif trait == "speech_style":
            imp.speech_style = value
        imp.confidence = min(5.0, imp.confidence + 0.5)

    # ── 衰减 ──
    def apply_decay(self, decay_days: int = 30, fuzzy_days: int = 60):
        now = time.strftime("%Y-%m-%d")
        to_fuzzy = []
        for pid, person in list(self.people.items()):
            if not person.last_seen:
                continue
            days = self._days_since(person.last_seen, now)
            if days > fuzzy_days and person.relationship_to_user.confidence <= 1:
                to_fuzzy.append(pid)
            elif days > decay_days:
                rel = person.relationship_to_user
                multiplier = self.DECAY_MULTIPLIERS.get(rel.primary, 1.0)
                decay_amount = (days - decay_days) / 30 * multiplier
                rel.confidence = max(0.5, rel.confidence - decay_amount)
                if rel.confidence <= 1:
                    rel.primary = "unknown"
        for pid in to_fuzzy:
            del self.people[pid]

    def check_relationship_milestone(self, user_id: str) -> dict | None:
        """检查某人的关系是否刚刚从非 close_friend 升级到 close_friend。
        仅在 confidence >= 3 且 evolving == 'improving' 时触发一次。"""
        person = self.people.get(user_id)
        if not person:
            return None
        rel = person.relationship_to_user
        if rel.primary == "close_friend" and rel.confidence >= 3 and rel.evolving == "improving":
            if not getattr(rel, "_milestone_reported", False):
                rel._milestone_reported = True
                return {"person_name": person.current_display_name or user_id}
            return None
        # 如果关系降级了，重置一次以便将来再次升级时触发
        if rel.primary != "close_friend":
            rel._milestone_reported = False
        return None

    # ── 格式化 ──
    def format_context_for(self, speaker_id: str, is_group: bool) -> str:
        """返回预格式化的社交语境字符串，供 Prompt 注入"""
        person = self.people.get(speaker_id)
        if not person:
            return ""

        parts = []
        rel = person.relationship_to_user

        # 关系类型
        if rel.primary != "unknown" and rel.confidence >= 2:
            type_names = {
                "close_friend": "死党",
                "casual_friend": "熟人",
                "authority": "敬重的人",
                "subordinate": "下属",
                "teasing": "互怼的那种",
                "admires": "佩服的人",
                "avoids": "想避开的人",
            }
            label = type_names.get(rel.primary, rel.primary)
            suffix = ""
            if person.observation_count < self.MIN_OBS_FOR_CONFIDENT:
                suffix = "（不太确定）"
            if rel.evolving == "unstable":
                suffix = "（你的印象有点矛盾）"
            parts.append(f"你和这个人的关系：{label}{suffix}（可信度 {rel.confidence:.0f}/5）")

        # 外号
        if rel.user_calls_them:
            names = [n.name for n in rel.user_calls_them[-3:] if hasattr(n, 'name')]
            if names:
                parts.append(f"用户叫他：{'/'.join(names)}")

        # imbot 的印象
        imp = person.imbots_own_impression
        if imp.confidence >= 2:
            if imp.personality:
                parts.append(f"性格：{imp.personality}")
            if imp.speech_style:
                parts.append(f"说话风格：{imp.speech_style}")
            if imp.attitude_to_imbot:
                parts.append(f"他对你的态度：{imp.attitude_to_imbot}")

        if not parts:
            return ""
        return "\n".join(parts)

    def format_self_context(self) -> str:
        """返回所有者画像的预格式化字符串"""
        profile = self.self_profile
        dims = []
        dim_labels = {
            "rhythm": "作息",
            "emotional_style": "情绪表达",
            "social_pattern": "社交倾向",
            "reliability": "靠谱程度",
            "vulnerability": "脆弱面",
            "attitude_to_imbot": "对你的态度",
            "relational_pattern": "关系模式",
        }
        for dim in OwnerProfile.DIMENSIONS:
            d = getattr(profile, dim)
            if d.confidence >= 3:
                dims.append(f"{dim_labels[dim]}：{d.label}（{d.confidence}/5）")
        if dims:
            return "你对用户的了解：\n" + "\n".join(dims)
        return ""

    # ── 内部 ──
    @staticmethod
    def _days_since(date_str: str, now_str: str) -> int:
        try:
            t1 = time.mktime(time.strptime(date_str, "%Y-%m-%d"))
            t2 = time.mktime(time.strptime(now_str, "%Y-%m-%d"))
            return max(0, int((t2 - t1) / 86400))
        except (ValueError, OSError):
            return 0
