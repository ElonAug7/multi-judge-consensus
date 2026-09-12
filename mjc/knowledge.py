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

配置（settings.json，可省略）：
  "knowledge": {"enabled": false, "backends": ["sogou","bing","baidu"], "max_per_review": 3, "timeout": 12}
或环境变量 MJC_KNOWLEDGE=off|sogou,bing,baidu ；MJC_KNOWLEDGE_CMD="<命令模板，含 {query}>"

API：fetch_evidence(query) -> {"backend": str, "snippets": [{title,url,text}], "fetched": int}
     fetch_evidence(query, merge=True)（v0.9.1）-> 多后端合并结果：
         {"backend": "sogou+bing", "snippets": [...], "fetched": int, "backends_tried": [...]}
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

TAG_RE = re.compile(r"<[^>]+>")


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
    return re.sub(r"\s+", " ", html.unescape(TAG_RE.sub(" ", s or ""))).strip()


def _http_get(url, timeout):
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


def _back_sogou(query, timeout):
    h = _http_get("https://www.sogou.com/web?query=" + urllib.parse.quote(query), timeout)
    blocks = re.findall(r'<div class="vrwrap"[^>]*>(.*?)(?=<div class="vrwrap"|$)', h, re.S)
    out = []
    for b in blocks[:6]:
        txt = _strip_tags(b)
        if txt and len(txt) > 30:
            out.append({"title": txt[:90], "url": "", "text": txt[:400]})
    return out


def _back_bing(query, timeout):
    h = _http_get("https://cn.bing.com/search?q=" + urllib.parse.quote(query), timeout)
    blocks = re.findall(r'<li class="b_algo".*?</li>', h, re.S)
    out = []
    for b in blocks[:6]:
        txt = _strip_tags(b)
        if txt and len(txt) > 30:
            out.append({"title": txt[:90], "url": "", "text": txt[:400]})
    return out


def _back_baidu(query, timeout):
    h = _http_get("https://www.baidu.com/s?ie=utf-8&wd=" + urllib.parse.quote(query), timeout)
    if "安全验证" in h or len(h) < 5000:
        return []  # 被反爬/限流
    blocks = re.findall(r'<div[^>]*class="result[^"]*c-container[^"]*"[^>]*>(.*?)</div>\s*</div>', h, re.S)
    out = []
    for b in blocks[:6]:
        txt = _strip_tags(b)
        if txt and len(txt) > 30:
            out.append({"title": txt[:90], "url": "", "text": txt[:400]})
    return out


def _back_cmd(query, timeout):
    tpl = os.environ.get("MJC_KNOWLEDGE_CMD", "").strip()
    if not tpl:
        return []
    cmd = tpl.replace("{query}", query)
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout + 5)
    try:
        d = json.loads(p.stdout)
        if isinstance(d, dict):
            d = d.get("snippets") or []
        return [{"title": str(s.get("title", ""))[:90], "url": str(s.get("url", "")),
                 "text": str(s.get("text", ""))[:400]} for s in d[:6]]
    except Exception:
        txt = (p.stdout or "").strip()
        return [{"title": "", "url": "", "text": txt[:400]}] if txt else []


BACKENDS = {"sogou": _back_sogou, "bing": _back_bing, "baidu": _back_baidu, "cmd": _back_cmd}


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
    except Exception:
        return [], False


def _search_merge(query, backends, timeout, min_total, max_backends, retry_empty=True):
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
    merged, seen, tried, contrib = [], set(), [], []
    for name in backends:
        if len(tried) >= max_backends:
            break
        fn = BACKENDS.get(name)
        if not fn:
            continue
        tried.append(name)
        snips, ok = _merge_try_backend(name, fn, query, tmo)
        if not snips and ok and retry_empty:
            snips, _ = _merge_try_backend(name, fn, query, tmo)  # 空结果重试 1 次
        if snips and _merge_extend(merged, seen, snips):
            contrib.append(name)
        if len(merged) >= min_total:
            break
    if not merged:
        return None
    return {"backend": "+".join(contrib or tried), "query": query, "snippets": merged,
            "fetched": len(merged), "backends_tried": tried}


def search(query, backends=None, timeout=None, merge=False,
           min_total=3, max_backends=3, retry_empty=True):
    """按序尝试后端。merge=False（默认，行为与 v0.9.0 完全一致）：第一个有结果的即返回，带缓存 + 限速。
    merge=True（v0.9.1）：多后端累积合并（见 _search_merge）；不缓存，避免与非 merge 结果互污。"""
    query = (query or "").strip()
    if not query:
        return None
    if merge:
        return _search_merge(query, backends if backends is not None else configured_backends(),
                             timeout, min_total, max_backends, retry_empty)
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


def fetch_evidence(query, backends=None, merge=False, min_total=3, max_backends=3):
    """给仲裁/证据门用的证据获取（尊重 max_per_review 的外层约束）。
    merge=True（v0.9.1）：多后端累积合并（证据门/调试默认）；默认 False = 与原行为逐字节一致。"""
    return search(query, backends=backends, merge=merge,
                  min_total=min_total, max_backends=max_backends)


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
