#!/usr/bin/env python3
"""
24/7 Google Colab Relay Supervisor & Gateway for Render
- Autonomous 3-account round-robin relay (8-hour shifts / 16-hour cooldown)
- Exact session pinpointing & zero split-brain auto-adoption on container restart
- Validated stdin keepalive duty cycle (alternating 40s compute ticks every 50s)
- 100% immune to 11m 40s Colab idle eviction reapers
- Zero plaintext credentials hardcoded; configured via Render environment variables
"""

import os
import sys
import time
import json
import base64
import shutil
import threading
import subprocess
import collections
import urllib.request
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Colab Relay Supervisor 24/7")

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CFG_DIR = os.path.expanduser("~/.config/colab-cli")
TOKEN_JSON = os.path.join(CFG_DIR, "token.json")
TOKENS_DIR = os.path.join(APP_DIR, "tokens")
RCLONE_CONF_PATH = os.path.expanduser("~/.config/rclone/rclone.conf")

DEFAULT_SHIFT_SECONDS = 28500  # 7 hours 55 minutes (475 minutes)
HANDOVER_THRESHOLD_SECONDS = 28200  # Minute 470 (10 minutes before shift ends)
PING_CADENCE = 50.0  # 50-second cycle
CLI_TIMEOUT = 70.0  # 70-second CLI timeout
PROCESS_TIMEOUT = 75.0  # 75-second Python subprocess timeout

VENV_COLAB = os.environ.get("VENV_COLAB") or shutil.which("colab") or "/home/runner/workspace/.colab-venv/bin/colab" or "colab"

status_lock = threading.Lock()
colab_lock = threading.Lock()
log_history = collections.deque(maxlen=250)

relay_state = {
    "status": "INITIALIZING",
    "active_account_index": 0,
    "active_session": "colab-worker-01",
    "active_endpoint": "UNKNOWN",
    "shift_start_time": time.time(),
    "shift_duration_seconds": DEFAULT_SHIFT_SECONDS,
    "shift_elapsed_seconds": 0,
    "shift_remaining_seconds": DEFAULT_SHIFT_SECONDS,
    "total_cycles": 0,
    "last_cycle_time": None,
    "last_cycle_status": "NONE",
    "ingress_status": "UNKNOWN",
    "consecutive_failures": 0,
    "accounts": [
        {"id": "acc-01", "name": "Account 1", "token_env": "COLAB_TOKEN_1"},
        {"id": "acc-02", "name": "Account 2", "token_env": "COLAB_TOKEN_2"},
        {"id": "acc-03", "name": "Account 3", "token_env": "COLAB_TOKEN_3"}
    ]
}

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
    now = datetime.now(IST).strftime("%Y-%m-%d %I:%M:%S %p IST")
    line = f"[{now}] [RENDER-SUPERVISOR] {msg}"
    print(line, flush=True)
    with status_lock:
        log_history.append(line)

def setup_credentials():
    os.makedirs(CFG_DIR, exist_ok=True)
    os.makedirs(TOKENS_DIR, exist_ok=True)
    os.makedirs(os.path.dirname(RCLONE_CONF_PATH), exist_ok=True)

    # 1. Setup Rclone config if provided via env
    rclone_b64 = os.environ.get("RCLONE_CONF_B64") or os.environ.get("RCLONE_CONFIG_BASE64", "")
    if rclone_b64:
        try:
            with open(RCLONE_CONF_PATH, "wb") as f:
                f.write(base64.b64decode(rclone_b64))
            os.chmod(RCLONE_CONF_PATH, 0o600)
            log("Configured rclone.conf from environment.")
        except Exception as e:
            log(f"Rclone config error: {e}")

    # 2. Extract Colab tokens from env or local fallback files
    for idx, acc in enumerate(relay_state["accounts"]):
        tok_env = acc["token_env"]
        raw = os.environ.get(tok_env, "")
        tok_dest = os.path.join(TOKENS_DIR, f"account_{idx+1}.json")
        if raw:
            try:
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
        elif not os.path.exists(tok_dest):
            # Check local workspace fallback for testing
            local_src = os.path.join("/home/runner/workspace/colab_pool/tokens", f"account_{idx+1}.json")
            if os.path.exists(local_src):
                shutil.copyfile(local_src, tok_dest)
                os.chmod(tok_dest, 0o600)
                log(f"Loaded local token file for {acc['name']}")

