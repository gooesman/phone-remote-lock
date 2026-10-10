# -*- coding: utf-8 -*-
"""
overlay.py —— 手机形状的「鼠标映射层」窗口（Qt 版）

窗口本身就是一块**和手机等比例对应的矩形**（不再是全屏遮黑）：
底色可以切黑 / 切白，不透明度可调，矩形以外完全透明，桌面照常可见可点。

交互（默认要**按住右键**才开始作用于手机，避免误触）：

    按住右键 + 左键单击     -> tap（点按）
    按住右键 + 左键拖动     -> swipe（滑动，刷视频 / 翻页）
    按住右键 + 左键按住不动 -> 长按（短视频倍速就是这么触发的）
    按住右键 + 滚轮上/下    -> 上一个 / 下一个视频
    按住右键 + 中键         -> 回到桌面
    右键快速单击（不按住）  -> 返回键
    ↑ / ↓                   -> 音量 + / -
    ← / →                   -> 亮度 + / -
    空格                    -> 播放 / 暂停
    F（按住）               -> 屏幕中心长按 = 倍速，松开恢复
    L / W                   -> 锁屏 / 唤醒
    B / H                   -> 返回 / 桌面
    N                       -> 通知栏
    T                       -> 底色 黑/白 切换（写回 config）
    [ / ]                   -> 不透明度 -/+（写回 config）
    P                       -> 手机端触摸圆点 开/关
    S                       -> 刷新状态
    R                       -> 重新读取分辨率（手机转过屏后用）
    Esc                     -> 退出

直接运行：python overlay.py      或双击 overlay.bat

手机端只开开发者选项里的「显示点按操作反馈」(show_touches) —— 安卓自带的
触摸圆点，点哪儿亮哪儿；不用「指针位置」那个十字线（它会自己消失）。
映射层退出时自动还原。
"""

import os
import queue
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lock_phone import Controller, load_config, log, save_config  # noqa: E402

try:
    from PySide6.QtCore import QPointF, QRect, QRectF, Qt, QTimer
    from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen, QRegion
    from PySide6.QtWidgets import QApplication, QWidget
except ImportError as exc:  # pragma: no cover
    sys.stderr.write(
        "缺少 PySide6，请先运行 setup.bat，或执行：\n"
        "  .venv\\Scripts\\python.exe -m pip install PySide6-Essentials\n"
        "原始错误: %s\n" % exc
    )
    raise

# 小球光标（橙色最显眼，黑白底上都看得见）
C_BALL = QColor(255, 122, 60)
C_BALL_RING = QColor(255, 255, 255, 230)
C_OK = QColor(141, 224, 160)
C_BAD = QColor(255, 143, 143)

# 一键尺寸预设：占屏幕可用高度的比例（按手机比例算宽度）
SIZE_PRESETS = [
    ("small", "小", 0.45),
    ("medium", "中", 0.65),
    ("large", "大", 0.85),
    ("max", "满高", 0.95),
]


HINTS_ARMED = [
    "映射中：左键=点按  拖动=滑动  按住不动=长按  滚轮=上/下视频  中键=桌面",
    "1/2/3=小中大  0=满高   +/-=微调   M=鼠标光标   ←→=系统亮度   Shift+←→=App亮度",
]
HINTS_IDLE = [
    "按住鼠标右键 才开始映射（避免误触）    右键快按一下 = 返回",
    "1/2/3=小中大   0=满高（也可点右上角小方块）   M=鼠标光标   T=黑/白   U=解锁   Esc=退出",
]


