"""
前台进程检测。Windows: GetForegroundWindow + GetWindowText。
分类：游戏/办公/视频/浏览/未知。不采集窗口标题原文，只输出类别。
"""
import ctypes
import time
from ctypes import wintypes


# 进程名 → 类别映射
GAME_EXES = {
    "valorant.exe", "val.exe", "league of legends.exe", "lol.exe",
    "csgo.exe", "cs2.exe", "dota2.exe", "r5apex.exe", "apex.exe",
    "overwatch.exe", "genshinimpact.exe", "yuanShen.exe", "starrail.exe",
    "wuthering waves.exe", "naraka bladepoint.exe", "pubg.exe",
    "minecraft.exe", "terraria.exe", "eldenring.exe", "sekiro.exe",
    "darktide.exe", "monsterhunterrise.exe", "monsterhunterworld.exe",
}

OFFICE_EXES = {
    "word.exe", "excel.exe", "powerpoint.exe", "outlook.exe",
    "wps.exe", "notepad.exe", "code.exe", "devenv.exe",
    "pycharm.exe", "idea64.exe", "clion.exe", "sublime_text.exe",
}

BROWSER_EXES = {
    "chrome.exe", "msedge.exe", "firefox.exe", "opera.exe",
    "brave.exe", "qqbrowser.exe", "360chrome.exe",
}

VIDEO_EXES = {
    "bilibili.exe", "youku.exe", "iqiyi.exe", "tencentvideo.exe",
    "potplayer.exe", "vlc.exe", "mpc-hc.exe", "mpv.exe",
}

CHAT_EXES = {
    "wechat.exe", "qq.exe", "telegram.exe", "discord.exe",
    "dingtalk.exe", "feishu.exe", "tim.exe",
}

CATEGORY_LABELS = {
    "game": "在打游戏",
    "office": "在工作",
    "browser": "在逛网页",
    "video": "在看视频",
    "chat": "在聊天",
    "unknown": "",
}


class ProcessMonitor:
    def __init__(self):
        self.process_name = ""
        self.category = "unknown"
        self.game_just_ended = False
        self.game_duration_minutes = 0
        self._has_win32 = True
        self._last_category = "unknown"
        self._game_start_time = 0.0

    def _update(self):
        """每轮定时循环调用"""
        if not self._has_win32:
            return
        # 重置一次性标志
        self.game_just_ended = False
        try:
            self.process_name = self._get_foreground_process()
            self.category = self._classify(self.process_name)
        except Exception:
            if not getattr(self, "_os_warned", False):
                self._os_warned = True
                from astrbot.api import logger
                logger.info("进程检测不可用（非 Windows 或权限不足），跳过")
            self.process_name = ""
            self.category = "unknown"

        # 游戏退出检测
        prev, cur = self._last_category, self.category
        if cur == "game" and prev != "game":
            self._game_start_time = time.time()
        elif prev == "game" and cur != "game":
            if self._game_start_time > 0:
                duration = (time.time() - self._game_start_time) / 60
                if duration >= 120:  # 持续 >2h 才触发
                    self.game_just_ended = True
                    self.game_duration_minutes = int(duration)
            self._game_start_time = 0.0

        self._last_category = self.category

    def context_text(self) -> str:
        return CATEGORY_LABELS.get(self.category, "")

    @staticmethod
    def _get_foreground_process() -> str:
        """获取前台窗口所属进程名"""
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        if not hwnd:
            return ""
        pid = wintypes.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        try:
            handle = ctypes.windll.kernel32.OpenProcess(0x0400 | 0x0010, False, pid)
            if not handle:
                return ""
            buf = ctypes.create_unicode_buffer(260)
            ctypes.windll.psapi.GetModuleBaseNameW(handle, None, buf, 260)
            ctypes.windll.kernel32.CloseHandle(handle)
            return buf.value.lower() if buf.value else ""
        except Exception:
            return ""

    @staticmethod
    def _classify(name: str) -> str:
        if not name:
            return "unknown"
        if name in GAME_EXES:
            return "game"
        if name in OFFICE_EXES:
            return "office"
        if name in BROWSER_EXES:
            return "browser"
        if name in VIDEO_EXES:
            return "video"
        if name in CHAT_EXES:
            return "chat"
        return "unknown"
