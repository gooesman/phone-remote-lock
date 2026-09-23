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
import re
import socket
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lock_phone import (  # noqa: E402
    Controller, default_config, load_config, log, save_config, tray_running,
)

try:
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtGui import QFont, QKeySequence
    from PySide6.QtWidgets import (
        QApplication, QComboBox, QDialog, QDialogButtonBox, QHBoxLayout,
        QHeaderView, QInputDialog, QKeySequenceEdit, QLabel, QLineEdit,
        QMessageBox, QPushButton, QTableWidget, QVBoxLayout, QWidget,
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
QPushButton#gear { min-height: 26px; max-width: 76px; padding: 4px 10px; }
QTableWidget { background: #1b1e23; border: 1px solid #34383f; border-radius: 6px; }
QTableWidget::item { padding: 4px; }
QComboBox, QKeySequenceEdit, QLineEdit { background: #23262c; border: 1px solid #34383f;
                                         border-radius: 5px; padding: 3px 6px; }
QHeaderView::section { background: #23262c; border: none; padding: 5px; color: #9aa0a8; }
"""

# 可绑定热键的功能（值要和 lock_phone.Controller.run_action 的动作名一致）
HOTKEY_ACTIONS = [
    ("lock", "锁定手机"),
    ("wake", "唤醒屏幕"),
    ("vol-up", "音量 +"),
    ("vol-down", "音量 -"),
    ("mute", "静音"),
    ("bright-up", "亮度 +"),
    ("bright-down", "亮度 -"),
    ("bright-get", "查看当前亮度"),
    ("next", "下一个视频"),
    ("prev", "上一个视频"),
    ("play-pause", "播放 / 暂停"),
    ("home", "回到桌面"),
    ("back", "返回键"),
    ("app-switch", "任务切换"),
    ("overlay-toggle", "切换映射层"),
    ("overlay-on", "开启映射层"),
    ("overlay-off", "关闭映射层"),
    ("panel", "打开控制面板"),
    ("connect", "重新连接手机"),
]

# Qt 的按键名 -> pynput 的写法
QT2PY = {
    "Ctrl": "<ctrl>", "Control": "<ctrl>", "Alt": "<alt>", "Shift": "<shift>",
    "Meta": "<cmd>", "Win": "<cmd>", "PgUp": "<page_up>", "PgDown": "<page_down>",
    "Space": "<space>", "Up": "<up>", "Down": "<down>", "Left": "<left>",
    "Right": "<right>", "Ins": "<insert>", "Insert": "<insert>", "Del": "<delete>",
    "Delete": "<delete>", "Home": "<home>", "End": "<end>", "Backspace": "<backspace>",
    "Return": "<enter>", "Enter": "<enter>", "Tab": "<tab>", "Esc": "<esc>",
    "Escape": "<esc>", "CapsLock": "<caps_lock>", "PrtSc": "<print_screen>",
    "Num+": "<num_add>", "Num-": "<num_subtract>", "Num*": "<num_multiply>",
    "Num/": "<num_divide>", "Num.": "<num_decimal>", "Num0": "<num_0>",
}
PY2QT = {value: key for key, value in QT2PY.items()}
PY2QT["<ctrl>"] = "Ctrl"


def qt_to_pynput(text):
    """把 Qt 的 "Ctrl+Alt+L" 转成 pynput 的 "<ctrl>+<alt>+l"。"""
    text = (text or "").strip()
    if not text:
        return ""
    parts = []
    for token in text.split("+"):
        token = token.strip()
        if not token:
            continue
        if token in QT2PY:
            parts.append(QT2PY[token])
        elif re.fullmatch(r"[Ff]\d{1,2}", token):
            parts.append("<%s>" % token.lower())
        elif len(token) == 1:
            parts.append(token.lower())
        else:
            parts.append("<%s>" % token.lower())
    return "+".join(parts)


def pynput_to_qt(hotkey):
    """反向转换，用于把已有配置显示回界面。"""
    hotkey = (hotkey or "").strip()
    if not hotkey:
        return ""
    parts = []
    for token in hotkey.split("+"):
        token = token.strip()
        if not token:
            continue
        if token.startswith("<") and token.endswith(">"):
            parts.append(PY2QT.get(token, token.strip("<>")))
        else:
            parts.append(token.upper())
    return "+".join(parts)


def has_modifier(hotkey):
    return any(tag in (hotkey or "") for tag in ("<ctrl>", "<alt>", "<shift>", "<cmd>"))


class SettingsDialog(QDialog):
    """设置窗口：给控制面板的功能绑定全局快捷键。"""

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle("设置 · 快捷键绑定")
        self.setMinimumWidth(560)

        root = QVBoxLayout(self)
        tip = QLabel("给下面的功能绑定全局快捷键（在哪都能按）。快捷键由托盘程序注册，"
                     "保存后约 2 秒自动生效，不用重启。")
        tip.setObjectName("dim")
        tip.setWordWrap(True)
        root.addWidget(tip)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["功能", "快捷键（点这一格后直接按键）", "操作"])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.Interactive)
        header.setSectionResizeMode(2, QHeaderView.Fixed)
        self.table.setColumnWidth(1, 210)
        self.table.setColumnWidth(2, 64)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.table.verticalHeader().setVisible(False)
        self.table.setMinimumHeight(300)
        root.addWidget(self.table)

        row = QHBoxLayout()
        add = QPushButton("添加绑定")
        add.clicked.connect(lambda: self._add_row())
        row.addWidget(add)
        reset = QPushButton("恢复默认")
        reset.clicked.connect(self._reset_default)
        row.addWidget(reset)
        row.addStretch(1)
        root.addLayout(row)

        self.hint = QLabel(self._tray_hint())
        self.hint.setObjectName("dim")
        self.hint.setWordWrap(True)
        root.addWidget(self.hint)

        note = QLabel("建议都带 Ctrl / Alt / Shift。单个字母这种没修饰键的会全局抢占键盘，"
                      "平时打字容易误触发。窗口开着时按 Esc 会关掉设置。")
        note.setObjectName("dim")
        note.setWordWrap(True)
        root.addWidget(note)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("保存")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        for hotkey, action in (cfg.get("actions") or {}).items():
            self._add_row(hotkey, action)
        if self.table.rowCount() == 0:
            self._add_row()

    # -- 行操作 ------------------------------------------------------

    def _add_row(self, hotkey="", action=""):
        row = self.table.rowCount()
        self.table.insertRow(row)

        combo = QComboBox()
        for value, label in HOTKEY_ACTIONS:
            combo.addItem(label, value)
        index = combo.findData(action)
        combo.setCurrentIndex(index if index >= 0 else 0)
        self.table.setCellWidget(row, 0, combo)

        editor = QKeySequenceEdit()
        if hotkey:
            editor.setKeySequence(QKeySequence(pynput_to_qt(hotkey)))
        self.table.setCellWidget(row, 1, editor)

        remove = QPushButton("删除")
        remove.clicked.connect(lambda _=False, btn=remove: self._remove_row(btn))
        self.table.setCellWidget(row, 2, remove)

    def _remove_row(self, button):
        for row in range(self.table.rowCount()):
            if self.table.cellWidget(row, 2) is button:
                self.table.removeRow(row)
                return

    def _reset_default(self):
        while self.table.rowCount():
            self.table.removeRow(0)
        for hotkey, action in default_config()["actions"].items():
            self._add_row(hotkey, action)

    def _tray_hint(self):
        if tray_running():
            return "托盘程序正在运行，保存后会立刻重载快捷键。"
        return ("⚠ 托盘程序没在运行：设置会保存进 config.json，但要等 start.bat 启动托盘后才生效。")

    # -- 保存 --------------------------------------------------------

    def _save(self):
        actions = {}
        for row in range(self.table.rowCount()):
            combo = self.table.cellWidget(row, 0)
            editor = self.table.cellWidget(row, 1)
            text = editor.keySequence().toString() if editor else ""
            if not text:
                continue  # 空着 = 不绑定
            hotkey = qt_to_pynput(text)
            action = combo.currentData()
            if not hotkey:
                QMessageBox.warning(self, "设置", "第 %d 行的快捷键没识别出来：%s" % (row + 1, text))
                return
            if hotkey in actions:
                QMessageBox.warning(self, "设置", "快捷键 %s 重复了（第 %d 行），请改掉一个。"
                                    % (text, row + 1))
                return
            if not has_modifier(hotkey) and not re.fullmatch(r"<f\d{1,2}>", hotkey):
                answer = QMessageBox.question(
                    self, "设置",
                    "%s 没有 Ctrl / Alt / Shift 修饰键，会在任何程序里抢占键盘。\n确定要用吗？"
                    % text,
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
                if answer != QMessageBox.Yes:
                    return
            actions[hotkey] = action

        old = self.cfg.get("actions") or {}
        if not actions and old:
            answer = QMessageBox.question(
                self, "设置", "一个快捷键都没留，等于关掉全部热键（托盘菜单仍可用）。确定吗？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                return

        self.cfg["actions"] = actions
        try:
            save_config(self.cfg)
        except Exception as exc:
            QMessageBox.critical(self, "设置", "写入 config.json 失败：%s" % exc)
            return
        self.saved_count = len(actions)
        log("[settings] 已保存 %s 个快捷键: %s" % (len(actions), sorted(actions.values())))
        self.accept()


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

        # 左上角：设置按钮
        top = QHBoxLayout()
        top.setSpacing(6)
        gear = QPushButton("⚙ 设置")
        gear.setObjectName("gear")
        gear.setToolTip("设置全局快捷键绑定")
        gear.clicked.connect(self._open_settings)
        top.addWidget(gear)
        top.addStretch(1)
        root.addLayout(top)

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

    def _open_settings(self):
        """左上角「设置」：改快捷键绑定，保存后托盘自动重载。"""
        dialog = SettingsDialog(self.host.cfg, self)
        if dialog.exec() == QDialog.Accepted:
            count = getattr(dialog, "saved_count", 0)
            if tray_running():
                self.set_status("已保存 %s 个快捷键，托盘正在重载（约 2 秒生效）" % count)
            else:
                self.set_status("已保存 %s 个快捷键。托盘未运行，需先启动 start.bat 才生效" % count)

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
