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
    python lock_phone.py overlay-toggle       切换全黑半透明鼠标映射层
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
    python lock_phone.py status               查看连接 / 屏幕 / 亮度
    python lock_phone.py connect | discover
    python lock_phone.py pair IP:PORT CODE    首次配对无线调试
"""

import json
import os
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
    让 stdout/stderr 跟随控制台真实代码页。

    原因：Windows 中文控制台默认 cp936，若环境里设了 PYTHONUTF8=1 或
    PYTHONIOENCODING=utf-8，Python 会按 UTF-8 写字节，控制台按 GBK 解码，
    中文就会变成乱码。这里检测到真实控制台时，把编码对齐到它。
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
    except Exception:
        return

    targets = ((sys.stdout, kernel32.GetConsoleOutputCP),
               (sys.stderr, kernel32.GetConsoleOutputCP),
               (sys.stdin, kernel32.GetConsoleCP))

    for stream, get_cp in targets:
        if stream is None or not hasattr(stream, "reconfigure"):
            continue
        try:
            handle = msvcrt.get_osfhandle(stream.fileno())
            mode = ctypes.c_uint()
            if not kernel32.GetConsoleMode(ctypes.c_void_p(handle), ctypes.byref(mode)):
                continue  # 不是真实控制台
            buffer_name = type(getattr(stream, "buffer", None)).__name__
            if buffer_name == "_WindowsConsoleIO":
                encoding = "utf-8"  # 底层是 WriteConsoleW，只接受 UTF-8
            else:
                cp = int(get_cp() or 0)
                encoding = "utf-8" if cp == 65001 else ("cp%d" % cp if cp else "utf-8")
            stream.reconfigure(encoding=encoding, errors="replace")
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
        "swipe_duration": 160,
        "left_click": "both",
        "panel_port": 59731,
        "panel_poll_seconds": 8,
        "overlay_alpha": 0.55,
        "overlay_margin": 0.06,
        "overlay_pointer": True,
        "command_timeout": 12,
        "auto_discover": True,
        "allow_kill_server": True,
        "retry_cooldown": 4.0,
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


class Adb(object):
    def __init__(self, cfg):
        self.cfg = cfg
        self.adb = resolve_adb(cfg)
        self.target = (cfg.get("device") or "").strip()
        self._last_fail = 0.0
        self._last_fail_msg = ""

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

    def _shell(self, command, timeout=None):
        return self._exec(["-s", self.target, "shell"] + list(command), timeout=timeout)

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
        """
        now = time.time()
        cooldown = float(self.cfg.get("retry_cooldown", 4.0) or 0)
        if not force and self._last_fail and (now - self._last_fail) < cooldown:
            return False, self._last_fail_msg

        ok, message = self._ensure_impl()
        if ok:
            self._last_fail = 0.0
            self._last_fail_msg = ""
        else:
            self._last_fail = time.time()
            self._last_fail_msg = message
        return ok, message

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

    def display_size(self):
        """
        当前逻辑显示尺寸（随旋转变化）。

        这就是 input tap / input swipe 真正使用的坐标系：
        横屏时是 3200x1440，竖屏时是 1440x3200。
        """
        code, out = self._shell(["dumpsys", "window", "displays"], timeout=15)
        if code == 0 and out:
            found = re.search(r"\bcur=(\d+)x(\d+)", out)
            if found:
                return int(found.group(1)), int(found.group(2))
        return self.screen_size()

    def swipe_ratio(self, fx1, fy1, fx2, fy2, duration=160):
        ok, info = self.ready()
        if not ok:
            return False, info
        w, h = self.display_size()
        if not w or not h:
            w, h = 1440, 3200
        return self.swipe(int(w * fx1), int(h * fy1), int(w * fx2), int(h * fy2), duration)

    # -- 亮度 ------------------------------------------------------

    def get_brightness(self):
        """用户亮度设置值，0-255。"""
        code, out = self._shell(["settings", "get", "system", "screen_brightness"], timeout=10)
        if code != 0:
            return None
        match = re.search(r"(\d+)", out or "")
        return int(match.group(1)) if match else None

    def brightness_state(self):
        """
        返回 (设置值, 占用窗口级亮度覆盖的包名, PMS 缓存亮度)。

        注：Android 13+ 起 `settings get system screen_brightness` 仍是权威的用户亮度，
        但最终生效值还会被两层覆盖压过：
          1. 系统级覆盖 cmd display set-brightness
          2. 应用窗口级覆盖（抖音/B站播放器的亮度手势），优先级最高
        这里把窗口覆盖的包名读出来，便于解释「改了没反应」。
        """
        setting = self.get_brightness()
        override = None
        cached = None
        code, out = self._shell(["dumpsys", "display"], timeout=25)
        if code == 0 and out:
            found = re.search(r"mBrightnessReason=override\(([\w.]+)/", out)
            if found:
                override = found.group(1)
            cached_all = re.findall(r"mCachedBrightnessInfo\.brightness=([\d.eE+-]+)", out)
            if cached_all:
                try:
                    cached = float(cached_all[-1])
                except ValueError:
                    cached = None
        return setting, override, cached

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
        current = self.get_brightness()
        msg = "亮度 -> %s/255" % (current if current is not None else value)
        if self.cfg.get("warn_window_override", True):
            _, override, _ = self.brightness_state()
            if override:
                msg += "（%s 正占用窗口级亮度覆盖，可能看不出变化）" % override
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
        current = self.get_brightness()
        msg = "亮度%s" % ("调高" if direction > 0 else "调低")
        if current is not None:
            msg += "（当前 %s/255）" % current
        return True, msg

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

    def run_action(self, name, **kwargs):
        """执行动作，返回 (ok, message)。"""
        name = (name or "").strip().lower().replace("_", "-")

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
        if name == "bright-up":
            return self.adb.brightness_key(+1, self.cfg.get("brightness_key_repeat", 1))
        if name == "bright-down":
            return self.adb.brightness_key(-1, self.cfg.get("brightness_key_repeat", 1))
        if name == "bright":
            return self.adb.set_brightness(kwargs.get("value"))
        if name == "bright-get":
            setting, override, cached = self.adb.brightness_state()
            if setting is None:
                return False, "读不到亮度"
            msg = "亮度设置 %s/255" % setting
            if cached is not None:
                msg += " · 生效值 %.4f" % cached
            if override:
                msg += " · %s 占用窗口覆盖" % override
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
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("映射层（全黑半透明）", pystray.Menu(
                pystray.MenuItem("开启", lambda: self._fire("overlay-on", "映射层")),
                pystray.MenuItem("关闭", lambda: self._fire("overlay-off", "映射层")),
                pystray.MenuItem("切换", lambda: self._fire("overlay-toggle", "映射层")),
            )),
            pystray.MenuItem("音量", pystray.Menu(
                pystray.MenuItem("音量 +", lambda: self._fire("vol-up", "音量")),
                pystray.MenuItem("音量 -", lambda: self._fire("vol-down", "音量")),
                pystray.MenuItem("静音", lambda: self._fire("mute", "音量")),
            )),
            pystray.MenuItem("亮度", pystray.Menu(
                pystray.MenuItem("亮度 +", lambda: self._fire("bright-up", "亮度")),
                pystray.MenuItem("亮度 -", lambda: self._fire("bright-down", "亮度")),
                pystray.MenuItem("查看当前亮度", lambda: self._fire("bright-get", "亮度")),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("最暗 (10%)", lambda: self._fire("bright", "亮度", value=26)),
                pystray.MenuItem("中等 (50%)", lambda: self._fire("bright", "亮度", value=128)),
                pystray.MenuItem("最亮 (100%)", lambda: self._fire("bright", "亮度", value=255)),
            )),
            pystray.MenuItem("刷视频", pystray.Menu(
                pystray.MenuItem("下一个（上滑）", lambda: self._fire("next", "刷视频")),
                pystray.MenuItem("上一个（下滑）", lambda: self._fire("prev", "刷视频")),
                pystray.MenuItem("播放 / 暂停", lambda: self._fire("play-pause", "播放")),
            )),
            pystray.MenuItem("导航", pystray.Menu(
                pystray.MenuItem("返回", lambda: self._fire("back", "导航")),
                pystray.MenuItem("回到桌面", lambda: self._fire("home", "导航")),
                pystray.MenuItem("任务切换", lambda: self._fire("app-switch", "导航")),
            )),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("重新连接", lambda: self._fire("connect", "无线 adb")),
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
            "connect": "连接",
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

        self.start_hotkeys()
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
    ("8", "查看当前亮度", "bright-get"),
    ("9", "下一个视频（上滑）", "next"),
    ("a", "上一个视频（下滑）", "prev"),
    ("p", "播放 / 暂停", "play-pause"),
    ("b", "返回键", "back"),
    ("h", "回到桌面", "home"),
    ("n", "通知栏", "key:83"),
    ("o", "切换映射层（全黑半透明）", "overlay-toggle"),
    ("k", "打开控制面板", "panel"),
    ("s", "查看连接状态", "status"),
    ("c", "重新连接", "connect"),
]


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


def interactive_menu(ctl):
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
                    ok, info = ctl.adb.ensure()
                    say(("OK   " if ok else "FAIL ") + info)
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
        "bright-get": {}, "panel": {}, "overlay-on": {}, "overlay-off": {},
        "overlay-toggle": {},
    }

    if action in simple:
        ok, info = ctl.run_action(action)
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
        return interactive_menu(ctl)

    if action == "connect":
        ok, info = ctl.adb.ensure()
        say(("OK   " if ok else "FAIL ") + info)
        return 0 if ok else 1

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
        say("      status / connect / discover / pair")
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
