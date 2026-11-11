#!/usr/bin/env bash
# CPU model server for LOG_ON (OpenAI-compatible): llama.cpp `llama-server` + Qwen2.5 Instruct GGUF (Q4_K_M).
# Model weights go to ~/.cache/log_on_models and are never committed.
#   scripts/serve_llamacpp.sh [0.5b|1.5b] [port]
# then: LOG_ON_LLM_PROVIDER=llamacpp LOG_ON_LLM_BASE_URL=http://127.0.0.1:<port>/v1 LOG_ON_LLM_MODEL=qwen2.5-<size>-instruct
set -euo pipefail
SIZE="${1:-1.5b}"; PORT="${2:-8082}"; BUILD="${LLAMA_CPP_BUILD:-b12283}"
BIN_DIR="$HOME/.local/llamacpp/llama-$BUILD"; MODELS="$HOME/.cache/log_on_models"
MODEL="$MODELS/qwen2.5-$SIZE-instruct-q4_k_m.gguf"
mkdir -p "$MODELS" "$HOME/.local/llamacpp"
if [ ! -x "$BIN_DIR/llama-server" ]; then
  curl -sL -o /tmp/llama.tgz "https://github.com/ggml-org/llama.cpp/releases/download/$BUILD/llama-$BUILD-bin-ubuntu-x64.tar.gz"
  tar xzf /tmp/llama.tgz -C "$HOME/.local/llamacpp"
fi
[ -f "$MODEL" ] || curl -sL -o "$MODEL" \
  "https://huggingface.co/Qwen/Qwen2.5-${SIZE^^}-Instruct-GGUF/resolve/main/qwen2.5-$SIZE-instruct-q4_k_m.gguf"
# -np 4: four parallel decoding slots (continuous batching); -c is split across slots (16384 -> 4096 each,
# enough for DSPy prompts carrying 8 few-shot demos). Override with LOG_ON_CTX.
exec "$BIN_DIR/llama-server" -m "$MODEL" --host 127.0.0.1 --port "$PORT" -c "${LOG_ON_CTX:-16384}" -np 4 -t "$(nproc --ignore=2)" \
  --alias "qwen2.5-$SIZE-instruct"
