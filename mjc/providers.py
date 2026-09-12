#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MJC · providers.py — 国产模型统一调用层（零第三方依赖，urllib）
端点（OpenAI 兼容）：
  qwen     https://dashscope.aliyuncs.com/compatible-mode/v1
  deepseek https://api.deepseek.com/v1
  glm      https://open.bigmodel.cn/api/paas/v4
Key 来源：环境变量 MJC_<NAME>_KEY > keys.local.json（600）> ~/.openclaw/keys/
"""
import json
import os
import urllib.request
import urllib.error

BASE = {
    # qwen: 百炼 token-plan 专属端点（dashscope 普通 key 欠费不可用，2026-09-06 实测）
    "qwen": "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "glm": "https://open.bigmodel.cn/api/paas/v4",
}

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEY_PATH = os.path.join(BASE_DIR, "keys.local.json")


def keys_path():
    return KEY_PATH


def reload_keys():
    """后台改了 key 后调用，清缓存强制重读"""
    global _KEYS
    _KEYS = None


def registry_providers():
    """settings.json 注册的厂商名（懒加载，避免循环 import）"""
    try:
        from mjc import settings
        return list(settings.load().get("providers", {}).keys())
    except Exception:
        return []


def endpoint_of(name):
    """端点：settings 注册表优先（后台可配），回退内置 BASE"""
    try:
        from mjc import settings
        ep = (settings.load().get("providers") or {}).get(name, {}).get("endpoint")
        if ep:
            return ep
    except Exception:
        pass
    return BASE.get(name)

# 默认模型（可按成本调整：qwen3-turbo 更便宜）
MODELS = {
    "qwen": "qwen3.8-max",
    "deepseek": "deepseek-v4-flash",
    "glm": "glm-4-plus",
    "dashscope": "qwen-max",
    "doubao": "doubao-seed-1.6-lite",
    "kimi": "kimi-latest",
}
# deepseek-chat 上下文可能不含 "deepseek-v3" 名字——deepseek 的 chat = V3；reasoner = R1
ALIAS = {"deepseek-chat": "DeepSeek-V3", "deepseek-reasoner": "DeepSeek-R1", "qwen3.8-max": "通义千问 Qwen3-Max", "glm-4-plus": "GLM-4-Plus"}

_KEYS = None


def _load_keys():
    global _KEYS
    if _KEYS is not None:
        return _KEYS
    keys = {}
    # 1) env
    for name in set(BASE) | set(registry_providers()):
        v = os.environ.get(f"MJC_{name.upper()}_KEY") or os.environ.get(f"MJC_{name}_KEY")
        if v:
            keys[name] = v
    # 2) keys.local.json
    local = KEY_PATH
    if os.path.exists(local):
        try:
            keys.update(json.load(open(local)))
        except Exception:
            pass
    # 3) openclaw keys dir（glm）
    oc_key = os.path.expanduser("~/.openclaw/keys/bigmodel.key")
    if "glm" not in keys and os.path.exists(oc_key):
        try:
            keys["glm"] = open(oc_key).read().strip()
        except Exception:
            pass
    _KEYS = keys
    return keys


def available_providers():
    return sorted(_load_keys().keys())


def has_key(name):
    return name in _load_keys()


def chat(name, messages, model=None, temperature=0.2, max_tokens=1500, timeout=60, retries=3):
    """调 OpenAI 兼容 chat completions。返回文本。
    重试策略（P1.2）：空响应/429/5xx/网络错误 → 自动重试（最多 retries 次，指数退避 1.5x 上限 8s）；
    其余 4xx（401/403/404 等）→ 立即抛错不重试（重试无意义）。"""
    import time as _t
    key = _load_keys().get(name)
    if not key:
        raise RuntimeError(f"provider {name} 无 key（env MJC_{name.upper()}_KEY 或 keys.local.json）")
    url = endpoint_of(name) + "/chat/completions"
    body = json.dumps({
        "model": model or MODELS[name],
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }).encode("utf-8")
    last_exc = None
    for attempt in range(max(retries, 1)):
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            content = (data["choices"][0]["message"].get("content") or "").strip()
            if content:
                u = data.get("usage") or {}
                record_usage(name, model or MODELS.get(name), u.get("prompt_tokens"), u.get("completion_tokens"))
                return content
            last_exc = RuntimeError(f"[{name}] 空响应 (attempt {attempt+1})")
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8")[:300]
            except Exception:
                pass
            last_exc = RuntimeError(f"[{name}] HTTP {e.code}: {detail}")
            if e.code not in (429, 500, 502, 503, 504):
                raise last_exc
        except Exception as e:
            last_exc = e
        if attempt < max(retries, 1) - 1:
            _t.sleep(min(1.5 * (attempt + 1), 8.0))  # 指数退避，上限 8s（防 retries 调大时失控）
    raise last_exc


def display_name(name, model=None):
    m = model or MODELS.get(name)
    return ALIAS.get(m, m or name)


# 用量采集：每次成功调用记录 (ts, name, model, prompt_tokens, completion_tokens)
# 用时间窗查询（并发安全弱一致即可）；超 1 小时旧记录在 append 时顺带修剪。
import time as _time
_USAGE_LOG = []  # 环形，上限 8000

_PRICES = {  # ¥/1K tokens（估算：输入, 输出）——只做成本参考，非账单
    "deepseek:deepseek-v4-flash": (0.0008, 0.004),
    "deepseek:deepseek-chat": (0.002, 0.008),
    "glm:glm-4-flash": (0.0006, 0.0012),
    "glm:glm-4-plus": (0.005, 0.02),
    "glm:glm-4.5": (0.01, 0.04),
    "qwen:qwen3-turbo": (0.0003, 0.0009),
    "qwen:qwen3.8-max": (0.02, 0.06),
    "dashscope:qwen3-turbo": (0.0003, 0.0009),
    "dashscope:qwen-plus": (0.0008, 0.002),
    "dashscope:qwen-max": (0.02, 0.06),
    "doubao:doubao-seed-1.6-lite": (0.0003, 0.0009),
    "doubao:doubao-seed-1.6": (0.0008, 0.002),
    "kimi:kimi-latest": (0.01, 0.03),
}
_DEF_PRICE = (0.002, 0.008)


def record_usage(name, model, prompt_tokens=0, completion_tokens=0):
    try:
        _USAGE_LOG.append((_time.time(), name, model or "", int(prompt_tokens or 0), int(completion_tokens or 0)))
        if len(_USAGE_LOG) > 8000:
            _USAGE_LOG[:2000] = []
    except Exception:
        pass


def usage_since(ts):
    """ts(秒) 之后发生的用量 → {spec(name:model): {prompt, completion}}；并修剪 1h 前旧记录"""
    try:
        cutoff = _time.time() - 3600
        kept, out = [], {}
        for t, name, model, pt, ct in _USAGE_LOG:
            if t < cutoff:
                continue
            kept.append((t, name, model, pt, ct))
            if t >= ts:
                spec = f"{name}:{model or MODELS.get(name, '')}"
                d = out.setdefault(spec, {"prompt": 0, "completion": 0})
                d["prompt"] += pt
                d["completion"] += ct
        if len(kept) != len(_USAGE_LOG):
            _USAGE_LOG[:] = kept
        return out
    except Exception:
        return {}


def price_of(name, model=None):
    m = model or MODELS.get(name)
    return _PRICES.get(f"{name}:{m}", _DEF_PRICE)


def estimate_cost_tokens(name, model=None):
    """兼容旧接口：输入单价 ¥/1K（≈ 输出按 3-4 倍估算的历史口径）"""
    return price_of(name, model)[0]
