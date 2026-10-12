# -*- coding: utf-8 -*-
"""
build_apk.py —— 不用 Gradle，直接用 SDK 自带工具打出 APK

只用 build-tools 里的 aapt2 / d8 / zipalign / apksigner + JDK 的 javac/keytool，
好处是完全可控、不用下载几百 MB 的 Gradle/AGP、也没有版本兼容问题。

    python android/build_apk.py
    → android/app/build/phone-cursor.apk

构建步骤
    1) aapt2 compile：把 res/ 编成 zip
    2) aapt2 link：资源 + AndroidManifest + 生成 R.java → base.apk
    3) javac：编译 java（bootclasspath 用 android.jar，目标是 Java 17）
    4) d8：class → classes.dex
    5) 打包：base.apk + classes.dex → unsigned.apk
    6) zipalign → apksigner 签名（有密钥就用，没有就自动生成一份调试密钥）
"""

import os
import shutil
import subprocess
import sys
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = r"C:\Users\78374\WorkBuddy\xiaomi\workbuddy"
TOOLS = r"C:\Users\78374\WorkBuddy\xiaomi\.workbuddy\tools\android"
SDK = os.path.join(TOOLS, "sdk")
BUILD_TOOLS = None
JDK = None

APP = os.path.join(HERE, "app")
SRC = os.path.join(APP, "src", "main")
OUT = os.path.join(APP, "build")
GEN = os.path.join(OUT, "gen")
CLASSES = os.path.join(OUT, "classes")
KEYSTORE = os.path.join(OUT, "debug.keystore")
APK = os.path.join(OUT, "phone-cursor.apk")


def find_tools():
    global BUILD_TOOLS, JDK
    if not os.path.isdir(SDK):
        sys.exit("找不到 SDK：%s（先跑 .workbuddy/tools/setup_android_sdk.py）" % SDK)
    bt_root = os.path.join(SDK, "build-tools")
    versions = sorted(os.listdir(bt_root)) if os.path.isdir(bt_root) else []
    for v in reversed(versions):
        aapt2 = os.path.join(bt_root, v, "aapt2.exe")
        if os.path.isfile(aapt2):
            BUILD_TOOLS = os.path.join(bt_root, v)
            break
    if not BUILD_TOOLS:
        sys.exit("没找到 build-tools（aapt2）")
    jdk_root = os.path.join(TOOLS, "jdk")
    for name in os.listdir(jdk_root):
        if os.path.isfile(os.path.join(jdk_root, name, "bin", "javac.exe")):
            JDK = os.path.join(jdk_root, name)
            break
    if not JDK:
        sys.exit("没找到 JDK（先跑 .workbuddy/tools/setup_android_sdk.py）")
    print("build-tools :", BUILD_TOOLS)
    print("JDK         :", JDK)


def run(args, label, cwd=None):
    t0 = time.time()
    proc = subprocess.run(args, capture_output=True, cwd=cwd)
    out = (proc.stdout or b"").decode("utf-8", "replace")
    err = (proc.stderr or b"").decode("utf-8", "replace")
    if proc.returncode != 0:
        print("[%s] 失败（退出码 %d）" % (label, proc.returncode))
        print((out + err)[-2500:])
        sys.exit(1)
    msgs = [l for l in (out + err).splitlines() if l.strip() and not l.strip().startswith("#")]
    print("[%s] ok  %.0fs%s" % (label, time.time() - t0,
                                 ("  " + msgs[-1][:90]) if msgs else ""))
    return out + err


def ensure_keystore():
    if os.path.isfile(KEYSTORE):
        return
    keytool = os.path.join(JDK, "bin", "keytool.exe")
    run([keytool, "-genkeypair", "-v", "-keystore", KEYSTORE, "-storepass", "android",
         "-keypass", "android", "-alias", "phonecursor", "-keyalg", "RSA", "-keysize", "2048",
         "-validity", "10000", "-dname", "CN=PhoneCursor, O=gooesman, C=CN"],
        "生成调试密钥")


