#!/usr/bin/env python3
"""
24/7 Google Colab Relay Supervisor & Gateway
Runs continuously on Render, maintaining the external interactive connection
and ping-pong keepalive across a 3-account round-robin pool.
Zero credentials hardcoded.
"""

import os
import sys
import time
import json
import base64
import shutil
import threading
import subprocess
import urllib.request
from datetime import datetime, timezone
from fastapi import FastAPI

app = FastAPI(title="Colab Relay Supervisor")

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CFG_DIR = os.path.expanduser("~/.config/colab-cli")
TOKEN_JSON = os.path.join(CFG_DIR, "token.json")
TOKENS_DIR = os.path.join(APP_DIR, "tokens")
RCLONE_CONF_PATH = os.path.expanduser("~/.config/rclone/rclone.conf")

DEFAULT_SHIFT_SECONDS = 28500  # 7 hours 55 minutes
PING_CADENCE = 50.0  # 50-second cycle
PING_TIMEOUT = 60.0  # 60-second CLI timeout

status_lock = threading.Lock()
relay_state = {
    "status": "INITIALIZING",
    "active_account_index": 0,
    "active_session": "colab-worker-01",
    "shift_start_time": time.time(),
    "shift_duration_seconds": DEFAULT_SHIFT_SECONDS,
    "shift_elapsed_seconds": 0,
    "shift_remaining_seconds": DEFAULT_SHIFT_SECONDS,
    "total_cycles": 0,
    "last_cycle_time": None,
    "last_cycle_status": "NONE",
    "ingress_status": "UNKNOWN",
    "accounts": [
        {"id": "acc-01", "name": "Account 1", "token_env": "COLAB_TOKEN_1"},
        {"id": "acc-02", "name": "Account 2", "token_env": "COLAB_TOKEN_2"},
        {"id": "acc-03", "name": "Account 3", "token_env": "COLAB_TOKEN_3"}
    ]
}

def log(msg):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{now}] [RENDER-SUPERVISOR] {msg}", flush=True)

def setup_credentials():
    os.makedirs(CFG_DIR, exist_ok=True)
    os.makedirs(TOKENS_DIR, exist_ok=True)
    os.makedirs(os.path.dirname(RCLONE_CONF_PATH), exist_ok=True)

    # 1. Setup Rclone config if provided via env
    rclone_b64 = os.environ.get("RCLONE_CONF_B64", "")
    if rclone_b64:
        try:
            with open(RCLONE_CONF_PATH, "wb") as f:
                f.write(base64.b64decode(rclone_b64))
            os.chmod(RCLONE_CONF_PATH, 0o600)
            log("Configured rclone.conf from environment.")
        except Exception as e:
            log(f"Rclone config error: {e}")

    # 2. Extract Colab tokens from env
    for idx, acc in enumerate(relay_state["accounts"]):
        tok_env = acc["token_env"]
        raw = os.environ.get(tok_env, "")
        if raw:
            tok_dest = os.path.join(TOKENS_DIR, f"account_{idx+1}.json")
            try:
                # Support plain json string or base64
                if raw.strip().startswith("{"):
                    content = raw
                else:
                    content = base64.b64decode(raw).decode("utf-8")
                with open(tok_dest, "w") as f:
                    f.write(content)
                os.chmod(tok_dest, 0o600)
                log(f"Installed token for {acc['name']}")
            except Exception as e:
                log(f"Error parsing {tok_env}: {e}")

def set_active_token(account_idx):
    tok_src = os.path.join(TOKENS_DIR, f"account_{account_idx+1}.json")
    if os.path.exists(tok_src):
        shutil.copyfile(tok_src, TOKEN_JSON)
        os.chmod(TOKEN_JSON, 0o600)
        log(f"Active CLI token switched to Account [{account_idx+1}]")
        return True
    log(f"Warning: Token file {tok_src} not found.")
    return False

def check_ingress():
    url = "https://colab.silver-xf.bond"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "RenderSupervisor/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            content = resp.read().decode("utf-8", errors="ignore")
            if "code-server" in content:
                return True, "200 OK (code-server)"
            return False, f"Unexpected body length: {len(content)}"
    except Exception as e:
        return False, str(e)

def run_cli_cmd(cmd, input_data=None, timeout=90):
    return subprocess.run(
        cmd,
        input=input_data,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout
    )

