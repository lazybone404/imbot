"""
媒体播放检测。Windows: 检测已知媒体播放器进程。
可扩展 winrt Windows.Media.Control 获取详细信息（标题/艺术家）。

输出粒度由配置控制：
- level 0: 仅返回 "在听歌"/"在看视频"
- level 1: + 标题
- level 2: + 艺术家
"""
import ctypes
from ctypes import wintypes


MUSIC_PLAYERS = {
    "cloudmusic.exe": "网易云音乐",
    "qqmusic.exe": "QQ音乐",
    "spotify.exe": "Spotify",
    "foobar2000.exe": "Foobar2000",
    "aimp.exe": "AIMP",
}

VIDEO_PLAYERS = {
    "bilibili.exe": "B站",
    "potplayer.exe": "PotPlayer",
    "vlc.exe": "VLC",
    "mpv.exe": "MPV",
    "tencentvideo.exe": "腾讯视频",
    "iqiyi.exe": "爱奇艺",
}

LIVE_PLAYERS = {
    "obs64.exe": "OBS",
    "douyu.exe": "斗鱼",
    "huya.exe": "虎牙",
}


class MediaMonitor:
    def __init__(self):
        self.is_playing = False
        self.media_type = ""        # "music" | "video" | "live" | ""
        self.player_name = ""
        self._has_win32 = True

    def _update(self):
        if not self._has_win32:
            return
        self.is_playing = False
        self.media_type = ""
        self.player_name = ""
        try:
            for proc, name in {**MUSIC_PLAYERS, **VIDEO_PLAYERS, **LIVE_PLAYERS}.items():
                if self._is_process_running(proc):
                    self.is_playing = True
                    self.player_name = name
                    if proc in MUSIC_PLAYERS:
                        self.media_type = "music"
                    elif proc in VIDEO_PLAYERS:
                        self.media_type = "video"
                    elif proc in LIVE_PLAYERS:
                        self.media_type = "live"
                    break
        except Exception:
            if not getattr(self, "_os_warned", False):
                self._os_warned = True
                from astrbot.api import logger
                logger.info("媒体检测不可用（非 Windows 或权限不足），跳过")

    def context_text(self, expose_level: int = 0) -> str:
        """expose_level: 0=仅类型, 1=+标题, 2=+艺术家"""
        if not self.is_playing:
            return ""
        labels = {"music": "在听歌", "video": "在看视频", "live": "在看直播"}
        base = labels.get(self.media_type, "")
        if expose_level >= 1 and self.title:
            base += f"《{self.title}》"
        if expose_level >= 2 and self.artist:
            base += f"（{self.artist}）"
        return base

    @staticmethod
    def _is_process_running(name: str) -> bool:
        """检查进程是否在运行"""
        try:
            kernel32 = ctypes.windll.kernel32
            snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
            if snapshot == -1:
                return False

            class PROCESSENTRY32(ctypes.Structure):
                _fields_ = [
                    ("dwSize", wintypes.DWORD),
                    ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.POINTER(wintypes.ULONG)),
                    ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_char * 260),
                ]

            pe = PROCESSENTRY32()
            pe.dwSize = ctypes.sizeof(PROCESSENTRY32)
            if kernel32.Process32First(snapshot, ctypes.byref(pe)):
                while True:
                    exe = pe.szExeFile.decode("utf-8", errors="ignore").lower()
                    if exe == name:
                        kernel32.CloseHandle(snapshot)
                        return True
                    if not kernel32.Process32Next(snapshot, ctypes.byref(pe)):
                        break
            kernel32.CloseHandle(snapshot)
        except Exception:
            pass
        return False
