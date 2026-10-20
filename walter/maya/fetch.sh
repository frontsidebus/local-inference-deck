#!/usr/bin/env bash
# fetch.sh -- download the Maya lab models' files, pinned by commit and checked against their sha256.
# Run on Walter as the models owner (operator), not root, under nohup (95 GB is about an hour or more):
#   nohup walter/maya/fetch.sh glm >> /models/logs/maya-fetch.log 2>&1 &
#   walter/maya/fetch.sh --list
# Each file downloads to <name>.part (resumable: curl -C -), is checked for its size and sha256 (the Hugging Face LFS
# hash, which is also the one in Maya's maya.py MODELS manifest), and only then gets its real name. A mismatch moves the
# .part to /models/quarantine. Uses $HF_TOKEN or ~/.cache/huggingface/token if present (the repos are not gated).
set -u
umask 022   # world-readable: the Maya container runs as uid 10010
DEST=/models/gguf/maya
QUAR=/models/quarantine

# model | HF repo@commit | file (path in the repo) | dest subdir | bytes | sha256
FILES='
glm|peasantsmith/GLM-5.3-Flash-Maya-GGUF@f16ee4ba50d8fd2bb726ea1862ca6ca14ba188da|Maya-S24/GLM-5.3-Flash-Maya-S24-IQ2_XXS_S-00001-of-00003.gguf|glm-5.3-flash/Maya-S24|46886573024|8ebd5c74bcf65ece2a8699b7b6fd2b8d177f350ed252cee252fdd99b86a0138c
glm|peasantsmith/GLM-5.3-Flash-Maya-GGUF@f16ee4ba50d8fd2bb726ea1862ca6ca14ba188da|Maya-S24/GLM-5.3-Flash-Maya-S24-IQ2_XXS_S-00002-of-00003.gguf|glm-5.3-flash/Maya-S24|46982144736|4bd445da3a0128a9c5b32228c924e0a622aa4a132c7a8dc9a2c207a4beea8e34
glm|peasantsmith/GLM-5.3-Flash-Maya-GGUF@f16ee4ba50d8fd2bb726ea1862ca6ca14ba188da|Maya-S24/GLM-5.3-Flash-Maya-S24-IQ2_XXS_S-00003-of-00003.gguf|glm-5.3-flash/Maya-S24|832958464|a6fd4e88007ac49b431c7d02be34e43ab7a09224d53a0c52782327613cb41917
'

TOK=${HF_TOKEN:-}
[[ -z $TOK && -r ~/.cache/huggingface/token ]] && TOK=$(cat ~/.cache/huggingface/token)
AUTH=()
[[ -n $TOK ]] && AUTH=(-H "Authorization: Bearer $TOK")

fetch() { # repo@rev file dir size sha256
  local repo=${1%@*} rev=${1#*@} file=$2 dir=$DEST/$3 size=$4 sha=$5 out part got
  mkdir -p "$dir"; out=$dir/$(basename "$file"); part=$out.part
  if [[ -f $out ]]; then echo "OK (present) $out"; return 0; fi
  for try in 1 2 3 4 5; do
    curl -fL --retry 5 -C - "${AUTH[@]}" -o "$part" -s -S "https://huggingface.co/$repo/resolve/$rev/$file" && break
    echo "RETRY $try $out"; sleep 10
  done
  got=$(stat -c %s "$part" 2>/dev/null || echo 0)
  if [[ $got != "$size" ]]; then echo "FAIL $out: $got bytes, expected $size"; return 1; fi
  echo "checking sha256 of $out ..."
  if [[ $(sha256sum "$part" | cut -d' ' -f1) != "$sha" ]]; then
    mkdir -p "$QUAR"; mv "$part" "$QUAR/"; echo "FAIL $out: sha256 mismatch, moved to $QUAR"; return 1
  fi
  mv "$part" "$out"; echo "OK $out $size sha256 ok"
}

if [[ ${1:-} == --list ]]; then
  printf '%s\n' "$FILES" | sed '/^$/d' | cut -d'|' -f1,3,5 | column -t -s'|'; exit 0
fi
[[ $EUID -ne 0 ]] || { echo "run as the models owner, not root" >&2; exit 1; }
[[ $# -gt 0 ]] || { echo "usage: $0 <model...> | --list" >&2; exit 2; }
want=" $* "
rc=0
echo "== $(date -Is) fetch $*"
while IFS='|' read -r model repo file dir size sha; do
  [[ -n $model && $want == *" $model "* ]] || continue
  fetch "$repo" "$file" "$dir" "$size" "$sha" || rc=1
done <<< "$FILES"
echo "== $(date -Is) done rc=$rc"
exit $rc
