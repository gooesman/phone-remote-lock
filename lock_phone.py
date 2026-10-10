# -*- coding: utf-8 -*-
"""
phone-remote-lock —— 把手机变成电脑的「遥控对象」：锁屏 / 音量 / 亮度 / 刷视频

原理：全部走 adb（无线调试），不装手机端 App、不需要 root。
    keyevent 223=休眠  24=音量+  25=音量-  164=静音  85=播放暂停
    亮度  -> settings put system screen_brightness
    刷视频 -> input swipe 上/下滑（抖音、B站短视频流）

前置条件（详见 README.md）：
    1. 手机已开「开发者选项」+「USB 调试」+「USB 调试(安全设置)」
    2. 手机已开「无线调试」，本机与手机在同一 Wi-Fi
    3. 本机已安装 adb（Android Platform Tools）

用法：
    python lock_phone.py                      托盘常驻 + 全局热键（左键唤起面板/映射层）
    python lock_phone.py panel                打开控制面板（按钮：亮度/音量/锁屏/刷视频）
    python lock_phone.py overlay-toggle       切换鼠标映射层（手机形状矩形，可黑可白）
    python lock_phone.py overlay-on / overlay-off
    python lock_phone.py menu                 交互式遥控面板（双击 remote.bat）
    python lock_phone.py lock                 锁屏
    python lock_phone.py vol-up | vol-down | mute
    python lock_phone.py bright-up | bright-down
    python lock_phone.py bright-get           查看当前亮度（含窗口覆盖提示）
    python lock_phone.py bright 60%           亮度设为 60%（绝对设置，持久化）
    python lock_phone.py next | prev          下一个 / 上一个视频（上滑 / 下滑）
    python lock_phone.py play-pause
    python lock_phone.py home | back
    python lock_phone.py swipe 540 2200 540 700 160
    python lock_phone.py key 26              发送任意 keycode
    python lock_phone.py mode                 看当前模式（B站竖屏/抖音/B站横屏）
    python lock_phone.py mode douyin          切换模式（手势随模式变）
    python lock_phone.py speed-toggle         倍速开关（按住不放，再按一次关闭）
    python lock_phone.py long-press-once      长按一下（同点 swipe，App 认这个）
    python lock_phone.py unlock               一键解锁（唤醒 + 上滑 + 可选 PIN）
    python lock_phone.py install-apk xx.apk    一键安装 APK（也可把 apk 拖到 install_apk.bat）
    python lock_phone.py uninstall-apk 包名     卸载
    python lock_phone.py launch-app 包名        启动某个 App
    python lock_phone.py next-video           下一个视频（横屏自动改为点「下一个」按钮）
    python lock_phone.py status               查看连接 / 屏幕 / 亮度
    python lock_phone.py connect              引导式输入 IP:端口 连接手机（双击 connect.bat）
    python lock_phone.py connect IP:PORT      直接连这个地址并记住
    python lock_phone.py discover             列出 mDNS 发现到的连接/配对端口
    python lock_phone.py pair IP:PORT CODE    首次配对无线调试
"""

import json
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
LOG_PATH = os.path.join(HERE, "lock_phone.log")
NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _fix_console_encoding():
    """
    让控制台里的中文不再是乱码。

    两种情况都要治：
    1. 双击 .bat 启动：有控制台，但中文控制台默认 cp936，而环境里若设了
       PYTHONUTF8=1 / PYTHONIOENCODING=utf-8，Python 按 UTF-8 写、控制台按 GBK 解 → 乱码。
    2. 托盘用 CREATE_NEW_CONSOLE 拉起来（右键菜单「输入 IP:端口 连接手机」）：
       父进程是 pythonw.exe，没有有效的标准句柄，子进程拿到的是坏句柄甚至 None，
       于是既判断不出控制台、又按 UTF-8 往 GBK 控制台写 → 满屏方块。
       这种直接重新打开 CONOUT$ / CONIN$ 解决。

    被重定向到管道/文件时不干预。
    """
    if os.name != "nt":
        return
    try:
        import ctypes
        import msvcrt
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
        kernel32.GetConsoleMode.restype = ctypes.c_int
        kernel32.GetConsoleOutputCP.restype = ctypes.c_uint
        kernel32.GetConsoleCP.restype = ctypes.c_uint
        kernel32.GetConsoleWindow.restype = ctypes.c_void_p
    except Exception:
        return

    # 没有控制台（pythonw 常驻、或输出被重定向）就别动
    try:
        if not kernel32.GetConsoleWindow():
            return
    except Exception:
        return

    def is_console(stream):
        if stream is None:
            return False
        try:
            handle = msvcrt.get_osfhandle(stream.fileno())
            mode = ctypes.c_uint()
            return bool(kernel32.GetConsoleMode(ctypes.c_void_p(handle), ctypes.byref(mode)))
        except Exception:
            return False

    # 句柄不可用（GUI 父进程拉的子控制台）→ 重新打开控制台设备
    if not is_console(sys.stdout) or not is_console(sys.stderr):
        try:
            sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)
        except Exception:
            pass
        try:
            sys.stderr = open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)
        except Exception:
            pass
    if not is_console(sys.stdin):
        try:
            sys.stdin = open("CONIN$", "r", encoding="utf-8", errors="replace")
        except Exception:
            pass

    # 统一把控制台代码页调成 UTF-8（改不动也没关系，下面按实际 CP 对齐）
    try:
        if kernel32.GetConsoleOutputCP() != 65001:
            kernel32.SetConsoleOutputCP(65001)
        if kernel32.GetConsoleCP() != 65001:
            kernel32.SetConsoleCP(65001)
    except Exception:
        pass

    # 关键：字节流编码必须和控制台「当前」代码页一致，否则就是乱码
    for stream in (sys.stdout, sys.stderr, sys.stdin):
        if stream is None or not hasattr(stream, "reconfigure"):
            continue
        try:
            cp = int((kernel32.GetConsoleCP() if stream is sys.stdin
                      else kernel32.GetConsoleOutputCP())() or 0)
            if not cp:
                cp = 65001
            stream.reconfigure(encoding="utf-8" if cp == 65001 else "cp%d" % cp,
                               errors="replace")
        except Exception:
            continue


_fix_console_encoding()

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass


# ---------------------------------------------------------------- 配置

DEFAULT_ACTIONS = {
    "<ctrl>+<alt>+l": "lock",
    "<ctrl>+<alt>+<up>": "vol-up",
    "<ctrl>+<alt>+<down>": "vol-down",
    "<ctrl>+<alt>+m": "mute",
    "<ctrl>+<alt>+<right>": "bright-up",
    "<ctrl>+<alt>+<left>": "bright-down",
    "<ctrl>+<alt>+<page_down>": "next",
    "<ctrl>+<alt>+<page_up>": "prev",
    "<ctrl>+<alt>+<space>": "play-pause",
    "<ctrl>+<alt>+o": "overlay-toggle",
    "<ctrl>+<alt>+p": "panel",
}


# ---------------------------------------------------------------- 模式
#
# 不同 App / 不同方向的手势完全不一样，用「模式」把这些差异收敛成一张表：
# 面板上选一个模式 → 该模式覆盖下面这些动作的默认实现。
#
# 查到的实测规则（2026-10 核实）：
#   抖音：上滑=下一个、下滑=上一个、单击=暂停、双击=点赞、
#         长按任意处≈1s=倍速浮层(默认2x)、长按右侧1/5宽=按住期间2x；横屏里上滑照样换视频
#   B站：竖屏上滑=下一个；横屏里 左右滑=进度、左侧上下滑=亮度、右侧上下滑=音量、双击=暂停、
#         长按=倍速(~3x)；**横屏没有上下滑换视频的手势**，只能点控件里的「下一个」
MODES = {
    "bili-portrait": {
        "label": "哔哩哔哩 · 竖屏",
        "hint": "上滑 = 下一个视频    双击 = 暂停/播放    长按画面 = 倍速",
        "app_bright_x": 0.12,      # 左侧上下滑 = 亮度
        "app_vol_x": 0.88,         # 右侧上下滑 = 音量
        # 倍速：B站长按画面任意处 → 按住期间加速（约 3x），松手恢复
        "speed_point": (0.5, 0.5),
        "speed_note": "长按画面（中心）→ 按住期间约 3x",
        "actions": {
            "next": {"type": "swipe", "path": [0.5, 0.80, 0.5, 0.25]},
            "prev": {"type": "swipe", "path": [0.5, 0.25, 0.5, 0.80]},
            "play-pause": {"type": "double-tap"},
            "like": {"type": "tap-node", "texts": ["点赞", "赞"],
                     "show_controls": False,   # 竖屏右侧的赞按钮一直显示，不用先单击
                     "msg": "B站的点赞要点右侧的赞按钮（长按还可以三连）"},
            "long-press": {"type": "long-press", "fx": 0.5, "fy": 0.5},
            "long-press-once": {"type": "long-press", "fx": 0.5, "fy": 0.5},
        },
    },
    "douyin": {
        "label": "抖音 · 竖屏",
        "hint": "上滑 = 下一个视频    单击 = 暂停/播放    双击 = 点赞    长按画面 = 倍速",
        "app_bright_x": 0.12,
        "app_vol_x": 0.88,
        # 倍速：抖音长按右侧 1/5 宽区域 → 按住期间 2x（不弹浮层），最适合做开关
        "speed_point": (0.90, 0.5),
        "speed_note": "长按右侧 1/5 区域 → 按住期间 2x（不弹浮层）",
        "actions": {
            "next": {"type": "swipe", "path": [0.5, 0.80, 0.5, 0.25]},
            "prev": {"type": "swipe", "path": [0.5, 0.25, 0.5, 0.80]},
            "play-pause": {"type": "tap"},
            "like": {"type": "double-tap"},
            "long-press": {"type": "long-press", "fx": 0.90, "fy": 0.5},
            "long-press-once": {"type": "long-press", "fx": 0.90, "fy": 0.5},
        },
    },
    "bili-landscape": {
        "label": "哔哩哔哩 · 横屏",
        "hint": "左右滑 = 进度    左侧上下滑 = 亮度    右侧上下滑 = 音量    双击 = 暂停；"
                "换视频靠「下一个」按钮",
        "app_bright_x": 0.12,
        "app_vol_x": 0.88,
        # 倍速：横屏同样是长按画面加速，但要避开左右两侧的进度手势区，取正中偏内
        "speed_point": (0.5, 0.5),
        "speed_note": "长按画面正中 → 按住期间倍速（避开两侧进度手势区）",
        "actions": {
            # 横屏没有上下滑换视频的手势 —— 只能单击唤出控件、找「下一个」按钮点它
            "next": {"type": "tap-node",
                     "texts": ["下一个", "下一个视频", "下一集", "下一P", "下一话"]},
            "prev": {"type": "tap-node",
                     "texts": ["上一个", "上一个视频", "上一集", "上一P"]},
            "play-pause": {"type": "double-tap"},
            "like": {"type": "tap-node", "texts": ["点赞", "赞"],
                     "msg": "B站的点赞要点右侧的赞按钮（长按还可以三连）"},
            "seek-forward": {"type": "swipe", "path": [0.35, 0.5, 0.75, 0.5]},
            "seek-back": {"type": "swipe", "path": [0.75, 0.5, 0.35, 0.5]},
            "long-press": {"type": "long-press", "fx": 0.5, "fy": 0.5},
            "long-press-once": {"type": "long-press", "fx": 0.5, "fy": 0.5},
        },
    },
}


def default_config():
    return {
        "adb_path": "",
        "device": "",
        "actions": dict(DEFAULT_ACTIONS),
        "lock_keyevent": 223,
        "wake_keyevent": 224,
        "brightness_disable_auto": True,
        "brightness_key_repeat": 1,
        "warn_window_override": True,
        "app_bright_x": 0.12,
        "app_bright_low": 0.38,
        "app_bright_high": 0.62,
        "app_bright_steps": 1,
        "app_bright_require_override": True,
        "app_vol_x": 0.88,
        "mode": "bili-portrait",
        "speed_hold_max_ms": 120000,
        "unlock_pin": "",
        "swipe_duration": 160,
        "long_press_ms": 600,
        "long_press_hold_ms": 1200,
        "long_press_max_ms": 4000,
        "left_click": "both",
        "panel_port": 59731,
        "panel_poll_seconds": 8,
        "overlay_alpha": 0.45,
        "overlay_margin": 0.01,
        "overlay_pointer": True,
        "overlay_color": "black",
        "overlay_fps": 30,
        "map_requires_right": True,
        "arm_click_ms": 250,
        "command_timeout": 12,
        "use_channel": True,
        "channel_timeout": 8,
        "channel_retry_gap": 30,
        "display_size_ttl": 120,
        "brightness_state_ttl": 2,
        "auto_discover": True,
        "allow_kill_server": True,
        "retry_cooldown": 4.0,
        "ensure_ttl": 5,
        "apk_grant_permissions": True,
        "apk_push_fallback": True,
        "apk_install_timeout": 300,
        "last_apk": "",
        "last_apk_dir": "",
        "notify": True,
    }


