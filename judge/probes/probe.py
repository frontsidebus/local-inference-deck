#!/usr/bin/env python3
"""judge/probes/probe.py: allowlisted, read-only claim probes for the judge.

Usage: probe.py <name> [args...]
    ssh_alias_test <alias>                  ssh -G summary + `ssh -o BatchMode=yes <alias> true`
                                            (alias must be in JUDGE_SSH_ALIASES or a Host in ~/.ssh/config)
    port_listening <walter|covenant> <port> `ss -ltnH` on the host
    http_status <https-url-on-SPARK_DOMAIN> status code + redirect target, no cookies, no body
    unit_state <walter|covenant> <unit>     `systemctl show` state properties
    file_hash <walter|covenant|local> <abs-path>   sha256 + owner/mode/size/mtime
    render_and_diff <repo-template-path> [<walter|covenant>:<abs-live-path>]
                                            render a repo *.tmpl with site.env; diff against the live file
                                            when given (both sides redacted), else print the redacted render
    check_sanitized <repo-path>             the judge repo's scripts/check-sanitized.sh run in <repo-path>
    slots                                   llama-server /slots summary from Walter's per-model localhost ports

Every probe validates each argument against a regex before anything runs; an unknown name or a bad
argument exits 64 with no execution. Commands are fixed and read-only; each step has a timeout
(JUDGE_PROBE_TIMEOUT, default 20 s). Output: the commands, their stdout+stderr (redacted) and exit codes.
Exit status: 0 if every step succeeded, else the first failing step's code (124 = timeout).

For tests/other callers: run_probe(name, args, cfg=None, runner=None) -> (exit_code, text) and
slots_summary(cfg=None, runner=None) -> dict. A runner is `runner(argv, timeout, cwd=None) -> (rc, out, err)`.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

JUDGE_DIR = Path(__file__).resolve().parent.parent
if str(JUDGE_DIR) not in sys.path:
    sys.path.insert(0, str(JUDGE_DIR))

from lib import config  # noqa: E402
from lib.redact import redact  # noqa: E402

EX_USAGE = 64
Runner = Callable[..., Tuple[int, str, str]]


class UsageError(Exception):
    pass


def subprocess_runner(argv: Sequence[str], timeout: float, cwd: Optional[str] = None) -> Tuple[int, str, str]:
    try:
        cp = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout, cwd=cwd,
                            stdin=subprocess.DEVNULL, errors="replace")
        return cp.returncode, cp.stdout, cp.stderr
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        return 124, out, f"timeout after {timeout}s"
    except OSError as e:
        return 127, "", f"cannot run {argv[0]}: {e.strerror}"


# ---------------------------------------------------------------- arg validation
HOST_RE = re.compile(r"^(walter|covenant)$")
HOST_OR_LOCAL_RE = re.compile(r"^(walter|covenant|local)$")
ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
PORT_RE = re.compile(r"^[0-9]{1,5}$")
UNIT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9@._:\\-]{0,127}$")
ABS_PATH_RE = re.compile(r"^/[A-Za-z0-9._@+/-]{0,1023}$")
TEMPLATE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._/-]{0,255}\.tmpl$")
LIVE_RE = re.compile(r"^(walter|covenant):(/[A-Za-z0-9._@+/-]{1,1023})$")
LIVE_ALLOWED_PREFIXES = ("/etc/", "/srv/", "/usr/local/", "/opt/")
LIVE_DENY_RE = re.compile(r"(?i)(key|secret|token|passw|credential|htpasswd|\.pem$|\.env$|wireguard|shadow|"
                          r"private|\.p12$|\.pfx$|letsencrypt/(live|archive))")


def _no_dotdot(p: str) -> bool:
    return ".." not in p.split("/") and "//" not in p


def _check(rx: re.Pattern, val: str, what: str) -> str:
    if not isinstance(val, str) or not rx.match(val):
        raise UsageError(f"bad {what}: {val!r}")
    return val


def _ssh_config_hosts() -> List[str]:
    out = []
    try:
        for line in Path(os.path.expanduser("~/.ssh/config")).read_text(errors="replace").splitlines():
            parts = line.strip().split()
            if len(parts) >= 2 and parts[0].lower() == "host":
                out += [h for h in parts[1:] if not any(c in h for c in "*?!")]
    except OSError:
        pass
    return out


def _abs_path(val: str) -> str:
    _check(ABS_PATH_RE, val, "absolute path")
    if not _no_dotdot(val):
        raise UsageError(f"bad absolute path: {val!r}")
    return val


def _https_url(val: str, cfg: Mapping[str, str]) -> str:
    dom = (cfg.get("SPARK_DOMAIN") or "").strip().lower()
    if not dom:
        raise UsageError("SPARK_DOMAIN is not configured")
    rx = re.compile(r"^https://(?:[a-z0-9-]+\.)*" + re.escape(dom) + r"(?::[0-9]{1,5})?(?:/[A-Za-z0-9._~/?=&%+:-]*)?$")
    return _check(rx, val, "url")


# ---------------------------------------------------------------- probe implementations
class Ctx:
    def __init__(self, cfg: Mapping[str, str], runner: Runner, timeout: float):
        self.cfg, self.runner, self.timeout = cfg, runner, timeout
        self.lines: List[str] = []
        self.rc = 0

    def step(self, argv: Sequence[str], cwd: Optional[str] = None, show: bool = True,
             timeout: Optional[float] = None) -> Tuple[int, str, str]:
        rc, out, err = self.runner(list(argv), timeout or self.timeout, cwd=cwd)
        if show:
            self.lines.append("$ " + " ".join(shlex.quote(a) for a in argv) + (f"   (cwd {cwd})" if cwd else ""))
            if out:
                self.lines.append(out.rstrip("\n"))
            if err:
                self.lines.append("[stderr] " + err.rstrip("\n"))
            self.lines.append(f"# exit {rc}")
        if rc != 0 and self.rc == 0:
            self.rc = rc
        return rc, out, err

    def note(self, text: str) -> None:
        self.lines.append(text)

    def ssh(self, host: str, remote_cmd: str, **kw) -> Tuple[int, str, str]:
        return self.step(config.host_ssh(host, self.cfg) + [remote_cmd], **kw)


def p_ssh_alias_test(ctx: Ctx, alias: str) -> None:
    ssh_opts = ["-o", "BatchMode=yes", "-o", f"ConnectTimeout={ctx.cfg.get('JUDGE_SSH_CONNECT_TIMEOUT') or 8}"]
    rc, out, err = ctx.step(["ssh", "-G", alias], show=False)
    keep = [ln for ln in out.splitlines() if ln.split(" ", 1)[0] in ("hostname", "user", "port", "identityfile")]
    ctx.note(f"$ ssh -G {alias}   (filtered: hostname user port identityfile)")
    ctx.note("\n".join(keep) if keep else f"[stderr] {err.strip()}")
    ctx.note(f"# exit {rc}")
    ctx.step(["ssh", *ssh_opts, alias, "true"])


def p_port_listening(ctx: Ctx, host: str, port: str) -> None:
    rc, out, _ = ctx.ssh(host, f"ss -ltnH 'sport = :{int(port)}'")
    if rc == 0:
        ctx.note(f"RESULT: port {int(port)} on {host}: {'LISTENING' if out.strip() else 'NOT LISTENING'}")


def p_http_status(ctx: Ctx, url: str) -> None:
    ctx.step(["curl", "-sS", "-o", "/dev/null", "--max-time", "15", "--proto", "=https", "--max-redirs", "0",
              "-w", "http_code=%{http_code} redirect_url=%{redirect_url} ssl_verify_result=%{ssl_verify_result}\\n",
              "--", url])


def p_unit_state(ctx: Ctx, host: str, unit: str) -> None:
    ctx.ssh(host, "systemctl show --no-pager -p Id,LoadState,ActiveState,SubState,UnitFileState,"
                  f"ActiveEnterTimestamp,NRestarts,Result -- {shlex.quote(unit)}")


def p_file_hash(ctx: Ctx, host: str, path: str) -> None:
    if host == "local":
        ctx.step(["sha256sum", "--", path])
        ctx.step(["stat", "-c", "%U:%G %a %s %y", "--", path])
    else:
        qp = shlex.quote(path)
        ctx.ssh(host, f"sha256sum -- {qp}; stat -c '%U:%G %a %s %y' -- {qp}")


def p_render_and_diff(ctx: Ctx, template: str, live: Optional[str] = None) -> None:
    repo = ctx.cfg.get("JUDGE_REPO_DIR") or str(config.REPO_ROOT)
    render = os.path.join(repo, "scripts", "render.sh")
    src = os.path.join(repo, template)
    if not os.path.isfile(src):
        ctx.note(f"template not found in {repo}: {template}")
        ctx.rc = ctx.rc or 1
        return
    with tempfile.TemporaryDirectory(prefix="judge-render-") as td:
        os.chmod(td, 0o700)
        dst = os.path.join(td, "rendered")
        argv = ["bash", render]
        if ctx.cfg.get("SITE_ENV"):
            argv += ["-e", ctx.cfg["SITE_ENV"]]
        rc, _, _ = ctx.step(argv + [src, dst], cwd=repo)
        if rc != 0:
            return
        try:
            rendered = redact(Path(dst).read_text(errors="replace"))
        except OSError as e:
            ctx.note(f"cannot read render output: {e.strerror}")
            ctx.rc = ctx.rc or 1
            return
    if not live:
        ctx.note(f"--- rendered {template} (redacted) ---")
        ctx.note(rendered.rstrip("\n"))
        return
    host, lpath = LIVE_RE.match(live).groups()
    rc, out, _ = ctx.ssh(host, f"cat -- {shlex.quote(lpath)}", show=False)
    ctx.note(f"$ ssh {host} cat -- {lpath}   (output redacted, shown as diff)\n# exit {rc}")
    if rc != 0:
        return
    diff = list(difflib.unified_diff(rendered.splitlines(), redact(out).splitlines(),
                                     fromfile=f"rendered/{template}", tofile=f"{host}:{lpath}", lineterm=""))
    ctx.note("\n".join(diff) if diff else "RESULT: no differences (after redaction)")


def p_check_sanitized(ctx: Ctx, repo_path: str) -> None:
    script = os.path.join(ctx.cfg.get("JUDGE_REPO_DIR") or str(config.REPO_ROOT), "scripts", "check-sanitized.sh")
    if not os.path.isfile(script):
        ctx.note(f"check-sanitized.sh not found at {script}")
        ctx.rc = ctx.rc or 1
        return
    if not os.path.isdir(repo_path) or not os.path.exists(os.path.join(repo_path, ".git")):
        ctx.note(f"not a git worktree: {repo_path}")
        ctx.rc = ctx.rc or 1
        return
    ctx.step(["bash", script], cwd=repo_path)


_PORT_MAP_RE = re.compile(r"(?:\d{1,3}(?:\.\d{1,3}){3}|\[?::\]?):(\d{1,5})->\d{1,5}/tcp")


def _slot_fields(s: Dict, threshold: int) -> Dict:
    nt = s.get("next_token")
    if isinstance(nt, list):
        nt = nt[0] if nt and isinstance(nt[0], dict) else {}
    nt = nt if isinstance(nt, dict) else {}
    params = s.get("params") if isinstance(s.get("params"), dict) else {}
    n_predict = params.get("n_predict", s.get("n_predict"))
    n_decoded = nt.get("n_decoded", s.get("n_decoded"))
    busy = s.get("is_processing", s.get("state") not in (None, 0))
    return {
        "id": s.get("id"),
        "id_task": s.get("id_task"),
        "is_processing": busy,
        "n_decoded": n_decoded,
        "n_predict": n_predict,
        "max_tokens": params.get("max_tokens"),
        "runaway_suspect": bool(busy and n_predict in (-1, None) and isinstance(n_decoded, int)
                                and n_decoded >= threshold),
    }


def slots_summary(cfg: Optional[Mapping[str, str]] = None, runner: Optional[Runner] = None,
                  timeout: Optional[float] = None, ctx: Optional[Ctx] = None) -> Dict:
    """Per-model llama-server slot summary from Walter (no API key: localhost ports of the model containers).

    Shape: {"<model container name>": [slot, ...], ...,
            "_collected": "...Z", "_error": "<string, only on failure>"}
    Slot: {id, id_task, is_processing, n_decoded, n_predict, max_tokens, runaway_suspect}. Keys starting with
    "_" are metadata and always hold strings; a model whose /slots could not be read maps to [] and is named
    in "_error"."""
    cfg = cfg or config.load_config()
    ctx = ctx or Ctx(cfg, runner or subprocess_runner, timeout or float(cfg.get("JUDGE_PROBE_TIMEOUT") or 20))
    res: Dict = {"_collected": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    try:
        threshold = int(cfg.get("JUDGE_RUNAWAY_TOKENS") or 20000)
    except ValueError:
        threshold = 20000
    rc, out, err = ctx.ssh("walter", "docker ps --filter label=llama-swap=1 --format '{{.Names}}\t{{.Ports}}'")
    if rc != 0:
        res["_error"] = f"docker ps on walter failed (exit {rc}): {redact(err.strip())[:300]}"
        return res
    models: List[Tuple[str, int]] = []
    for line in out.splitlines():
        name, _, ports = line.partition("\t")
        m = _PORT_MAP_RE.search(ports)
        if name.strip() and not name.startswith("_") and m and 0 < int(m.group(1)) < 65536:
            models.append((name.strip(), int(m.group(1))))
    if not models:
        res["_note"] = "no llama-swap model containers running"
        return res
    loop = "; ".join(f"echo '=== {p}'; curl -s -m 5 http://127.0.0.1:{p}/slots; echo" for _, p in models)
    rc, out, err = ctx.ssh("walter", loop)
    chunks: Dict[int, str] = {}
    cur = None
    for line in out.splitlines():
        m = re.match(r"^=== (\d+)$", line)
        if m:
            cur = int(m.group(1))
            chunks[cur] = ""
        elif cur is not None:
            chunks[cur] += line + "\n"
    errors = []
    for name, port in models:
        try:
            raw = json.loads(chunks.get(port, "").strip() or "null")
            if not isinstance(raw, list):
                raise ValueError("not a slot list")
            res[name] = [_slot_fields(s, threshold) for s in raw if isinstance(s, dict)]
        except ValueError as e:
            res[name] = []
            errors.append(f"{name} (127.0.0.1:{port}): no slot data ({str(e)[:80]})")
    if errors:
        res["_error"] = "; ".join(errors)
    return res


def p_slots(ctx: Ctx) -> None:
    ctx_quiet = Ctx(ctx.cfg, ctx.runner, ctx.timeout)
    res = slots_summary(ctx.cfg, ctx=ctx_quiet)
    ctx.rc = ctx_quiet.rc
    ctx.note(json.dumps(res, indent=2))


# name -> (validators for required args, validators for optional args, implementation)
def _registry(cfg: Mapping[str, str]):
    def alias_ok(v):
        _check(ALIAS_RE, v, "alias")
        allowed = set((cfg.get("JUDGE_SSH_ALIASES") or "").split()) | set(_ssh_config_hosts())
        if v not in allowed:
            raise UsageError(f"alias {v!r} is not in JUDGE_SSH_ALIASES or ~/.ssh/config")
        return v

    def port_ok(v):
        _check(PORT_RE, v, "port")
        if not 0 < int(v) < 65536:
            raise UsageError(f"bad port: {v!r}")
        return v

    def unit_ok(v):
        return _check(UNIT_RE, v, "unit")

    def template_ok(v):
        _check(TEMPLATE_RE, v, "template path")
        if not _no_dotdot(v):
            raise UsageError(f"bad template path: {v!r}")
        return v

    def live_ok(v):
        m = LIVE_RE.match(v or "")
        if not m or not _no_dotdot(m.group(2)) or not m.group(2).startswith(LIVE_ALLOWED_PREFIXES) \
                or LIVE_DENY_RE.search(m.group(2)):
            raise UsageError(f"bad live target: {v!r} (host:/etc|/srv|/usr/local|/opt path, no secret files)")
        return v

    return {
        "ssh_alias_test": ([alias_ok], [], p_ssh_alias_test),
        "port_listening": ([lambda v: _check(HOST_RE, v, "host"), port_ok], [], p_port_listening),
        "http_status": ([lambda v: _https_url(v, cfg)], [], p_http_status),
        "unit_state": ([lambda v: _check(HOST_RE, v, "host"), unit_ok], [], p_unit_state),
        "file_hash": ([lambda v: _check(HOST_OR_LOCAL_RE, v, "host"), _abs_path], [], p_file_hash),
        "render_and_diff": ([template_ok], [live_ok], p_render_and_diff),
        "check_sanitized": ([_abs_path], [], p_check_sanitized),
        "slots": ([], [], p_slots),
    }


PROBE_NAMES = ("ssh_alias_test", "port_listening", "http_status", "unit_state", "file_hash", "render_and_diff",
               "check_sanitized", "slots")


def validate_args(name: str, args: Sequence[str], cfg: Mapping[str, str]):
    reg = _registry(cfg)
    if name not in reg:
        raise UsageError(f"unknown probe: {name!r} (allowed: {', '.join(PROBE_NAMES)})")
    req, opt, impl = reg[name]
    if not len(req) <= len(args) <= len(req) + len(opt):
        raise UsageError(f"{name}: expected {len(req)}..{len(req) + len(opt)} args, got {len(args)}")
    for v, val in zip(req + opt, args):
        v(val)
    return impl


def run_probe(name: str, args: Sequence[str], cfg: Optional[Mapping[str, str]] = None,
              runner: Optional[Runner] = None) -> Tuple[int, str]:
    """Validate then run. Returns (exit_code, text). Bad name/args -> (64, message), nothing executed."""
    cfg = cfg or config.load_config()
    try:
        impl = validate_args(name, list(args), cfg)
    except UsageError as e:
        return EX_USAGE, f"probe: {e}\n"
    try:
        timeout = float(cfg.get("JUDGE_PROBE_TIMEOUT") or 20)
    except ValueError:
        timeout = 20.0
    ctx = Ctx(cfg, runner or subprocess_runner, timeout)
    ctx.note(f"# probe {name} {' '.join(args)}  ({datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')})")
    try:
        impl(ctx, *args)
    except ValueError as e:  # e.g. host_ssh() without the site vars
        ctx.note(f"probe error: {e}")
        ctx.rc = ctx.rc or 1
    ctx.note(f"# probe exit {ctx.rc}")
    return ctx.rc, redact("\n".join(ctx.lines)) + "\n"


def main(argv: Optional[Sequence[str]] = None, runner: Optional[Runner] = None,
         cfg: Optional[Mapping[str, str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if argv else EX_USAGE
    rc, text = run_probe(argv[0], argv[1:], cfg, runner)
    (sys.stderr if rc == EX_USAGE else sys.stdout).write(text)
    return rc


if __name__ == "__main__":
    sys.exit(main())