def provision_session(session_name):
    log(f"Provisioning session '{session_name}'...")
    r = run_cli_cmd(["colab", "new", "-s", session_name], timeout=60)
    log(f"colab new: {r.stdout.strip()}")
    if r.returncode != 0:
        log(f"Error provisioning session: {r.stdout}")
        return False

    time.sleep(3)

    # Upload ping scripts and credentials to VM
    ping_a = os.path.join(APP_DIR, "ping_a.py")
    ping_b = os.path.join(APP_DIR, "ping_b.py")
    hydrate = os.path.join(APP_DIR, "hydrate_and_setup.py")

    run_cli_cmd(["colab", "upload", "-s", session_name, ping_a, "/content/ping_a.py"])
    run_cli_cmd(["colab", "upload", "-s", session_name, ping_b, "/content/ping_b.py"])

    if os.path.exists(RCLONE_CONF_PATH):
        run_cli_cmd(["colab", "upload", "-s", session_name, RCLONE_CONF_PATH, "/content/rclone.conf"])

    tunnel_token = os.environ.get("TUNNEL_TOKEN", "")
    if tunnel_token:
        r_tok = run_cli_cmd(["colab", "exec", "-s", session_name], input_data=f"with open('/content/tunnel_token.txt', 'w') as f: f.write('{tunnel_token}')\n")

    log("Hydrating services on new session...")
    r_hyd = run_cli_cmd(["colab", "exec", "-s", session_name, "-f", hydrate, "--timeout", "180"], timeout=200)
    log(f"Hydration exit code: {r_hyd.returncode}")

    return True

def supervisor_worker():
    setup_credentials()

    curr_idx = 0
    curr_session = f"colab-worker-{curr_idx+1:02d}"
    set_active_token(curr_idx)

    # Initial allocation if needed
    r = run_cli_cmd(["colab", "ls"])
    if curr_session not in r.stdout:
        provision_session(curr_session)

    with status_lock:
        relay_state["status"] = "RUNNING"
        relay_state["active_account_index"] = curr_idx
        relay_state["active_session"] = curr_session
        relay_state["shift_start_time"] = time.time()

    cycle = 0
    while True:
        try:
            with status_lock:
                start_time = relay_state["shift_start_time"]
                duration = relay_state["shift_duration_seconds"]
                curr_idx = relay_state["active_account_index"]
                curr_session = relay_state["active_session"]

            elapsed = time.time() - start_time
            remaining = max(0.0, duration - elapsed)

            with status_lock:
                relay_state["shift_elapsed_seconds"] = int(elapsed)
                relay_state["shift_remaining_seconds"] = int(remaining)

            # Check if 8-hour shift is complete
            if elapsed >= duration:
                log(f"Shift of {elapsed:.0f}s completed! Initiating hot handover...")
                next_idx = (curr_idx + 1) % 3
                next_session = f"colab-worker-{next_idx+1:02d}"

                # Switch to successor token and provision
                set_active_token(next_idx)
                if provision_session(next_session):
                    log(f"Successor '{next_session}' hydrated. Retiring predecessor '{curr_session}'...")
                    set_active_token(curr_idx)
                    run_cli_cmd(["colab", "stop", "-s", curr_session])
                    set_active_token(next_idx)

                    with status_lock:
                        relay_state["active_account_index"] = next_idx
                        relay_state["active_session"] = next_session
                        relay_state["shift_start_time"] = time.time()
                    log("Handover complete! Continuing bouncer on successor.")
                else:
                    log("Handover failed. Retrying in 60s...")
                    time.sleep(60)
                    continue

            # Run ping-pong keepalive cycle
            cycle += 1
            script_file = os.path.join(APP_DIR, "ping_a.py" if (cycle % 2 != 0) else "ping_b.py")
            script_name = "ping_a.py" if (cycle % 2 != 0) else "ping_b.py"

            cycle_start = time.time()
            r_exec = run_cli_cmd(["colab", "exec", "-s", curr_session, "-f", script_file, "--timeout", str(PING_TIMEOUT)], timeout=75)

            exec_duration = time.time() - cycle_start

            with status_lock:
                relay_state["total_cycles"] = cycle
                relay_state["last_cycle_time"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                relay_state["last_cycle_status"] = f"{script_name}: code {r_exec.returncode} in {exec_duration:.1f}s"

            if r_exec.returncode != 0 and "not found" in r_exec.stdout.lower():
                log(f"CRITICAL: Session '{curr_session}' lost! Re-provisioning immediately...")
                provision_session(curr_session)

            # Check ingress
            if cycle % 2 == 0:
                ok, reason = check_ingress()
                with status_lock:
                    relay_state["ingress_status"] = "LIVE" if ok else f"DOWN ({reason})"

            spent = time.time() - cycle_start
            to_sleep = max(0.0, PING_CADENCE - spent)
            if to_sleep > 0:
                time.sleep(to_sleep)

        except Exception as e:
            log(f"Supervisor worker exception: {e}")
            time.sleep(10)

@app.on_event("startup")
def on_startup():
    t = threading.Thread(target=supervisor_worker, daemon=True)
    t.start()

@app.get("/")
def get_root():
    with status_lock:
        return dict(relay_state)

@app.get("/health")
def get_health():
    with status_lock:
        return {
            "status": "healthy",
            "session": relay_state["active_session"],
            "cycles": relay_state["total_cycles"],
            "last_cycle": relay_state["last_cycle_status"],
            "ingress": relay_state["ingress_status"]
        }
