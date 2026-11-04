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

# 3. Packaging workspace and persistent claude history
echo "[SYNC] Packaging /content/workspace and persistent chat sessions..."
TARGETS_TO_PACK=()
[ -d "$WORKSPACE_DIR" ] && TARGETS_TO_PACK+=("workspace")
[ -d "$CLAUDE_DIR" ] && TARGETS_TO_PACK+=("root_claude")

# Create temporary symlink for root_claude if not packed from /content
if [ -d "$CLAUDE_DIR" ] && [ ! -L "$CLAUDE_DIR" ]; then
  ln -sfn "$CLAUDE_DIR" "/content/root_claude"
fi

TAR_EXCLUDES=(
  --exclude='workspace/.suwayomi_state/bin'
  --exclude='workspace/.suwayomi_state/cache'
  --exclude='workspace/backups'
  --exclude='workspace/Master_Backup_Bundle.zip'
  --exclude='workspace/Core_Backup_Bundle.zip'
  --exclude='*.pyc'
  --exclude='__pycache__'
  --exclude='*.pytest_cache'
  --exclude='*.sock'
  --exclude='workspace/amazon_loot_grabber/logs/*'
  --exclude='workspace/Telegram_S3_Gateway_Bot/logs/*'
  --exclude='workspace/Stream_Vault/logs/*'
  --exclude='workspace/cai_tts_bundle/logs/*'
  --exclude='workspace/code_server_bot/logs/*'
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
rclone copyto "$LOCAL_ARCHIVE" "$STORAGE_TARGET/colab_bundle.tar.gz" \
  --drive-chunk-size=32M \
  --drive-upload-cutoff=32M \
  --drive-use-trash=false \
  --buffer-size=32M \
  --use-mmap \
  --retries=5 \
  --timeout=5m

echo "[SYNC] Google Colab state backup completed successfully ($ARCHIVE_SIZE)."
