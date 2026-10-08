#!/usr/bin/env bash
# scripts/setup-self-hosted-runner.sh
# Minimal runner environment preparation and security configuration guide for HAFlow.

set -euo pipefail

RUNNER_DIR="${RUNNER_DIR:-$HOME/actions-runner-haflow}"
REPO_SLUG="${REPO_SLUG:-allinai0506/HAFlow}"

echo "============================================================"
echo "HAFlow Self-hosted Runner Minimal Environment Preparation"
echo "============================================================"
echo "Target directory: $RUNNER_DIR"
echo "Repository:       $REPO_SLUG"
echo ""

# 1. Security Preflight Check
echo "[1/4] Checking security isolation boundaries..."
if [ -d "$HOME/.ssh" ] && [ "$RUNNER_DIR" = "$HOME" ]; then
  echo "ERROR: Running a self-hosted runner directly in the developer home directory" >&2
  echo "violates HAFlow security policy (Section IV: no private developer credentials)." >&2
  echo "Please specify a dedicated directory (e.g. RUNNER_DIR=/opt/actions-runner) or run inside an isolated container." >&2
  exit 1
fi
echo "✓ Security boundary verified: isolated runner directory."

# 2. Detect OS & Architecture
echo "[2/4] Detecting platform architecture..."
OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
ARCH="$(uname -m)"

case "$OS" in
  darwin)
    RUNNER_OS="osx"
    ;;
  linux)
    RUNNER_OS="linux"
    ;;
  *)
    echo "Unsupported OS: $OS" >&2
    exit 1
    ;;
esac

case "$ARCH" in
  arm64|aarch64)
    RUNNER_ARCH="arm64"
    ;;
  x86_64)
    RUNNER_ARCH="x64"
    ;;
  *)
    echo "Unsupported ARCH: $ARCH" >&2
    exit 1
    ;;
esac

echo "✓ Platform: $RUNNER_OS-$RUNNER_ARCH"

# 3. Download Runner Package
RUNNER_VERSION="2.337.0"
TAR_NAME="actions-runner-${RUNNER_OS}-${RUNNER_ARCH}-${RUNNER_VERSION}.tar.gz"
DOWNLOAD_URL="https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/${TAR_NAME}"

mkdir -p "$RUNNER_DIR"
cd "$RUNNER_DIR"

if [ ! -f "config.sh" ]; then
  echo "[3/4] Downloading Actions Runner package from $DOWNLOAD_URL..."
  if command -v curl >/dev/null 2>&1; then
    curl -o "$TAR_NAME" -L "$DOWNLOAD_URL"
  else
    wget -O "$TAR_NAME" "$DOWNLOAD_URL"
  fi
  tar xzf "$TAR_NAME"
  rm -f "$TAR_NAME"
  echo "✓ Runner package extracted into $RUNNER_DIR"
else
  echo "[3/4] Runner already extracted at $RUNNER_DIR"
fi

# 4. Configuration Instructions
echo "[4/4] Registration & Execution Guidelines:"
echo ""
echo "To register this runner with GitHub, obtain a registration token via GitHub CLI:"
echo "  TOKEN=\$(gh api -X POST repos/${REPO_SLUG}/actions/runners/registration-token --jq .token)"
echo ""
echo "Then configure and run:"
echo "  ./config.sh --url https://github.com/${REPO_SLUG} --token \$TOKEN --labels self-hosted,${RUNNER_OS},${RUNNER_ARCH},haflow"
echo "  ./run.sh"
echo ""
echo "SECURITY NOTICE:"
echo "1. Do NOT run this runner on developer workstations with sensitive SSH keys or API tokens."
echo "2. For LLM Shadow Review with 'agy', run in an isolated container/sandbox with HERDR_SECURE_LLM_RUNNER=1."
echo "3. Without verified isolation, AI shadow review is safely skipped (shadow_skipped) to protect security."
echo "============================================================"
