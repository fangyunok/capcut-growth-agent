#!/usr/bin/env bash
# Run on the rented AutoDL instance. The application and source cards stay local.
set -euo pipefail

model="${GROWTH_QWEN_MODEL:-qwen3:4b-instruct}"
log_dir="/root/autodl-tmp/capcut-growth-agent"
mkdir -p "$log_dir"

echo "GPU:"
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader

if ! command -v zstd >/dev/null 2>&1; then
  echo "Installing zstd required by the Ollama installer..."
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y zstd
fi

if ! command -v ollama >/dev/null 2>&1; then
  echo "Installing Ollama from its official Linux installer..."
  curl -fsSL https://ollama.com/install.sh | sh
fi

if ! curl -fsS --max-time 3 http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
  echo "Starting Ollama on the instance loopback interface..."
  nohup env OLLAMA_HOST=127.0.0.1:11434 OLLAMA_CONTEXT_LENGTH=8192 \
    ollama serve >"$log_dir/ollama.log" 2>&1 </dev/null &
  for attempt in $(seq 1 30); do
    if curl -fsS --max-time 3 http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done
fi

curl -fsS --max-time 3 http://127.0.0.1:11434/api/tags >/dev/null
echo "Pulling $model..."
ollama pull "$model"

echo "Installed models:"
curl -fsS http://127.0.0.1:11434/v1/models
echo
echo "Model endpoint is ready at http://127.0.0.1:11434/v1"
