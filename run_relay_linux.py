#!/usr/bin/env python3
"""
Autonomous Google Colab Relay & Keepalive Engine (Windows & Linux Cross-Platform)
- Designed for Native Windows (PowerShell / CMD) & WSL2 / Linux.
- Supports 3-Account Token Pool (Account 1, Account 2, Account 3).
- Bootstraps Cloaked OpenVSCode Server (port 8443) and Disguised Cloudflare Tunnel.
- Executes the validated 40s active ping-pong duty cycle (ping_a.py / ping_b.py) every 50s.
- Keeps Colab kernel continuously in BUSY state to defeat 11m 40s idle eviction reapers.
"""

import os
import sys
import json
import time
import shutil
import argparse
import subprocess

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

LOG_FILE = os.path.join(SCRIPT_DIR, "keepalive_relay.log")

TUNNEL_TOKEN = (os.environ.get("TUNNEL_TOKEN") or os.environ.get("CLOUDFLARE_TUNNEL_TOKEN") or "").strip()
OPENVSCODE_RELEASE = "https://github.com/gitpod-io/openvscode-server/releases/download/openvscode-server-v1.109.5/openvscode-server-v1.109.5-linux-x64.tar.gz"

ACCOUNTS = {
    1: {"email": "sagesilveryt@gmail.com", "file": "account_1.json"},
    2: {"email": "aravprakashsingh2@gmail.com", "file": "account_2.json"},
    3: {"email": "recapmanhua72@gmail.com", "file": "account_3.json"},
}


def log(msg):
    # Indian Standard Time (IST = UTC + 5:30)
    ist_time = time.gmtime(time.time() + 19800)
    now = time.strftime("%Y-%m-%d %I:%M:%S %p IST", ist_time)
    line = f"[{now}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def get_colab_cmd():
    """Locate the colab CLI executable or fall back to python -m colab_cli.cli."""
    # If colab_cli is bundled locally in SCRIPT_DIR, use the active python interpreter directly
    bundled_pkg = os.path.join(SCRIPT_DIR, "colab_cli")
    if os.path.exists(bundled_pkg):
        return [sys.executable, "-m", "colab_cli.cli"]

    colab_bin = shutil.which("colab")
    if colab_bin:
        return [colab_bin]

    venv_colab = os.path.join(SCRIPT_DIR, ".colab-venv", "bin", "colab")
    if os.path.exists(venv_colab):
        return [venv_colab]

    return [sys.executable, "-m", "colab_cli.cli"]


def run_colab(cmd_args, input_data=None, timeout=120):
    """Execute a colab CLI command safely with cross-platform output capture."""
    base_cmd = get_colab_cmd()
    full_cmd = base_cmd + cmd_args
    env = os.environ.copy()
    current_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{SCRIPT_DIR}{os.pathsep}{current_pythonpath}" if current_pythonpath else SCRIPT_DIR
    try:
        r = subprocess.run(
            full_cmd,
            input=input_data,
            stdin=subprocess.DEVNULL if input_data is None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
            cwd=SCRIPT_DIR,
            env=env
        )
        return r.returncode, r.stdout.strip()
    except subprocess.TimeoutExpired:
        log(f"WARNING: Command '{' '.join(full_cmd)}' timed out after {timeout}s")
        return -1, "TimeoutExpired"
    except Exception as e:
        log(f"ERROR: Command execution exception: {e}")
        return -1, str(e)


def run_colab_stream(cmd_args, timeout=240):
    """Execute a colab CLI command while streaming stdout line-by-line live to the terminal."""
    base_cmd = get_colab_cmd()
    full_cmd = base_cmd + cmd_args
    env = os.environ.copy()
    current_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{SCRIPT_DIR}{os.pathsep}{current_pythonpath}" if current_pythonpath else SCRIPT_DIR
    lines = []
    try:
        proc = subprocess.Popen(
            full_cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=SCRIPT_DIR,
            env=env
        )
        start_time = time.time()
        while True:
            line = proc.stdout.readline()
            if not line and proc.poll() is not None:
                break
            if line:
                cleaned = line.rstrip("\r\n")
                if cleaned:
                    print(f"  {cleaned}", flush=True)
                    lines.append(cleaned)
            if time.time() - start_time > timeout:
                proc.kill()
                log(f"WARNING: Command '{' '.join(full_cmd)}' timed out after {timeout}s")
                return -1, "\n".join(lines) + "\nTimeoutExpired"
        proc.wait()
        return proc.returncode, "\n".join(lines)
    except Exception as e:
        log(f"ERROR: Stream execution exception: {e}")
        return -1, str(e)