def load_config():
    cfg = default_config()
    if os.path.isfile(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                user = json.load(fh) or {}
        except Exception as exc:
            log("读取配置失败: %s" % exc)
            user = {}

        legacy = user.pop("hotkeys", None)
        cfg.update(user)

        if not cfg.get("actions"):
            cfg["actions"] = {hk: "lock" for hk in (legacy or [])} or dict(DEFAULT_ACTIONS)
        elif legacy:
            for hk in legacy:
                cfg["actions"].setdefault(hk, "lock")
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=2)
    except Exception as exc:
        log("写入配置失败: %s" % exc)


def apply_mode(cfg, mode):
    """切换使用模式，并把该模式的手势参数（亮度/音量竖线位置）一起落到配置里。"""
    mode = (mode or "").strip()
    if mode not in MODES:
        return False, "未知模式：%s（可选：%s）" % (mode, " / ".join(MODES))
    cfg["mode"] = mode
    info = MODES[mode]
    if info.get("app_bright_x") is not None:
        cfg["app_bright_x"] = info["app_bright_x"]
    if info.get("app_vol_x") is not None:
        cfg["app_vol_x"] = info["app_vol_x"]
    save_config(cfg)
    return True, "已切到「%s」：%s" % (info.get("label", mode), info.get("hint", ""))


def log(msg):
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        if os.path.isfile(LOG_PATH) and os.path.getsize(LOG_PATH) > 512 * 1024:
            with open(LOG_PATH, "w", encoding="utf-8") as fh:
                fh.write("")
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


_mutex_handle = []


def single_instance(name="phone_remote_lock_tray"):
    """
    Windows 命名互斥体做单实例锁。

    返回 True 表示可以继续（本进程是唯一实例）；
    False 表示已经有一个在跑了。
    非 Windows 直接放行。
    """
    if os.name != "nt":
        return True
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE

        ctypes.set_last_error(0)
        handle = kernel32.CreateMutexW(None, False, name)
        if not handle:
            return True

        ERROR_ALREADY_EXISTS = 183
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            return False

        _mutex_handle.append(handle)  # 保持引用，进程存活期间不释放
        return True
    except Exception:
        return True


def tray_running(name="phone_remote_lock_tray"):
    """托盘程序是否已经在跑（用于提示「快捷键要不要先启动托盘」）。"""
    if os.name != "nt":
        return True
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.OpenMutexW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        SYNCHRONIZE = 0x00100000
        handle = kernel32.OpenMutexW(SYNCHRONIZE, False, name)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return False
    except Exception:
        return True


# ---------------------------------------------------------------- adb

ADB_CANDIDATES = [
    r"C:\platform-tools\adb.exe",
    r"C:\Program Files\platform-tools\adb.exe",
    r"D:\adb\platform-tools\adb.exe",
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Android", "Sdk", "platform-tools", "adb.exe"),
    os.path.join(os.environ.get("USERPROFILE", ""), "scoop", "apps", "adb", "current", "adb.exe"),
    os.path.join(os.environ.get("USERPROFILE", ""), "AppData", "Local", "Microsoft", "WinGet", "Links", "adb.exe"),
]


def resolve_adb(cfg):
    configured = (cfg.get("adb_path") or "").strip().strip('"')
    if configured and os.path.isfile(configured):
        return configured
    found = shutil.which("adb") or shutil.which("adb.exe")
    if found:
        return found
    for cand in ADB_CANDIDATES:
        if cand and os.path.isfile(cand):
            return cand
    return None


# ---------------------------------------------------------------- 常驻 adb 通道

CHANNEL_MARK = "__ADBCHAN__"


def _quote(arg):
    """把参数安全地拼进设备端 shell 的一行命令里。"""
    text = str(arg)
    if text and all(c.isalnum() or c in "-_./:=@,+%" for c in text):
        return text
    return "'" + text.replace("'", "'\\''") + "'"


class AdbChannel(object):
    """
    常驻 `adb shell` 长连接：命令写进 stdin，输出按哨兵行分帧。

    为什么值得做：实测每次 `adb.exe` 调用光「进程启动 + 与 adb server 握手」就要 ~36ms，
    而设备真正执行往往只有 10-60ms —— 固定开销占了大头。
    常驻一条 shell 之后，每条命令的额外成本只剩本机管道 + 设备执行（约 1-3ms）。

    只用来跑**快命令**（settings / cmd / dumpsys / cat / getprop / keyevent / tap /
    motionevent / text）；`input swipe`、长按这类会阻塞几百毫秒的命令仍走一次性进程，
    免得把通道堵住（那是一条串行管道）。

    任何异常（超时、连接断、进程退出）都会把通道标记为 broken 并返回 None，
    调用方回退到一次性进程 —— 也就是「最坏情况退化成现在的行为」。
    """

    def __init__(self, argv, timeout=8.0):
        self.argv = list(argv)
        self.timeout = timeout
        self.broken = False
        self.reason = ""
        self.started = 0
        self.commands = 0
        self._proc = None
        self._queue = None
        self._lock = threading.Lock()
        self._seq = 0
        self._start()

    # -- 生命周期 --------------------------------------------------

    def _start(self):
        try:
            self._proc = subprocess.Popen(
                self.argv,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, creationflags=NO_WINDOW, bufsize=0)
        except Exception as exc:
            self.broken = True
            self.reason = "启动失败: %s" % exc
            log("[channel] %s" % self.reason)
            return
        self._queue = queue.Queue()
        self.started = time.time()
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        """后台读线程：一行一行塞进队列，EOF 时塞 None 作为断开信号。"""
        try:
            stream = self._proc.stdout
            while True:
                raw = stream.readline()
                if not raw:
                    break
                self._queue.put(raw.decode("utf-8", "replace").rstrip("\r\n"))
        except Exception:
            pass
        try:
            self._queue.put(None)
        except Exception:
            pass

    def _kill(self, reason):
        self.broken = True
        self.reason = reason
        log("[channel] 关闭：%s" % reason)
        try:
            if self._proc and self._proc.poll() is None:
                self._proc.kill()
        except Exception:
            pass

    def alive(self):
        return (not self.broken) and self._proc is not None and self._proc.poll() is None

    def close(self):
        if self._proc is not None:
            self._kill("主动关闭")

    # -- 执行 ------------------------------------------------------

    def run(self, command, timeout=None, script=False):
        """
        跑一条命令。command 为 list（自动加哨兵）或 script=True 时的整段 shell 文本。

        返回 (code, output)；通道不可用时返回 None（调用方回退）。
        """
        if not self.alive():
            return None
        timeout = float(timeout or self.timeout)
        if script:
            text = str(command)
        else:
            text = " ".join(_quote(a) for a in command)
        with self._lock:
            if not self.alive():
                return None
            self._seq += 1
            token = "%s%d__" % (CHANNEL_MARK, self._seq)
            line = "%s; echo %s$?\n" % (text, token)
            try:
                self._proc.stdin.write(line.encode("utf-8"))
                self._proc.stdin.flush()
            except Exception as exc:
                self._kill("写入失败: %s" % exc)
                return None

            out = []
            deadline = time.time() + timeout
            while True:
                remain = deadline - time.time()
                if remain <= 0:
                    self._kill("命令超时（%ss）" % timeout)
                    return None
                try:
                    item = self._queue.get(timeout=remain)
                except queue.Empty:
                    self._kill("命令超时（%ss）" % timeout)
                    return None
                if item is None:
                    self._kill("设备连接已断开")
                    return None
                match = re.match(r"^%s(\d+)__(\d+)$" % CHANNEL_MARK, item)
                if match:
                    self.commands += 1
                    return int(match.group(2)), "\n".join(out)
                out.append(item)

    def stats(self):
        return "命令 %d 条 / 存活 %.0fs" % (self.commands, time.time() - self.started)


