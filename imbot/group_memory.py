import json
import os
from datetime import datetime, timedelta

from imbot.utils import atomic_write_json, calc_time_feel, days_since


class GroupMemoryManager:
    def __init__(self, base_dir: str, max_entries: int = 30, base_decay_rate: float = 0.03):
        self._base_dir = base_dir
        self._max = max_entries
        self._decay_rate = base_decay_rate
        self._caches: dict[str, list[dict]] = {}

    def _path(self, group_id: str) -> str:
        return os.path.join(self._base_dir, f"{group_id}.json")

    def _get(self, group_id: str) -> list[dict]:
        if group_id not in self._caches:
            self._load(group_id)
        return self._caches.setdefault(group_id, [])

    # ── 写入 ──
    def add(self, group_id: str, summary: str, type_: str,
            emotion_intensity: int, env_snapshot: dict,
            seed: str = "", group_topic: str = ""):
        if not summary.strip() or not group_id:
            return
        entries = self._get(group_id)
        entry = {
            "id": f"gmem_{len(entries) + 1:04d}",
            "type": type_,
            "seed": seed or summary[:80],
            "summary": summary[:200],
            "time_feel": "recent",
            "timestamp": datetime.now().isoformat(),
            "emotion_intensity": max(1, min(5, emotion_intensity)),
            "env_snapshot": env_snapshot,
            "weight": min(1.0, len(summary) / 50) + emotion_intensity * 0.5,
            "tags": [],
            "group_topic": group_topic,
            "decay_next_at": (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d"),
        }
        entries.append(entry)
        self._trim(group_id)

    # ── 检索（含话题匹配）──
    def retrieve_relevant(self, group_id: str, limit: int = 3,
                          group_topic: str = "") -> list[dict]:
        entries = self._get(group_id)
        for e in entries:
            e["_score"] = e["weight"]
            # time_feel 加成
            if e.get("time_feel") in ("recent", "this_week"):
                e["_score"] += 0.5
            # 话题匹配
            if group_topic and e.get("group_topic") == group_topic:
                e["_score"] += 1.5
        entries.sort(key=lambda m: m.get("_score", 0), reverse=True)
        result = entries[:limit]
        for e in result:
            e.pop("_score", None)
        return result

    # ── 衰减 ──
    def apply_decay(self, group_id: str):
        entries = self._get(group_id)
        today = datetime.now().strftime("%Y-%m-%d")
        for mem in entries:
            if mem.get("decay_next_at", "") > today:
                continue
            factor = 0.5 if mem["emotion_intensity"] >= 4 else 1.0
            mem["weight"] = max(0.1, mem["weight"] - self._decay_rate * factor)
            mem["decay_next_at"] = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
            # time_feel 老化
            days = days_since(mem["timestamp"])
            mem["time_feel"] = calc_time_feel(days)
        self._trim(group_id)

    # ── 内部 ──
    def _trim(self, group_id: str):
        entries = self._get(group_id)
        entries.sort(key=lambda m: m["weight"], reverse=True)
        self._caches[group_id] = entries[:self._max]

    def _load(self, group_id: str):
        try:
            with open(self._path(group_id), "r", encoding="utf-8") as f:
                self._caches[group_id] = json.load(f)
            self._trim(group_id)
        except FileNotFoundError:
            self._caches[group_id] = []
        except json.JSONDecodeError:
            backup = self._path(group_id) + ".corrupted"
            try:
                os.rename(self._path(group_id), backup)
            except OSError:
                pass
            self._caches[group_id] = []

    def save(self, group_id: str):
        if group_id not in self._caches:
            return
        try:
            os.makedirs(self._base_dir, exist_ok=True)
            atomic_write_json(self._path(group_id), self._caches[group_id])
        except OSError:
            pass

    def reset(self, group_id: str = None):
        if group_id:
            self._caches.pop(group_id, None)
            try:
                os.remove(self._path(group_id))
            except OSError:
                pass
        else:
            self._caches.clear()

