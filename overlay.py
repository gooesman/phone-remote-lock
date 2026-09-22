# -*- coding: utf-8 -*-
"""
overlay.py —— 全黑半透明「鼠标映射层」（Qt 版）

把电脑鼠标映射成手机的触摸操作，不用低头看手机也能操作：

    左键单击          -> tap（点按）
    左键拖动          -> swipe（滑动，刷视频 / 翻页）
    滚轮向下 / 向上    -> 下一个 / 上一个视频
    右键              -> 返回键
    中键              -> 回到桌面
    ↑ / ↓             -> 音量 + / -
    ← / →             -> 亮度 + / -
    空格              -> 播放 / 暂停
    L / W             -> 锁屏 / 唤醒
    N                 -> 通知栏
    S                 -> 刷新状态（含亮度窗口覆盖提示）
    R                 -> 重新读取分辨率（手机转过屏后用）
    Esc               -> 退出

直接运行：python overlay.py      或双击 overlay.bat

窗口整体是「黑色 + 可调不透明度」（config.json 的 overlay_alpha），
中间那块竖向矩形是手机屏幕的等比例映射区，鼠标只有落在里面才会作用到手机。
"""

import os
import queue
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lock_phone import Controller, load_config, log  # noqa: E402

try:
    from PySide6.QtCore import QPoint, QRectF, Qt, QTimer
    from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPixmap
    from PySide6.QtWidgets import QApplication, QWidget
except ImportError as exc:  # pragma: no cover
    sys.stderr.write(
        "缺少 PySide6，请先运行 setup.bat，或执行：\n"
        "  .venv\\Scripts\\python.exe -m pip install PySide6\n"
        "原始错误: %s\n" % exc
    )
    raise

C_FRAME = QColor(122, 122, 122)
C_GUIDE = QColor(58, 58, 58)
C_CURSOR = QColor(158, 203, 255)
C_TEXT = QColor(232, 232, 232)
C_DIM = QColor(144, 144, 144)
C_OK = QColor(141, 224, 160)
C_BAD = QColor(255, 143, 143)

HINTS = [
    "左键单击 = 点按      左键拖动 = 滑动      滚轮 = 上/下一个视频",
    "右键 = 返回     中键 = 桌面     ↑↓ = 音量     ←→ = 亮度     空格 = 播放/暂停",
    "L = 锁屏    W = 唤醒    N = 通知栏    S = 状态    R = 重读分辨率    Esc = 退出",
]


