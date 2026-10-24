"""scripts/render.sh and scripts/gen-secrets.sh: site-env lookup
-e/--site-env > $SITE_ENV > ~/.config/deck/site.env > <repo>/site.env, and a dangling
~/.config/deck/site.env is an error. HOME is a temp dir. Run:
    python3 -m pytest scripts/tests -q -p no:cacheprovider
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    shutil.copytree(REPO / "scripts", r / "scripts", ignore=shutil.ignore_patterns("tests", "__pycache__"))
    (r / "site.env.example").write_text("SPARK_DOMAIN=example.com\n")
    (r / "t.tmpl").write_text("${SPARK_DOMAIN}\n")
    (tmp_path / "home").mkdir()
    return r


def env(tmp_path, **extra):
    return {"PATH": os.environ["PATH"], "HOME": str(tmp_path / "home"), **extra}


def render(repo, e, *args):
    out = repo / "out.txt"
    r = subprocess.run(["bash", str(repo / "scripts/render.sh"), *args, str(repo / "t.tmpl"), str(out)],
                       env=e, capture_output=True, text=True, timeout=60)
    return r, (out.read_text().strip() if r.returncode == 0 else None)


def cfg(tmp_path):
    p = tmp_path / "home/.config/deck/site.env"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def test_render_order(repo, tmp_path):
    (repo / "site.env").write_text("SPARK_DOMAIN=root.example.org\n")
    assert render(repo, env(tmp_path))[1] == "root.example.org"
    cfg(tmp_path).write_text("SPARK_DOMAIN=config.example.org\n")
    assert render(repo, env(tmp_path))[1] == "config.example.org"
    se = tmp_path / "se.env"
    se.write_text("SPARK_DOMAIN=var.example.org\n")
    assert render(repo, env(tmp_path, SITE_ENV=str(se)))[1] == "var.example.org"
    fl = tmp_path / "fl.env"
    fl.write_text("SPARK_DOMAIN=flag.example.org\n")
    assert render(repo, env(tmp_path, SITE_ENV=str(se)), "-e", str(fl))[1] == "flag.example.org"


def test_render_dangling_config_is_an_error_unless_e(repo, tmp_path):
    (repo / "site.env").write_text("SPARK_DOMAIN=root.example.org\n")
    cfg(tmp_path).symlink_to(tmp_path / "gone.env")
    r, _ = render(repo, env(tmp_path))
    assert r.returncode != 0 and "dangling" in r.stderr
    assert render(repo, env(tmp_path), "-e", str(repo / "site.env"))[1] == "root.example.org"


def test_render_vars_union_includes_site_only_names(repo, tmp_path):
    cfg(tmp_path).write_text("SPARK_DOMAIN=config.example.org\nONLY_IN_SITE=x\n")
    r = subprocess.run(["bash", str(repo / "scripts/render.sh"), "--vars"], env=env(tmp_path),
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "ONLY_IN_SITE" in r.stdout.split()


def gen_covenant(repo, e, *args):
    root = repo.parent / "root"
    return subprocess.run(["bash", str(repo / "scripts/gen-secrets.sh"), "covenant", "--dry-run",
                           "--destdir", str(root), *args], env=e, capture_output=True, text=True,
                          timeout=60)


def test_gen_secrets_reads_digest_gate_by_lookup_order(repo, tmp_path):
    r = gen_covenant(repo, env(tmp_path))
    assert r.returncode == 0 and "oauth2-proxy-digest" not in r.stderr      # no site env: digest off
    cfg(tmp_path).write_text('SPARK_DIGEST_HOST="digest.example.com"\n')
    r = gen_covenant(repo, env(tmp_path))
    assert r.returncode == 0 and "oauth2-proxy-digest" in r.stderr
    off = tmp_path / "off.env"
    off.write_text('SPARK_DIGEST_HOST=""\n')
    assert "oauth2-proxy-digest" not in gen_covenant(repo, env(tmp_path), "--site-env", str(off)).stderr
    assert "oauth2-proxy-digest" not in gen_covenant(repo, env(tmp_path, SITE_ENV=str(off))).stderr


def test_gen_secrets_dangling_config_is_exit_2(repo, tmp_path):
    cfg(tmp_path).symlink_to(tmp_path / "gone.env")
    r = gen_covenant(repo, env(tmp_path))
    assert r.returncode == 2 and "dangling" in r.stderr
