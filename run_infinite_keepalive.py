#!/usr/bin/env python3
"""
Perpetual Robust Keepalive Daemon for Google Colab Sessions (Linux & Cross-Platform)
- Uses bundled colab_cli module via active Python interpreter (zero $PATH guessing)
- Alternates ping_a.py and ping_b.py physical file (-f) compute ticks (40s active / 50s cycle)
- Periodic 60-cycle (~50 minute) Google Drive state checkpoint backups via execute_backup.py
- Enforces Indian Standard Time (IST, 12-hour AM/PM) across all logs
"""
import os
import sys
import time
import shutil
import subprocess

APP_DIR = os.path.dirname(os.path.abspath(__file__))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

os.environ["COLAB_HEADLESS"] = "1"

SESSION_NAME = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("COLAB_SESSION", "colab-prod-node1")
LOG_FILE = os.environ.get("KEEPALIVE_LOG", "/tmp/colab_keepalive.log")


def get_ist_str():
    ist_time = time.gmtime(time.time() + 19800)
    return time.strftime("%Y-%m-%d %I:%M:%S %p IST", ist_time)


def log(msg):
    now = get_ist_str()
    entry = f"[{now}] {msg}\n"
    sys.stdout.write(entry)
    sys.stdout.flush()
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(entry)
    except Exception:
        pass


def get_colab_cmd():
    bundled_pkg = os.path.join(APP_DIR, "colab_cli")
    if os.path.exists(bundled_pkg):
        return [sys.executable, "-m", "colab_cli.cli"]
    colab_bin = shutil.which("colab")
    if colab_bin:
        return [colab_bin]
    return [sys.executable, "-m", "colab_cli.cli"]


def run_colab(cmd_args, timeout=75):
    full_cmd = get_colab_cmd() + cmd_args
    env = os.environ.copy()
    env["COLAB_HEADLESS"] = "1"
    curr_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{APP_DIR}{os.pathsep}{curr_pp}" if curr_pp else APP_DIR
    return subprocess.run(
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


def main():
    log(f"=== Starting Perpetual Colab Keepalive Daemon for '{SESSION_NAME}' ===")
    log(f"Colab CLI invocation: {' '.join(get_colab_cmd())}")
    cycle = 0

    while True:
        cycle += 1
        name = "ping_a.py" if (cycle % 2 != 0) else "ping_b.py"
        script_path = os.path.join(APP_DIR, name)
        start = time.time()

        try:
            r = run_colab(["exec", "-s", SESSION_NAME, "-f", script_path, "--timeout", "70"], timeout=75)
            elapsed = time.time() - start
            out_summary = r.stdout.strip().replace("\n", " | ")[:120]
            log(f"[Cycle {cycle:04d}] {name} exit={r.returncode} in {elapsed:.1f}s | {out_summary}")
        except subprocess.TimeoutExpired:
            elapsed = time.time() - start
            log(f"WARNING: Keepalive cycle {cycle:04d} timed out after {elapsed:.1f}s; cleaning hanging child processes...")
            if os.name != "nt":
                subprocess.run(["pkill", "-f", f"exec -s {SESSION_NAME}"], check=False)
        except Exception as e:
            elapsed = time.time() - start
            log(f"ERROR: Exception executing {name}: {e} in {elapsed:.1f}s")

        # Periodic state backup every 60 cycles (~50 minutes)
        if cycle % 60 == 0:
            backup_script = os.path.join(APP_DIR, "execute_backup.py")
            if os.path.exists(backup_script):
                log(f"[Auto-Backup] Executing periodic 60-cycle checkpoint on '{SESSION_NAME}'...")
                try:
                    b_res = run_colab(["exec", "-s", SESSION_NAME, "-f", backup_script, "--timeout", "180"], timeout=200)
                    log(f"[Auto-Backup] Completed (exit={b_res.returncode})")
                except Exception as e:
                    log(f"[Auto-Backup] Exception: {e}")

        spent = time.time() - start
        to_sleep = max(1.0, 50.0 - spent)
        time.sleep(to_sleep)


if __name__ == "__main__":
    main()
