#!/usr/bin/env bash
# Install Ollama as a *user* service (no sudo), pinned to the NVIDIA GPU, and pull the model.
# Safe to re-run. Needs: NVIDIA driver installed, `loginctl enable-linger $USER` (so it runs without a login).
set -euo pipefail
VERSION="${OLLAMA_VERSION:-v0.40.17}"
MODEL="${OLLAMA_MODEL:-qwen3:4b-instruct}"
DEST="$HOME/.local/ollama"
HERE="$(cd "$(dirname "$0")" && pwd)"

command -v nvidia-smi >/dev/null || { echo "nvidia-smi not found: install the NVIDIA driver first."; exit 1; }
command -v zstd >/dev/null || { echo "zstd not found: sudo apt install zstd"; exit 1; }
[[ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" == yes ]] || \
  echo "NOTE: run 'sudo loginctl enable-linger $USER' so Ollama starts at boot without a login."

if [[ ! -x "$DEST/bin/ollama" ]] || ! "$DEST/bin/ollama" --version 2>/dev/null | grep -q "${VERSION#v}"; then
  echo "Downloading Ollama $VERSION (~1.6GB)..."
  tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
  curl -fL --progress-bar -o "$tmp/ollama.tar.zst" \
    "https://github.com/ollama/ollama/releases/download/$VERSION/ollama-linux-amd64.tar.zst"
  rm -rf "$DEST"; mkdir -p "$DEST"
  tar --zstd -xf "$tmp/ollama.tar.zst" -C "$DEST"
fi
# Never let Ollama touch an Intel/AMD iGPU (Plex uses Quick Sync): remove the Vulkan backend.
if [[ -d "$DEST/lib/ollama/vulkan" ]]; then
  mv "$DEST/lib/ollama/vulkan" "$DEST/lib/ollama/vulkan.disabled-plex-igpu"
fi
mkdir -p "$DEST/models" "$HOME/.config/systemd/user"
install -m 644 "$HERE/ollama.service" "$HOME/.config/systemd/user/ollama.service"
systemctl --user daemon-reload
systemctl --user enable --now ollama.service
for _ in $(seq 1 30); do curl -sf http://127.0.0.1:11434/api/tags >/dev/null && break; sleep 1; done
"$DEST/bin/ollama" pull "$MODEL"
echo "Ollama ready: $("$DEST/bin/ollama" --version | tail -1), model $MODEL"
