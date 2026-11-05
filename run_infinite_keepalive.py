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

APP_DIR = os.path.dirname(os.path.abspath(__file__))

def log(msg):
    # Indian Standard Time (IST = UTC + 5:30)
    ist_time = time.gmtime(time.time() + 19800)
    now = time.strftime("%Y-%m-%d %I:%M:%S %p IST", ist_time)
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
        name = "ping_a.py" if (cycle % 2 != 0) else "ping_b.py"
        script_path = os.path.join(APP_DIR, name)
        start = time.time()

        try:
            r = subprocess.run(
                [VENV_COLAB, "exec", "-s", SESSION_NAME, "-f", script_path, "--timeout", "70"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=75
            )
            elapsed = time.time() - start
            out_summary = r.stdout.strip().replace("\n", " | ")[:120]
            log(f"[Cycle {cycle:04d}] {name} exit={r.returncode} in {elapsed:.1f}s | {out_summary}")
        except subprocess.TimeoutExpired:
            elapsed = time.time() - start
            log(f"WARNING: Keepalive cycle {cycle:04d} timed out after {elapsed:.1f}s; cleaning hanging child processes...")
            subprocess.run(["pkill", "-f", f"colab exec -s {SESSION_NAME}"], check=False)
        except Exception as e:
            elapsed = time.time() - start
            log(f"ERROR: Exception executing {name}: {e} in {elapsed:.1f}s")

        spent = time.time() - start
        to_sleep = max(1.0, 50.0 - spent)
        time.sleep(to_sleep)

if __name__ == "__main__":
    main()
