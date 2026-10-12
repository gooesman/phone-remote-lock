package com.gooesman.phonecursor;

import android.content.Intent;
import android.net.wifi.WifiManager;
import android.os.Bundle;
import android.provider.Settings;
import android.view.View;
import android.widget.TextView;

/** 引导页：显示本机 IP + 两个授权入口（无障碍、悬浮窗）。 */
public class MainActivity extends android.app.Activity {

    private TextView ip, status;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        ip = findViewById(R.id.ip);
        status = findViewById(R.id.status);

        findViewById(R.id.btn_accessibility).setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                try {
                    startActivity(new Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS));
                } catch (Exception e) {
                    status.setText("打不开设置：" + e);
                }
            }
        });
        findViewById(R.id.btn_overlay).setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                try {
                    startActivity(new Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                            android.net.Uri.parse("package:" + getPackageName())));
                } catch (Exception e) {
                    status.setText("打不开设置：" + e);
                }
            }
        });
        findViewById(R.id.btn_refresh).setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                showInfo();
            }
        });
        showInfo();
    }

    @Override
    protected void onResume() {
        super.onResume();
        showInfo();
    }

    private void showInfo() {
        String addr = "拿不到 IP";
        try {
            WifiManager wm = (WifiManager) getSystemService(WIFI_SERVICE);
            int ip = wm.getConnectionInfo().getIpAddress();
            addr = String.format("%d.%d.%d.%d", ip & 0xFF, (ip >> 8) & 0xFF,
                    (ip >> 16) & 0xFF, (ip >> 24) & 0xFF);
        } catch (Exception ignore) {
        }
        ip.setText("本机 IP：" + addr + "　端口：" + CursorService.PORT);
        boolean a11y = isAccessibilityOn();
        boolean overlay = hasOverlayPermission();
        status.setText("无障碍服务：" + (a11y ? "已开启 ✓" : "未开启")
                + "　悬浮窗：" + (overlay ? "已允许 ✓" : "未允许")
                + "\n两项都开完，电脑上就能用映射层控制这台手机了。");
    }

    private boolean isAccessibilityOn() {
        String id = getString(R.string.accessibility_service_id);
        try (android.database.Cursor c = getContentResolver().query(
                android.provider.Settings.Secure.CONTENT_URI, null, null, null, null)) {
            if (c == null) return false;
            while (c.moveToNext()) {
                String key = c.getString(0);
                if (key != null && key.contains(id)) {
                    String val = c.getString(1);
                    if (val != null && val.contains(id)) return true;
                }
            }
        } catch (Exception ignore) {
        }
        return false;
    }

    private boolean hasOverlayPermission() {
        try {
            return android.provider.Settings.canDrawOverlays(this);
        } catch (Throwable t) {
            return false;
        }
    }
}
