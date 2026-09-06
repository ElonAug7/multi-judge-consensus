#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_dispositions.py — 处置记录（纠正过程数据源）离线测试（零 API）
覆盖：append 校验（空/越界/重复/漏条/非法 action）、落盘格式、recent 顺序与容错、summary 裁剪。
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mjc import dispositions


def _entry(n_issues=2):
    return {
        "ts": "2026-09-06T09:00:00", "kind": "code", "sha": "abc123", "len": 200,
        "verdict": "revise", "pass_votes": 0, "reject_votes": 0, "revise_votes": 2,
        "api_calls": 4, "memory": {"primary": "mnemosyne"},
        "issues": [{"type": "factual_error", "judge": "GLM-4-Plus", "desc": "数字算错", "sug": "重算"},
                   {"type": "style", "judge": "glm-4-flash", "desc": "语气", "sug": ""}][:n_issues],
        "task": "某任务",
    }


def run():
    tmp = tempfile.mkdtemp()
    os.environ["MJC_DISPOSITIONS_TMP"] = tmp
    # 用临时文件隔离：monkeypatch PATH
    old_path = dispositions.PATH
    dispositions.PATH = os.path.join(tmp, "dispositions.jsonl")
    ok = True

    def check(name, cond):
        nonlocal ok
        print(("  ✅ " if cond else "  ❌ ") + name)
        if not cond:
            ok = False

    # 1) 空 issues → ValueError
    try:
        dispositions.append({"issues": []}, [])
        check("空 issues 拒绝", False)
    except ValueError:
        check("空 issues 拒绝", True)

    # 2) decisions 越界/漏条/重复
    e = _entry()
    for bad, name in [([{"idx": 5, "adopted": True}], "idx 越界拒绝"),
                      ([{"idx": 0, "adopted": True}], "漏条拒绝"),
                      ([{"idx": 0, "adopted": True}, {"idx": 0, "adopted": False}], "重复 idx 拒绝"),
                      ([{"idx": 0, "adopted": True, "action": "hack"}, {"idx": 1, "adopted": False}], "非法 action 拒绝")]:
        try:
            dispositions.append(e, bad)
            check(name, False)
        except ValueError:
            check(name, True)

    # 3) 合法 append → 落盘格式
    rec = dispositions.append(e, [
        {"idx": 0, "adopted": True, "action": "fixed", "note": "按建议重算为 71.9%"},
        {"idx": 1, "adopted": False, "action": "ignored", "note": "风格偏好，不影响正确性"},
    ])
    check("append 返回 at+review+decisions", rec.get("at") and rec["review"]["verdict"] == "revise")
    check("note 截断", len(rec["decisions"][0]["note"]) <= dispositions.MAX_NOTE)
    line = open(dispositions.PATH, encoding="utf-8").readline()
    d = json.loads(line)
    check("落盘可解析且含 task 摘要", d["review"]["task"] == "某任务" and len(d["decisions"]) == 2)
    check("summary 不含原始大字段", "memory" not in d["review"])

    # 4) recent 顺序（新→旧）与坏行容错
    with open(dispositions.PATH, "a", encoding="utf-8") as f:
        f.write("{bad json\n")
    dispositions.append(_entry(1), [{"idx": 0, "adopted": False, "note": "x"}])
    r = dispositions.recent(10)
    check("recent 新→旧 且坏行跳过", len(r) == 2 and r[0]["at"] >= r[1]["at"])

    # 5) 无文件 → []
    dispositions.PATH = os.path.join(tmp, "none.jsonl")
    check("无文件返回 []", dispositions.recent() == [])

    dispositions.PATH = old_path
    print("== dispositions 全部通过 ✅ ==" if ok else "== ❌ 有失败 ==")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
