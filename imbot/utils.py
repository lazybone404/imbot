"""共享工具：时间语境 + LLM JSON 解析 + 轻量调用。"""
import asyncio
import json as _json
import time as _time
from datetime import datetime

# ── 记忆时间感知 ──
TIME_FEEL_THRESHOLDS = (
    (3, "recent"),
    (14, "this_week"),
    (60, "a_while_ago"),
    (float("inf"), "long_ago"),
)


def calc_time_feel(days: int) -> str:
    for threshold, label in TIME_FEEL_THRESHOLDS:
        if days <= threshold:
            return label
    return "long_ago"


def days_since(date_str: str) -> int:
    try:
        dt = datetime.fromisoformat(date_str)
        return max(0, (datetime.now() - dt).days)
    except (ValueError, OSError):
        return 0

# ── 时间窗口解析 ──


def parse_time_window(start: str, end: str) -> bool:
    """检查当前时间是否在 HH:MM 窗口内（支持跨日如 22:00-02:00）。解析失败返回 False。"""
    try:
        sh, sm = int(start.split(":")[0]), int(start.split(":")[1])
        eh, em = int(end.split(":")[0]), int(end.split(":")[1])
        now_min = _time.localtime().tm_hour * 60 + _time.localtime().tm_min
        start_min = sh * 60 + sm
        end_min = eh * 60 + em
        if start_min > end_min:  # 跨日
            return now_min >= start_min or now_min < end_min
        return start_min <= now_min < end_min
    except (ValueError, IndexError):
        return False

WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

PERIOD_MAP = {
    (5, 8): "清晨", (8, 11): "上午", (11, 13): "中午",
    (13, 17): "下午", (17, 20): "傍晚", (20, 0): "深夜", (0, 5): "凌晨",
}

TIME_FEEL_MAP = {
    "清晨": "天刚亮，新的一天", "上午": "上午了，一天刚开始",
    "中午": "中午了", "下午": "下午了",
    "傍晚": "傍晚了，天快黑了", "深夜": "夜深了，该休息了", "凌晨": "凌晨了，又熬夜了",
}


def get_current_time_context() -> dict:
    now = datetime.now()
    hour = now.hour
    period = "深夜"
    for (start, end), p in PERIOD_MAP.items():
        if start <= hour < end or (start > end and (hour >= start or hour < end)):
            period = p
            break
    return {
        "date": f"{now.year}年{now.month}月{now.day}日",
        "time": now.strftime("%H:%M"),
        "hour": hour,
        "weekday": WEEKDAY_CN[now.weekday()],
        "period": period,
        "time_feel": TIME_FEEL_MAP.get(period, "夜深了"),
    }


# ── LLM 辅助 ──

def parse_llm_json(text: str) -> dict | None:
    """从 LLM 输出中解析 JSON。支持 ```json...``` 包裹。失败返回 None。"""
    try:
        clean = text.strip()
        if clean.startswith("```"):
            if "\n" in clean:
                clean = clean.split("\n", 1)[1]
            else:
                clean = clean[3:]
            clean = clean.rsplit("```", 1)[0].strip()
        return _json.loads(clean)
    except Exception:
        return None


async def call_lightweight_llm(engine, prompt: str, timeout: float = 5.0) -> str:
    """调用轻量 LLM，超时或失败返回空字符串。"""
    try:
        result = await asyncio.wait_for(
            engine._llm_generate_lightweight(prompt), timeout=timeout
        )
        return result or ""
    except (asyncio.TimeoutError, Exception):
        return ""
