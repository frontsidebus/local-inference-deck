"""Shared helpers for the deck's contract tests (contracts/tests). Offline; renders with site.env.example only."""
import json
import re
import subprocess
import sys
from pathlib import Path

sys.dont_write_bytecode = True
REPO = Path(__file__).resolve().parents[2]
CONTRACTS = REPO / "contracts"
EXAMPLE = REPO / "site.env.example"
RENDER = REPO / "scripts" / "render.sh"
sys.path.insert(0, str(CONTRACTS / "tools"))
import contracts as TOOL  # noqa: E402,F401  (contracts/tools/contracts.py)


def load_yaml(path):
    import yaml
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def example_value(key):
    m = re.search(rf"^{key}=([^\s#]*)", EXAMPLE.read_text(), re.M)
    return m.group(1).strip('"') if m else None


def example_env(tmp: Path, digest=True) -> Path:
    """site.env.example, with the optional digest app turned on (a fake example host) when asked."""
    text = EXAMPLE.read_text()
    if digest:
        text = re.sub(r"^SPARK_DIGEST_HOST=.*$", 'SPARK_DIGEST_HOST="digest.example.com"', text, flags=re.M)
    env = tmp / "site.env"
    env.write_text(text)
    return env


def render(src: Path, dst: Path, env: Path) -> str:
    dst.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["bash", str(RENDER), "-e", str(env), str(src), str(dst)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"render {src}: {r.stderr}"
    return dst.read_text()
