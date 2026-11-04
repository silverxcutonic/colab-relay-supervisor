#!/usr/bin/env python3
"""
Perpetual Robust Keepalive Daemon for Google Colab Sessions
- Alternates ping_a and ping_b compute ticks via direct stdin kernel execution
- 40 seconds active duty cycle per 50-second interval
- Runs indefinitely to keep Jupyter kernel in BUSY state and defeat idle reapers
"""
import os
import sys
import time
import shutil
import subprocess

SESSION_NAME = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("COLAB_SESSION", "colab-prod-node1")
VENV_COLAB = os.environ.get("VENV_COLAB") or shutil.which("colab") or "/home/runner/workspace/.colab-venv/bin/colab"
LOG_FILE = os.environ.get("KEEPALIVE_LOG", "/tmp/colab_keepalive.log")

PING_CODE_A = """
import time, sys
print("[PING-A] Starting 40s duty cycle...", flush=True)
for i in range(8):
    time.sleep(5)
    print(f"[PING-A] Active tick {i+1}/8 | Kernel BUSY", flush=True)
print("[PING-A] Finished duty cycle.", flush=True)
"""

PING_CODE_B = """
import time, sys
print("[PING-B] Starting 40s duty cycle...", flush=True)
for i in range(8):
    time.sleep(5)
    print(f"[PING-B] Active tick {i+1}/8 | Kernel BUSY", flush=True)
print("[PING-B] Finished duty cycle.", flush=True)
"""

def log(msg):
    now = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    entry = f"[{now}] {msg}\n"
    sys.stdout.write(entry)
    sys.stdout.flush()
    try:
        with open(LOG_FILE, "a") as f:
            f.write(entry)
    except Exception:
        pass

def main():
    log(f"=== Starting Perpetual Colab Keepalive Daemon for '{SESSION_NAME}' ===")
    log(f"Colab CLI binary: {VENV_COLAB}")
    cycle = 0

    while True:
        cycle += 1
        name = "PING-A" if (cycle % 2 != 0) else "PING-B"
        code = PING_CODE_A if (cycle % 2 != 0) else PING_CODE_B
        start = time.time()

        try:
            r = subprocess.run(
                [VENV_COLAB, "exec", "-s", SESSION_NAME, "--timeout", "70"],
                input=code,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT
            )
            elapsed = time.time() - start
            out_summary = r.stdout.strip().replace("\n", " | ")[:120]
            log(f"[Cycle {cycle:04d}] {name} exit={r.returncode} in {elapsed:.1f}s | {out_summary}")
        except Exception as e:
            elapsed = time.time() - start
            log(f"ERROR: Exception executing {name}: {e} in {elapsed:.1f}s")

        spent = time.time() - start
        to_sleep = max(1.0, 50.0 - spent)
        time.sleep(to_sleep)

if __name__ == "__main__":
    main()
