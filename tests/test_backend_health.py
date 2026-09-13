#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_backend_health.py — 检索后端健康状态离线测试（v0.11.0 · 零网络）

背景（2026-09-13 实测）：
  · sogou 返回 5.5KB 反爬页（含 window.imgCode / checkSNUID），baidu 返回「百度安全验证
    网络不给力」1.4KB，二者稳定 0 条；只有 bing 可用。
  · 旧实现把这些都记成"0 条"→ 证据门静默判 insufficient（"没有证据"），而真相是"根本没
    检索成"（基础设施故障）。两者含义完全不同，必须分开。
本测试锁定：反爬页 → blocked；结构不匹配 → unparsed；能解析 → ok；全后端被拦截且 0 片段 →
evidence_check 判 error（保守阻断 + 如实说明），而不是 insufficient。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mjc import knowledge, repair

SOGOU_BLOCK = ('<html><script>window.imgCode = -1; function checkSNUID(){}</script>'
               '<body>请输入验证码以继续访问</body></html>')
BAIDU_BLOCK = '<html><body>百度安全验证 网络不给力，请稍后重试 返回首页 问题反馈</body></html>'
BING_OK = ('<ol><li class="b_algo"><h2>香港平安鐘協會有限公司</h2>'
           '<p>香港平安鐘協會有限公司由 2009 年成立至今，致力為社會的有需要人士提供支援服務。</p>'
           '</li></ol>')
SO360_OK = ('<ul><li class="res-list"><style>.x{font-size:.83em}</style>'
            '<h3>香港平安鐘協會有限公司</h3>'
            '<p>香港平安鐘協會有限公司由 2009 年成立至今，服務長者。 hongkongssa.com</p></li></ul>')
SO360_JUNK = ('<ul><li class="res-list"><style>.g-v-feedback{box-shadow:0 4px 16px 0 rgba(0,0,0,.12)}'
              '</style><script>var t="v=20200925b";</script></li></ul>')
BLANK_OK = '<html><body>' + ('普通页面内容，没有结果块。' * 20) + '</body></html>'


