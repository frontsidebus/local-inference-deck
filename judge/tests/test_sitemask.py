"""Site values are masked before every frontier call (lib/sitemask, runner/run_judge.py), and the agent's free text
of a mixed infra bundle is claims-self-checked before it goes to the frontier judge (collector/claims_only.py).
Example values only (RFC 5737 addresses, example-site.net)."""
import json
import sys
from pathlib import Path

import pytest

JUDGE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(JUDGE))
sys.path.insert(0, str(JUDGE / "runner"))
sys.path.insert(0, str(JUDGE / "collector"))
from lib import sitemask as SM  # noqa: E402
import claims_only as CO  # noqa: E402
import run_judge as RJ  # noqa: E402
import validate as V  # noqa: E402
from test_runner import env, frontier, local, rid, enqueue, finding, KEY  # noqa: E402,F401

SITE = {
    "SPARK_DOMAIN": "example-site.net",
    "SPARK_API_HOST": "api.example-site.net",
    "SPARK_CHAT_HOST": "chat.example-site.net",
    "SPARK_DIGEST_HOST": "digest.example-site.net",
    "ADMIN_EMAIL": "owner@example-site.net",
    "EDGE_PUBLIC_IP": "198.51.100.7",
    "BACKEND_LAN_IP": "192.0.2.10",
    "BACKEND_WG_IP": "10.100.0.2",
    "HYPERVISOR_BRIDGE_IP": "192.0.2.1",
    "ADMIN_SOURCE_IPS": "203.0.113.5 203.0.113.6",
    "WG_SUBNET": "10.100.0.0/24",
    "BACKEND_SSH_USER": "operator",
    "EDGE_SSH_USER": "ubuntu",
    "SPARK_USERS": "alicex",
    "JUDGE_SSH_ALIASES": "edgebox-1",
    "RESTIC_BUCKET": "example-site-backups",
    "SPARK_SITE_NAME": "Northwind Spark",
    "WG_EDGE_PUBLIC_KEY": "Q2hhbmdlTWVQdWJsaWNLZXkwMTIzNDU2Nzg5YWJjZGVm=",
    "OAUTH2_PROXY_CLIENT_ID": "telemetry",
    "WEBUI_PORT": "3000",
}
VALUES = ["example-site.net", "198.51.100.7", "192.0.2.10", "10.100.0.2", "192.0.2.1", "203.0.113.5", "203.0.113.6",
          "10.100.0.0/24", "alicex", "edgebox-1", "example-site-backups", "Northwind Spark",
          "Q2hhbmdlTWVQdWJsaWNLZXkwMTIzNDU2Nzg5YWJjZGVm=", "198-51-100-7"]


@pytest.fixture
def mask():
    return SM.build(SITE)


def site_env(tmp_path, monkeypatch, extra=""):
    p = tmp_path / "site.env"
    p.write_text("".join(f'{k}="{v}"\n' for k, v in SITE.items()) + extra)
    monkeypatch.setenv("SITE_ENV", str(p))
    for k in SITE:
        monkeypatch.delenv(k, raising=False)
    return p


# ------------------------------------------------------------------ lib/sitemask
def test_placeholders_named_after_site_env_keys(mask):
    keys = set(mask.keys)
    for k in ("${SPARK_DOMAIN}", "${SPARK_API_HOST}", "${SPARK_DIGEST_HOST}", "${ADMIN_EMAIL}", "${EDGE_PUBLIC_IP}",
              "${EDGE_PUBLIC_IP:dashed}", "${ADMIN_SOURCE_IPS[1]}", "${ADMIN_SOURCE_IPS[2]}", "${WG_SUBNET}",
              "${WG_SUBNET:net}", "${BACKEND_SSH_USER}", "${EDGE_SSH_USER}", "${SPARK_USERS}", "${JUDGE_SSH_ALIASES}",
              "${RESTIC_BUCKET}", "${SPARK_SITE_NAME}", "${SPARK_DOMAIN:stem}", "${WG_EDGE_PUBLIC_KEY}"):
        assert k in keys, k
    # a plain-word client id and ports are not identifiers worth garbling the text for
    assert "${OAUTH2_PROXY_CLIENT_ID}" not in keys and "${WEBUI_PORT}" not in keys
    assert SM.build({}).keys == [] and not SM.build({})


