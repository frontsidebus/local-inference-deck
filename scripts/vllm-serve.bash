#!/usr/bin/env bash
set -euo pipefail

MODEL="${1:-Qwen/Qwen2.5-7B-Instruct}"
HOST="10.100.0.2"
PORT="8000"

# kill any existing vllm
pkill -f "vllm serve" || true
sleep 2

echo "Starting vllm with model: $MODEL"
exec vllm serve "$MODEL" \
  --host "$HOST" \
  --port "$PORT" \
  --enable-auto-tool-choice \
  --tool-call-parser hermes