class Overlay(QWidget):
    def __init__(self, cfg, on_close=None, standalone=True):
        """
        on_close   : 关闭时的回调（面板托管时用它同步按钮状态）
        standalone : 是不是独立进程。独立时关闭 = 退出程序；
                     被面板托管（同一进程）时关闭**只能关自己**，
                     否则会把面板一起带走 —— 之前按 Esc 把控制面板也关了就是这个原因。
        """
        super().__init__()
        self.cfg = cfg
        self.ctl = Controller(cfg)
        self.on_close = on_close
        self.standalone = standalone

        self.pw = 1440
        self.ph = 3200
        self.fx0 = self.fy0 = self.fx1 = self.fy1 = 0

        self.queue = queue.Queue()
        self.result_box = [None]
        self.need_layout = False
        self.connected = False

        self.drag_from = None
        self.drag_t0 = 0.0
        self.drag_moved = False
        self.holding = False        # 正在手机上进行长按（倍速）状态
        self.hold_key = False       # F 键按住中
        self.last_wheel = 0.0
        self.status_text = "正在连接手机…"
        self.status_ok = None
        self.mouse_pos = None
        self._dirty = True
        self._size_guard = False
        self._last_size = (self.width(), self.height())
        # 手机端触摸圆点：连上之前设置不了，所以连上后要自动补一次（补到成功为止）
        self._pointer_never_ok = bool(cfg.get("overlay_pointer", True))
        self._pointer_retry_at = 0.0
        # 鼠标模式下把光标位置同步到手机（限速，别把 adb 打满）
        self._last_move = 0.0
        self._cfg_check_at = 0.0
        self._move_rate = max(5, min(60, int(cfg.get("overlay_move_rate", 20) or 20)))

        # 外观
        self.alpha = float(cfg.get("overlay_alpha", 0.45) or 0.45)
        self.fill_color = self._norm_color(cfg.get("overlay_color", "black"))
        self.fps = max(10, min(60, int(cfg.get("overlay_fps", 30) or 30)))

        # 「按住右键才映射」
        self.requires_arm = bool(cfg.get("map_requires_right", True))
        self.arm_click_ms = int(cfg.get("arm_click_ms", 250) or 250)
        self.armed = not self.requires_arm     # 不需要按住时视为一直就绪
        self.right_t0 = 0.0
        self.arm_timer = QTimer(self)
        self.arm_timer.setSingleShot(True)
        self.arm_timer.timeout.connect(self._arm_on)

        # 长按（倍速）：按住不动超过 long_press_ms 就在光标处开始长按
        self.long_press_ms = int(cfg.get("long_press_ms", 600) or 600)
        self.long_press_hold_ms = int(cfg.get("long_press_hold_ms", 1200) or 1200)
        self.long_press_max_ms = int(cfg.get("long_press_max_ms", 4000) or 4000)
        self.press_timer = QTimer(self)
        self.press_timer.setSingleShot(True)
        self.press_timer.timeout.connect(self._hold_start)
        self.hold_timer = QTimer(self)
        self.hold_timer.setSingleShot(True)
        self.hold_timer.timeout.connect(self._hold_timeout)

        # 手机端触摸圆点：None = 还没碰过设置，(show_touches, pointer_location) = 原值
        self._pointer_prev = None
        self._pointer_on = False

        self.setWindowTitle("手机映射层")
        # 普通置顶窗口：带标题栏，可拖动 / 缩放
        self.setWindowFlags(Qt.Window | Qt.WindowStaysOnTopHint
                            | Qt.WindowMinimizeButtonHint | Qt.WindowCloseButtonHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)  # 矩形外全透明
        self.setMinimumSize(200, 300)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)

        self._relayout()

        threading.Thread(target=self._worker, daemon=True).start()

        # 状态轮询降频：以前 80ms 一次，鼠标高频移动时容易把重绘挤掉
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll)
        self.timer.start(200)

        # 重绘节流：鼠标事件只记坐标，统一按 fps 节流重绘（1000Hz 鼠标也不会打满）
        self.paint_timer = QTimer(self)
        self.paint_timer.timeout.connect(self._paint_tick)
        self.paint_timer.start(max(16, int(1000 / self.fps)))

        self._enqueue("connect")
        self._refresh_size_async()

    # ---------------------------------------------------------- 外观

    @staticmethod
    def _norm_color(value):
        text = str(value or "black").strip().lower()
        if text in ("black", "dark", "黑", "黑色"):
            return QColor(0, 0, 0)
        if text in ("white", "light", "白", "白色"):
            return QColor(255, 255, 255)
        color = QColor(text)
        return color if color.isValid() else QColor(0, 0, 0)

    def _is_dark(self):
        return self.fill_color.lightness() < 128

    def _fg(self, alpha=255):
        """在当前底色上可读的文字颜色。"""
        color = QColor(255, 255, 255) if self._is_dark() else QColor(20, 20, 20)
        color.setAlpha(alpha)
        return color

    # ---------------------------------------------------------- 尺寸

    def _read_size(self, force=False):
        try:
            size = self.ctl.adb.display_size(force=force)
        except Exception:
            size = None
        if size and size[0] and size[1]:
            self.pw, self.ph = size

    def _refresh_size_async(self):
        """
        异步刷新分辨率。

        构造函数里绝不能同步读 adb：手机离线时一次 adb 调用可能卡十几秒，
        而这里是主线程，会把整个界面冻住（也会拖住后面的队列命令）。
        """
        def runner():
            try:
                self._read_size(force=True)
                self.need_layout = True
            except Exception as exc:
                log("[overlay] 读取分辨率失败: %s" % exc)
        threading.Thread(target=runner, daemon=True).start()

    def _relayout(self):
        w = max(self.width(), 100)
        h = max(self.height(), 100)
        margin = float(self.cfg.get("overlay_margin", 0.01) or 0.0)
        fh = int(h * (1 - 2 * margin))
        fw = int(fh * self.pw / float(self.ph))
        max_w = int(w * (1 - 2 * margin))
        if fw > max_w:
            fw = max_w
            fh = int(fw * self.ph / float(self.pw))
        self.fx0 = (w - fw) // 2
        self.fy0 = (h - fh) // 2
        self.fx1 = self.fx0 + fw
        self.fy1 = self.fy0 + fh
        self._apply_mask()
        self._dirty = True
        self.update()

    def _apply_mask(self):
        """窗口形状 = 整个窗口（这样四边还能拖动改大小）。

        之前把窗口裁成矩形是为了让矩形外的空白能"点穿"到桌面，
        但那会让窗口边框一起被裁掉 → 没法用鼠标拖边缘改大小。
        现在改成整窗，矩形外只是透明（点上去不动作，会提示"不在矩形内"）。
        """
        try:
            self.setMask(QRegion(self.rect()))
        except Exception as exc:
            log("[overlay] 设置窗口形状失败: %s" % exc)

    def _fit_aspect(self):
        """拖动窗口时保持手机比例：谁变得多就按谁反推另一边。"""
        if self._size_guard or not self.pw or not self.ph:
            return
        aspect = self.pw / float(self.ph)
        w, h = self.width(), self.height()
        lw, lh = self._last_size
        new_h = max(120, int(round(w / aspect)))
        new_w = max(120, int(round(h * aspect)))
        try:
            self._size_guard = True
            if abs(w - lw) >= abs(h - lh):
                if abs(new_h - h) > 2:
                    self.resize(w, new_h)
            elif abs(new_w - w) > 2:
                self.resize(new_w, h)
        except Exception:
            pass
        finally:
            self._size_guard = False
        self._last_size = (self.width(), self.height())

    def apply_preset(self, name):
        """一键切到预设尺寸（按屏幕可用高度算，保持手机比例）。"""
        table = {key: (label, ratio) for key, label, ratio in SIZE_PRESETS}
        if name in ("next", "cycle"):
            keys = [k for k, _l, _r in SIZE_PRESETS]
            cur = self.cfg.get("overlay_size_preset") or ""
            name = keys[(keys.index(cur) + 1) % len(keys)] if cur in keys else keys[0]
        if name not in table:
            return False, "未知尺寸预设：%s" % name
        label, ratio = table[name]
        screen = QApplication.primaryScreen().availableGeometry()
        h = max(160, int(screen.height() * ratio))
        w = max(120, int(h * self.pw / float(self.ph)))
        if w > screen.width() * 0.98:
            w = int(screen.width() * 0.98)
            h = int(w * self.ph / float(self.pw))
        self._size_guard = True
        self.resize(w, h)
        self._size_guard = False
        self._last_size = (w, h)
        self.move(screen.x() + (screen.width() - w) // 2,
                  screen.y() + (screen.height() - h) // 2)
        self.cfg["overlay_size_preset"] = name
        self.cfg["overlay_win_w"] = w
        self.cfg["overlay_win_h"] = h
        self._persist()
        self._relayout()
        self._set_status("窗口已切到「%s」%dx%d" % (label, w, h), True)
        return True, label

    def _cycle_size(self):
        ok, label = self.apply_preset("next")
        if ok:
            self._set_status("窗口尺寸：%s（再点右上角切换）" % label, True)

    def scale_window(self, factor):
        """按比例放大/缩小窗口（保持手机比例），并把大小记进配置。"""
        w = max(160, int(round(self.width() * factor)))
        h = max(160, int(round(w * self.ph / float(self.pw))))
        self._size_guard = True
        self.resize(w, h)
        self._size_guard = False
        self._last_size = (w, h)
        self._save_window_size()
        self._set_status("窗口 %dx%d（比例已锁手机）" % (w, h), True)
        self._relayout()

    def _save_window_size(self):
        self.cfg["overlay_win_w"] = self.width()
        self.cfg["overlay_win_h"] = self.height()
        self._persist()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_aspect()
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

    def _paint_tick(self):
        if self._dirty:
            self._dirty = False
            self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)

        # 透明窗口必须每帧彻底清屏，否则会出现残影 / 画着画着就没了
        painter.setCompositionMode(QPainter.CompositionMode_Source)
        painter.fillRect(self.rect(), Qt.transparent)
        painter.setCompositionMode(QPainter.CompositionMode_SourceOver)

        rect = QRect(self.fx0, self.fy0, self.fx1 - self.fx0, self.fy1 - self.fy0)
        fill = QColor(self.fill_color)
        fill.setAlphaF(max(0.0, min(1.0, self.alpha)))
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(fill))
        painter.drawRect(rect)

        # 淡淡的九宫参考线
        painter.setPen(QPen(self._fg(28), 1))
        for i in range(1, 4):
            y = self.fy0 + (self.fy1 - self.fy0) * i / 4.0
            painter.drawLine(self.fx0 + 1, int(y), self.fx1 - 1, int(y))
        for i in range(1, 3):
            x = self.fx0 + (self.fx1 - self.fx0) * i / 3.0
            painter.drawLine(int(x), self.fy0 + 1, int(x), self.fy1 - 1)

        # 边框：映射中就亮一点，一眼看出状态
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(self._fg(220 if self.armed else 90), 2))
        painter.drawRect(rect)

        # 矩形内部：标题 / 状态 / 提示
        painter.setFont(self._font(9))
        painter.setPen(self._fg(190))
        painter.drawText(self.fx0 + 6, self.fy0 + 14,
                         "%s   %sx%s" % ("映射中 ●" if self.armed else "待命 ○",
                                        self.pw, self.ph))

        painter.setFont(self._font(9))
        painter.setPen(self._fg(150))
        painter.drawText(self.fx0 + 6, self.fy1 - 30,
                         "底色 %s  不透明度 %.2f  输入 %s" % (
                             "黑" if self._is_dark() else "白", self.alpha,
                             "鼠标光标" if self.ctl.adb.is_mouse_mode() else "触摸"))

        painter.setFont(self._font(9, bold=True))
        painter.setPen(C_OK if self.status_ok else (C_BAD if self.status_ok is False
                                                   else self._fg(200)))
        painter.drawText(self.fx0 + 6, self.fy1 - 12,
                         self.status_text[:60])

        # 右上角「尺寸」小方块
        corner = QRectF(self.fx1 - 30, self.fy0 + 6, 24, 24)
        painter.setPen(QPen(self._fg(120), 1))
        painter.setBrush(QBrush(self._fg(30)))
        painter.drawRoundedRect(corner, 4, 4)
        painter.setPen(QPen(self._fg(200), 1.2))
        painter.drawLine(QPointF(corner.left() + 6, corner.bottom() - 8),
                         QPointF(corner.right() - 6, corner.bottom() - 8))
        painter.drawLine(QPointF(corner.right() - 8, corner.top() + 6),
                         QPointF(corner.right() - 8, corner.bottom() - 6))

        hints = HINTS_ARMED if self.armed else HINTS_IDLE
        painter.setFont(self._font(8))
        painter.setPen(self._fg(120))
        y = self.fy0 + 30
        for line in hints:
            painter.drawText(self.fx0 + 6, y, line)
            y += 13

        # 小球光标
        if self.mouse_pos:
            x, y = self.mouse_pos
            painter.setPen(QPen(C_BALL_RING, 1.5))
            painter.setBrush(QBrush(C_BALL))
            painter.drawEllipse(QPointF(x, y), 6.0, 6.0)
            if self.armed:
                painter.setPen(QPen(C_BALL, 1))
                painter.setBrush(Qt.NoBrush)
                painter.drawEllipse(QPointF(x, y), 10.0, 10.0)
        painter.end()

    # ---------------------------------------------------------- 鼠标

    def mouseMoveEvent(self, event):
        pos = event.position().toPoint()
        self.mouse_pos = (pos.x(), pos.y())
        self._dirty = True
        # 鼠标模式：把光标位置同步给手机（手机屏幕上是系统鼠标光标在走）
        if self.armed and self.ctl.adb.is_mouse_mode():
            now = time.time()
            if now - self._last_move >= 1.0 / self._move_rate:
                self._last_move = now
                point = self.to_phone(pos.x(), pos.y())
                if point:
                    self._enqueue("__move__", x=point[0], y=point[1])
        if self.drag_from and not self.drag_moved:
            dx = abs(pos.x() - self.drag_from[0])
            dy = abs(pos.y() - self.drag_from[1])
            if dx + dy > 12:
                self.drag_moved = True
                self.press_timer.stop()   # 拖动了就不算长按

    def mousePressEvent(self, event):
        pos = event.position().toPoint()
        if event.button() == Qt.RightButton:
            # 按住 = 进入映射；快按一下 = 返回
            self.right_t0 = time.time()
            if self.requires_arm:
                self.arm_timer.start(self.arm_click_ms)
            self._dirty = True
            return

        # 右上角小方块 = 一键切换窗口尺寸（任何状态下都能点，不会发给手机）
        if (event.button() == Qt.LeftButton
                and self.fx1 - 30 <= pos.x() <= self.fx1 - 6
                and self.fy0 + 6 <= pos.y() <= self.fy0 + 30):
            self._cycle_size()
            return

        if not self.armed:
            self._set_status("按住鼠标右键才能操作手机", None)
            return

        if event.button() == Qt.LeftButton:
            self.drag_from = (pos.x(), pos.y())
            self.drag_t0 = time.time()
            self.drag_moved = False
            if self.to_phone(pos.x(), pos.y()):
                self.press_timer.start(self.long_press_ms)
        elif event.button() == Qt.MiddleButton:
            self._enqueue("home")
        self._dirty = True

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.RightButton:
            self.arm_timer.stop()
            held = (time.time() - self.right_t0) * 1000 if self.right_t0 else 0
            self.right_t0 = 0.0
            if self.armed and self.requires_arm:
                self.armed = False
                self._stop_drag()
                self._set_status("已退出映射（松开右键）", None)
            elif held < self.arm_click_ms:
                self._enqueue("back")      # 快按一下 = 返回键
            self._dirty = True
            return

        if event.button() != Qt.LeftButton or self.drag_from is None:
            return
        self.press_timer.stop()
        pos = event.position().toPoint()
        sx, sy = self.drag_from
        self.drag_from = None

        # 已经进入长按状态：抬手就结束长按（倍速停止）
        if self.holding:
            self._hold_end()
            return

        if not self.armed:
            return

        start = self.to_phone(sx, sy)
        end = self.to_phone(pos.x(), pos.y())
        if not start or not end:
            self._set_status("起点或终点不在矩形内", False)
            return

        if not self.drag_moved and abs(pos.x() - sx) + abs(pos.y() - sy) < 12:
            self._enqueue("tap", x=start[0], y=start[1])
            return

        elapsed = int((time.time() - self.drag_t0) * 1000)
        self._enqueue("swipe", x1=start[0], y1=start[1], x2=end[0], y2=end[1],
                      duration=max(120, min(700, elapsed)))

    # ---------------------------------------------------------- 映射开关

    def _arm_on(self):
        """右键按住超过 arm_click_ms：进入映射状态。"""
        if self.right_t0:
            self.armed = True
            self._set_status("映射中：左键 = 点按 / 拖动", True)
            self._dirty = True

    def _stop_drag(self):
        self.press_timer.stop()
        if self.holding:
            self._hold_end()
        self.drag_from = None

    # ---------------------------------------------------------- 长按 / 倍速

    def _hold_start(self):
        """鼠标按住不动到阈值：开始"按住不放"（短视频里就是倍速）。"""
        if not self.armed or not self.drag_from or self.drag_moved or self.holding:
            return
        point = self.to_phone(*self.drag_from)
        if not point:
            return
        self.holding = True
        self._enqueue("__hold_down__",
                      fx=point[0] / float(self.pw), fy=point[1] / float(self.ph))
        self.hold_timer.start(self.long_press_max_ms)   # 兜底：防止一直按住不放

    def _hold_end(self):
        self.holding = False
        self.hold_timer.stop()
        self._enqueue("__hold_up__")

    def _hold_timeout(self):
        if self.holding:
            self.holding = False
            self._enqueue("__hold_up__")
            self._set_status("长按已达上限 %sms，已自动松开" % self.long_press_max_ms, True)

    def keyReleaseEvent(self, event):
        super().keyReleaseEvent(event)

    def wheelEvent(self, event):
        if not self.armed:
            self._set_status("按住鼠标右键才能操作手机", None)
            return
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
            if event.modifiers() & Qt.ShiftModifier:
                self._enqueue("app-vol-up")          # Shift+↑ = App 音量（右侧滑动）
            else:
                self._enqueue("vol-up")
        elif key == Qt.Key_Down:
            if event.modifiers() & Qt.ShiftModifier:
                self._enqueue("app-vol-down")
            else:
                self._enqueue("vol-down")
        elif key == Qt.Key_Left:
            if event.modifiers() & Qt.ShiftModifier:
                self._enqueue("app-bright-down")     # Shift+← = App 亮度（左侧滑动）
            else:
                self._enqueue("bright-down")
        elif key == Qt.Key_Right:
            if event.modifiers() & Qt.ShiftModifier:
                self._enqueue("app-bright-up")
            else:
                self._enqueue("bright-up")
        elif key == Qt.Key_Space:
            self._enqueue("play-pause")
        elif key == Qt.Key_L:
            self._enqueue("lock")
        elif key == Qt.Key_W:
            self._enqueue("wake")
        elif key == Qt.Key_B:
            self._enqueue("back")
        elif key == Qt.Key_H:
            self._enqueue("home")
        elif key == Qt.Key_N:
            self._enqueue("key", keycode=83)
        elif key == Qt.Key_S:
            self._enqueue("__state__")
        elif key == Qt.Key_R:
            self._enqueue("__size__")
        elif key == Qt.Key_P:
            self._enqueue("__pointer__")
        elif key == Qt.Key_T:
            self._toggle_color()
        elif key == Qt.Key_BracketLeft:
            self._bump_alpha(-0.05)
        elif key == Qt.Key_BracketRight:
            self._bump_alpha(+0.05)
        elif key == Qt.Key_F:
            # 倍速开关：按一下开（按住不放），再按一下关
            if not event.isAutoRepeat():
                self._enqueue("speed-toggle")
        elif key == Qt.Key_G:
            # 长按一下（同点 swipe，App 认这个）
            if not event.isAutoRepeat():
                self._enqueue("long-press-once")
        elif key == Qt.Key_U:
            self._enqueue("unlock")
        elif key == Qt.Key_M:
            self._enqueue("mouse-mode")
        elif key == Qt.Key_1:
            self.apply_preset("small")
        elif key == Qt.Key_2:
            self.apply_preset("medium")
        elif key == Qt.Key_3:
            self.apply_preset("large")
        elif key == Qt.Key_0:
            self.apply_preset("max")
        elif key in (Qt.Key_Plus, Qt.Key_Equal):
            self.scale_window(1.08)
        elif key == Qt.Key_Minus:
            self.scale_window(1 / 1.08)
        elif key == Qt.Key_F11:
            if self.isFullScreen():
                self.showNormal()
            else:
                self.showFullScreen()
        else:
            super().keyPressEvent(event)

    # ---------------------------------------------------------- 外观调节

    def _toggle_color(self):
        self.fill_color = QColor(255, 255, 255) if self._is_dark() else QColor(0, 0, 0)
        self.cfg["overlay_color"] = "white" if not self._is_dark() else "black"
        self._persist()
        self._set_status("底色已切换为 %s" % ("白" if not self._is_dark() else "黑"), True)
        self._dirty = True

    def _bump_alpha(self, delta):
        self.alpha = max(0.05, min(1.0, round(self.alpha + delta, 2)))
        self.cfg["overlay_alpha"] = self.alpha
        self._persist()
        self._set_status("不透明度 %.2f" % self.alpha, True)
        self._dirty = True

    def _persist(self):
        try:
            save_config(self.cfg)
        except Exception as exc:
            log("[overlay] 保存外观设置失败: %s" % exc)

    # ---------------------------------------------------------- 手机端触摸圆点

    def _pointer_setup(self):
        """worker 线程调用：只开安卓自带的「显示点按操作反馈」圆点。"""
        if not self.cfg.get("overlay_pointer", True):
            self._pointer_on = False
            return "手机触摸圆点已在 config.json 关闭（overlay_pointer=false）"
        try:
            prev = self.ctl.adb.pointer_aids_state()
            if self._pointer_prev is None:
                self._pointer_prev = prev
            # 圆点开、指针位置（十字线）关 —— 十字线会自己消失，不用
            ok, msg = self.ctl.adb.set_pointer_aids(1, 0)
            show, _loc = self.ctl.adb.pointer_aids_state()
            self._pointer_on = bool(ok) and show == 1
            if self._pointer_on:
                return "已开启手机触摸圆点（安卓自带）"
            if ok:
                return "设置写进去了但读回还是 %s（开发者选项总开关可能关着）" % show
            return "开启手机触摸圆点失败: %s" % (msg or "未知错误")
        except Exception as exc:
            return "开启手机触摸圆点异常: %s" % exc

    def _pointer_restore(self):
        """还原打开映射层之前的设置。"""
        if self._pointer_prev is None:
            return
        try:
            show, loc = self._pointer_prev
            self.ctl.adb.set_pointer_aids(
                show if show is not None else 0,
                loc if loc is not None else 0)
        except Exception as exc:
            log("[overlay] 还原手机触摸圆点失败: %s" % exc)
        self._pointer_prev = None
        self._pointer_on = False

    def _pointer_restore_blocking(self, timeout=4.0):
        """退出时同步还原（worker 是 daemon 线程，进程退出前必须等它做完）。"""
        if self._pointer_prev is None:
            return
        thread = threading.Thread(target=self._pointer_restore)
        thread.start()
        thread.join(timeout=timeout)

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
            if action == "__pointer__":
                self._pointer_never_ok = False      # 手动关过就别再自动补
                if self._pointer_on:
                    self._pointer_restore()
                    self.result_box[0] = (True, "已关闭手机触摸圆点")
                else:
                    self.result_box[0] = (True, self._pointer_setup())
                continue
            if action == "__move__":
                self.ctl.adb.move_pointer(kwargs.get("x", 0), kwargs.get("y", 0))
                continue
            if action == "__pointer_ensure__":
                if self._pointer_on:
                    self._pointer_never_ok = False
                    continue
                msg = self._pointer_setup()
                if self._pointer_on:
                    self._pointer_never_ok = False
                    self.result_box[0] = (True, "已补开手机触摸圆点")
                else:
                    self.result_box[0] = (False, msg)
                continue
            if action == "__hold_down__":
                # 按住不放：DOWN + MOVE 心跳，等 __hold_up__ 才松手
                ok, info = self.ctl.adb.hold_start(kwargs.get("fx", 0.5),
                                                   kwargs.get("fy", 0.5))
                self.result_box[0] = (ok, "按住中（倍速）" if ok else info, None)
                continue
            if action == "__hold_up__":
                ok, info = self.ctl.adb.hold_stop()
                self.result_box[0] = (True, "已松开" if ok else info, None)
                continue
            try:
                ok, info = self.ctl.run_action(action, **kwargs)
                if action == "connect":
                    self.connected = ok
                    if ok:
                        self._read_size()
                        self.need_layout = True
                        info += " · " + self._pointer_setup()
            except Exception as exc:
                ok, info = False, "异常: %s" % exc
            log("[overlay] %s: %s %s" % (action, "OK" if ok else "FAIL", info))
            self.result_box[0] = (ok, info)

    # ---------------------------------------------------------- 刷新

    def _apply_cfg_size(self):
        """看配置文件里的尺寸预设有没有被外面改过（面板/托盘），有就应用。"""
        try:
            data = load_config()
        except Exception:
            return
        want = data.get("overlay_size_preset") or ""
        if not want or want == self.cfg.get("overlay_size_preset"):
            return
        self.cfg["overlay_size_preset"] = want
        self.apply_preset(want)
        self._set_status("已按设置切到「%s」" % want, True)

    def _set_status(self, text, ok):
        self.status_text = text
        self.status_ok = ok
        self._dirty = True
        self.update()

    def _poll(self):
        if self.need_layout:
            self.need_layout = False
            self._relayout()
        # 面板/托盘改了尺寸预设 → 2 秒内自动应用
        if time.time() - getattr(self, "_cfg_check_at", 0) >= 2.0:
            self._cfg_check_at = time.time()
            self._apply_cfg_size()
        # 启动时手机可能还没连上，导致"开圆点"这步被跳过 —— 连上后自动补
        if (self.connected and self._pointer_never_ok
                and time.time() >= self._pointer_retry_at):
            self._pointer_retry_at = time.time() + 5
            self._enqueue("__pointer_ensure__")
        item = self.result_box[0]
        if item is None:
            return
        self.result_box[0] = None
        ok, info = item
        if not self.connected:
            info += "   ⚠ 未连接（按 S 重试）"
        self._set_status(info, ok)

    def _release_hold_blocking(self):
        """退出前务必松开：否则手机上会残留一个"一直按着"的手指。"""
        self.holding = False
        self.hold_key = False
        try:
            if self.ctl.adb.is_holding():
                self.ctl.adb.hold_stop()
            else:
                self.ctl.adb.press_up()
        except Exception as exc:
            log("[overlay] 释放按住失败: %s" % exc)

    def _stop_timers(self):
        for timer in (self.timer, self.press_timer, self.hold_timer,
                      self.paint_timer, self.arm_timer):
            try:
                timer.stop()
            except Exception:
                pass

    def _quit(self):
        """关掉映射层本身（托管在面板进程里时，绝不能把面板也退了）。

        顺序讲究：**先隐藏窗口**（按键立刻有反馈），设备侧的收尾（松手、还原触摸圆点）
        可能要一两次 adb 往返甚至几秒，绝不卡住 UI。
        """
        self.timer.stop()
        self.paint_timer.stop()
        self.press_timer.stop()
        self.hold_timer.stop()
        self.arm_timer.stop()
        self.hide()
        if callable(self.on_close):
            try:
                self.on_close()
            except Exception as exc:
                log("[overlay] 关闭回调失败: %s" % exc)
        if self.standalone:
            self._shutdown(join=1.5)
            QApplication.quit()
        else:
            # 托管在面板进程里：收尾丢后台，面板继续正常用
            threading.Thread(target=self._shutdown, kwargs={"join": 4.0},
                             daemon=True).start()

    def _shutdown(self, join=2.0):
        """释放设备状态（松手、还原触摸圆点）；join 是等 adb 收尾的最长时间。"""
        try:
            self._release_hold_blocking()
        except Exception as exc:
            log("[overlay] 松手失败: %s" % exc)
        try:
            self._pointer_restore_blocking(timeout=join)
        except Exception as exc:
            log("[overlay] 还原触摸圆点失败: %s" % exc)

    def closeEvent(self, event):
        self._shutdown(join=2.0)
        super().closeEvent(event)