def test_mask_longest_first_word_boundaries_and_urls(mask):
    t = ("base_url=https://api.example-site.net/v1 and https://digest.example-site.net:443/ui, "
         "new.example-site.net, mail owner@example-site.net, EXAMPLE-SITE.NET")
    assert mask.mask(t) == ("base_url=https://${SPARK_API_HOST}/v1 and https://${SPARK_DIGEST_HOST}:443/ui, "
                            "new.${SPARK_DOMAIN}, mail ${ADMIN_EMAIL}, ${SPARK_DOMAIN}")


def test_mask_addresses_exactly(mask):
    t = ("ssh operator@192.0.2.10; 192.0.2.100 and 192.0.2.1:8080 and 1.192.0.2.1; http://192.0.2.10/x "
         "route 10.100.0.0/24 via 10.100.0.2 net 10.100.0.0; ec2-198-51-100-7.compute; 9-198-51-100-7")
    assert mask.mask(t) == ("ssh ${BACKEND_SSH_USER}@${BACKEND_LAN_IP}; 192.0.2.100 and ${HYPERVISOR_BRIDGE_IP}:8080 "
                            "and 1.192.0.2.1; http://${BACKEND_LAN_IP}/x route ${WG_SUBNET} via ${BACKEND_WG_IP} net "
                            "${WG_SUBNET:net}; ec2-${EDGE_PUBLIC_IP:dashed}.compute; 9-198-51-100-7")


def test_generic_accounts_only_in_login_position(mask):
    t = "Ubuntu 24.04, the operator, ubuntu@198.51.100.7, /home/operator/x, ~ubuntu/y, ssh -l operator h, User ubuntu"
    assert mask.mask(t) == ("Ubuntu 24.04, the operator, ${EDGE_SSH_USER}@${EDGE_PUBLIC_IP}, /home/${BACKEND_SSH_USER}/x,"
                            " ~${EDGE_SSH_USER}/y, ssh -l ${BACKEND_SSH_USER} h, User ${EDGE_SSH_USER}")


def test_names_aliases_buckets_lists_and_sweep(mask):
    t = ("Northwind   Spark dashboard; ssh edgebox-1 true; s3:example-site-backups; from 203.0.113.6; "
         "home /home/alicex; file_alicex_notes; Host edgebox-1x")
    out = mask.mask(t)
    assert out == ("${SPARK_SITE_NAME} dashboard; ssh ${JUDGE_SSH_ALIASES} true; s3:${RESTIC_BUCKET}; from "
                   "${ADMIN_SOURCE_IPS[2]}; home /home/${SPARK_USERS}; file_${SPARK_USERS}_notes; Host "
                   "${JUDGE_SSH_ALIASES}x")
    assert mask.residual(out) == {}


def test_existing_placeholders_are_protected(mask):
    m = SM.build({**SITE, "SPARK_USERS": "spark"})
    t = "template ${SPARK_DOMAIN} and ${SPARK_API_KEY} and user spark"
    out = m.mask(t)
    assert out == "template ${SPARK_DOMAIN} and ${SPARK_API_KEY} and user ${SPARK_USERS}"
    assert m.residual(out) == {}
    assert m.literal_placeholders(t) == {"${SPARK_DOMAIN}"}


def test_unmask_round_trip_and_keep(mask):
    t = "curl https://api.example-site.net/v1 -> 401; ssh operator@192.0.2.10; bucket example-site-backups"
    m = mask.mask(t)
    assert mask.residual(m) == {} and all(v.lower() not in m.lower() for v in VALUES)
    assert mask.unmask(m) == t
    assert mask.unmask("see ${SPARK_DOMAIN} and ${SPARK_API_HOST}", keep={"${SPARK_DOMAIN}"}) == \
        "see ${SPARK_DOMAIN} and api.example-site.net"
    assert mask.unmask("unknown ${NOT_A_KEY}") == "unknown ${NOT_A_KEY}"
    obj = {"a": [m, {"b": m}], "n": 3}
    assert mask.unmask_obj(mask.mask_obj(obj)) == {"a": [t, {"b": t}], "n": 3}