def main():
    find_tools()
    if os.path.isdir(OUT):
        shutil.rmtree(OUT, ignore_errors=True)
    os.makedirs(GEN, exist_ok=True)
    os.makedirs(CLASSES, exist_ok=True)
    ensure_keystore()

    aapt2 = os.path.join(BUILD_TOOLS, "aapt2.exe")
    android_jar = os.path.join(SDK, "platforms", "android-35", "android.jar")
    if not os.path.isfile(android_jar):
        sys.exit("找不到 %s" % android_jar)

    # 1) 编译资源
    res_zip = os.path.join(OUT, "res.zip")
    run([aapt2, "compile", "--dir", os.path.join(SRC, "res"), "-o", res_zip], "aapt2 compile")

    # 2) 链接资源 + 生成 R.java
    base_apk = os.path.join(OUT, "base.apk")
    run([aapt2, "link", "-o", base_apk, "-I", android_jar,
         "--manifest", os.path.join(SRC, "AndroidManifest.xml"),
         "--java", GEN, "--min-sdk-version", "24", "--target-sdk-version", "35",
         res_zip], "aapt2 link")

    # 3) 编译 Java
    sources = []
    for root, _dirs, files in os.walk(os.path.join(SRC, "java")):
        sources += [os.path.join(root, f) for f in files if f.endswith(".java")]
    sources += [os.path.join(GEN, p) for p in
                (os.path.relpath(os.path.join(r, f), GEN)
                 for r, _d, fs in os.walk(GEN) for f in fs if f.endswith(".java"))]
    print("java 源文件 %d 个" % len(sources))
    run([os.path.join(JDK, "bin", "javac.exe"), "-source", "17", "-target", "17",
         "-encoding", "UTF-8", "-nowarn", "-bootclasspath", android_jar,
         "-classpath", android_jar, "-d", CLASSES] + sources, "javac")

    # 4) dex
    classes_dex = os.path.join(OUT, "classes.dex")
    d8 = os.path.join(BUILD_TOOLS, "d8.bat") if os.path.isfile(
        os.path.join(BUILD_TOOLS, "d8.bat")) else os.path.join(BUILD_TOOLS, "d8.exe")
    class_files = []
    for root, _dirs, files in os.walk(CLASSES):
        class_files += [os.path.join(root, f) for f in files if f.endswith(".class")]
    run([d8, "--lib", android_jar, "--min-api", "24", "--output", OUT] + class_files,
        "d8 → classes.dex")
    if not os.path.isfile(classes_dex):
        sys.exit("d8 没产出 classes.dex")

    # 5) 把 dex 塞进 apk
    unsigned = os.path.join(OUT, "unsigned.apk")
    with zipfile.ZipFile(base_apk, "r") as src, \
            zipfile.ZipFile(unsigned, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            dst.writestr(item, src.read(item.filename))
        dst.write(classes_dex, "classes.dex")

    # 6) 对齐 + 签名
    aligned = os.path.join(OUT, "aligned.apk")
    zipalign = os.path.join(BUILD_TOOLS, "zipalign.exe")
    run([zipalign, "-f", "4", unsigned, aligned], "zipalign")
    apksigner = os.path.join(BUILD_TOOLS, "apksigner.bat")
    run([apksigner, "sign", "--ks", KEYSTORE, "--ks-pass", "pass:android",
         "--key-pass", "pass:android", "--ks-key-alias", "phonecursor",
         "--out", APK, aligned], "apksigner 签名")
    run([apksigner, "verify", APK], "验签")

    size = os.path.getsize(APK) / 1048576.0
    print()
    print("APK 好了：%s  (%.2f MB)" % (APK, size))
    print("把它拖到 install_apk.bat 上就能装进手机。")


if __name__ == "__main__":
    main()