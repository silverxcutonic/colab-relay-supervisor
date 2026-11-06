import subprocess
import sys

print("[BACKUP] Starting Google Drive state backup via sync_state.sh...", flush=True)
try:
    res = subprocess.run(["bash", "/content/sync_state.sh"], capture_output=True, text=True)
    print(res.stdout, flush=True)
    if res.stderr:
        print(f"[STDERR] {res.stderr}", flush=True)
    print(f"[BACKUP] Backup script finished with exit code: {res.returncode}", flush=True)
    sys.exit(res.returncode)
except Exception as e:
    print(f"[BACKUP] Error running sync_state.sh: {e}", flush=True)
    sys.exit(1)