def test_residual_counts_and_short_values_ignored():
    m = SM.build({"SPARK_DOMAIN": "example-site.net", "SPARK_USERS": "ab", "JUDGE_SSH_ALIASES": "walter"})
    assert m.keys == ["${SPARK_DOMAIN:stem}", "${SPARK_DOMAIN}"]  # 2-char user and the walter codename kept
    assert m.residual("x example-site.net y EXAMPLE-SITE.NET") == {"${SPARK_DOMAIN}": 2}


# ------------------------------------------------------------------ runner: frontier input is masked
LOG = ("2026-10-03 03:30:00,000 INFO [20261002_165907_fdc8ec] run_agent: provider=custom "
       "base_url=https://api.example-site.net/v1\n"
       "2026-10-03 03:31:00,000 ERROR [20261002_165907_fdc8ec] agent.tool_executor: connect to "
       "api.example-site.net:443 refused from 192.0.2.10 as operator@192.0.2.10\n")
REPLY = {"items": [{"id": "F1", "rubric": "R1", "severity": "high", "claim": "The API host answers",
                    "evidence": "hermes-log.txt shows `${SPARK_API_HOST}:443` was refused",
                    "verdict": "false",
                    "recommendation": "Check the gateway on ${BACKEND_LAN_IP} and https://${SPARK_API_HOST}/v1"}]}


def masked_bundle(env, r, claims="The API host answers"):
    req = enqueue(env, r)
    req["claims"] = claims
    (env / "queue" / f"{r}.json").write_text(json.dumps(req))
    ev = env / "evidence" / r
    (ev / "manifest.json").write_text(json.dumps({"request": req, "data_class": "infra", "artifacts": []}))
    (ev / "hermes-log.txt").write_text(LOG)
    (ev / "probes" / "ssh_alias_test-1.txt").unlink()
    return req


def test_frontier_input_has_no_site_values_and_finding_is_unmasked(env, frontier, tmp_path, monkeypatch):
    site_env(tmp_path, monkeypatch)
    monkeypatch.setenv("JUDGE_MODE", "frontier")
    frontier.replies(json.dumps(REPLY))
    r = rid()
    masked_bundle(env, r)
    assert RJ.main([r]) == 0
    (call,) = frontier.calls()
    sent = call["stdin"] + "\n".join(call["argv"])
    for v in VALUES + ["owner@example-site.net"]:
        assert v.lower() not in sent.lower(), v
    assert "https://${SPARK_API_HOST}/v1" in call["stdin"] and "${BACKEND_SSH_USER}@${BACKEND_LAN_IP}" in call["stdin"]
    assert "SITE VALUES ARE MASKED" in call["argv"][call["argv"].index("--system-prompt") + 1]
    f = finding(env, r)
    (it,) = f["items"]
    # validated against the masked bundle: the quote matched, so the false verdict stands
    assert it["verdict"] == "false" and it["severity"] == "high"
    # ... and stored unmasked: the owner and Hermes see real names
    assert it["evidence"] == "hermes-log.txt shows `api.example-site.net:443` was refused"
    assert it["recommendation"] == "Check the gateway on 192.0.2.10 and https://api.example-site.net/v1"
    assert any(n.startswith("site values masked for the frontier judge") for n in f["notes"])
    assert not any("example-site" in n or "192.0.2" in n for n in f["notes"])
    raw = (env / "evidence" / r / "judge-raw.txt").read_text()
    assert "${SPARK_API_HOST}" in raw  # the reply as received


def test_validating_against_the_unmasked_bundle_would_be_wrong(env, tmp_path, monkeypatch):
    """The order matters: the judge quotes the masked text, so the unmasked bundle does not contain its quote."""
    mask = SM.build(SITE)
    bundle = "=== FILE: hermes-log.txt ===\n" + LOG
    req = {"id": rid(), "kind": "completion", "claims": "The API host answers", "plan": None}
    kw = dict(request_id=rid(), judge="t", mode="frontier", created="2026-10-03T04:00:00Z")
    good, _, _ = V.validate_finding(json.loads(json.dumps(REPLY)), bundle_text=mask.mask(bundle),
                                    request=mask.mask_obj(req), **kw)
    bad, _, _ = V.validate_finding(json.loads(json.dumps(REPLY)), bundle_text=bundle, request=req, **kw)
    assert good["items"][0]["verdict"] == "false"
    assert bad["items"][0]["verdict"] == "n/a"  # would have been downgraded as "absence of evidence"
    ev = "api ${SPARK_API_HOST}:443 refused from ${BACKEND_LAN_IP} as"
    assert V.evidence_reason(ev, mask.mask(bundle)) == "bundle" and V.evidence_reason(ev, bundle) is None