def setup_account_token(account_num):
    """Configure the selected account token in ~/.config/colab-cli/token.json."""
    if account_num not in ACCOUNTS:
        raise ValueError(f"Invalid account number {account_num}. Choose 1, 2, or 3.")

    info = ACCOUNTS[account_num]
    candidate_paths = [
        os.path.join(SCRIPT_DIR, "colab_pool", "tokens", info["file"]),
        os.path.join(SCRIPT_DIR, "tokens", info["file"]),
        os.path.join("/home/runner/workspace/colab_pool/tokens", info["file"]),
        os.path.join("/home/runner/workspace/colab_windows_pack/colab_pool/tokens", info["file"]),
    ]
    token_src = None
    for cand in candidate_paths:
        if os.path.exists(cand):
            token_src = cand
            break

    # Also support COLAB_TOKEN_1..3 environment variables on Linux/Render
    if not token_src:
        env_raw = os.environ.get(f"COLAB_TOKEN_{account_num}", "").strip()
        if env_raw:
            import base64
            tok_dir = os.path.join(SCRIPT_DIR, "tokens")
            os.makedirs(tok_dir, exist_ok=True)
            token_src = os.path.join(tok_dir, info["file"])
            content = env_raw if env_raw.startswith("{") else base64.b64decode(env_raw).decode("utf-8")
            with open(token_src, "w", encoding="utf-8") as f:
                f.write(content)

    if not token_src or not os.path.exists(token_src):
        raise FileNotFoundError(f"Account {account_num} token file not found in {candidate_paths}")

    cfg_dir = os.path.normpath(os.path.expanduser("~/.config/colab-cli"))
    os.makedirs(cfg_dir, exist_ok=True)
    dst_token = os.path.normpath(os.path.join(cfg_dir, "token.json"))
    shutil.copyfile(token_src, dst_token)
    try:
        os.chmod(dst_token, 0o600)
    except Exception:
        pass

    # Silence update check notice
    settings_path = os.path.normpath(os.path.join(cfg_dir, "settings.json"))
    try:
        with open(settings_path, "w", encoding="utf-8") as f:
            json.dump({"enable_update_check": False}, f)
    except Exception:
        pass

    log(f"Configured token for Account {account_num} ({info['email']}) -> {dst_token}")


def check_or_create_session(session_name):
    """Check if session is active; create if missing."""
    log(f"Checking status for session '{session_name}'...")
    code, out = run_colab(["status", "-s", session_name], timeout=30)
    out_lower = out.lower()

    # Verify if session actually exists and is active
    is_not_found = ("not found" in out_lower) or ("no active sessions" in out_lower)
    is_active = (code == 0) and (not is_not_found) and (("idle" in out_lower) or ("busy" in out_lower) or (session_name in out))

    if is_active:
        log(f"Session '{session_name}' is currently ACTIVE.")
        return True

    log(f"Session '{session_name}' is NOT active on backend. Allocating fresh session via Colab API...")
    code, out = run_colab(["new", "-s", session_name], timeout=150)
    log(f"colab new result:\n{out}")
    if code == 0 and ("ready" in out.lower() or "created" in out.lower() or "session" in out.lower()):
        log(f"Successfully provisioned fresh session '{session_name}'. Waiting 8s for container initialization...")
        time.sleep(8)
        return True
    else:
        # Check if session exists anyway via colab status
        check_code, check_out = run_colab(["status", "-s", session_name], timeout=30)
        if check_code == 0 and "not found" not in check_out.lower():
            log(f"Session '{session_name}' successfully verified active.")
            return True
        log(f"Failed to allocate session '{session_name}'! Error output:\n{out}")
        return False


