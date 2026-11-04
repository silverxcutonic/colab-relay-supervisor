#!/usr/bin/env python3
"""
Perpetual Robust Dual-Shield Keepalive Daemon for Google Colab Sessions
- Shield 1: Alternates ping_a and ping_b compute ticks via direct stdin kernel execution
  (40s active duty cycle per 50s interval keeps Jupyter kernel BUSY and defeats in-VM 11m 40s reaper)
- Shield 2: Periodic TFE web keep-alive pings to colab.research.google.com/tun/m/{endpoint}/keep-alive/
  (defeats Google control-plane ~2.5-3h unmonitored headless eviction reaper)
"""
import os
import sys
import time
import json
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

def get_endpoint_for_session(session_name):
    cfg = os.path.expanduser("~/.config/colab-cli/sessions.json")
    if os.path.exists(cfg):
        try:
            with open(cfg) as f:
                data = json.load(f)
            if session_name in data:
                return data[session_name].get("endpoint")
        except Exception:
            pass
    return None

def send_tfe_keepalive(endpoint):
    if not endpoint:
        return "NO_ENDPOINT"
    try:
        from colab_cli.common import state
        url = f"https://colab.research.google.com/tun/m/{endpoint}/keep-alive/?authuser=0"
        headers = {"X-Colab-Tunnel": "Google"}
        resp = state.client.session.get(url, headers=headers, timeout=15)
        if resp.status_code == 200:
            return "HTTP 200 OK"
        return f"HTTP {resp.status_code}"
    except Exception as e:
        return f"ERR:{e}"

def main():
    log(f"=== Starting Perpetual Colab Dual-Shield Keepalive Daemon for '{SESSION_NAME}' ===")
    log(f"Colab CLI binary: {VENV_COLAB}")
    cycle = 0

    while True:
        cycle += 1
        name = "PING-A" if (cycle % 2 != 0) else "PING-B"
        code = PING_CODE_A if (cycle % 2 != 0) else PING_CODE_B
        start = time.time()

        endpoint = get_endpoint_for_session(SESSION_NAME)
        tfe_status = send_tfe_keepalive(endpoint)

        try:
            r = subprocess.run(
                [VENV_COLAB, "exec", "-s", SESSION_NAME, "--timeout", "70"],
                input=code,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=75
            )
            elapsed = time.time() - start
            out_summary = r.stdout.strip().replace("\n", " | ")[:120]
            log(f"[Cycle {cycle:04d}] {name} exit={r.returncode} in {elapsed:.1f}s | TFE: {tfe_status} | {out_summary}")
        except subprocess.TimeoutExpired:
            elapsed = time.time() - start
            log(f"WARNING: Keepalive cycle {cycle:04d} timed out after {elapsed:.1f}s | TFE: {tfe_status}; cleaning hanging child processes...")
            subprocess.run(["pkill", "-f", f"colab exec -s {SESSION_NAME}"], check=False)
        except Exception as e:
            elapsed = time.time() - start
            log(f"ERROR: Exception executing {name}: {e} in {elapsed:.1f}s | TFE: {tfe_status}")

        spent = time.time() - start
        to_sleep = max(1.0, 50.0 - spent)
        time.sleep(to_sleep)

if __name__ == "__main__":
    main()
