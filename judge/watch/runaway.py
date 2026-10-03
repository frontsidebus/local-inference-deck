#!/usr/bin/env python3
"""C6 runaway-generation watcher for llama-server slots (behind llama-swap on Walter).

Polls ``probes/probe.py slots`` every ``--interval`` seconds (default 30; ``--once`` for one pass) and
alerts when a slot that is processing satisfies EITHER trigger:

  tokens   ``n_decoded >= JUDGE_RUNAWAY_TOKENS`` (default 24000), whatever ``n_predict`` is. Since the
           output cap (gateway ``max_tokens`` clamp + llama-server ``-n``) every slot has ``n_predict > 0``;
           the alert then shows progress ``n_decoded/n_predict``. ``n_predict == -1`` (no cap: the cap was
           lost or bypassed) is called out in the reason.
  minutes  the same ``id_task`` has been processing for ``>= JUDGE_RUNAWAY_MINUTES`` (default 10)

The token default sits above the gateway's default limit (16384, so a request that sets no limit never
trips it) and below the 32768 model maximum (so a request that asked for a near-maximum generation is
flagged roughly three quarters of the way through, a few minutes before it ends).

One alert per (model, slot, id_task). Each alert appends a JSONL line to ``$JUDGE_REVIEW_DIR/watch.log``,
writes a ``runaway`` review request, prints the alert, and runs ``notify-send`` when available and
``DISPLAY`` is set. It NEVER cancels or unloads anything: stopping a generation is a human decision, so the
alert carries the command a human can run to unload the model.

``--slots-file PATH`` (``-`` for stdin) reads a slots JSON document instead of running the probe.
State (first-seen times, alerted tasks) lives in ``$JUDGE_REVIEW_DIR/watch-state.json``.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

sys.dont_write_bytecode = True  # never leave __pycache__ in the judge/ tree
JUDGE_DIR = Path(__file__).resolve().parent.parent
PROBE = JUDGE_DIR / "probes" / "probe.py"
STATE_TTL_S = 24 * 3600
DEFAULT_TOKENS = 24000   # keep in sync with lib/config.py DEFAULTS
DEFAULT_MINUTES = 10


# ------------------------------------------------------------------ config / queue adapters
def _import(name: str) -> Any:
    try:
        if str(JUDGE_DIR) not in sys.path:
            sys.path.insert(0, str(JUDGE_DIR))
        import importlib

        return importlib.import_module(f"lib.{name}")
    except Exception:
        return None


_site_cache: Optional[Dict[str, str]] = None


def _site() -> Dict[str, str]:
    global _site_cache
    if _site_cache is None:
        _site_cache = {}
        cfg = _import("config")
        for fn_name in ("load", "load_config", "load_site_env", "site_env"):
            fn = getattr(cfg, fn_name, None) if cfg else None
            if callable(fn):
                try:
                    data = fn()
                    if isinstance(data, dict):
                        _site_cache = {str(k): str(v) for k, v in data.items() if v is not None}
                        break
                except Exception:
                    continue
    return _site_cache


def setting(name: str, default: str = "") -> str:
    val = os.environ.get(name) or _site().get(name)
    return val if val else default


def _int_setting(name: str, default: int) -> int:
    try:
        return int(float(setting(name, str(default))))
    except ValueError:
        return default


def review_dir() -> Path:
    env = os.environ.get("JUDGE_REVIEW_DIR")
    if env:
        return Path(os.path.expanduser(env))
    return Path(os.path.expanduser(os.environ.get("HERMES_HOME") or "~/.hermes")) / "review"


def _utc_iso(ts: Optional[float] = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else time.time(), timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _append(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as fh:
        fh.write(line.rstrip("\n") + "\n")


def write_request(req: Dict[str, Any]) -> str:
    q = _import("queue")
    fn = getattr(q, "write_request", None) if q else None
    if callable(fn):
        return str(fn(req))
    dst = review_dir() / "queue" / f"{req['id']}.json"
    _atomic_write(dst, json.dumps(req, indent=2))
    return str(dst)


def request_id(session: str, now: float) -> str:
    dt = datetime.fromtimestamp(now, timezone.utc)
    q = _import("queue")
    fn = getattr(q, "new_request_id", None) if q else None
    if callable(fn):
        try:
            return str(fn("runaway", session, dt))
        except Exception:
            pass
    short = re.sub(r"[^A-Za-z0-9]", "", session)[-6:] or "nosess"
    return f"{dt.strftime('%Y%m%dT%H%M%SZ')}-{short}-runaway"


# ------------------------------------------------------------------ slot parsing
def _first(d: Dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


def _as_int(v: Any) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def normalize_slot(raw: Dict[str, Any], model: str) -> Dict[str, Any]:
    """Map a llama-server /slots entry (any recent layout) or a probe summary row to flat fields."""
    params = raw.get("params") if isinstance(raw.get("params"), dict) else {}
    nt = raw.get("next_token")
    if isinstance(nt, list):
        nt = nt[0] if nt and isinstance(nt[0], dict) else {}
    nt = nt if isinstance(nt, dict) else {}
    n_predict = _as_int(_first(raw, "n_predict"))
    if n_predict is None:
        n_predict = _as_int(_first(params, "n_predict", "max_tokens"))
    n_decoded = _as_int(_first(raw, "n_decoded"))
    if n_decoded is None:
        n_decoded = _as_int(nt.get("n_decoded"))
    processing = raw.get("is_processing")
    if processing is None:
        processing = raw.get("state") not in (None, 0, "idle")
    return {
        "model": str(raw.get("model") or model or "?"),
        "slot": _as_int(raw.get("id")) if raw.get("id") is not None else _as_int(raw.get("slot")),
        "id_task": _as_int(raw.get("id_task")),
        "is_processing": bool(processing),
        "n_predict": n_predict,
        "n_decoded": n_decoded,
        "n_ctx": _as_int(raw.get("n_ctx")),
        "truncated": raw.get("truncated", nt.get("truncated")),
    }


def iter_slots(data: Any, model: str = "") -> Iterator[Dict[str, Any]]:
    """Find every dict carrying ``id_task`` anywhere in *data*; the model name is taken from the nearest
    enclosing ``model``/``name`` field or mapping key (probe output may be per-model or flat)."""
    if isinstance(data, list):
        for item in data:
            yield from iter_slots(item, model)
    elif isinstance(data, dict):
        if "id_task" in data:
            yield normalize_slot(data, model)
            return
        here = data.get("model") or data.get("name") or model
        here = here if isinstance(here, str) else model
        for k, v in data.items():
            if isinstance(v, (dict, list)):
                sub_model = here
                if isinstance(v, (list, dict)) and k not in ("slots", "data", "models", "upstreams") \
                        and not data.get("model"):
                    sub_model = k if isinstance(k, str) else here
                yield from iter_slots(v, sub_model)


def parse_probe_output(text: str) -> Any:
    text = text.strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    m = re.search(r"[\[{]", text)
    if not m:
        raise ValueError("no JSON in slots probe output")
    obj, _ = json.JSONDecoder().raw_decode(text[m.start():])
    return obj


def fetch_slots(timeout: float = 60) -> Any:
    cp = subprocess.run([sys.executable, str(PROBE), "slots"], capture_output=True, text=True,
                        timeout=timeout, stdin=subprocess.DEVNULL)
    if cp.returncode != 0:
        raise RuntimeError(f"slots probe exit {cp.returncode}: {(cp.stderr or cp.stdout)[-300:]}")
    return parse_probe_output(cp.stdout)


# ------------------------------------------------------------------ detection
def task_key(s: Dict[str, Any]) -> str:
    return f"{s['model']}:{s['slot']}:{s['id_task']}"


def evaluate(slots: List[Dict[str, Any]], state: Dict[str, Any], now: float, tokens: int,
             minutes: float) -> List[Dict[str, Any]]:
    """Update *state* in place and return new alerts (each task key alerts at most once)."""
    tasks: Dict[str, Any] = state.setdefault("tasks", {})
    alerted: Dict[str, Any] = state.setdefault("alerted", {})
    alerts = []
    for s in slots:
        if not s.get("is_processing") or s.get("id_task") is None:
            continue
        key = task_key(s)
        t = tasks.get(key)
        if t is None:
            t = tasks[key] = {"first_seen": now, "last_seen": now, "n_decoded": s.get("n_decoded")}
        prev_seen, prev_dec = t.get("last_seen", now), t.get("n_decoded")
        rate = None
        if isinstance(prev_dec, int) and isinstance(s.get("n_decoded"), int) and now > prev_seen:
            rate = round((s["n_decoded"] - prev_dec) / (now - prev_seen), 1)
        t.update(last_seen=now, n_decoded=s.get("n_decoded"))
        if rate is not None:
            t["tok_per_s"] = rate
        elapsed_min = (now - t["first_seen"]) / 60.0
        reasons = []
        n_dec, n_pred = s.get("n_decoded") or 0, s.get("n_predict")
        if n_dec >= tokens:
            if isinstance(n_pred, int) and n_pred > 0:
                pct = min(100, round(100 * n_dec / n_pred))
                reasons.append(f"n_decoded={n_dec} >= {tokens} (cap n_predict={n_pred}: {n_dec}/{n_pred}, {pct}%)")
            elif n_pred == -1:
                reasons.append(f"n_decoded={n_dec} >= {tokens} with NO output cap (n_predict=-1)")
            else:
                reasons.append(f"n_decoded={n_dec} >= {tokens} (n_predict unknown)")
        if elapsed_min >= minutes:
            reasons.append(f"task {s['id_task']} processing for {elapsed_min:.1f} min >= {minutes:g}")
        if reasons and key not in alerted:
            alerted[key] = now
            alerts.append(dict(s, key=key, reasons=reasons, elapsed_min=round(elapsed_min, 1),
                               tok_per_s=t.get("tok_per_s")))
    # forget tasks that finished; drop old alert marks
    live = {task_key(s) for s in slots if s.get("is_processing") and s.get("id_task") is not None}
    for k in [k for k in tasks if k not in live]:
        del tasks[k]
    for k in [k for k, ts in alerted.items() if now - float(ts) > STATE_TTL_S]:
        del alerted[k]
    return alerts


def unload_command(model: str) -> str:
    user = setting("BACKEND_SSH_USER", "<BACKEND_SSH_USER>")
    host = setting("BACKEND_LAN_IP", "<BACKEND_LAN_IP>")
    safe = model if re.fullmatch(r"[\w.:-]+", model or "") else "<model-id>"
    # same as the llama-swap cmdStop for these models; llama-swap reloads it on the next request
    return f"ssh {user}@{host} docker rm -f {safe}"


# ------------------------------------------------------------------ alert side effects
def load_state() -> Dict[str, Any]:
    p = review_dir() / "watch-state.json"
    try:
        data = json.loads(p.read_text())
        state = data if isinstance(data, dict) else {}
    except Exception:
        state = {}
    # alerts already in watch.log also count (state file lost or reset)
    alerted = state.setdefault("alerted", {})
    try:
        lines = (review_dir() / "watch.log").read_text().splitlines()[-500:]
        for line in lines:
            try:
                rec = json.loads(line)
                if rec.get("key") and rec["key"] not in alerted:
                    alerted[rec["key"]] = rec.get("epoch", time.time())
            except ValueError:
                continue
    except OSError:
        pass
    return state


def save_state(state: Dict[str, Any]) -> None:
    _atomic_write(review_dir() / "watch-state.json", json.dumps(state))


def notify(summary: str, body: str) -> None:
    if os.environ.get("DISPLAY") and shutil.which("notify-send"):
        try:
            subprocess.run(["notify-send", "-u", "critical", summary, body], timeout=10,
                           capture_output=True)
        except Exception:
            pass


def emit(alert: Dict[str, Any], now: float, out: Any) -> None:
    cmd = unload_command(alert["model"])
    rec = {"ts": _utc_iso(now), "epoch": now, "event": "runaway", **alert, "unload_cmd": cmd}
    _append(review_dir() / "watch.log", json.dumps(rec, sort_keys=True))
    text = (f"RUNAWAY {alert['model']} slot {alert['slot']} task {alert['id_task']}: "
            + "; ".join(alert["reasons"])
            + (f" (~{alert['tok_per_s']} tok/s)" if alert.get("tok_per_s") else ""))
    out.write(text + f"\n  Not cancelled. To stop it, a human can unload the model:\n    {cmd}\n")
    out.flush()
    try:
        session = f"watch-task{alert['id_task']}"
        write_request({
            "id": request_id(session, now), "kind": "runaway", "session": session,
            "created": _utc_iso(now), "since": _utc_iso(now - alert["elapsed_min"] * 60 - 60),
            "changed_paths": [], "claims": text[:4000], "plan": None, "data_class": "infra",
            "source_event": "watch", "detail": dict(alert, unload_cmd=cmd),
        })
    except Exception as e:
        out.write(f"  (could not write runaway request: {e})\n")
    notify(f"Runaway generation: {alert['model']}", text)


def run_once(slots_data: Any, out: Any, now: Optional[float] = None) -> List[Dict[str, Any]]:
    now = time.time() if now is None else now
    state = load_state()
    alerts = evaluate(list(iter_slots(slots_data)), state, now,
                      _int_setting("JUDGE_RUNAWAY_TOKENS", DEFAULT_TOKENS),
                      float(_int_setting("JUDGE_RUNAWAY_MINUTES", DEFAULT_MINUTES)))
    for a in alerts:
        emit(a, now, out)
    save_state(state)
    return alerts


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--once", action="store_true", help="single pass, then exit")
    ap.add_argument("--interval", type=float, default=30.0, help="seconds between polls (default 30)")
    ap.add_argument("--slots-file", help="read slots JSON from this file ('-' = stdin) instead of the probe")
    ap.add_argument("-v", "--verbose", action="store_true", help="print a one-line summary per slot")
    args = ap.parse_args(argv)
    while True:
        try:
            if args.slots_file:
                text = sys.stdin.read() if args.slots_file == "-" else Path(args.slots_file).read_text()
                data = parse_probe_output(text)
            else:
                data = fetch_slots()
            if args.verbose or args.once:
                for s in iter_slots(data):
                    print(f"{s['model']} slot={s['slot']} task={s['id_task']} processing={s['is_processing']}"
                          f" n_predict={s['n_predict']} n_decoded={s['n_decoded']} n_ctx={s['n_ctx']}")
            run_once(data, sys.stdout)
        except Exception as e:  # keep watching; a probe hiccup is not fatal
            print(f"runaway watch: poll failed: {e}", file=sys.stderr)
            if args.once:
                return 1
        if args.once:
            return 0
        time.sleep(max(5.0, args.interval))


if __name__ == "__main__":
    sys.exit(main())
