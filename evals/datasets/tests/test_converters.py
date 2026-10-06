"""Converter tests on tiny invented fixtures (no network).

Run: python3 -m pytest evals/datasets/tests -q -p no:cacheprovider
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

DS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DS))
import _common as c  # noqa: E402


def load(suite: str):
    spec = importlib.util.spec_from_file_location(f"fetch_{suite.replace('-', '_')}", DS / suite / "fetch.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fx(suite: str, name: str) -> Path:
    return DS / suite / "fixtures" / name


def test_ctibench():
    m = load("ctibench")
    mcq = m.convert_mcq(m.read_tsv(fx("ctibench", "cti-mcq.tsv").read_text()))
    c.validate(mcq)
    assert [i["id"] for i in mcq] == ["ctibench-mcq-0001", "ctibench-mcq-0002"]
    assert mcq[0]["choices"][1] == "B) Network segmentation" and mcq[0]["answer"] == "B"
    assert mcq[1]["answer"] == "A"  # lowercase label in the source is normalized
    assert mcq[0]["meta"]["category"] == "attack.mitre.org/techniques"
    rcm = m.convert_rcm(m.read_tsv(fx("ctibench", "cti-rcm.tsv").read_text()))
    c.validate(rcm)
    assert rcm[0]["answer"] == "CWE-79" and rcm[0]["scorer"] == "cwe_match"
    assert rcm[0]["meta"]["cve"] == "CVE-2099-0001"
    vsp = m.convert_vsp(m.read_tsv(fx("ctibench", "cti-vsp.tsv").read_text()))
    c.validate(vsp)
    assert vsp[0]["scorer"] == "cvss_mae" and vsp[0]["answer"].startswith("CVSS:3.1/AV:N")
    ate = m.convert_ate(m.read_tsv(fx("ctibench", "cti-ate.tsv").read_text()))
    c.validate(ate)
    assert ate[0]["answer"] == ["T1071", "T1083"] and ate[0]["scorer"] == "exact_set"


def test_cybermetric():
    m = load("cybermetric")
    items = m.convert(json.loads(fx("cybermetric", "CyberMetric-80-v1.json").read_text()), "80")
    c.validate(items)
    assert items[1]["choices"] == ["A) 443", "B) 80", "C) 25", "D) 22"]  # sorted by letter
    assert [i["answer"] for i in items] == ["B", "A"]
    assert items[0]["suite"] == "cybermetric-80"


def test_secqa():
    m = load("secqa")
    items = m.convert(fx("secqa", "secqa_v2_test.csv").read_text(), "v2")
    c.validate(items)
    assert items[0]["choices"][2] == 'C) To store logs, "forever"'
    assert [i["answer"] for i in items] == ["B", "C"]


def test_sevenllm():
    m = load("sevenllm")
    lines = fx("sevenllm", "test.jsonl").read_text().splitlines()
    en = m.convert(lines, "en")
    c.validate(en["sevenllm-mcq"] + en["sevenllm-qa"])
    assert len(en["sevenllm-mcq"]) == 1 and en["sevenllm-mcq"][0]["answer"] == "B"
    assert en["sevenllm-mcq"][0]["prompt"].startswith("The invented group")
    qa = en["sevenllm-qa"]
    assert [q["type"] for q in qa] == ["extract", "freeform"]
    assert json.loads(qa[0]["answer"])["malware"] == "ExampleRAT"
    allv = m.convert(lines, "all")
    assert len(allv["sevenllm-mcq"]) == 2 and allv["sevenllm-mcq"][1]["meta"]["lang"] == "zh"


def test_cse_frr():
    m = load("cse-frr")
    items = m.convert(json.loads(fx("cse-frr", "mitre_frr.json").read_text()))
    c.validate(items)
    assert len(items) == 1  # malicious row dropped
    assert items[0]["scorer"] == "llm_judge" and items[0]["meta"]["expected_behavior"] == "comply"


def test_nvd_recent():
    m = load("nvd-recent")
    doc = json.loads(fx("nvd-recent", "cves-page.json").read_text())
    out = m.convert(doc["vulnerabilities"])
    cwe, cvss = out["nvd-cwe"], out["nvd-cvss"]
    c.validate(cwe + cvss)
    # sorted by CVE id; rejected, leaky (CWE named in text), ambiguous and noinfo rows dropped
    assert [(i["meta"]["cve"], i["answer"], i["meta"]["label_source"]) for i in cwe] == [
        ("CVE-2099-0001", "CWE-79", "cna"), ("CVE-2099-0003", "CWE-89", "nvd")]
    assert [(i["meta"]["cve"], i["meta"]["label_source"]) for i in cvss] == [
        ("CVE-2099-0001", "cna"), ("CVE-2099-0003", "nvd"), ("CVE-2099-0004", "cna"),
        ("CVE-2099-0006", "cna")]
    assert cvss[1]["answer"] == "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"  # NVD wins over CNA


def test_stratified_sample_is_deterministic_and_proportional():
    items = [{"id": f"x-{i}", "meta": {"category": "a" if i < 80 else "b"}} for i in range(100)]
    s1 = c.stratified_sample(items, 10, seed=7)
    s2 = c.stratified_sample(items, 10, seed=7)
    assert s1 == s2 and len(s1) == 10
    assert sum(1 for i in s1 if i["meta"]["category"] == "b") == 2
    assert [i["id"] for i in s1] == sorted((i["id"] for i in s1), key=lambda s: int(s[2:]))
    assert c.stratified_sample(items, 500) == items


def test_validate_rejects_bad_items():
    good = {"id": "s-1", "suite": "s", "type": "mcq", "prompt": "q", "choices": ["A) x", "B) y"],
            "answer": "B", "scorer": "mcq_letter", "meta": {"source": "t", "license": "t"}}
    c.validate([good])
    with pytest.raises(ValueError):
        c.validate([{**good, "scorer": "nope"}])
    with pytest.raises(ValueError):
        c.validate([{**good, "answer": "C"}])
    with pytest.raises(ValueError):
        c.validate([good, good])


def test_end_to_end_offline(tmp_path):
    """main() with a cached fixture: checksum check, JSONL, sample and provenance."""
    m = load("cse-frr")
    src = fx("cse-frr", "mitre_frr.json")
    cache = tmp_path / "cache" / "cse-frr"
    cache.mkdir(parents=True)
    shutil.copy(src, cache / "mitre_frr.json")
    m.SHA256 = c.sha256_file(src)
    out = tmp_path / "out"
    assert m.main(["--offline", "--cache-dir", str(tmp_path / "cache"), "--out-dir", str(out),
                   "--sample", "1"]) == 0
    lines = (out / "cse-frr.jsonl").read_text().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["id"] == "cse-frr-0001"
    prov = json.loads((out / "cse-frr.provenance.json").read_text())
    assert prov["items"] == 1 and prov["sources"][0]["sha256"] == m.SHA256
    assert prov["jsonl_sha256"] == c.sha256_file(out / "cse-frr.jsonl")
    assert (out / "cse-frr.sample1.jsonl").exists()
    # a wrong pin must fail, not silently convert
    m.SHA256 = "0" * 64
    with pytest.raises(FileNotFoundError):
        m.main(["--offline", "--cache-dir", str(tmp_path / "cache"), "--out-dir", str(out)])
