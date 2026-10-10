# -*- coding: utf-8 -*-
"""
covert.py —— 「摸鱼模式」：无窗口，用电脑鼠标偷偷操作手机

设计目标：**屏幕上什么都看不出来**（手机藏起来，电脑这边不弹任何窗口），
鼠标键盘照常放在电脑上，按住右键才开始控制手机：

    按住右键             → 接管手机（这时电脑鼠标事件被吞掉，不会误点你电脑上的东西）
    （按住时）移动鼠标    → 手机上的光标跟着走（等比映射整个屏幕）
    （按住时）左键短按    → 点按
    （按住时）左键拖动    → 滑动（刷视频 / 翻页）
    （按住时）左键按住不动 → 长按（短视频的倍速就是这么触发的）
    （按住时）滚轮        → 上一个 / 下一个视频（按当前模式）
    （按住时）中键        → 回到桌面
    右键快按一下（松开前） → 返回键
    松开右键             → 控制交回电脑（电脑鼠标秒回正常）

退出：托盘菜单里再点一次「摸鱼模式」，或热键 Ctrl+Alt+Q。
退出/松手时一定会释放所有按下状态，不会在手机上留下一个按着的手指。

用法：
    python covert.py            启动摸鱼模式（无窗口，静默）
    python covert.py --status   看它有没有在跑
    python covert.py --stop     让它退出

配置（config.json）：
    covert_move_rate    鼠标位置同步频率（默认 25/秒，越高越跟手、越费 adb）
    covert_suppress     接管期间是否吞掉电脑鼠标事件（默认 true）
    covert_wake_on_arm  接管时先唤醒手机屏幕（默认 true）
    covert_osd          是否显示一个小圆点（默认 false = 完全不显示）
"""

import argparse
import os
import queue
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lock_phone import Controller, load_config, log  # noqa: E402

try:
    from pynput import keyboard as pk
    from pynput import mouse as pm
except Exception as exc:  # pragma: no cover
    sys.stderr.write("需要 pynput：.venv\\Scripts\\python.exe -m pip install pynput\n%s\n" % exc)
    raise

LOCK_PORT = 59732          # 单实例锁（和面板 59731 错开）
DRAG_THRESHOLD = 12        # 超过这么多像素算"拖动"而不是"点按"
WHEEL_GAP = 0.18           # 滚轮最短间隔（秒）


