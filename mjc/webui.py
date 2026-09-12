#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MJC · webui.py — Web 界面（P4 起 = 审查台 + 后台管理）
  审查台：    POST /api/review    贴文本 → 三模型委员会审查（与 CLI 同 pipeline）
  后台管理：  GET  /api/state      完整状态（厂商/key 掩码/模型目录/当前档位/信任分/缓存）
             POST /api/admin/keys  {action:set|clear, provider, key}    管理 API key
             POST /api/admin/probe {provider} 或 {"provider":"all"}      连通性探测(1 次极小调用)
             POST /api/admin/apply {tier? committee? screen_enabled? screen_model?
                                    screen_conf? degrade? cache?}         改档位/委员会/开关 → 落盘
             POST /api/admin/switch {enabled}                             自动审查总开关（暂停=hook 静音）
             GET  /api/health      {ok, version, providers, cache}
安全：默认绑 127.0.0.1。非回环绑定必须设置 MJC_WEBUI_TOKEN（代码强制，红线）→ 页面 JS 弹窗要 token。
零第三方依赖（仅标准库 http.server）。设置持久化：settings.json + keys.local.json（均 600）。
"""
import argparse
import json
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from mjc import cache, providers, pipeline, settings
from mjc.judge import build_pool
from mjc.paths import LOG_DIR, TRUST_PATH
from mjc.page_html import load_page, load_page_js

MAX_TASK = 4000
MAX_OUTPUT = 20000

# ---------------- 页面（HTML 模板 + 独立 JS，见 mjc/assets/ + page_html.py） ----------------
PAGE = load_page()
PAGE_JS = load_page_js()


def _require_token():
    return os.environ.get("MJC_WEBUI_TOKEN", "").strip()


def _authed(headers, raw_path):
    tok = _require_token()
    if not tok:
        return True
    if headers.get("X-MJC-Token") == tok:
        return True
    q = urllib.parse.parse_qs(urllib.parse.urlparse(raw_path).query)
    return q.get("token", [""])[0] == tok


def _health():
    return {"ok": True, "app": "mjc", "version": __import__("mjc").__version__,
            "providers": providers.available_providers(),
            "cache_entries": cache.count()}


def _slim(record):
    slim = dict(record)
    slim["rounds"] = [{"round": r.get("round"), "kind": r.get("kind"), "opinions": [
        {k: v for k, v in o.items() if k in ("judge_id", "judge_display", "verdict", "confidence", "issues", "final_reasoning")}
        for o in (r.get("opinions") or [])]} for r in (record.get("rounds") or [])]
    return slim


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # 轻量访问日志：method path status —— stderr → journalctl --user -u mjc-webui
        try:
            import sys
            sys.stderr.write(f"[req] {self.command} {self.path} -> " + (fmt % args) + "\n")
        except Exception as _e:
            sys.stderr.write(f"[req] log failed: {_e}\n")

    def _send(self, code, obj, ctype="application/json; charset=utf-8"):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8") if not isinstance(obj, (bytes, str)) else (obj.encode("utf-8") if isinstance(obj, str) else obj)
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        _p = self.path.split("?", 1)[0]
        if _p == "/" or _p == "/index.html":
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif _p == "/assets/page.js":
            # 页面 JS 独立文件（node --check 直接查）；no-store 由 _send 统一加
            self._send(200, PAGE_JS, "application/javascript; charset=utf-8")
        elif self.path.startswith("/api/state"):
            if not _authed(self.headers, self.path):
                self._send(401, {"error": "unauthorized"})
                return
            try:
                self._send(200, settings.admin_state())
            except Exception as e:
                self._send(500, {"error": f"state 失败: {e}"})
        elif self.path.startswith("/api/live"):
            if not _authed(self.headers, self.path):
                self._send(401, {"error": "unauthorized"})
                return
            try:
                from mjc import live
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                after = int((q.get("after") or ["0"])[0])
                evs, last = live.read_after(after, limit=300)
                self._send(200, {"events": evs, "last": last})
            except Exception as e:
                self._send(500, {"error": f"live 失败: {e}"})
        elif self.path.startswith("/api/health"):
            if not _authed(self.headers, self.path):
                self._send(401, {"error": "unauthorized"})
                return
            self._send(200, _health())
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if not _authed(self.headers, self.path):
            self._send(401, {"error": "unauthorized"})
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except Exception:
            self._send(400, {"error": "body 需为 JSON"})
            return

        if self.path.startswith("/api/review"):
            self._review(req)
        elif self.path.startswith("/api/admin/keys"):
            self._admin_keys(req)
        elif self.path.startswith("/api/admin/probe"):
            self._admin_probe(req)
        elif self.path.startswith("/api/admin/apply"):
            self._admin_apply(req)
        elif self.path.startswith("/api/admin/switch"):
            self._admin_switch(req)
        else:
            self._send(404, {"error": "not found"})

    # ---------- 审查台 ----------
    def _review(self, req):
        task = (req.get("task") or "").strip()
        output = (req.get("output") or "").strip()
        if not task or not output:
            self._send(400, {"error": "task 与 output 均必填"})
            return
        task, output = task[:MAX_TASK], output[:MAX_OUTPUT]
        try:
            eff = settings.effective()
            default_pool = eff["committee"]
        except Exception:
            default_pool = None
        pool_spec = req.get("pool") or default_pool
        if isinstance(pool_spec, str):
            pool_spec = [x.strip() for x in pool_spec.split(",") if x.strip()]
        pool = build_pool(list(pool_spec)) if pool_spec else []
        if len(pool) < 2:
            self._send(400, {"error": f"Judge 不足（{len(pool)}），请先到后台管理配好 key/委员会"})
            return
        use_screen = bool(req.get("screen", True))
        screen_j = pipeline.resolve_screen_judge(None) if use_screen else None
        # degrade/cache：请求显式给 → 用之；否则跟后台设置
        try:
            eff = settings.effective()
            cur_degrade, cur_cache = bool(eff.get("degrade", False)), bool(eff.get("cache", True))
        except Exception:
            cur_degrade, cur_cache = False, True
        use_cache = (not bool(req.get("no_cache"))) if "no_cache" in req else cur_cache
        use_degrade = bool(req.get("degrade")) if "degrade" in req else cur_degrade
        import hashlib as _hl
        from mjc import live as _live
        _tid = _hl.sha1(output.encode()).hexdigest()[:10]
        _emit = (lambda ev: _live.append(ev["kind"], _tid,
            **{k: v for k, v in ev.items() if k not in ("kind", "ts_ms", "at", "task_id")})) \
            if os.environ.get("MJC_LIVE") != "0" else None
        record, meta = pipeline.run_review_once(
            task, output, pool,
            screen_judge=screen_j,
            screen_conf=None,  # pipeline 内部读后台设置
            use_screen=screen_j is not None,
            use_cache=use_cache,
            use_degrade=use_degrade,
            trust_path=TRUST_PATH, debate_log_dir=LOG_DIR,
            emit=_emit,
        )
        self._send(200, {"record": _slim(record), "meta": meta})

    # ---------- 后台管理 ----------
    def _admin_keys(self, req):
        action = req.get("action")
        provider = (req.get("provider") or "").strip()
        if not provider:
            self._send(400, {"error": "provider 必填"})
            return
        try:
            if action == "set":
                masked = settings.set_key(provider, req.get("key") or "")
                self._send(200, {"ok": True, "masked": masked})
            elif action == "clear":
                removed = settings.clear_key(provider)
                self._send(200, {"ok": True, "removed": removed})
            else:
                self._send(400, {"error": "action 需为 set/clear"})
        except ValueError as e:
            self._send(400, {"error": str(e)})
        except Exception as e:
            self._send(500, {"error": f"key 操作失败: {e}"})

    def _admin_probe(self, req):
        provider = (req.get("provider") or "").strip()
        force = bool(req.get("force"))  # UI 显式点击→真探测；其余走 5 分钟 TTL 缓存
        try:
            if provider in ("all", "*"):
                out = {}
                for p in settings.load().get("providers", {}):
                    out[p] = settings.probe(p, force=force)
                self._send(200, {"ok": True, "results": out})
            else:
                self._send(200, settings.probe(provider, force=force))
        except Exception as e:
            self._send(500, {"error": f"probe 失败: {e}"})

    def _admin_apply(self, req):
        patch = {k: v for k, v in req.items() if v is not None}
        try:
            settings.apply(patch)
            self._send(200, {"ok": True})
        except ValueError as e:
            self._send(400, {"error": str(e)})
        except Exception as e:
            self._send(500, {"error": f"apply 失败: {e}"})

    def _admin_switch(self, req):
        """自动审查总开关：{"enabled": bool} 或缺省=查询。"""
        try:
            from mjc import autoswitch
            if "enabled" in req:
                st = autoswitch.set_enabled(bool(req.get("enabled")), by="webui")
            else:
                st = autoswitch.state()
            self._send(200, {"ok": True, "auto_switch": st})
        except Exception as e:
            self._send(500, {"error": f"switch 失败: {e}"})


def main(host=None, port=None):
    host = host or os.environ.get("MJC_WEBUI_HOST", "127.0.0.1")
    port = port or int(os.environ.get("MJC_WEBUI_PORT", "8123"))
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(f"⚠️ 非回环地址 {host} —— 红线：本工具不应对公网开放", file=sys.stderr)
        if not _require_token():
            print("🚫 已拒绝启动：非回环绑定必须设置 MJC_WEBUI_TOKEN（安全红线）", file=sys.stderr)
            return 1
        print("🔐 已启用 token 鉴权（MJC_WEBUI_TOKEN）", file=sys.stderr)
    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"MJC Web UI → http://{host}:{port}  (Ctrl+C 退出)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")
        srv.server_close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("MJC_WEBUI_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("MJC_WEBUI_PORT", "8123")))
    a = ap.parse_args()
    sys.exit(main(a.host, a.port) or 0)
