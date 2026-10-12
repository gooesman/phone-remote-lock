package com.gooesman.phonecursor;

import android.accessibilityservice.AccessibilityService;
import android.accessibilityservice.GestureDescription;
import android.graphics.Path;
import android.graphics.Rect;
import android.os.Bundle;
import android.util.Log;
import android.view.accessibility.AccessibilityEvent;

import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.InetAddress;
import java.net.SocketTimeoutException;

/**
 * 手机端核心：UDP 收电脑发来的鼠标指令 → 无障碍服务执行手势 + 更新悬浮光标。
 *
 * 之所以放在 AccessibilityService 里而不是 Activity：
 * 无障碍服务是常驻组件（只要用户在设置里开着就一直活着），不依赖界面生命周期，
 * 也就省掉了前台服务通知那一套。
 */
public class CursorService extends AccessibilityService {

    public static final int PORT = 4321;
    private static final String TAG = "PhoneCursor";

    private DatagramSocket socket;
    private Thread worker;
    private volatile boolean running = true;

    private CursorView cursorView;
    private int screenW = 1080, screenH = 2400;
    private float x = 0, y = 0;
    private boolean pressed = false;
    private int pending = 0;                 // 连续手势排队计数

    @Override
    protected void onServiceConnected() {
        super.onServiceConnected();
        Log.i(TAG, "无障碍服务已连接");
        try {
            android.util.DisplayMetrics dm = getResources().getDisplayMetrics();
            screenW = dm.widthPixels;
            screenH = dm.heightPixels;
        } catch (Throwable ignore) {
        }
        cursorView = new CursorView(this);
        cursorView.setSize(screenW, screenH);
        cursorView.setVisible(true);

        startReceiver();
    }

    private void startReceiver() {
        try {
            if (socket != null) {          // 服务可能被系统反复重连，先释放旧 socket
                try { socket.close(); } catch (Exception ignore) { }
                socket = null;
            }
            socket = new DatagramSocket(null);
            socket.setReuseAddress(true);
            socket.bind(new java.net.InetSocketAddress(PORT));
            socket.setSoTimeout(1000);
        } catch (Exception exc) {
            Log.e(TAG, "UDP 端口被占用或不可用: " + exc);
            return;
        }
        worker = new Thread(new Runnable() {
            @Override
            public void run() {
                byte[] buf = new byte[64];
                while (running) {
                    try {
                        DatagramPacket pkt = new DatagramPacket(buf, buf.length);
                        socket.receive(pkt);
                        handle(buf, pkt.getLength(), pkt.getAddress(), pkt.getPort());
                    } catch (SocketTimeoutException ignore) {
                        // 每秒醒一次，检查是否还在运行
                    } catch (Exception exc) {
                        Log.e(TAG, "接收出错: " + exc);
                    }
                }
            }
        }, "cursor-udp");
        worker.setDaemon(true);
        worker.start();
        Log.i(TAG, "UDP 监听已启动，端口 " + PORT);
    }

    // ---------------- 协议 ----------------

    private int u16(byte[] d, int off) {
        return (d[off] & 0xFF) | ((d[off + 1] & 0xFF) << 8);
    }

    private int s16(byte[] d, int off) {
        return (short) u16(d, off);
    }

    private void handle(byte[] d, int len, InetAddress from, int port) {
        if (len < 1) return;
        int cmd = d[0] & 0xFF;
        try {
            switch (cmd) {
                case 0x01:                              // MOVE
                    moveTo(u16(d, 1), u16(d, 3));
                    break;
                case 0x02: {                            // TAP（dur >= 600 视为长按）
                    int tx = u16(d, 1), ty = u16(d, 3);
                    int dur = len >= 7 ? u16(d, 5) : 0;
                    moveTo(tx, ty);
                    doTap(tx, ty, dur);
                    break;
                }
                case 0x03: {                            // SWIPE
                    int x1 = u16(d, 1), y1 = u16(d, 3);
                    int x2 = u16(d, 5), y2 = u16(d, 7);
                    int dur = len >= 11 ? Math.max(80, u16(d, 9)) : 180;
                    doSwipe(x1, y1, x2, y2, dur);
                    break;
                }
                case 0x04:                              // DOWN
                    moveTo(u16(d, 1), u16(d, 3));
                    pressed = true;
                    updateCursor();
                    break;
                case 0x05:                              // UP
                    pressed = false;
                    updateCursor();
                    break;
                case 0x06: {                            // SCROLL
                    int amount = len >= 7 ? s16(d, 5) : 0;
                    int sx = len >= 7 ? u16(d, 1) : screenW / 2;
                    int sy = len >= 7 ? u16(d, 3) : screenH / 2;
                    doSwipe(sx, sy - amount * 40, sx, sy + amount * 40, 220);
                    break;
                }
                case 0x07: {                            // KEY
                    int code = len >= 3 ? s16(d, 1) : 0;
                    if (code != 0) performGlobalAction(
                            code == 4 ? GLOBAL_ACTION_BACK
                                    : code == 3 ? GLOBAL_ACTION_HOME : GLOBAL_ACTION_RECENTS);
                    break;
                }
                case 0x08:                              // CURSOR 显示开关
                    if (cursorView != null) cursorView.setVisible(len < 2 || (d[1] & 1) == 1);
                    break;
                case 0x09: {                            // STATUS 应答
                    byte[] out = new byte[14];
                    out[0] = 'S'; out[1] = '1';
                    put16(out, 2, screenW);
                    put16(out, 4, screenH);
                    put16(out, 6, (int) x);
                    put16(out, 8, (int) y);
                    put16(out, 10, pressed ? 1 : 0);
                    put16(out, 12, cursorView != null ? 1 : 0);
                    socket.send(new DatagramPacket(out, out.length, from, port));
                    break;
                }
                default:
                    Log.i(TAG, "未知指令 " + cmd);
            }
        } catch (Exception exc) {
            Log.e(TAG, "处理指令失败", exc);
        }
    }

