"""
空闲状态检测。Windows: GetLastInputInfo，非 Windows: 降级为 "active"。
状态机: active →(>5min)→ away →(>30min)→ long_away →(回来)→ just_returned → active
"""
import time

try:
    import ctypes
    from ctypes import wintypes
    _HAS_WIN32 = True
except ImportError:
    _HAS_WIN32 = False


class IdleMonitor:
    def __init__(self):
        self._prev_state = "active"
        self.state = "active"
        self.idle_seconds = 0.0

    def _update(self):
        """每轮定时循环调用，推进状态机"""
        if _HAS_WIN32:
            self.idle_seconds = self._get_idle_win32()
        else:
            self.idle_seconds = 0.0

        # 状态机
        if self.idle_seconds > 1800:       # >30min
            new_state = "long_away"
        elif self.idle_seconds > 300:      # >5min
            new_state = "away"
        else:
            new_state = "active"

        # just_returned 检测
        if self._prev_state in ("away", "long_away") and new_state == "active":
            self.state = "just_returned"
        else:
            self.state = new_state

        self._prev_state = self.state if self.state != "just_returned" else "active"

    @staticmethod
    def _get_idle_win32() -> float:
        """Windows: 自上次输入以来的毫秒数"""
        try:
            class LASTINPUTINFO(ctypes.Structure):
                _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]
            lii = LASTINPUTINFO()
            lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
            ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii))
            return (ctypes.windll.kernel32.GetTickCount() - lii.dwTime) / 1000.0
        except Exception:
            return 0.0

    def context_text(self) -> str:
        """自然语言描述"""
        if self.state == "long_away":
            return "人不在"
        elif self.state == "away":
            return "可能走开了"
        elif self.state == "just_returned":
            return "刚回来"
        return ""
