#!/usr/bin/env python3
"""
24/7 Google Colab Relay Supervisor & Gateway (Render & Linux Production Engine)
- Autonomous 3-account round-robin relay (8-hour shifts / 16-hour cooldown)
- Bundled colab_cli module invocation (zero $PATH or virtualenv guessing)
- Exact session pinpointing & zero split-brain auto-adoption on container restart
- Validated physical file (-f) keepalive duty cycle (40s compute ticks every 50s)
- Zero stdin piping under Uvicorn (100% immune to EOF/stdin truncation bugs)
- In-VM Telegram Bot Supervisor & 60-cycle (~50m) automatic Google Drive backups
- Indian Standard Time (IST, 12-hour AM/PM) telemetry across all logs and endpoints
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
import urllib.error
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

APP_DIR = os.path.dirname(os.path.abspath(__file__))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

os.environ["COLAB_HEADLESS"] = "1"

app = FastAPI(title="Colab Relay Supervisor 24/7")

CFG_DIR = os.path.normpath(os.path.expanduser("~/.config/colab-cli"))
TOKEN_JSON = os.path.normpath(os.path.join(CFG_DIR, "token.json"))
SETTINGS_JSON = os.path.normpath(os.path.join(CFG_DIR, "settings.json"))
TOKENS_DIR = os.path.join(APP_DIR, "tokens")
RCLONE_CONF_PATH = os.path.normpath(os.path.expanduser("~/.config/rclone/rclone.conf"))
LOCAL_RCLONE_CONF = os.path.join(APP_DIR, "rclone.conf")
LOCAL_TUNNEL_TOKEN = os.path.join(APP_DIR, "tunnel_token.txt")
LOCAL_TG_CONFIG = os.path.join(APP_DIR, "tg_config.json")
NODE_INFO_PATH = os.path.join(APP_DIR, "node_info.json")

DEFAULT_SHIFT_SECONDS = 8 * 3600       # 8 hours (28,800 seconds)
HANDOVER_THRESHOLD_SECONDS = 28500     # 7 hours 55 minutes (475 minutes)
PING_CADENCE = 50.0                    # 50-second duty cycle
CLI_TIMEOUT = 85.0                     # 85-second CLI execution timeout
PROCESS_TIMEOUT = 95.0                 # 95-second Python subprocess timeout

ACCOUNTS = {
    1: {"id": "acc-01", "name": "Account 1", "email": "sagesilveryt@gmail.com", "token_env": "COLAB_TOKEN_1", "file": "account_1.json"},
    2: {"id": "acc-02", "name": "Account 2", "email": "aravprakashsingh2@gmail.com", "token_env": "COLAB_TOKEN_2", "file": "account_2.json"},
    3: {"id": "acc-03", "name": "Account 3", "email": "recapmanhua72@gmail.com", "token_env": "COLAB_TOKEN_3", "file": "account_3.json"},
}

status_lock = threading.Lock()
colab_lock = threading.Lock()
log_history = collections.deque(maxlen=250)

relay_state = {
    "status": "INITIALIZING",
    "active_account_index": 1,
    "active_account_num": 2,
    "active_account_email": ACCOUNTS[2]["email"],
    "active_session": "colab-prod-node2",
    "active_endpoint": "UNKNOWN",
    "shift_start_time": time.time(),
    "shift_start_ist": "",
    "shift_duration_seconds": DEFAULT_SHIFT_SECONDS,
    "shift_elapsed_seconds": 0,
    "shift_remaining_seconds": DEFAULT_SHIFT_SECONDS,
    "total_cycles": 0,
    "last_cycle_time": None,
    "last_cycle_status": "NONE",
    "last_backup_time": "NONE",
    "ingress_status": "UNKNOWN",
    "consecutive_failures": 0,
    "accounts": [
        {"id": "acc-01", "name": "Account 1", "email": ACCOUNTS[1]["email"], "token_env": "COLAB_TOKEN_1"},
        {"id": "acc-02", "name": "Account 2", "email": ACCOUNTS[2]["email"], "token_env": "COLAB_TOKEN_2"},
        {"id": "acc-03", "name": "Account 3", "email": ACCOUNTS[3]["email"], "token_env": "COLAB_TOKEN_3"},
    ]
}


def get_ist_str():
    ist_time = time.gmtime(time.time() + 19800)
    return time.strftime("%Y-%m-%d %I:%M:%S %p IST", ist_time)


def log(msg):
    now = get_ist_str()
    line = f"[{now}] [SUPERVISOR] {msg}"
    print(line, flush=True)
    with status_lock:
        log_history.append(line)


def get_colab_cmd():
    """Always use the bundled colab_cli package via active Python interpreter."""
    bundled_pkg = os.path.join(APP_DIR, "colab_cli")
    if os.path.exists(bundled_pkg):
        return [sys.executable, "-m", "colab_cli.cli"]
    colab_bin = shutil.which("colab")
    if colab_bin:
        return [colab_bin]
    return [sys.executable, "-m", "colab_cli.cli"]


def run_colab(cmd_args, timeout=120):
    """
    Execute a colab CLI command safely with stdin=DEVNULL and PYTHONPATH=APP_DIR.
    Eliminates all Uvicorn stdin inheritance bugs and $PATH guesswork.
    """
    full_cmd = get_colab_cmd() + cmd_args
    env = os.environ.copy()
    env["COLAB_HEADLESS"] = "1"
    curr_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{APP_DIR}{os.pathsep}{curr_pp}" if curr_pp else APP_DIR

    try:
        r = subprocess.run(
            full_cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
            cwd=APP_DIR,
            env=env
        )
        return r.returncode, r.stdout.strip()
    except subprocess.TimeoutExpired:
        log(f"WARNING: Command '{' '.join(cmd_args)}' timed out after {timeout}s")
        return -1, "TimeoutExpired"
    except Exception as e:
        log(f"ERROR: Command execution exception ({' '.join(cmd_args)}): {e}")
        return -1, str(e)


def setup_credentials():
    """Initialize tokens, rclone.conf, tunnel_token.txt, and colab-cli settings."""
    os.makedirs(CFG_DIR, exist_ok=True)
    os.makedirs(TOKENS_DIR, exist_ok=True)
    os.makedirs(os.path.dirname(RCLONE_CONF_PATH), exist_ok=True)

    # 1. Disable colab_cli background PyPI update check
    try:
        with open(SETTINGS_JSON, "w", encoding="utf-8") as f:
            json.dump({"enable_update_check": False}, f)
    except Exception as e:
        log(f"Settings write note: {e}")

    # 2. Setup Rclone config from env or local fallback (support multiple aliases)
    rclone_b64 = (
        os.environ.get("RCLONE_CONF_B64")
        or os.environ.get("RCLONE_CONFIG_BASE64")
        or os.environ.get("RCLONE_CONFIG_B64")
        or ""
    ).strip()
    rclone_raw = (
        os.environ.get("RCLONE_CONF")
        or os.environ.get("RCLONE_CONFIG")
        or os.environ.get("RCLONE_CONF_RAW")
        or ""
    ).strip()

    if rclone_b64:
        try:
            decoded = base64.b64decode(rclone_b64)
            with open(RCLONE_CONF_PATH, "wb") as f:
                f.write(decoded)
            with open(LOCAL_RCLONE_CONF, "wb") as f:
                f.write(decoded)
            os.chmod(RCLONE_CONF_PATH, 0o600)
            os.chmod(LOCAL_RCLONE_CONF, 0o600)
            log("Configured rclone.conf from base64 environment variable.")
        except Exception as e:
            log(f"Rclone base64 config error: {e}")
    elif rclone_raw:
        try:
            with open(RCLONE_CONF_PATH, "w", encoding="utf-8") as f:
                f.write(rclone_raw)
            with open(LOCAL_RCLONE_CONF, "w", encoding="utf-8") as f:
                f.write(rclone_raw)
            os.chmod(RCLONE_CONF_PATH, 0o600)
            os.chmod(LOCAL_RCLONE_CONF, 0o600)
            log("Configured rclone.conf from raw environment variable.")
        except Exception as e:
            log(f"Rclone raw config error: {e}")
    else:
        # Check local fallback paths
        fallback_rclone_paths = [
            LOCAL_RCLONE_CONF,
            RCLONE_CONF_PATH,
            "/home/runner/workspace/colab_windows_pack/rclone.conf",
            "/config/.config/rclone/rclone.conf",
        ]
        for rp in fallback_rclone_paths:
            if os.path.exists(rp):
                if os.path.abspath(rp) != os.path.abspath(RCLONE_CONF_PATH):
                    shutil.copyfile(rp, RCLONE_CONF_PATH)
                    os.chmod(RCLONE_CONF_PATH, 0o600)
                if os.path.abspath(rp) != os.path.abspath(LOCAL_RCLONE_CONF):
                    shutil.copyfile(rp, LOCAL_RCLONE_CONF)
                    os.chmod(LOCAL_RCLONE_CONF, 0o600)
                log(f"Loaded rclone.conf from {rp}")
                break

    # 3. Setup Cloudflare Tunnel Token file (support multiple aliases)
    tunnel_token = (
        os.environ.get("TUNNEL_TOKEN")
        or os.environ.get("CLOUDFLARE_TUNNEL_TOKEN")
        or os.environ.get("CLOUDFLARED_TOKEN")
        or ""
    ).strip()
    if tunnel_token:
        try:
            with open(LOCAL_TUNNEL_TOKEN, "w", encoding="utf-8") as f:
                f.write(tunnel_token + "\n")
            os.chmod(LOCAL_TUNNEL_TOKEN, 0o600)
            log("Configured tunnel_token.txt from environment variable.")
        except Exception as e:
            log(f"Tunnel token file write error: {e}")
    elif not os.path.exists(LOCAL_TUNNEL_TOKEN):
        for tp in [
            "/home/runner/workspace/colab_windows_pack/tunnel_token.txt",
        ]:
            if os.path.exists(tp):
                try:
                    shutil.copyfile(tp, LOCAL_TUNNEL_TOKEN)
                    os.chmod(LOCAL_TUNNEL_TOKEN, 0o600)
                    log(f"Loaded tunnel_token.txt from {tp}")
                    break
                except Exception:
                    pass

    # 4. Extract Colab tokens for Accounts 1-3 from env or local fallback directories
    for acc_num, acc in ACCOUNTS.items():
        tok_env = acc["token_env"]
        raw = (
            os.environ.get(tok_env, "")
            or os.environ.get(f"TOKEN_{acc_num}", "")
            or os.environ.get(f"COLAB_TOKEN_ACC_{acc_num}", "")
            or os.environ.get(f"ACCOUNT_{acc_num}_TOKEN", "")
        ).strip()
        tok_dest = os.path.join(TOKENS_DIR, acc["file"])
        if raw:
            try:
                if raw.startswith("{"):
                    content = raw
                else:
                    content = base64.b64decode(raw).decode("utf-8")
                with open(tok_dest, "w", encoding="utf-8") as f:
                    f.write(content)
                os.chmod(tok_dest, 0o600)
                log(f"Installed token for {acc['name']} ({acc['email']}) from environment")
            except Exception as e:
                log(f"Error parsing token for Account {acc_num}: {e}")
        elif not os.path.exists(tok_dest):
            fallback_token_dirs = [
                os.path.join(APP_DIR, "colab_pool", "tokens"),
                "/home/runner/workspace/colab_pool/tokens",
                "/home/runner/workspace/colab_windows_pack/colab_pool/tokens",
            ]
            for fdir in fallback_token_dirs:
                cand = os.path.join(fdir, acc["file"])
                if os.path.exists(cand):
                    shutil.copyfile(cand, tok_dest)
                    os.chmod(tok_dest, 0o600)
                    log(f"Loaded local token file for {acc['name']} ({acc['email']})")
                    break

    # 5. Log Environment Variable Health Audit (Zero Plaintext Secrets)
    tg_bot, tg_chat = get_tg_credentials()
    has_rclone = os.path.exists(RCLONE_CONF_PATH) or os.path.exists(LOCAL_RCLONE_CONF)
    has_tunnel = os.path.exists(LOCAL_TUNNEL_TOKEN) and os.path.getsize(LOCAL_TUNNEL_TOKEN) > 10
    has_t1 = os.path.exists(os.path.join(TOKENS_DIR, "account_1.json"))
    has_t2 = os.path.exists(os.path.join(TOKENS_DIR, "account_2.json"))
    has_t3 = os.path.exists(os.path.join(TOKENS_DIR, "account_3.json"))
    has_tg = bool(tg_bot and tg_chat)

    log("=== [RENDER CONFIGURATION HEALTH AUDIT] ===")
    log(f"  * COLAB_TOKEN_1: {'[CONFIGURED]' if has_t1 else '[MISSING]'}")
    log(f"  * COLAB_TOKEN_2: {'[CONFIGURED]' if has_t2 else '[MISSING]'}")
    log(f"  * COLAB_TOKEN_3: {'[CONFIGURED]' if has_t3 else '[MISSING]'}")
    log(f"  * RCLONE_CONF:   {'[CONFIGURED]' if has_rclone else '[MISSING]'}")
    log(f"  * TUNNEL_TOKEN:  {'[CONFIGURED]' if has_tunnel else '[MISSING]'}")
    log(f"  * TELEGRAM_BOT:  {'[CONFIGURED]' if bool(tg_bot) else '[MISSING]'}")
    log(f"  * TELEGRAM_CHAT: {'[CONFIGURED]' if bool(tg_chat) else '[MISSING]'}")
    log("==========================================")

    with status_lock:
        relay_state["config_health"] = {
            "colab_token_1": has_t1,
            "colab_token_2": has_t2,
            "colab_token_3": has_t3,
            "rclone_conf": has_rclone,
            "tunnel_token": has_tunnel,
            "tg_bot_token": bool(tg_bot),
            "tg_chat_id": bool(tg_chat)
        }


_active_token_account_idx = None


def set_active_token(account_idx, force=False):
    """Switch active token.json to 0-based account_idx (0 -> Account 1, 1 -> Account 2, 2 -> Account 3).
    Preserves refreshed OAuth access tokens instead of overwriting token.json on every 50s cycle.
    """
    global _active_token_account_idx
    acc_num = account_idx + 1
    tok_src = os.path.join(TOKENS_DIR, f"account_{acc_num}.json")

    # If already on this account and token.json exists, sync any refreshed OAuth token back to TOKENS_DIR
    if not force and _active_token_account_idx == account_idx and os.path.exists(TOKEN_JSON):
        try:
            if os.path.getsize(TOKEN_JSON) > 20:
                shutil.copyfile(TOKEN_JSON, tok_src)
                os.chmod(tok_src, 0o600)
        except Exception:
            pass
        return True

    # Before switching away from a previous account, persist its refreshed token.json back to TOKENS_DIR
    if _active_token_account_idx is not None and _active_token_account_idx != account_idx and os.path.exists(TOKEN_JSON):
        prev_num = _active_token_account_idx + 1
        prev_dst = os.path.join(TOKENS_DIR, f"account_{prev_num}.json")
        try:
            if os.path.getsize(TOKEN_JSON) > 20:
                shutil.copyfile(TOKEN_JSON, prev_dst)
                os.chmod(prev_dst, 0o600)
        except Exception:
            pass

    if os.path.exists(tok_src):
        os.makedirs(CFG_DIR, exist_ok=True)
        shutil.copyfile(tok_src, TOKEN_JSON)
        try:
            os.chmod(TOKEN_JSON, 0o600)
        except Exception:
            pass
        try:
            from colab_cli.common import state
            state._client = None
            state._sessions = None
        except Exception:
            pass
        _active_token_account_idx = account_idx
        log(f"Active CLI token switched to Account {acc_num} ({ACCOUNTS[acc_num]['email']})")
        return True
    log(f"WARNING: Token file {tok_src} not found for Account {acc_num}!")
    return False


def get_tg_credentials():
    """Resolve Telegram Bot Token and Chat ID from environment or local gitignored tg_config.json."""
    bot_token = os.environ.get("TG_BOT_TOKEN", "").strip()
    chat_id = (os.environ.get("TG_CHAT_ID") or os.environ.get("TG_OWNER_ID") or "").strip()
    if not bot_token or not chat_id:
        for cfg_path in [
            LOCAL_TG_CONFIG,
            "/home/runner/workspace/colab_windows_pack/tg_config.json",
        ]:
            if os.path.exists(cfg_path):
                try:
                    with open(cfg_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        if not bot_token:
                            bot_token = str(data.get("tg_bot_token", "")).strip()
                        if not chat_id:
                            chat_id = str(data.get("tg_chat_id", "")).strip()
                    break
                except Exception:
                    pass
    return bot_token, chat_id


def generate_node_info(account_num, session_name):
    """Write node_info.json so in-VM colab_tg_bot.py knows the active account, session, and Telegram credentials."""
    tg_bot_token, tg_chat_id = get_tg_credentials()
    info = {
        "account": account_num,
        "email": ACCOUNTS.get(account_num, {}).get("email", f"Account {account_num}"),
        "session": session_name,
        "start_time_ist": get_ist_str(),
        "target_hours": 8,
        "tg_bot_token": tg_bot_token,
        "tg_chat_id": tg_chat_id
    }
    try:
        with open(NODE_INFO_PATH, "w", encoding="utf-8") as f:
            json.dump(info, f, indent=2)
        os.chmod(NODE_INFO_PATH, 0o600)
    except Exception as e:
        log(f"Warning: Could not write node_info.json: {e}")


def upload_all_scripts(session_name):
    """Upload all 9 operational scripts and configs to /content/ on the active Colab VM."""
    log(f"Uploading operational scripts & configs to '{session_name}':/content/...")
    files_to_upload = [
        ("ping_a.py", "/content/ping_a.py"),
        ("ping_b.py", "/content/ping_b.py"),
        ("rclone.conf", "/content/rclone.conf"),
        ("tunnel_token.txt", "/content/tunnel_token.txt"),
        ("sync_state.sh", "/content/sync_state.sh"),
        ("hydrate_and_setup.py", "/content/hydrate_and_setup.py"),
        ("execute_backup.py", "/content/execute_backup.py"),
        ("colab_tg_bot.py", "/content/colab_tg_bot.py"),
        ("node_info.json", "/content/node_info.json"),
    ]
    all_ok = True
    for fname, dst in files_to_upload:
        src = os.path.join(APP_DIR, fname)
        if os.path.exists(src):
            code, out = run_colab(["upload", "-s", session_name, src, dst], timeout=60)
            if code == 0:
                log(f"  [UPLOAD OK] {fname} -> {dst}")
            else:
                log(f"  [UPLOAD FAIL] {fname} -> {dst} (exit={code}): {out}")
                all_ok = False
        else:
            log(f"  [SKIP] Local file {fname} not present at {src}")
    return all_ok


def hydrate_session(session_name, force=False):
    """Run hydrate_and_setup.py on the Colab VM via physical file (-f)."""
    hydrate_path = os.path.join(APP_DIR, "hydrate_and_setup.py")
    cmd = ["exec", "-s", session_name, "-f", hydrate_path, "--timeout", "240"]
    if force:
        cmd.extend(["--env", "FORCE_RESTORE=1"])
    log(f"Hydrating services on '{session_name}' (force={force})...")
    code, out = run_colab(cmd, timeout=260)
    summary = out.replace("\n", " | ")[-240:] if out else ""
    log(f"Hydration on '{session_name}' finished with exit={code} | {summary}")
    return code == 0


def check_ingress():
    url = "https://colab.silver-xf.bond"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ColabRelaySupervisor/2.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            content = resp.read().decode("utf-8", errors="ignore")
            if resp.status == 200 or "vscode" in content.lower() or "login" in content.lower():
                return True, "HTTP 200 OK"
            return False, f"Unexpected HTTP status: {resp.status}"
    except urllib.error.HTTPError as he:
        if he.code == 405:
            return True, "HTTP 405 (OpenVSCode Active)"
        return False, f"HTTPError {he.code}"
    except Exception as e:
        return False, str(e)


def get_current_endpoint(session_name):
    """Retrieve the active endpoint ID for a session from sessions.json."""
    cfg = os.path.normpath(os.path.expanduser("~/.config/colab-cli/sessions.json"))
    if os.path.exists(cfg):
        try:
            with open(cfg, "r", encoding="utf-8") as f:
                data = json.load(f)
            if session_name in data:
                return data[session_name].get("endpoint")
        except Exception:
            pass
    return None


def provision_session(account_idx, session_name):
    """
    Create or verify a Colab session for account_idx, upload all 9 scripts, and run hydration.
    First checks Google's backend assignments via sync_sessions() to re-adopt existing VMs
    before falling back to 'colab new', preventing unnecessary node churn.
    """
    acc_num = account_idx + 1
    set_active_token(account_idx)

    log(f"Checking status for session '{session_name}' on Account {acc_num}...")
    s_code, s_out = run_colab(["status", "-s", session_name], timeout=35)
    s_lower = s_out.lower()
    is_not_found = ("not found" in s_lower) or ("no active sessions" in s_lower)
    is_active = (s_code == 0) and (not is_not_found) and (("idle" in s_lower) or ("busy" in s_lower) or (session_name in s_out))

    if not is_active:
        # Before calling 'colab new', check if the VM is still alive on Google's backend
        try:
            from colab_cli.common import state
            from colab_cli.state import SessionState

            state._client = None
            state._sessions = None
            _, assignments = state.sync_sessions()
            if assignments:
                a = assignments[0]
                s = SessionState(
                    name=session_name,
                    token=a.runtime_proxy_info.token,
                    url=a.runtime_proxy_info.url,
                    endpoint=a.endpoint,
                    token_expires_at=(
                        a.runtime_proxy_info.expires_at()
                        if hasattr(a, "runtime_proxy_info")
                        else None
                    ),
                    variant=getattr(a.variant, "name", "DEFAULT"),
                    accelerator=getattr(a.accelerator, "value", "NONE"),
                    machine_shape=getattr(a.machine_shape, "name", "STANDARD"),
                )
                state.store.add(s)
                is_active = True
                log(f"=== RE-ADOPTED live backend assignment '{a.endpoint}' on Account {acc_num} as '{session_name}' (avoided colab new) ===")
        except Exception as e:
            log(f"Backend assignment check note on Account {acc_num}: {e}")

    if not is_active:
        log(f"Allocating fresh session '{session_name}' on Account {acc_num}...")
        code, out = run_colab(["new", "-s", session_name], timeout=150)
        log(f"colab new output: {out}")
        if code != 0 and "ready" not in out.lower():
            # Verify via status before declaring failure
            c2, o2 = run_colab(["status", "-s", session_name], timeout=30)
            if c2 != 0 or "not found" in o2.lower():
                log(f"ERROR: Failed to provision session '{session_name}' on Account {acc_num}: {out}")
                return False
        log("Waiting 6s for fresh container initialization...")
        time.sleep(6)
    else:
        log(f"Session '{session_name}' is already active on Account {acc_num}.")

    generate_node_info(acc_num, session_name)
    upload_all_scripts(session_name)
    hydrate_session(session_name, force=False)

    ep = get_current_endpoint(session_name) or "ACTIVE"
    with status_lock:
        relay_state["active_endpoint"] = ep
    log(f"Session '{session_name}' (Account {acc_num}) ready with endpoint '{ep}'")
    return True


def auto_adopt_or_provision():
    """
    Auto-adopt any currently live Colab VM across the 3 accounts on startup.
    Runs Safe Smart Hydration on adopted sessions so all scripts and daemons are guaranteed present.
    """
    start_acc_env = int(os.environ.get("STARTING_ACCOUNT", "2"))
    if start_acc_env not in (1, 2, 3):
        start_acc_env = 2

    # Check all 3 accounts starting from STARTING_ACCOUNT
    search_order = [(start_acc_env - 1 + i) % 3 for i in range(3)]
    for idx in search_order:
        if not set_active_token(idx):
            continue
        try:
            from colab_cli.common import state
            from colab_cli.state import SessionState

            sessions, assignments = state.sync_sessions()
            if assignments:
                a = assignments[0]
                acc_num = idx + 1
                session_name = f"colab-prod-node{acc_num}"
                s = SessionState(
                    name=session_name,
                    token=a.runtime_proxy_info.token,
                    url=a.runtime_proxy_info.url,
                    endpoint=a.endpoint,
                    token_expires_at=(
                        a.runtime_proxy_info.expires_at()
                        if hasattr(a, "runtime_proxy_info")
                        else None
                    ),
                    variant=getattr(a.variant, "name", "DEFAULT"),
                    accelerator=getattr(a.accelerator, "value", "NONE"),
                    machine_shape=getattr(a.machine_shape, "name", "STANDARD"),
                )
                state.store.add(s)
                log(f"=== AUTO-ADOPTED running assignment '{a.endpoint}' on Account {acc_num} as '{session_name}' ===")
                generate_node_info(acc_num, session_name)
                upload_all_scripts(session_name)
                hydrate_session(session_name, force=False)
                return idx, session_name, a.endpoint
        except Exception as e:
            log(f"Assignment check note for Account {idx+1}: {e}")

    # No existing assignment found; provision fresh on STARTING_ACCOUNT
    target_idx = start_acc_env - 1
    acc_num = target_idx + 1
    session_name = f"colab-prod-node{acc_num}"
    log(f"No active assignments found across token pool. Provisioning fresh session '{session_name}' on Account {acc_num}...")
    provision_session(target_idx, session_name)
    ep = get_current_endpoint(session_name) or "UNKNOWN"
    return target_idx, session_name, ep


def execute_keepalive_pulse(session_name, cycle_num):
    """
    Execute the validated 40-second active duty cycle via physical file (-f).
    Maintains the Jupyter kernel in BUSY state and avoids stdin piping under Uvicorn.
    """
    script_name = "ping_a.py" if (cycle_num % 2 != 0) else "ping_b.py"
    script_path = os.path.join(APP_DIR, script_name)
    start = time.time()

    code, out = run_colab(
        ["exec", "-s", session_name, "-f", script_path, "--timeout", str(int(CLI_TIMEOUT))],
        timeout=PROCESS_TIMEOUT
    )
    elapsed = time.time() - start
    out_summary = out.replace("\n", " | ")[:140]
    success = (code == 0)

    if code == -1 and os.name != "nt":
        subprocess.run(["pkill", "-f", f"colab_cli.cli exec -s {session_name}"], check=False)

    log(f"[Cycle {cycle_num:04d}] {script_name} exit={code} in {elapsed:.1f}s | {out_summary}")
    return success, code, elapsed, out_summary


def backup_session(session_name):
    """
    Trigger execute_backup.py via physical file (-f) on the active session.
    """
    log(f"Executing Google Drive state backup on '{session_name}'...")
    backup_script = os.path.join(APP_DIR, "execute_backup.py")
    code, out = run_colab(["exec", "-s", session_name, "-f", backup_script, "--timeout", "180"], timeout=200)
    summary = out.replace("\n", " | ")[:180]
    log(f"Backup on '{session_name}' exit={code} | {summary}")
    if code == 0:
        with status_lock:
            relay_state["last_backup_time"] = get_ist_str()
    return code, out


def supervisor_worker():
    """
    Master 24/7 background daemon.
    Orchestrates 8-hour shift countdowns, periodic 60-cycle backups, hot handovers, and 50s keepalive pulses.
    """
    setup_credentials()

    curr_idx, curr_session, endpoint = auto_adopt_or_provision()
    acc_num = curr_idx + 1

    with status_lock:
        relay_state["status"] = "RUNNING"
        relay_state["active_account_index"] = curr_idx
        relay_state["active_account_num"] = acc_num
        relay_state["active_account_email"] = ACCOUNTS[acc_num]["email"]
        relay_state["active_session"] = curr_session
        relay_state["active_endpoint"] = endpoint
        relay_state["shift_start_time"] = time.time()
        relay_state["shift_start_ist"] = get_ist_str()

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

            # === HOT HANDOVER SEQUENCE AT 7h 55m (28,500s) ===
            if elapsed >= HANDOVER_THRESHOLD_SECONDS:
                next_idx = (curr_idx + 1) % 3
                next_acc_num = next_idx + 1
                next_session = f"colab-prod-node{next_acc_num}"
                log(f"=== INITIATING 8-HOUR SHIFT HANDOVER: Account {curr_idx+1} -> Account {next_acc_num} ===")

                with colab_lock:
                    # 1. Final Google Drive state backup on current node
                    set_active_token(curr_idx)
                    backup_session(curr_session)

                    # 2. Provision and hydrate successor node on next account
                    if provision_session(next_idx, next_session):
                        log(f"Successor '{next_session}' hydrated. Stopping predecessor '{curr_session}'...")
                        # 3. Cleanly stop predecessor session via 'colab stop -s'
                        set_active_token(curr_idx)
                        run_colab(["stop", "-s", curr_session], timeout=45)

                        # 4. Switch active token and state to successor
                        set_active_token(next_idx)
                        ep = get_current_endpoint(next_session) or "ACTIVE"
                        with status_lock:
                            relay_state["active_account_index"] = next_idx
                            relay_state["active_account_num"] = next_acc_num
                            relay_state["active_account_email"] = ACCOUNTS[next_acc_num]["email"]
                            relay_state["active_session"] = next_session
                            relay_state["active_endpoint"] = ep
                            relay_state["shift_start_time"] = time.time()
                            relay_state["shift_start_ist"] = get_ist_str()
                            relay_state["shift_duration_seconds"] = DEFAULT_SHIFT_SECONDS
                            relay_state["consecutive_failures"] = 0
                        curr_idx = next_idx
                        curr_session = next_session
                        log(f"=== HANDOVER COMPLETE: Account {next_acc_num} ({ACCOUNTS[next_acc_num]['email']}) is now serving ===")
                    else:
                        log("WARNING: Successor provisioning failed; extending current shift by 10 minutes to retry...")
                        set_active_token(curr_idx)
                        with status_lock:
                            relay_state["shift_duration_seconds"] += 600

            # === EXECUTE VERIFIED 40s KEEPALIVE PULSE ===
            cycle += 1
            with colab_lock:
                set_active_token(curr_idx)
                success, code_res, exec_duration, out_text = execute_keepalive_pulse(curr_session, cycle)

            with status_lock:
                relay_state["total_cycles"] = cycle
                relay_state["last_cycle_time"] = get_ist_str()
                relay_state["last_cycle_status"] = f"exit={code_res} in {exec_duration:.1f}s"
                if success:
                    relay_state["consecutive_failures"] = 0
                else:
                    relay_state["consecutive_failures"] += 1

            # Periodic Google Drive checkpoint backup every 60 cycles (~50 minutes)
            if cycle % 60 == 0:
                log(f"[Auto-Backup] Executing periodic 60-cycle checkpoint on '{curr_session}'...")
                with colab_lock:
                    backup_session(curr_session)

            # Auto-remediation if 3 consecutive keepalive pulses fail
            if relay_state["consecutive_failures"] >= 3:
                log(f"CRITICAL: 3 consecutive keepalive failures on '{curr_session}'! Triggering self-healing...")
                with colab_lock:
                    provision_session(curr_idx, curr_session)
                    with status_lock:
                        relay_state["consecutive_failures"] = 0

            # Probe public Web IDE ingress every 2 cycles
            if cycle % 2 == 0:
                ok, reason = check_ingress()
                with status_lock:
                    relay_state["ingress_status"] = "LIVE" if ok else f"DOWN ({reason})"

            to_sleep = max(1.0, PING_CADENCE - exec_duration)
            time.sleep(to_sleep)

        except Exception as e:
            log(f"Supervisor worker exception: {e}")
            time.sleep(10)


@app.on_event("startup")
def on_startup():
    t = threading.Thread(target=supervisor_worker, daemon=True)
    t.start()


@app.api_route("/healthz", methods=["GET", "HEAD"])
@app.api_route("/health", methods=["GET", "HEAD"])
def get_health():
    with status_lock:
        return {
            "status": "ok" if relay_state["consecutive_failures"] < 3 else "degraded",
            "time_ist": get_ist_str(),
            "active_account": relay_state["active_account_num"],
            "active_email": relay_state["active_account_email"],
            "session": relay_state["active_session"],
            "endpoint": relay_state.get("active_endpoint", "UNKNOWN"),
            "cycles": relay_state["total_cycles"],
            "shift_elapsed_mins": relay_state["shift_elapsed_seconds"] // 60,
            "shift_remaining_mins": relay_state["shift_remaining_seconds"] // 60,
            "last_cycle": relay_state["last_cycle_status"],
            "last_cycle_time": relay_state["last_cycle_time"],
            "last_backup_time": relay_state["last_backup_time"],
            "ingress": relay_state["ingress_status"],
            "consecutive_failures": relay_state["consecutive_failures"],
            "config_health": relay_state.get("config_health", {}),
        }


@app.api_route("/status", methods=["GET", "HEAD"])
def get_status():
    with status_lock:
        return dict(relay_state)


@app.api_route("/", methods=["GET", "HEAD"])
def get_root(request: Request):
    accept = request.headers.get("accept", "")
    with status_lock:
        state_copy = dict(relay_state)
        logs_copy = list(log_history)[-60:]

    if "application/json" in accept and "text/html" not in accept:
        return JSONResponse(state_copy)

    el_hrs = state_copy["shift_elapsed_seconds"] / 3600.0
    rem_hrs = state_copy["shift_remaining_seconds"] / 3600.0
    logs_html = "\n".join(logs_copy) if logs_copy else "Initializing supervisor..."

    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Colab 24/7 Relay Supervisor</title>
  <meta http-equiv="refresh" content="15">
  <style>
    body {{ background: #0d1117; color: #e6edf3; font-family: ui-monospace, SFMono-Regular, Consolas, monospace; margin: 24px; }}
    .card {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; margin-bottom: 16px; }}
    h2 {{ margin-top: 0; color: #58a6ff; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 12px; }}
    .stat {{ background: #0d1117; padding: 12px; border-radius: 6px; border: 1px solid #21262d; }}
    .label {{ color: #8b949e; font-size: 12px; }}
    .val {{ font-size: 16px; font-weight: bold; margin-top: 4px; color: #3fb950; }}
    pre {{ background: #010409; padding: 14px; border-radius: 6px; border: 1px solid #30363d; overflow-x: auto; max-height: 480px; font-size: 12px; line-height: 1.45; }}
    a {{ color: #58a6ff; text-decoration: none; }}
  </style>
</head>
<body>
  <div class="card">
    <h2>Google Colab 24/7 Autonomous Relay Supervisor</h2>
    <div class="grid">
      <div class="stat"><div class="label">STATUS</div><div class="val">{state_copy['status']}</div></div>
      <div class="stat"><div class="label">ACTIVE ACCOUNT</div><div class="val">Account {state_copy['active_account_num']} ({state_copy['active_account_email']})</div></div>
      <div class="stat"><div class="label">SESSION / ENDPOINT</div><div class="val">{state_copy['active_session']} ({state_copy['active_endpoint']})</div></div>
      <div class="stat"><div class="label">SHIFT PROGRESS</div><div class="val">{el_hrs:.2f}h elapsed / {rem_hrs:.2f}h left</div></div>
      <div class="stat"><div class="label">KEEPALIVE CYCLES</div><div class="val">{state_copy['total_cycles']} ({state_copy['last_cycle_status']})</div></div>
      <div class="stat"><div class="label">WEB IDE INGRESS</div><div class="val"><a href="https://colab.silver-xf.bond" target="_blank">{state_copy['ingress_status']}</a></div></div>
    </div>
  </div>
  <div class="card">
    <h2>Live Telemetry Console (IST)</h2>
    <pre>{logs_html}</pre>
  </div>
</body>
</html>"""
    return HTMLResponse(content=html)


@app.api_route("/logs", methods=["GET", "HEAD"])
def get_logs():
    with status_lock:
        return list(log_history)


@app.post("/rotate")
def force_rotation():
    """Manual trigger to immediately force rotation to the next Google account."""
    with status_lock:
        relay_state["shift_duration_seconds"] = relay_state["shift_elapsed_seconds"]
    log("Received manual API request to force immediate shift rotation.")
    return {"status": "rotation_triggered", "time_ist": get_ist_str()}


@app.post("/backup-test")
def run_backup_test():
    with status_lock:
        curr_session = relay_state["active_session"]
    log(f"Received API request for on-demand backup on '{curr_session}'")
    with colab_lock:
        code, output = backup_session(curr_session)
    return {
        "session": curr_session,
        "returncode": code,
        "time_ist": get_ist_str(),
        "output": output
    }
