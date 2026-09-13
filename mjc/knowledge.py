#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MJC · knowledge.py — 外部知识源（事实核查的证据检索层，零幻觉架构第二块拼图）

定位：给「事实仲裁」提供外部证据片段，让“修复/否证”有据可依，而不是纯凭模型记忆。
设计原则：
  - 可插拔后端（按序尝试，失败降级）：cmd（用户自定义命令）> sogou > bing > baidu（HTML 抓取，尽力而为）
  - v0.9.1 merge 模式（证据门默认路径）：多后端累积 + 去重（URL 或归一化文本相同视为同一
    snippet）、累积 ≥ min_total（默认 3）或试满 max_backends（默认 3）即停、空结果对同后端
    重试 1 次、单后端失败不中断；merge=False（默认）严格保持原「首个有结果即返回」行为
  - 默认关闭（off）：显式配置后才联网（保持“零依赖、纯本地”的默认姿态）
  - 守规矩：单次审查最多 N 次检索、同 query 24h 进程级缓存、后端间隔限速、可配超时
  - 证据只是线索：可能不相关/低质，消费方（仲裁）必须自行判别
  - **v0.11.0 后端健康状态（重要）**：抓取型后端会被反爬拦截，而旧实现把"被拦截"和"真的没
    检索到"都记成 0 条 → 证据门静默判 insufficient，真因不可见（2026-09-13 实测：sogou 返回
    HTTP 403、baidu 返回「百度安全验证」1.4KB，二者稳定 0 条）。
    现在每后端记录 status 并随结果返回 `backend_status`；`evidence_check` 据此区分"检索不到
    证据"与"根本没检索成"，后者判 error（保守阻断 + 如实说明）。
    **状态语义边界（勿混用）**：
      · ok        — 成功解析出 ≥1 条片段
      · blocked   — **服务可达但拒绝服务**：反爬验证页指纹，或 HTTP 401/403/429（限流/封禁）
      · unparsed  — 拿到了页面但结果块正则 0 命中（页面结构变化 / 空页），非反爬
      · empty     — 后端显式返回空（保留位）
      · error     — **其它故障**：超时、DNS、连接失败、5xx、解析异常（非"被封"）

配置（settings.json，可省略）：
  "knowledge": {"enabled": false, "backends": ["sogou","bing","baidu"], "max_per_review": 3, "timeout": 12}
或环境变量 MJC_KNOWLEDGE=off|sogou,bing,baidu ；MJC_KNOWLEDGE_CMD="<命令模板，含 {query}>"

API：fetch_evidence(query) -> {"backend": str, "snippets": [{title,url,text}], "fetched": int}
     fetch_evidence(query, merge=True)（v0.9.1）-> 多后端合并结果：
         {"backend": "sogou+bing", "snippets": [...], "fetched": int, "backends_tried": [...],
          "backend_status": {name: {status, note, at}}}（backend_status 为 v0.11.0 新增）
     backend_health() -> 最近一次各后端状态快照（探活用）
