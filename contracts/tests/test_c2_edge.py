"""C2 (deck, both sides in this repo): the trusted proxy, OIDC clients and groups, and identity headers of
contracts/edge.yaml agree with the rendered edge (covenant) and backend (walter) files.

The port part of C2 is walter/tests/test_port_invariants.py (it reads the same contract). Everything is rendered
with scripts/render.sh and site.env.example (digest on, with a fake example host). No site values.
Run: python3 -m pytest contracts/tests -q -p no:cacheprovider
"""
import re

import pytest

from contract_helpers import CONTRACTS, REPO, example_env, example_value, load_yaml, render

EDGE = load_yaml(CONTRACTS / "edge.yaml")
SITES = REPO / "covenant/nginx/sites-available"


@pytest.fixture(scope="module")
def out(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("c2-edge")
    env = example_env(tmp)
    srcs = [*SITES.glob("*.tmpl"), *(REPO / "covenant/oauth2-proxy").glob("*.cfg.tmpl"),
            REPO / "walter/webui/pocketid-bootstrap.py.tmpl",
            *(REPO / f for f in EDGE["trusted_proxy"]["backend_settings"])]
    res = {}
    for src in srcs:
        rel = src.relative_to(REPO).as_posix()
        res[rel.removesuffix(".tmpl")] = render(src, tmp / "out" / rel.removesuffix(".tmpl"), env)
    return res


def _sub(text):
    """${KEY} -> its site.env.example value (what render.sh does), for contract strings."""
    return re.sub(r"\$\{(\w+)\}", lambda m: example_value(m.group(1)) or "digest.example.com", text)


def test_contract_header():
    assert EDGE["contract"] == "edge" and EDGE["contract_version"].startswith("1.")
    assert EDGE["trusted_proxy"]["key"] == "EDGE_WG_IP" and EDGE["backend_bind"] == "BACKEND_WG_IP"


@pytest.mark.parametrize("src", sorted(EDGE["trusted_proxy"]["backend_settings"]))
def test_backend_trusts_only_the_edge(out, src):
    setting = EDGE["trusted_proxy"]["backend_settings"][src]
    assert setting in (REPO / src).read_text(), src       # the template names the key, not a value
    assert _sub(setting) in out[src.removesuffix(".tmpl")], src


def test_every_edge_site_is_in_the_contract():
    contract_sites = {v["edge_site"] for v in EDGE["ports"].values()}
    proxied = {p.name.removesuffix(".tmpl") for p in SITES.glob("*.tmpl")
               if re.search(r"proxy_pass\s+https?://\$\{BACKEND_WG_IP\}", p.read_text())}
    assert proxied == contract_sites
    for key, v in EDGE["ports"].items():
        assert f"server_name ${{{v['host']}}};" in (SITES / f"{v['edge_site']}.tmpl").read_text(), key


@pytest.mark.parametrize("name", ["telemetry", "digest"])
def test_oauth2_proxy_clients(out, name):
    c = EDGE["oidc"]["clients"][name]
    cfg = out[c["relying_party"].removesuffix(".tmpl")]

    def val(k):
        return re.search(rf'^{k}\s*=\s*(.+)$', cfg, re.M).group(1).strip()
    assert val("client_id") == f'"{example_value(c["id_key"])}"'
    assert val("allowed_groups") == f'["{example_value(c["group_key"])}"]'
    assert val("redirect_url") == f'"{_sub(c["callback"])}"'
    assert val("http_address") == f'"{c["edge_listener"]}"'
    assert val("oidc_issuer_url") == f'"{_sub(EDGE["oidc"]["issuer"])}"'
    assert val("scope") == f'"{EDGE["oidc"]["scopes"]}"' and val("oidc_groups_claim") == f'"{EDGE["oidc"]["groups_claim"]}"'
    site = next(v["edge_site"] for v in EDGE["ports"].values() if v["stack"] == name)
    assert f"proxy_pass http://{c['edge_listener']};" in out[f"covenant/nginx/sites-available/{site}"]


def test_pocket_id_bootstrap_creates_the_contract_clients(out):
    boot = out["walter/webui/pocketid-bootstrap.py"]
    for name in ("open-webui", "telemetry"):
        c = EDGE["oidc"]["clients"][name]
        cid = c.get("id") or example_value(c["id_key"])
        assert re.search(rf'"id": "{re.escape(cid)}", "name": "[^"]+",\s*"callbackURLs": \["{re.escape(_sub(c["callback"]))}"\]',
                         boot), name
        assert f'{c["group_key"]} = "{example_value(c["group_key"])}"' in boot
    webui = out["walter/webui/.env.example"]
    assert f"OAUTH_CLIENT_ID={EDGE['oidc']['clients']['open-webui']['id']}" in webui
    assert f"OAUTH_SCOPES={EDGE['oidc']['scopes']}" in webui
    assert f"OPENID_PROVIDER_URL={_sub(EDGE['oidc']['issuer'])}/.well-known/openid-configuration" in webui


def _backend_locations(site_text):
    ip = re.escape(example_value("BACKEND_WG_IP"))
    blocks = re.findall(r"location[^{]*\{(.*?)\n    \}", site_text, re.S)
    return [b for b in blocks if re.search(rf"proxy_pass\s+https?://{ip}:", b)]


@pytest.mark.parametrize("site", EDGE["auth_headers"]["gated_sites"])
def test_gated_sites_set_identity_headers_only_from_auth_request(out, site):
    text = out[f"covenant/nginx/sites-available/{site}"]
    h = EDGE["auth_headers"]
    assert "auth_request_set $auth_user  $upstream_http_x_auth_request_user;" in text
    assert "auth_request_set $auth_email $upstream_http_x_auth_request_email;" in text
    locs = _backend_locations(text)
    assert locs, site
    for b in locs:
        assert "auth_request /oauth2/auth;" in b
        set_ = dict(re.findall(r"proxy_set_header\s+(X-[\w-]+)\s+(\S+);", b))
        assert set_.get(h["set"][0]) == "$auth_user" and set_.get(h["set"][1]) == "$auth_email", site
        for c in h["cleared"]:
            assert set_.get(c) == '""', (site, c)


def test_ungated_sites_add_no_identity_header(out):
    gated = set(EDGE["auth_headers"]["gated_sites"])
    names = EDGE["auth_headers"]["set"] + EDGE["auth_headers"]["cleared"]
    for v in EDGE["ports"].values():
        if v["edge_site"] in gated:
            continue
        text = out[f"covenant/nginx/sites-available/{v['edge_site']}"]
        assert not any(n in text for n in names), v["edge_site"]