class Adb(object):
    def __init__(self, cfg):
        self.cfg = cfg
        self.adb = resolve_adb(cfg)
        self.target = (cfg.get("device") or "").strip()
        self._last_fail = 0.0
        self._last_fail_msg = ""
        self._chan = None
        self._chan_fail_at = 0.0
        self._size_cache = None          # ((w, h), 取到的时间)
        self._bright_cache = None        # ((设置值, 覆盖包名, 生效值), 取到的时间)
        self.chan_hits = 0
        self.chan_misses = 0
        self._last_ok = 0.0             # 最近一次确认在线的时间（ensure 快路径用）

    # -- 底层 ------------------------------------------------------

    def _exec(self, args, timeout=None):
        if not self.adb:
            return -1, "未找到 adb，请安装 platform-tools 或填写 config.json 的 adb_path"
        try:
            proc = subprocess.run(
                [self.adb] + list(args),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout or self.cfg.get("command_timeout", 12),
                creationflags=NO_WINDOW,
            )
            out = proc.stdout.decode("utf-8", "replace") if proc.stdout else ""
            return proc.returncode, out
        except subprocess.TimeoutExpired:
            return -1, "adb 命令超时"
        except Exception as exc:
            return -1, "adb 执行失败: %s" % exc

    # -- 常驻通道 --------------------------------------------------

    def _channel_allowed(self, command):
        """哪些命令可以走常驻通道：必须是"快命令"，不能阻塞几百毫秒。"""
        if not self.cfg.get("use_channel", True) or not command:
            return False
        head = command[0]
        if head in ("settings", "cmd", "dumpsys", "cat", "getprop", "wm", "ls",
                    "echo", "pm", "sh", "which"):
            return True
        if head == "input":
            sub = command[1] if len(command) > 1 else ""
            return sub in ("keyevent", "tap", "motionevent", "text")
        return False

    def _get_channel(self, force=False):
        """拿常驻通道；不可用返回 None（调用方回退）。失败后 30 秒内不再重试。"""
        if not self.cfg.get("use_channel", True):
            return None
        if not self.adb or not self.target:
            return None
        now = time.time()
        if self._chan is not None and self._chan.alive():
            return self._chan
        if not force and now - self._chan_fail_at < float(self.cfg.get("channel_retry_gap", 30)):
            return None
        if self._chan is not None:
            self._chan.close()
            self._chan = None
        self._chan = AdbChannel([self.adb, "-s", self.target, "shell"],
                                timeout=self.cfg.get("channel_timeout", 8))
        if not self._chan.alive():
            self._chan_fail_at = now
            self._chan = None
            return None
        log("[channel] 已建立常驻通道（%s）" % self.target)
        return self._chan

    def close_channel(self, reason=""):
        """设备重连、切模式、退出时调用：确保下次重新建连。"""
        if self._chan is not None:
            self._chan.close()
            self._chan = None
        self._chan_fail_at = 0.0
        if reason:
            log("[channel] 已重置：%s" % reason)

    def channel_stats(self):
        if self._chan is None:
            return "常驻通道未启用（命中 %d / 回退 %d）" % (self.chan_hits, self.chan_misses)
        return "常驻通道 %s（命中 %d / 回退 %d）" % (
            self._chan.stats(), self.chan_hits, self.chan_misses)

    def _shell(self, command, timeout=None, script=False):
        """
        跑设备端命令。

        快命令优先走常驻通道（省掉每次 ~36ms 的进程启动），
        其余（swipe / 长命令 / 通道不可用）走一次性进程 —— 行为完全等价。
        """
        if script or self._channel_allowed(command):
            chan = self._get_channel()
            if chan is not None:
                result = chan.run(command, timeout=timeout, script=script)
                if result is not None:
                    self.chan_hits += 1
                    return result
                self.chan_misses += 1          # 通道坏了，下面回退
                self._chan_fail_at = time.time()
        args = ["-s", self.target, "shell"]
        if script:
            args += ["sh", "-c", str(command)]
        else:
            args += list(command)
        return self._exec(args, timeout=timeout)

    # -- 连接 ------------------------------------------------------

    def _devices(self):
        """返回 {serial: state}，state 可能是 device / offline / unauthorized。"""
        code, out = self._exec(["devices"], timeout=10)
        result = {}
        if code != 0:
            return result
        for line in out.splitlines()[1:]:
            line = line.strip()
            if not line or "\t" not in line:
                continue
            serial, state = line.split("\t", 1)
            result[serial.strip()] = state.strip()
        return result

    def _online(self):
        return [serial for serial, state in self._devices().items() if state == "device"]

    def _connect(self, addr):
        self._exec(["connect", addr], timeout=15)

    def _wait_online(self, addr, tries=4, gap=0.8):
        for _ in range(tries):
            if addr in self._online():
                return True
            time.sleep(gap)
        return False

    def mdns_services(self):
        """列出 mDNS 发现的无线调试服务，返回 [(name, service, addr)]。"""
        code, out = self._exec(["mdns", "services"], timeout=10)
        entries = []
        if code != 0:
            return entries
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[1].startswith("_adb-tls-"):
                entries.append((parts[0], parts[1], parts[-1]))
        return entries

    def discover(self):
        """只要可用于 adb connect 的地址。"""
        return [addr for _name, service, addr in self.mdns_services()
                if service == "_adb-tls-connect._tcp"]

    def ensure(self, force=False):
        """
        确保有一条可用连接。

        手机离线时一次完整性重连要跑好几轮，所以失败后短时间内直接复用上次的错误，
        避免每个按钮都卡十几秒。显式点「重新连接」时传 force=True 绕过冷却。

        在线时也有两级快路径，避免**每个动作**都新起一个 adb 进程去查 `adb devices`：
          1. 刚成功过（ensure_ttl 秒内）→ 直接返回；
          2. 常驻通道还活着 → 用一条 echo 探活（1-3ms，不起新进程）。
        """
        now = time.time()
        cooldown = float(self.cfg.get("retry_cooldown", 4.0) or 0)
        if not force and self._last_fail and (now - self._last_fail) < cooldown:
            return False, self._last_fail_msg

        ttl = float(self.cfg.get("ensure_ttl", 5) or 0)
        if not force and self._last_ok and (now - self._last_ok) < ttl:
            return True, self.target
        if not force and self._ensure_fast():
            self._last_ok = now
            self._last_fail = 0.0
            return True, self.target

        ok, message = self._ensure_impl()
        if ok:
            self._last_fail = 0.0
            self._last_fail_msg = ""
            self._last_ok = time.time()
        else:
            self._last_ok = 0.0
            self._last_fail = time.time()
            self._last_fail_msg = message
        return ok, message

    def _ensure_fast(self):
        """常驻通道活着就算在线（一条 echo 探活），失败则作废通道返回 False。"""
        chan = self._get_channel()
        if chan is None:
            return False
        result = chan.run(["echo", "ok"], timeout=min(3.0, float(
            self.cfg.get("channel_timeout", 8) or 8)))
        if result is None:
            self._chan_fail_at = time.time()
            return False
        code, out = result
        return code == 0 and "ok" in (out or "")

    def _ensure_impl(self):
        if not self.adb:
            return False, "未找到 adb，请先安装 Android Platform Tools"

        saw_offline = False
        tried_kill = False

        for attempt in range(3):
            states = self._devices()

            if self.target and states.get(self.target) == "device":
                return True, self.target

            if self.target and states.get(self.target) in ("offline", "unauthorized"):
                # 陈旧连接：adb connect 只会回 "already connected"，必须先断开
                saw_offline = True
                log("检测到 %s 状态为 %s，先断开再重连" % (self.target, states.get(self.target)))
                self._exec(["disconnect", self.target], timeout=10)
                time.sleep(0.5)

            if self.target:
                self._connect(self.target)
                if self._wait_online(self.target):
                    return True, self.target

            if self.cfg.get("auto_discover", True):
                for cand in self.discover():
                    if cand == self.target:
                        continue
                    self._connect(cand)
                    if self._wait_online(cand):
                        log("自动发现并连接 %s" % cand)
                        self._remember(cand)
                        return True, cand

            online = self._online()
            if len(online) == 1:
                self._remember(online[0])
                return True, online[0]

            if saw_offline and not tried_kill and self.cfg.get("allow_kill_server", True):
                # 最后兜底：本地 adb server 的 TLS 状态也可能僵死
                tried_kill = True
                log("重试无效，重启 adb server 后再试一次")
                self._exec(["kill-server"], timeout=10)
                time.sleep(0.8)

        if saw_offline:
            return False, ("手机无线调试连接已失效（offline）。"
                           "请在手机上：开发者选项 → 无线调试 → 关闭再打开"
                           "（端口会变，本工具会自动重新发现）；若仍不行就重新配对一次")
        return False, "连接失败：请确认手机「无线调试」已开启，且与本机在同一 Wi-Fi"

    def _remember(self, addr):
        self.target = addr
        self.cfg["device"] = addr
        save_config(self.cfg)

    def connect_addr(self, addr):
        """连接指定的 IP:端口（手动输入用），成功后写入 config.json。"""
        addr = (addr or "").strip()
        if not addr:
            return False, "地址为空"
        if ":" not in addr:
            return False, "地址格式应为 IP:端口，例如 192.168.1.23:42007"

        self.target = addr
        prev_target = self.cfg.get("device") or ""
        self.cfg["device"] = addr

        # 陈旧连接要先断开，否则 adb connect 只会回 "already connected"
        self._exec(["disconnect", addr], timeout=10)
        self._connect(addr)
        if self._wait_online(addr, tries=6, gap=0.7):
            self._remember(addr)
            self._last_fail = 0.0
            self._last_fail_msg = ""
            return True, "已连接并记住 %s" % addr

        state = self._devices().get(addr)

        # 端口不通时兜底试一次 mDNS 自动发现（无线调试重开后端口会变）
        if self.cfg.get("auto_discover", True):
            for cand in self.discover():
                if cand == addr:
                    continue
                self._connect(cand)
                if self._wait_online(cand):
                    self._remember(cand)
                    return True, ("你给的 %s 连不上，但自动发现了 %s，已连接并记住"
                                  % (addr, cand))

        # 没连上就别把错误地址留在配置里，否则之后每次都先浪费一轮重试
        self.cfg["device"] = prev_target
        self.target = prev_target

        if state == "unauthorized":
            return False, "设备未授权：请解锁手机，在「允许 USB 调试吗？」弹窗里点允许"
        if state == "offline":
            return False, ("连上但状态是 offline：请把手机「无线调试」关掉再打开"
                           "（端口会变），或重新配对一次")
        return False, ("连不上 %s（端口无响应）。请确认：\n"
                       "         1) 端口是「无线调试」页面显示的 IP:端口\n"
                       "         2) 不是「使用配对码配对设备」弹窗里的那个端口\n"
                       "         3) 手机与本机在同一 Wi-Fi，且无线调试处于开启状态" % addr)

    # -- 通用动作 --------------------------------------------------

    def ready(self, force=False):
        ok, info = self.ensure(force=force)
        return (True, self.target) if ok else (False, info)

    def send_keyevent(self, key):
        ok, info = self.ready()
        if not ok:
            return False, info
        code, out = self._shell(["input", "keyevent", str(int(key))])
        if code != 0:
            return False, "adb 返回错误: %s" % (out.strip() or "unknown")
        return True, "keyevent %s" % key

    def tap(self, x, y):
        ok, info = self.ready()
        if not ok:
            return False, info
        code, out = self._shell(["input", "tap", str(int(x)), str(int(y))])
        if code != 0:
            return False, "点击失败: %s" % (out.strip() or "unknown")
        return True, "已点击 %s,%s" % (x, y)

    def swipe(self, x1, y1, x2, y2, duration=160):
        ok, info = self.ready()
        if not ok:
            return False, info
        code, out = self._shell(["input", "swipe", str(int(x1)), str(int(y1)),
                                 str(int(x2)), str(int(y2)), str(int(duration))])
        if code != 0:
            return False, "滑动失败: %s" % (out.strip() or "unknown")
        return True, "已滑动 %s,%s -> %s,%s" % (x1, y1, x2, y2)

    def screen_size(self):
        """物理（自然）尺寸，不随旋转变化。"""
        code, out = self._shell(["wm", "size"], timeout=10)
        if code != 0:
            return None, None
        sizes = re.findall(r"(\d+)\s*x\s*(\d+)", out or "")
        if not sizes:
            return None, None
        w, h = sizes[-1]
        return int(w), int(h)

    def display_size(self, force=False):
        """
        当前逻辑显示尺寸（随旋转变化）。

        这就是 input tap / input swipe 真正使用的坐标系：
        横屏时是 3200x1440，竖屏时是 1440x3200。

        带缓存：每次 swipe/tap 前都问一次的话，每条动作白白多一次
        `dumpsys window displays`（一次 adb 调用 ≈36ms）。
        旋转后要么按 R（映射层）刷新，要么等 ttl 到期，要么连接时自动失效。
        """
        ttl = float(self.cfg.get("display_size_ttl", 180) or 0)
        if not force and self._size_cache:
            size, at = self._size_cache
            if size and (ttl <= 0 or time.time() - at < ttl):
                return size
        code, out = self._shell(["dumpsys", "window", "displays"], timeout=15)
        if code == 0 and out:
            found = re.search(r"\bcur=(\d+)x(\d+)", out)
            if found:
                size = (int(found.group(1)), int(found.group(2)))
                self._size_cache = (size, time.time())
                return size
        size = self.screen_size()
        self._size_cache = ((size or (None, None)), time.time())
        return size

    def invalidate_size(self):
        self._size_cache = None

    def swipe_ratio(self, fx1, fy1, fx2, fy2, duration=160):
        ok, info = self.ready()
        if not ok:
            return False, info
        w, h = self.display_size()
        if not w or not h:
            w, h = 1440, 3200
        return self.swipe(int(w * fx1), int(h * fy1), int(w * fx2), int(h * fy2), duration)

    # -- 长按（倍速播放）-------------------------------------------

    def supports_motionevent(self):
        """
        Android 11+ 的 input 支持 motionevent，可以真「按下 / 抬起」分开发。

        这是长按能做「按住倍速、松开恢复」的前提；不支持就只能用同点 swipe 模拟。
        结果缓存在实例上，避免每次长按都多跑一次 adb。
        """
        cached = getattr(self, "_motion_ok", None)
        if cached is not None:
            return cached
        code, out = self._shell(["input"], timeout=10)
        self._motion_ok = bool(code == 0 and "motionevent" in (out or ""))
        return self._motion_ok

    def press_down(self, x, y):
        """按下不抬（配合 press_up 做长按/倍速）。"""
        code, out = self._shell(
            ["input", "motionevent", "DOWN", str(int(x)), str(int(y))], timeout=8)
        if code != 0:
            return False, (out or "").strip() or "按下失败"
        self._press_xy = (int(x), int(y))
        return True, "已按下 (%s,%s)" % (int(x), int(y))

    def press_up(self, x=None, y=None):
        """抬起。不传坐标就沿用上一次按下的位置。"""
        if x is None or y is None:
            pos = getattr(self, "_press_xy", None)
            if not pos:
                return False, "当前没有正在按下的手指"
            x, y = pos
        code, out = self._shell(
            ["input", "motionevent", "UP", str(int(x)), str(int(y))], timeout=8)
        self._press_xy = None
        if code != 0:
            return False, (out or "").strip() or "抬起失败"
        return True, "已抬起 (%s,%s)" % (int(x), int(y))

    def long_press(self, x, y, ms=800):
        """在 (x,y) 长按 ms 毫秒（抖音/B站的倍速就是这么触发的）。"""
        ok, info = self.ready()
        if not ok:
            return False, info
        ms = max(100, int(ms))
        if self.supports_motionevent():
            ok, info = self.press_down(x, y)
            if not ok:
                return False, info
            time.sleep(ms / 1000.0)
            return self.press_up(x, y)
        # 退化方案：起点终点相同的 swipe 等效于长按
        ok, info = self.swipe(x, y, x, y, duration=ms)
        if ok:
            return True, "已长按 (%s,%s) %sms" % (int(x), int(y), ms)
        return ok, info

    def long_press_center(self, ms=800):
        """长按屏幕正中（短视频的倍速播放就靠这个）。"""
        w, h = self.display_size()
        if not w or not h:
            return False, "读不到分辨率，无法定位屏幕中心"
        return self.long_press(w // 2, h // 2, ms)

    def long_press_ratio(self, fx, fy, ms=1200):
        """按比例长按。用「同点 swipe」实现——实测这个才是 App 认的长按。"""
        w, h = self.display_size()
        if not w or not h:
            return False, "读不到分辨率"
        x, y = int(w * fx), int(h * fy)
        ok, info = self.swipe(x, y, x, y, duration=max(100, int(ms)))
        if ok:
            return True, "已长按 (%.2f,%.2f) %dms" % (fx, fy, ms)
        return ok, info

    # -- 按住不放（倍速开关用）-------------------------------------

    def hold_start(self, fx=0.5, fy=0.5):
        """
        开始「按住不放」：DOWN 之后每隔一会儿补一个 MOVE 心跳。

        为什么要心跳：只有单个 DOWN 时 App 的长按识别经常不触发
        （两次独立 input 命令的 downTime 对不上），补 MOVE 让它看起来像真手指。
        必须配 hold_stop() 收尾，否则手机上会残留一个按下的手指。
        """
        if getattr(self, "_holding", False):
            return True, "已经在按住状态"
        ok, info = self.ready()
        if not ok:
            return False, info
        w, h = self.display_size()
        if not w or not h:
            return False, "读不到分辨率"
        x, y = int(w * fx), int(h * fy)
        ok, info = self.press_down(x, y)
        if not ok:
            return False, info
        self._holding = True
        self._hold_xy = (x, y)
        self._hold_stop_flag = threading.Event()
        self._hold_thread = threading.Thread(
            target=self._hold_heartbeat, args=(x, y, self._hold_stop_flag), daemon=True)
        self._hold_thread.start()
        # 兜底：一直按住不放太久会自动松开，避免手机上残留按下的手指
        limit = int(self.cfg.get("speed_hold_max_ms", 120000) or 0)
        if limit > 0:
            threading.Thread(target=self._hold_watchdog,
                             args=(self._hold_stop_flag, limit), daemon=True).start()
        return True, "已开始按住 (%.2f,%.2f)（倍速中）" % (fx, fy)

    def _hold_watchdog(self, stop_flag, limit_ms):
        if not stop_flag.wait(limit_ms / 1000.0) and self.is_holding():
            log("[adb] 按住超过 %sms，自动松开" % limit_ms)
            self.hold_stop()

    def _hold_heartbeat(self, x, y, stop_flag):
        """按住期间反复补 MOVE：偏移 1px 来回抖，看起来像手指微动。"""
        offset = 0
        while not stop_flag.wait(0.5):
            offset = 1 - offset
            self._shell(["input", "motionevent", "MOVE",
                         str(x + offset), str(y + offset)], timeout=6)

    def hold_stop(self):
        """结束「按住不放」。没在按住时返回 False，避免发出多余的 UP。"""
        if not getattr(self, "_holding", False):
            return False, "当前没有处于按住状态"
        flag = getattr(self, "_hold_stop_flag", None)
        if flag is not None:
            flag.set()
        self._holding = False
        x, y = getattr(self, "_hold_xy", (None, None))
        return self.press_up(x, y)

    def is_holding(self):
        return bool(getattr(self, "_holding", False))

    # -- 屏幕坐标小工具 --------------------------------------------

    def tap_ratio(self, fx, fy):
        w, h = self.display_size()
        if not w or not h:
            return False, "读不到分辨率"
        return self.tap(int(w * fx), int(h * fy))

    def double_tap_ratio(self, fx=0.5, fy=0.5, gap=0.09):
        """双击（抖音点赞、B站播放/暂停都是双击）。两条 tap 合成一次调用。"""
        w, h = self.display_size()
        if not w or not h:
            return False, "读不到分辨率"
        x, y = int(w * fx), int(h * fy)
        script = "input tap %d %d; sleep %.2f; input tap %d %d" % (x, y, gap, x, y)
        code, out = self._shell(script, script=True)
        if code != 0:
            return False, (out or "").strip() or "双击失败"
        return True, "已双击 (%.2f,%.2f)" % (fx, fy)

    # -- 找屏幕上的按钮（横屏换视频等）-----------------------------

    def dump_ui(self):
        """把当前界面的 UI 树 dump 下来（uiautomator），返回 XML 文本。

        dump + cat 合成一条命令，省一次 adb 往返（uiautomator 的提示输出丢掉）。
        """
        script = ("uiautomator dump /sdcard/window_dump.xml >/dev/null 2>&1; "
                  "cat /sdcard/window_dump.xml")
        code, out = self._shell(script, timeout=25, script=True)
        return out if code == 0 else ""

    def find_node(self, texts, descs=(), ids=()):
        """在 UI 树里按 text / content-desc / resource-id 找节点，返回 (x, y, 匹配到的词)。"""
        xml = self.dump_ui()
        if not xml or "<hierarchy" not in xml:
            return None
        wanted = [t for t in list(texts) + list(descs) if t]
        for node in re.findall(r"<node[^>]*>", xml):
            text = re.search(r'\btext="([^"]*)"', node)
            desc = re.search(r'content-desc="([^"]*)"', node)
            rid = re.search(r'resource-id="([^"]*)"', node)
            bounds = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', node)
            if not bounds:
                continue
            values = [g for g in (text.group(1) if text else "",
                                  desc.group(1) if desc else "") if g]
            for want in wanted:
                if any(want == v or want in v for v in values):
                    return self._node_center(bounds, want)
            if ids:
                rid_value = (rid.group(1) if rid else "").rsplit("/", 1)[-1].lower()
                for want in ids:
                    if want and want.lower() in rid_value:
                        return self._node_center(bounds, "id:" + want)
        return None

    @staticmethod
    def _node_center(bounds, matched):
        x1, y1, x2, y2 = (int(bounds.group(i)) for i in range(1, 5))
        if x2 <= x1 or y2 <= y1:
            return None
        return ((x1 + x2) // 2, (y1 + y2) // 2, matched)

    def tap_node(self, texts, descs=(), ids=(), show_controls=True, attempts=2):
        """
        点 UI 上某个按钮（例如 B站横屏播放器里的「下一个」）。

        控件栏会自动隐藏，所以先单击画面唤出来再 dump；一次找不到就再试一次
        （第一次单击可能只是把控件显示出来、第二次才是稳定状态）。
        """
        ok, info = self.ready()
        if not ok:
            return False, info
        ids = list(ids) or ["next", "next_video", "next_episode"]
        for attempt in range(max(1, int(attempts))):
            if show_controls:
                w, h = self.display_size()
                if w and h:
                    self.tap(w // 2, h // 2)          # 单击唤出播放控件
                    time.sleep(0.8 if attempt == 0 else 0.5)
            found = self.find_node(texts, descs, ids)
            if found:
                x, y, matched = found
                ok, info = self.tap(x, y)
                if ok:
                    return True, "已点击「%s」(%d,%d)" % (matched, x, y)
                return ok, info
        return False, "界面上没找到这些按钮：%s" % "/".join(list(texts))

    # -- 安装 / 卸载 APK -------------------------------------------

    def list_third_party(self):
        """第三方包名集合（用来对比安装前后，识别刚装的是哪个包）。"""
        code, out = self._shell(["pm", "list", "packages", "-3"], timeout=20)
        if code != 0:
            return set()
        return {line.strip().split(":")[-1] for line in (out or "").splitlines()
                if line.strip().startswith("package:")}

    @staticmethod
    def _install_flags(replace=True, grant=True, downgrade=True, test_only=True):
        flags = []
        if replace:
            flags.append("-r")
        if grant:
            flags.append("-g")          # 装完直接把运行时权限授全，省得手机上点
        if test_only:
            flags.append("-t")          # 允许 test-only 包
        if downgrade:
            flags.append("-d")
        return flags

    @staticmethod
    def _install_hint(output):
        """把 adb install 的错误码翻译成人话 + 该怎么办。"""
        text = output or ""
        table = [
            ("INSTALL_FAILED_USER_RESTRICTED",
             "手机限制了 USB 安装。小米/HyperOS：开发者选项 → 打开「USB 安装」"
             "（部分机型要登录小米账号 + 插 SIM 卡才给开）"),
            ("INSTALL_FAILED_UPDATE_INCOMPATIBLE",
             "签名不一致：手机上装过同包名但签名不同的版本，先卸载它再装"
             "（用「卸载 APK」按钮或 adb uninstall <包名>）"),
            ("INSTALL_FAILED_VERSION_DOWNGRADE", "版本比手机上现有的低，已尝试 -d 降级安装"),
            ("INSTALL_FAILED_ALREADY_EXISTS", "同名包已存在，已尝试 -r 覆盖安装"),
            ("INSTALL_FAILED_INSUFFICIENT_STORAGE", "手机存储空间不足"),
            ("INSTALL_FAILED_VERIFICATION_FAILURE",
             "被系统的安装校验拦了（MIUI 安全中心/应用校验），手机上允许一次即可"),
            ("INSTALL_PARSE_FAILED_NO_CERTIFICATES", "APK 没有签名或签名损坏"),
            ("INSTALL_PARSE_FAILED", "APK 解析失败：文件损坏或与设备不兼容"),
            ("INSTALL_FAILED_NO_MATCHING_ABIS", "APK 不含这台机器的 CPU 架构（如只要 x86）"),
            ("INSTALL_FAILED_OLDER_SDK", "APK 要求的最低 Android 版本比手机高"),
            ("INSTALL_FAILED_INVALID_APK", "APK 无效（下载不完整？）"),
            ("failed to read copy response", "传输中断（无线 adb 偶尔会），可以重试或走 push 方式"),
            ("device offline", "连接掉了，先重新连接手机"),
        ]
        for key, tip in table:
            if key in text:
                return tip
        return ""

    @staticmethod
    def _transport_error(output):
        """是不是"传输断了"这一类错误（只有这类才值得换 push 方式重试）。

        像 INSTALL_FAILED_USER_RESTRICTED / UPDATE_INCOMPATIBLE 这种，
        换 push 还是会失败，白等一轮还会误导人。
        """
        text = (output or "").lower()
        for key in ("failed to read copy response", "failed to write", "error: closed",
                    "broken pipe", "connection reset", "protocol fault",
                    "couldn't read status", "no error"):
            if key in text:
                return True
        return False

    def install_apk(self, path, replace=True, grant=None, push_fallback=None):
        """
        一键装 APK：`adb install -r -g -t -d`；失败时自动换 push + `pm install` 再试一次。

        装完用「第三方包名集合 diff」确认新增的是哪个包，返回给界面显示。
        """
        ok, info = self.ready()
        if not ok:
            return False, info
        path = os.path.abspath(os.path.expanduser(str(path or "").strip('"')))
        if not os.path.isfile(path):
            return False, "找不到文件：%s" % path
        if not path.lower().endswith(".apk"):
            return False, "只支持 .apk 文件（拆分包请把多个 apk 放进同一个文件夹再选文件夹）"
        if grant is None:
            grant = bool(self.cfg.get("apk_grant_permissions", True))
        if push_fallback is None:
            push_fallback = bool(self.cfg.get("apk_push_fallback", True))
        timeout = int(self.cfg.get("apk_install_timeout", 300) or 300)
        size_mb = os.path.getsize(path) / 1048576.0

        before = self.list_third_party()
        flags = self._install_flags(replace=replace, grant=grant)
        code, out = self._exec(["install"] + flags + [path], timeout=timeout)
        text = (out or "").strip()
        success = (code == 0 and "Success" in text)

        if not success and push_fallback and self._transport_error(text):
            log("[apk] 传输类失败，改走 push + pm install：%s" % text[:120])
            pushed = self._install_via_push(path, grant, timeout)
            if pushed[0]:
                success, text = True, pushed[1]
            else:
                text += "\n（push 方式也失败：%s）" % pushed[1]
        elif not success and push_fallback:
            log("[apk] 非传输类失败，不重试：%s" % text[:120])

        if not success:
            hint = self._install_hint(text)
            msg = "安装失败：%s" % (text[-400:] or "adb 没有返回原因")
            if hint:
                msg += "\n提示：%s" % hint
            return False, msg

        after = self.list_third_party()
        new_pkgs = sorted(after - before)
        self.close_channel("装完 APK 后刷新连接")
        if new_pkgs:
            return True, "已安装 %.1fMB：%s" % (size_mb, "、".join(new_pkgs))
        return True, "已安装 %.1fMB（adb 返回 Success；包名没变化说明是覆盖升级）" % size_mb

    def _install_via_push(self, path, grant, timeout):
        """先 push 到 /data/local/tmp 再用 pm install —— 无线 adb 不稳时的备选。"""
        remote = "/data/local/tmp/_push_install.apk"
        code, out = self._exec(["push", path, remote], timeout=timeout)
        if code != 0:
            return False, "push 失败：%s" % (out or "").strip()[-200:]
        flags = self._install_flags(grant=grant)
        script = "pm install %s %s" % (" ".join(flags), remote)
        code, out = self._shell(script, timeout=timeout, script=True)
        self._shell(["rm", "-f", remote], timeout=10)
        if code == 0 and "Success" in (out or ""):
            return True, "已安装（push + pm install）"
        return False, (out or "").strip()[-200:]

    def uninstall_package(self, package, keep_data=False):
        ok, info = self.ready()
        if not ok:
            return False, info
        package = (package or "").strip().split(":")[-1]
        if not package or "." not in package:
            return False, "包名看起来不对：%s（形如 com.example.app）" % package
        args = ["uninstall"] + (["-k"] if keep_data else []) + [package]
        code, out = self._exec(args, timeout=120)
        text = (out or "").strip()
        if "Success" in text:
            return True, "已卸载 %s" % package
        hint = self._install_hint(text)
        msg = "卸载失败：%s" % (text or "adb 没有返回原因")
        if hint:
            msg += "\n提示：%s" % hint
        return False, msg

    def launch_package(self, package):
        ok, info = self.ready()
        if not ok:
            return False, info
        package = (package or "").strip().split(":")[-1]
        if not package:
            return False, "请给包名"
        script = ("monkey -p %s -c android.intent.category.LAUNCHER 1" % package)
        code, out = self._shell(script, timeout=20, script=True)
        if code == 0 and "Events injected: 1" in (out or ""):
            return True, "已启动 %s" % package
        return False, "启动失败：%s" % ((out or "").strip()[-200:] or "包名对不对？")

    # -- 锁屏 / 解锁 -----------------------------------------------

    def is_locked(self):
        """当前是否还在锁屏界面（两种判据都看一眼）。"""
        code, out = self._shell(["dumpsys", "window"], timeout=20)
        if code != 0 or not out:
            return None
        if re.search(r"mDreamingLockscreen=true", out):
            return True
        focus = re.findall(r"mCurrentFocus=.*?\s([\w.]+/[\w.$]+)", out)
        if focus:
            top = focus[-1]
            return top.startswith("com.android.systemui") and (
                "StatusBar" in top or "keyguard" in top.lower())
        return None

    def unlock(self, pin=None):
        """
        一键解锁：唤醒 → 上滑 → （配置了 PIN 就）输入 PIN 并回车。

        ⚠ 安全锁屏（PIN/密码）对「注入文本」的限制因 ROM 而异，
        本函数会复查是否真的解开了，没解开就如实返回。
        """
        ok, info = self.ready()
        if not ok:
            return False, info
        self.send_keyevent(self.cfg.get("wake_keyevent", 224))   # 先唤醒
        time.sleep(0.4)
        self.swipe_ratio(0.5, 0.86, 0.5, 0.28, duration=180)     # 上滑密码盘
        time.sleep(0.7)
        if self.is_locked() is False:
            return True, "已解锁（没有设置安全锁）"
        if not pin:
            return False, ("上滑后仍在锁屏：这台手机设了安全锁，"
                           "请在 config.json 里填 unlock_pin（你的锁屏数字密码）")
        code, out = self._shell(["input", "text", str(pin)], timeout=10)
        if code != 0:
            return False, "注入密码失败: %s" % (out or "").strip()
        time.sleep(0.3)
        self.send_keyevent(66)                                   # 回车
        time.sleep(0.8)
        locked = self.is_locked()
        if locked is False:
            return True, "已解锁（PIN 注入成功）"
        if locked is None:
            return True, "唤醒 + 已上滑并输入 PIN（读不到锁屏状态，请看一眼手机）"
        return False, ("输入 PIN 后仍在锁屏：这个 ROM 挡住了注入文本。"
                       "建议改用小米的「蓝牙设备解锁」或 Smart Lock（贴身解锁）")

    # -- 亮度 ------------------------------------------------------

    def get_brightness(self):
        """用户亮度设置值，0-255。"""
        code, out = self._shell(["settings", "get", "system", "screen_brightness"], timeout=10)
        if code != 0:
            return None
        match = re.search(r"(\d+)", out or "")
        return int(match.group(1)) if match else None

    def brightness_state(self, force=False):
        """
        返回 (设置值, 占用窗口级亮度覆盖的包名, PMS 缓存亮度)。

        注：Android 13+ 起 `settings get system screen_brightness` 仍是权威的用户亮度，
        但最终生效值还会被两层覆盖压过：
          1. 系统级覆盖 cmd display set-brightness
          2. 应用窗口级覆盖（抖音/B站播放器的亮度手势），优先级最高
        这里把窗口覆盖的包名读出来，便于解释「改了没反应」。

        settings + dumpsys 合成**一次** adb 调用（原本是两次，白白多花 ~36ms）。
        """
        ttl = float(self.cfg.get("brightness_state_ttl", 2) or 0)
        if not force and self._bright_cache:
            value, at = self._bright_cache
            if ttl > 0 and time.time() - at < ttl:
                return value
        script = ("settings get system screen_brightness; echo @@BRIGHT@@; dumpsys display")
        code, out = self._shell(script, timeout=25, script=True)
        result = (None, None, None)
        if code == 0 and out:
            head, _sep, tail = out.partition("@@BRIGHT@@")
            match = re.search(r"(\d+)", head)
            setting = int(match.group(1)) if match else None
            override = None
            found = re.search(r"mBrightnessReason=override\(([\w.]+)/", tail)
            if found:
                override = found.group(1)
            cached = None
            cached_all = re.findall(r"mCachedBrightnessInfo\.brightness=([\d.eE+-]+)", tail)
            if cached_all:
                try:
                    cached = float(cached_all[-1])
                except ValueError:
                    cached = None
            result = (setting, override, cached)
        elif code != 0:
            # 通道/设备异常时退回老路径，保证信息不断
            setting = self.get_brightness()
            override, cached = None, None
            code2, out2 = self._shell(["dumpsys", "display"], timeout=25)
            if code2 == 0 and out2:
                found = re.search(r"mBrightnessReason=override\(([\w.]+)/", out2)
                if found:
                    override = found.group(1)
                cached_all = re.findall(r"mCachedBrightnessInfo\.brightness=([\d.eE+-]+)", out2)
                if cached_all:
                    try:
                        cached = float(cached_all[-1])
                    except ValueError:
                        cached = None
            result = (setting, override, cached)
        self._bright_cache = (result, time.time())
        return result

    def set_brightness(self, value):
        """绝对值设置（走 settings，持久化，重启后保留）。"""
        ok, info = self.ready()
        if not ok:
            return False, info
        value = int(max(1, min(255, value)))
        if self.cfg.get("brightness_disable_auto", True):
            self._shell(["settings", "put", "system", "screen_brightness_mode", "0"])
        code, out = self._shell(["settings", "put", "system", "screen_brightness", str(value)])
        if code != 0:
            return False, "设置亮度失败: %s" % (out.strip() or "unknown")
        self._bright_cache = None
        current = self.get_brightness()
        msg = "系统亮度 -> %s/255" % (current if current is not None else value)
        if self.cfg.get("warn_window_override", True):
            _, override, _ = self.brightness_state()
            if override:
                msg += ("｜⚠ %s 正在全屏占用亮度，屏幕上暂时看不出变化，"
                        "退出全屏后就是你设的这个值" % override)
        return True, msg

    def brightness_key(self, direction, repeat=1):
        """
        步进调节（走亮度键事件 keyevent 221/220）。

        为什么不用 settings：应用窗口级亮度覆盖优先级最高，settings 会被压住；
        而亮度键是系统自身通路，实测能穿透覆盖，且步长符合系统感知曲线。
        """
        ok, info = self.ready()
        if not ok:
            return False, info
        key = 221 if direction > 0 else 220
        code, out = self._shell(["input", "keyevent"] + [str(key)] * max(1, int(repeat)))
        if code != 0:
            return False, "亮度键发送失败: %s" % (out.strip() or "unknown")
        self._bright_cache = None
        current = self.get_brightness()
        msg = "系统亮度%s" % ("调高" if direction > 0 else "调低")
        if current is not None:
            msg += "（%s/255）" % current
        if self.cfg.get("warn_window_override", True):
            _, override, _ = self.brightness_state()
            if override:
                msg += "｜%s 全屏覆盖中，如需调它请用「App 亮度」滑动" % override
        return True, msg

    # -- App 内亮度（全屏里的滑动亮度，跟系统亮度是两条通道）-------

    def app_brightness_gesture(self, direction=1, steps=1):
        """
        模拟 App 自己的亮度手势（B站 / 抖音全屏里右侧上下滑）。

        ⚠ 这条通道改的是**应用窗口级亮度覆盖**，完全不动系统设置：
           - 全屏时：屏幕上看到的就是它
           - 退出全屏：回到我们用 settings / 亮度键设的系统亮度
        优先上滑增加、下滑降低（跟 B 站横屏一致），位置取屏幕右侧一条竖线，
        离边缘留足距离，避免误触系统的边缘返回手势。
        """
        ok, info = self.ready()
        if not ok:
            return False, info

        # 安全阀：没有 App 占用窗口亮度时，这记滑动可能被当成"切换视频/翻页"，宁可不发
        _, override, _ = self.brightness_state()
        if not override and self.cfg.get("app_bright_require_override", True):
            return False, ("现在没有 App 占用窗口亮度（手机上看到的就是系统亮度），"
                           "这记滑动可能会被当成翻页/切视频，已拦下。"
                           "先让视频进全屏再调；确实要发可以在 config.json 里把 "
                           "app_bright_require_override 设为 false")

        x = float(self.cfg.get("app_bright_x") or 0.12)
        low = float(self.cfg.get("app_bright_low", 0.38))
        high = float(self.cfg.get("app_bright_high", 0.62))
        steps = max(1, int(steps))
        for _ in range(steps):
            if direction > 0:
                ok, info = self.swipe_ratio(x, high, x, low, duration=180)   # 上滑 = 更亮
            else:
                ok, info = self.swipe_ratio(x, low, x, high, duration=180)   # 下滑 = 更暗
            if not ok:
                return False, info
            time.sleep(0.12)
        self._bright_cache = None
        msg = "App 亮度%s（%.0f%% 竖线滑动 %d 次）" % (
            "调高" if direction > 0 else "调低", x * 100, steps)
        if override:
            msg += "｜%s 正在占用窗口亮度" % override
        else:
            msg += "｜没有 App 占用窗口亮度（app_bright_require_override 已关闭，照样发出）"
        return True, msg

    def app_volume_gesture(self, direction=1, steps=1):
        """模拟 App 自己的音量手势（B站横屏右侧上下滑），同样不动系统音量。"""
        ok, info = self.ready()
        if not ok:
            return False, info
        x = float(self.cfg.get("app_vol_x") or 0.88)
        low = float(self.cfg.get("app_bright_low", 0.38))
        high = float(self.cfg.get("app_bright_high", 0.62))
        steps = max(1, int(steps))
        for _ in range(steps):
            if direction > 0:
                ok, info = self.swipe_ratio(x, high, x, low, duration=180)
            else:
                ok, info = self.swipe_ratio(x, low, x, high, duration=180)
            if not ok:
                return False, info
            time.sleep(0.12)
        return True, "App 音量%s（%.0f%% 竖线滑动 %d 次）" % (
            "调高" if direction > 0 else "调低", x * 100, steps)

    # -- 状态 ------------------------------------------------------

    def pointer_aids_state(self):
        """读取开发者选项里的指针显示设置。

        返回 (show_touches, pointer_location)，各项为 0/1，读不到为 None。
        """
        def one(key):
            code, out = self._shell(["settings", "get", "system", key], timeout=6)
            if code != 0:
                return None
            out = (out or "").strip()
            return int(out) if out in ("0", "1") else None
        return one("show_touches"), one("pointer_location")

    def set_pointer_aids(self, show_touches=None, pointer_location=None):
        """开关「显示触摸操作」/「指针位置」，传 None 的项不动。"""
        ok, msg = True, ""
        for key, val in (("show_touches", show_touches),
                         ("pointer_location", pointer_location)):
            if val is None:
                continue
            code, out = self._shell(["settings", "put", "system", key, str(int(val))],
                                    timeout=6)
            if code != 0:
                ok, msg = False, (out or "").strip()
        return ok, msg

    def screen_state(self):
        code, out = self._shell(["dumpsys", "power"], timeout=15)
        if code != 0:
            return None
        match = re.search(r"mWakefulness=(\w+)", out or "")
        if match:
            return match.group(1)
        match = re.search(r"Display Power: state=(\w+)", out or "")
        return match.group(1) if match else None

    # -- 配对 ------------------------------------------------------

    def pair(self, addr, code_str):
        code, out = self._exec(["pair", addr, code_str], timeout=40)
        text = (out or "").strip()
        ok = code == 0 and ("Successfully paired" in text or "already paired" in text.lower())
        if ok:
            self._connect(addr)
            if self._wait_online(addr, tries=5):
                self._remember(addr)
                return True, "配对成功，已连接 %s" % addr
            return True, "配对成功。请查看手机「无线调试」页的 IP:端口，再执行 connect"
        return False, text or "配对失败"


# ---------------------------------------------------------------- 动作表

KEYEVENTS = {
    "vol-up": 24,
    "vol-down": 25,
    "mute": 164,
    "play-pause": 85,
    "next-track": 87,
    "prev-track": 88,
    "home": 3,
    "back": 4,
    "enter": 66,
    "app-switch": 187,
}


class Controller(object):
    def __init__(self, cfg):
        self.cfg = cfg
        self.adb = Adb(cfg)

    # -- 模式 ------------------------------------------------------

    def mode(self):
        return (self.cfg.get("mode") or "bili-portrait").strip()

    def mode_info(self):
        return MODES.get(self.mode()) or MODES["bili-portrait"]

    def _mode_spec(self, action):
        return (self.mode_info().get("actions") or {}).get(action)

    def speed_point(self):
        """当前模式下"长按倍速"该按在哪儿（抖音是右侧 1/5，B站是画面中心）。"""
        point = self.mode_info().get("speed_point") or (0.5, 0.5)
        return float(point[0]), float(point[1])

    def _run_gesture(self, spec):
        """按模式表里的一条手势规格执行。"""
        kind = (spec or {}).get("type")
        adb = self.adb
        if kind == "swipe":
            path = spec.get("path") or [0.5, 0.8, 0.5, 0.25]
            ok, info = adb.swipe_ratio(*path, duration=spec.get("duration", 180))
            return (True, "已滑动 %s" % ("→".join("%.2f" % v for v in path))) if ok else (ok, info)
        if kind == "tap":
            return adb.tap_ratio(spec.get("fx", 0.5), spec.get("fy", 0.5))
        if kind == "double-tap":
            return adb.double_tap_ratio(spec.get("fx", 0.5), spec.get("fy", 0.5))
        if kind == "long-press":
            return adb.long_press_ratio(spec.get("fx", 0.5), spec.get("fy", 0.5),
                                        spec.get("ms", self.cfg.get("long_press_hold_ms", 1200)))
        if kind == "tap-node":
            ok, info = adb.tap_node(spec.get("texts") or [], spec.get("descs") or [],
                                    ids=spec.get("ids") or [],
                                    show_controls=spec.get("show_controls", True))
            if not ok and spec.get("msg"):
                info += "；" + spec["msg"]
            return ok, info
        return False, "模式表里有不认识的手势类型: %s" % kind

    def run_action(self, name, **kwargs):
        """执行动作，返回 (ok, message)。"""
        name = (name or "").strip().lower().replace("_", "-")
        if name == "next-video":
            name = "next"          # 横屏模式的「下一个视频」就是点按钮
        if name == "mode":
            return apply_mode(self.cfg, kwargs.get("value") or kwargs.get("mode"))

        # 模式表优先：同一个「下一个视频」在竖屏是上滑、在横屏是点按钮
        if name not in ("connect", "panel", "overlay-on", "overlay-off",
                        "overlay-toggle", "status", "mode"):
            spec = self._mode_spec(name)
            if spec is not None:
                return self._run_gesture(spec)

        if name == "lock":
            return self.adb.send_keyevent(self.cfg.get("lock_keyevent", 223))
        if name == "wake":
            return self.adb.send_keyevent(self.cfg.get("wake_keyevent", 224))
        if name in KEYEVENTS:
            return self.adb.send_keyevent(KEYEVENTS[name])
        if name == "key":
            return self.adb.send_keyevent(kwargs.get("keycode"))
        if name == "next":
            return self.adb.swipe_ratio(0.5, 0.80, 0.5, 0.25, self.cfg.get("swipe_duration", 160))
        if name == "prev":
            return self.adb.swipe_ratio(0.5, 0.25, 0.5, 0.80, self.cfg.get("swipe_duration", 160))
        if name in ("long-press", "speed"):
            ms = kwargs.get("ms") or self.cfg.get("long_press_hold_ms", 1200)
            if kwargs.get("x") is not None and kwargs.get("y") is not None:
                return self.adb.long_press(kwargs["x"], kwargs["y"], ms)
            return self.adb.long_press_center(ms)
        if name == "long-press-once":
            # 保留的「按一下长按」：同点 swipe，实测 App 认这个
            ms = kwargs.get("ms") or self.cfg.get("long_press_hold_ms", 1200)
            return self.adb.long_press_ratio(kwargs.get("fx", 0.5), kwargs.get("fy", 0.5), ms)
        if name in ("speed-on", "speed-toggle", "speed-off"):
            # 倍速开关：按住不放（带 MOVE 心跳），再按一次才松开。
            # 按哪儿由模式决定：抖音在右侧 1/5（按住 2x 不弹浮层），B站在画面中心。
            fx, fy = self.speed_point()
            if name == "speed-off" or self.adb.is_holding():
                ok, info = self.adb.hold_stop()
                return (True, "倍速已关（已松开）") if ok else (ok, info)
            if "fx" in kwargs or "fy" in kwargs:
                fx = kwargs.get("fx", fx)
                fy = kwargs.get("fy", fy)
            ok, info = self.adb.hold_start(fx, fy)
            if ok:
                return True, ("倍速已开（按住中）· 再按一次关闭｜%s"
                              % self.mode_info().get("speed_note", ""))
            return ok, info
        if name in ("like", "double-tap"):
            return self.adb.double_tap_ratio(kwargs.get("fx", 0.5), kwargs.get("fy", 0.5))
        if name in ("seek-forward", "seek-back"):
            if name == "seek-forward":
                return self.adb.swipe_ratio(0.35, 0.5, 0.75, 0.5, 180)
            return self.adb.swipe_ratio(0.75, 0.5, 0.35, 0.5, 180)
        if name in ("app-vol-up", "app-vol-down"):
            return self.adb.app_volume_gesture(
                +1 if name.endswith("up") else -1,
                kwargs.get("steps") or self.cfg.get("app_bright_steps", 1))
        if name == "unlock":
            return self.adb.unlock((self.cfg.get("unlock_pin") or "").strip() or None)
        if name in ("install-apk", "install"):
            return self.adb.install_apk(kwargs.get("path") or self.cfg.get("last_apk", ""))
        if name == "uninstall-apk":
            return self.adb.uninstall_package(kwargs.get("package") or kwargs.get("pkg"))
        if name in ("launch-app", "launch"):
            return self.adb.launch_package(kwargs.get("package") or kwargs.get("pkg"))
        if name in ("tap-xy",):
            return self.adb.tap_ratio(kwargs.get("fx", 0.5), kwargs.get("fy", 0.5))
        if name == "bright-up":
            return self.adb.brightness_key(+1, self.cfg.get("brightness_key_repeat", 1))
        if name == "bright-down":
            return self.adb.brightness_key(-1, self.cfg.get("brightness_key_repeat", 1))
        if name in ("app-bright-up", "fullscreen-bright-up", "window-bright-up"):
            return self.adb.app_brightness_gesture(
                +1, kwargs.get("steps") or self.cfg.get("app_bright_steps", 1))
        if name in ("app-bright-down", "fullscreen-bright-down", "window-bright-down"):
            return self.adb.app_brightness_gesture(
                -1, kwargs.get("steps") or self.cfg.get("app_bright_steps", 1))
        if name == "bright":
            return self.adb.set_brightness(kwargs.get("value"))
        if name == "bright-get":
            setting, override, cached = self.adb.brightness_state()
            if setting is None:
                return False, "读不到亮度"
            msg = "系统亮度 %s/255" % setting
            if cached is not None:
                msg += " · 当前生效值 %.4f" % cached
            if override:
                msg += ("｜⚠ %s 正在全屏占用窗口亮度（全屏里看到的不是系统亮度，"
                        "要调它用「App 亮度 + / −」滑动；退出全屏就是 %s/255）"
                        % (override, setting))
            else:
                msg += "｜当前没有 App 占用，屏幕上生效的就是它"
            return True, msg
        if name == "swipe":
            return self.adb.swipe(kwargs.get("x1"), kwargs.get("y1"),
                                  kwargs.get("x2"), kwargs.get("y2"),
                                  kwargs.get("duration", 160))
        if name == "tap":
            return self.adb.tap(kwargs.get("x"), kwargs.get("y"))
        if name == "connect":
            ok, info = self.adb.ensure(force=True)
            return ok, info
        if name == "panel":
            ok = _panel("panel", self.cfg)
            return ok, "控制面板已唤起" if ok else "控制面板正在启动，稍后再试"
        if name in ("overlay", "overlay-toggle"):
            ok = _panel("overlay-toggle", self.cfg)
            return ok, "映射层已切换" if ok else "映射层正在启动，稍后再试"
        if name == "overlay-on":
            ok = _panel("overlay-on", self.cfg)
            return ok, "映射层已开启" if ok else "映射层正在启动，稍后再试"
        if name == "overlay-off":
            ok = _panel("overlay-off", self.cfg)
            return ok, "映射层已关闭" if ok else "映射层正在启动，稍后再试"
        return False, "未知动作: %s" % name


# ---------------------------------------------------------------- 托盘 + 热键

CLR_OK = (29, 158, 117)
CLR_WARN = (186, 117, 23)
CLR_ERR = (226, 75, 74)
CLR_DARK = (95, 94, 90)


class TrayApp(object):
    def __init__(self, cfg):
        self.cfg = cfg
        self.ctl = Controller(cfg)
        self.icon = None
        self._busy = threading.Lock()
        self.hotkeys = None
        self._cfg_mtime = 0.0

    # -- 配置热重载 --------------------------------------------------

    def _config_mtime(self):
        try:
            return os.path.getmtime(CONFIG_PATH)
        except OSError:
            return 0.0

    def _watch_config(self):
        """盯着 config.json：在控制面板里改完快捷键后自动重载，不用重启托盘。"""
        while True:
            time.sleep(2.0)
            mtime = self._config_mtime()
            if mtime and mtime != self._cfg_mtime:
                self._cfg_mtime = mtime
                self._reload_config()

    def _reload_config(self):
        try:
            new_cfg = load_config()
        except Exception as exc:
            log("[config] 重新读取失败: %s" % exc)
            return
        self.cfg = new_cfg
        self.ctl = Controller(new_cfg)
        if self.hotkeys is not None:
            try:
                self.hotkeys.stop()
            except Exception:
                pass
            self.hotkeys = None
        self.hotkeys = self.start_hotkeys()
        try:
            if self.icon is not None:
                self.icon.menu = self._menu()
                self.icon.update_menu()
        except Exception as exc:
            log("[config] 刷新托盘菜单失败: %s" % exc)
        count = len(new_cfg.get("actions") or {})
        log("配置已变更：已重载 %s 个热键" % count)
        self._notify("已重载 %s 个快捷键" % count, "设置已生效")

    def _image(self, color):
        from PIL import Image, ImageDraw

        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle([16, 3, 48, 61], radius=7, outline=CLR_DARK, width=3)
        draw.rectangle([27, 7, 37, 9], fill=CLR_DARK)
        draw.rounded_rectangle([22, 30, 42, 45], radius=3, fill=color)
        draw.arc([26, 19, 38, 33], start=180, end=360, fill=color, width=3)
        return img

    def _set_color(self, color):
        if self.icon is not None:
            try:
                self.icon.icon = self._image(color)
            except Exception:
                pass

    def _notify(self, message, title):
        if not self.cfg.get("notify", True) or self.icon is None:
            return
        try:
            self.icon.notify(message, title)
        except Exception:
            pass

    def _fire(self, action, title, **kwargs):
        """把动作丢到后台线程执行，避免阻塞热键监听。"""
        def runner():
            if not self._busy.acquire(blocking=False):
                return
            try:
                ok, info = self.ctl.run_action(action, **kwargs)
                log(("%s: %s" % (action, "OK" if ok else "FAIL")) + " " + info)
                self._set_color(CLR_OK if ok else CLR_ERR)
                self._notify(info if ok else ("失败\n" + info), title)
            except Exception as exc:
                log("动作异常 %s: %s" % (action, exc))
            finally:
                self._busy.release()
        threading.Thread(target=runner, daemon=True).start()

    # -- 托盘菜单 --------------------------------------------------

    def _left_click_spec(self):
        """按 config.left_click 决定左键单击做什么：both / panel / overlay / none。"""
        mode = str(self.cfg.get("left_click", "both") or "both").strip().lower()
        if mode == "none":
            return None
        if mode == "panel":
            return ("panel", "打开控制面板（左键）")
        if mode == "overlay":
            return ("overlay-on", "打开映射层（左键）")
        return ("overlay-on", "打开面板 + 映射层（左键）")

    def _menu(self):
        import pystray

        items = []
        spec = self._left_click_spec()
        if spec:
            action, label = spec
            items.append(pystray.MenuItem(
                label, lambda a=action: self._fire(a, "控制面板"), default=True))

        items += [
            pystray.MenuItem("控制面板", lambda: self._fire("panel", "控制面板")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("锁定手机", lambda: self._fire("lock", "锁屏")),
            pystray.MenuItem("唤醒屏幕", lambda: self._fire("wake", "唤醒")),
            pystray.MenuItem("解锁手机（唤醒+上滑+PIN）", lambda: self._fire("unlock", "解锁")),
            pystray.MenuItem("安装 APK 到手机…", _open_install_console),
            pystray.MenuItem("安装上次的 APK（%s）"
                             % (os.path.basename(self.cfg.get("last_apk") or "未设置")[:24],),
                             lambda: self._fire("install-apk", "安装 APK",
                                                path=self.cfg.get("last_apk") or "")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("映射层（手机矩形）", pystray.Menu(
                pystray.MenuItem("开启", lambda: self._fire("overlay-on", "映射层")),
                pystray.MenuItem("关闭", lambda: self._fire("overlay-off", "映射层")),
                pystray.MenuItem("切换", lambda: self._fire("overlay-toggle", "映射层")),
            )),
            pystray.MenuItem("音量", pystray.Menu(
                pystray.MenuItem("音量 +", lambda: self._fire("vol-up", "音量")),
                pystray.MenuItem("音量 -", lambda: self._fire("vol-down", "音量")),
                pystray.MenuItem("静音", lambda: self._fire("mute", "音量")),
            )),
            pystray.MenuItem("系统亮度（退出全屏后生效）", pystray.Menu(
                pystray.MenuItem("亮度 +", lambda: self._fire("bright-up", "亮度")),
                pystray.MenuItem("亮度 -", lambda: self._fire("bright-down", "亮度")),
                pystray.MenuItem("查看当前亮度", lambda: self._fire("bright-get", "亮度")),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("最暗 (10%)", lambda: self._fire("bright", "亮度", value=26)),
                pystray.MenuItem("中等 (50%)", lambda: self._fire("bright", "亮度", value=128)),
                pystray.MenuItem("最亮 (100%)", lambda: self._fire("bright", "亮度", value=255)),
            )),
            pystray.MenuItem("App 内滑动（全屏时用）", pystray.Menu(
                pystray.MenuItem("亮度 +（左侧上滑）", lambda: self._fire("app-bright-up", "App 亮度")),
                pystray.MenuItem("亮度 -（左侧下滑）", lambda: self._fire("app-bright-down", "App 亮度")),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("音量 +（右侧上滑）", lambda: self._fire("app-vol-up", "App 音量")),
                pystray.MenuItem("音量 -（右侧下滑）", lambda: self._fire("app-vol-down", "App 音量")),
            )),
            pystray.MenuItem("使用模式", pystray.Menu(*[
                pystray.MenuItem(info.get("label", key),
                                 lambda _i=None, m=key: self._fire("mode", "模式", value=m),
                                 checked=lambda item, m=key: (self.cfg.get("mode") or "") == m,
                                 radio=True)
                for key, info in MODES.items()
            ])),
            pystray.MenuItem("刷视频", pystray.Menu(
                pystray.MenuItem("下一个（上滑）", lambda: self._fire("next", "刷视频")),
                pystray.MenuItem("上一个（下滑）", lambda: self._fire("prev", "刷视频")),
                pystray.MenuItem("播放 / 暂停", lambda: self._fire("play-pause", "播放")),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("倍速播放（长按屏幕中心）", lambda: self._fire("long-press", "倍速")),
            )),
            pystray.MenuItem("长按 / 倍速", pystray.Menu(
                pystray.MenuItem("倍速开关（按住不放）",
                                 lambda: self._fire("speed-toggle", "倍速")),
                pystray.MenuItem("长按一下（按一次长按）",
                                 lambda: self._fire("long-press-once", "长按")),
            )),
            pystray.MenuItem("导航", pystray.Menu(
                pystray.MenuItem("返回", lambda: self._fire("back", "导航")),
                pystray.MenuItem("回到桌面", lambda: self._fire("home", "导航")),
                pystray.MenuItem("任务切换", lambda: self._fire("app-switch", "导航")),
            )),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("重新连接", lambda: self._fire("connect", "无线 adb")),
            pystray.MenuItem("连接手机（IP:端口 / 配对码）", _open_connect_console),
            pystray.MenuItem("打开配置文件", lambda: _open(CONFIG_PATH)),
            pystray.MenuItem("打开日志", lambda: _open(LOG_PATH)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", self._quit),
        ]
        return pystray.Menu(*items)

    def _quit(self, *_args):
        try:
            _panel_send("quit", self.cfg)
        except Exception:
            pass
        if self.icon is not None:
            try:
                self.icon.stop()
            except Exception:
                pass

    # -- 热键 ------------------------------------------------------

    def start_hotkeys(self):
        try:
            from pynput import keyboard
        except Exception as exc:
            log("pynput 不可用，热键已跳过: %s" % exc)
            return None

        labels = {
            "lock": "锁屏", "wake": "唤醒", "vol-up": "音量+", "vol-down": "音量-",
            "mute": "静音", "play-pause": "播放暂停", "next": "下一个", "prev": "上一个",
            "bright-up": "亮度+", "bright-down": "亮度-", "home": "桌面", "back": "返回",
            "overlay": "映射层", "overlay-toggle": "映射层", "overlay-on": "映射层",
            "overlay-off": "映射层", "panel": "控制面板", "bright-get": "亮度",
            "connect": "连接", "long-press": "倍速", "speed": "倍速",
            "app-bright-up": "App亮度", "app-bright-down": "App亮度",
            "app-vol-up": "App音量", "app-vol-down": "App音量",
            "unlock": "解锁", "speed-on": "倍速开", "speed-off": "倍速关",
            "speed-toggle": "倍速开关", "long-press-once": "长按", "like": "点赞",
            "seek-forward": "快进", "seek-back": "快退", "mode": "模式",
        }
        mapping = {}
        for hotkey, action in (self.cfg.get("actions") or {}).items():
            if not hotkey or not action:
                continue
            title = labels.get(action, action)
            mapping[hotkey] = (lambda a=action, t=title: self._fire(a, t))

        if not mapping:
            return None
        try:
            listener = keyboard.GlobalHotKeys(mapping)
            listener.daemon = True
            listener.start()
            for hotkey, action in (self.cfg.get("actions") or {}).items():
                log("热键 %s -> %s" % (hotkey, action))
            return listener
        except Exception as exc:
            log("热键启动失败: %s" % exc)
            return None

    def run(self):
        have_tray = True
        try:
            import PIL  # noqa: F401
            import pystray  # noqa: F401
        except Exception as exc:
            have_tray = False
            log("托盘不可用（%s），进入纯热键模式" % exc)

        self._cfg_mtime = self._config_mtime()
        threading.Thread(target=self._watch_config, daemon=True).start()
        self.hotkeys = self.start_hotkeys()
        self._fire("connect", "无线 adb")

        if have_tray:
            import pystray
            self.icon = pystray.Icon("phone_remote_lock", self._image(CLR_WARN),
                                     "手机遥控（左键=面板/映射层，右键=菜单）", self._menu())
            log("托盘已启动")
            self.icon.run()
        else:
            print("未安装 pystray / Pillow，已切换为纯热键模式。")
            for hotkey, action in (self.cfg.get("actions") or {}).items():
                print("  %-28s -> %s" % (hotkey, action))
            print("按 Ctrl+C 退出。")
            while True:
                time.sleep(1)


PANEL_SPAWN_COOLDOWN = 8.0
_last_panel_spawn = [0.0]


def panel_port(cfg=None):
    return int((cfg or load_config()).get("panel_port", 59731))


def _panel_send(command, cfg=None, timeout=1.5):
    """把命令发给已运行的控制面板进程。"""
    try:
        with socket.create_connection(("127.0.0.1", panel_port(cfg)), timeout=timeout) as sock:
            sock.sendall(str(command).encode("utf-8"))
            sock.settimeout(timeout)
            sock.recv(64)
        return True
    except Exception:
        return False


def _panel(command="panel", cfg=None):
    """
    唤起控制面板进程并把命令交给它；没在运行就启动一个。

    面板是单实例的（用 127.0.0.1 端口做锁），所以可以放心反复点。
    """
    cfg = cfg or load_config()
    if _panel_send(command, cfg):
        return True

    now = time.time()
    if now - _last_panel_spawn[0] < PANEL_SPAWN_COOLDOWN:
        log("控制面板正在启动中，忽略重复唤起（%s）" % command)
        return False
    _last_panel_spawn[0] = now

    try:
        script = os.path.join(HERE, "panel.py")
        if not os.path.isfile(script):
            log("未找到 panel.py，无法唤起控制面板")
            return False
        exe = sys.executable
        pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.isfile(pyw):
            exe = pyw
        flags = 0x00000008 | NO_WINDOW if os.name == "nt" else 0
        subprocess.Popen([exe, script, "--cmd", command], cwd=HERE, creationflags=flags)
        log("已启动控制面板进程（cmd=%s）" % command)

        for _ in range(12):
            time.sleep(0.4)
            if _panel_send("ping", cfg):
                # 子进程已经自己执行过这个命令，这里不能再发一次（否则 toggle 会相互抵消）
                return True
        return True
    except Exception as exc:
        log("启动控制面板失败: %s" % exc)
        return False


def _open(path):
    try:
        if os.name == "nt":
            os.startfile(path)  # noqa
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception as exc:
        log("打开 %s 失败: %s" % (path, exc))


def _open_install_console():
    """在新控制台里跑「安装 APK」引导（托盘菜单用）。"""
    exe = sys.executable or ""
    if exe.lower().endswith("pythonw.exe"):
        cand = os.path.join(os.path.dirname(exe), "python.exe")
        if os.path.exists(cand):
            exe = cand
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    flags = 0x00000010 if os.name == "nt" else 0
    try:
        subprocess.Popen([exe, os.path.join(HERE, "lock_phone.py"), "install-apk"],
                         cwd=HERE, creationflags=flags, env=env)
    except Exception as exc:
        log("[apk] 打开安装窗口失败: %s" % exc)


def _open_connect_console():
    """在新控制台窗口里跑「输入 IP:端口 连接手机」的引导（托盘菜单用）。"""
    exe = sys.executable or ""
    if exe.lower().endswith("pythonw.exe"):
        cand = os.path.join(os.path.dirname(exe), "python.exe")
        if os.path.exists(cand):
            exe = cand
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"  # 兜底：保证写进新控制台的是 UTF-8
    flags = 0x00000010 if os.name == "nt" else 0  # CREATE_NEW_CONSOLE
    try:
        subprocess.Popen([exe, os.path.join(HERE, "lock_phone.py"), "connect"],
                         cwd=HERE, creationflags=flags, env=env)
    except Exception as exc:
        log("[connect] 打开连接窗口失败: %s" % exc)


# ---------------------------------------------------------------- CLI

def say(message):
    try:
        print(message)
    except Exception:
        pass


def _parse_bright(text):
    text = str(text).strip()
    if text.endswith("%"):
        return int(round(float(text[:-1]) / 100.0 * 255))
    return int(float(text))


MENU_ITEMS = [
    ("1", "锁定手机", "lock"),
    ("2", "唤醒屏幕", "wake"),
    ("3", "音量 +", "vol-up"),
    ("4", "音量 -", "vol-down"),
    ("5", "静音", "mute"),
    ("6", "亮度 +", "bright-up"),
    ("7", "亮度 -", "bright-down"),
    ("+", "App 亮度 +（全屏内滑动）", "app-bright-up"),
    ("-", "App 亮度 -（全屏内滑动）", "app-bright-down"),
    ("8", "查看当前亮度", "bright-get"),
    ("9", "下一个视频（上滑）", "next"),
    ("a", "上一个视频（下滑）", "prev"),
    ("p", "播放 / 暂停", "play-pause"),
    ("b", "返回键", "back"),
    ("h", "回到桌面", "home"),
    ("n", "通知栏", "key:83"),
    ("v", "倍速开关（按住不放，再按关闭）", "speed-toggle"),
    ("g", "长按一下（1.2 秒）", "long-press-once"),
    ("u", "解锁手机", "unlock"),
    ("", "安装 APK 到手机（拖文件到这里也行）", "install-apk"),
    ("o", "切换映射层（手机矩形）", "overlay-toggle"),
    ("k", "打开控制面板", "panel"),
    ("s", "查看连接状态", "status"),
    ("c", "重新连接（用已保存的地址）", "connect"),
    ("i", "输入 IP:端口 连接手机", "connect-input"),
]


def _ask(prompt, default=""):
    """读取一行输入；EOF / Ctrl+C 返回 None 表示取消。"""
    try:
        text = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return None
    return text or default


def _split_addr(text, fallback):
    """把用户输入整理成 IP:端口。

    允许三种写法：完整的 192.168.1.23:42007 / 只给端口 42007 / 只给 IP。
    缺的那一半用 fallback（上次保存的地址）补齐。
    """
    text = (text or "").strip()
    if not text:
        return ""
    if ":" in text:
        return text
    f_ip, _, f_port = (fallback or "").partition(":")
    if text.isdigit():
        return "%s:%s" % (f_ip, text) if f_ip else ""
    return "%s:%s" % (text, f_port) if f_port else ""


def _local_subnets():
    """本机所有 IPv4 的 /24 前缀，用来判断保存的地址是不是已经不在当前网段。"""
    prefixes = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127."):
                prefixes.add(ip.rsplit(".", 1)[0])
    except Exception:
        pass
    return prefixes


def _mismatch_note(addr):
    """保存的地址看起来不在本机网段时给一句提示（换了 Wi-Fi 后常见）。"""
    ip = (addr or "").split(":")[0]
    if not ip or "." not in ip:
        return ""
    prefixes = _local_subnets()
    if prefixes and ip.rsplit(".", 1)[0] not in prefixes:
        return "（不在本机当前网段，多半已失效，建议以手机「无线调试」页显示的为准）"
    return ""


def _parse_pair_line(text, fallback):
    """解析配对输入，支持一次粘贴「IP:端口 123456」或只给端口 / 只给 IP。

    返回 (配对地址, 配对码)，缺的部分为空字符串。
    """
    text = (text or "").strip()
    if not text:
        return "", ""
    parts = text.split()
    addr_part, code = "", ""
    for piece in parts:
        if not addr_part and (":" in piece or piece.replace(".", "").isdigit()):
            addr_part = piece
        elif not code and piece.isdigit() and len(piece) >= 6:
            code = piece
    return _split_addr(addr_part, fallback), code


def interactive_connect(ctl, cfg):
    """交互式绑定手机：手动输入 IP:端口，连不上还可以用配对码重新配对。"""
    adb = ctl.adb
    say("=" * 52)
    say("   连接手机（无线 adb）")
    say("=" * 52)

    if not adb.adb:
        say("FAIL 未找到 adb。请先装 Android Platform Tools，")
        say("     或在 config.json 的 adb_path 里填 adb.exe 的完整路径。")
        return 1

    say("   手机端：设置 → 开发者选项 → 无线调试（打开）")
    say("   页面顶部显示的 IP:端口 就是要填的地址，例如 192.168.1.23:42007")
    say("   提示：刚换过 Wi-Fi、或者提示 offline / 连不上时，")
    say("         手机「无线调试」页 →「使用配对码配对设备」走一次配对最省事。")
    say("-" * 52)

    saved = (cfg.get("device") or "").strip()
    guess = ""
    pair_hint = ""
    for _name, service, addr in adb.mdns_services():
        if service == "_adb-tls-connect._tcp" and not guess:
            guess = addr
        elif service == "_adb-tls-pairing._tcp" and not pair_hint:
            pair_hint = addr
    if guess:
        say("   自动发现到设备 : %s" % guess)
    if pair_hint:
        say("   自动发现到配对口 : %s（手机上的配对弹窗正开着）" % pair_hint)
    if saved and saved != guess:
        say("   上次保存的地址 : %s %s" % (saved, _mismatch_note(saved)))
    say("")

    default = guess or saved
    text = _ask("   请输入 手机IP:端口%s: "
                % ("（直接回车用 %s）" % default if default else "（如 192.168.1.23:42007）"))
    if text is None:
        say("已取消")
        return 1

    addr = _split_addr(text or default, saved)
    if not addr:
        if not text and not default:
            say("FAIL 没有输入地址，已取消")
        else:
            say("FAIL 地址不完整，请直接填 IP:端口（例如 192.168.1.23:42007）")
        return 1

    say("")
    say("   正在连接 %s …" % addr)
    ok, info = adb.connect_addr(addr)
    say(("OK   " if ok else "FAIL ") + info)
    if ok:
        say("")
        print_status(ctl)
        return 0

    # 连不上：直接把配对选项摆出来（默认就是配对，回车即进）
    say("")
    say("   连接失败。要用「配对码」重新配对再连一次吗？")
    say("   手机上打开「无线调试」→「使用配对码配对设备」，会显示 端口 + 6 位配对码。")
    answer = _ask("   [Y] 现在配对 / [n] 退出（直接回车 = 配对）: ", "y") or "y"
    if answer.lower() not in ("y", "yes"):
        say("已退出。地址没变，改好后可以重跑本脚本，或：lock_phone.py connect IP:端口")
        return 1

    say("")
    say("   把手机上的「配对端口」和「配对码」填进来；")
    say("   也可以一次整行粘进来，例如：%s 123456"
        % (pair_hint or "192.168.1.23:44667"))
    pair_text = _ask("   配对 IP:端口 + 配对码: ")
    if pair_text is None:
        say("FAIL 已取消")
        return 1
    pair_addr, code = _parse_pair_line(pair_text, pair_hint or addr)
    if not pair_addr:
        say("FAIL 配对地址没认出来，已取消")
        return 1
    if not code:
        code = _ask("   6 位配对码: ") or ""
    if not code:
        say("FAIL 没有配对码，已取消")
        return 1

    say("")
    say("   正在配对 %s …" % pair_addr)
    ok, info = adb.pair(pair_addr, code)
    say(("OK   " if ok else "FAIL ") + info)
    if not ok:
        say("   配对失败通常是：配对码过期（弹窗关了要重开）、")
        say("   或者填成了「连接端口」而不是「配对端口」。")
        return 1

    # 配对成功后，用手机当前显示的连接端口再连一次
    new_addr = addr
    if pair_addr.split(":")[0] == addr.split(":")[0] and guess and guess != addr:
        new_addr = guess
    say("   正在连接 %s …" % new_addr)
    ok, info = adb.connect_addr(new_addr)
    say(("OK   " if ok else "FAIL ") + info)
    if ok:
        say("")
        print_status(ctl)
        return 0

    if new_addr != addr:
        say("   换个端口再试：%s" % addr)
        ok, info = adb.connect_addr(addr)
        say(("OK   " if ok else "FAIL ") + info)
        if ok:
            say("")
            print_status(ctl)
            return 0
    return 1


def print_status(ctl):
    if not ctl.adb.adb:
        say("FAIL 未找到 adb，请安装 Android Platform Tools")
        return False
    ok, info = ctl.adb.ensure()
    if not ok:
        say("FAIL " + info)
        return False
    say("OK   已连接 %s" % info)
    say("     屏幕    : %s" % (ctl.adb.screen_state() or "未知"))
    setting, override, cached = ctl.adb.brightness_state()
    if setting is None:
        say("     亮度    : 读取失败")
    else:
        line = "     亮度    : 设置 %s/255" % setting
        if cached is not None:
            line += " · 生效 %.4f" % cached
        say(line)
        if override:
            say("               ⚠ %s 正在占用窗口级亮度覆盖" % override)
            say("                 （抖音/B站 播放器的亮度手势会这样，此时只有亮度键能穿透）")
    size = ctl.adb.screen_size()
    say("     分辨率  : %s" % ("%s x %s" % size if size[0] else "未知"))
    return True


def interactive_menu(ctl, cfg):
    """给「双击运行」用的遥控面板。"""
    clear = "cls" if os.name == "nt" else "clear"
    while True:
        os.system(clear)
        say("=" * 48)
        say("   手机遥控器   (绑定无线 adb)")
        say("=" * 48)
        for key, label, _ in MENU_ITEMS:
            say("   %-3s %s" % (key, label))
        say("   q   退出")
        say("=" * 48)
        try:
            choice = input("   请选择: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return 0
        if choice in ("q", "quit", "exit"):
            return 0
        for key, label, act in MENU_ITEMS:
            if choice == key:
                say("")
                if act == "status":
                    print_status(ctl)
                elif act.startswith("key:"):
                    ok, info = ctl.run_action("key", keycode=int(act.split(":", 1)[1]))
                    say(("OK   " if ok else "FAIL ") + info)
                elif act == "connect":
                    ok, info = ctl.adb.ensure(force=True)
                    say(("OK   " if ok else "FAIL ") + info)
                elif act == "connect-input":
                    interactive_connect(ctl, cfg)
                else:
                    ok, info = ctl.run_action(act)
                    say(("OK   " if ok else "FAIL ") + info)
                input("\n   按回车继续…")
                break


def main(argv):
    cfg = load_config()
    action = (argv[1].lower() if len(argv) > 1 else "").strip().replace("_", "-")
    ctl = Controller(cfg)

    simple = {
        "lock": {}, "wake": {}, "vol-up": {}, "vol-down": {}, "mute": {},
        "play-pause": {}, "next-track": {}, "prev-track": {}, "home": {}, "back": {},
        "app-switch": {}, "next": {}, "prev": {}, "bright-up": {}, "bright-down": {},
        "app-bright-up": {}, "app-bright-down": {},
        "app-vol-up": {}, "app-vol-down": {}, "unlock": {}, "like": {},
        "speed-on": {}, "speed-off": {}, "speed-toggle": {}, "long-press-once": {},
        "seek-forward": {}, "seek-back": {}, "next-video": {},
        "install-apk": {}, "uninstall-apk": {}, "launch-app": {},
        "bright-get": {}, "panel": {}, "overlay-on": {}, "overlay-off": {},
        "overlay-toggle": {},
    }

    if action in simple:
        ok, info = ctl.run_action(action)
        say(("OK   " if ok else "FAIL ") + info)
        return 0 if ok else 1

    if action in ("install-apk", "install"):
        path = argv[2] if len(argv) > 2 else ""
        if not path:
            say("把 APK 拖到这个窗口里，或直接输入完整路径。")
            say("（也可以把 APK 拖到 install_apk.bat 上，一步装完）")
            path = _ask("   APK 路径: ") or ""
        path = path.strip().strip('"')
        if not path:
            say("FAIL 没给路径，已取消")
            return 1
        say("   正在安装 %s …" % path)
        ok, info = ctl.run_action("install-apk", path=path)
        say(("OK   " if ok else "FAIL ") + info)
        if ok:
            cfg["last_apk"] = path
            cfg["last_apk_dir"] = os.path.dirname(path)
            save_config(cfg)
        return 0 if ok else 1

    if action in ("uninstall-apk", "uninstall"):
        pkg = argv[2] if len(argv) > 2 else ""
        if not pkg:
            pkg = _ask("   要卸载的包名（如 com.example.app）: ") or ""
        if not pkg:
            say("FAIL 没给包名，已取消")
            return 1
        ok, info = ctl.run_action("uninstall-apk", package=pkg)
        say(("OK   " if ok else "FAIL ") + info)
        return 0 if ok else 1

    if action in ("launch-app", "launch"):
        pkg = argv[2] if len(argv) > 2 else ""
        if not pkg:
            pkg = _ask("   要启动的包名: ") or ""
        ok, info = ctl.run_action("launch-app", package=pkg)
        say(("OK   " if ok else "FAIL ") + info)
        return 0 if ok else 1

    if action == "mode":
        if len(argv) < 3:
            key = ctl.mode()
            info = ctl.mode_info()
            say("当前模式: %s（%s）" % (key, info.get("label", key)))
            say("        %s" % info.get("hint", ""))
            say("可用: %s" % " / ".join(MODES))
            return 0
        ok, info = apply_mode(cfg, argv[2])
        say(("OK   " if ok else "FAIL ") + info)
        if ok:
            ctl = Controller(cfg)
        return 0 if ok else 2

    if action in ("long-press", "speed"):
        ms = int(argv[2]) if len(argv) > 2 and argv[2].isdigit() else None
        ok, info = ctl.run_action("long-press", **({"ms": ms} if ms else {}))
        say(("OK   " if ok else "FAIL ") + info)
        return 0 if ok else 1

    if action == "bright":
        if len(argv) < 3:
            say("用法: python lock_phone.py bright 60%   或   bright 153")
            return 2
        ok, info = ctl.run_action("bright", value=_parse_bright(argv[2]))
        say(("OK   " if ok else "FAIL ") + info)
        return 0 if ok else 1

    if action == "key":
        if len(argv) < 3:
            say("用法: python lock_phone.py key 26")
            return 2
        ok, info = ctl.run_action("key", keycode=int(argv[2]))
        say(("OK   " if ok else "FAIL ") + info)
        return 0 if ok else 1

    if action == "swipe":
        if len(argv) < 6:
            say("用法: python lock_phone.py swipe x1 y1 x2 y2 [毫秒]")
            return 2
        duration = int(argv[6]) if len(argv) > 6 else 160
        ok, info = ctl.run_action("swipe", x1=int(argv[2]), y1=int(argv[3]),
                                  x2=int(argv[4]), y2=int(argv[5]), duration=duration)
        say(("OK   " if ok else "FAIL ") + info)
        return 0 if ok else 1

    if action == "tap":
        if len(argv) < 4:
            say("用法: python lock_phone.py tap x y")
            return 2
        ok, info = ctl.run_action("tap", x=int(argv[2]), y=int(argv[3]))
        say(("OK   " if ok else "FAIL ") + info)
        return 0 if ok else 1

    if action == "status":
        return 0 if print_status(ctl) else 1

    if action == "menu":
        return interactive_menu(ctl, cfg)

    if action == "connect":
        # 带地址：connect 192.168.1.23:42007
        if len(argv) > 2:
            ok, info = ctl.adb.connect_addr(argv[2])
            say(("OK   " if ok else "FAIL ") + info)
            return 0 if ok else 1
        # 没有地址：能交互就进入引导输入，否则用已保存地址自动重连
        if sys.stdin and sys.stdin.isatty():
            return interactive_connect(ctl, cfg)
        ok, info = ctl.adb.ensure(force=True)
        say(("OK   " if ok else "FAIL ") + info)
        return 0 if ok else 1

    if action in ("link", "setup-connection", "connect-input"):
        return interactive_connect(ctl, cfg)

    if action == "discover":
        if not ctl.adb.adb:
            say("FAIL 未找到 adb")
            return 1
        entries = ctl.adb.mdns_services()
        if not entries:
            say("未发现设备。请确认手机「无线调试」已开启，且本机与手机在同一 Wi-Fi。")
            return 1
        connect_addrs = []
        pairing_addrs = []
        for _name, service, addr in entries:
            if service == "_adb-tls-connect._tcp":
                connect_addrs.append(addr)
            elif service == "_adb-tls-pairing._tcp":
                pairing_addrs.append(addr)
        for addr in connect_addrs:
            say("  [连接] adb connect %s" % addr)
        for addr in pairing_addrs:
            say("  [配对] adb pair %s <6位配对码>" % addr)
        if pairing_addrs:
            say("")
            say("  提示：配对端口和连接端口是两个不同的端口，别混用。")
        return 0 if connect_addrs else 1

    if action == "pair":
        if len(argv) < 4:
            say("用法: python lock_phone.py pair IP:PORT CODE")
            return 2
        ok, info = ctl.adb.pair(argv[2], argv[3])
        say(("OK   " if ok else "FAIL ") + info)
        return 0 if ok else 1

    if action in ("-h", "--help", "help", "?"):
        say(__doc__)
        return 0

    if action:
        say("未知命令: %s" % action)
        say("可用: lock / wake / vol-up / vol-down / mute / play-pause / next / prev /")
        say("      bright-up / bright-down / bright <值> / swipe / tap / key /")
        say("      status / connect [IP:PORT] / discover / pair / mode [key] /")
        say("      speed-toggle / long-press-once / unlock / like / app-bright-up /")
        say("      app-vol-up / seek-forward / next-video")
        return 2

    if not single_instance():
        say("已经有一个托盘程序在运行了，本次不再启动（避免热键重复触发）。")
        say("想重启的话：在托盘图标上右键 → 退出，然后再运行 start.bat。")
        return 0

    say("adb       : %s" % (ctl.adb.adb or "(未找到)"))
    say("已保存设备: %s" % (ctl.adb.target or "(尚未连接)"))
    say("热键映射  :")
    for hotkey, act in (cfg.get("actions") or {}).items():
        say("    %-28s -> %s" % (hotkey, act))
    say("正在启动托盘程序…")
    TrayApp(cfg).run()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
