#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MJC · scan 转录扫描器离线测试（零 API，纯文件系统）
  python3 tests/test_scan.py

覆盖（P2.3 + P4.1/P4.3 + 2026-09-12 全量介入）：
  - _parse_ts：Z 后缀 / 毫秒 / 带时区 / 垃圾输入
  - newest_candidate：assistant 终稿提取（长度≥min_len（默认 1）、无 toolCall；NO_REPLY/纯符号跳过）、
    user/工具中间步跳过、多 session 取最新、36h 未改动文件跳过、字段（id/ts_ms/ts/content/len）+ session id
  - find_new：无 cursor 首次初始化（不回溯）；cursor 损坏 → 自动重置 + 初始化 + scan-events.jsonl 事件；
    有新稿返回、无新稿 None
  - session 排除（P4.3）：cron/subagent/dreaming 等机器会话不入选（sessions.json 注册表 + 孤儿
    trajectory 元数据兑底），main/dashboard/openclaw-* 渠道放行，trajectory trace 跳过，
    无索引/无轨迹孤儿回退放行
  - lock：set_lock 后 locked() True；文件过期（mtime 超 60s）→ False；无文件 → False
全部走临时目录（monkeypatch scan 模块级路径常量），不触碰真实 logs/auto。
"""
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mjc import scan


def _fresh_dirs():
    """每测独立临时目录，monkeypatch scan 路径常量（模块级，函数运行时取全局）"""
    root = tempfile.mkdtemp(prefix="mjc-scan-")
    sess = os.path.join(root, "sessions")
    logs = os.path.join(root, "logs", "auto")
    os.makedirs(sess)
    os.makedirs(logs)
    scan.SESS_DIR = sess
    scan.LOG_DIR = logs
    scan.CURSOR = os.path.join(logs, ".cursor.json")
    scan.LOCK = os.path.join(logs, ".scan-lock")
    return root, sess, logs


def _txt(t):
    return {"type": "text", "text": t}


def _tool():
    return {"type": "toolCall", "name": "read", "id": "tc1"}


def _iso(dt):
    """aware datetime → ISO（Z 后缀，带毫秒）—— 与真实转录格式一致"""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _line(mid, role, blocks, dt):
    return json.dumps({"id": mid, "type": "message", "timestamp": _iso(dt),
                       "message": {"role": role, "content": blocks}}, ensure_ascii=False)


def _write_session(sess, fn, lines, mode="w"):
    path = os.path.join(sess, fn)
    with open(path, mode, encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


LONG = "这是一段足够长的助手终稿文本。" * 20  # ~340 字，>150
SHORT = "太短了。"


def test_parse_ts():
    assert scan._parse_ts("2026-09-06T10:00:00Z") == int(
        datetime(2026, 9, 6, 10, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    assert scan._parse_ts("2026-09-06T18:00:00.250000+08:00") == int(
        datetime(2026, 9, 6, 10, 0, 0, 250000, tzinfo=timezone.utc).timestamp() * 1000)
    assert scan._parse_ts("2026-09-06T10:00:00+08:00") == int(
        datetime(2026, 9, 6, 2, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    assert scan._parse_ts("垃圾") is None
    assert scan._parse_ts("") is None
    print("  ✅ _parse_ts：Z/毫秒/时区换算正确，垃圾输入 → None")


def test_candidate_extraction():
    root, sess, logs = _fresh_dirs()
    t0 = datetime.now(timezone.utc) - timedelta(minutes=5)
    # a.jsonl：user + 短 assistant + 长 assistant（终稿候选）
    _write_session(sess, "a.jsonl", [
        _line("u1", "user", [_txt("用户问题")], t0),
        _line("a1", "assistant", [_txt(SHORT)], t0 + timedelta(seconds=1)),
        _line("a2", "assistant", [_txt(LONG)], t0 + timedelta(seconds=2)),
    ])
    cand, sid = scan.newest_candidate()
    assert cand is not None and sid == "a", (cand, sid)
    assert cand["id"] == "a2" and cand["len"] == len(LONG)
    assert cand["content"] == LONG
    assert cand["ts_ms"] == scan._parse_ts(_iso(t0 + timedelta(seconds=2))), cand
    # 带 toolCall 的中间步（即使 text 长）不是终稿 → 跳过
    t1 = t0 + timedelta(seconds=10)
    _write_session(sess, "a.jsonl", [_line("a3", "assistant", [_tool(), _txt(LONG)], t1)], mode="a")
    cand, sid = scan.newest_candidate()
    assert cand["id"] == "a2", f"toolCall 中间步应被跳过: {cand}"
    # 新 session 更新 → 取全库最新
    t2 = t0 + timedelta(seconds=20)
    _write_session(sess, "b.jsonl", [_line("b1", "assistant", [_txt(LONG)], t2)])
    cand, sid = scan.newest_candidate()
    assert cand["id"] == "b1" and sid == "b", (cand, sid)
    # 损坏 JSON 行 → 跳过不炸
    with open(os.path.join(sess, "c.jsonl"), "w", encoding="utf-8") as f:
        f.write("{not json\n")
    cand, sid = scan.newest_candidate()
    assert cand["id"] == "b1", cand
    # 显式 min_len 仍可按门槛过滤（兼容旧语义：高于现有文本长度 → 无候选）
    cand, sid = scan.newest_candidate(min_len=len(LONG) + 1)
    assert cand is None and sid is None, (cand, sid)
    print("  ✅ newest_candidate：user/toolCall 中间步/坏行跳过，多 session 取最新，字段完整；min_len 可配")


def test_default_full_intervention_short_and_placeholders():
    root, sess, logs = _fresh_dirs()
    t0 = datetime.now(timezone.utc) - timedelta(minutes=5)
    # 短回复（旧 150 门槛会漏审）→ 默认（min_len=1）应入选
    _write_session(sess, "s1.jsonl", [_line("s1-e1", "assistant", [_txt("好的，我修好了。")], t0)])
    cand, sid = scan.newest_candidate()
    assert cand is not None and cand["id"] == "s1-e1", (cand, sid)
    # NO_REPLY / 纯符号：即使更新也不入选
    _write_session(sess, "s1.jsonl",
                   [_line("s1-e2", "assistant", [_txt("NO_REPLY")], t0 + timedelta(seconds=1))], mode="a")
    cand, sid = scan.newest_candidate()
    assert cand["id"] == "s1-e1", cand
    _write_session(sess, "s1.jsonl",
                   [_line("s1-e3", "assistant", [_txt("\U0001f99e\U0001f44c\u2705")], t0 + timedelta(seconds=2))], mode="a")
    cand, sid = scan.newest_candidate()
    assert cand["id"] == "s1-e1", cand
    # 阈值仍可用参数控制：显式 150 → 短回复被过滤，无候选
    cand2, _ = scan.newest_candidate(min_len=150)
    assert cand2 is None, cand2
    print("  ✅ 全量介入默认：短回复入选、NO_REPLY/纯符号跳过、min_len 可显式控制")


def test_stale_file_skipped():
    root, sess, logs = _fresh_dirs()
    t0 = datetime.now(timezone.utc) - timedelta(minutes=5)
    path = _write_session(sess, "old.jsonl", [_line("o1", "assistant", [_txt(LONG)], t0)])
    # 文件 mtime 改成 48h 前 → 36h 窗口外，整体跳过
    old = time.time() - 48 * 3600
    os.utime(path, (old, old))
    cand, sid = scan.newest_candidate()
    assert cand is None and sid is None, (cand, sid)
    # 目录不存在 → (None, None)
    scan.SESS_DIR = os.path.join(root, "no-such-dir")
    cand, sid = scan.newest_candidate()
    assert cand is None and sid is None
    print("  ✅ 36h 窗口外文件跳过、SESS_DIR 不存在安全返回")


def test_find_new_cursor_flows():
    root, sess, logs = _fresh_dirs()
    t0 = datetime.now(timezone.utc) - timedelta(minutes=5)
    _write_session(sess, "a.jsonl", [_line("a1", "assistant", [_txt(LONG)], t0)])
    # 无 cursor → 首次初始化（从当前最新盯起），不回溯返回
    cand, sid = scan.find_new()
    assert cand is None and sid is None, (cand, sid)
    assert json.load(open(scan.CURSOR))["ts_ms"] == scan._parse_ts(_iso(t0)), "cursor 应初始化为最新 ts"
    # 新终稿 → 返回；调用方审完后 set_cursor 推进 → 无新稿 None
    t1 = t0 + timedelta(seconds=30)
    _write_session(sess, "b.jsonl", [_line("b1", "assistant", [_txt(LONG)], t1)])
    cand, sid = scan.find_new()
    assert cand is not None and cand["id"] == "b1" and sid == "b", (cand, sid)
    scan.set_cursor(cand["ts_ms"])  # 模拟调用方（cli cmd_scan）审完推进 cursor
    cand, sid = scan.find_new()
    assert cand is None and sid is None, "cursor 已到最新，不应重复返回"
    print("  ✅ find_new：首次初始化不回溯、新稿返回、无新稿 None")


def test_cursor_corrupt_autoreset():
    root, sess, logs = _fresh_dirs()
    t0 = datetime.now(timezone.utc) - timedelta(minutes=5)
    _write_session(sess, "a.jsonl", [_line("a1", "assistant", [_txt(LONG)], t0)])
    with open(scan.CURSOR, "w", encoding="utf-8") as f:
        f.write("{broken json!!")
    # 损坏 cursor → 视同首次：自动重置 + 初始化，不回溯轰炸
    cand, sid = scan.find_new()
    assert cand is None and sid is None, (cand, sid)
    d = json.load(open(scan.CURSOR))
    assert d["ts_ms"] == scan._parse_ts(_iso(t0)), f"损坏 cursor 应被自动重置: {d}"
    # P4.1：重置应记事件日志（可追溯）
    ev = os.path.join(logs, "scan-events.jsonl")
    assert os.path.exists(ev), "损坏 cursor 应写 scan-events.jsonl"
    e = json.loads(open(ev, encoding="utf-8").readline())
    assert e["event"] == "cursor_reset" and e["detail"] == "corrupt_json", e
    print("  ✅ cursor 损坏：自动重置 + scan-events.jsonl 记录事件（P4.1）")


def test_session_kind_exclusion():
    root, sess, logs = _fresh_dirs()
    t0 = datetime.now(timezone.utc) - timedelta(minutes=3)
    # sessions.json（真实形态：sessionKey → {sessionId, ...}）
    idx = {
        "s-main": "agent:main:main",
        "s-dash": "agent:main:dashboard:de96de24-83a0-42f7-bdbb-f70f2f65e007",
        "s-cron": "agent:main:cron:9a566bf2-83c2-49e8-b4f2-94fdedeacf17:run:6057119a",
        "s-sub": "agent:main:subagent:8124bcbe-9854-46ca-a76f-864160a1b332",
        "s-wx": "agent:main:openclaw-weixin:group:o9cq809owasy6d5xjf6pxysmpggk@im.wechat",
    }
    reg = {k: {"sessionId": sid, "updatedAt": 1788600000000} for sid, k in idx.items()}
    with open(os.path.join(sess, "sessions.json"), "w", encoding="utf-8") as f:
        json.dump(reg, f)
    t = t0
    for sid in ("s-main", "s-dash", "s-cron", "s-sub", "s-wx"):
        _write_session(sess, f"{sid}.jsonl", [_line(sid + "-e1", "assistant", [_txt(LONG)], t)])
        t += timedelta(seconds=5)
    # cron/subagent 时间戳更晚但应被排除；允许对象里 wx 最新 → 选 s-wx
    cand, sid = scan.newest_candidate()
    assert cand is not None and sid == "s-wx", f"cron/subagent 应排除: {sid}"
    # .trajectory.jsonl 是 trace 非转录：即使含 message 行也不入选
    _write_session(sess, "zz.trajectory.jsonl",
                   [_line("tr1", "assistant", [_txt(LONG)], t + timedelta(seconds=5))])
    cand, sid = scan.newest_candidate()
    assert cand is not None and sid == "s-wx", f"trajectory 不应入选: {sid}"
    # 未登记 sessions.json 的孤儿文件（如 main 旧化身）→ 放行
    _write_session(sess, "s-unknown.jsonl",
                   [_line("u1", "assistant", [_txt(LONG)], t + timedelta(seconds=10))])
    cand, sid = scan.newest_candidate()
    assert cand is not None and sid == "s-unknown", f"未登记文件应放行: {sid}"
    # 兜底①：删掉 sessions.json 后无轨迹可查 → 回退不过滤（全放行）
    os.remove(os.path.join(sess, "sessions.json"))
    cand, sid = scan.newest_candidate()
    assert cand is not None and sid == "s-unknown", "无 sessions.json 应回退不过滤"
    # 兜底②：真实形态——sessions.json 条目被清理但 trajectory 元数据仍在（50 转录里 37 孤儿 cron）
    def _traj_meta(session_key):
        return json.dumps({"type": "trace.metadata", "seq": 2, "sessionKey": session_key,
                           "sessionId": "x"}) + "\n"

    for sid, key in (("s-cron", idx["s-cron"]), ("s-sub", idx["s-sub"]),
                     ("s-wx", idx["s-wx"])):
        with open(os.path.join(sess, f"{sid}.trajectory.jsonl"), "w", encoding="utf-8") as fh:
            fh.write(_traj_meta(key))
    # cron/sub 转录比 s-unknown 更新，但凭 trajectory 兜底仍应排除
    _write_session(sess, "s-cron.jsonl", [_line("cron2", "assistant", [_txt(LONG)], t + timedelta(seconds=50))])
    _write_session(sess, "s-sub.jsonl", [_line("sub2", "assistant", [_txt(LONG)], t + timedelta(seconds=55))])
    cand, sid = scan.newest_candidate()
    assert cand is not None and sid == "s-unknown", f"trajectory 兜底应排除 cron/sub: {sid}"
    # 兜底不应误伤交互会话：wx（openclaw-weixin 渠道）更新 → 入选
    _write_session(sess, "s-wx.jsonl", [_line("wx2", "assistant", [_txt(LONG)], t + timedelta(seconds=60))])
    cand, sid = scan.newest_candidate()
    assert cand is not None and sid == "s-wx", f"trajectory 兜底误伤渠道会话: {sid}"
    print("  ✅ session 排除：cron/subagent 不入选、trajectory 跳过、孤儿放行、无索引回退、trajectory 兑底（P4.3）")


def test_lock_logic():
    root, sess, logs = _fresh_dirs()
    assert scan.locked() is False, "无锁文件 → 未锁"
    scan.set_lock()
    assert scan.locked() is True, "set_lock 后应立即 locked"
    # 模拟 60s TTL 过期：mtime 改到 2 分钟前
    old = time.time() - 120
    os.utime(scan.LOCK, (old, old))
    assert scan.locked() is False, "超 60s 的锁应视为过期"
    # 锁文件异常（路径不可 stat，如父目录不存在）→ 不炸，视为未锁
    scan.LOCK = os.path.join(logs, "no-such-sub", ".scan-lock")
    assert scan.locked() is False
    print("  ✅ lock：set 后锁定、60s 过期解锁、异常安全")


def main():
    print("== scan 转录扫描器离线测试（零 API）==")
    test_parse_ts()
    test_candidate_extraction()
    test_default_full_intervention_short_and_placeholders()
    test_stale_file_skipped()
    test_find_new_cursor_flows()
    test_cursor_corrupt_autoreset()
    test_session_kind_exclusion()
    test_lock_logic()
    print("== scan 全部通过 ✅ ==")


if __name__ == "__main__":
    main()
