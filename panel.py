# -*- coding: utf-8 -*-
"""
panel.py —— 控制面板 + 鼠标映射层宿主（Qt）

一个进程同时管两样东西：
    1. 控制面板窗口（按钮：亮度 / 音量 / 锁屏 / 刷视频 / 导航）
    2. 鼠标映射层（全屏黑色半透明，可随时开关）

单实例：用 127.0.0.1:<panel_port> 作为锁。
已有实例在跑时，新进程会把命令转发过去然后退出。

用法：
    python panel.py                     显示控制面板
    python panel.py --cmd panel         显示控制面板
    python panel.py --cmd overlay-on    开映射层（顺带显示面板）
    python panel.py --cmd overlay-off   关映射层，只留面板
    python panel.py --cmd overlay-toggle
    python panel.py --cmd hide          只开映射层，不显示面板
    python panel.py --cmd quit          退出面板进程

一般不用手动跑，由托盘图标左键或右键菜单唤起。
"""

import os
import queue
import socket
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lock_phone import Controller, load_config, log  # noqa: E402

try:
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import (
        QApplication, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QPushButton,
        QVBoxLayout, QWidget,
    )
except ImportError as exc:  # pragma: no cover
    sys.stderr.write("缺少 PySide6：.venv\\Scripts\\python.exe -m pip install PySide6\n%s\n" % exc)
    raise

import overlay as overlay_mod  # noqa: E402

SECTIONS = [
    ("亮度", [
        ("减", "bright-down", {}),
        ("加", "bright-up", {}),
        ("10%", "bright", {"value": 26}),
        ("50%", "bright", {"value": 128}),
        ("100%", "bright", {"value": 255}),
    ]),
    ("音量", [
        ("减小", "vol-down", {}),
        ("增大", "vol-up", {}),
        ("静音", "mute", {}),
    ]),
    ("播放 / 刷视频", [
        ("上一个", "prev", {}),
        ("播放暂停", "play-pause", {}),
        ("下一个", "next", {}),
    ]),
    ("屏幕", [
        ("锁屏", "lock", {}),
        ("唤醒", "wake", {}),
        ("通知栏", "key", {"keycode": 83}),
    ]),
    ("导航", [
        ("返回", "back", {}),
        ("桌面", "home", {}),
        ("任务切换", "app-switch", {}),
    ]),
]

STYLE = """
QWidget { background: #16181c; color: #e6e6e6; font-family: "Microsoft YaHei UI"; font-size: 12px; }
QLabel#title { font-size: 15px; font-weight: 500; color: #f0f0f0; }
QLabel#dim { color: #8b8f96; font-size: 11px; }
QLabel#status { color: #c3c8d0; font-size: 11px; }
QLabel#sect { color: #7f858e; font-size: 11px; }
QPushButton { background: #23262c; border: 1px solid #34383f; border-radius: 6px;
              padding: 6px 4px; min-height: 24px; }
QPushButton:hover { background: #2d3138; }
QPushButton:pressed { background: #3b4049; }
QPushButton:checked { background: #2f6b4f; border-color: #47936c; color: #eafff4; }
QPushButton#wide { min-height: 30px; }
"""


def send_command(port, command, timeout=1.5):
    """向已运行的面板进程发命令；成功返回 True。"""
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout) as sock:
            sock.sendall(str(command).encode("utf-8"))
            sock.settimeout(timeout)
            sock.recv(64)
        return True
    except Exception:
        return False


GUI_COMMANDS = ("overlay-on", "overlay-off", "overlay-toggle",
                "show", "panel", "hide", "quit")

GUI_DIRECT = {"__overlay-on__", "__overlay-off__", "__overlay-toggle__",
              "__quit__", "__show__", "__hide__", "__panel__"}


def normalize(name):
    """把 UI 命令统一成 __x__ 形式，其它原样返回。"""
    name = (name or "").strip().lower()
    if name in GUI_COMMANDS:
        return "__%s__" % name
    return name