def generate_node_info(account_num, session_name):
    """Save active session metadata to node_info.json for in-VM daemons."""
    tg_bot_token = os.environ.get("TG_BOT_TOKEN", "").strip()
    tg_chat_id = (os.environ.get("TG_CHAT_ID") or os.environ.get("TG_OWNER_ID") or "").strip()
    if not tg_bot_token or not tg_chat_id:
        for cfg_cand in [
            os.path.join(SCRIPT_DIR, "tg_config.json"),
            "/home/runner/workspace/colab_windows_pack/tg_config.json",
        ]:
            if os.path.exists(cfg_cand):
                try:
                    with open(cfg_cand, "r", encoding="utf-8") as f:
                        cfg = json.load(f)
                        if not tg_bot_token:
                            tg_bot_token = str(cfg.get("tg_bot_token", "")).strip()
                        if not tg_chat_id:
                            tg_chat_id = str(cfg.get("tg_chat_id", "")).strip()
                    break
                except Exception:
                    pass
    info = {
        "account": account_num,
        "email": ACCOUNTS.get(account_num, {}).get("email", f"Account {account_num}"),
        "session": session_name,
        "start_time_ist": time.strftime("%Y-%m-%d %I:%M:%S %p IST", time.gmtime(time.time() + 19800)),
        "target_hours": 8,
        "tg_bot_token": tg_bot_token,
        "tg_chat_id": tg_chat_id
    }
    path = os.path.join(SCRIPT_DIR, "node_info.json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(info, f, indent=2)
    except Exception as e:
        log(f"Warning: Could not save node_info.json: {e}")


def upload_all_scripts(session_name):
    """Upload all keepalive, rclone, and hydration scripts to /content/ on the VM."""
    log("Uploading operational scripts & configs to /content/...")

    # Ensure tunnel_token.txt exists locally if TUNNEL_TOKEN is configured
    local_tun = os.path.join(SCRIPT_DIR, "tunnel_token.txt")
    if not os.path.exists(local_tun) and TUNNEL_TOKEN:
        try:
            with open(local_tun, "w", encoding="utf-8") as f:
                f.write(TUNNEL_TOKEN.strip() + "\n")
        except Exception:
            pass

    # Ensure rclone.conf exists locally if available in standard Linux/Windows locations
    local_rclone = os.path.join(SCRIPT_DIR, "rclone.conf")
    if not os.path.exists(local_rclone):
        for r_cand in [
            os.path.expanduser("~/.config/rclone/rclone.conf"),
            "/home/runner/workspace/colab_windows_pack/rclone.conf",
            "/config/.config/rclone/rclone.conf",
        ]:
            if os.path.exists(r_cand):
                try:
                    shutil.copyfile(r_cand, local_rclone)
                    break
                except Exception:
                    pass

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

    success = True
    for fname, dst in files_to_upload:
        src = os.path.join(SCRIPT_DIR, fname)
        if os.path.exists(src):
            code, out = run_colab(["upload", "-s", session_name, src, dst], timeout=60)
            if code == 0:
                log(f"  [UPLOAD OK] {fname} -> {dst}")
            else:
                log(f"  [UPLOAD FAIL] {fname} -> {dst} (code={code}): {out}")
                success = False
        else:
            log(f"  [SKIP] Local file {fname} not found.")

    return success


def hydrate_session(session_name, force=False):
    """
    Executes the authoritative hydrate_and_setup.py on the Colab VM:
    - If force=False (Safe Smart Hydration): Skips tar extraction if /content/workspace
      is already populated, preserving active edits and running OpenVSCode/tunnel.
    - If force=True (Explicit Force Restore): Overwrites workspace from central Google Drive backup.
    """
    log("=== [HYDRATION] Executing hydrate_and_setup.py on Colab VM ===")
    hydrate_script = os.path.join(SCRIPT_DIR, "hydrate_and_setup.py")
    cmd = ["exec", "-s", session_name, "-f", hydrate_script, "--timeout", "180"]
    if force:
        log("Force restore enabled: passing --env FORCE_RESTORE=1 to VM hydration...")
        cmd.extend(["--env", "FORCE_RESTORE=1"])
    else:
        log("Safe smart hydration mode: live files and running services will be preserved.")

    code, out = run_colab_stream(cmd, timeout=200)
    log(f"Hydration completed (exit={code})")

    if code == 0 and ("AUTONOMOUS RELAY NODE INITIALIZED AND RUNNING CLEANLY" in out or "SAFE SKIP" in out):
        log("=== [SUCCESS] Google Colab Workspace Restored & Live! ===")
        log("Public Web IDE: https://colab.silver-xf.bond")
        return True
    elif "Local OpenVSCode HTTP status: 200" in out or "8443" in out:
        log("=== [SUCCESS] OpenVSCode is alive on port 8443! ===")
        log("Public Web IDE: https://colab.silver-xf.bond")
        return True
    else:
        log(f"WARNING: Hydration finished with exit code {code}.")
        return False


def backup_session(session_name):
    """Executes execute_backup.py on Colab VM to sync workspace back to Google Drive."""
    log(f"=== [BACKUP] Triggering Google Drive state sync on '{session_name}' ===")
    backup_script = os.path.join(SCRIPT_DIR, "execute_backup.py")
    code, out = run_colab(["exec", "-s", session_name, "-f", backup_script, "--timeout", "180"], timeout=200)
    log(f"Backup output (exit={code}):\n{out}")
    return code == 0


def run_keepalive_loop(account_num, session_name, max_hours=8.0):
    """
    Perpetual 40s active ping-pong duty cycle every 50 seconds via physical file (-f).
    Includes automatic Google Drive state backups every 60 cycles (~50 minutes).
    Returns True when max_hours is reached to advance the shift to the next account.
    """
    log(f"=== Starting Perpetual 50s Keepalive Duty Cycle for '{session_name}' (Account {account_num}) ===")
    log("Kernel will execute 40s compute pulses, remaining in BUSY state ~80% of every minute.")
    log(f"Shift Duration Target: {max_hours:.1f} Hours")
    cycle = 0
    shift_start = time.time()
    shift_duration_sec = max_hours * 3600.0

    while True:
        elapsed_shift = time.time() - shift_start
        if elapsed_shift >= shift_duration_sec:
            log(f"=== [SHIFT COMPLETE] Account {account_num} reached target duration ({max_hours:.1f}h) ===")
            log("Ensuring final pre-handover backup is completed on Google Drive...")
            try:
                backup_session(session_name)
            except Exception as e:
                log(f"Final backup note: {e}")
            return True

        cycle += 1
        script_name = "ping_a.py" if (cycle % 2 != 0) else "ping_b.py"
        script_path = os.path.join(SCRIPT_DIR, script_name)
        start = time.time()

        # Execute physical file directly on remote VM kernel
        code, out = run_colab(["exec", "-s", session_name, "-f", script_path, "--timeout", "70"], timeout=75)
        elapsed = time.time() - start
        summary = out.replace("\n", " | ")[:120]

        if code == 0:
            log(f"[Cycle {cycle:04d}] {script_name} exit={code} in {elapsed:.1f}s | {summary}")
        else:
            log(f"[Cycle {cycle:04d}] WARNING: {script_name} exit={code} in {elapsed:.1f}s | {summary}")
            if code == -1:
                log("Cleaning any hung execution processes...")
                if os.name != "nt":
                    subprocess.run(["pkill", "-f", f"colab exec -s {session_name}"], check=False)

        # Periodic state backup every 60 cycles (~50 minutes)
        if cycle % 60 == 0:
            log(f"[Auto-Backup] Periodic Google Drive backup checkpoint at cycle {cycle:04d}...")
            try:
                backup_session(session_name)
            except Exception as e:
                log(f"[Auto-Backup] Exception: {e}")

        spent = time.time() - start
        to_sleep = max(1.0, 50.0 - spent)
        time.sleep(to_sleep)


def verify_dependencies():
    missing = []
    for mod in ["click", "filelock", "requests", "jupyter_kernel_client", "pydantic", "typer", "websocket"]:
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    if missing:
        log("=================================================================")
        log(f"ERROR: Missing Python dependencies: {', '.join(missing)}")
        log("Please run '02_install_requirements.bat' to install required packages.")
        log("=================================================================")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Google Colab 24/7 Autonomous Relay & Keepalive Engine")
    parser.add_argument("--account", type=int, default=1, choices=[1, 2, 3], help="Account number (1, 2, or 3). Default: 1")
    parser.add_argument("--session", type=str, default="", help="Session name (Default: colab-prod-node<account>)")
    parser.add_argument("--bootstrap", action="store_true", help="Restore Google Drive backup and bootstrap OpenVSCode & Tunnel")
    parser.add_argument("--restore-backup", action="store_true", help="Restore Google Drive backup into the currently running session and exit without starting keepalive")
    parser.add_argument("--backup", action="store_true", help="Trigger an immediate Google Drive workspace state backup and exit")
    parser.add_argument("--skip-upload", action="store_true", help="Skip uploading operational scripts")
    parser.add_argument("--single-shift", action="store_true", help="Run only one 8-hour shift and exit without rotating")
    parser.add_argument("--hours", type=float, default=8.0, help="Shift duration in hours (Default: 8.0)")
    args = parser.parse_args()

    # 0. Verify required dependencies
    verify_dependencies()

    # Interactive prompt if account was not explicitly supplied via CLI flags
    if "--account" not in sys.argv:
        print("\n=================================================================")
        print("   Google Colab 24/7 Autonomous Relay Engine (Windows & Linux)   ")
        print("=================================================================")
        print("[?] Select Google Colab starting account:")
        for num, info in ACCOUNTS.items():
            print(f"    {num}) Account {num} ({info['email']})")
        try:
            raw_choice = input("\nEnter choice [1-3] (Default: 1, Press Enter): ").strip()
            if raw_choice in ["1", "2", "3"]:
                args.account = int(raw_choice)
            else:
                args.account = 1
        except Exception:
            args.account = 1

    if not args.session:
        args.session = f"colab-prod-node{args.account}"

    # Handle standalone restore backup request on running session
    if args.restore_backup:
        setup_account_token(args.account)
        log(f"=== Restoring Google Drive Backup to Active Session '{args.session}' (FORCE MODE) ===")
        upload_all_scripts(args.session)
        success = hydrate_session(args.session, force=True)
        log(f"Restore finished (status={success}).")
        return

    # Handle standalone backup request
    if args.backup:
        setup_account_token(args.account)
        log("Executing on-demand backup...")
        upload_all_scripts(args.session)
        success = backup_session(args.session)
        log(f"On-demand backup finished (status={success}).")
        return

    current_account = args.account
    current_session = args.session

    # Autonomous multi-account relay loop (Account 1 -> Account 2 -> Account 3 -> Account 1)
    while True:
        log("=================================================================")
        log("  Google Colab Autonomous Relay Engine (Windows & Linux Runner)  ")
        log(f"  Active Account: {current_account} ({ACCOUNTS[current_account]['email']})")
        log(f"  Active Session: {current_session}")
        log(f"  Shift Duration: {args.hours:.1f} Hours")
        log("=================================================================")

        # 1. Configure account token
        setup_account_token(current_account)

        # 2. Check or create session
        if not check_or_create_session(current_session):
            log(f"ERROR: Could not establish session for Account {current_account}. Retrying in 30s...")
            time.sleep(30)
            continue

        # 3. Generate node_info.json and upload all operational scripts
        generate_node_info(current_account, current_session)
        if not args.skip_upload:
            upload_all_scripts(current_session)

        # 4. Hydrate session: Safe smart bootstrap (preserves workspace if already populated)
        # On fresh rotation, this pulls the newly backed up colab_bundle.tar.gz from Google Drive
        hydrate_session(current_session, force=False)

        # 5. Start perpetual keepalive loop for the duration of the shift
        run_keepalive_loop(current_account, current_session, max_hours=args.hours)

        if args.single_shift:
            log(f"Single-shift flag set: shift for Account {current_account} completed. Exiting.")
            break

        # 6. Shift rotation: advance to next account (1 -> 2 -> 3 -> 1)
        prev_account = current_account
        prev_session = current_session
        current_account = (prev_account % 3) + 1
        current_session = f"colab-prod-node{current_account}"
        log(f"=== [SHIFT ROTATION] Handing over from Account {prev_account} to Account {current_account} ===")

        # Cleanly stop old session to conserve compute and avoid duplicate active sessions
        try:
            log(f"Releasing old session '{prev_session}' on Account {prev_account} for cooldown...")
            setup_account_token(prev_account)
            run_colab(["stop", "-s", prev_session], timeout=30)
        except Exception as e:
            log(f"Note stopping old session: {e}")


if __name__ == "__main__":
    main()
