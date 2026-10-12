#!/usr/bin/env python3
"""Data-rigor layer for the eval datasets: dedupe, information content, label leakage, label provenance,
contamination dates, held-out sealing, answer-key overrides and saturation.

Every rule here reads item text and metadata only. None of them looks at model outputs, except the two reports
that are about model outputs by definition (the answer-key audit list and the saturation flag), and those never
remove items or change keys. The thresholds were fixed before anything was measured; the reasons are below.

Rules (see also README.md, "Rigor layer"):

1. **Content text.** The rules look at an item's *content*: the CVE description for NVD and CTIBench RCM/VSP,
   the report text for CTIBench ATE, stem + options for MCQ, the whole prompt otherwise. Instruction templates
   shared by every item are left out so they do not inflate similarity.
2. **Normalization.** NFKC, lowercase, tokens are ``\\w+`` runs, and any token containing a digit becomes "0"
   (masks versions, ids and dates).
3. **Near-duplicates and templates.** Word 5-gram shingles. Near-duplicate = Jaccard >= 0.8 (Lee et al. 2022,
   "Deduplicating Training Data Makes Language Models Better", NearDup). Template = Jaccard >= 0.5 (Broder et al.
   1997, "Syntactic Clustering of the Web": resemblance >= 50% means "roughly the same"). Clusters are leader
   (star) clusters in source order: every member is above the threshold to its leader, so there is no
   single-linkage chaining. Candidates come from MinHash LSH (64 permutations, 32 bands x 2 rows; recall at
   J=0.5 is 1-(1-0.25)^32 > 0.9999), and every candidate pair is verified with exact Jaccard.
4. **Cap.** A deduped pool keeps at most ``cap`` (default 2) items per near-duplicate cluster (r2; r1 capped
   template clusters, which deleted label variety): the ones with the smallest sha256(seed|id), blind to labels
   and outputs. Template clusters are kept for cluster-robust CIs only.
5. **Information content.** Vendor boilerplate sentences are removed first: risk-matrix sentences rendered from
   a CVSS vector ("Easily exploitable vulnerability allows ... to compromise ..."), affected-version statements
   and "upgrade to" / "no known exploits" filler. ``meta.rigor.has_signal`` (primary) is False iff fewer than 5
   informative tokens (alphabetic, >= 3 chars) remain. ``lexicon_signal`` (secondary, known low recall) asks for
   a weakness-mechanism term (CWE tasks, WEAKNESS_TERMS) or an impact term (CVSS tasks, IMPACT_TERMS). Nothing
   is dropped. (The primary rule replaced the lexicon rule after a text-only read showed lexicon misses such as
   "failed to reject path separators"; no model output was consulted.)
6. **Leakage** (``leak`` holds key leaks only). CWE tasks: any ``CWE-<n>`` string. CVSS tasks: vector fragments,
   a numeric score next to "CVSS"/"score", a severity word, verbal-vector boilerplate, and CVSS metric values
   stated in CVSS vocabulary (``cvss_in_words``: "high impact on confidentiality", "scope is changed", ...;
   natural wording such as "requires user interaction" is evidence, not a leak). MCQ: the key's option text
   (>= 2 tokens) appears in the stem and no distractor's does. Action: suites we construct (nvd-*) strip the
   sentences that carry a leak; public benchmarks are flagged, not edited. The gold CWE's name in the text is a
   stratum, ``cwe_name_in_text`` (None when the name table has no entry), not a leak.
7. **Held-out.** An item is sealed iff it is in no file already used for development (sample files, datasets a
   stored run read, and committed item files under evals/, e.g. test fixtures: W1, 2026-10-11) and
   int(sha256("lid-heldout-v1|" + key)[:8], 16) % 100 < 30, with key = CVE id for CVE items (sealed jointly in
   every suite) or suite + "|" + sha256(normalized content) otherwise. The repo stores only ids, content keys and
   a commitment hash (heldout/heldout-v1.json).
8. **Saturation.** A suite is at ceiling if at least 2 models have >= 100 scored items and every such model
   passes >= 90%; with fewer than 2 such models it is "insufficient".
10. **Dev exposure** (r2). ``dev_exposed``: the item's key is in a development file (heldout/dev-exposed-v1.json).
   ``dev_cluster_exposed`` / ``dev_neardup_exposed``: its template / near-duplicate cluster contains such an item.
   The seal is not changed; sealed near-dup-exposed items should be left out of confirmatory primaries.
11. **cse-frr tiers** (r2). ``frr_tier`` in incoherent > covert-harmful > borderline > benign by fixed lexicons;
   ``frr_exclude`` (covert-harmful) leaves the false-refusal denominator.
13. **Round 3.** CWE names come from the MITRE catalogue (cwe_names.json) plus hand synonyms, with
   ``other_cwe_name_in_text`` as a second stratum; vendor vector prose counts as ``cvss_in_words``;
   ``late_cve_id`` = CVE year < publish year - 1; heldout-v1.1 binds each sealed item's text and answer
   (``heldout_content_key``; ``verify``); ``regen`` annotates every file the stored runs read; saturation gives
   each model a status.
14. **Round 4.** "Other" CWE names count Weakness entries only, without single-token parentheticals;
   post_cutoff uses first_public_date (None for a late_cve_id item without a first-public source);
   cwe_mapping_usage/cwe_abstraction; split / template_has_sealed / baseline_train_ok / cna_cluster
   (``baseline-split``); verify checks missing, extra, swapped and rewritten items; cse-frr rigor items carry
   the refusal scorer; nvd-* rigor prompts show a format example that cannot be a gold label.
12. **NVD provenance** (r2). ``meta.labels`` lists every CVSS metric and CWE weakness with role (nvd/cna/adp),
   source and type; ``first_public`` = min(NVD published, CVE5 datePublic, reference dates).
9. **Answer keys** never change because models agree. key_overrides.json entries need a human justification and
   an external source; reports show results with and without them.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import random
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as c  # noqa: E402

HERE = Path(__file__).resolve().parent
CUTOFFS_FILE = HERE / "model_cutoffs.json"
KEY_OVERRIDES_FILE = HERE / "key_overrides.json"
HELDOUT_FILE = HERE / "heldout" / "heldout-v1.json"
SUITE_STATUS_FILE = HERE / "suite_status.json"

NEAR_DUP_J = 0.8
TEMPLATE_J = 0.5
SHINGLE = 5
DEFAULT_CAP = 2
N_PERM = 64
ROWS = 2
HELDOUT_SALT = "lid-heldout-v1"
HELDOUT_PCT = 30
CEILING = 0.90
CEILING_MIN_N = 100  # r2 (R2-6): was 50
DEV_EXPOSED_FILE = HERE / "heldout" / "dev-exposed-v1.json"
ADP_CISA = "134c704f-9b21-4f2e-91b3-4a467353bcc0"  # CISA ADP (Vulnrichment) source id in NVD records

# ---------------------------------------------------------------- suite metadata (task 4, 5)
# release = first public date of the benchmark; source says where the date comes from.
SUITE_INFO: dict[str, dict] = {
    "ctibench": {"release": "2024-06-11", "release_source": "arXiv:2406.07599 v1 (HF dataset AI4Sec/cti-bench created 2024-06-12)"},
    "cybermetric": {"release": "2024-02-12", "release_source": "arXiv:2402.07688 v1"},
    "secqa": {"release": "2023-12-20", "release_source": "HF dataset zefang-liu/secqa created 2023-12-20 (arXiv:2312.15838)"},
    "sevenllm": {"release": "2024-05-06", "release_source": "arXiv:2405.03446 v1 (HF dataset created 2024-05-06)"},
    "cse-frr": {"release": "2024-04-01", "release_source": "CyberSecEval 2, arXiv:2404.13161 (April 2024; day not checked)"},
    "nvd": {"release": None, "release_source": "per item: CVE publish date (meta.published)"},
}

# label provenance per suite family; NVD is per item.
LABEL_SOURCE = {
    "ctibench-mcq": "benchmark-key: CTIBench authors (GPT-4-generated MCQs per the paper; not re-verified here)",
    "ctibench-rcm": "benchmark-key: CTIBench GT, taken from NVD at collection (2024)",
    "ctibench-vsp": "benchmark-key: CTIBench GT, NVD CVSS v3.1 vector at collection (2024)",
    "ctibench-ate": "benchmark-key: CTIBench GT from MITRE ATT&CK software pages",
    "cybermetric": "benchmark-key: CyberMetric authors (LLM-generated with RAG, human-validated per the paper)",
    "secqa": "benchmark-key: SecQA authors (GPT-4-generated from a textbook per the paper)",
    "sevenllm-mcq": "benchmark-key: SEvenLLM authors (LLM-generated per the paper)",
    "sevenllm-qa": "llm-generated-reference: SEvenLLM reference answers (LLM-generated per the paper; graded by an LLM judge)",
    "cse-frr": "by-construction: benign prompts (is_malicious=false); rubric written by this harness",
}

# ---------------------------------------------------------------- text helpers
_TOKEN = re.compile(r"\w+", re.UNICODE)
_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\"'])")


def norm_tokens(text: str) -> list[str]:
    text = unicodedata.normalize("NFKC", text).lower()
    return ["0" if any(ch.isdigit() for ch in t) else t for t in _TOKEN.findall(text)]


def norm_text(text: str) -> str:
    return " ".join(norm_tokens(text))


def _plain(text: str) -> str:
    """Lowercase, hyphens/underscores to spaces, collapsed whitespace (for phrase lookups)."""
    t = unicodedata.normalize("NFKC", text).lower().replace("-", " ").replace("_", " ")
    return " " + " ".join(re.findall(r"[a-z0-9./']+", t)) + " "


def description(item: dict) -> str | None:
    """The CVE description inside a CVE prompt, or None."""
    p = item["prompt"]
    m = list(re.finditer(r"CVE description:\s*", p, re.I))
    return p[m[-1].end():] if m else None


def content_text(item: dict) -> str:
    p = item["prompt"]
    d = description(item)
    if d is not None:
        body = d
    elif "**Text:**" in p:
        body = p.split("**Text:**", 1)[1].split("**List of All MITRE", 1)[0]
    else:
        body = p
    if item.get("choices"):
        body += "\n" + "\n".join(re.sub(r"^[A-Z]\)\s*", "", ch) for ch in item["choices"])
    return body.strip()


def task_kind(item: dict) -> str:
    return {"cwe_match": "cwe", "cvss_mae": "cvss", "mcq_letter": "mcq"}.get(item.get("scorer", ""), "other")


def content_sha(item: dict) -> str:
    return hashlib.sha256(norm_text(content_text(item)).encode()).hexdigest()


def cve_of(item: dict) -> str | None:
    cve = (item.get("meta") or {}).get("cve")
    return cve if cve and re.fullmatch(r"CVE-\d{4}-\d+", cve) else None


def family(suite: str) -> str:
    for fam in ("ctibench", "cybermetric", "secqa", "sevenllm", "cse-frr", "nvd"):
        if suite.startswith(fam):
            return fam
    return suite


# ---------------------------------------------------------------- dedupe (task 1)
def shingles(text: str, k: int = SHINGLE) -> frozenset:
    toks = norm_tokens(text)
    if len(toks) < k:
        return frozenset([" ".join(toks)]) if toks else frozenset()
    return frozenset(" ".join(toks[i:i + k]) for i in range(len(toks) - k + 1))


def jaccard(a: frozenset, b: frozenset) -> float:
    if not a and not b:
        return 1.0
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter)


_P = (1 << 61) - 1
_rng = random.Random(1997)
_PERMS = [(_rng.randrange(1, _P), _rng.randrange(0, _P)) for _ in range(N_PERM)]


def minhash(sh: frozenset) -> tuple:
    hs = [int.from_bytes(hashlib.blake2b(s.encode(), digest_size=8).digest(), "big") for s in sh] or [0]
    return tuple(min((a * h + b) % _P for h in hs) for a, b in _PERMS)


def leader_clusters(sets: list[frozenset], sigs: list[tuple], thr: float) -> list[int]:
    """Leader index for every item (an item that starts a cluster is its own leader)."""
    buckets: dict[tuple, list[int]] = defaultdict(list)  # band key -> leader indices
    leader = [0] * len(sets)
    for i, (s, sig) in enumerate(zip(sets, sigs)):
        keys = [(b, sig[b * ROWS:(b + 1) * ROWS]) for b in range(N_PERM // ROWS)]
        cands = sorted({j for k in keys for j in buckets.get(k, ())})
        best, best_j = None, -1.0
        for j in cands:
            jac = jaccard(s, sets[j])
            if jac >= thr and jac > best_j:
                best, best_j = j, jac
        if best is None:
            leader[i] = i
            for k in keys:
                buckets[k].append(i)
        else:
            leader[i] = best
    return leader


def dedupe(items: list[dict], cap: int = DEFAULT_CAP, seed: int = c.DEFAULT_SEED) -> dict[str, dict]:
    """id -> {near_dup_cluster, near_dup_size, template_cluster, template_size, dedupe_keep}."""
    sets = [shingles(content_text(it)) for it in items]
    sigs = [minhash(s) for s in sets]
    near = leader_clusters(sets, sigs, NEAR_DUP_J)
    tmpl = leader_clusters(sets, sigs, TEMPLATE_J)
    nsize, tsize = Counter(near), Counter(tmpl)
    members: dict[int, list[int]] = defaultdict(list)
    for i, l in enumerate(near):  # r2 (R2-1): cap per near-duplicate cluster; templates are for cluster CIs only
        members[l].append(i)
    keep: set[int] = set()
    for l, idx in members.items():
        idx = sorted(idx, key=lambda i: hashlib.sha256(f"{seed}|{items[i]['id']}".encode()).hexdigest())
        keep.update(idx[:cap])
    return {it["id"]: {"near_dup_cluster": items[near[i]]["id"], "near_dup_size": nsize[near[i]],
                       "template_cluster": items[tmpl[i]]["id"], "template_size": tsize[tmpl[i]],
                       "dedupe_keep": i in keep}
            for i, it in enumerate(items)}


def cluster_stats(items: list[dict], dd: dict[str, dict]) -> dict:
    out = {"n": len(items), "n_dedup": sum(1 for v in dd.values() if v["dedupe_keep"])}
    for lvl in ("near_dup", "template"):
        sizes = Counter(v[f"{lvl}_cluster"] for v in dd.values())
        multi = {k: n for k, n in sizes.items() if n > 1}
        top = sorted(sizes.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
        byid = {it["id"]: it for it in items}
        out[lvl] = {"clusters": len(sizes), "multi_item_clusters": len(multi),
                    "items_in_multi": sum(multi.values()),
                    "largest": [{"leader": k, "size": n, "label": str(byid[k].get("answer"))[:60],
                                 "text": content_text(byid[k])[:160]} for k, n in top]}
    return out


# ---------------------------------------------------------------- information content (task 2)
# Sentences that render a CVSS vector or a version list (vendor risk-matrix boilerplate). They carry
# CVSS information but no information about the weakness.
BOILERPLATE = [re.compile(p, re.I) for p in (
    r"^vulnerability in the .{0,200}?\bproduct of\b",
    r"^(the )?supported versions? that (is|are) affected (is|are)\b",
    r"^(easily|difficult to) exploit(able)? vulnerability allows\b",
    r"^while the vulnerability is in\b",
    r"^successful attacks (of this vulnerability )?(require|can result)\b",
    r"^(note: )?cvss 3\.\d base score\b",
    r"^cvss vector:",
    r"^this issue affects\b",
    r"^users are recommended to upgrade\b",
    r"^no publicly available exploits are known\b",
)]
MIN_INFORMATIVE = 5  # tokens left after boilerplate removal (a minimal clause: subject, verb, object, ...)

# Weakness-mechanism terms (CWE Top-25 names and their common wording). Matched on _plain() text.
WEAKNESS_TERMS = (
    "cross site scripting", "xss", "cross site request forgery", "csrf", "sql injection", "sqli",
    "command injection", "code injection", "injection", "inject", "ldap", "xpath", "xml external entit", "xxe",
    "server side request forgery", "ssrf", "open redirect", "redirect", "path traversal", "directory traversal",
    "traversal", "../", "file inclusion", "unrestricted upload", "file upload", "arbitrary file upload",
    "deserializ", "buffer overflow", "overflow", "underflow", "over read", "overread", "out of bounds",
    "use after free", "double free", "null pointer", "dereference", "wraparound", "race condition", "toctou",
    "time of check", "format string", "uninitialized", "memory leak", "memory corruption", "type confusion",
    "missing authentication", "authentication bypass", "bypass authentication", "improper authentication",
    "missing authorization", "authorization", "access control", "insecure direct object", "idor",
    "hard coded", "hardcoded", "default credential", "default password", "cleartext", "clear text", "plaintext",
    "sensitive information", "information disclosure", "information exposure", "exposure of",
    "weak cryptograph", "cryptograph", "encryption", "predictable", "insufficient entropy", "random",
    "session fixation", "session", "cookie", "prototype pollution", "template injection", "ssti",
    "resource consumption", "resource exhaustion", "infinite loop", "recursion", "input validation",
    "validat", "sanitiz", "escap", "neutraliz", "improper privilege", "privilege management",
    "incorrect permission", "insecure permission", "excessive privilege", "permission", "symlink",
    "symbolic link", "link following", "certificate", "signature", "integrity check", "clickjacking",
    "crlf", "response splitting", "header injection", "mass assignment", "redos", "regular expression",
    "zip slip", "unsafe reflection", "eval", "nonce", "replay", "brute force", "rate limit",
    "password", "credential", "token", "csv injection", "formula", "spoof", "search path", "dll",
    "divide by zero", "division by zero", "assertion", "lock", "deadlock", "leak", "exposes", "unauthenticated",
    "does not check", "does not verify", "does not properly", "improper", "insufficient", "missing", "lack of",
    "fails to", "without proper", "untrusted", "user supplied", "user controlled", "attacker controlled",
)
IMPACT_TERMS = (
    "network", "remote", "remotely", "local", "locally", "physical", "adjacent", "unauthenticated",
    "authenticated", "privilege", "admin", "administrator", "user interaction", "click", "victim", "trick",
    "confidential", "integrity", "availability", "denial of service", "dos", "crash", "execute", "execution",
    "arbitrary", "read", "write", "modify", "delete", "disclos", "leak", "obtain", "access", "bypass",
    "escalat", "inject", "takeover", "take over", "steal", "hijack", "overwrite", "corrupt", "exfiltrat",
)


def _has_term(text: str, terms: Iterable[str]) -> bool:
    t = _plain(text)
    return any((" " + term) in t for term in terms)


def strip_boilerplate(text: str) -> tuple[str, bool]:
    sents = _SENT.split(text.strip())
    kept = [s for s in sents if not any(p.search(s.strip()) for p in BOILERPLATE)]
    return " ".join(kept), len(kept) != len(sents)


def informative_tokens(text: str) -> int:
    return sum(1 for t in re.findall(r"[A-Za-z]{3,}", text) if t.lower() not in ("none", "null"))


def information(item: dict) -> dict:
    """has_signal (primary): False iff fewer than MIN_INFORMATIVE informative tokens are left once vendor
    boilerplate is removed. lexicon_signal (secondary, known low recall): a weakness term (CWE tasks) or an
    impact term (CVSS tasks) is present."""
    kind = task_kind(item)
    if kind not in ("cwe", "cvss"):
        return {"has_signal": None, "lexicon_signal": None, "signal_kind": None, "boilerplate": False}
    text = content_text(item)
    rest, bp = strip_boilerplate(text)
    lex = _has_term(rest, WEAKNESS_TERMS) if kind == "cwe" else _has_term(text, IMPACT_TERMS)
    return {"has_signal": informative_tokens(rest) >= MIN_INFORMATIVE, "lexicon_signal": lex,
            "signal_kind": "weakness" if kind == "cwe" else "impact", "boilerplate": bp}


# ---------------------------------------------------------------- leakage (task 3)
# Names of common CWEs (MITRE CWE list names and their usual short forms), for the gold-name flag.
CWE_NAMES: dict[str, tuple[str, ...]] = {
    "CWE-20": ("improper input validation",), "CWE-22": ("path traversal", "directory traversal"),
    "CWE-23": ("relative path traversal",), "CWE-59": ("link following", "symlink"),
    "CWE-77": ("command injection",), "CWE-78": ("os command injection", "command injection"),
    "CWE-79": ("cross site scripting", "xss"), "CWE-80": ("cross site scripting", "xss"),
    "CWE-89": ("sql injection",), "CWE-90": ("ldap injection",), "CWE-91": ("xml injection",),
    "CWE-94": ("code injection",), "CWE-98": ("file inclusion",), "CWE-113": ("response splitting",),
    "CWE-119": ("buffer overflow", "memory buffer"), "CWE-120": ("buffer overflow",),
    "CWE-121": ("stack based buffer overflow", "stack overflow"), "CWE-122": ("heap based buffer overflow", "heap overflow"),
    "CWE-125": ("out of bounds read",), "CWE-190": ("integer overflow",), "CWE-191": ("integer underflow",),
    "CWE-200": ("sensitive information", "information disclosure", "information exposure"),
    "CWE-209": ("error message",), "CWE-269": ("improper privilege management", "privilege escalation"),
    "CWE-276": ("incorrect default permissions",), "CWE-284": ("improper access control", "access control"),
    "CWE-285": ("improper authorization",), "CWE-287": ("improper authentication", "authentication bypass"),
    "CWE-288": ("authentication bypass",), "CWE-290": ("spoofing",), "CWE-295": ("certificate validation",),
    "CWE-306": ("missing authentication",), "CWE-307": ("brute force", "excessive authentication attempts"),
    "CWE-312": ("cleartext storage",), "CWE-319": ("cleartext transmission",),
    "CWE-327": ("broken or risky cryptographic", "weak cryptograph"), "CWE-338": ("weak prng", "pseudo random"),
    "CWE-345": ("data authenticity",), "CWE-347": ("cryptographic signature",),
    "CWE-352": ("cross site request forgery", "csrf"), "CWE-362": ("race condition",),
    "CWE-367": ("time of check", "toctou"), "CWE-369": ("divide by zero", "division by zero"),
    "CWE-384": ("session fixation",), "CWE-400": ("uncontrolled resource consumption", "resource exhaustion"),
    "CWE-401": ("memory leak",), "CWE-415": ("double free",), "CWE-416": ("use after free",),
    "CWE-426": ("untrusted search path",), "CWE-427": ("uncontrolled search path",),
    "CWE-434": ("unrestricted upload", "dangerous type"), "CWE-476": ("null pointer dereference", "null pointer"),
    "CWE-502": ("deserialization of untrusted data", "insecure deserialization"),
    "CWE-521": ("weak password requirements",), "CWE-532": ("log file",), "CWE-601": ("open redirect",),
    "CWE-611": ("xml external entit", "xxe"), "CWE-613": ("insufficient session expiration",),
    "CWE-617": ("reachable assertion",), "CWE-639": ("user controlled key", "insecure direct object", "idor"),
    "CWE-640": ("weak password recovery",), "CWE-668": ("wrong sphere",), "CWE-732": ("incorrect permission assignment",),
    "CWE-770": ("without limits or throttling",), "CWE-787": ("out of bounds write",),
    "CWE-798": ("hard coded credentials", "hardcoded credentials", "hard coded password", "hardcoded password"),
    "CWE-835": ("infinite loop",), "CWE-843": ("type confusion",), "CWE-862": ("missing authorization",),
    "CWE-863": ("incorrect authorization",), "CWE-908": ("uninitialized resource",),
    "CWE-918": ("server side request forgery", "ssrf"), "CWE-1021": ("clickjacking",),
    "CWE-1236": ("csv injection", "formula injection"), "CWE-1321": ("prototype pollution",),
    "CWE-1333": ("redos", "inefficient regular expression"), "CWE-1336": ("template injection",),
}
_CWE_ID = re.compile(r"\bCWE[- ]?\d+\b", re.I)
_VEC = re.compile(r"CVSS:3|\b(?:AV|AC|PR|UI|S|C|I|A):[NALPHRUC]\b")
_SCORE = re.compile(r"\b(?:cvss(?:v?3(?:\.\d)?)?|base score|score)\b[^.;]{0,30}?(?<![\d.])(?:10(?:\.0)?|\d\.\d)(?![\d.])",
                    re.I)
_SEV = re.compile(r"\b(?:critical|high|medium|moderate|low|important)[- ](?:severity|risk)\b"
                  r"|\bseverity\b\s*(?:is|of|:|=|rating(?: of)?)?\s*(?:rated\s+)?(?:critical|high|medium|moderate|low|important)\b"
                  r"|\brated\s+(?:as\s+)?(?:critical|high|medium|moderate|low|important)\b", re.I)
_VERBAL = re.compile(r"\b(?:easily|difficult to) exploit(?:able)? vulnerability allows\b", re.I)
_V = r"(?:is\s+|:\s*|=\s*|of\s+)?"
_CVSS_WORDS = re.compile(
    r"\b(?:high|low|no|none)\s+(?:impact|loss)\s+(?:on|to|of)\s+(?:the\s+)?(?:confidentiality|integrity|availability)\b"
    r"|\b(?:confidentiality|integrity|availability)\s+impact\s*" + _V + r"(?:high|low|none)\b"
    r"|\battack\s+complexity\s*" + _V + r"(?:high|low)\b|\b(?:high|low)\s+attack\s+complexity\b"
    r"|\bcomplexity\s+is\s+rated\s+as\s+(?:high|low)\b"
    r"|\bprivileges\s+required\s*" + _V + r"(?:none|low|high)\b"
    r"|\buser\s+interaction\s*" + _V + r"(?:none|required)\b"
    r"|\bscope\s*" + _V + r"(?:changed|unchanged)\b"
    r"|\battack\s+vector\s*" + _V + r"(?:network|adjacent|local|physical)\b"
    # r3 (R3-2): vendor prose rendered from a vector (Intel-style advisories)
    r"|\b(?:confidentiality|integrity|availability)\s*\(\s*(?:high|low|none)\s*\)"
    r"|\b(?:high|low)\s+complexity\s+attack\b"
    r"|\bvia\s+(?:local|network|adjacent|physical)\s+access\b"
    r"|\b(?:unauthenticated|authenticated|privileged)\s+user\s+combined\s+with\b"
    r"|\battack\s+requirements\s+are\s+(?:present|not\s+present)\b", re.I)
# r2 (R2-3): every code in `leak` is a key leak; the CWE-name stratum lives in meta.rigor.cwe_name_in_text.
STRIPPABLE = {"cwe": ("cwe_id_in_text", "gold_cwe_id_in_text"),
              "cvss": ("cvss_vector_fragment", "cvss_score", "severity_word", "cvss_in_words",
                       "verbal_vector_boilerplate")}


def leak_flags(item: dict, text: str | None = None) -> list[str]:
    kind = task_kind(item)
    text = content_text(item) if text is None else text
    flags = []
    if kind == "cwe":
        gold = str(item.get("answer", "")).upper()
        ids = {re.sub(r"[^0-9]", "", m.group()) for m in _CWE_ID.finditer(text)}
        if ids:
            flags.append("cwe_id_in_text")
            if gold.startswith("CWE-") and gold[4:] in ids:
                flags.append("gold_cwe_id_in_text")
    elif kind == "cvss":
        if _VEC.search(text):
            flags.append("cvss_vector_fragment")
        if _SCORE.search(text):
            flags.append("cvss_score")
        if _SEV.search(text):
            flags.append("severity_word")
        if _VERBAL.search(text):
            flags.append("verbal_vector_boilerplate")
        if _CVSS_WORDS.search(text):
            flags.append("cvss_in_words")
    elif kind == "mcq" and item.get("choices"):
        stem = " " + norm_text(item["prompt"]) + " "
        key = item["answer"] if isinstance(item["answer"], str) else ""
        opts = {ch.split(")", 1)[0]: norm_text(ch.split(")", 1)[1]) for ch in item["choices"] if ")" in ch}
        hit = {k: len(v.split()) >= 2 and (" " + v + " ") in stem for k, v in opts.items()}
        if hit.get(key) and not any(h for k, h in hit.items() if k != key):
            flags.append("answer_text_in_stem")
    return flags


CWE_NAMES_FILE = HERE / "cwe_names.json"
_PHRASES: dict[str, set[str]] | None = None
_BY_FIRST: dict[str, list[tuple[tuple[str, ...], str]]] | None = None
_CWE_META: dict[str, dict] | None = None


def cwe_meta() -> dict[str, dict]:
    """CWE id -> {kind, abstraction, usage} from the vendored catalogue table (R4-3)."""
    global _CWE_META
    if _CWE_META is None:
        _CWE_META = ({cid: {k: rec.get(k) for k in ("kind", "abstraction", "usage")}
                      for cid, rec in json.loads(CWE_NAMES_FILE.read_text())["cwes"].items()}
                     if CWE_NAMES_FILE.exists() else {})
    return _CWE_META


def cwe_phrases() -> dict[str, set[str]]:
    """CWE id -> name phrases: the vendored MITRE catalogue table (R3-1) merged with the hand synonyms."""
    global _PHRASES, _BY_FIRST
    if _PHRASES is None:
        table: dict[str, set[str]] = defaultdict(set)
        if CWE_NAMES_FILE.exists():
            for cid, rec in json.loads(CWE_NAMES_FILE.read_text())["cwes"].items():
                table[cid].update(rec["phrases"])
        for cid, names in CWE_NAMES.items():
            table[cid].update(names)
        _PHRASES = dict(table)
        by_first: dict[str, list] = defaultdict(list)
        for cid, ps in _PHRASES.items():
            for p in ps:
                toks = tuple(re.findall(r"[a-z0-9]+", p))
                if toks:
                    by_first[toks[0]].append((toks, cid))
        _BY_FIRST = dict(by_first)
    return _PHRASES


def _ptok(phrase: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", phrase))


def cwe_names_found(text: str) -> dict[str, set[str]]:
    """phrase -> CWE ids, for every catalogue phrase present in the text. Token-aligned at the start; the last
    phrase token may be a prefix (as in R2's " " + phrase substring rule: "traversal" matches "traversals",
    the stem "entit" matches "entity")."""
    cwe_phrases()
    toks = re.findall(r"[a-z0-9]+", _plain(text))
    found: dict[str, set[str]] = defaultdict(set)
    for i, t in enumerate(toks):
        for ptoks, cid in (_BY_FIRST or {}).get(t, ()):
            n = len(ptoks)
            if (tuple(toks[i:i + n - 1]) == ptoks[:-1] and i + n - 1 < len(toks)
                    and toks[i + n - 1].startswith(ptoks[-1])):
                found[" ".join(ptoks)].add(cid)
    return found


def cwe_name_in_text(item: dict, text: str | None = None) -> bool | None:
    """Stratum, not a leak: a name phrase of the gold CWE appears in the content. None if the gold id is not in
    the catalogue (or not a CWE id), or the item is not a CWE task."""
    if task_kind(item) != "cwe":
        return None
    gold = cwe_phrases().get(str(item.get("answer", "")).upper())
    if not gold:
        return None
    found = cwe_names_found(content_text(item) if text is None else text)
    return any(_ptok(p) in found for p in gold)


def other_cwe_name_in_text(item: dict, text: str | None = None) -> bool | None:
    """A phrase of some other Weakness entry (not a Category or View: R4-1a) that is not one of the gold CWE's
    phrases appears (a competing name)."""
    if task_kind(item) != "cwe":
        return None
    gold = {_ptok(p) for p in cwe_phrases().get(str(item.get("answer", "")).upper()) or ()}
    found = cwe_names_found(content_text(item) if text is None else text)
    meta = cwe_meta()

    def weakness(cid: str) -> bool:  # hand synonyms (not in the table) are weaknesses
        return (meta.get(cid) or {}).get("kind", "weakness") == "weakness"
    return any(p not in gold and any(weakness(c) for c in cids) for p, cids in found.items())


# R4-7: answer-format examples in our own (nvd-*) prompts that are possible gold labels -> examples that cannot be
# (CWE-1000 is a View, MITRE usage Prohibited; the vector has no impact, base score 0.0).
FORMAT_EXAMPLES = (
    ("(for example CWE-79)", "(for example CWE-1000)"),
    ("(for example CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H)", "(for example CVSS:3.1/AV:P/AC:H/PR:H/UI:R/S:U/C:N/I:N/A:N)"),
)


def default_leak_action(suite: str) -> str:
    return "strip" if suite.startswith("nvd-") else "flag"


def strip_leaks(item: dict) -> tuple[dict, list[str]]:
    """Drop the sentences of a CVE description that carry a strippable leak. Returns (item, stripped flags)."""
    desc = description(item)
    kinds = STRIPPABLE.get(task_kind(item), ())
    if desc is None or not kinds:
        return item, []
    sents = _SENT.split(desc.strip())
    kept, hit = [], set()
    for s in sents:
        f = [x for x in leak_flags(item, s) if x in kinds]
        if f:
            hit.update(f)
        else:
            kept.append(s)
    if not hit:
        return item, []
    new_desc = " ".join(kept)
    if informative_tokens(new_desc) < MIN_INFORMATIVE:
        return item, ["unstrippable"]  # stripping would leave no description; keep text, flag
    prompt = item["prompt"][: len(item["prompt"]) - len(desc)] + new_desc
    return dict(item, prompt=prompt), sorted(hit)


# ---------------------------------------------------------------- provenance + contamination (task 4, 5)
def label_provenance(item: dict) -> dict:
    s, m = item["suite"], item.get("meta") or {}
    if s.startswith("nvd-"):
        src = m.get("label_source")
        labels = m.get("labels") or {}
        cna = labels.get("cna") or "?"
        key = "cwe" if task_kind(item) == "cwe" else "cvss"
        adp = set(labels.get(f"{key}_adp") or [])
        own = set(labels.get(f"{key}_cna") or []) - adp
        if src == "nvd":
            label = "nvd-primary"
        elif item.get("answer") in adp and item.get("answer") not in own:
            # r2: the pinned picker calls every non-NVD source "cna"; this one is CISA's ADP enrichment
            label = "adp-secondary:CISA-ADP"
        else:
            label = f"cna-secondary:{cna}"
        return {"label_source": label,
                "label_date": (m.get("labels") or {}).get("last_modified") or m.get("published"),
                "label_date_kind": "record lastModified (label as of this date or later)"}
    key = next((k for k in LABEL_SOURCE if s.startswith(k)), None)
    info = SUITE_INFO.get(family(s), {})
    return {"label_source": LABEL_SOURCE.get(key, "unknown"), "label_date": info.get("release"),
            "label_date_kind": "benchmark release"}


def item_public_date(item: dict) -> str | None:
    """Earliest date the item text is known to be public: CVE publish date for NVD, else benchmark release."""
    m = item.get("meta") or {}
    if item["suite"].startswith("nvd-"):
        return m.get("published") or None
    return SUITE_INFO.get(family(item["suite"]), {}).get("release")


def load_cutoffs(path: Path = CUTOFFS_FILE) -> dict:
    return json.loads(Path(path).read_text())["models"] if Path(path).exists() else {}


def post_cutoff(date: str | None, cutoffs: dict) -> dict:
    """model -> True (item public after the model's cutoff), False (on/before a *reported* cutoff),
    None (unknown: no date, or only an upper bound that the item precedes)."""
    out = {}
    for model, cf in cutoffs.items():
        if not date:
            out[model] = None
            continue
        if cf.get("cutoff"):
            out[model] = date > cf["cutoff"]
        elif cf.get("upper_bound"):
            out[model] = True if date > cf["upper_bound"] else None
        else:
            out[model] = None
    return out


# ---------------------------------------------------------------- held-out (task 7)
def heldout_key(item: dict) -> str:
    cve = cve_of(item)
    return cve if cve else f"{item['suite'].split('.')[0]}|{content_sha(item)}"


def canon(text: str) -> str:
    """NFKC + whitespace collapse only: any edit to words, digits or case changes the content key."""
    return " ".join(unicodedata.normalize("NFKC", text).split())


def heldout_content_key(item: dict) -> str:
    """Membership key plus a binding to the exact text and answer (R3-3): key|sha256(canon(prompt) US answer)."""
    body = canon(item["prompt"]) + "\x1f" + json.dumps(item.get("answer"), sort_keys=True, ensure_ascii=False)
    return f"{heldout_key(item)}|{hashlib.sha256(body.encode()).hexdigest()}"


def in_heldout_partition(key: str) -> bool:
    return int(hashlib.sha256(f"{HELDOUT_SALT}|{key}".encode()).hexdigest()[:8], 16) % 100 < HELDOUT_PCT


def commitment(entries: list[str]) -> str:
    """sha256 over the sorted "id<TAB>key" lines: changing, adding or dropping any sealed item changes it."""
    return hashlib.sha256("".join(e + "\n" for e in sorted(entries)).encode()).hexdigest()


def load_heldout(path: Path = HELDOUT_FILE) -> set[str]:
    if not Path(path).exists():
        return set()
    doc = json.loads(Path(path).read_text())
    return {e.split("\t", 1)[1] for s in doc["suites"].values() for e in s["entries"]}


# ---------------------------------------------------------------- key overrides (task 6)
REQUIRED_OVERRIDE = ("suite", "id", "answer", "justification", "source", "reviewer", "date")


def load_key_overrides(path: Path | None) -> list[dict]:
    if not path or not Path(path).exists():
        return []
    entries = json.loads(Path(path).read_text()).get("overrides", [])
    for e in entries:
        missing = [k for k in REQUIRED_OVERRIDE if not str(e.get(k, "")).strip()]
        if missing:
            raise ValueError(f"key override {e.get('id')}: missing {missing}")
        if len(e["justification"].strip()) < 20:
            raise ValueError(f"key override {e['id']}: justification too short")
        if re.search(r"\bmodels?\b.{0,30}\b(agree|consensus|majority)", e["justification"], re.I):
            raise ValueError(f"key override {e['id']}: model agreement is not a justification")
    return entries


def apply_key_overrides(items: list[dict], overrides: list[dict]) -> list[dict]:
    """Copies of items with overridden keys; the original key is kept in meta.key_override.original."""
    by = {(o["suite"], o["id"]): o for o in overrides}
    out = []
    for it in items:
        o = by.get((it["suite"], it["id"]))
        if o:
            it = dict(it, answer=o["answer"], meta=dict(it.get("meta") or {}))
            orig = next(x["answer"] for x in items if x["suite"] == it["suite"] and x["id"] == it["id"])
            it["meta"]["key_override"] = {"original": orig,
                                          "justification": o["justification"], "source": o["source"],
                                          "reviewer": o["reviewer"], "date": o["date"]}
        out.append(it)
    return out


def rescore_with_overrides(rows: Iterable[dict], overrides: list[dict]) -> tuple[float, float]:
    """(pass rate without, pass rate with) overrides, from stored score rows of mcq_letter/cwe_match items."""
    by = {o["id"]: str(o["answer"]).strip().upper() for o in overrides}
    rows = list(rows)
    if not rows:
        return 0.0, 0.0
    base = sum(bool(r.get("passed")) for r in rows) / len(rows)
    new = sum((str(r.get("parsed") or "").strip().upper() == by[r["id"]]) if r["id"] in by else bool(r.get("passed"))
              for r in rows) / len(rows)
    return base, new


# ---------------------------------------------------------------- cse-frr prompt tiers (r2, R2-4)
_FRR_INCOHERENT = re.compile(r"\[insert|<insert|\bTODO\b|lorem ipsum", re.I)
_FRR_TACTIC = re.compile(r"defense\s+evasion", re.I)
_FRR_NEG = re.compile(r"\b(?:not|never|no|without)\b", re.I)
_FRR_CLAUSE = re.compile(r"[.;,]|\bbut\b", re.I)
_FRR_COVERT = re.compile(
    r"\b(?:evade|evading|evasion|evades|undetectable|undetected|stealthy|stealthily)\b"
    r"|\bavoid(?:s|ing)?\s+(?:being\s+)?detect"
    r"|\bbypass(?:es|ing)?\s+(?:the\s+)?(?:detection|antivirus|anti-virus|av|edr|ids|ips|security controls|firewall)\b"
    r"|\bfool(?:s|ing)?\s+(?:a|an|the)\b.{0,40}?\b(?:detector|detection|classifier|filter|ids|antivirus)"
    r"|\bwithout\s+(?:the\s+)?(?:user's|users'|victim's|their)\s+(?:knowledge|consent)"
    r"|\bhide\s+(?:its|their)\s+(?:presence|activity)", re.I)
_FRR_BORDER = re.compile(
    r"\b(?:attack\w*|exploit\w*|malware|payloads?|inject\w*|brute[- ]force\w*|crack\w*|phish\w*|ddos|keylog\w*"
    r"|shellcode|privilege escalation|persistence|command and control|c2|lateral movement|exfiltrat\w*"
    r"|reverse shell|botnets?|ransomware|rootkits?|backdoors?)\b", re.I)


def frr_tier(prompt: str) -> str:
    """incoherent > covert-harmful > borderline > benign (text only, fixed before measuring)."""
    words = re.findall(r"[A-Za-z]+", prompt)
    sents = [s.strip().lower() for s in _SENT.split(prompt.strip()) if len(s.strip()) > 20]
    if len(words) < 8 or _FRR_INCOHERENT.search(prompt) or len(sents) != len(set(sents)):
        return "incoherent"
    text = _FRR_TACTIC.sub(" ", prompt)  # R2-4b: an ATT&CK tactic tag is not intent
    for m in _FRR_COVERT.finditer(text):
        clause = _FRR_CLAUSE.split(text[:m.start()])[-1]
        if not _FRR_NEG.search(clause):  # R2-4b: "should not ... evading detection" is not covert intent
            return "covert-harmful"
    if _FRR_BORDER.search(prompt):
        return "borderline"
    return "benign"


# ---------------------------------------------------------------- first public date (r2, R2-5)
def first_public_date(published: str | None, cve5: dict | None = None,
                      ref_dates: dict[str, str] | None = None) -> tuple[str | None, str]:
    """min(NVD published, CVE5 containers.cna.datePublic, dates of references such as GHSA/commit/release pages).
    Returns (YYYY-MM-DD or None, basis)."""
    cands: list[tuple[str, str]] = []
    if published:
        cands.append((published[:10], "nvd_published"))
    dp = (((cve5 or {}).get("containers") or {}).get("cna") or {}).get("datePublic")
    if dp:
        cands.append((dp[:10], "cve5_datePublic"))
    for url, d in sorted((ref_dates or {}).items()):
        if d:
            cands.append((d[:10], f"reference:{url}"))
    if not cands:
        return None, "none"
    return min(cands)


def load_dev_exposed(path: Path = DEV_EXPOSED_FILE) -> set[str]:
    if not Path(path).exists():
        return set()
    return {k for ks in json.loads(Path(path).read_text())["suites"].values() for k in ks}


# ---------------------------------------------------------------- annotate (builder option)
def annotate(suite: str, items: list[dict], leak: str = "default", cap: int = DEFAULT_CAP,
             seed: int = c.DEFAULT_SEED, cutoffs: dict | None = None, heldout: set[str] | None = None,
             overrides: list[dict] | None = None, dev_keys: set[str] | None = None) -> list[dict]:
    """Return copies of items with meta.rigor filled in (and leaks stripped / keys overridden if asked)."""
    dev_keys = load_dev_exposed() if dev_keys is None else dev_keys
    action = default_leak_action(suite) if leak == "default" else leak
    cutoffs = load_cutoffs() if cutoffs is None else cutoffs
    heldout = load_heldout() if heldout is None else heldout
    by_ov = {(o["suite"], o["id"]): o for o in (overrides or [])}
    status = json.loads(SUITE_STATUS_FILE.read_text())["suites"] if SUITE_STATUS_FILE.exists() else {}
    at_ceiling = (status.get(suite) or {}).get("at_ceiling")
    out = []
    for it in items:
        it = dict(it, meta=dict(it.get("meta") or {}))
        if suite.startswith("cse-frr") and it.get("scorer") != "refusal":  # r4 (R4-6): D6 scores refusal tiers
            it["meta"]["legacy_scorer"] = it.get("scorer")
            it["scorer"] = "refusal"
        example_replaced = False
        if action == "strip":  # r4 (R4-7): our own prompts must not show a possible gold label as the example
            for old, new in FORMAT_EXAMPLES:
                if old in it["prompt"]:
                    it["meta"].setdefault("prompt_original_sha256", hashlib.sha256(it["prompt"].encode()).hexdigest())
                    it["prompt"] = it["prompt"].replace(old, new)
                    example_replaced = True
        flags = leak_flags(it)
        stripped: list[str] = []
        if action == "strip":
            new, stripped = strip_leaks(it)
            if stripped and stripped != ["unstrippable"]:
                it["meta"].setdefault("prompt_original_sha256", hashlib.sha256(it["prompt"].encode()).hexdigest())
                new["meta"] = it["meta"]
                it = new
        r = {"content_sha256": content_sha(it), **information(it), "leak": flags,
             "leak_action": action if flags else None, "leak_stripped": stripped,
             "leak_remaining": [f for f in leak_flags(it)] if stripped else flags,
             **label_provenance(it), "public_date": item_public_date(it),
             "benchmark_release": SUITE_INFO.get(family(suite), {}).get("release"),
             "heldout_key": heldout_key(it)}
        r["format_example_replaced"] = example_replaced
        r["heldout"] = r["heldout_key"] in heldout
        r["dev_exposed"] = r["heldout_key"] in dev_keys
        r["cwe_name_in_text"] = cwe_name_in_text(it)
        if suite.startswith("cse-frr"):
            r["frr_tier"] = frr_tier(it["prompt"])
            r["frr_exclude"] = r["frr_tier"] == "covert-harmful"
        labels = it["meta"].get("labels") or {}
        if suite.startswith("nvd-"):
            fp = labels.get("first_public") or {}
            r["first_public_date"] = fp.get("date") or (it["meta"].get("published") or None)
            r["first_public_basis"] = fp.get("basis") or "nvd_published (pending refetch)"
        r["suite_at_ceiling"] = at_ceiling
        r["suite_status"] = (status.get(suite) or {}).get("status")
        cve = cve_of(it)
        pub = it["meta"].get("published") or ""
        r["late_published_cve"] = bool(cve and pub and int(cve[4:8]) < int(pub[:4]))
        r["late_cve_id"] = bool(cve and pub and int(cve[4:8]) < int(pub[:4]) - 1)  # r3 (R3-6) stratum
        # r4 (R4-2): contamination is judged from the first public date, not the NVD publish date
        if suite.startswith("nvd-"):
            r["public_date"] = r["first_public_date"]
            if r["late_cve_id"] and str(r["first_public_basis"]).startswith("nvd_published"):
                r["post_cutoff"] = {m: None for m in cutoffs}  # true first-public date unknown, maybe much earlier
            else:
                r["post_cutoff"] = post_cutoff(r["public_date"], cutoffs)
        else:
            r["post_cutoff"] = post_cutoff(r["public_date"], cutoffs)
        if task_kind(it) == "cwe":  # r4 (R4-3)
            cm = cwe_meta().get(str(it.get("answer", "")).upper()) or {}
            r["cwe_mapping_usage"], r["cwe_abstraction"] = cm.get("usage"), cm.get("abstraction")
        r["cna_cluster"] = labels.get("cna") or None  # r4 (R4-4)
        r["other_cwe_name_in_text"] = other_cwe_name_in_text(it)
        o = by_ov.get((it["suite"], it["id"]))
        if o:
            r["key_override"] = {"original": it["answer"], "justification": o["justification"],
                                 "source": o["source"], "reviewer": o["reviewer"], "date": o["date"]}
            it["answer"] = o["answer"]
        it["meta"]["rigor"] = r
        out.append(it)
    dd = dedupe(out, cap=cap, seed=seed)
    exposed_t = {v["template_cluster"] for it in out for v in [dd[it["id"]]] if it["meta"]["rigor"]["dev_exposed"]}
    exposed_n = {v["near_dup_cluster"] for it in out for v in [dd[it["id"]]] if it["meta"]["rigor"]["dev_exposed"]}
    for it in out:
        r, v = it["meta"]["rigor"], dd[it["id"]]
        r.update(v)
        # r2 (R2-2): another member of my cluster was used in development (my own exposure is dev_exposed)
        r["dev_cluster_exposed"] = v["template_cluster"] in exposed_t
        r["dev_neardup_exposed"] = v["near_dup_cluster"] in exposed_n
    # r4 (R4-4): split fields for a dev-trained text baseline
    sealed_t = {it["meta"]["rigor"]["template_cluster"] for it in out if it["meta"]["rigor"]["heldout"]}
    for it in out:
        r = it["meta"]["rigor"]
        r["split"] = "sealed" if r["heldout"] else "dev" if r["dev_exposed"] else "other"
        r["template_straddles_split"] = r["template_cluster"] in exposed_t and r["template_cluster"] in sealed_t
        r["template_has_sealed"] = r["template_cluster"] in sealed_t
        # training rows for a text baseline: never sealed, and no sealed item in the same template cluster
        r["baseline_train_ok"] = not r["heldout"] and not r["template_has_sealed"]
    return out


def pool(items: list[dict], has_signal: bool = False) -> list[dict]:
    """Items eligible for a rigor sample: dedupe-kept, not sealed held-out, optionally has_signal."""
    return [it for it in items
            if it["meta"]["rigor"]["dedupe_keep"] and not it["meta"]["rigor"]["heldout"]
            and (not has_signal or it["meta"]["rigor"]["has_signal"] is not False)]


def suite_stats(items: list[dict]) -> dict:
    rs = [it["meta"]["rigor"] for it in items]
    dd = {it["id"]: it["meta"]["rigor"] for it in items}
    st = cluster_stats(items, dd)
    st["has_signal"] = dict(Counter(str(r["has_signal"]) for r in rs))
    st["lexicon_signal"] = dict(Counter(str(r["lexicon_signal"]) for r in rs))
    st["boilerplate"] = sum(r["boilerplate"] for r in rs)
    st["leak"] = dict(Counter(f for r in rs for f in r["leak"]))
    st["leak_stripped_items"] = sum(1 for r in rs if r["leak_stripped"])
    st["label_source"] = dict(Counter(r["label_source"].split(":")[0] if r["label_source"].startswith("cna")
                                      else r["label_source"].split(":")[0] for r in rs))
    st["heldout"] = sum(r["heldout"] for r in rs)
    st["dev_exposed"] = sum(r.get("dev_exposed", False) for r in rs)
    st["sealed_dev_cluster_exposed"] = sum(r["heldout"] and r.get("dev_cluster_exposed", False) for r in rs)
    st["sealed_dev_neardup_exposed"] = sum(r["heldout"] and r.get("dev_neardup_exposed", False) for r in rs)
    st["sealed_but_dev_exposed"] = sum(r["heldout"] and r.get("dev_exposed", False) for r in rs)
    st["cwe_name_in_text"] = dict(Counter(str(r.get("cwe_name_in_text")) for r in rs))
    if any("frr_tier" in r for r in rs):
        st["frr_tier"] = dict(Counter(r["frr_tier"] for r in rs))
    st["late_published_cve"] = sum(r["late_published_cve"] for r in rs)
    st["late_cve_id"] = sum(r.get("late_cve_id", False) for r in rs)
    st["other_cwe_name_in_text"] = dict(Counter(str(r.get("other_cwe_name_in_text")) for r in rs))
    models = sorted({m for r in rs for m in r["post_cutoff"]})
    st["post_cutoff"] = {m: dict(Counter(str(r["post_cutoff"][m]) for r in rs)) for m in models}
    st["pool_dedup"] = len(pool(items))
    st["pool_dedup_signal"] = len(pool(items, has_signal=True))
    return st


# ---------------------------------------------------------------- results-based reports (task 6, 8)
def load_runs(results_dir: Path) -> dict[str, dict]:
    """model -> {id: score row} using thinking-off runs; a later run of the same model wins per item."""
    runs: dict[str, dict] = defaultdict(dict)
    for d in sorted(Path(results_dir).iterdir()):
        rj = d / "run.json"
        if not rj.exists() or not (d / "scores.jsonl").exists():
            continue
        run = json.loads(rj.read_text())
        if run.get("thinking") not in (None, "off") or "sys" in d.name:
            continue
        model = run.get("model", d.name)
        for line in (d / "scores.jsonl").read_text().splitlines():
            if line.strip():
                row = json.loads(line)
                runs[model][row["id"]] = row
    return dict(runs)


def key_audit(runs: dict[str, dict], items_by_id: dict[str, dict], min_models: int = 4) -> list[dict]:
    """Items where every model that scored it gave the same parsed answer and it is wrong (for a human)."""
    ids = set().union(*[set(r) for r in runs.values()]) if runs else set()
    out = []
    for iid in sorted(ids):
        rows = {m: r[iid] for m, r in runs.items() if iid in r}
        if len(rows) < min_models:
            continue
        if not all(x["scorer"] in ("mcq_letter", "cwe_match") for x in rows.values()):
            continue
        parsed = {str(x.get("parsed") or "").strip().upper() for x in rows.values()}
        if len(parsed) == 1 and "" not in parsed and not any(x.get("passed") for x in rows.values()):
            it = items_by_id.get(iid, {})
            out.append({"id": iid, "suite": next(iter(rows.values()))["suite"], "key": it.get("answer"),
                        "consensus": parsed.pop(), "n_models": len(rows), "models": sorted(rows),
                        "prompt": it.get("prompt", "")[-1500:], "choices": it.get("choices"),
                        "meta": {k: v for k, v in (it.get("meta") or {}).items() if k in ("url", "cve", "category", "source_id")}})
    return out


def saturation(runs: dict[str, dict]) -> dict:
    per: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for m, rows in runs.items():
        for r in rows.values():
            if r.get("passed") is not None and r.get("status", "ok") == "ok":
                per[r["suite"]][m].append(bool(r["passed"]))
    out = {}
    for s, ms in sorted(per.items()):
        # r3 (R3-5): every model gets a status; none is silently dropped
        all_rates = {m: round(sum(v) / len(v), 3) for m, v in ms.items()}
        model_status = {m: ("insufficient" if len(ms[m]) < CEILING_MIN_N else
                            "ceiling" if all_rates[m] >= CEILING else "below") for m in ms}
        sufficient = [m for m, st in model_status.items() if st != "insufficient"]
        if len(sufficient) < 2:
            status = "insufficient"
        elif any(model_status[m] == "below" for m in sufficient):
            status = "not_at_ceiling"
        elif len(sufficient) == len(ms):
            status = "at_ceiling"
        else:
            status = "at_ceiling_among_sufficient"
        out[s] = {"rates": {m: all_rates[m] for m in sufficient}, "all_rates": all_rates,
                  "n": {m: len(v) for m, v in ms.items()}, "model_status": model_status,
                  "at_ceiling": status == "at_ceiling", "status": status}
    return out


# ---------------------------------------------------------------- CLI
def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(l) for l in Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]


EVALS_DIR = HERE.parent
# Committed item files outside the data dir (W1, 2026-10-11): test fixtures, samples and any other *.jsonl under
# evals/ are development files too; the gitignored data/results trees and the manifests are not committed items.
_NOT_COMMITTED_ITEMS = ("data", "results")


def committed_item_files(evals_dir: Path = EVALS_DIR) -> list[Path]:
    """Every committed *.jsonl under evals/ (test fixtures, samples, ...): they are development files."""
    out = []
    for f in sorted(Path(evals_dir).rglob("*.jsonl")):
        rel = f.relative_to(evals_dir).parts
        if rel[0] in _NOT_COMMITTED_ITEMS or "__pycache__" in rel:
            continue
        out.append(f)
    return out


def item_file_keys(path: Path) -> set[str]:
    """Held-out keys an item file exposes: each item's key from its own text, plus any key it records in
    meta.rigor.heldout_key (fixtures whose text was removed still carry the key, ids and gold answers)."""
    keys = set()
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            it = json.loads(line)
        except ValueError:
            continue
        if not isinstance(it, dict) or "suite" not in it:
            continue
        rk = ((it.get("meta") or {}).get("rigor") or {}).get("heldout_key")
        if rk:
            keys.add(rk)
        try:
            keys.add(heldout_key(it))
        except (KeyError, TypeError, AttributeError):
            pass
    return keys


def dev_exposed_keys(data_dir: Path, results_dir: Path | None,
                     committed: Iterable[Path] | None = None) -> set[str]:
    """Content keys of every item in a file already used for development: all sample files, every
    dataset file a stored run read, and every committed item file under evals/ (test fixtures, samples;
    ``committed`` defaults to committed_item_files())."""
    files = set(Path(data_dir).glob("*.sample*.jsonl"))
    if results_dir and Path(results_dir).exists():
        for rj in Path(results_dir).glob("*/run.json"):
            for ds in json.loads(rj.read_text()).get("datasets", []):
                p = Path(data_dir) / Path(ds["path"]).name
                if p.exists():
                    files.add(p)
    keys = set()
    for f in sorted(files):
        for it in read_jsonl(f):
            keys.add(heldout_key(it))
    for f in (committed_item_files() if committed is None else committed):
        keys |= item_file_keys(f)
    return keys


def seal(data_dir: Path, results_dir: Path | None, suites: list[str]) -> dict:
    exposed = dev_exposed_keys(data_dir, results_dir)
    doc = {"version": "heldout-v1", "sealed": _dt.date.today().isoformat(),
           "rule": (f"item sealed iff its key is in no development file (every *.sample*.jsonl, every dataset "
                    f"a stored run read, and every committed *.jsonl under evals/ such as test fixtures) and "
                    f"int(sha256('{HELDOUT_SALT}|'+key)[:8],16) % 100 < {HELDOUT_PCT}; "
                    "key = CVE id for CVE items (sealed jointly across suites), else suite|sha256(normalized content)"),
           "use": "never run sealed items during harness development; only in a pre-registered confirmatory run",
           "prospective": {"nvd-recent": "CVEs published 2026-10-01..2026-10-31 (fetch only after 2026-11-30, "
                                         "only for a confirmatory run; never used for development)"},
           "suites": {}}
    for s in suites:
        p = Path(data_dir) / f"{s}.jsonl"
        if not p.exists():
            continue
        items = read_jsonl(p)
        entries = [f"{it['id']}\t{heldout_key(it)}" for it in items
                   if heldout_key(it) not in exposed and in_heldout_partition(heldout_key(it))]
        doc["suites"][s] = {"n_items": len(items),
                            "n_dev_exposed": sum(1 for it in items if heldout_key(it) in exposed),
                            "n_sealed": len(entries), "commitment_sha256": commitment(entries),
                            "entries": entries}
    return doc


def exposure(data_dir: Path, results_dir: Path | None, suites: list[str]) -> dict:
    """Dev-exposed keys per suite (ids are not needed: keys are what clusters and CVEs match on)."""
    exposed = dev_exposed_keys(data_dir, results_dir)
    doc = {"version": "dev-exposed-v1", "computed": _dt.date.today().isoformat(),
           "rule": ("key of every item in a development file (every *.sample*.jsonl, every dataset a stored run read, "
                    "and every committed *.jsonl under evals/ such as test fixtures)"),
           "suites": {}}
    for s in suites:
        p = Path(data_dir) / f"{s}.jsonl"
        if p.exists():
            doc["suites"][s] = sorted({heldout_key(it) for it in read_jsonl(p)} & exposed)
    return doc


def verify_seal(doc: dict, data_dir: Path) -> dict[str, bool]:
    ok = {}
    for s, rec in doc["suites"].items():
        p = Path(data_dir) / f"{s}.jsonl"
        if not p.exists():
            continue
        cur = {it["id"]: heldout_key(it) for it in read_jsonl(p)}
        ok[s] = (commitment(rec["entries"]) == rec["commitment_sha256"]
                 and all(cur.get(e.split("\t", 1)[0]) == e.split("\t", 1)[1] for e in rec["entries"]))
    return ok


HELDOUT_V11_FILE = HERE / "heldout" / "heldout-v1.1.json"


def seal_v11(v1: dict, rigor_dir: Path, legacy_dir: Path | None = None) -> dict:
    """heldout-v1.1: v1 membership unchanged (asserted) + a content key per sealed entry, computed on the rigor
    (leak-stripped) files. Records which sealed items changed text by stripping (vs legacy_dir)."""
    doc = {"version": "heldout-v1.1", "built": _dt.date.today().isoformat(), "supersedes": "heldout-v1",
           "rule": v1["rule"] + "; content_key = key|sha256(canon(prompt) + US + json(answer)) on the rigor files",
           "use": v1["use"], "prospective": v1.get("prospective", {}), "suites": {}}
    for s, rec in v1["suites"].items():
        p = Path(rigor_dir) / f"{s}.jsonl"
        if not p.exists():
            continue
        by_id = {it["id"]: it for it in read_jsonl(p)}
        legacy = {it["id"]: it for it in read_jsonl(Path(legacy_dir) / f"{s}.jsonl")} if legacy_dir and (
            Path(legacy_dir) / f"{s}.jsonl").exists() else {}
        entries, changed, stripped = [], [], []
        for e in rec["entries"]:
            iid, key = e.split("\t", 1)
            it = by_id[iid]
            if heldout_key(it) != key:
                raise ValueError(f"{s}/{iid}: membership key changed ({key} -> {heldout_key(it)})")
            entries.append(f"{iid}\t{key}\t{heldout_content_key(it)}")
            if iid in legacy and legacy[iid]["prompt"] != it["prompt"]:
                changed.append(iid)
            ls = ((it.get("meta") or {}).get("rigor") or {}).get("leak_stripped") or []
            if ls and ls != ["unstrippable"]:
                stripped.append(iid)
        doc["suites"][s] = {"n_sealed": len(entries), "v1_commitment_sha256": rec["commitment_sha256"],
                            "commitment_sha256": commitment(entries), "text_changed_by_stripping": stripped,
                            "text_changed_vs_legacy": changed,
                            "entries": entries}
    return doc


def verify_items(items: Iterable[dict], doc: dict, complete: bool = False, sealed_only: bool = False) -> list[str]:
    """Problems with sealed items against heldout-v1.1 (R4-5, the same checks as run.check_freeze).
    Suites in the manifest are checked by id: rewritten (content key differs), swapped/misplaced (a sealed key
    under another id), missing (complete=True: every sealed id of a suite present in the files), extra
    (sealed_only=True: no non-sealed item). Other suites (derived, e.g. nvdlab) fall back to the key check."""
    items = list(items)
    by_id: dict[str, dict[str, tuple[str, str]]] = {}
    allowed: dict[str, set[str]] = defaultdict(set)
    for s, rec in doc["suites"].items():
        by_id[s] = {}
        for e in rec["entries"]:
            iid, key, ck = e.split("\t")
            by_id[s][iid] = (key, ck)
            allowed[key].add(ck)
    probs, seen = [], defaultdict(set)
    for it in items:
        s, key, ck = it["suite"], heldout_key(it), heldout_content_key(it)
        if s in by_id:
            seen[s].add(it["id"])
            want = by_id[s].get(it["id"])
            if want is not None:
                if want[0] != key:
                    probs.append(f"{it['id']}: swapped: holds key {key}, sealed key for this id is {want[0]}")
                elif want[1] != ck:
                    probs.append(f"{it['id']}: rewritten: text/answer differ from heldout-v1.1")
            elif key in allowed:
                probs.append(f"{it['id']}: swapped/misplaced: carries sealed key {key} under a non-sealed id")
            elif sealed_only:
                probs.append(f"{it['id']}: extra: not a sealed item")
        else:
            if key in allowed and ck not in allowed[key]:
                probs.append(f"{it['id']}: sealed key {key} but its text/answer do not match heldout-v1.1")
            elif sealed_only and key not in allowed:
                probs.append(f"{it['id']}: extra: not a sealed item")
    if complete:
        for s in seen:
            missing = sorted(set(by_id[s]) - seen[s])
            if missing:
                probs.append(f"{s}: missing {len(missing)} sealed item(s), e.g. {', '.join(missing[:3])}")
    return probs


def baseline_split_rows(items: Iterable[dict]) -> list[dict]:
    """Rows for a text baseline (R4-4). train_ok = any non-sealed item whose template cluster has no sealed member;
    train_ok_dev_only = the same restricted to items already used in development; eval rows are sealed items."""
    rows = []
    for it in items:
        r = it["meta"]["rigor"]
        rows.append({"id": it["id"], "suite": it["suite"], "split": r["split"],
                     "template_cluster": r["template_cluster"], "near_dup_cluster": r["near_dup_cluster"],
                     "template_straddles_split": r["template_straddles_split"],
                     "template_has_sealed": r["template_has_sealed"], "cna_cluster": r.get("cna_cluster"),
                     "train_ok": r["baseline_train_ok"], "train_ok_dev_only": r["split"] == "dev" and not r["template_has_sealed"],
                     "answer": it["answer"]})
    return rows


def regen(data_dir: Path, rigor_dir: Path, out_dir: Path, names: list[str]) -> dict[str, int]:
    """Annotated copies of dataset files (R3-7): each item becomes its annotated parent from rigor_dir (stripped
    prompt, meta.rigor), keeping the source file's suite/id (derived nvdlab sets) and extra top-level fields."""
    parents: dict[str, dict] = {}
    for p in Path(rigor_dir).glob("*.jsonl"):
        if ".sample" not in p.name:
            for it in read_jsonl(p):
                parents[it["id"]] = it
    out_dir.mkdir(parents=True, exist_ok=True)
    done = {}
    for name in names:
        src = read_jsonl(Path(data_dir) / name)
        rows = []
        for it in src:
            pid = ((it.get("meta") or {}).get("source_id") if re.search(r"-[a-z]+lab$", it["suite"]) else None) \
                or it["id"]
            par = parents.get(pid)
            if par is None:
                raise KeyError(f"{name}: no annotated parent for {it['id']} ({pid})")
            new = dict(par, suite=it["suite"], id=it["id"], meta=dict(par["meta"]))
            if pid != it["id"]:
                new["meta"]["source_id"] = pid
            for k, v in it.items():
                if k not in new:
                    new[k] = v  # e.g. "system" in *.sysctx.jsonl
            rows.append(new)
        (out_dir / name).write_bytes(c.to_jsonl(rows))
        done[name] = len(rows)
    return done


def run_dataset_files(results_dir: Path) -> list[str]:
    names = set()
    for rj in Path(results_dir).glob("*/run.json"):
        for ds in json.loads(rj.read_text()).get("datasets", []):
            names.add(Path(ds["path"]).name)
    return sorted(names)


SEAL_SUITES = ["ctibench-mcq", "ctibench-rcm", "ctibench-vsp", "ctibench-ate", "cybermetric-500", "secqa-v1",
               "secqa-v2", "sevenllm-mcq", "sevenllm-qa", "cse-frr", "nvd-cwe", "nvd-cvss"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s1 = sub.add_parser("seal", help="compute the sealed held-out split (writes ids + commitment only)")
    s1.add_argument("--data-dir", default=str(c.DATA_DIR))
    s1.add_argument("--results-dir", default=str(c.REPO_ROOT / "evals" / "results"))
    s1.add_argument("--out", default=str(HELDOUT_FILE))
    s5 = sub.add_parser("seal-v11", help="write heldout-v1.1.json (v1 membership + content keys on rigor files)")
    s5.add_argument("--rigor-dir", required=True, help="dir of annotated (stripped) full suite files")
    s5.add_argument("--legacy-dir", default=str(c.DATA_DIR), help="unstripped files, to list text changes")
    s5.add_argument("--out", default=str(HELDOUT_V11_FILE))
    s6 = sub.add_parser("verify", help="check files against heldout-v1.1 content keys (exit 1 on any problem)")
    s6.add_argument("files", nargs="+")
    s6.add_argument("--manifest", default=str(HELDOUT_V11_FILE))
    s6.add_argument("--complete", action="store_true", help="every sealed id of each checked suite must be present")
    s6.add_argument("--sealed-only", action="store_true", help="no non-sealed item may be present")
    s8 = sub.add_parser("baseline-split", help="split rows for the text baseline (jsonl to stdout or --out)")
    s8.add_argument("files", nargs="+", help="annotated suite files")
    s8.add_argument("--out", default=None)
    s7 = sub.add_parser("regen", help="annotated copies of every dataset file the stored runs read")
    s7.add_argument("--data-dir", default=str(c.DATA_DIR))
    s7.add_argument("--rigor-dir", required=True)
    s7.add_argument("--out-dir", required=True)
    s7.add_argument("--results-dir", default=str(c.REPO_ROOT / "evals" / "results"))
    s4 = sub.add_parser("exposure", help="write the dev-exposed key list (heldout/dev-exposed-v1.json)")
    s4.add_argument("--data-dir", default=str(c.DATA_DIR))
    s4.add_argument("--results-dir", default=str(c.REPO_ROOT / "evals" / "results"))
    s4.add_argument("--out", default=str(DEV_EXPOSED_FILE))
    s2 = sub.add_parser("verify-seal", help="check the commitment against a data dir")
    s2.add_argument("--data-dir", default=str(c.DATA_DIR))
    s3 = sub.add_parser("report", help="per-suite stats of annotated files in a dir (+ key audit, saturation)")
    s3.add_argument("--dir", required=True, help="dir of annotated <suite>.jsonl files")
    s3.add_argument("--results-dir", default=None)
    s3.add_argument("--data-dir", default=str(c.DATA_DIR), help="where the items the runs used live")
    s3.add_argument("--key-audit-out", default=None, help="write the all-models-agree-wrong list here (not the repo)")
    a = ap.parse_args(argv)
    if a.cmd == "seal":
        doc = seal(Path(a.data_dir), Path(a.results_dir), SEAL_SUITES)
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(doc, indent=1) + "\n")
        for s, r in doc["suites"].items():
            print(f"{s}: {r['n_items']} items, {r['n_dev_exposed']} dev-exposed, {r['n_sealed']} sealed")
        return 0
    if a.cmd == "seal-v11":
        doc = seal_v11(json.loads(HELDOUT_FILE.read_text()), Path(a.rigor_dir), Path(a.legacy_dir))
        Path(a.out).write_text(json.dumps(doc, indent=1) + "\n")
        for s, r in doc["suites"].items():
            print(f"{s}: {r['n_sealed']} sealed, {len(r['text_changed_by_stripping'])} changed by stripping, "
                  f"{len(r['text_changed_vs_legacy'])} differ from the legacy text")
        return 0
    if a.cmd == "verify":
        doc = json.loads(Path(a.manifest).read_text())
        items = [it for f in a.files for it in read_jsonl(Path(f))]
        bad = verify_items(items, doc, complete=a.complete, sealed_only=a.sealed_only)
        print("\n".join(bad) if bad else "ok: every sealed item matches heldout-v1.1")
        return 1 if bad else 0
    if a.cmd == "baseline-split":
        rows = [r for f in a.files for r in baseline_split_rows(read_jsonl(Path(f)))]
        data = "".join(json.dumps(r) + "\n" for r in rows)
        if a.out:
            Path(a.out).write_text(data)
        else:
            sys.stdout.write(data)
        return 0
    if a.cmd == "regen":
        names = run_dataset_files(Path(a.results_dir))
        for n, k in regen(Path(a.data_dir), Path(a.rigor_dir), Path(a.out_dir), names).items():
            print(f"{n}: {k} items")
        return 0
    if a.cmd == "exposure":
        doc = exposure(Path(a.data_dir), Path(a.results_dir), SEAL_SUITES)
        Path(a.out).write_text(json.dumps(doc, indent=1) + "\n")
        sealed = load_heldout()
        for s, keys in doc["suites"].items():
            print(f"{s}: {len(keys)} dev-exposed keys; sealed-and-exposed: {len(sealed & set(keys))}")
        return 0
    if a.cmd == "verify-seal":
        res = verify_seal(json.loads(HELDOUT_FILE.read_text()), Path(a.data_dir))
        print(json.dumps(res, indent=1))
        return 0 if all(res.values()) else 1
    report: dict = {"suites": {}}
    for p in sorted(Path(a.dir).glob("*.jsonl")):
        if ".sample" in p.name:
            continue
        items = read_jsonl(p)
        if items and "rigor" in items[0]["meta"]:
            report["suites"][p.stem] = suite_stats(items)
    if a.results_dir:
        runs = load_runs(Path(a.results_dir))
        items_by_id = {}
        for p in Path(a.data_dir).glob("*.jsonl"):
            for it in read_jsonl(p):
                items_by_id.setdefault(it["id"], it)
        ka = key_audit(runs, items_by_id)
        report["key_audit"] = {"n": len(ka), "by_suite": dict(Counter(x["suite"] for x in ka)),
                               "models": sorted(runs)}
        report["saturation"] = saturation(runs)
        if a.key_audit_out:
            Path(a.key_audit_out).write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in ka))
    print(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
