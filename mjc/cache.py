#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MJC · cache.py — 审查结果缓存（P3.2）
相同 task + agent_output + Judge 池 + 审查选项 → 命中直接复用上次完整 record，零 API 调用。
存储：logs/cache/<sha1>.json（一记录一文件，原子写，超期自动清理）
用法：
  from mjc import cache
  key = cache.cache_key(task, output, pool_spec, opts)
  rec = cache.get(key)          # None 或 record
  cache.put(key, rec)
"""
import hashlib
import json
import os
import random
import tempfile
import time

DEFAULT_DIR = None  # 由上层(如 pipeline/cli)设置 os.path.join(logs, "cache")
MAX_AGE_S = 7 * 86400  # 缓存有效期 7 天
_PRUNE_PROB = 0.02     # 每次 put 以该概率清理过期文件


def cache_dir():
    return DEFAULT_DIR or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs", "cache")


def set_cache_dir(d):
    global DEFAULT_DIR
    DEFAULT_DIR = d


def cache_key(task, output, pool_spec, opts=None):
    """task+output+池+审查选项 的稳定指纹（不截断——必须全量一致才算同一审查）"""
    payload = json.dumps({
        "task": task,
        "output": output,
        "pool": sorted(pool_spec),
        "opts": opts or {},
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _path(key):
    return os.path.join(cache_dir(), key + ".json")


def get(key):
    p = _path(key)
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        if time.time() - data.get("ts", 0) > MAX_AGE_S:
            try:
                os.remove(p)
            except Exception:
                pass
            return None
        rec = data.get("record")
        if rec:
            rec = dict(rec)
            rec["_cache_hit"] = True
        return rec
    except Exception:
        return None


def put(key, record):
    """原子写（同目录 tmp + rename）。失败静默——缓存是优化不是正确性依赖。"""
    try:
        os.makedirs(cache_dir(), exist_ok=True)
        p = _path(key)
        fd, tmp = tempfile.mkstemp(dir=cache_dir(), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"key": key, "ts": time.time(), "record": record}, f, ensure_ascii=False)
        os.replace(tmp, p)
        if random.random() < _PRUNE_PROB:
            _prune()
    except Exception:
        pass


def _prune():
    """清理超期缓存文件"""
    try:
        now = time.time()
        for fn in os.listdir(cache_dir()):
            if not fn.endswith(".json"):
                continue
            p = os.path.join(cache_dir(), fn)
            try:
                if now - os.path.getmtime(p) > MAX_AGE_S:
                    os.remove(p)
            except Exception:
                pass
    except Exception:
        pass


def count():
    """当前缓存条目数（统计用）"""
    try:
        return len([f for f in os.listdir(cache_dir()) if f.endswith(".json")])
    except Exception:
        return 0