class Overlay(QWidget):
    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.ctl = Controller(cfg)

        self.pw = 1440
        self.ph = 3200
        self.fx0 = self.fy0 = self.fx1 = self.fy1 = 0

        self.queue = queue.Queue()
        self.result_box = [None]
        self.need_layout = False
        self.connected = False

        self.drag_from = None
        self.drag_t0 = 0.0
        self.last_wheel = 0.0
        self.status_text = "正在连接手机…"
        self.status_ok = None
        self.mouse_pos = None

        self._bg = None

        self.setWindowTitle("mouse mapping overlay")
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        try:
            self.setWindowOpacity(float(cfg.get("overlay_alpha", 0.55)))
        except Exception:
            pass

        self._relayout()

        threading.Thread(target=self._worker, daemon=True).start()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll)
        self.timer.start(80)

        self._enqueue("connect")
        self._refresh_size_async()

    # ---------------------------------------------------------- 尺寸

    def _read_size(self):
        try:
            size = self.ctl.adb.display_size()
        except Exception:
            size = None
        if size and size[0] and size[1]:
            self.pw, self.ph = size

    def _refresh_size_async(self):
        """
        异步刷新分辨率。

        构造函数里绝不能同步读 adb：手机离线时一次 adb 调用可能卡十几秒，
        而这里是主线程，会把整个界面冻住（也会拖住后面的问卷命令）。
        """
        def runner():
            try:
                self._read_size()
                self.need_layout = True
            except Exception as exc:
                log("[overlay] 读取分辨率失败: %s" % exc)
        threading.Thread(target=runner, daemon=True).start()

    def _relayout(self):
        w = max(self.width(), 100)
        h = max(self.height(), 100)
        margin = float(self.cfg.get("overlay_margin", 0.06))
        fh = int(h * (1 - 2 * margin))
        fw = int(fh * self.pw / float(self.ph))
        max_w = int(w * 0.92)
        if fw > max_w:
            fw = max_w
            fh = int(fw * self.ph / float(self.pw))
        self.fx0 = (w - fw) // 2
        self.fy0 = (h - fh) // 2
        self.fx1 = self.fx0 + fw
        self.fy1 = self.fy0 + fh
        self._bg = None
        self.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._relayout()

    # ---------------------------------------------------------- 坐标映射

    def to_phone(self, x, y):
        if not (self.fx0 <= x <= self.fx1 and self.fy0 <= y <= self.fy1):
            return None
        fw = float(self.fx1 - self.fx0)
        fh = float(self.fy1 - self.fy0)
        px = int((x - self.fx0) / fw * self.pw)
        py = int((y - self.fy0) / fh * self.ph)
        return (max(0, min(self.pw - 1, px)), max(0, min(self.ph - 1, py)))

    # ---------------------------------------------------------- 绘制

    def _font(self, size, bold=False):
        font = QFont("Microsoft YaHei UI", size)
        font.setBold(bold)
        return font

    def _rebuild_bg(self):
        pixmap = QPixmap(self.size())
        pixmap.fill(QColor(0, 0, 0))
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing, True)

        painter.setPen(QPen(C_GUIDE, 1))
        for i in range(1, 4):
            y = self.fy0 + (self.fy1 - self.fy0) * i / 4.0
            painter.drawLine(self.fx0 + 2, int(y), self.fx1 - 2, int(y))
        for i in range(1, 3):
            x = self.fx0 + (self.fx1 - self.fx0) * i / 3.0
            painter.drawLine(int(x), self.fy0 + 2, int(x), self.fy1 - 2)

        painter.setPen(QPen(C_FRAME, 2))
        painter.drawRect(self.fx0, self.fy0, self.fx1 - self.fx0, self.fy1 - self.fy0)

        painter.setPen(C_DIM)
        painter.setFont(self._font(9))
        painter.drawText(self.fx0 + 4, self.fy0 - 6,
                         "映射区 %sx%s   R 重读 / Esc 退出" % (self.pw, self.ph))

        painter.setFont(self._font(9))
        y = self.fy1 + 18
        for line in HINTS:
            painter.drawText(0, y, self.width(), 16, Qt.AlignHCenter, line)
            y += 17

        painter.end()
        self._bg = pixmap

    def paintEvent(self, event):
        painter = QPainter(self)
        if self._bg is None or self._bg.size() != self.size():
            self._rebuild_bg()
        painter.drawPixmap(0, 0, self._bg)

        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setFont(self._font(10, bold=True))
        painter.setPen(C_TEXT if self.status_ok is None else (C_OK if self.status_ok else C_BAD))
        painter.drawText(0, self.fy1 + 2, self.width(), 16, Qt.AlignHCenter, self.status_text)

        if self.mouse_pos:
            x, y = self.mouse_pos
            painter.setPen(QPen(C_CURSOR, 2))
            painter.drawEllipse(QRectF(x - 9, y - 9, 18, 18))
            painter.setPen(C_CURSOR)
            painter.setFont(self._font(8))
            point = self.to_phone(x, y)
            painter.drawText(x + 13, y + 16, "%s,%s" % point if point else "")
        painter.end()

    # ---------------------------------------------------------- 鼠标

    def mouseMoveEvent(self, event):
        pos = event.position().toPoint()
        self.mouse_pos = (pos.x(), pos.y())
        self.update()

    def mousePressEvent(self, event):
        pos = event.position().toPoint()
        if event.button() == Qt.LeftButton:
            self.drag_from = (pos.x(), pos.y())
            self.drag_t0 = time.time()
        elif event.button() == Qt.RightButton:
            self._enqueue("back")
        elif event.button() == Qt.MiddleButton:
            self._enqueue("home")
        self.update()

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.LeftButton or self.drag_from is None:
            return
        pos = event.position().toPoint()
        sx, sy = self.drag_from
        self.drag_from = None

        start = self.to_phone(sx, sy)
        end = self.to_phone(pos.x(), pos.y())
        if not start or not end:
            self._set_status("起点或终点不在映射区内", False)
            return

        if abs(pos.x() - sx) + abs(pos.y() - sy) < 12:
            self._enqueue("tap", x=start[0], y=start[1])
            return

        elapsed = int((time.time() - self.drag_t0) * 1000)
        self._enqueue("swipe", x1=start[0], y1=start[1], x2=end[0], y2=end[1],
                      duration=max(120, min(700, elapsed)))

    def wheelEvent(self, event):
        now = time.time()
        if now - self.last_wheel < 0.18:
            return
        self.last_wheel = now
        self._enqueue("next" if event.angleDelta().y() < 0 else "prev")

    # ---------------------------------------------------------- 键盘

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key_Escape:
            self._quit()
        elif key == Qt.Key_Up:
            self._enqueue("vol-up")
        elif key == Qt.Key_Down:
            self._enqueue("vol-down")
        elif key == Qt.Key_Left:
            self._enqueue("bright-down")
        elif key == Qt.Key_Right:
            self._enqueue("bright-up")
        elif key == Qt.Key_Space:
            self._enqueue("play-pause")
        elif key == Qt.Key_L:
            self._enqueue("lock")
        elif key == Qt.Key_W:
            self._enqueue("wake")
        elif key == Qt.Key_N:
            self._enqueue("key", keycode=83)
        elif key == Qt.Key_S:
            self._enqueue("__state__")
        elif key == Qt.Key_R:
            self._enqueue("__size__")
        else:
            super().keyPressEvent(event)

    # ---------------------------------------------------------- 任务队列

    def _enqueue(self, action, **kwargs):
        if self.queue.qsize() > 4:
            return
        self.queue.put((action, kwargs))

    def _worker(self):
        while True:
            action, kwargs = self.queue.get()
            if action == "__size__":
                self._refresh_size_async()
                self.result_box[0] = (True, "正在重新读取分辨率…", None)
                continue
            if action == "__state__":
                ok, info = self.ctl.adb.ensure()
                if ok:
                    setting, override, cached = self.ctl.adb.brightness_state()
                    info = "亮度设置 %s/255" % setting
                    if cached is not None:
                        info += " · 生效 %.4f" % cached
                    if override:
                        info += "  ⚠ %s 占用窗口亮度覆盖" % override
                self.result_box[0] = (ok, info)
                continue
            try:
                ok, info = self.ctl.run_action(action, **kwargs)
                if action == "connect":
                    self.connected = ok
                    if ok:
                        self._read_size()
                        self.need_layout = True
            except Exception as exc:
                ok, info = False, "异常: %s" % exc
            log("[overlay] %s: %s %s" % (action, "OK" if ok else "FAIL", info))
            self.result_box[0] = (ok, info)

    # ---------------------------------------------------------- 刷新

    def _set_status(self, text, ok):
        self.status_text = text
        self.status_ok = ok
        self.update()

    def _poll(self):
        if self.need_layout:
            self.need_layout = False
            self._relayout()
        item = self.result_box[0]
        if item is None:
            return
        self.result_box[0] = None
        ok, info = item
        if not self.connected:
            info += "   ⚠ 未连接（按 S 重试）"
        self._set_status(info, ok)

    def _quit(self):
        self.timer.stop()
        QApplication.quit()

    def closeEvent(self, event):
        self.timer.stop()
        super().closeEvent(event)


def main():
    cfg = load_config()
    app = QApplication(sys.argv)
    overlay = Overlay(cfg)
    overlay.showFullScreen()
    overlay.activateWindow()
    overlay.raise_()
    overlay.setFocus()
    return app.exec()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        log("[overlay] 异常退出: %s" % exc)
        raise
