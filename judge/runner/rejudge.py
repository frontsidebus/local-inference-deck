#!/usr/bin/env python3
"""Re-run the judge on EXISTING evidence bundles, for comparing prompts/validators/models on identical input.

    rejudge.py <request-id>... --out DIR [--mode local|frontier] [--model X] [--no-budget] [--sensitive-local]

Reads the request from queue/ or done/ and the bundle from evidence/<id>/ under $JUDGE_REVIEW_DIR (else the
usual review dir). It never collects evidence, never runs probes (PROBES ALLOWED: no) and never writes to
queue/, done/, findings/, acks/ or the evidence bundle. Per request it writes to DIR:
  <id>.json        the new finding (same validation, verdict rules and local severity cap as run_judge.py)
  <id>.md          its rendered form
  <id>.raw.txt     the judge's raw replies
  <id>.input.txt   the exact user message sent to the judge
and finally DIR/summary.json + a stdout table comparing each new finding with findings/<id>.json (read only).

--mode    default: what run_judge.py would choose (a sensitive bundle is judged locally). An explicit
          `--mode frontier` on a sensitive bundle is REFUSED for that request: no model call, an error in
          summary.json and the table, exit 1. The local model may be a large one that evicts other models,
          so rejudge never falls back to it silently.
--sensitive-local  with `--mode frontier`, judge sensitive bundles locally instead (with a note).
--model   sets JUDGE_LOCAL_MODEL or JUDGE_FRONTIER_MODEL (by --mode; both without --mode), e.g. `--model vision`.
--no-budget  do not count frontier calls against JUDGE_FRONTIER_DAILY_MAX (usage.json is then not touched).
Exit: 0 all judged, 1 at least one request failed, 64 usage.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402
import run_judge as RJ  # noqa: E402

PROTECTED = ("queue", "done", "findings", "acks", "evidence")


def _summ(f: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not isinstance(f, dict):
        return None
    items = f.get("items") or []
    return {"mode": f.get("mode"), "judge": f.get("judge"), "items": len(items),
            "severity": dict(Counter(str(i.get("severity")) for i in items)),
            "verdict": dict(Counter(str(i.get("verdict")) for i in items)),
            "high_false": sum(1 for i in items if i.get("severity") == "high" and i.get("verdict") == "false")}


def _check_out(out: Path) -> Optional[str]:
    rd = C.review_dir().resolve()
    o = out.resolve()
    for name in PROTECTED:
        p = (rd / name).resolve()
        if o == p or p in o.parents:
            return f"--out must not be inside {p}"
    return None


def rejudge_one(rid: str, out: Path, mode_arg: Optional[str], use_budget: bool,
                sensitive_local: bool = False) -> Dict[str, Any]:
    row: Dict[str, Any] = {"request": rid}
    request = C.request_for(rid)
    if request is None:
        row["error"] = "request not found in queue/ or done/"
        return row
    ev = C.sub("evidence") / rid
    try:
        manifest = C.read_json(ev / "manifest.json")
        if not isinstance(manifest, dict):
            raise ValueError("not an object")
    except Exception as exc:
        row["error"] = f"no usable evidence bundle at {ev} ({exc}); rejudge never collects"
        return row
    data_class = RJ.bundle_data_class(request, manifest)
    mode, notes = RJ.choose_mode(data_class)
    if mode_arg:
        if data_class != "infra" and mode_arg == "frontier":
            if not sensitive_local:
                row["refused"] = True
                row["error"] = (f"refused: --mode frontier on a data_class={data_class} bundle (sensitive data never "
                                f"goes to the frontier judge); not judged. Pass --sensitive-local to judge it "
                                f"locally with JUDGE_LOCAL_MODEL instead")
                return row
            notes.append(f"rejudge: --mode frontier refused for data_class={data_class}; judged locally "
                         f"(--sensitive-local)")
            mode = "local"
        else:
            mode = mode_arg
    notes.append("rejudge: re-judged an existing bundle (no collection, no probes)")
    try:
        res = RJ.judge_bundle(rid, request, ev, mode, notes, probes_allowed=False, use_budget=use_budget)
    except RJ.JudgeError as exc:
        row["error"] = f"judge backend failed: {exc}"
        return row
    finding = res["finding"]
    finding["notes"] = res["notes"]
    C.write_json(out / f"{rid}.json", finding)
    C.atomic_write(out / f"{rid}.md", RJ.render_md(finding, res["notes"]))
    C.atomic_write(out / f"{rid}.raw.txt", res["raw_record"] + "\n")
    C.atomic_write(out / f"{rid}.input.txt", res["input"])
    old = None
    try:
        old = C.read_json(C.sub("findings") / f"{rid}.json")
    except Exception:
        pass
    row.update({"old": _summ(old), "new": _summ(finding)})
    return row


def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(prog="rejudge.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("ids", nargs="+")
    ap.add_argument("--mode", choices=("local", "frontier"))
    ap.add_argument("--model")
    ap.add_argument("--out", required=True)
    ap.add_argument("--no-budget", action="store_true")
    ap.add_argument("--sensitive-local", action="store_true",
                    help="with --mode frontier: judge sensitive bundles locally instead of refusing them")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code == 0 else 64
    out = Path(os.path.expanduser(args.out))
    err = _check_out(out)
    if err:
        print(err, file=sys.stderr)
        return 64
    bad = [r for r in args.ids if not C.REQUEST_ID_RE.match(r)]
    if bad:
        print(f"invalid request id(s): {bad}", file=sys.stderr)
        return 64
    if args.model:
        if args.mode != "local":
            os.environ["JUDGE_FRONTIER_MODEL"] = args.model
        if args.mode != "frontier":  # no --mode: the mode is chosen per request, so set both
            os.environ["JUDGE_LOCAL_MODEL"] = args.model
    out.mkdir(parents=True, exist_ok=True)
    rows = [rejudge_one(rid, out, args.mode, not args.no_budget, args.sensitive_local) for rid in args.ids]
    C.write_json(out / "summary.json", {"created": C.iso(C.utc_now()), "mode": args.mode, "model": args.model,
                                        "sensitive_local": args.sensitive_local, "requests": rows})
    print(f"{'request':40} {'old (mode items high-false)':32} new (mode items high-false)")
    for r in rows:
        if "error" in r:
            print(f"{r['request']:40} {'REFUSED' if r.get('refused') else 'ERROR'} {r['error']}")
            continue
        o, n = r["old"], r["new"]
        fo = f"{o['mode']} {o['items']} {o['high_false']}" if o else "-"
        print(f"{r['request']:40} {fo:32} {n['mode']} {n['items']} {n['high_false']}")
    return 1 if any("error" in r for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
