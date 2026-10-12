# -*- coding: utf-8 -*-
"""
net_pointer.py —— 电脑侧发送端：把鼠标操作发给手机上的「手机光标」App

和 android/ 里的 App 是一对（协议见 android/DESIGN.md）：
    电脑（这里）──UDP──► 手机（CursorService）
                          ├─ 无障碍服务：执行点按 / 滑动 / 长按 / 滚轮
                          └─ 悬浮窗：画出光标

为什么需要它
------------
adb 注入的触摸/鼠标事件**永远不会有光标**（HyperOS 只对物理手指和真鼠标设备可视化）。
要让手机屏幕上出现一个可见的光标并接受点击，手机端就得自己画 + 自己做手势 ——
也就是 android/ 那个 App。电脑这边只是把坐标和按键发过去。

方法名刻意和 `Adb` 对齐（tap / swipe / move_pointer / press_down / hold_start …），
这样映射层换个后端就继续用，操作习惯完全不变。
"""

import socket
import struct
import time

from lock_phone import log

MOVE = 0x01
TAP = 0x02
SWIPE = 0x03
DOWN = 0x04
UP = 0x05
SCROLL = 0x06
KEY = 0x07
CURSOR = 0x08
STATUS = 0x09

DEFAULT_PORT = 4321
LONG_PRESS_MS = 600


def _u16(value):
    value = int(max(0, min(65535, value)))
    return struct.pack("<H", value)


def _s16(value):
    value = int(max(-32768, min(32767, value)))
    return struct.pack("<h", value)


class NetPointer(object):
    """通过 UDP 把指针操作发给手机（接口与 Adb 同名，便于替换后端）。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.host = (cfg.get("net_host") or "").strip()
        self._cx = int((cfg.get("last_display_size") or [1080, 2400])[0]) // 2
        self._cy = int((cfg.get("last_display_size") or [1080, 2400])[1]) // 2
        self.port = int(cfg.get("net_port") or DEFAULT_PORT)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sent = 0
        self.last_error = ""
        self._holding = False

    # -- 基础 ------------------------------------------------------

    def addr(self):
        if not self.host:
            self.last_error = "没填 net_host（手机 IP）"
            return None
        return (self.host, self.port)

    def send(self, payload):
        target = self.addr()
        if target is None:
            return False
        try:
            self.sock.sendto(bytes(payload), target)
            self.sent += 1
            return True
        except Exception as exc:
            self.last_error = str(exc)
            log("[net] 发送失败: %s" % exc)
            return False

    def status(self, timeout=1.5):
        """问手机一句"你在吗"，顺便拿屏幕宽高和当前光标位置。"""
        target = self.addr()
        if target is None:
            return False, self.last_error
        try:
            self.sock.settimeout(timeout)
            self.sock.sendto(bytes([STATUS]), target)
            data, _ = self.sock.recvfrom(64)
            if len(data) >= 12 and data[0:2] == b"S1":
                w, h, cx, cy = struct.unpack("<HHHH", data[2:10])
                pressed = struct.unpack("<H", data[10:12])[0] == 1
                return True, "在线：屏幕 %dx%d，光标 (%d,%d)%s" % (
                    w, h, cx, cy, "，按着" if pressed else "")
        except socket.timeout:
            return False, ("手机没应答（IP/端口对吗？App 的无障碍服务开了吗？）")
        except Exception as exc:
            return False, "出错：%s" % exc
        return False, "回包格式不认识"

    # -- 与 Adb 对齐的接口 ------------------------------------------

    def display_size(self, force=False):
        size = self.cfg.get("last_display_size") or [1080, 2400]
        return int(size[0]), int(size[1])

    def invalidate_size(self):
        pass

    def move_pointer(self, x, y):
        # 记住位置：DOWN 指令要带上坐标（手机那边要先把光标移过去）
        self._cx, self._cy = int(x), int(y)
        return self.send(bytes([MOVE]) + _u16(x) + _u16(y))

    def tap(self, x, y, hold_ms=0):
        return self.send(bytes([TAP]) + _u16(x) + _u16(y) + _u16(hold_ms))

    def tap_ratio(self, fx=0.5, fy=0.5):
        w, h = self.display_size()
        return self.tap(int(w * fx), int(h * fy))

    def double_tap_ratio(self, fx=0.5, fy=0.5, gap=0.09):
        w, h = self.display_size()
        x, y = int(w * fx), int(h * fy)
        self.send(bytes([TAP]) + _u16(x) + _u16(y) + _u16(0))
        time.sleep(gap)
        return self.send(bytes([TAP]) + _u16(x) + _u16(y) + _u16(0))

    def long_press_ratio(self, fx=0.5, fy=0.5, ms=1200):
        w, h = self.display_size()
        return self.tap(int(w * fx), int(h * fy), hold_ms=int(ms))

    def swipe(self, x1, y1, x2, y2, duration=180):
        return self.send(bytes([SWIPE]) + _u16(x1) + _u16(y1) + _u16(x2) + _u16(y2)
                          + _u16(max(80, duration)))

    def swipe_ratio(self, x1, y1, x2, y2, duration=180):
        w, h = self.display_size()
        return self.swipe(int(w * x1), int(h * y1), int(w * x2), int(h * y2), duration)

    def press_down(self, x=None, y=None):
        if x is not None:
            self.move_pointer(int(x), int(y))
        self._holding = True
        return self.send(bytes([DOWN]) + _u16(self._x()) + _u16(self._y()))

    def press_up(self, x=None, y=None):
        self._holding = False
        return self.send(bytes([UP]))

    def is_holding(self):
        return self._holding

    def hold_start(self, fx=0.5, fy=0.5):
        """倍速用的"按住不放"。"""
        w, h = self.display_size()
        x, y = int(w * fx), int(h * fy)
        self.move_pointer(x, y)
        self._holding = True
        return self.send(bytes([DOWN]) + _u16(x) + _u16(y))

    def hold_stop(self):
        self._holding = False
        return self.send(bytes([UP]))

    def scroll(self, x=None, y=None, amount=3):
        w, h = self.display_size()
        px = int(w / 2) if x is None else int(x)
        py = int(h / 2) if y is None else int(y)
        return self.send(bytes([SCROLL]) + _u16(px) + _u16(py) + _s16(amount))

    def key(self, code):
        return self.send(bytes([KEY]) + _s16(code))

    def show_cursor(self, on=True):
        return self.send(bytes([CURSOR, 1 if on else 0]))

    # 便捷：无 adb 时也能给按键
    def send_keyevent(self, key):
        return self.key(key)

    # 内部：记住上次光标位置，DOWN 时要用
    def _x(self):
        return getattr(self, "_cx", self.display_size()[0] // 2)

    def _y(self):
        return getattr(self, "_cy", self.display_size()[1] // 2)

    def close_channel(self, reason=""):
        try:
            self.sock.close()
        except Exception:
            pass