def test_template_placeholders_stay_ambiguous(env, frontier, tmp_path, monkeypatch):
    site_env(tmp_path, monkeypatch)
    monkeypatch.setenv("JUDGE_MODE", "frontier")
    r = rid()
    masked_bundle(env, r)
    (env / "evidence" / r / "agent-diff.patch").write_text(
        "--- a/edge/Caddyfile.tmpl\n+++ b/edge/Caddyfile.tmpl\n@@ -1 +1 @@\n-old\n+chat.${SPARK_DOMAIN} {\n")
    frontier.replies(json.dumps({"items": [{
        "id": "F1", "rubric": "R2", "severity": "low", "claim": "template uses the domain variable",
        "evidence": "agent-diff.patch: `+chat.${SPARK_DOMAIN} {` and `base_url=https://${SPARK_API_HOST}/v1`",
        "verdict": "true", "recommendation": "none"}]}))
    assert RJ.main([r]) == 0
    f = finding(env, r)
    ev = f["items"][0]["evidence"]
    assert "+chat.${SPARK_DOMAIN} {" in ev and "https://api.example-site.net/v1" in ev
    assert any("${SPARK_DOMAIN}" in n and "literally" in n for n in f["notes"])


def test_local_judge_gets_real_values(env, local, tmp_path, monkeypatch):
    site_env(tmp_path, monkeypatch)
    monkeypatch.setenv("JUDGE_MODE", "local")
    r = rid()
    masked_bundle(env, r)
    assert RJ.main([r]) == 0
    (req,) = local["requests"]
    text = json.dumps(req["body"]["messages"])
    assert "api.example-site.net" in text and "${SPARK_API_HOST}" not in text
    assert "SITE VALUES ARE MASKED" not in text


def test_probe_args_unmasked_and_results_masked(env, frontier, tmp_path, monkeypatch):
    site_env(tmp_path, monkeypatch)
    monkeypatch.setenv("JUDGE_MODE", "frontier")
    r = rid()
    req = masked_bundle(env, r)
    seen = {}

    def fake_probes(reqs, ev):
        seen["reqs"] = reqs
        return "$ probe http_status https://api.example-site.net/v1\n401\nexit=0\n"
    monkeypatch.setattr(RJ, "run_probes", fake_probes)
    frontier.replies(json.dumps({"probe_requests": [{"name": "http_status", "args": ["https://${SPARK_API_HOST}/v1"]}]}),
                     json.dumps(REPLY))
    res = RJ.judge_bundle(r, req, env / "evidence" / r, "frontier", [], probes_allowed=True, use_budget=False)
    assert seen["reqs"] == [{"name": "http_status", "args": ["https://api.example-site.net/v1"]}]
    second = frontier.calls()[1]["stdin"]
    assert "probe http_status https://${SPARK_API_HOST}/v1" in second and "example-site" not in second
    assert "example-site" not in res["input"]


def test_mask_self_check_fails_closed(env, frontier, tmp_path, monkeypatch):
    site_env(tmp_path, monkeypatch)
    r = rid()
    req = masked_bundle(env, r)
    monkeypatch.setattr(SM.SiteMask, "residual", lambda self, text: {"${SPARK_DOMAIN}": 1})
    with pytest.raises(RJ.JudgeError, match="site-mask self-check failed"):
        RJ.judge_bundle(r, req, env / "evidence" / r, "frontier", [], probes_allowed=False, use_budget=False)
    assert frontier.calls() == []


def test_claims_stage_input_masked_too(env, frontier, local, tmp_path, monkeypatch):
    site_env(tmp_path, monkeypatch)
    monkeypatch.setenv("JUDGE_MODE", "frontier")
    r = rid()
    req = masked_bundle(env, r, claims="Deployed; the API at api.example-site.net answers 200.")
    frontier.replies(json.dumps({"items": [{"id": "F1", "rubric": "R1", "severity": "low", "claim": "answers 200",
                                            "evidence": "tool-activity.jsonl: `\"tool_calls\": 0`", "verdict": "n/a",
                                            "recommendation": "Probe https://${SPARK_API_HOST}/v1 next time."}]}))
    res = RJ.judge_claims(r, req, env / "evidence" / r, [], use_budget=False)
    (call,) = frontier.calls()
    assert "example-site" not in call["stdin"].lower() and "example-site" not in res["input"]
    assert res["finding"]["items"][0]["recommendation"] == "Probe https://api.example-site.net/v1 next time."