class CommandServer(threading.Thread):
    """极简文本协议：收一条命令，交给 handler，回 OK。"""

    def __init__(self, port, handler):
        super().__init__(daemon=True)
        self.port = int(port)
        self.handler = handler
        self.ready = threading.Event()
        self.bound = False
        self._stop = False

    def run(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            server.bind(("127.0.0.1", self.port))
        except OSError:
            self.bound = False
            self.ready.set()
            return
        server.listen(8)
        server.settimeout(0.4)
        self.bound = True
        self.ready.set()
        while not self._stop:
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                conn.settimeout(2.0)
                data = conn.recv(512).decode("utf-8", "replace").strip()
                if data:
                    self.handler(data)
                conn.sendall(b"OK\n")
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

    def stop(self):
        self._stop = True


class ControlPanel(QWidget):
    def __init__(self, host):
        super().__init__()
        self.host = host
        self.setWindowTitle("手机遥控面板")
        self.setMinimumWidth(380)
        self.setStyleSheet(STYLE)
        self._build()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(8)

        title = QLabel("手机遥控")
        title.setObjectName("title")
        root.addWidget(title)

        self.device_label = QLabel("正在连接…")
        self.device_label.setObjectName("dim")
        root.addWidget(self.device_label)

        self.bright_label = QLabel("亮度：读取中…")
        self.bright_label.setObjectName("dim")
        root.addWidget(self.bright_label)

        for name, items in SECTIONS:
            caption = QLabel(name)
            caption.setObjectName("sect")
            root.addWidget(caption)
            row = QHBoxLayout()
            row.setSpacing(6)
            for label, action, kwargs in items:
                button = QPushButton(label)
                button.clicked.connect(
                    lambda _=False, a=action, k=kwargs: self.host.fire(a, **k))
                row.addWidget(button)
            root.addLayout(row)

        caption = QLabel("映射层")
        caption.setObjectName("sect")
        root.addWidget(caption)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.overlay_button = QPushButton("开启映射层")
        self.overlay_button.setObjectName("wide")
        self.overlay_button.setCheckable(True)
        self.overlay_button.clicked.connect(self.host.toggle_overlay)
        row.addWidget(self.overlay_button)

        refresh = QPushButton("刷新状态")
        refresh.setObjectName("wide")
        refresh.clicked.connect(lambda: self.host.fire("__state__"))
        row.addWidget(refresh)

        reconnect = QPushButton("重新连接")
        reconnect.setObjectName("wide")
        reconnect.clicked.connect(lambda: self.host.fire("connect"))
        row.addWidget(reconnect)

        manual = QPushButton("输入地址连接")
        manual.setObjectName("wide")
        manual.clicked.connect(self._ask_addr)
        row.addWidget(manual)
        root.addLayout(row)

        self.status_label = QLabel("就绪")
        self.status_label.setObjectName("status")
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)

        hint = QLabel("按钮点击即作用于手机。Esc 关闭映射层；关掉本窗口只是隐藏，托盘图标可再唤起。")
        hint.setObjectName("dim")
        hint.setWordWrap(True)
        root.addWidget(hint)

    def _ask_addr(self):
        """弹出输入框，手动填手机 IP:端口 后连接。"""
        saved = (self.host.cfg.get("device") or "").strip()
        text, ok = QInputDialog.getText(
            self, "连接手机",
            "手机 IP:端口（手机上「无线调试」页面显示的那个，例如 192.168.1.23:42007）：",
            QLineEdit.Normal, saved)
        text = (text or "").strip()
        if ok and text:
            self.set_status("正在连接 %s …" % text)
            self.host.fire("connect-addr", addr=text)

    def set_status(self, text):
        self.status_label.setText(text)

    def set_device(self, device, online):
        self.device_label.setText("设备：%s    %s" % (device or "未连接", "已连接" if online else "未连接"))

    def set_brightness(self, setting, override):
        if setting is None:
            self.bright_label.setText("亮度：读取失败")
            return
        text = "亮度：%s/255" % setting
        if override:
            text += "   ⚠ %s 占用窗口亮度覆盖" % override
        self.bright_label.setText(text)

    def set_overlay_state(self, on):
        self.overlay_button.setChecked(bool(on))
        self.overlay_button.setText("关闭映射层" if on else "开启映射层")

    def closeEvent(self, event):
        event.ignore()
        self.hide()