def run():
    ok = True

    def check(name, cond):
        nonlocal ok
        print(("  ✅ " if cond else "  ❌ ") + name)
        if not cond:
            ok = False

    # ---- 1) 反爬指纹识别 ----
    check("sogou 反爬页 → 命中指纹", knowledge._detect_block(SOGOU_BLOCK) is not None)
    check("baidu 安全验证页 → 命中指纹", knowledge._detect_block(BAIDU_BLOCK) is not None)
    check("正常结果页 → 不误判", knowledge._detect_block(BING_OK) is None)
    check("so360 正常结果页 → 不误判", knowledge._detect_block(SO360_OK) is None)

    # ---- 1c) 片段提取必须剥掉 script/style/注释（否则值匹配会命中版本号等噪音）----
    noisy = knowledge._strip_tags(SO360_JUNK)
    check("_strip_tags 剥掉 <style> 内容（CSS 不进片段）", "box-shadow" not in noisy and "feedback" not in noisy)
    check("_strip_tags 剥掉 <script> 内容（JS 不进片段）", "20200925" not in noisy and "var t" not in noisy)
    keep = knowledge._strip_tags(SO360_OK)
    check("_strip_tags 保留正文证据文本", "2009" in keep and "hongkongssa.com" in keep)

    # ---- 1b) 状态语义边界：401/403/429 = 拒绝服务 → blocked；其它 → error ----
    import urllib.error

    knowledge.BACKEND_STATUS.clear()
    knowledge._note_exception("sogou", urllib.error.HTTPError("u", 403, "Forbidden", {}, None))
    check("HTTP 403 → blocked（拒绝服务，不是 generic error）",
          knowledge.BACKEND_STATUS["sogou"]["status"] == "blocked")
    knowledge._note_exception("x", urllib.error.HTTPError("u", 429, "Too Many Requests", {}, None))
    check("HTTP 429 → blocked（限流）", knowledge.BACKEND_STATUS["x"]["status"] == "blocked")
    knowledge._note_exception("y", TimeoutError("timed out"))
    check("超时 → error（非封禁）", knowledge.BACKEND_STATUS["y"]["status"] == "error")
    knowledge.BACKEND_STATUS.clear()

    old_get = knowledge._http_get
    old_fetch = knowledge.fetch_evidence
    saved_status = dict(knowledge.BACKEND_STATUS)

    def fake_get(html):
        return lambda url, timeout: html

    try:
        # ---- 2) 后端状态：blocked / unparsed / ok ----
        knowledge.BACKEND_STATUS.clear()
        knowledge._http_get = fake_get(SOGOU_BLOCK)
        sn = knowledge._back_sogou("q", 5)
        check("sogou 反爬 → 返回 0 条且 status=blocked",
              sn == [] and knowledge.BACKEND_STATUS["sogou"]["status"] == "blocked")

        knowledge._http_get = fake_get(BAIDU_BLOCK)
        sn = knowledge._back_baidu("q", 5)
        check("baidu 反爬 → 返回 0 条且 status=blocked",
              sn == [] and knowledge.BACKEND_STATUS["baidu"]["status"] == "blocked")

        knowledge._http_get = fake_get(BLANK_OK)
        sn = knowledge._back_sogou("q", 5)
        check("无结果块（非反爬）→ status=unparsed（≠ blocked）",
              sn == [] and knowledge.BACKEND_STATUS["sogou"]["status"] == "unparsed")

        knowledge._http_get = fake_get(BING_OK)
        sn = knowledge._back_bing("q", 5)
        check("bing 正常解析 → status=ok 且有片段",
              len(sn) == 1 and knowledge.BACKEND_STATUS["bing"]["status"] == "ok")
        check("解析出的片段含官网证据", "2009" in sn[0]["text"])

        knowledge.BACKEND_STATUS.clear()
        knowledge._http_get = fake_get(SO360_OK)
        sn = knowledge._back_so360("q", 5)
        check("so360 正常解析 → status=ok 且片段无 CSS 噪音",
              len(sn) == 1 and knowledge.BACKEND_STATUS["so360"]["status"] == "ok"
              and "box-shadow" not in sn[0]["text"] and "2009" in sn[0]["text"])

        knowledge._http_get = fake_get(SOGOU_BLOCK)
        sn = knowledge._back_so360("q", 5)
        check("so360 反爬 → status=blocked 且 0 条",
              sn == [] and knowledge.BACKEND_STATUS["so360"]["status"] == "blocked")

        # ---- 3) merge 结果带 backend_status ----
        knowledge.BACKEND_STATUS.clear()
        knowledge._http_get = fake_get(BING_OK)

        def _selftest_backend(q, t):
            knowledge._note_status("_selftest", "ok")
            return [{"title": "t", "url": "", "text": "香港平安鐘協會有限公司由 2009 年成立至今。"}]

        knowledge.BACKENDS["_selftest"] = _selftest_backend
        try:
            r = knowledge.search("q", backends=["_selftest", "nosuch"], merge=True)
            check("merge 结果携带 backend_status",
                  isinstance(r, dict) and r.get("backend_status", {}).get("_selftest", {}).get("status") == "ok")
        finally:
            knowledge.BACKENDS.pop("_selftest", None)

        # ---- 4) evidence_check：全后端被拦截 ≠ 没有证据 ----
        knowledge.BACKEND_STATUS.clear()

        def all_blocked(query, *a, **k):
            knowledge._note_status("sogou", "blocked", "反爬页指纹: checksnuid")
            knowledge._note_status("baidu", "blocked", "反爬页指纹: 安全验证")
            return None

        knowledge.fetch_evidence = all_blocked
        r = repair.evidence_check("请用一句话以内回答下面的问题：\n香港平安钟协会最早成立于哪一年？",
                                  {"1997"}, {"2009"}, min_snippets=2)
        check("全后端被拦截 → verdict=error（不是 insufficient）", r["verdict"] == "error")
        check("note 说明是反爬拦截", "拦截" in (r.get("note") or ""))
        check("backends 状态随结果外显", (r.get("backends") or {}).get("sogou", {}).get("status") == "blocked")

        def bing_junk(query, *a, **k):
            knowledge._note_status("bing", "ok")
            return {"backend": "bing", "snippets": [{"title": "香港", "text": "香港特别行政区简介"}]}

        knowledge.fetch_evidence = bing_junk
        r2 = repair.evidence_check("题目", {"1997"}, {"2009"}, min_snippets=2)
        check("有健康后端但无支持 → insufficient（保持原语义）", r2["verdict"] == "insufficient")
        check("部分拦截时 note 说明", "拦截" in (r2.get("note") or "") or r2.get("note") is None)

        def nothing(query, *a, **k):
            return None

        knowledge.fetch_evidence = nothing
        r3 = repair.evidence_check("题目", {"1997"}, {"2009"}, min_snippets=2)
        check("无任何后端状态且无结果 → insufficient（既有行为不变）", r3["verdict"] == "insufficient")

        # ---- 6) 反爬冷却：不对刚封你的后端立刻重试（实测会加重封禁）----
        knowledge.BACKEND_STATUS.clear()
        knowledge._BLOCKED_AT.clear()
        knowledge._LAST_CALL.clear()
        calls = {"n": 0}

        def blocked_backend(q, t):
            calls["n"] += 1
            knowledge._note_status("_cd", "blocked", "测试用反爬")
            return []

        knowledge.BACKENDS["_cd"] = blocked_backend
        try:
            knowledge.search("q", backends=["_cd"], merge=True, retry_empty=True)
            check("刚返回 blocked 的后端不被立刻重试（只调用 1 次）", calls["n"] == 1)
            check("该后端进入反爬冷却", knowledge._in_cooldown("_cd"))
            calls["n"] = 0
            knowledge.search("q", backends=["_cd"], merge=True)
            check("冷却期内直接跳过，不再发起请求", calls["n"] == 0)
            st = knowledge.backend_health().get("_cd") or {}
            check("跳过也记 blocked（上层据此判 error 而非『无证据』）", st.get("status") == "blocked")
        finally:
            knowledge.BACKENDS.pop("_cd", None)
            knowledge._BLOCKED_AT.clear()
            knowledge.BACKEND_STATUS.clear()

        # ---- 5) cmd 插件口（接正规搜索 API 的唯一通道）状态可见 ----
        knowledge.BACKEND_STATUS.clear()
        old_env = os.environ.pop("MJC_KNOWLEDGE_CMD", None)
        try:
            out = knowledge._back_cmd("q", 5)
            check("未配置 MJC_KNOWLEDGE_CMD → 空且不记状态（不算故障）",
                  out == [] and "cmd" not in knowledge.BACKEND_STATUS)

            os.environ["MJC_KNOWLEDGE_CMD"] = ("printf '%s' "
                                               "'{\"snippets\":[{\"title\":\"t\",\"url\":\"u\","
                                               "\"text\":\"协会由 2009 年成立至今\"}]}'")
            out = knowledge._back_cmd("q", 5)
            check("cmd 返回 JSON → 解析成功且 status=ok",
                  len(out) == 1 and "2009" in out[0]["text"]
                  and knowledge.BACKEND_STATUS["cmd"]["status"] == "ok")

            os.environ["MJC_KNOWLEDGE_CMD"] = "true"  # 成功但无输出
            out = knowledge._back_cmd("q", 5)
            check("cmd 无输出 → status=error（可见，而非静默 0 条）",
                  out == [] and knowledge.BACKEND_STATUS["cmd"]["status"] == "error")

            os.environ["MJC_KNOWLEDGE_CMD"] = "printf '%s' '纯文本证据 2009 年成立'"
            out = knowledge._back_cmd("q", 5)
            check("cmd 非 JSON → 按纯文本单条片段处理",
                  len(out) == 1 and "2009" in out[0]["text"])
        finally:
            os.environ.pop("MJC_KNOWLEDGE_CMD", None)
            if old_env is not None:
                os.environ["MJC_KNOWLEDGE_CMD"] = old_env
    finally:
        knowledge.fetch_evidence = old_fetch
        knowledge._http_get = old_get
        knowledge.BACKEND_STATUS.clear()
        knowledge.BACKEND_STATUS.update(saved_status)

    print("== 后端健康状态（v0.11.0）全部通过 ✅ ==" if ok else "== ❌ 有失败 ==")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