# ------------------------------------------------------------------ mixed bundles: agent free text self-checked
IDENT = CO.Identity(users=["alicex"], hosts=["edgebox-1"], domains=["example-site.net"])


def test_is_mixed():
    assert CO.is_mixed({"data_class": "infra", "classification": {"withheld_sensitive_paths": 2}})
    assert CO.is_mixed({"data_class": "infra", "withheld": {"sensitive_paths": "2 non-infra path(s) ..."}})
    assert not CO.is_mixed({"data_class": "infra", "classification": {"withheld_sensitive_paths": 0}})
    assert not CO.is_mixed({"data_class": "sensitive", "classification": {"withheld_sensitive_paths": 2}})
    assert not CO.is_mixed(None)


def test_gate_free_text_masks_then_checks():
    infra = lambda p: p.startswith("/etc/")  # noqa: E731
    text = ("Updated /etc/caddy/Caddyfile and /home/alicex/private/notes.txt; the box at 198.51.100.99 owned by "
            "alicex:alicex now; digest 0123456789abcdef0123.")
    out, problems = CO.gate_free_text(text, IDENT, infra)
    assert problems == []
    assert "/etc/caddy/Caddyfile" in out and "/home/alicex/private" not in out and "file#1" in out
    assert "198.51.100.99" not in out and "ip#1" in out and "alicex:alicex" not in out
    assert "0123456789abcdef0123" not in out and "hex#1" in out


def test_gate_free_text_withholds_what_cannot_be_masked():
    text = "Here is the change:\n@@ -1,2 +1,2 @@\n-secret_mode = off\n+secret_mode = on\n"
    out, problems = CO.gate_free_text(text, IDENT, lambda p: True)
    assert problems and out.startswith("[agent free text withheld: the claims self-check refused it (")
    assert "secret_mode" not in out and "diff hunk or header" in out
    out2, problems2 = CO.gate_free_text("token=sk-abcdefghijklmnopqrstuvwxyz0123", IDENT, lambda p: True)
    assert "sk-abc" not in out2 and problems2 == []  # redacted first, then nothing left to refuse


def mixed_bundle(env, r, claims, excerpt=None):
    req = masked_bundle(env, r, claims=claims)
    ev = env / "evidence" / r
    man = {"request": req, "data_class": "infra", "artifacts": [], "masked_request": True,
           "classification": {"withheld_sensitive_paths": 1},
           "withheld": {"sensitive_paths": "1 non-infra path(s) next to the infra ones (#43)"}}
    (ev / "manifest.json").write_text(json.dumps(man, indent=2))
    if excerpt is not None:
        (ev / "gate-decisions.jsonl").write_text(json.dumps(
            {"ts": "2026-10-03T03:30:00Z", "tool": "terminal", "decision": "pass", "excerpt": excerpt}) + "\n")
    return req


def test_mixed_bundle_free_text_masked_or_withheld_before_frontier(env, frontier, tmp_path, monkeypatch):
    site_env(tmp_path, monkeypatch)
    monkeypatch.setenv("JUDGE_MODE", "frontier")
    r = rid()
    claims = ("Done. The withheld notes say:\n@@ -1 +1 @@\n-PLAN_B=1\n+PLAN_B=2\nAPI at api.example-site.net is up.")
    mixed_bundle(env, r, claims, excerpt="cat /home/zed/diary.txt | grep 198.51.100.99")
    frontier.replies(json.dumps({"items": []}))
    assert RJ.main([r]) == 0
    (call,) = frontier.calls()
    stdin = call["stdin"]
    assert "PLAN_B" not in stdin and stdin.count("[agent free text withheld: the claims self-check refused it") == 2
    assert "/home/zed/diary.txt" not in stdin and "198.51.100.99" not in stdin and "file#1" in stdin
    f = finding(env, r)
    assert any(n.startswith("mixed bundle (1 withheld sensitive path(s))") and "1 withheld" in n and
               "diff hunk or header" in n for n in f["notes"])


