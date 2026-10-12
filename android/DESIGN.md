# 手机端接收端 App（自建）— 设计与协议

目标：**电脑鼠标直接操作手机，手机上有可见光标**，纯软件、无线、免 root。

## 为什么需要自建

- 电脑上没有任何"鼠标控手机"的 App 能在**手机上**显示光标（主流的要么方向相反
  ——手机控电脑，要么把手机画面投到电脑窗口）；
- adb 注入事件永远不会有光标（HyperOS 只对物理触摸/真鼠标设备可视化，
  `dumpsys input` 里 `mMouseInfoMap` 实测始终为 `<EMPTY>`）；
- 手机端要"画光标 + 执行点击"，本质上就是：**悬浮窗画光标 + 无障碍服务 dispatchGesture**，
  这正是这个 App 做的事。

## 组成

```
phone-remote-lock/
└─ android/                      ← 我们的 App（Java，无第三方依赖）
   ├─ settings.gradle
   ├─ build.gradle
   ├─ gradle.properties
   └─ app/
      ├─ build.gradle
      └─ src/main/
         ├─ AndroidManifest.xml
         ├─ java/com/gooesman/phonecursor/
         │   ├─ MainActivity.java        开机监听 + 显示本机 IP + 授权引导
         │   ├─ CursorService.java       无障碍服务：收 UDP → 执行手势 / 更新光标
         │   └─ CursorView.java          悬浮光标（画圆环 + 十字，可调透明度）
         └─ res/…                        布局/图标/字符串
```

电脑侧：`lock_phone.py` 里加一个 `NetPointer` 后端（协议与 `Adb` 同名方法对齐），
配置项 `pointer_backend = adb | net`，映射层把鼠标操作改发 UDP。

## 协议（UDP，一条指令一个数据报）

所有数值都是**小端有符号/无符号 16 位**（坐标上限 65535，够任何屏幕）。

| 指令 | 载荷 | 说明 |
|---|---|---|
| `0x01` MOVE | `01 x y` | 移动光标到绝对坐标 (x, y) |
| `0x02` TAP | `02 x y dur` | 点按；dur = 按住毫秒数（0=轻点，≥600=长按） |
| `0x03` SWIPE | `03 x1 y1 x2 y2 dur` | 滑动 |
| `0x04` DOWN | `04 x y` | 按下不放（连续拖动/倍速长按） |
| `0x05` UP | `05` | 抬起 |
| `0x06` SCROLL | `06 x y amount` | 滚轮（正数向下） |
| `0x07` KEY | `07 code` | 发送按键（keycode，如 4 返回 / 3 桌面 / 26 电源） |
| `0x08` CURSOR | `08 on` | 光标显示开关（1 显示 / 0 隐藏） |
| `0x09` STATUS | `09` | 回一个状态包（屏幕宽高、光标位置、是否按下） |

- 默认端口 **4321**（UDP）。
- 状态包（对 `0x09` 的应答）：`S1 w h x y pressed alpha`（都是 16 位）。
- 鉴权：同一个局域网内不做鉴权（和这类工具的通行做法一样）；协议预留 1 字节 flags 便于以后加校验。

## 手机端实现要点

1. **无障碍服务**（`CursorService`）里直接开 `DatagramSocket` 收指令 ——
   无障碍服务是常驻组件，不依赖 Activity 生命周期，也就不用前台服务通知。
2. **手势**用 `AccessibilityService.dispatchGesture`：
   - 点按/长按 → 一条 `GestureDescription`：`ACTION_DOWN` → (可选 wait) → `ACTION_UP`；
   - 滑动 → `ACTION_DOWN` → 一串 `ACTION_MOVE`（按时长铺开）→ `ACTION_UP`。
3. **光标悬浮窗**：`WindowManager` + `TYPE_APPLICATION_OVERLAY`，
   旗标 `NOT_FOCUSABLE | NOT_TOUCHABLE | LAYOUT_NO_LIMITS`（不吃触摸，只画）；
   用 `FLAG_NOT_TOUCH_MODAL` 保证不打断别的 App。
4. **光标位置**只在收到 MOVE/DOWN 时更新；点击时短暂放大 + 变色（按下态），做视觉反馈。
5. 屏幕旋转/分辨率变化：监听配置变化，收到指令时若坐标越界则夹到有效范围内。
6. 权限：首次启动引导开启「无障碍服务」+「显示在其他应用上层」（`ACTION_MANAGE_ACCESSIBILITY_SETTINGS`
   与 `ACTION_MANAGE_OVERLAY_PERMISSION` 两个 Intent）。

## 电脑侧接入

`lock_phone.py` 新增：

```python
class NetPointer:      # 方法名与 Adb 对齐，映射层可以无缝换后端
    def display_size() / move_pointer() / tap() / swipe() / double_tap_ratio()
    def press_down() / press_up() / hold_start() / hold_stop() / scroll() / key()
```

`Controller.pointer` 属性：配置 `pointer_backend="net"` 时返回 `NetPointer`，否则返回 `Adb`；
映射层与面板里凡是"指针类"操作都改成走 `self.ctl.pointer`。
配置项：`pointer_backend`、`net_host`（手机 IP）、`net_port`（默认 4321）。