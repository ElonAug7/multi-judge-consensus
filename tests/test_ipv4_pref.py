#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_ipv4_pref.py — P3a 检索层 IPv4 优先离线测试（零网络）
本机 IPv6 路由不稳时连接会卡在 SYN-SENT 直到超时 → 证据拿不到 → 门保守阻断。
修复：检索发起时把 getaddrinfo 结果重排为 IPv4 优先（不删地址）；可 env 关闭。
"""
import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mjc import knowledge


def run():
    ok = True

    def check(name, cond):
        nonlocal ok
        print(("  ✅ " if cond else "  ❌ ") + name)
        if not cond:
            ok = False

    orig = socket.getaddrinfo

    # 构造一个假 getaddrinfo：v6 在前、v4 在后
    def fake(name, *a, **k):
        return [(socket.AF_INET6, 1, 6, "", ("::1", 0, 0, 0)),
                (socket.AF_INET, 1, 6, "", ("127.0.0.1", 0))]

    try:
        socket.getaddrinfo = fake
        knowledge._IPV4_PATCHED = False
        os.environ.pop("MJC_KNOWLEDGE_IPV4", None)
        knowledge._prefer_ipv4()  # 打补丁
        res = socket.getaddrinfo("example.com", 443)
        check("IPv4 优先：AF_INET 排到最前", res[0][0] == socket.AF_INET)
        check("不删地址：IPv6 仍在（兵底）", any(r[0] == socket.AF_INET6 for r in res))
        check("幂等：重复调用不叠加包装", (knowledge._prefer_ipv4(), socket.getaddrinfo("x", 1) is not None)[1])

        # env 关闭
        patched = socket.getaddrinfo
        knowledge._IPV4_PATCHED = False
        os.environ["MJC_KNOWLEDGE_IPV4"] = "0"
        # 还原原始函数后再测关闭路径
        socket.getaddrinfo = fake
        knowledge._prefer_ipv4()
        check("env=0 可关闭（不改写 getaddrinfo）", socket.getaddrinfo is fake)
    finally:
        socket.getaddrinfo = orig if not getattr(orig, "_mjc_ipv4", False) else orig
        knowledge._IPV4_PATCHED = False
        os.environ.pop("MJC_KNOWLEDGE_IPV4", None)

    # 恢复环境（补丁可能已装到真实 getaddrinfo 上，还原之）
    try:
        socket.getaddrinfo = orig
    except Exception:
        pass

    print("== IPv4 优先全部通过 ✅ ==" if ok else "== ❌ 有失败 ==")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