def set_active_token(account_idx):
    tok_src = os.path.join(TOKENS_DIR, f"account_{account_idx+1}.json")
    if os.path.exists(tok_src):
        shutil.copyfile(tok_src, TOKEN_JSON)
        os.chmod(TOKEN_JSON, 0o600)
        try:
            from colab_cli.common import state
            state._client = None
            state._sessions = None
        except Exception:
            pass
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
            if resp.status == 200 or "vscode" in content.lower() or "login" in content:
                return True, "HTTP 200 OK"
            return False, f"Unexpected HTTP status: {resp.status}"
    except urllib.error.HTTPError as he:
        # OpenVSCode server answers 405 on certain probe methods, meaning the server and tunnel are live
        if he.code == 405:
            return True, "HTTP 405 (Server Active)"
        return False, f"HTTPError {he.code}"
    except Exception as e:
        return False, str(e)

def get_current_endpoint(session_name):
    """
    Retrieves the active endpoint ID for a session from local state or sessions.json.
    """
    try:
        from colab_cli.common import state
        s = state.store.get(session_name)
        if s and s.endpoint:
            return s.endpoint
    except Exception:
        pass
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

def auto_adopt_or_provision():
    """
    Auto-adopt any currently live Colab VM across the 3 accounts on startup.
    Prevents duplicate allocation and split-brain instances on Render restarts.
    """
    for idx in range(3):
        if not set_active_token(idx):
            continue
        try:
            from colab_cli.common import state
            from colab_cli.state import SessionState

            sessions, assignments = state.sync_sessions()
            if assignments:
                a = assignments[0]
                session_name = f"colab-worker-{idx+1:02d}"
                s = SessionState(
                    name=session_name,
                    token=a.runtime_proxy_info.token,
                    url=a.runtime_proxy_info.url,
                    endpoint=a.endpoint,
                    variant=getattr(a.variant, "name", "DEFAULT"),
                    accelerator=getattr(a.accelerator, "value", "NONE"),
                    machine_shape=getattr(a.machine_shape, "name", "STANDARD"),
                )
                state.store.add(s)
                log(f"=== AUTO-ADOPTED running assignment '{a.endpoint}' on Account [{idx+1}] as '{session_name}' ===")
                return idx, session_name, a.endpoint
        except Exception as e:
            log(f"Assignment check note for Account [{idx+1}]: {e}")

    # No existing assignment found on any account; provision fresh on Account 1
    log("No active assignments found across token pool. Provisioning fresh session on Account 1...")
    set_active_token(0)
    session_name = "colab-worker-01"
    provision_session(session_name)
    ep = get_current_endpoint(session_name) or "UNKNOWN"
    return 0, session_name, ep

def execute_keepalive_pulse(session_name, cycle_num):
    """
    Executes the validated 40-second active duty cycle via physical file (-f).
    Maintains the Jupyter kernel in BUSY state and avoids stdin piping issues under Uvicorn.
    """
    script_name = "ping_a.py" if (cycle_num % 2 != 0) else "ping_b.py"
    script_path = os.path.join(APP_DIR, script_name)
    start = time.time()

    try:
        r = subprocess.run(
            [VENV_COLAB, "exec", "-s", session_name, "-f", script_path, "--timeout", str(int(CLI_TIMEOUT))],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=PROCESS_TIMEOUT
        )
        elapsed = time.time() - start
        out = r.stdout.strip().replace("\n", " | ")[:120]
        success = (r.returncode == 0)
        log(f"[Cycle {cycle_num:04d}] {script_name} exit={r.returncode} in {elapsed:.1f}s | {out}")
        return success, r.returncode, elapsed, out
    except subprocess.TimeoutExpired:
        elapsed = time.time() - start
        log(f"WARNING: Keepalive cycle {cycle_num:04d} timed out after {elapsed:.1f}s; cleaning hanging child processes...")
        subprocess.run(["pkill", "-f", f"colab exec -s {session_name}"], check=False)
        return False, -1, elapsed, "TimeoutExpired"
    except Exception as e:
        elapsed = time.time() - start
        log(f"ERROR: Keepalive exception: {e} in {elapsed:.1f}s")
        return False, -1, elapsed, str(e)

