#!/usr/bin/env bash
# One-time setup on macOS: Homebrew, ffmpeg, uv, gh, Python 3.12 venv, dependencies.
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v brew >/dev/null 2>&1; then
  echo "Installing Homebrew (asks for your password)..."
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  eval "$(/opt/homebrew/bin/brew shellenv 2>/dev/null || /usr/local/bin/brew shellenv)"
fi

brew install ffmpeg uv gh

uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e ".[dev]"

[ -f .env ] || cp .env.example .env
mkdir -p secrets data

echo
echo "Done. Next:"
echo "  1. Put your Anthropic API key in .env"
echo "  2. source .venv/bin/activate && clipper doctor"