def apply_saved_geometry(overlay, cfg):
    """按配置里的尺寸预设（或上次保存的大小）把窗口摆好。

    main() 和"面板里开启映射层"两条路都用它，保证行为一致
    （之前面板走的是 showFullScreen()，会和"手机形状矩形"的新设计打架）。
    """
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    screen = app.primaryScreen().availableGeometry() if app else None
    if screen is None:
        return
    height = min(int(screen.height() * 0.82), 980)
    width = max(200, int(height * overlay.pw / float(overlay.ph)))
    preset = (cfg.get("overlay_size_preset") or "").strip()
    table = {key: ratio for key, _label, ratio in SIZE_PRESETS}
    if preset in table:
        height = max(160, int(screen.height() * table[preset]))
        width = max(120, int(height * overlay.pw / float(overlay.ph)))
    else:
        try:
            saved_w = int(cfg.get("overlay_win_w") or 0)
            saved_h = int(cfg.get("overlay_win_h") or 0)
            if saved_w > 120 and saved_h > 120:
                width, height = saved_w, saved_h
        except Exception:
            pass
    overlay._size_guard = True
    overlay.resize(width, height)
    overlay._size_guard = False
    overlay._last_size = (width, height)
    overlay.move(screen.x() + (screen.width() - width) // 2,
                 screen.y() + (screen.height() - height) // 2)
    overlay._relayout()


def main():
    cfg = load_config()
    app = QApplication(sys.argv)
    overlay = Overlay(cfg)

    apply_saved_geometry(overlay, cfg)

    overlay.show()
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
