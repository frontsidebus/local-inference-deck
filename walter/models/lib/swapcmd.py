"""The llama-swap model entries of a config, reduced to what the eval gate records as serving identity.

``spark-model-agent serving`` runs this on the LIVE /etc/llama-swap/config.yaml, so the gate records the cmd that
actually serves, not the repo template. The parse is the same as evals/run.py's ``llama_swap_entry`` (the same
regexes, macro expansion and flag list; walter/tests/test_spark_model_serving.py keeps the two in step), so a cmd
hashes the same whichever side reads it. Only hashes, ids, aliases and a few server flags leave Walter: the cmd
itself can hold paths and site values. Stdlib only.
"""
import hashlib
import re

# evals/run.py: ("-c", "-n", "-sm", "-m", *SERVER_FLAG_FOR.values())
FLAGS = ("-c", "-n", "-sm", "-m", "--temp", "--top-p", "--top-k", "--min-p", "--presence-penalty",
         "--frequency-penalty", "--repeat-penalty", "--dry-multiplier", "--xtc-probability")


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def entries(text: str) -> dict:
    """model id -> {"aliases", "name", "cmd_sha256", "server_flags"} for every model in a llama-swap config."""
    macros = {}
    msec = re.search(r"^macros:\n(.*?)^\S", text, re.S | re.M)
    if msec:
        for k, v in re.findall(r'^\s+"([\w-]+)":\s*"(.*)"\s*$', msec.group(1), re.M):
            macros[k] = v
    mods = re.search(r"^models:\n(.*?)(?=^\S|\Z)", text, re.S | re.M)
    out = {}
    if not mods:
        return out
    for m in re.finditer(r'^  "([^"]+)":\n((?:^(?:    .*|\s*)\n)+)', mods.group(1), re.M):
        mid, block = m.group(1), m.group(2)
        al = re.search(r"aliases:\s*\[(.*?)\]", block)
        aliases = re.findall(r'"([^"]+)"', al.group(1)) if al else []
        cm = re.search(r"^    cmd: \|\n((?:^      .*\n)+)", block, re.M)
        cmd = " ".join(ln.strip() for ln in cm.group(1).splitlines()) if cm else ""
        for _ in range(3):
            cmd = re.sub(r"\$\{([\w-]+)\}", lambda x: macros.get(x.group(1), x.group(0)), cmd)
        flags = {}
        for flag in FLAGS:
            fm = re.search(rf"(?:^|\s){re.escape(flag)}\s+(\S+)", cmd)
            if fm:
                flags[flag] = fm.group(1)
        if "-m" in flags:  # the basename only, as run.py records it
            flags["-m"] = flags["-m"].rsplit("/", 1)[-1]
        nm = re.search(r'name:\s*"([^"]+)"', block)
        out[mid] = {"aliases": aliases, "name": nm.group(1) if nm else mid, "cmd_sha256": sha256_text(cmd),
                    "server_flags": flags}
    return out