class CovertMode(object):
    def __init__(self, cfg):
        self.cfg = cfg
        self.ctl = Controller(cfg)
        self.armed = False
        self.queue = queue.Queue()
        self._stop = False
        self.move_rate = max(5, min(60, int(cfg.get("covert_move_rate", 25) or 25)))
        self.suppress = bool(cfg.get("covert_suppress", True))
        self.wake_on_arm = bool(cfg.get("covert_wake_on_arm", True))
        self.long_press_ms = int(cfg.get("long_press_ms", 600) or 600)
        self.long_press_max_ms = int(cfg.get("long_press_max_ms", 4000) or 4000)
        self.press_xy = None       # 左键按下的位置
        self.press_t0 = 0.0
        self.moved = False
        self.holding = False
        self.right_t0 = 0.0
        self.last_move = 0.0
        self.last_wheel = 0.0
        self.size = (1440, 3200)
        self.screen = (1920, 1080)
        self.mouse = None
        self.listener = None      # 鼠标钩子（退出时要显式停，否则解释器关闭时可能段错误）
        self.hotkeys = None

    # -- 屏幕 → 手机 坐标 -----------------------------------------

    def refresh_size(self):
        try:
            size = self.ctl.adb.display_size(force=True)
            if size and size[0] and size[1]:
                self.size = size
        except Exception as exc:
            log("[covert] 读分辨率失败: %s" % exc)

    def to_phone(self, x, y):
        sw, sh = self.screen
        pw, ph = self.size
        px = int(max(0, min(1, x / float(sw))) * pw)
        py = int(max(0, min(1, y / float(sh))) * ph)
        return px, py

    # -- 后台执行（不阻塞鼠标回调）--------------------------------

    def _worker(self):
        while not self._stop:
            try:
                fn = self.queue.get(timeout=0.3)
            except queue.Empty:
                continue
            try:
                fn()
            except Exception as exc:
                log("[covert] 执行失败: %s" % exc)

    def _submit(self, fn):
        if self.queue.qsize() > 6:      # 鼠标移动很密，满了就丢，保证跟手
            return
        self.queue.put(fn)

    # -- 鼠标事件 -------------------------------------------------

    def _filter(self, msg, data):
        """返回 False = 吞掉这个事件（pynput 的语义）。接管期间吞掉，避免误点电脑。"""
        if not self.suppress:
            return True
        return not self.armed

    def on_move(self, x, y, *extra):
        if not self.armed:
            return
        now = time.time()
        if now - self.last_move < 1.0 / self.move_rate:
            return
        self.last_move = now
        px, py = self.to_phone(x, y)
        self._submit(lambda: self.ctl.adb.move_pointer(px, py))
        if self.press_xy and not self.moved:
            if abs(px - self.press_xy[0]) + abs(py - self.press_xy[1]) > DRAG_THRESHOLD * 4:
                self.moved = True

    def on_click(self, x, y, button, pressed, *extra):
        if button == pm.Button.right:
            if pressed:
                self.right_t0 = time.time()
                self._arm()
            else:
                held = (time.time() - self.right_t0) * 1000 if self.right_t0 else 0
                self.right_t0 = 0.0
                if self.armed and not self.holding:
                    if held < 250:
                        self._submit(lambda: self.ctl.run_action("back"))
                    self._disarm()
            return

        if not self.armed:
            return
        if button == pm.Button.left:
            if pressed:
                self.press_xy = self.to_phone(x, y)
                self.press_t0 = time.time()
                self.moved = False
            else:
                self._on_left_release(x, y)
        elif button == pm.Button.middle:
            self._submit(lambda: self.ctl.run_action("home"))

    def _on_left_release(self, x, y):
        if not self.press_xy:
            return
        start = self.press_xy
        self.press_xy = None
        if self.holding:                      # 长按结束
            self.holding = False
            self._submit(lambda: self.ctl.adb.hold_stop())
            return
        end = self.to_phone(x, y)
        dist = abs(end[0] - start[0]) + abs(end[1] - start[1])
        if not self.moved and dist < DRAG_THRESHOLD * 4:
            self._submit(lambda s=start: self.ctl.adb.tap(s[0], s[1]))
            return
        ms = int((time.time() - self.press_t0) * 1000)
        ms = max(120, min(800, ms))
        self._submit(lambda s=start, e=end, d=ms: self.ctl.adb.swipe(s[0], s[1], e[0], e[1], d))

    def on_scroll(self, x, y, dx, dy, *extra):
        if not self.armed:
            return
        now = time.time()
        if now - self.last_wheel < WHEEL_GAP:
            return
        self.last_wheel = now
        action = "next" if dy < 0 else "prev"
        self._submit(lambda a=action: self.ctl.run_action(a))

    # -- 长按检测（左键按住不动）----------------------------------

    def _hold_watch(self):
        while not self._stop:
            time.sleep(0.05)
            if (self.armed and self.press_xy and not self.moved and not self.holding
                    and (time.time() - self.press_t0) * 1000 >= self.long_press_ms):
                self.holding = True
                px, py = self.press_xy
                self._submit(lambda p=px, q=py: self.ctl.adb.hold_start(
                    p / float(self.size[0]), q / float(self.size[1])))

    # -- 接管 / 交回 ----------------------------------------------

    def _arm(self):
        self.armed = True
        log("[covert] 接管鼠标（按住右键中）")
        if self.wake_on_arm:
            self._submit(lambda: self.ctl.adb.send_keyevent(self.cfg.get("wake_keyevent", 224)))
        self._submit(lambda: self.ctl.adb.ensure())

    def _disarm(self):
        self.armed = False
        self.moved = False
        log("[covert] 交回鼠标")
        try:
            if self.holding or self.ctl.adb.is_holding():
                self.holding = False
                self.ctl.adb.hold_stop()
        except Exception:
            pass

    # -- 启动 / 停止 ----------------------------------------------

    def run(self):
        self.screen = _screen_size()
        self.refresh_size()
        log("[covert] 屏幕 %sx%s → 手机 %sx%s，同步 %d/秒"
            % (self.screen[0], self.screen[1], self.size[0], self.size[1], self.move_rate))

        threading.Thread(target=self._worker, daemon=True).start()
        threading.Thread(target=self._hold_watch, daemon=True).start()

        self.hotkeys = pk.GlobalHotKeys({"<ctrl>+<alt>+q": self.stop})
        self.hotkeys.daemon = True
        self.hotkeys.start()

        self.listener = pm.Listener(on_move=self.on_move, on_click=self.on_click,
                                    on_scroll=self.on_scroll, event_filter=self._filter,
                                    suppress=self.suppress)
        self.listener.daemon = True
        self.listener.start()
        log("[covert] 摸鱼模式已启动（按住右键控制手机，Ctrl+Alt+Q 退出）")
        _notify(self.cfg, "摸鱼模式已启动：按住右键控制手机，Ctrl+Alt+Q 退出")
        while not self._stop:
            time.sleep(0.2)
        self.cleanup()

    def stop(self):
        self._stop = True

    def cleanup(self):
        # 顺序很重要：先松开手机上可能按住的手指 → 再停钩子 → 最后关通道
        try:
            if self.holding or self.ctl.adb.is_holding():
                self.ctl.adb.hold_stop()
            else:
                self.ctl.adb.press_up()
        except Exception:
            pass
        for hook in (self.listener, self.hotkeys):
            if hook is None:
                continue
            try:
                hook.stop()
            except Exception:
                pass
        time.sleep(0.2)
        try:
            self.ctl.adb.close_channel("摸鱼模式退出")
        except Exception:
            pass
        log("[covert] 已退出")