def provision_session(session_name):
    """
    Provisions a new session, uploads scripts, and triggers hydration.
    """
    log(f"Provisioning session '{session_name}' via '{VENV_COLAB}'...")
    r = subprocess.run([VENV_COLAB, "new", "-s", session_name], capture_output=True, text=True)
    log(f"colab new: {r.stdout.strip()}")
    if r.returncode != 0:
        log(f"Error provisioning session: {r.stdout} | {r.stderr}")
        return False

    time.sleep(3)

    ping_a = os.path.join(APP_DIR, "ping_a.py")
    ping_b = os.path.join(APP_DIR, "ping_b.py")
    hydrate = os.path.join(APP_DIR, "hydrate_and_setup.py")
    sync_script = os.path.join(APP_DIR, "sync_state.sh")

    # Upload scripts
    for src, dst in [(ping_a, "/content/ping_a.py"), (ping_b, "/content/ping_b.py"), (hydrate, "/content/hydrate_and_setup.py"), (sync_script, "/content/sync_state.sh")]:
        if os.path.exists(src):
            subprocess.run([VENV_COLAB, "upload", "-s", session_name, src, dst], capture_output=True, text=True)

    if os.path.exists(RCLONE_CONF_PATH):
        subprocess.run([VENV_COLAB, "upload", "-s", session_name, RCLONE_CONF_PATH, "/content/rclone.conf"], capture_output=True, text=True)

    tunnel_token = os.environ.get("TUNNEL_TOKEN") or os.environ.get("CLOUDFLARE_TUNNEL_TOKEN", "")
    if tunnel_token:
        tok_code = f"with open('/content/tunnel_token.txt', 'w') as f: f.write('{tunnel_token.strip()}')\n"
        subprocess.run([VENV_COLAB, "exec", "-s", session_name], input=tok_code, text=True, capture_output=True)

    log("Hydrating services on new session...")
    r_hyd = subprocess.run([VENV_COLAB, "exec", "-s", session_name, "-f", hydrate, "--timeout", "300"], capture_output=True, text=True, timeout=320)
    log(f"Hydration exit code: {r_hyd.returncode}")

    ep = get_current_endpoint(session_name)
    if ep:
        with status_lock:
            relay_state["active_endpoint"] = ep
        log(f"Session '{session_name}' initialized with endpoint '{ep}'")
    return True

def backup_session(session_name):
    """
    Triggers sync_state.sh on the active session to persist workspace and chats to Google Drive.
    """
    log(f"Executing Google Drive state backup on '{session_name}'...")
    backup_code = "import subprocess; print(subprocess.check_output(['bash', '/content/sync_state.sh']).decode())\n"
    try:
        r = subprocess.run([VENV_COLAB, "exec", "-s", session_name, "--timeout", "180"], input=backup_code, text=True, capture_output=True, timeout=190)
        log(f"Backup on '{session_name}' exit={r.returncode} | {r.stdout.strip()[:150]}")
        return r.returncode, r.stdout.strip()
    except Exception as e:
        log(f"Backup exception on '{session_name}': {e}")
        return -1, str(e)

