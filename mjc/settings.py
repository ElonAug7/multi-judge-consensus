#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MJC · settings.py — 后台管理配置层（P4：管理界面数据层）
单文件 settings.json（600 权限），默认值内置本模块：

  providers  厂商注册表 {name: {label, endpoint, models:[...], note}} —— OpenAI 兼容端点，加 key 即用
  model_tags 模型目录 { "厂商:模型": {tag: 🥬白菜|💰平价|💎旗舰|未知, note} }
  tiers      档位预设 {id: {label, desc, committee, screen_enabled, screen_model, screen_conf, degrade, cache}}
  current    当前生效 {tier, 覆盖字段(可空)} —— 档位默认 + 覆盖
  limits     审查长度门槛 {gate, auto, scan}（字符数；默认 1 ≈ 全量送审；0 = 不限）
  falsifier  证伪者（红队找错）{enabled, model(空=自动找跨厂), min_len}
  key_status key 连通性探测缓存 {provider: {ok, at, detail}}

key 本体仍存 keys.local.json（600），由 providers.py 读取；本模块只提供增删改入口。
CLI 未显式给 --pool/--screen-conf 时与 WebUI 同读 current —— 行为一致。
"""
import copy
import json
import os
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# v0.11.0c：可用 MJC_SETTINGS_PATH 指向一份**冻结的配置快照**（实验可复现：代码 SHA + 配置一起钉）。
# 未设置时行为与旧版逐字节一致（读仓库内 settings.json）。
PATH = os.environ.get("MJC_SETTINGS_PATH") or os.path.join(BASE_DIR, "settings.json")
KEYS_PATH = os.path.join(BASE_DIR, "keys.local.json")

PROBE_CACHE_TTL = 300   # 秒：探测结果 5 分钟内复用，防手滑连点重复烧调用
MAX_TRIED = 3           # probe 结果 tried 列表保留最近失败条数上限（防超长厂商列表刷屏）

DEFAULT_PROVIDERS = {
    "deepseek": {
        "label": "DeepSeek 深度求索", "endpoint": "https://api.deepseek.com/v1",
        "models": ["deepseek-v4-flash", "deepseek-chat"],
        "note": "deepseek-chat=V3；flash 白菜价",
    },
    "glm": {
        "label": "智谱 GLM", "endpoint": "https://open.bigmodel.cn/api/paas/v4",
        "models": ["glm-4-flash", "glm-4-plus", "glm-4.5"],
        "note": "glm-4-flash 接近免费，实测审查主力",
    },
    "qwen": {
        "label": "阿里百炼·token-plan", "endpoint": "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
        "models": ["qwen3.8-max", "qwen3-turbo"],
        "note": "OpenClaw 专属 token-plan 端点；qwen3.8-max 需该套餐已购买（09-06 实测 403 未购买）",
    },
    "dashscope": {
        "label": "阿里百炼·标准版", "endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": ["qwen3-turbo", "qwen-plus", "qwen-max"],
        "note": "百炼标准 API key（dashscope.console 里那个）；qwen3-turbo 白菜价，欠费需充值",
    },
    "doubao": {
        "label": "字节豆包·火山方舟", "endpoint": "https://ark.cn-beijing.volces.com/api/v3",
        "models": ["doubao-seed-1.6-lite", "doubao-seed-1.6"],
        "note": "模型 ID 以火山方舟控制台为准；lite 白菜价",
    },
    "kimi": {
        "label": "Moonshot Kimi", "endpoint": "https://api.moonshot.cn/v1",
        "models": ["kimi-latest"],
        "note": "模型 ID 以官方文档为准；未实测审查质量",
    },
}

DEFAULT_TAGS = {
    "deepseek:deepseek-v4-flash": {"tag": "🥬白菜", "note": "便宜快，实测可靠"},
    "deepseek:deepseek-chat": {"tag": "💰平价", "note": "V3 级，更强但略贵"},
    "glm:glm-4-flash": {"tag": "🥬白菜", "note": "接近免费；初筛+委员会实测可用"},
    "glm:glm-4-plus": {"tag": "💎旗舰", "note": "精审主力，实测 streak 最高"},
    "glm:glm-4.5": {"tag": "💎旗舰", "note": "更强，未进基准测试"},
    "qwen:qwen3-turbo": {"tag": "🥬白菜", "note": "阿里白菜价；token-plan 端点模型需套餐购买"},
    "qwen:qwen3.8-max": {"tag": "💎旗舰", "note": "token-plan 直连（09-06 实测 403 未购买）"},
    "dashscope:qwen3-turbo": {"tag": "🥬白菜", "note": "百炼标准端点；欠费需充值"},
    "dashscope:qwen-plus": {"tag": "💰平价", "note": "百炼标准端点"},
    "dashscope:qwen-max": {"tag": "💎旗舰", "note": "百炼标准端点"},
    "doubao:doubao-seed-1.6-lite": {"tag": "🥬白菜", "note": "未实测"},
    "doubao:doubao-seed-1.6": {"tag": "💰平价", "note": "未实测"},
    "kimi:kimi-latest": {"tag": "💰平价", "note": "未实测"},
}

DEFAULT_TIERS = {
    "eco": {
        "label": "🥬 省钱档", "desc": "2×白菜模型交叉，最快最省（干净内容初筛 1 调用放行）",
        "committee": ["glm:glm-4-flash", "deepseek:deepseek-v4-flash"],
        "screen_enabled": True, "screen_model": "glm:glm-4-flash",
        "screen_conf": 0.8, "degrade": False, "cache": True,
    },
    "standard": {
        "label": "⚖️ 标准档", "desc": "2 白菜 + 1 精审旗舰，默认（基准 100% 识别 / 0% 误杀）",
        "committee": ["deepseek:deepseek-v4-flash", "glm:glm-4-flash", "glm:glm-4-plus"],
        "screen_enabled": True, "screen_model": "glm:glm-4-flash",
        "screen_conf": 0.75, "degrade": False, "cache": True,
    },
    "pro": {
        "label": "💎 精审档", "desc": "最强模型全量委员会（不用初筛），最贵最稳",
        "committee": ["glm:glm-4-plus", "glm:glm-4.5", "deepseek:deepseek-chat"],
        "screen_enabled": False, "screen_model": None,
        "screen_conf": 0.75, "degrade": False, "cache": True,
    },
}

DEFAULT = {
    "providers": DEFAULT_PROVIDERS,
    "model_tags": DEFAULT_TAGS,
    "tiers": DEFAULT_TIERS,
    "current": {"tier": "standard",
                "committee": None, "screen_enabled": None, "screen_model": None,
                "screen_conf": None, "degrade": None, "cache": None},
    "limits": {"gate": 1, "auto": 1, "scan": 1},
    "falsifier": {"enabled": True, "model": "", "min_len": 40},
    "consensus": {"dissent_guard": {"enabled": True,
                                     "blocking_types": ["factual_error", "hallucination", "logical_error"],
                                     "min_conf": 0.0}},
    "key_status": {},
}


def _deep_merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load():
    if os.path.exists(PATH):
        try:
            return _deep_merge(DEFAULT, json.load(open(PATH, encoding="utf-8")))
        except Exception:
            pass
    return copy.deepcopy(DEFAULT)


def save(data):
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.chmod(tmp, 0o600)
    os.replace(tmp, PATH)


def _spec_ok(spec):
    """'provider:model' → provider 是否有 key"""
    p = spec.split(":", 1)[0]
    from mjc import providers
    return bool(p) and providers.has_key(p)


def effective(data=None):
    """当前生效配置：档位默认 + current 覆盖 + 按 key 过滤委员会。
    返回 {tier,tier_label,committee,screen_*,degrade,cache}；可用模型不足 2 个抛 ValueError。"""
    d = data or load()
    cur = d["current"]
    tier_id = cur.get("tier") or "standard"
    if tier_id not in d["tiers"]:
        tier_id = "standard"
    base = dict(d["tiers"][tier_id])
    for k in ("committee", "screen_enabled", "screen_model", "screen_conf", "degrade", "cache"):
        v = cur.get(k)
        if v is not None:
            base[k] = v
    specs = [s for s in (base.get("committee") or []) if isinstance(s, str) and _spec_ok(s)]
    # 去重保序
    seen, specs2 = set(), []
    for s in specs:
        if s not in seen:
            seen.add(s)
            specs2.append(s)
    if len(specs2) < 2:
        raise ValueError("可用模型不足 2 个：请先在「后台管理」配置至少两家有 key 的厂商模型")
    out = dict(base)
    out["committee"] = specs2
    out["tier"] = tier_id
    out["tier_label"] = d["tiers"][tier_id]["label"]
    return out


def limit(name, data=None):
    """审查长度门槛（字符数）：gate=闸门 / auto=自动审查 / scan=转录扫描。
    settings.limits 可覆盖；默认 1（≈全量送审，防短答被门槛绕过——2026-09-12 实验教训）。"""
    d = data or load()
    lim = d.get("limits") or {}
    v = lim.get(name)
    if v is None:
        v = DEFAULT["limits"].get(name, 1)
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return DEFAULT["limits"].get(name, 1)


def apply(patch, data=None):
    """应用后台修改（patch 为 current 覆盖字段或 tier 名）。校验后落盘。"""
    d = data or load()
    cur = d["current"]
    if "tier" in patch:
        tid = patch["tier"]
        if tid not in d["tiers"]:
            raise ValueError(f"未知档位 {tid}")
        cur["tier"] = tid
    for k in ("committee",):
        if k in patch and patch[k] is not None:
            v = patch[k] if isinstance(patch[k], list) else [x.strip() for x in str(patch[k]).split(",") if x.strip()]
            _check_specs(v, d)
            cur[k] = list(dict.fromkeys(v))
        elif k in patch:  # 显式 null → 回档位默认
            cur[k] = None
    if "screen_model" in patch:
        v = patch["screen_model"]
        if isinstance(v, list):  # 兼容旧 bug：数组当字符串修
            v = v[0] if v else None
        if v:  # 单个 spec 字符串
            v = str(v).strip()
            _check_specs([v], d)
            cur["screen_model"] = v
        else:  # 空/None → 关闭（不回落档位默认，避免 UI 困惑）
            cur["screen_model"] = None
    if "screen_enabled" in patch and patch["screen_enabled"] is not None:
        cur["screen_enabled"] = bool(patch["screen_enabled"])
    if "screen_conf" in patch and patch["screen_conf"] is not None:
        try:
            cur["screen_conf"] = max(0.0, min(1.0, float(patch["screen_conf"])))
        except (TypeError, ValueError):
            raise ValueError("screen_conf 需为 0-1 数字")
    if "degrade" in patch and patch["degrade"] is not None:
        cur["degrade"] = bool(patch["degrade"])
    if "cache" in patch and patch["cache"] is not None:
        cur["cache"] = bool(patch["cache"])
    if "limits" in patch and patch["limits"] is not None:
        lim = d.get("limits") or {}
        for k, v in dict(patch["limits"]).items():
            if k in ("gate", "auto", "scan") and v is not None:
                try:
                    lim[k] = max(0, int(v))
                except (TypeError, ValueError):
                    raise ValueError("limits 需为整数（字符数门槛）")
        d["limits"] = lim
    save(d)
    return effective(d)


# ---------- key 管理 ----------

def _read_keys_file():
    try:
        return json.load(open(KEYS_PATH, encoding="utf-8"))
    except Exception:
        return {}


def set_key(provider, key):
    key = (key or "").strip()
    if not key:
        raise ValueError("key 为空")
    d = load()
    if provider not in d["providers"]:
        raise ValueError(f"未知厂商 {provider}（可用: {', '.join(d['providers'])}）")
    keys = _read_keys_file()
    keys[provider] = key
    _write_keys_file(keys)
    from mjc import providers
    providers.reload_keys()
    d["key_status"].pop(provider, None)
    save(d)
    try:  # 保存后自动探测一次（1 次极小调用），UI 直接看到可用性
        probe(provider)
    except Exception:
        pass
    return _mask(key)


def clear_key(provider):
    keys = _read_keys_file()
    removed = keys.pop(provider, None)
    if removed is None:
        return False
    _write_keys_file(keys)
    from mjc import providers
    providers.reload_keys()
    d = load()
    d["key_status"].pop(provider, None)
    save(d)
    return True


def _write_keys_file(keys):
    tmp = KEYS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(keys, f, ensure_ascii=False, indent=1)
    os.chmod(tmp, 0o600)
    os.replace(tmp, KEYS_PATH)


def _mask(v):
    if not v:
        return None
    return v[:4] + "…" + v[-4:] if len(v) > 12 else "***"


def _check_specs(specs, d):
    reg = set()
    for p, meta in d["providers"].items():
        reg.update(f"{p}:{m}" for m in meta.get("models", []))
    for s in specs:
        if s not in reg:
            raise ValueError(f"未知模型 {s}（不在模型目录）")


def _fresh_status(st):
    """key_status 条目的 at(HH:MM:SS) 距今 < PROBE_CACHE_TTL 判为新鲜（跨零点按模 86400 处理）"""
    at = st.get("at")
    if not at:
        return False
    try:
        hh, mm, ss = (int(x) for x in str(at).split(":", 2))
        stored = hh * 3600 + mm * 60 + ss
    except (ValueError, TypeError):
        return False
    now = time.localtime()
    cur = now.tm_hour * 3600 + now.tm_min * 60 + now.tm_sec
    return (cur - stored) % 86400 < PROBE_CACHE_TTL


def _classify_error(name, detail):
    """把探测错误翻译成人话 + 修复提示"""
    d = str(detail)
    if "401" in d:
        return "key 无效或已过期(401)——检查 key 是否复制完整/前后空格"
    if "403" in d:
        return "无权限/未购买(403)——key 有效但该账号未开通此模型或套餐（如 qwen token-plan 需单独购买）"
    if "404" in d or "model_not_found" in d or "Model not exist" in d:
        return "模型不存在(404)——模型 ID 不对，或该端点不支持此模型（看下方已尝试列表，模型 ID 以官方文档为准）"
    if "空响应" in d or "empty" in d.lower():
        return "服务端空响应（瞬时故障）——key 通常没问题，稍后重试即可"
    if "429" in d:
        return "限流(429)——请求太频繁，稍后重试"
    if "timeout" in d.lower() or "timed out" in d.lower():
        return "连接超时——网络/端点问题，检查端点地址"
    return d[:150]


def probe(provider, model=None, data=None, force=False):
    """连通性探测：逐个尝试该厂商注册表里的模型（首个成功即止，max_tokens=8）。
    结果写 key_status {ok, detail, hint, tried, at}。
    TTL 缓存：PROBE_CACHE_TTL 秒内的旧结果直接复用（force=True 跳过），防连点烧调用。
    返回复用的缓存结果时带 cached=True 标记。
    tried 只保留最近 MAX_TRIED 条失败，超出部分以省略说明折叠。"""
    d = data or load()
    meta = d["providers"].get(provider)
    if not meta:
        raise ValueError(f"未知厂商 {provider}（可用: {', '.join(d['providers'])}）")
    if not force:
        cached = d["key_status"].get(provider)
        if cached and _fresh_status(cached):
            r = dict(cached)
            r["cached"] = True
            return r
    from mjc import providers as P
    if not P.has_key(provider):
        r = {"ok": False, "detail": "未配置 key", "hint": "先在后台粘贴该厂商的 API key", "at": time.strftime("%H:%M:%S")}
        d["key_status"][provider] = r
        save(d)
        return r
    models = [m for m in (meta.get("models") or [])]
    if model and model not in models:
        models.insert(0, model)
    if not models:
        models = [P.MODELS.get(provider)]
    tried, last_err, dropped = [], None, 0
    def _note(m, e):
        nonlocal dropped
        tried.append(f"{m}: {str(e)[:100]}")
        if len(tried) > MAX_TRIED:
            dropped += 1
            del tried[0]
    for m in models:
        try:
            P.chat(provider, [{"role": "user", "content": "ping"}], model=m, max_tokens=8, timeout=30, retries=2)
            tried_disp = tried[:]
            if dropped:
                tried_disp = [f"…另有 {dropped} 次失败未列"] + tried_disp
            r = {"ok": True, "detail": f"{m} 连通正常", "tried": tried_disp, "at": time.strftime("%H:%M:%S")}
            d["key_status"][provider] = r
            save(d)
            return r
        except Exception as e:
            _note(m, e)
            last_err = e
    err = str(last_err or "未知错误")
    tried_disp = tried[:]
    if dropped:
        tried_disp = [f"…另有 {dropped} 次失败未列"] + tried_disp
    r = {"ok": False, "detail": err[:200], "hint": _classify_error(provider, err), "tried": tried_disp, "at": time.strftime("%H:%M:%S")}
    d["key_status"][provider] = r
    save(d)
    return r


def _recent_dispositions(n=12):
    """最近处置记录（供 WebUI 纠正过程页）"""
    try:
        from mjc import dispositions
        return dispositions.recent(n)
    except Exception:
        return []


def _auto_switch_state():
    """自动审查总开关状态（供管理台/doctor）。"""
    try:
        from mjc import autoswitch
        return autoswitch.state()
    except Exception as e:
        return {"enabled": True, "updated_at": None, "by": f"error:{e}"}


def _usage_summary():
    """累计用量摘要（logs/usage.json；缺失 → 全 0）。"""
    try:
        from mjc.paths import LOG_DIR
        import json as _json
        p = os.path.join(LOG_DIR, "usage.json")
        if os.path.exists(p):
            u = _json.load(open(p, encoding="utf-8"))
            return {"reviews": u.get("reviews", 0), "tokens": u.get("tokens", 0),
                    "cost_yuan": u.get("cost_yuan", 0), "nonpass": u.get("nonpass", 0)}
    except Exception:
        pass
    return {"reviews": 0, "tokens": 0, "cost_yuan": 0, "nonpass": 0}


def _limits_merged(d):
    """limits 视图：内置默认 + 用户覆盖（只认 gate/auto/scan 三个键）"""
    lim = dict(DEFAULT["limits"])
    lim.update({k: v for k, v in (d.get("limits") or {}).items() if k in lim})
    return lim


def admin_state(data=None):
    """给管理界面/doctor 的完整状态（不含明文 key）"""
    from mjc import cache, trust, providers
    from mjc.paths import TRUST_PATH
    d = data or load()
    cur = d["current"]
    tier_id = cur.get("tier") or "standard"
    keys = _read_keys_file()
    provs, catalog = [], {}
    for p, meta in d["providers"].items():
        has = bool(keys.get(p))
        provs.append({
            "name": p, "label": meta["label"], "endpoint": meta["endpoint"],
            "note": meta.get("note", ""), "has_key": has, "key_masked": _mask(keys.get(p)),
            "status": d["key_status"].get(p),
            "models": [f"{p}:{m}" for m in meta.get("models", [])],
        })
        for m in meta.get("models", []):
            spec = f"{p}:{m}"
            t = d["model_tags"].get(spec, {})
            catalog[spec] = {
                "spec": spec, "provider": p, "model": m,
                "tag": t.get("tag", "未知"), "note": t.get("note", ""),
                "has_key": has,
            }
    try:
        eff = effective(d)
        committee = eff["committee"]
        cur_eff = {
            "tier": eff["tier"], "tier_label": eff["tier_label"],
            "committee": committee,
            "screen_enabled": eff.get("screen_enabled", True),
            "screen_model": eff.get("screen_model"),
            "screen_conf": eff.get("screen_conf", 0.75),
            "degrade": eff.get("degrade", False),
            "cache": eff.get("cache", True),
        }
    except ValueError as e:
        committee = []
        cur_eff = {"tier": tier_id, "error": str(e)}
    return {
        "version": __import__("mjc").__version__,
        "providers": provs,
        "catalog": sorted(catalog.values(), key=lambda x: (x["provider"], x["model"])),
        "current": cur_eff,
        "limits": _limits_merged(d),
        "tiers": {tid: {k: v for k, v in t.items() if k != "committee"} | {"committee": t["committee"]}
                  for tid, t in d["tiers"].items()},
        "cache_entries": cache.count(),
        "trust": trust.describe(path=TRUST_PATH),
        "dispositions": _recent_dispositions(),
        "auto_switch": _auto_switch_state(),
        "usage": _usage_summary(),
        "saved_at": os.path.getmtime(PATH) if os.path.exists(PATH) else None,
    }
