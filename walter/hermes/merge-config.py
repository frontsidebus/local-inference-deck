#!/usr/bin/env python3
"""merge-config.py FRAGMENT CONFIG -- merge a Hermes config fragment into config.yaml.

Top-level keys of FRAGMENT replace the same keys in CONFIG, except `security`, whose
sub-keys are merged. Run with the Hermes venv python (it has PyYAML).
Exit 0 = merged (CONFIG rewritten, backup CONFIG.bak-<ts>), 10 = already up to date.
Comments in CONFIG are not preserved by a rewrite.
"""
import os
import shutil
import sys
import time

import yaml

frag_path, cfg_path = sys.argv[1], sys.argv[2]
frag = yaml.safe_load(open(frag_path)) or {}
cfg = yaml.safe_load(open(cfg_path)) or {}
new = dict(cfg)
for k, v in frag.items():
    if k == "security" and isinstance(v, dict):
        new[k] = dict(cfg.get(k) or {}, **v)
    else:
        new[k] = v
if new == cfg:
    sys.exit(10)
bak = "%s.bak-%s" % (cfg_path, time.strftime("%Y%m%d-%H%M%S"))
shutil.copy2(cfg_path, bak)
st = os.stat(cfg_path)
tmp = cfg_path + ".merge-tmp"
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    yaml.safe_dump(new, f, sort_keys=False, allow_unicode=True)
os.chown(tmp, st.st_uid, st.st_gid)
os.replace(tmp, cfg_path)
print("merged; backup " + bak)