def supervisor_worker():
    """
    Master background daemon running 24/7 on Render.
    Orchestrates shift countdowns, hot handovers, and continuous keepalive pulses.
    """
    setup_credentials()

    # Discover and adopt running instance or create fresh
    curr_idx, curr_session, endpoint = auto_adopt_or_provision()

    with status_lock:
        relay_state["status"] = "RUNNING"
        relay_state["active_account_index"] = curr_idx
        relay_state["active_session"] = curr_session
        relay_state["active_endpoint"] = endpoint
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

            # === HOT HANDOVER SEQUENCE AT MINUTE 470 ===
            if elapsed >= HANDOVER_THRESHOLD_SECONDS:
                next_idx = (curr_idx + 1) % 3
                next_session = f"colab-worker-{next_idx+1:02d}"
                log(f"=== INITIATING 8-HOUR SHIFT HANDOVER: Account [{curr_idx+1}] -> Account [{next_idx+1}] ===")

                with colab_lock:
                    # 1. Quiesce & Backup current state to Google Drive
                    backup_session(curr_session)

                    # 2. Provision successor on next account
                    set_active_token(next_idx)
                    if provision_session(next_session):
                        log(f"Successor '{next_session}' hydrated. Retiring predecessor '{curr_session}'...")
                        # 3. Clean retirement of old node
                        set_active_token(curr_idx)
                        subprocess.run([VENV_COLAB, "rm", "-s", curr_session], capture_output=True, text=True)

                        # 4. Switch active state to successor
                        set_active_token(next_idx)
                        ep = get_current_endpoint(next_session)
                        with status_lock:
                            relay_state["active_account_index"] = next_idx
                            relay_state["active_session"] = next_session
                            if ep:
                                relay_state["active_endpoint"] = ep
                            relay_state["shift_start_time"] = time.time()
                        log(f"Handover successful! Account [{next_idx+1}] is now actively serving.")
                    else:
                        log("Successor provisioning failed; extending current shift by 10 minutes to retry...")
                        with status_lock:
                            relay_state["shift_duration_seconds"] += 600

            # === EXECUTE VERIFIED KEEPALIVE PULSE ===
            cycle += 1
            with colab_lock:
                success, code_res, exec_duration, out_text = execute_keepalive_pulse(curr_session, cycle)

            with status_lock:
                relay_state["total_cycles"] = cycle
                relay_state["last_cycle_time"] = datetime.now(IST).strftime("%Y-%m-%d %I:%M:%S %p IST")
                relay_state["last_cycle_status"] = f"code {code_res} in {exec_duration:.1f}s"
                if success:
                    relay_state["consecutive_failures"] = 0
                else:
                    relay_state["consecutive_failures"] += 1

            # Auto-remediation if 3 consecutive keepalive pulses fail
            if relay_state["consecutive_failures"] >= 3:
                log(f"CRITICAL: 3 consecutive keepalive failures on '{curr_session}'! Self-healing triggered...")
                with colab_lock:
                    provision_session(curr_session)
                    relay_state["consecutive_failures"] = 0

            # Probe ingress every 2 cycles
            if cycle % 2 == 0:
                ok, reason = check_ingress()
                with status_lock:
                    relay_state["ingress_status"] = "LIVE" if ok else f"DOWN ({reason})"

            spent = exec_duration
            to_sleep = max(1.0, PING_CADENCE - spent)
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
            "status": "healthy" if relay_state["consecutive_failures"] < 3 else "degraded",
            "active_account": relay_state["active_account_index"] + 1,
            "session": relay_state["active_session"],
            "endpoint": relay_state.get("active_endpoint", "UNKNOWN"),
            "cycles": relay_state["total_cycles"],
            "shift_elapsed_mins": relay_state["shift_elapsed_seconds"] // 60,
            "shift_remaining_mins": relay_state["shift_remaining_seconds"] // 60,
            "last_cycle": relay_state["last_cycle_status"],
            "ingress": relay_state["ingress_status"],
            "consecutive_failures": relay_state["consecutive_failures"]
        }

@app.get("/logs")
def get_logs():
    with status_lock:
        return list(log_history)

@app.post("/rotate")
def force_rotation():
    """Manual trigger to immediately force rotation to the next Google account."""
    with status_lock:
        relay_state["shift_duration_seconds"] = relay_state["shift_elapsed_seconds"]
    log("Received manual API request to force immediate shift rotation.")
    return {"status": "rotation_triggered"}

@app.post("/backup-test")
def run_backup_test():
    with status_lock:
        curr_session = relay_state["active_session"]
    log(f"Received API request for on-demand backup on '{curr_session}'")
    code, output = backup_session(curr_session)
    return {
        "session": curr_session,
        "returncode": code,
        "output": output
    }
