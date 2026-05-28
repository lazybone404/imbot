import json
import math
import os
import random
import re
import time
from datetime import datetime, timedelta


from imbot.utils import atomic_write_json, calc_time_feel, days_since

RECALL_PATTERNS = [
    "你记得{seed}",
    "上次好像{seed}",
    "那时候{seed}",
    "突然想到{seed}",
    "不知道为什么想到{seed}",
]

NEW_INFO_KEYWORDS = re.compile(
    r"告诉|听说|你知道|对了|忘了说|跟你说|想起来|其实|说实话"
)


class MemoryManager:
    def __init__(self, path: str, max_entries: int = 150, config: dict = None,
                 plugin_context=None):
        self._path = path
        self._max = max_entries
        self._cfg = config or {}
        self._deep_max = self._cfg.get("deep_pool_size", 500)
        self._core_limit = self._cfg.get("core_limit", 5)
        self._owner_decay_mult = self._cfg.get("owner_decay_mult", 1.5)
        self._owner_retrieval_bonus = self._cfg.get("owner_retrieval_bonus", 1.0)
        self._owner_core_limit = self._cfg.get("owner_core_limit", 3)
        self._plugin_context = plugin_context
        self._decay_rates = self._cfg.get("decay_rates", {
            "event": 0.02, "impression": 0.01, "emotion": 0.025,
        })
        self._intensity_mod = self._cfg.get("intensity_decay_mod", 0.5)
        self._negative_mod = self._cfg.get("negative_emotion_mod", 0.7)
        self._mood_congruence = self._cfg.get("mood_congruence_bonus", 1.5)
        self._output_diversity = self._cfg.get("output_diversity", True)
        self._tot_enabled = self._cfg.get("tot_enabled", True)
        self._emotion_residue = self._cfg.get("emotion_residue", True)
        self._consolidation_enabled = self._cfg.get("consolidation", True)

        self.entries: list[dict] = []
        self.deep_entries: list[dict] = []
        self.emotion_residues: list[str] = []
        self._last_write_time: float = 0
        self._recall_hits = 0
        self._recall_total = 0

        self._load()

    # ═══════════════════════════════════════════
    # 写入
    # ═══════════════════════════════════════════
    def should_write(self, user_msg: str, response_text: str,
                     emotion_intensity: int, is_proactive: bool = False,
                     time_period: str = "", is_owner: bool = False) -> bool:
        """写入门控：返回 True 表示应该写入"""
        if is_owner:
            return True
        now = time.time()
        if now - self._last_write_time < 300:
            return False
        if emotion_intensity >= 3:
            return True
        if len(user_msg) > 50:
            return True
        if time_period in ("深夜", "凌晨"):
            return True
        if NEW_INFO_KEYWORDS.search(user_msg):
            return True
        if is_proactive:
            return True
        if len(user_msg) < 10 and not any(
            kw in user_msg for kw in ("烦", "累", "哈", "想", "哭", "气")
        ):
            return False
        return False

    def add(self, summary: str, type_: str, emotion_intensity: int,
            env_snapshot: dict, user_id: str = "",
            seed: str = "", event_importance: float = 0.5,
            is_owner: bool = False):
        if not summary.strip():
            return

        self._last_write_time = time.time()

        entry = self._make_entry(
            type_=type_,
            seed=seed or summary[:80],
            summary=summary[:200],
            emotion_intensity=max(1, min(5, emotion_intensity)),
            event_importance=event_importance,
            env_snapshot=env_snapshot,
            user_id=user_id,
            is_owner=is_owner,
        )

        # core 检查
        if emotion_intensity >= 5 and event_importance >= 1.5:
            if is_owner:
                owner_cores = sum(1 for e in self.entries if e.get("core") and e.get("owner"))
                if owner_cores >= self._owner_core_limit:
                    self._evict_one_core(owner_only=True)
                entry["core"] = True
            else:
                core_count = sum(1 for e in self.entries if e.get("core"))
                if core_count >= self._core_limit:
                    self._evict_one_core(owner_only=False)
                entry["core"] = True

        self.entries.append(entry)
        # 标记待 LLM 生成 seed 的条目（高重要性 + 有 plugin_context）
        if event_importance >= 1.0 and self._plugin_context and not seed:
            entry["_pending_seed"] = True
        if entry.get("core"):
            from astrbot.api import logger
            logger.info(f"永久记忆标记: {entry['seed'][:40]}...")
        self._trim()

    def _make_entry(self, **kwargs) -> dict:
        seed = kwargs.get("seed", "")
        intensity = kwargs.get("emotion_intensity", 1)
        importance = kwargs.get("event_importance", 0.5)
        weight = round(importance + min(1.0, len(seed) / 50) + intensity * 0.5, 2)

        return {
            "id": f"mem_{int(time.time() * 1000)}_{random.randint(0, 9999):04d}",
            "type": kwargs.get("type_", "event"),
            "seed": seed,
            "summary": kwargs.get("summary", seed[:100]),
            "time_feel": "recent",
            "user_id": kwargs.get("user_id", ""),
            "timestamp": datetime.now().isoformat(),
            "emotion_intensity": intensity,
            "event_importance": importance,
            "core": kwargs.get("core", False),
            "owner": kwargs.get("is_owner", False),
            "superseded_by": None,
            "env_snapshot": kwargs.get("env_snapshot", {}),
            "weight": weight,
            "decay_next_at": (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d"),
            "tags": [],
            "group_topic": kwargs.get("group_topic"),
            "embedding": None,
            "_recall_count": 0,
        }

    # ═══════════════════════════════════════════
    # 浮现
    # ═══════════════════════════════════════════
    def retrieve_relevant(self, time_period: str = "", mood: str = "",
                          user_id: str = "", group_topic: str = "",
                          limit: int = 3) -> list[dict]:
        self._recall_total += 1

        # 语义搜索桩
        semantic = self._semantic_search(time_period + " " + mood, limit)
        if semantic:
            self._recall_hits += 1
            return semantic

        scored = []
        for mem in self.entries:
            score = mem["weight"]

            # 环境匹配
            if time_period and mem["env_snapshot"].get("time_period") == time_period:
                score += 1
            elif time_period and self._periods_near(time_period, mem["env_snapshot"].get("time_period", "")):
                score += 0.5

            # 情绪一致性
            if mood and mem["env_snapshot"].get("she_was") == mood:
                score += self._mood_congruence

            # 用户匹配
            if user_id and mem.get("user_id") == user_id:
                score += 1

            # 近因加成
            hours = self._hours_since(mem["timestamp"])
            if hours < 24:
                score += 1.0
            elif hours < 72:
                score += 0

            # time_feel 加成
            if mem.get("time_feel") in ("recent", "this_week"):
                score += 0.5

            # 群聊话题
            if group_topic and mem.get("group_topic") == group_topic:
                score += 1.5

            # 所有者加成
            if mem.get("owner"):
                score += self._owner_retrieval_bonus

            # 回忆增强
            score += min(0.3, mem.get("_recall_count", 0) * 0.3)

            scored.append((score, mem))

        scored.sort(key=lambda x: x[0], reverse=True)

        # 舌尖现象
        tot_seed = self._check_tot(scored, limit)

        result = [m for _, m in scored[:limit]]

        # 记忆增强
        for _, m in scored[:limit]:
            m["_recall_count"] = m.get("_recall_count", 0) + 1
            m["weight"] = min(5.0, m["weight"] + 0.3)
            m["decay_next_at"] = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")

        if result:
            self._recall_hits += 1
            if tot_seed:
                result = list(result)
                result.append({"_tot": True, "seed": tot_seed})

        return result

    def _check_tot(self, scored: list, limit: int) -> str | None:
        if not self._tot_enabled:
            return None
        if len(scored) <= limit:
            return None
        # 第 limit 名和第 limit+1 名的分差
        nth_score = scored[limit - 1][0]
        next_score = scored[limit][0]
        if 0 < nth_score - next_score < 0.5 and scored[limit][1]["weight"] > 3.0:
            return scored[limit][1]["seed"]
        return None

    def get_recall_rate(self) -> tuple[int, int]:
        return self._recall_hits, self._recall_total

    # ═══════════════════════════════════════════
    # 衰减（艾宾浩斯指数）
    # ═══════════════════════════════════════════
    def apply_decay(self):
        today = datetime.now().strftime("%Y-%m-%d")
        for mem in self.entries:
            if mem.get("core"):
                continue
            if mem.get("decay_next_at", "") > today:
                continue
            self._decay_one(mem, today)

        # 深层池同样衰减
        for mem in self.deep_entries:
            if mem.get("decay_next_at", "") > today:
                continue
            self._decay_one(mem, today)

        self._trim()
        self._trim_deep()

    def _decay_one(self, mem: dict, today: str):
        days = days_since(mem["timestamp"])
        if days <= 0:
            return

        base_tau = 14
        rate = self._decay_rates.get(mem.get("type", "event"), 0.02)
        tau = base_tau * (0.02 / max(0.005, rate))
        if mem["emotion_intensity"] >= 4:
            tau *= (2 - self._intensity_mod)
        if mem["emotion_intensity"] <= 2:
            tau *= self._negative_mod

        if mem.get("owner"):
            tau *= self._owner_decay_mult

        mem["weight"] = round(mem["weight"] * math.exp(-days / tau), 2)
        mem["weight"] = max(0.1, mem["weight"])
        mem["decay_next_at"] = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

        # time_feel 老化
        mem["time_feel"] = calc_time_feel(days)

    # ═══════════════════════════════════════════
    # 离线巩固
    # ═══════════════════════════════════════════
    def consolidate(self):
        """启动时调用：强化重要记忆，合并相似，整理碎片"""
        if not self._consolidation_enabled:
            return
        from astrbot.api import logger
        before = len(self.entries)
        logger.debug(f"离线巩固开始: 记忆{len(self.entries)}条, 深层{len(self.deep_entries)}条")
        now = datetime.now()

        # 强化高强度/高重要性记忆
        for mem in self.entries:
            if mem.get("core"):
                continue
            if mem["emotion_intensity"] >= 4 or mem.get("event_importance", 0) >= 1.0:
                mem["weight"] = min(5.0, mem["weight"] + 0.1)

        # time_feel 按时间戳自动老化
        for mem in self.entries:
            days = days_since(mem["timestamp"])
            mem["time_feel"] = calc_time_feel(days)

        # 合并相似 seed（关键词交集 > 50%）
        merged = self._merge_similar()
        if merged:
            self.entries = merged

        self._trim()
        from astrbot.api import logger
        if len(self.entries) != before:
            logger.info(f"离线巩固完成: {before}→{len(self.entries)}条 (合并了{before - len(self.entries)}条)")

    def _merge_similar(self) -> list[dict]:
        """合并语义相似的记忆：关键词交集 > 50% → 合并 seed"""
        result = list(self.entries)
        i = 0
        while i < len(result):
            a = result[i]
            if a.get("core"):
                i += 1
                continue
            a_words = set(re.findall(r"[一-鿿]+", a.get("seed", "")))
            if len(a_words) < 3:
                i += 1
                continue
            j = i + 1
            while j < len(result):
                b = result[j]
                if b.get("core"):
                    j += 1
                    continue
                b_words = set(re.findall(r"[一-鿿]+", b.get("seed", "")))
                if len(b_words) < 3:
                    j += 1
                    continue
                intersection = a_words & b_words
                union = a_words | b_words
                if len(intersection) / max(1, len(union)) > 0.5:
                    # 合并：保留更抽象的种子
                    a["seed"] = a.get("seed", "")[:40] + "..." if len(a.get("seed", "")) > len(b.get("seed", "")) else b.get("seed", "")[:40] + "..."
                    a["weight"] = max(a["weight"], b["weight"]) + 0.2
                    a["emotion_intensity"] = max(a["emotion_intensity"], b["emotion_intensity"])
                    result.pop(j)
                else:
                    j += 1
            i += 1
        return result

    # ═══════════════════════════════════════════
    # 内存管理
    # ═══════════════════════════════════════════
    def _trim(self):
        self.entries.sort(key=lambda m: (m.get("owner", False), m["weight"]), reverse=True)
        overflow = self.entries[self._max:]
        self.entries = self.entries[:self._max]

        # 淘汰 → 深层池 / 情绪残留
        for mem in overflow:
            if mem.get("core"):
                self.entries.append(mem)
                continue
            if mem["emotion_intensity"] >= 3 or mem.get("type") == "impression":
                self.deep_entries.append(mem)
                self._trim_deep()
            else:
                if self._emotion_residue:
                    mood = mem["env_snapshot"].get("she_was", "")
                    if mood:
                        self.emotion_residues.append(mood)
                        if len(self.emotion_residues) > 100:
                            self.emotion_residues = self.emotion_residues[-50:]

    def _trim_deep(self):
        self.deep_entries.sort(key=lambda m: (m.get("owner", False), m["weight"]), reverse=True)
        self.deep_entries = self.deep_entries[:self._deep_max]

    def _evict_one_core(self, owner_only: bool = False):
        """超过 core_limit 时替换权重最低的 core。owner_only=True 时只踢所有者 core"""
        core_entries = [e for e in self.entries if e.get("core")]
        if owner_only:
            core_entries = [e for e in core_entries if e.get("owner")]
        if not core_entries and owner_only:
            core_entries = [e for e in self.entries if e.get("core")]
        if core_entries:
            core_entries.sort(key=lambda e: e["weight"])
            core_entries[0]["core"] = False

    # ═══════════════════════════════════════════
    # 打捞 + 修正
    # ═══════════════════════════════════════════
    def salvage(self, query: str) -> bool:
        """深层池匹配打捞：成功返回 True"""
        words = set(re.findall(r"[一-鿿]+", query))
        if len(words) < 2:
            return False
        for mem in list(self.deep_entries):
            m_words = set(re.findall(r"[一-鿿]+", mem.get("seed", "")))
            if len(words & m_words) / max(1, len(words)) > 0.4:
                mem["weight"] = 2.0
                mem["decay_next_at"] = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
                self.entries.append(mem)
                self.deep_entries.remove(mem)
                self._trim()
                self._trim_deep()
                return True
        return False

    def supersede(self, old_id: str, new_entry: dict):
        """用户修正记忆：标记旧记忆 + 写入新记忆"""
        for mem in self.entries:
            if mem["id"] == old_id:
                mem["superseded_by"] = new_entry.get("id", "")
                mem["weight"] -= 1.0
        for mem in self.deep_entries:
            if mem["id"] == old_id:
                mem["superseded_by"] = new_entry.get("id", "")
        self.entries.append(new_entry)
        self._trim()

    # ═══════════════════════════════════════════
    # 格式化输出
    # ═══════════════════════════════════════════
    def format_memories(self, memories: list[dict], is_group: bool = False,
                        attitude_to_imbot: str = "") -> str:
        parts = []
        recount_style = "点到即止" if is_group else "可以多些情感细节"
        if attitude_to_imbot:
            parts.append(f"（你对这个人的态度：{attitude_to_imbot}）")

        for mem in memories:
            if mem.get("_tot"):
                parts.append(f"（隐约觉得有什么事，但想不起来——好像是关于：{mem['seed']}）")
                continue

            seed = mem.get("seed", mem.get("summary", ""))
            if self._output_diversity:
                pattern = random.choice(RECALL_PATTERNS)
                line = pattern.format(seed=seed)
            else:
                line = f"你记得: {seed}"
            if mem.get("superseded_by"):
                line += "（不过你后来觉得不是这样）"
            parts.append(line)

        if parts:
            parts.insert(0, f"回忆方式：{recount_style}")
        return "\n".join(parts)

    def format_emotion_residue(self) -> str:
        if not self.emotion_residues:
            return ""
        mood = random.choice(self.emotion_residues[-10:])
        if random.random() < 0.1:
            return f"（不知道为什么，总觉得今天有点{mood}）"
        return ""

    # ═══════════════════════════════════════════
    # 持久化
    # ═══════════════════════════════════════════
    def _load(self):
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.entries = data.get("entries", [])
            self.deep_entries = data.get("deep_entries", [])
            self.emotion_residues = data.get("emotion_residues", [])
            self._last_write_time = data.get("last_write_time", 0)
            self._recall_hits = data.get("recall_hits", 0)
            self._recall_total = data.get("recall_total", 0)
            self._trim()
            self._trim_deep()
        except FileNotFoundError:
            pass
        except json.JSONDecodeError:
            backup = self._path + ".corrupted"
            try:
                os.rename(self._path, backup)
            except OSError:
                pass

    def save(self):
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            atomic_write_json(self._path, {
                "entries": self.entries,
                "deep_entries": self.deep_entries,
                "emotion_residues": self.emotion_residues,
                "last_write_time": self._last_write_time,
                "recall_hits": self._recall_hits,
                "recall_total": self._recall_total,
            })
        except OSError:
            pass

    def reset(self):
        self.entries.clear()
        self.deep_entries.clear()
        self.emotion_residues.clear()
        self._recall_hits = 0
        self._recall_total = 0

    # ═══════════════════════════════════════════
    # 内部工具
    # ═══════════════════════════════════════════
    async def generate_pending_seeds(self):
        """异步为标记 _pending_seed 的条目调用 LLM 生成摘要 seed（每次最多 3 条）"""
        if not self._plugin_context:
            return
        pending = [e for e in self.entries if e.get("_pending_seed")]
        if not pending:
            return
        from astrbot.api import logger
        count = 0
        for entry in pending[:3]:
            try:
                summary = entry.get("summary", "")
                if not summary or len(summary) < 20:
                    continue
                owner_id = self._cfg.get("owner_id", "")
                umo = f"aiocqhttp:FriendMessage:{owner_id}" if owner_id else ""
                prov_id = await self._plugin_context.get_current_chat_provider_id(umo) if umo else ""
                if not prov_id:
                    continue
                resp = await self._plugin_context.llm_generate(
                    chat_provider_id=prov_id,
                    prompt=f"用一句话（不超过20字）摘要以下对话的关键信息。只输出摘要本身，不加任何说明。\n\n{summary[:500]}",
                )
                if resp and resp.completion_text:
                    seed = resp.completion_text.strip()[:60]
                    entry["seed"] = seed
                entry.pop("_pending_seed", None)
                count += 1
            except Exception:
                logger.info("seed 生成失败", exc_info=True)
                entry.pop("_pending_seed", None)
        if count:
            self.save()

    def find_anchor_match(self, time_period: str, min_weight: float = 2.0) -> dict | None:
        """在记忆池中查找与当前时段匹配的高权重记忆。返回记忆条目或 None。"""
        for mem in self.entries:
            env = mem.get("env_snapshot", {})
            if env.get("time_period") == time_period and mem.get("weight", 0) >= min_weight:
                return mem
        for mem in self.deep_entries:
            env = mem.get("env_snapshot", {})
            if env.get("time_period") == time_period and mem.get("weight", 0) >= min_weight:
                return mem
        return None

    async def compute_embeddings_pending(self):
        """为没有 embedding 的条目异步计算向量（每次最多 2 条）。失败则标记跳过。"""
        if not self._plugin_context:
            return
        try:
            providers = self._plugin_context.get_all_embedding_providers()
            if not providers:
                return
            target = getattr(self, "_embedding_provider", "")
            emb_prov = None
            if target:
                for p in providers:
                    if getattr(p, "provider_name", "") == target or getattr(p, "provider_id", "") == target:
                        emb_prov = p
                        break
            if not emb_prov:
                emb_prov = providers[0]  # 降级：第一个可用
            pending = [e for e in self.entries if "embedding" not in e and not e.get("_emb_skip")]
            if not pending:
                return
            count = 0
            for entry in pending[:2]:
                try:
                    seed = entry.get("seed", "")
                    if not seed:
                        continue
                    vec = await emb_prov.get_embedding(seed[:200])
                    entry["embedding"] = list(vec)
                    count += 1
                except Exception:
                    entry["_emb_skip"] = True
            if count:
                self.save()
        except Exception:
            from astrbot.api import logger
            logger.error("embedding 计算失败", exc_info=True)

    def _semantic_search(self, query_vec: list[float], limit: int) -> list[dict]:
        """用 cosine 相似度对全部有向量的记忆排序，返回 top-N"""
        import math
        scored = []
        for mem in self.entries:
            v = mem.get("embedding")
            if not v:
                continue
            dot = sum(a * b for a, b in zip(query_vec, v))
            norm_a = math.sqrt(sum(a * a for a in query_vec))
            norm_b = math.sqrt(sum(b * b for b in v))
            sim = dot / (norm_a * norm_b) if norm_a and norm_b else 0
            scored.append((sim, mem))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [m for _, m in scored[:limit]]

    @staticmethod
    def _hours_since(date_str: str) -> float:
        try:
            dt = datetime.fromisoformat(date_str)
            return max(0, (datetime.now() - dt).total_seconds() / 3600)
        except (ValueError, OSError):
            return 999

    @staticmethod
    def _periods_near(a: str, b: str) -> bool:
        """相近时段：深夜↔凌晨, 清晨↔上午"""
        pairs = [("深夜", "凌晨"), ("清晨", "上午"), ("下午", "傍晚")]
        for x, y in pairs:
            if {a, b} == {x, y}:
                return True
        return False
