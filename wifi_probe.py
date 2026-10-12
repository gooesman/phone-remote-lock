# -*- coding: utf-8 -*-
"""
wifi_probe.py —— 局域网协议探针（抓"电脑控手机"App 的通信格式）

用途
----
很多"电脑控手机"的 App（WiFi Mouse / Remote Mouse / 电脑控手机…）都是闭源的，
但它们的电脑端客户端 ↔ 手机 App 之间通常就是 **UDP 或 TCP 明文包**。
我们不需要逆向它的 exe，只要**让客户端把包发到我们这边**：

    客户端里把"手机地址"填成 **电脑的局域网 IP**（不是手机 IP），
    本工具在电脑上监听对应端口 → 收下包、打印、**再转发给真实手机**，
    于是：手机上照常能用，我们同时看到了协议格式。

用法
----
    # 先用 -s 找出手机在局域网的 IP（手机端 App 一般会显示它）
    python wifi_probe.py --find

    # TCP：监听 5000，把包转发到 手机IP:5000
    python wifi_probe.py --listen 5000 --target 192.168.1.23:5000

    # UDP：同上（大多数这类 App 用 UDP）
    python wifi_probe.py --udp --listen 5000 --target 192.168.1.23:5000

    # 一次监听多个端口，协议没猜准时把常见端口都开上
    python wifi_probe.py --udp --listen 5000 8080 7777 9999 --target 192.168.1.23:5000

输出
----
    [12:31:02.123] PC→手机  TCP  len=6   01 00 00 00 00 00            |......|
    [12:31:02.140] 手机→PC  TCP  len=4   ac 00 00 00                  |....|
结束时会打印"包长分布"和"前若干字节的分布"，方便判断字段含义。
"""

import argparse
import collections
import socket
import sys
import threading
import time

HERE = socket.gethostbyname_ex(socket.gethostname())[2]
STATS = {"pc2phone": collections.Counter(), "phone2pc": collections.Counter()}
STATS_LEN = {"pc2phone": collections.Counter(), "phone2pc": collections.Counter()}
LOCK = threading.Lock()


def log(direction, proto, data):
    now = time.strftime("%H:%M:%S") + ".%03d" % (int(time.time() * 1000) % 1000)
    head = " ".join("%02x" % b for b in data[:24])
    text = "".join(chr(b) if 32 <= b < 127 else "." for b in data[:24])
    tag = "PC→手机" if direction == "pc2phone" else "手机→PC"
    print("[%s] %s  %s  len=%-4d %-72s |%s|"
          % (now, tag, proto, len(data), head, text))
    with LOCK:
        STATS[direction][" ".join("%02x" % b for b in data[:12])] += 1
        STATS_LEN[direction][len(data)] += 1


def find_devices():
    """列出本机所有网卡地址，方便你把客户端指向哪台机器。"""
    print("本机地址：")
    for addr in sorted(set(HERE)):
        print("   ", addr)
    print("\n把客户端里的『手机地址』改成上面除 127.0.0.1 之外的那个 IP，端口保持不变。")


def run_tcp(listen_port, target_host, target_port):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", listen_port))
    srv.listen(8)
    srv.settimeout(1.0)
    print("TCP 监听 :%d  →  转发到 %s:%d" % (listen_port, target_host, target_port))

    while True:
        try:
            client, addr = srv.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        print(">> 客户端连上来了：%s:%d" % addr)
        threading.Thread(target=handle_tcp, args=(client, target_host, target_port),
                         daemon=True).start()


def handle_tcp(client, target_host, target_port):
    try:
        up = socket.create_connection((target_host, target_port), timeout=5)
    except Exception as exc:
        print("!! 连不上目标 %s:%d（%s）" % (target_host, target_port, exc))
        client.close()
        return

    def pump(src, dst, direction):
        try:
            while True:
                data = src.recv(4096)
                if not data:
                    break
                log(direction, "TCP", data)
                dst.sendall(data)
        except Exception:
            pass
        finally:
            try:
                dst.shutdown(socket.SHUT_WR)
            except Exception:
                pass

    t1 = threading.Thread(target=pump, args=(client, up, "pc2phone"), daemon=True)
    t2 = threading.Thread(target=pump, args=(up, client, "phone2pc"), daemon=True)
    t1.start(); t2.start(); t1.join(); t2.join()
    client.close(); up.close()
    print("<< 连接结束")


def run_udp(listen_port, target_host, target_port):
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", listen_port))
    srv.settimeout(1.0)
    peer = [None]                     # 记住客户端地址，后续它的回包也转发
    out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    print("UDP 监听 :%d  →  转发到 %s:%d" % (listen_port, target_host, target_port))
    while True:
        try:
            data, addr = srv.recvfrom(65535)
        except socket.timeout:
            continue
        except OSError:
            break
        if peer[0] != addr:
            peer[0] = addr
            print(">> 客户端：%s:%d" % addr)
        log("pc2phone", "UDP", data)
        try:
            out.sendto(data, (target_host, target_port))
        except Exception as exc:
            print("!! 转发失败：%s" % exc)
        # 单独线程收手机回来的包
        threading.Thread(target=recv_back, args=(out, addr), daemon=True).start()


def recv_back(sock, client_addr):
    sock.settimeout(0.5)
    while True:
        try:
            data, _ = sock.recvfrom(65535)
        except socket.timeout:
            return
        except OSError:
            return
        log("phone2pc", "UDP", data)
        try:
            out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            out.sendto(data, client_addr)
            out.close()
        except Exception:
            pass


def dump_stats():
    print()
    print("=== 包长分布 ===")
    for key, label in (("pc2phone", "PC→手机"), ("phone2pc", "手机→PC")):
        if STATS_LEN[key]:
            top = STATS_LEN[key].most_common(6)
            print("  %-8s %s" % (label, ", ".join("%d字×%d" % (k, v) for k, v in top)))
    print()
    print("=== 出现最多的包头（推断字段用）===")
    for key, label in (("pc2phone", "PC→手机"), ("phone2pc", "手机→PC")):
        if STATS[key]:
            print("  %s：" % label)
            for head, count in STATS[key].most_common(6):
                print("     %-40s × %d" % (head, count))


def main():
    parser = argparse.ArgumentParser(description="局域网协议探针（帮你看清 App 的通信格式）")
    parser.add_argument("--find", action="store_true", help="只打印本机地址（填给客户端用）")
    parser.add_argument("--listen", type=int, nargs="+", default=[5000], help="要监听的端口")
    parser.add_argument("--target", default="", help="真实手机地址，形如 192.168.1.23:5000")
    parser.add_argument("--udp", action="store_true", help="按 UDP 监听（默认 TCP）")
    args = parser.parse_args()

    if args.find:
        find_devices()
        return 0

    if not args.target or ":" not in args.target:
        print("需要 --target 手机IP:端口，例如 --target 192.168.1.23:5000")
        return 2
    host, port = args.target.rsplit(":", 1)
    port = int(port)

    print("提示：客户端里的『手机地址』要改成电脑 IP（下面这串），端口保持 %d" % port)
    print("      " + ", ".join(sorted(set(HERE))))
    print()
    try:
        if args.udp:
            for p in args.listen:
                threading.Thread(target=run_udp, args=(p, host, port), daemon=True).start()
        else:
            for p in args.listen:
                threading.Thread(target=run_tcp, args=(p, host, port), daemon=True).start()
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        dump_stats()
    return 0


if __name__ == "__main__":
    sys.exit(main())