class PanelHost(object):
    def __init__(self):
        self.cfg = load_config()
        self.ctl = Controller(self.cfg)
        self.queue = queue.Queue()
        self.gui_queue = queue.Queue()
        self.result_box = [None]
        self.overlay = None
        self.is_primary = False
        self.server = None

        self.app = QApplication(sys.argv)
        self.app.setFont(QFont("Microsoft YaHei UI", 9))
        self.window = ControlPanel(self)

        self.timer = QTimer()
        self.timer.timeout.connect(self._poll)
        self.timer.start(80)

        self.poll_timer = QTimer()
        self.poll_timer.timeout.connect(self._periodic)
        seconds = float(self.cfg.get("panel_poll_seconds", 8) or 0)
        if seconds > 0:
            self.poll_timer.start(max(2000, int(seconds * 1000)))

        threading.Thread(target=self._worker, daemon=True).start()
        self._start_server()

    # -- 单实例锁 --------------------------------------------------

    def _start_server(self):
        port = int(self.cfg.get("panel_port", 59731))
        self.server = CommandServer(port, self._dispatch)
        self.server.start()
        self.server.ready.wait(3.0)
        self.is_primary = bool(self.server.bound)

    def _dispatch(self, command):
        """网络线程调用，只投递，不碰 Qt。"""
        self._submit(command, {})

    def fire(self, action, **kwargs):
        self._submit(action, kwargs)

    def _submit(self, action, kwargs):
        """
        界面类命令直接进 gui_queue。

        不能都丢给 worker：手机离线时 ensure() 会跑多轮重试，
        排在后面的开/关映射层会被堵十几秒。
        """
        name = normalize(action)
        if name in GUI_DIRECT:
            self.gui_queue.put((name, kwargs))
        else:
            self.queue.put((name, kwargs))

    def toggle_overlay(self):
        self.fire("__overlay-toggle__")

    # -- Qt 操作（只在主线程执行） --------------------------------

    def _do_overlay_on(self):
        if self.overlay is None:
            try:
                self.overlay = overlay_mod.Overlay(self.cfg)
            except Exception as exc:
                log("[panel] 创建映射层失败: %s" % exc)
                return False, "创建映射层失败：%s" % exc
        try:
            self.overlay.showFullScreen()
            self.overlay.raise_()
            self.overlay.activateWindow()
            self.overlay.setFocus()
        except Exception as exc:
            log("[panel] 显示映射层失败: %s" % exc)
            return False, "显示映射层失败：%s" % exc
        self.window.set_overlay_state(True)
        self.window.raise_()
        self.window.activateWindow()
        log("[panel] 映射层已开启 %sx%s" % (self.overlay.pw, self.overlay.ph))
        return True, "映射层已开启（按 Esc 关闭）"

    def _do_overlay_off(self):
        try:
            if self.overlay is not None:
                self.overlay.hide()
        except Exception as exc:
            log("[panel] 关闭映射层失败: %s" % exc)
        self.window.set_overlay_state(False)
        log("[panel] 映射层已关闭")
        return True, "映射层已关闭"

    def _do_overlay_toggle(self):
        if self.overlay is not None and self.overlay.isVisible():
            return self._do_overlay_off()
        return self._do_overlay_on()

    # -- 主线程轮询 ------------------------------------------------

    def _poll(self):
        while True:
            try:
                action, kwargs = self.gui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                if action == "__show__" or action == "__panel__":
                    self.window.show()
                    self.window.raise_()
                    self.window.activateWindow()
                    self.result_box[0] = (True, "面板已就绪", None)
                elif action == "__hide__":
                    self.result_box[0] = self._do_overlay_on() + (None,)
                elif action == "__overlay-on__":
                    self.result_box[0] = self._do_overlay_on() + (None,)
                elif action == "__overlay-off__":
                    self.result_box[0] = self._do_overlay_off() + (None,)
                elif action == "__overlay-toggle__":
                    self.result_box[0] = self._do_overlay_toggle() + (None,)
                elif action == "__quit__":
                    self.app.quit()
                    return
            except Exception as exc:
                log("[panel] 界面命令 %s 异常: %s" % (action, exc))
                self.result_box[0] = (False, "界面命令异常：%s" % exc, None)

        item = self.result_box[0]
        if item is None:
            return
        self.result_box[0] = None
        ok, text, info = item
        self.window.set_status(("✓ " if ok else "✗ ") + text)
        if info:
            self.window.set_device(info.get("device"), info.get("online"))
            self.window.set_brightness(info.get("brightness"), info.get("override"))

    def _periodic(self):
        if self.window.isVisible():
            self.queue.put(("__state__", {}))

    # -- 工作线程 --------------------------------------------------

    def _state_info(self):
        ok, _info = self.ctl.adb.ensure()
        setting = override = None
        if ok:
            setting, override, _cached = self.ctl.adb.brightness_state()
        return ok, setting, override

    def _worker(self):
        while True:
            action, kwargs = self.queue.get()

            if action in ("__overlay-on__", "__overlay-off__", "__overlay-toggle__",
                          "__quit__", "__show__", "__hide__", "__panel__"):
                self.gui_queue.put((action, kwargs))
                continue

            if action == "ping":
                self.result_box[0] = (True, "面板已就绪", None)
                continue

            if action == "__state__":
                try:
                    ok, setting, override = self._state_info()
                except Exception as exc:
                    ok, setting, override = False, None, None
                    log("[panel] 状态读取失败: %s" % exc)
                text = "亮度 %s/255" % setting if setting is not None else "亮度读取失败"
                if override:
                    text += "，且 %s 占用窗口亮度覆盖" % override
                self.result_box[0] = (ok, text, {
                    "device": self.ctl.adb.target,
                    "online": ok,
                    "brightness": setting,
                    "override": override,
                })
                continue

            if action == "show":
                self.result_box[0] = (True, "面板已就绪", None)
                continue

            if action == "connect-addr":
                addr = (kwargs.get("addr") or "").strip()
                try:
                    ok, text = self.ctl.adb.connect_addr(addr)
                except Exception as exc:
                    ok, text = False, "异常: %s" % exc
                log("[panel] connect-addr %s: %s %s" % (addr, "OK" if ok else "FAIL", text))
                self.result_box[0] = (ok, text, None)
                continue

            try:
                ok, text = self.ctl.run_action(action, **kwargs)
            except Exception as exc:
                ok, text = False, "异常: %s" % exc
            log("[panel] %s: %s %s" % (action, "OK" if ok else "FAIL", text))

            info = None
            if action in ("bright-up", "bright-down", "bright", "connect", "connect-addr"):
                try:
                    ok2, setting, override = self._state_info()
                    if ok2:
                        info = {
                            "device": self.ctl.adb.target,
                            "online": True,
                            "brightness": setting,
                            "override": override,
                        }
                except Exception:
                    info = None
            self.result_box[0] = (ok, text, info)

    # -- 启动 ------------------------------------------------------

    def run(self, command):
        if command == "quit":
            return 0

        show_panel = command != "hide"
        overlay_on = command in ("overlay-on", "overlay-toggle", "hide")
        overlay_off = command == "overlay-off"

        if show_panel:
            self.window.show()
            self.window.raise_()
            self.window.activateWindow()
        else:
            self.window.hide()

        self.fire("connect")
        if overlay_on:
            self.fire("overlay-on")
        elif overlay_off:
            self.fire("overlay-off")

        return self.app.exec()


def main():
    argv = sys.argv[1:]
    command = None
    if "--cmd" in argv:
        index = argv.index("--cmd")
        if len(argv) > index + 1:
            command = argv[index + 1]

    cfg = load_config()
    port = int(cfg.get("panel_port", 59731))

    if command is not None and send_command(port, command):
        log("[panel] 命令 %s 已转发给运行中的实例" % command)
        return 0

    host = PanelHost()
    if not host.is_primary:
        if command is not None and send_command(port, command):
            log("[panel] 端口被占用，命令已转发")
        return 0

    log("[panel] 面板进程启动，port=%s cmd=%s" % (port, command))
    return host.run(command)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        log("[panel] 异常退出: %s" % exc)
        raise
