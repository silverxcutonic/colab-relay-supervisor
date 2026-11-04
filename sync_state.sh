#!/usr/bin/env bash
# ==============================================================================
# Google Colab State Backup & Google Drive Sync Script
# High-Speed Archiving of /content/workspace, .claude sessions, and configurations
# Fast: Syncs in ~15 seconds with 32M chunk sizes to prevent GDrive API rate limits
# ==============================================================================
set -euo pipefail

PRIMARY_STORAGE_TARGET="gdrive:coolify-relay-state/colab-relay-state"
STORAGE_TARGET="${1:-${STORAGE_TARGET:-$PRIMARY_STORAGE_TARGET}}"
STAGE_DIR="/tmp/colab_stage"
WORKSPACE_DIR="/content/workspace"
CLAUDE_DIR="/root/.claude"
LOCAL_ARCHIVE="$STAGE_DIR/colab_bundle.tar.gz"

echo "[SYNC] === Initiating Google Colab State & Chat Backup ==="
mkdir -p "$STAGE_DIR"

# 1. Clean scratch / cache files before packing
find "$WORKSPACE_DIR" -type d \( -name "__pycache__" -o -name ".pytest_cache" \) -prune -exec rm -rf {} + 2>/dev/null || true

# 2. Guard: Never overwrite cloud state with an empty workspace
WORKSPACE_FILES=$(find "$WORKSPACE_DIR" -mindepth 1 2>/dev/null | wc -l || echo 0)
if [ "$WORKSPACE_FILES" -eq 0 ]; then
  echo "[SYNC] Guard Triggered: Workspace is empty. Skipping upload to protect remote state."
  exit 0
fi

# 3. Ensure persistent claude transcripts are safely housed in workspace
echo "[SYNC] Securing persistent chat sessions and memory..."
PERSISTENT_CLAUDE="$WORKSPACE_DIR/.claude_transcripts"
mkdir -p "$PERSISTENT_CLAUDE"

# If /root/.claude is a real directory and not yet symlinked, migrate its files
if [ -d "$CLAUDE_DIR" ] && [ ! -L "$CLAUDE_DIR" ]; then
  cp -a "$CLAUDE_DIR"/. "$PERSISTENT_CLAUDE"/ 2>/dev/null || true
  mv "$CLAUDE_DIR" "${CLAUDE_DIR}.bak" 2>/dev/null || true
  ln -sfn "$PERSISTENT_CLAUDE" "$CLAUDE_DIR"
fi

# Ensure /root/.claude symlink exists
if [ ! -e "$CLAUDE_DIR" ]; then
  ln -sfn "$PERSISTENT_CLAUDE" "$CLAUDE_DIR"
fi

echo "[SYNC] Packaging /content/workspace (including persistent .claude_transcripts)..."
TARGETS_TO_PACK=("workspace")

TAR_EXCLUDES=(
  --exclude='workspace/.suwayomi_state/bin'
  --exclude='workspace/.suwayomi_state/cache'
  --exclude='workspace/-config-workspace'
  --exclude='workspace/backups'
  --exclude='workspace/*.zip'
  --exclude='workspace/*.tar.gz'
  --exclude='*.zip'
  --exclude='*.tar.gz'
  --exclude='*.pyc'
  --exclude='__pycache__'
  --exclude='*.pytest_cache'
  --exclude='*.sock'
  --exclude='*.log'
  --exclude='workspace/*/logs/*'
  --exclude='workspace/manga_narrator_test/audio_cache/*'
  --exclude='workspace/*/cloudflared'
)

if command -v pigz >/dev/null 2>&1; then
  tar -cpf - -C /content --warning=no-file-changed --warning=no-file-removed "${TAR_EXCLUDES[@]}" "${TARGETS_TO_PACK[@]}" | pigz -p 4 -1 > "$LOCAL_ARCHIVE"
else
  tar -czpf "$LOCAL_ARCHIVE" -C /content --warning=no-file-changed --warning=no-file-removed "${TAR_EXCLUDES[@]}" "${TARGETS_TO_PACK[@]}"
fi

ARCHIVE_SIZE=$(du -sh "$LOCAL_ARCHIVE" | cut -f1)
echo "[SYNC] Archive created ($ARCHIVE_SIZE). Uploading to $STORAGE_TARGET/colab_bundle.tar.gz..."

# 4. Upload to Google Drive with 32M buffer/chunk discipline
if ! command -v rclone >/dev/null 2>&1; then
  echo "[SYNC] Error: rclone is not installed. State bundle saved locally at $LOCAL_ARCHIVE."
  exit 1
fi

if [ ! -f "/root/.config/rclone/rclone.conf" ] && [ ! -f "/content/rclone.conf" ] && [ -z "${RCLONE_CONFIG:-}" ]; then
  echo "[SYNC] Error: No rclone.conf found. State bundle saved locally at $LOCAL_ARCHIVE."
  exit 1
fi

rclone copyto "$LOCAL_ARCHIVE" "$STORAGE_TARGET/colab_bundle.tar.gz" \
  --drive-chunk-size=32M \
  --drive-upload-cutoff=32M \
  --drive-use-trash=false \
  --buffer-size=32M \
  --use-mmap \
  --retries=5 \
  --timeout=5m

rm -f "$LOCAL_ARCHIVE"
echo "[SYNC] Google Colab state backup completed successfully ($ARCHIVE_SIZE)."
