#!/bin/sh
# Used by llama-swap model cmds: spark-docker-run.sh <container-name> <docker run args...>
# On a config reload llama-swap starts the new container while the old one with the same name
# is still being removed; plain `docker run --name` then fails with a name conflict.
# Wait (max 30 s) for the name to be free, then exec docker run so llama-swap tracks this PID.
name="$1"; shift
i=0
while docker container inspect "$name" >/dev/null 2>&1 && [ $i -lt 60 ]; do sleep 0.5; i=$((i+1)); done
exec docker run --name "$name" "$@"