    private void put16(byte[] b, int off, int v) {
        b[off] = (byte) (v & 0xFF);
        b[off + 1] = (byte) ((v >> 8) & 0xFF);
    }

    // ---------------- 光标 ----------------

    private void moveTo(int nx, int ny) {
        x = Math.max(0, Math.min(screenW - 1, nx));
        y = Math.max(0, Math.min(screenH - 1, ny));
        updateCursor();
    }

    private void updateCursor() {
        if (cursorView != null) cursorView.update((int) x, (int) y, pressed);
    }

    // ---------------- 手势 ----------------

    private void doTap(final int tx, final int ty, final int holdMs) {
        final Path path = new Path();
        path.moveTo(tx, ty);
        // 注：API 35 的公开桩里 GestureDescription.OnCompletedCallback 已不可用，
        // 光标按下态的复位统一交给下面的 Handler 定时器。
        final long hold = Math.max(40, holdMs);
        GestureDescription desc = new GestureDescription.Builder()
                .addStroke(new GestureDescription.StrokeDescription(path, 0, hold))
                .build();
        pending++;
        dispatchGesture(desc, new AccessibilityService.GestureResultCallback() {
            @Override
            public void onCompleted(GestureDescription d) {
                pending = Math.max(0, pending - 1);
            }

            @Override
            public void onCancelled(GestureDescription d) {
                pending = Math.max(0, pending - 1);
            }
        }, null);
        pressed = true;
        updateCursor();
        if (holdMs == 0) {
            // 轻点：按下时先亮一下
            new android.os.Handler(android.os.Looper.getMainLooper()).postDelayed(
                    new Runnable() {
                        @Override
                        public void run() {
                            pressed = false;
                            updateCursor();
                        }
                    }, 90);
        }
    }

    private void doSwipe(int x1, int y1, int x2, int y2, int durationMs) {
        final int steps = Math.max(8, Math.min(36, durationMs / 12));
        final long dur = Math.max(80, durationMs);
        final long start = android.os.SystemClock.uptimeMillis();
        final long stepGap = dur / steps;
        final Path path = new Path();
        path.moveTo(x1, y1);
        for (int i = 1; i <= steps; i++) {
            path.lineTo(x1 + (x2 - x1) * i / steps, y1 + (y2 - y1) * i / steps);
        }
        pressed = true;
        updateCursor();
        dispatchGesture(new GestureDescription.Builder()
                .addStroke(new GestureDescription.StrokeDescription(path, 0, dur))
                .build(), new AccessibilityService.GestureResultCallback() {
            @Override
            public void onCompleted(GestureDescription d) {
                pressed = false;
                moveTo(x2, y2);
            }

            @Override
            public void onCancelled(GestureDescription d) {
                pressed = false;
            }
        }, null);
    }

    // ---------------- 生命周期 ----------------

    @Override
    public void onAccessibilityEvent(AccessibilityEvent event) {
        // 不用读界面，所有动作都是按坐标注入的
    }

    @Override
    public void onInterrupt() {
    }

    @Override
    public void onDestroy() {
        running = false;
        try {
            if (socket != null) socket.close();
        } catch (Exception ignore) {
        }
        if (cursorView != null) cursorView.destroy();
        super.onDestroy();
    }

    @Override
    public boolean onUnbind(android.content.Intent intent) {
        running = false;
        if (socket != null) socket.close();
        if (cursorView != null) cursorView.destroy();
        return super.onUnbind(intent);
    }
}