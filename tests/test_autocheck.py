#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MJC · autocheck 离线测试（零 API）— auto_review / cmd_auto / cmd_scan
P3.2 拆分后新增：验证自 cli.py 移入 mjc/autocheck.py 的行为不变（含 P4.4：kind=code 无 task 时默认文案）。
隔离：pipeline.run_review_once / resolve_screen_judge / settings.effective / build_pool / memctx 全 mock；
AUTO_LOG_DIR / LOG_DIR 指临时目录，不碰真 logs/。scan 模块函数全 fake，不碰真 cursor/锁/会话目录。
  python3 tests/test_autocheck.py     # 离线（默认）
"""
import json
import os
import sys
import tempfile
import datetime
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mjc.pipeline
from mjc import settings as settings_mod
import mjc.scan
import mjc.autocheck as ac
from mjc.autocheck import auto_review, cmd_auto, cmd_scan

CODE_DEFAULT_TASK = "（代码任务收尾审查）回顾以下改动描述与代码片段是否准确"
MSG_DEFAULT_TASK = "（自动审查）回顾以下 Agent 输出是否准确可靠"

PASS_RECORD = {"final": "pass", "pass_votes": 2, "reject_votes": 0, "revise_votes": 0,
               "debate_rounds": 1, "rounds": [{"round": 1, "kind": "debate",
               "opinions": [{"judge_display": "judge-a", "verdict": "pass", "issues": []}]}]}
PASS_META = {"api_calls": 2, "screened": False, "screen_passed": False}
REJECT_RECORD = dict(PASS_RECORD, final="reject", reject_votes=2)
REVISE_RECORD = dict(PASS_RECORD, final="revise", revise_votes=2)


def _setup(tmp, screen_on=False):
    """通用 mock 环境。返回 (captured, tmp)。captured: {"task_text":..., "content":..., "calls": N}"""
    captured = {"task_text": None, "content": None, "calls": 0}

    def _fake_run(task_text, content, pool, **kw):
        captured["task_text"] = task_text
        captured["content"] = content
        captured["calls"] += 1
        return PASS_RECORD, PASS_META

    ac.AUTO_LOG_DIR = tmp
    ac.LOG_DIR = tmp
    ac.build_pool = lambda specs: [object(), object()]  # 只要 len≥2
    settings_mod.effective = lambda: {"committee": ["deepseek:deepseek-v4-flash", "glm:glm-4-flash"]}
    mjc.pipeline.run_review_once = _fake_run
    mjc.pipeline.resolve_screen_judge = (lambda *a, **k: object()) if screen_on else (lambda *a, **k: None)
    return captured


def test_too_short():
    tmp = tempfile.mkdtemp(prefix="mjc-auto-")
    _setup(tmp)
    code, out = auto_review("短", no_memory=True)
    assert code == 0 and out.get("skipped") == "too_short" and out["len"] == 1, out
    assert os.listdir(tmp) == [], "too_short 不应落任何文件"
    print("  ✅ too_short(<80)：跳过、零文件、exit 0")


def test_kind_code_default_task():
    tmp = tempfile.mkdtemp(prefix="mjc-auto-")
    captured = _setup(tmp)
    content = "改动描述与代码片段" * 20  # 120+ 字符
    code, out = auto_review(content, channel="ch", kind="code", no_memory=True)
    assert code == 0 and out["reviewed"] and out["kind"] == "code", out
    assert captured["task_text"] == CODE_DEFAULT_TASK, captured["task_text"]
    assert captured["content"] == content
    files = os.listdir(tmp)
    day = __import__("datetime").date.today().isoformat()
    assert f"{day}.jsonl" in files, files
    entry = json.load(open(os.path.join(tmp, f"{day}.jsonl"), encoding="utf-8"))
    assert entry["verdict"] == "pass" and entry["channel"] == "ch" and entry["sha"], entry
    assert out["verdict"] == "pass" and out["memory_source"] == "none"
    print("  ✅ kind=code 无 task → 默认任务文案 + 落盘 day.jsonl（P4.4）")


def test_kind_message_default_task():
    tmp = tempfile.mkdtemp(prefix="mjc-auto-")
    captured = _setup(tmp)
    code, out = auto_review("一段需要审查的回复文本内容" * 12, kind="message", no_memory=True)
    assert code == 0 and captured["task_text"] == MSG_DEFAULT_TASK, captured["task_text"]
    print("  ✅ kind=message 无 task → 默认任务文案")


def test_reject_writes_findings():
    tmp = tempfile.mkdtemp(prefix="mjc-auto-")

    def _fake_run(task_text, content, pool, **kw):
        return REJECT_RECORD, PASS_META

    _setup(tmp)
    mjc.pipeline.run_review_once = _fake_run
    code, out = auto_review("这段内容会被判 reject" * 10, no_memory=True)
    assert code == 0 and out["verdict"] == "reject", out
    assert os.path.exists(os.path.join(tmp, "findings.jsonl")), "reject 应追加 findings.jsonl"
    f = json.loads(open(os.path.join(tmp, "findings.jsonl"), encoding="utf-8").readline())
    assert f["verdict"] == "reject" and f["reject_votes"] == 2, f
    print("  ✅ reject → findings.jsonl 追加 + 票型落盘")


def test_revise_no_findings():
    tmp = tempfile.mkdtemp(prefix="mjc-auto-")

    def _fake_run(task_text, content, pool, **kw):
        return REVISE_RECORD, PASS_META

    _setup(tmp)
    mjc.pipeline.run_review_once = _fake_run
    code, out = auto_review("这段内容会被判 revise" * 10, no_memory=True)
    assert code == 0 and out["verdict"] == "revise", out
    assert not os.path.exists(os.path.join(tmp, "findings.jsonl")), "revise 是软提示，不写 findings"
    print("  ✅ revise → 软提示，不写 findings")


def test_cmd_auto_stdin_content():
    tmp = tempfile.mkdtemp(prefix="mjc-auto-")
    _setup(tmp)
    args = SimpleNamespace(in_file=None, content="命令行直接传的内容" * 12, task=None,
                           kind="message", no_memory=True)
    code = cmd_auto(args)  # prints json
    assert code == 0
    print("  ✅ cmd_auto：直接 content → 0（stdout 已打 JSON）")


def test_cmd_scan_too_short_branch():
    cand = {"ts_ms": 9999, "ts": "2026-09-06T10:00:00", "content": "短" * 60, "id": "e1"}
    called = {}

    def _noop(*a, **k):
        called["set_cursor"] = a[0] if a else k.get("ts_ms")

    mjc.scan.locked = lambda: False
    mjc.scan.newest_candidate = lambda: (cand, "sess-1")
    mjc.scan._cursor = lambda: 1000  # 旧 cursor 更小 → 有新稿
    mjc.scan.set_cursor = _noop
    mjc.scan.set_lock = lambda: None
    args = SimpleNamespace(force=False, no_memory=True)
    code = cmd_scan(args)
    assert code == 0 and called.get("set_cursor") == 9999, called
    print("  ✅ cmd_scan：<150 且非 force → 推进 cursor、skip too_short、不锁")


def test_cmd_scan_full_path():
    cand = {"ts_ms": 9999, "ts": "2026-09-06T10:00:00", "content": "长终稿内容" * 60, "id": "e1"}
    state = {"lock": 0, "cursor": None, "reviewed": None}

    mjc.scan.locked = lambda: False
    mjc.scan.newest_candidate = lambda: (cand, "sess-1")
    mjc.scan._cursor = lambda: 1000
    mjc.scan.set_lock = lambda: state.__setitem__("lock", state["lock"] + 1)
    mjc.scan.set_cursor = lambda ts_ms: state.__setitem__("cursor", ts_ms)
    ac.auto_review = lambda content, **kw: state.__setitem__("reviewed", content) or (0, {"reviewed": True})
    args = SimpleNamespace(force=False, no_memory=True)
    code = cmd_scan(args)
    assert code == 0, code
    assert state["lock"] == 1 and state["cursor"] == 9999 and state["reviewed"] == cand["content"], state
    print("  ✅ cmd_scan：≥150 → set_lock → auto_review(content) → 推进 cursor")


def test_dedupe_same_sha_kind_skips():
    tmp = tempfile.mkdtemp(prefix="mjc-auto-")
    captured = _setup(tmp)
    content = "同一段需要重复审查的内容文本" * 15  # >80 字
    code, out = auto_review(content, channel="ch1", kind="message", no_memory=True)
    assert code == 0 and out.get("reviewed") and captured["calls"] == 1, out
    # 同 sha+同 kind 6h 内 → dup 跳过，不二次跑 pipeline、不落盘
    code, out2 = auto_review(content, channel="ch2", kind="message", no_memory=True)
    assert code == 0 and out2.get("skipped") == "dup", out2
    assert out2["sha"] == out["sha"] and out2.get("since"), out2
    assert captured["calls"] == 1 and out2.get("reviewed") is None, (captured, out2)
    # 同内容不同 kind（message→code）：审查语境不同，不去重
    code, out3 = auto_review(content, kind="code", no_memory=True)
    assert code == 0 and out3.get("reviewed") and captured["calls"] == 2, (out3, captured)
    # 已审条目超 6h 窗口 → 重新审查
    day = datetime.date.today().isoformat()
    path = os.path.join(tmp, f"{day}.jsonl")
    stale = datetime.datetime.now() - datetime.timedelta(hours=7)
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    with open(path, "w", encoding="utf-8") as fh:
        for l in lines:
            e = json.loads(l)
            e["ts"] = stale.isoformat(timespec="seconds")
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")
    code, out4 = auto_review(content, kind="message", no_memory=True)
    assert code == 0 and out4.get("reviewed") and captured["calls"] == 3, (out4, captured)
    print("  ✅ 去重：同 sha+kind 6h 内 dup 跳过；kind 不同 / 超窗 → 重审（P4.2）")


def main():
    print("== autocheck 离线测试（零 API）==")
    test_too_short()
    test_kind_code_default_task()
    test_kind_message_default_task()
    test_reject_writes_findings()
    test_revise_no_findings()
    test_cmd_auto_stdin_content()
    test_cmd_scan_too_short_branch()
    test_cmd_scan_full_path()
    test_dedupe_same_sha_kind_skips()
    print("== autocheck 全部通过 ✅ ==")


def _test_no_key_verifier_fallback():
    """无委员会（无 key）时：确定性验证器仍可零成本拦截；干净内容报错。"""
    import sys as _sys
    from unittest import mock
    from mjc import autocheck
    fake_settings = mock.MagicMock()
    fake_settings.effective.side_effect = ValueError("无 key")
    with mock.patch.object(autocheck, "build_pool", return_value=[]), \
         mock.patch.object(autocheck.pipeline, "run_review_once") as rro, \
         mock.patch.dict(_sys.modules, {"mjc.settings": fake_settings}):
        code, out = autocheck.auto_review("这段内容没有任何数字日期错误。", task="t", kind="code", min_len=10)
        assert out.get("error"), "干净内容+无池应报错"
        rro.return_value = ({"final": "revise", "pass_votes": 0, "reject_votes": 0, "revise_votes": 1,
                             "debate_rounds": 0, "rounds": [{"round": 1, "kind": "verifier", "opinions": []}],
                             "tokens": {}, "cost_yuan": 0.0},
                            {"api_calls": 0, "verifier": True})
        code2, out2 = autocheck.auto_review("8 月 31 日提交，9 月 5 日合并，历时 4 天。", task="t", kind="code", min_len=10)
        assert out2.get("verdict") == "revise" and rro.called, "无池+可验算错误应走验证器拦截"
    print("  ✅ 无 key 兜底：干净报错 / 可验算错误零成本拦截")
    return True


if __name__ == "__main__":
    main()