def _screen_size():
    """主屏尺寸：优先用 Qt（免依赖），失败给个保守值。"""
    try:
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        geo = app.primaryScreen().geometry()
        return geo.width(), geo.height()
    except Exception:
        try:
            import ctypes
            user32 = ctypes.windll.user32
            user32.SetProcessDPIAware()
            return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
        except Exception:
            return 1920, 1080


def _notify(cfg, message):
    if not cfg.get("notify", True):
        return
    try:
        from lock_phone import log
        log("[covert] %s" % message)
    except Exception:
        pass


def already_running():
    s = socket.socket()
    s.settimeout(0.6)
    try:
        s.connect(("127.0.0.1", LOCK_PORT))
        return True
    except Exception:
        return False
    finally:
        s.close()


def main():
    parser = argparse.ArgumentParser(description="摸鱼模式：无窗口用电脑鼠标操作手机")
    parser.add_argument("--status", action="store_true", help="看有没有在跑")
    parser.add_argument("--stop", action="store_true", help="让它退出")
    args = parser.parse_args()

    if args.status:
        print("摸鱼模式：%s" % ("正在运行" if already_running() else "没在跑"))
        return 0

    if args.stop:
        try:
            with socket.create_connection(("127.0.0.1", LOCK_PORT), timeout=1.0) as sock:
                sock.sendall(b"quit")
            print("已通知摸鱼模式退出")
        except Exception as exc:
            print("它好像没在跑（%s）" % exc)
        return 0

    if already_running():
        print("摸鱼模式已经在运行了（屏幕上看不到任何窗口是正常的）")
        return 0

    cfg = load_config()
    server = socket.socket()
    try:
        server.bind(("127.0.0.1", LOCK_PORT))
        server.listen(4)
        server.settimeout(0.5)
    except OSError:
        print("端口被占用，可能已经在跑了")
        return 1

    mode = CovertMode(cfg)

    def control_loop():
        while not mode._stop:
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                conn.settimeout(1.0)
                if conn.recv(16).strip().lower().startswith(b"quit"):
                    log("[covert] 收到退出指令")
                    mode.stop()
            except Exception:
                pass
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        try:
            server.close()
        except Exception:
            pass

    threading.Thread(target=control_loop, daemon=True).start()
    code = 0
    try:
        mode.run()
    except Exception as exc:
        log("[covert] 运行异常: %s" % exc)
        code = 1
    # pynput 的钩子线程在解释器正常退出时可能触发段错误，这里硬退出绕过
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None:      # pythonw 下这两个是 None
                stream.flush()
        except Exception:
            pass
    os._exit(code)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as exc:
        log("[covert] 异常退出: %s" % exc)
        raise
