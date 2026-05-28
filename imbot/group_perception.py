import re


class GroupPerception:
    """
    群聊感知器。需要 RuntimeState 做说话者身份识别。
    """

    def __init__(self, state):
        self._state = state
        self._msg_timestamps: dict[str, list[float]] = {}

    def detect_mention(self, event) -> dict:
        """
        检测消息是否@了imbot或所有者。
        fallback: 若无法从消息结构获取@信息，用 message_str 做字符串匹配。
        """
        result = {
            "mentioned_imbot": False,
            "mentioned_owner": False,
            "mention_content": "",
        }

        msg_str = event.message_str or ""

        # 尝试从消息对象中获取 @ 信息
        msg_obj = getattr(event, "message_obj", None)
        if msg_obj:
            imbot_id = getattr(msg_obj, "self_id", "")
            mentions = getattr(msg_obj, "mentions", None) or []
            for m in mentions:
                uid = str(getattr(m, "user_id", ""))
                if uid == self._state._owner_id:
                    result["mentioned_owner"] = True
                if uid and uid == imbot_id:
                    result["mentioned_imbot"] = True

        # fallback: 字符串匹配
        if "imbot" in msg_str:
            result["mentioned_imbot"] = True
        if self._state._owner_id:
            result["mentioned_owner"] = f"@{self._state._owner_id}" in msg_str

        return result

    def record_activity(self, group_id: str):
        """在全局 GROUP_MESSAGE 钩子中调用，追踪群消息时间戳。"""
        import time
        if not group_id:
            return
        now = time.time()
        stamps = self._msg_timestamps.setdefault(group_id, [])
        stamps.append(now)
        stamps[:] = [t for t in stamps if now - t < 60]
        # 定期清理超过 1 小时未活跃的群组
        stale = [gid for gid, ts_list in self._msg_timestamps.items()
                 if not ts_list or now - max(ts_list) > 3600]
        for gid in stale:
            del self._msg_timestamps[gid]

    def assess_activity(self, event) -> str:
        """根据已记录的消息时间戳判断群活跃度（由 record_activity 提供数据）。"""
        group_id = event.get_group_id()
        if not group_id:
            return "安静"
        stamps = self._msg_timestamps.get(group_id, [])
        count = len(stamps)
        if count <= 2:
            return "安静"
        elif count <= 5:
            return "正常"
        elif count <= 10:
            return "活跃"
        else:
            return "很吵"

    def identify_speakers_in_context(self, event) -> list[dict]:
        """
        识别消息上下文中出现的说话者身份。
        """
        speakers = []
        sender_id = event.get_sender_id()
        if sender_id:
            speakers.append({
                "user_id": sender_id,
                "type": self._state.classify_speaker(sender_id),
            })
        return speakers

    def extract_topic(self, event) -> str:
        """从群聊最近消息提取高频话题词"""
        msg = getattr(event, "message_str", "") or ""
        words = re.findall(r"[一-鿿]{2,}", msg)
        # 过滤停用词
        stop = {"这个", "那个", "什么", "怎么", "为什么", "是不是", "有没有", "不知道", "就是", "然后", "但是", "可以", "已经", "没有"}
        words = [w for w in words if w not in stop]
        if not words:
            return ""
        # 返回最高频词
        from collections import Counter
        return Counter(words).most_common(1)[0][0] if words else ""
