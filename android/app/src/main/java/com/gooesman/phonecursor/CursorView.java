package com.gooesman.phonecursor;

import android.content.Context;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.graphics.PixelFormat;
import android.os.Build;
import android.view.Gravity;
import android.view.WindowManager;

/**
 * 悬浮光标：一个圆环 + 十字，画在所有 App 之上。
 *
 * 关键旗标：
 *  - TYPE_APPLICATION_OVERLAY：允许"显示在其他应用上层"就能画
 *  - NOT_FOCUSABLE / NOT_TOUCHABLE / LAYOUT_NO_LIMITS：**不吃触摸**，
 *    只是画，不影响你在手机上的正常操作
 */
public class CursorView {

    private final android.content.Context ctx;
    private final WindowManager wm;
    private final Paint paint = new Paint(Paint.ANTI_ALIAS_FLAG);
    private WindowManager.LayoutParams params;
    private int x = -100, y = -100;
    private boolean pressed = false;
    private boolean visible = false;

    public CursorView(Context ctx) {
        this.ctx = ctx.getApplicationContext();
        this.wm = (WindowManager) this.ctx.getSystemService(Context.WINDOW_SERVICE);
        int type = Build.VERSION.SDK_INT >= Build.VERSION_CODES.O
                ? WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
                : WindowManager.LayoutParams.TYPE_PHONE;
        params = new WindowManager.LayoutParams(
                WindowManager.LayoutParams.MATCH_PARENT,
                WindowManager.LayoutParams.MATCH_PARENT,
                type,
                WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE
                        | WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE
                        | WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS
                        | WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN,
                PixelFormat.TRANSLUCENT);
        params.gravity = Gravity.TOP | Gravity.START;
        current = new Overlay();
        try {
            wm.addView(current, params);
            attached = true;
            android.util.Log.i("PhoneCursor", "悬浮窗已添加");
        } catch (Exception exc) {
            android.util.Log.e("PhoneCursor",
                    "悬浮窗添加失败（是否已允许「显示在其他应用上层」？）", exc);
        }
    }

    public void setSize(int w, int h) {
        params.width = w;
        params.height = h;
        applyLayout();
    }

    public void update(int nx, int ny, boolean isPressed) {
        this.x = nx;
        this.y = ny;
        this.pressed = isPressed;
        refresh();
    }

    public void setVisible(boolean v) {
        this.visible = v;
        refresh();
    }

    /** 绘制内容变了 → 只要重绘。 */
    private void refresh() {
        // 注意：**不要**在这里调 updateViewLayout。
        // 光标位置是画在 canvas 上的，不是布局参数变了；
        // 而带 FLAG_LAYOUT_NO_LIMITS 的窗口在 Android 12+ 调 updateViewLayout
        // 会直接抛异常（logcat 里每次移动都在报），反而把重绘也带崩了。
        try {
            current.invalidate();
        } catch (Exception ignore) {
        }
    }

    /** 第一次真正需要改布局参数时才更新（并且要有 try/catch 兜底）。 */
    private void applyLayout() {
        try {
            wm.updateViewLayout(current, params);
        } catch (Exception exc) {
            android.util.Log.w("PhoneCursor", "updateViewLayout 失败（忽略）", exc);
        }
    }

    private boolean attached = false;

    public boolean isVisible() {
        return visible;
    }

    /** 悬浮窗没加上（比如权限是服务启动之后才给的）就补一次。 */
    public void ensureAttached() {
        if (attached) {
            refresh();
            return;
        }
        try {
            wm.addView(current, params);
            attached = true;
            android.util.Log.i("PhoneCursor", "悬浮窗补挂成功");
        } catch (Exception exc) {
            android.util.Log.w("PhoneCursor", "悬浮窗仍挂不上："
                    + "请确认已允许「显示在其他应用上层」", exc);
        }
        refresh();
    }

    public void destroy() {
        attached = false;
        try {
            wm.removeView(current);
        } catch (Exception ignore) {
        }
    }

    private Overlay current;
    private boolean drawnOnce = false;

    private class Overlay extends android.view.View {
        Overlay() {
            super(ctx);
        }

        @Override
        protected void onDraw(Canvas canvas) {
            if (!visible) return;
            if (!drawnOnce) {
                drawnOnce = true;
                android.util.Log.i("PhoneCursor", "光标首帧已绘制 @" + (int) x + "," + (int) y);
            }
            paint.setStyle(Paint.Style.STROKE);
            paint.setStrokeWidth(pressed ? 5f : 3.5f);
            paint.setColor(pressed ? Color.argb(235, 255, 140, 40) : Color.argb(200, 255, 255, 255));
            float r = pressed ? 16f : 13f;
            canvas.drawCircle(x, y, r, paint);
            paint.setStrokeWidth(2f);
            paint.setColor(pressed ? Color.argb(180, 255, 140, 40) : Color.argb(110, 255, 255, 255));
            canvas.drawLine(x - r * 2.2f, y, x + r * 2.2f, y, paint);
            canvas.drawLine(x, y - r * 2.2f, x, y + r * 2.2f, paint);
            paint.setStyle(Paint.Style.FILL);
            paint.setColor(pressed ? Color.argb(120, 255, 140, 40) : Color.argb(60, 255, 255, 255));
            canvas.drawCircle(x, y, r * 0.35f, paint);
        }
    }
}