def test_mixed_bundle_clean_answer_keeps_paths_and_site_placeholders(env, frontier, tmp_path, monkeypatch):
    site_env(tmp_path, monkeypatch)
    monkeypatch.setenv("JUDGE_MODE", "frontier")
    r = rid()
    mixed_bundle(env, r, "Edited /etc/caddy/Caddyfile; https://api.example-site.net/v1 answers 401.")
    frontier.replies(json.dumps({"items": []}))
    assert RJ.main([r]) == 0
    stdin = frontier.calls()[0]["stdin"]
    assert "/etc/caddy/Caddyfile" in stdin and "https://${SPARK_API_HOST}/v1 answers 401" in stdin
    assert "withheld: the claims self-check" not in stdin and "example-site" not in stdin


def test_non_mixed_and_local_bundles_are_not_gated(env, frontier, local, tmp_path, monkeypatch):
    site_env(tmp_path, monkeypatch)
    claims = "diff:\n@@ -1 +1 @@\n-a\n+b\n"
    monkeypatch.setenv("JUDGE_MODE", "frontier")
    r = rid()
    masked_bundle(env, r, claims=claims)  # infra, nothing withheld
    frontier.replies(json.dumps({"items": []}))
    assert RJ.main([r]) == 0
    assert "@@ -1 +1 @@" in frontier.calls()[0]["stdin"]
    monkeypatch.setenv("JUDGE_MODE", "local")
    r2 = rid(short="aaaaaa")
    mixed_bundle(env, r2, claims)
    local["replies"] = [json.dumps({"items": []})]
    assert RJ.main([r2]) == 0
    assert "@@ -1 +1 @@" in json.dumps(local["requests"][0]["body"]["messages"])


# ------------------------------------------------------------------ code-version stamp (guarded lib/version)
class FakeVersion:
    @staticmethod
    def code_version():
        return {"sha": "b" * 40, "commit": None, "dirty": False, "source": "git"}

    @staticmethod
    def mismatch_note(written, current, what="request", by="runner"):
        if isinstance(written, dict) and written.get("sha") == current.get("sha"):
            return None
        return f"code version: {what} mismatch ({by})"


def test_code_versions_stamped_when_lib_version_present(env, frontier, tmp_path, monkeypatch):
    site_env(tmp_path, monkeypatch)
    monkeypatch.setenv("JUDGE_MODE", "frontier")
    monkeypatch.setattr(RJ, "VER", FakeVersion)
    r = rid()
    req = masked_bundle(env, r)
    req["code_version"] = {"sha": "a" * 40, "commit": None, "dirty": False, "source": "git"}
    (env / "queue" / f"{r}.json").write_text(json.dumps(req))
    ev = env / "evidence" / r
    man = json.loads((ev / "manifest.json").read_text())
    man["code_versions"] = {"collector": FakeVersion.code_version()}
    (ev / "manifest.json").write_text(json.dumps(man))
    frontier.replies(json.dumps(REPLY))
    assert RJ.main([r]) == 0
    f = finding(env, r)
    assert f["code_versions"] == {"request": req["code_version"], "collector": FakeVersion.code_version(),
                                  "runner": FakeVersion.code_version()}
    assert "code version: request mismatch (runner)" in f["notes"]
    assert not any("evidence bundle mismatch" in n for n in f["notes"])
    assert f["items"][0]["evidence"].endswith("`api.example-site.net:443` was refused")  # still unmasked


def test_no_stamp_without_lib_version(env, frontier, monkeypatch):
    monkeypatch.setenv("JUDGE_MODE", "frontier")
    monkeypatch.setattr(RJ, "VER", None)
    r = rid()
    enqueue(env, r)
    assert RJ.main([r]) == 0
    assert "code_versions" not in finding(env, r)


def test_unconfirmed_labels_are_left_alone(mask):
    t = ('"unconfirmed_paths": ["[unconfirmed path #1 withheld]"], [sensitive path #2 withheld], '
         "code version: request written by judge code abcdef012345, judged by runner abcdef012345")
    assert mask.mask(t) == t and mask.unmask(t) == t
    out, problems = CO.gate_free_text("Touched [unconfirmed path #1 withheld] only.", IDENT, lambda p: True)
    assert problems == [] and out == "Touched [unconfirmed path #1 withheld] only."
