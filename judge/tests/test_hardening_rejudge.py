"""Judge hardening, runner side: rejudge.py notes a code version mismatch between the request / bundle and the
code re-judging it (a warning, never a failure) and records code_versions in the finding. Stub backends only."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "runner"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import rejudge as RG  # noqa: E402
from lib import queue as q  # noqa: E402
from lib import version  # noqa: E402
from test_runner import enqueue as rq_enqueue, env, local, rid  # noqa: E402,F401  (fixtures)


# ------------------------------------------------------------------ rejudge warns on a version mismatch
def test_rejudge_notes_version_mismatch(env, local, tmp_path):
    review = env
    r = rid()
    req = rq_enqueue(review, r)
    req["code_version"] = {"sha": "1" * 40, "commit": None, "dirty": False, "source": "git"}
    (review / "queue" / f"{r}.json").write_text(json.dumps(req))
    man = json.loads((review / "evidence" / r / "manifest.json").read_text())
    man["code_versions"] = {"request": req["code_version"], "collector": req["code_version"]}
    (review / "evidence" / r / "manifest.json").write_text(json.dumps(man))
    out = tmp_path / "out"
    assert RG.main([r, "--mode", "local", "--out", str(out)]) == 0      # a warning, not a failure
    new = json.loads((out / f"{r}.json").read_text())
    cur = version.code_version()
    assert new["code_versions"] == {"request": req["code_version"], "collector": req["code_version"],
                                    "runner": cur}
    vn = [n for n in new["notes"] if n.startswith("code version:")]
    assert len(vn) == 2 and "request written by judge code 111111111111" in vn[0]
    assert "evidence bundle written by judge code" in vn[1]
    assert json.loads((out / "summary.json").read_text())["requests"][0]["version_warning"] is True
    assert q.validate_finding(new) == []


def test_rejudge_same_version_no_note(env, local, tmp_path, monkeypatch):
    review = env
    same = {"sha": "2" * 40, "commit": None, "dirty": False, "source": "git"}
    monkeypatch.setattr(version, "code_version", lambda *a, **k: dict(same))
    r = rid()
    req = rq_enqueue(review, r)
    req["code_version"] = same
    (review / "queue" / f"{r}.json").write_text(json.dumps(req))
    man = json.loads((review / "evidence" / r / "manifest.json").read_text())
    man["code_versions"] = {"request": same, "collector": same}
    (review / "evidence" / r / "manifest.json").write_text(json.dumps(man))
    out = tmp_path / "out"
    assert RG.main([r, "--mode", "local", "--out", str(out)]) == 0
    new = json.loads((out / f"{r}.json").read_text())
    assert not [n for n in new["notes"] if n.startswith("code version:")]
    assert "version_warning" not in json.loads((out / "summary.json").read_text())["requests"][0]