"""
import html
import json
import os
import re
import subprocess
import time
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126 Safari/537.36")

_CACHE = {}          # query -> (ts, result)
_CACHE_TTL = 24 * 3600
_LAST_CALL = {}       # backend -> ts（限速）
_MIN_INTERVAL = 2.0   # 同后端两次请求最小间隔（秒）
# v0.11.0c：被反爬拦截后的冷却（秒）。实测 so360 被封后 ~180s 恢复；冷却期内直接跳过该后端，
# 既省延迟也不再加重封禁（旧实现会对刚返回 blocked 的后端**立刻再打一次**）。
_BLOCK_COOLDOWN = 180.0
_BLOCKED_AT = {}      # backend -> ts（最近一次被判 blocked 的时刻）


def _block_cooldown():
    """冷却秒数：settings.knowledge.blocked_cooldown（默认 180，实测 so360 恢复时间）。"""
    try:
        v = _settings_cfg().get("blocked_cooldown")
        return max(0.0, float(v)) if v is not None else _BLOCK_COOLDOWN
    except Exception:
        return _BLOCK_COOLDOWN


def _in_cooldown(name):
    ts = _BLOCKED_AT.get(name)
    return bool(ts) and (time.time() - ts) < _block_cooldown()


def _mark_blocked(name):
    _BLOCKED_AT[name] = time.time()

BACKEND_STATUS = {}   # backend -> {"status","note","at"}（v0.11.0 健康状态，供门/探活消费）

# 反爬/验证页指纹（命中即判 blocked，而非"没检索到"）
BLOCK_MARKERS = (
    "安全验证", "网络不给力", "请输入验证码", "验证码", "window.imgcode", "checksnuid",
    "captcha", "robot check", "unusual traffic", "访问过于频繁",
)


def _note_status(name, status, note="", mark=True):
    BACKEND_STATUS[name] = {"status": status, "note": (note or "")[:160], "at": time.time()}
    if status == "blocked" and mark:
        _mark_blocked(name)


def _note_exception(name, exc):
    """异常 → 状态分类（v0.11.0 语义边界，见模块 docstring）：
    401/403/429 = 服务可达但拒绝服务（反爬/限流）→ blocked；其余（超时/DNS/解析/5xx）→ error。
    这样 sogou 的 HTTP 403 与 baidu 的验证页归入同一语义：都是"被封了"，而不是"没检索到"。"""
    code = getattr(exc, "code", None)
    txt = str(exc)[:120]
    if code in (401, 403, 429) or re.search(r"\b(401|403|429)\b", txt[:60]):
        _note_status(name, "blocked", f"HTTP {code or '?'} 拒绝服务（反爬/限流）: {txt[:80]}")
    else:
        _note_status(name, "error", f"{type(exc).__name__}: {txt}")


def _detect_block(html_text):
    """返回反爬拦截原因（str）或 None。用于区分"被拦截"与"没结果"。"""
    t = (html_text or "")[:20000].lower()
    for m in BLOCK_MARKERS:
        if m in t:
            return m
    return None


def backend_health():
    """最近一次各后端状态快照（探活用）：{name: {status, note, at}}"""
    return {k: dict(v) for k, v in BACKEND_STATUS.items()}


TAG_RE = re.compile(r"<[^>]+>")
# v0.11.0c：剥 script/style/注释——否则片段文本混入 JS/CSS，值匹配会命中版本号等噪音
# （实测：so.com 首个结果块纯为 CSS；bing 页面含 "v=20200925b"，
#  子串匹配 "2009" in "20200925" 为 True → 证据门可能凭版本号"支持"一个值替换）
NOISE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>", re.S | re.I)
COMMENT_RE = re.compile(r"<!--.*?-->", re.S)


def _settings_cfg():
    try:
        from mjc import settings
        return (settings.load().get("knowledge") or {})
    except Exception:
        return {}


def configured_backends():
    """解析生效后端列表：env 优先 > settings > 默认 off"""
    env = os.environ.get("MJC_KNOWLEDGE", "").strip()
    if env:
        raw = env
    else:
        cfg = _settings_cfg()
        if not cfg.get("enabled"):
            return []
        raw = ",".join(cfg.get("backends") or [])
    out = []
    for b in raw.split(","):
        b = b.strip().lower()
        if b and b != "off" and b not in out:
            out.append(b)
    return out


def _limits():
    cfg = _settings_cfg()
    try:
        max_n = int(cfg.get("max_per_review", 3))
    except Exception:
        max_n = 3
    try:
        timeout = float(cfg.get("timeout", 12))
    except Exception:
        timeout = 12.0
    return max(1, min(max_n, 6)), max(4.0, min(timeout, 30.0))


def _strip_tags(s):
    s = NOISE_RE.sub(" ", s or "")
    s = COMMENT_RE.sub(" ", s)
    return re.sub(r"\s+", " ", html.unescape(TAG_RE.sub(" ", s))).strip()


_IPV4_PATCHED = False


def _prefer_ipv4():
    """v0.10 P3：本机 IPv6 路由不稳（2026-09-13 实测：连接卡在 SYN-SENT 直到超时）
    → 检索层优先 IPv4。惰性生效（仅在真正发起检索时打补丁，避免影响无关进程）。
    只重排 getaddrinfo 结果（IPv4 在前、IPv6 兵底），不删地址；env MJC_KNOWLEDGE_IPV4=0 可关。"""
    global _IPV4_PATCHED
    if _IPV4_PATCHED:
        return
    _IPV4_PATCHED = True
    if os.environ.get("MJC_KNOWLEDGE_IPV4", "1").strip().lower() in ("0", "off", "false", "no"):
        return
    import socket as _socket
    orig = _socket.getaddrinfo
    if getattr(orig, "_mjc_ipv4", False):
        return

    def _gai(*a, **k):
        res = orig(*a, **k)
        try:
            v4 = [r for r in res if r and r[0] == _socket.AF_INET]
            v6 = [r for r in res if r and r[0] != _socket.AF_INET]
            return (v4 + v6) if v4 else res
        except Exception:
            return res

    _gai._mjc_ipv4 = True
    _socket.getaddrinfo = _gai


def _http_get(url, timeout):
    _prefer_ipv4()
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read(600 * 1024)
    for enc in ("utf-8", "gb18030", "latin-1"):
        try:
            return data.decode(enc)
        except Exception:
            continue
    return ""


def _pack_blocks(blocks, limit=6):
    """把抓取到的 HTML 结果块打包成 snippets（统一长度门槛）。"""
    out = []
    for b in (blocks or [])[:limit]:
        txt = _strip_tags(b)
        if txt and len(txt) > 30:
            out.append({"title": txt[:90], "url": "", "text": txt[:400]})
    return out


def _back_sogou(query, timeout):
    h = _http_get("https://www.sogou.com/web?query=" + urllib.parse.quote(query), timeout)
    reason = _detect_block(h)
    if reason:
        _note_status("sogou", "blocked", f"反爬页指纹: {reason}")
        return []
    blocks = re.findall(r'<div class="vrwrap"[^>]*>(.*?)(?=<div class="vrwrap"|$)', h, re.S)
    out = _pack_blocks(blocks)
    _note_status("sogou", "ok" if out else "unparsed",
                 "" if out else f"HTML {len(h)} 字符但命中 0 个 vrwrap 块")
    return out


def _back_bing(query, timeout):
    h = _http_get("https://cn.bing.com/search?q=" + urllib.parse.quote(query), timeout)
    reason = _detect_block(h)
    if reason:
        _note_status("bing", "blocked", f"反爬页指纹: {reason}")
        return []
    blocks = re.findall(r'<li class="b_algo".*?</li>', h, re.S)
    out = _pack_blocks(blocks)
    _note_status("bing", "ok" if out else "unparsed",
                 "" if out else f"HTML {len(h)} 字符但命中 0 个 b_algo 块")
    return out


def _back_baidu(query, timeout):
    h = _http_get("https://www.baidu.com/s?ie=utf-8&wd=" + urllib.parse.quote(query), timeout)
    reason = _detect_block(h)
    if reason:
        _note_status("baidu", "blocked", f"反爬页指纹: {reason}")
        return []
    if len(h) < 5000:
        _note_status("baidu", "unparsed", f"HTML 仅 {len(h)} 字符（疑似限流/空页）")
        return []
    blocks = re.findall(r'<div[^>]*class="result[^"]*c-container[^"]*"[^>]*>(.*?)</div>\s*</div>', h, re.S)
    out = _pack_blocks(blocks)
    _note_status("baidu", "ok" if out else "unparsed",
                 "" if out else f"HTML {len(h)} 字符但命中 0 个 result 块")
    return out


def _back_so360(query, timeout):
    """360 搜索（v0.11.0c 新增）：2026-09-13 实测**唯一免 key 且可用的通用搜索后端**
    （sogou 403、baidu 反爬、bing 召回差、境外端点不可达）。csqa-07 实测其第 2 条结果块即
    协会官网 hongkongssa.com「由 2009 年成立至今」——真·独立证据。"""
    h = _http_get("https://www.so.com/s?q=" + urllib.parse.quote(query), timeout)
    reason = _detect_block(h)
    if reason:
        _note_status("so360", "blocked", f"反爬页指纹: {reason}")
        return []
    blocks = re.findall(r'<li class="res-list.*?</li>', h, re.S)
    out = _pack_blocks(blocks)
    _note_status("so360", "ok" if out else "unparsed",
                 "" if out else f"HTML {len(h)} 字符但命中 0 个 res-list 块")
    return out


def _back_cmd(query, timeout):
    """自定义检索后端（插件口）：`MJC_KNOWLEDGE_CMD="<命令模板，含 {query}>"`。

    v0.11.0 起这是**推荐的证据来源**：三个抓取型后端已实测不可用（sogou 403 / baidu 反爬 /
    bing 召回差），境外端点（ddg-lite/startpage）本机不可达。接一个正规搜索 API 只需：
        export MJC_KNOWLEDGE_CMD='curl -s "https://<api>/search?q={query}&key=$KEY"'
    约定：stdout 输出 JSON —— {"snippets":[{"title","url","text"},...]} 或直接数组；
    非 JSON 时整段纯文本当作单条片段。
    状态：未配置 → 不记状态（不算故障）；配置了但失败/无输出 → error/unparsed（可见）。
    """
    tpl = os.environ.get("MJC_KNOWLEDGE_CMD", "").strip()
    if not tpl:
        return []
    cmd = tpl.replace("{query}", query)
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout + 5)
    except Exception as e:  # noqa
        _note_status("cmd", "error", f"{type(e).__name__}: {str(e)[:120]}")
        return []
    try:
        d = json.loads(p.stdout)
        if isinstance(d, dict):
            d = d.get("snippets") or []
        out = [{"title": str(s.get("title", ""))[:90], "url": str(s.get("url", "")),
                "text": str(s.get("text", ""))[:400]} for s in d[:6]]
        _note_status("cmd", "ok" if out else "unparsed",
                     "" if out else "命令成功但返回 0 条片段")
        return out
    except Exception:
        txt = (p.stdout or "").strip()
        if txt:
            _note_status("cmd", "ok", "非 JSON 输出，按纯文本片段处理")
            return [{"title": "", "url": "", "text": txt[:400]}]
        _note_status("cmd", "error",
                     f"命令无有效输出 (exit={p.returncode}) {(p.stderr or '').strip()[:80]}")
        return []


BACKENDS = {"so360": _back_so360, "sogou": _back_sogou, "bing": _back_bing,
            "baidu": _back_baidu, "cmd": _back_cmd}


def _norm_snippet(s):
    """snippet 文本归一化（去重键用）：大小写折叠 + 去除全部空白。"""
    return re.sub(r"\s+", "", str(s or "").casefold())


def _snippet_keys(sn):
    """snippet 去重键：URL 或归一化文本（任一相同即视为同一片段）。"""
    keys = []
    url = str(sn.get("url") or "").strip().lower()
    if url:
        keys.append("u:" + url)
    txt = _norm_snippet(sn.get("text"))
    if txt:
        keys.append("t:" + txt)
    return keys


def _merge_extend(merged, seen, snips):
    """把 snips 去重后并入 merged（跨后端不重复计数）；返回新增条数。"""
    added = 0
    for sn in snips:
        if not isinstance(sn, dict):
            continue
        keys = _snippet_keys(sn)
        if keys and any(k in seen for k in keys):
            continue
        for k in keys:
            seen.add(k)
        merged.append(sn)
        added += 1
    return added


def _merge_try_backend(name, fn, query, timeout):
    """merge 模式单次后端调用（同限速策略）；返回 (snips, ok)，异常 → ([], False)。"""
    gap = time.time() - _LAST_CALL.get(name, 0)
    if gap < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - gap)
    try:
        _LAST_CALL[name] = time.time()
        return list(fn(query, timeout) or []), True
    except Exception as e:  # noqa
        _note_exception(name, e)
        return [], False


def _parallel_enabled():
    """v0.10 P3b：多后端并行检索开关（默认关，保持串行早停语义）。
    env MJC_KNOWLEDGE_PARALLEL=1 或 settings.knowledge.parallel_backends=true 开启。"""
    env = os.environ.get("MJC_KNOWLEDGE_PARALLEL", "").strip().lower()
    if env in ("1", "on", "true", "yes"):
        return True
    if env in ("0", "off", "false", "no"):
        return False
    try:
        return bool(_settings_cfg().get("parallel_backends"))
    except Exception:
        return False


def _search_merge_parallel(query, names, timeout, min_total, retry_empty):
    """并行版合并检索：所有后端同时发（首轮不睡限速），取全部结果后按原名次合并。
    代价：不早停（会多打几个后端）；仅 opt-in。返回与 _search_merge 同结构。"""
    import concurrent.futures
    if not names:
        return None
    results = {}

    def _fetch(n):
        try:
            return _merge_try_backend(n, BACKENDS[n], query, timeout)
        except Exception:
            return [], False

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(names)) as ex:
        futs = {ex.submit(_fetch, n): n for n in names}
        for fut in concurrent.futures.as_completed(futs):
            results[futs[fut]] = fut.result()
    if retry_empty:
        empties = [n for n in names if results.get(n, ([], False))[1] and not results[n][0]
                   and (BACKEND_STATUS.get(n) or {}).get("status") != "blocked"]
        if empties:
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(empties)) as ex:
                futs = {ex.submit(_fetch, n): n for n in empties}
                for fut in concurrent.futures.as_completed(futs):
                    results[futs[fut]] = fut.result()
    merged, seen, contrib = [], set(), []
    for n in names:
        snips, _ok = results.get(n, ([], False))
        if snips and _merge_extend(merged, seen, snips):
            contrib.append(n)
    if not merged:
        return None
    return {"backend": "+".join(contrib or names), "query": query, "snippets": merged,
            "fetched": len(merged), "backends_tried": list(names),
            "backend_status": {n: dict(BACKEND_STATUS.get(n) or {}) for n in names}}


def _search_merge(query, backends, timeout, min_total, max_backends, retry_empty=True,
                  parallel=None):
    """v0.9.1 多后端合并检索（知识证据门默认路径）：
    - 按 backends 顺序累积去重（URL / 归一化文本相同 → 同一 snippet，跨后端不重复计数）；
    - 停止：去重后累积 ≥ min_total 或已试满 max_backends（先到先停）；
    - 空结果 → 同后端重试 1 次（retry_empty，仅 merge 模式）；异常 → 直接继续下一后端；
    - 不读写进程级缓存（避免与非 merge 结果互污，门场景保持每次真实检索）。
    返回 {"backend": "a+b", "query", "snippets", "fetched"(去重后条数), "backends_tried"}；
    全空 → None（不抛）。"""
    if not backends:
        return None
    try:
        min_total = max(1, int(min_total))
    except (TypeError, ValueError):
        min_total = 3
    try:
        max_backends = max(1, int(max_backends))
    except (TypeError, ValueError):
        max_backends = 3
    tmo = timeout or timeout_from_settings()
    if parallel is None:
        parallel = _parallel_enabled()
    if parallel:
        names = [n for n in backends if BACKENDS.get(n) and not _in_cooldown(n)][:max_backends]
        if not names:
            for n in backends:
                if BACKENDS.get(n):
                    _note_status(n, "blocked", "反爬冷却期内，本轮跳过（避免加重封禁）", mark=False)
            return None
        return _search_merge_parallel(query, names, tmo, min_total, retry_empty)
    merged, seen, tried, contrib = [], set(), [], []
    for name in backends:
        if len(tried) >= max_backends:
            break
        fn = BACKENDS.get(name)
        if not fn:
            continue
        tried.append(name)
        if _in_cooldown(name):
            # 冷却期内跳过（不重试、不再触发）；状态仍记 blocked，让上层如实判 error 而非"无证据"
            _note_status(name, "blocked", "反爬冷却期内，本轮跳过（避免加重封禁）", mark=False)
            continue
        snips, ok = _merge_try_backend(name, fn, query, tmo)
        was_blocked = (BACKEND_STATUS.get(name) or {}).get("status") == "blocked"
        if not snips and ok and retry_empty and not was_blocked:
            # 刚被判 blocked 的后端不立刻重试（实测会加重封禁）——冷却期内由 _in_cooldown 跳过
            snips, _ = _merge_try_backend(name, fn, query, tmo)
        if snips and _merge_extend(merged, seen, snips):
            contrib.append(name)
        if len(merged) >= min_total:
            break
    if not merged:
        return None
    return {"backend": "+".join(contrib or tried), "query": query, "snippets": merged,
            "fetched": len(merged), "backends_tried": tried,
            "backend_status": {n: dict(BACKEND_STATUS.get(n) or {}) for n in tried}}


def search(query, backends=None, timeout=None, merge=False,
           min_total=3, max_backends=3, retry_empty=True, parallel=None):
    """按序尝试后端。merge=False（默认，行为与 v0.9.0 完全一致）：第一个有结果的即返回，带缓存 + 限速。
    merge=True（v0.9.1）：多后端累积合并（见 _search_merge）；不缓存，避免与非 merge 结果互污。
    parallel=True（v0.10 opt-in）：merge 模式下多后端并行检索（不早停，换延迟）。"""
    query = (query or "").strip()
    if not query:
        return None
    if merge:
        return _search_merge(query, backends if backends is not None else configured_backends(),
                             timeout, min_total, max_backends, retry_empty, parallel)
    key = query[:300]
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < _CACHE_TTL:
        return hit[1]
    backends = backends if backends is not None else configured_backends()
    if not backends:
        return None
    _max_n, timeout = _limits()
    timeout = timeout or timeout_from_settings()
    for name in backends:
        fn = BACKENDS.get(name)
        if not fn:
            continue
        gap = time.time() - _LAST_CALL.get(name, 0)
        if gap < _MIN_INTERVAL:
            time.sleep(_MIN_INTERVAL - gap)
        try:
            _LAST_CALL[name] = time.time()
            snips = fn(query, timeout) or []
        except Exception:
            snips = []
        if snips:
            res = {"backend": name, "query": query, "snippets": snips}
            _CACHE[key] = (time.time(), res)
            return res
    _CACHE[key] = (time.time(), None)
    return None


def timeout_from_settings():
    try:
        return _limits()[1]
    except Exception:
        return 12.0


def fetch_evidence(query, backends=None, merge=False, min_total=3, max_backends=3, parallel=None):
    """给仲裁/证据门用的证据获取（尊重 max_per_review 的外层约束）。
    merge=True（v0.9.1）：多后端累积合并（证据门/调试默认）；默认 False = 与原行为逐字节一致。
    parallel=True（v0.10 opt-in）：merge 模式下多后端并行（不早停）。"""
    kw = {"parallel": parallel} if parallel is not None else {}
    return search(query, backends=backends, merge=merge,
                  min_total=min_total, max_backends=max_backends, **kw)


class Budget:
    """单次审查的检索预算（次数上限）。"""

    def __init__(self, n=None):
        self.max_n = n if n is not None else _limits()[0]
        self.used = 0

    def take(self, query, backends=None):
        if self.used >= self.max_n:
            return None
        self.used += 1
        return fetch_evidence(query, backends=backends)
