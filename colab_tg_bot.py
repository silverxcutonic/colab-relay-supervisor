#!/usr/bin/env python3
"""
Google Colab In-VM Telegram Bot Supervisor & Handover Monitor
- Runs directly inside the active Colab container (/content/colab_tg_bot.py).
- Microsecond-accurate shift tracking via /proc/uptime (8-hour shift cycle).
- Automated Telegram telemetry:
    * Startup shift announcement with Account email & Web IDE link.
    * 2-hour periodic heartbeats (2h, 4h, 6h).
    * Handover countdown cascade: 30m, 10m, 5m, 2m (Red Alert), 0m (Freeze & Backup).
- Out-of-band disaster recovery: Inline button [⚡ Trigger Immediate Backup] allows
  instant one-tap Google Drive checkpointing directly from mobile even if local laptop dies.
"""

import os
import sys
import json
import time
import urllib.request
import urllib.parse
import threading
import subprocess
import argparse

NODE_INFO_FILE = "/content/node_info.json"
SHIFT_DURATION = 8 * 3600  # 8 hours = 28,800 seconds

ACCOUNTS = {
    1: "sagesilveryt@gmail.com",
    2: "aravprakashsingh2@gmail.com",
    3: "recapmanhua72@gmail.com"
}


def get_tg_credentials():
    """Resolve Telegram Bot Token and Chat ID from env, /content/node_info.json, or local tg_config.json."""
    bot_token = os.environ.get("TG_BOT_TOKEN", "").strip()
    chat_id = (os.environ.get("TG_CHAT_ID") or os.environ.get("TG_OWNER_ID") or "").strip()
    for cfg_file in [
        NODE_INFO_FILE,
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "node_info.json"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "tg_config.json"),
    ]:
        if (not bot_token or not chat_id) and os.path.exists(cfg_file):
            try:
                with open(cfg_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if not bot_token:
                        bot_token = str(data.get("tg_bot_token", "")).strip()
                    if not chat_id:
                        chat_id = str(data.get("tg_chat_id", "")).strip()
            except Exception:
                pass
    return bot_token, chat_id


BOT_TOKEN, CHAT_ID = get_tg_credentials()

backup_lock = threading.Lock()
is_backing_up = False


def log(msg):
    ist_time = time.gmtime(time.time() + 19800)
    now = time.strftime("%Y-%m-%d %I:%M:%S %p IST", ist_time)
    print(f"[{now}] [TG-BOT] {msg}", flush=True)


def get_ist_str():
    ist_time = time.gmtime(time.time() + 19800)
    return time.strftime("%Y-%m-%d %I:%M:%S %p IST", ist_time)


def get_uptime_seconds():
    try:
        with open("/proc/uptime", "r") as f:
            return float(f.readline().split()[0])
    except Exception:
        return 0.0


def format_duration(seconds):
    seconds = max(0, int(seconds))
    hrs = seconds // 3600
    mins = (seconds % 3600) // 60
    secs = seconds % 60
    if hrs > 0:
        return f"{hrs}h {mins}m {secs}s"
    return f"{mins}m {secs}s"


def tg_api_call(method, payload):
    global BOT_TOKEN, CHAT_ID
    if not BOT_TOKEN or not CHAT_ID:
        BOT_TOKEN, CHAT_ID = get_tg_credentials()
    if not BOT_TOKEN:
        log(f"Telegram API call '{method}' skipped: TG_BOT_TOKEN not configured.")
        return None
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        log(f"Telegram API call '{method}' failed: {e}")
        return None


def send_message(text, reply_markup=None):
    global BOT_TOKEN, CHAT_ID
    if not BOT_TOKEN or not CHAT_ID:
        BOT_TOKEN, CHAT_ID = get_tg_credentials()
    if not CHAT_ID:
        log("send_message skipped: TG_CHAT_ID not configured.")
        return None
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return tg_api_call("sendMessage", payload)


def answer_callback(callback_id, text=None):
    payload = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text
        payload["show_alert"] = False
    return tg_api_call("answerCallbackQuery", payload)


def get_backup_keyboard():
    return {
        "inline_keyboard": [[
            {"text": "⚡ Trigger Immediate Backup", "callback_data": "trigger_backup"}
        ]]
    }


def execute_gdrive_backup(trigger_reason="Telegram Trigger"):
    global is_backing_up
    with backup_lock:
        if is_backing_up:
            return False, "Backup already in progress!"
        is_backing_up = True

    try:
        log(f"Executing Google Drive backup: {trigger_reason}")
        start_t = time.time()

        # Use sync_state.sh or execute_backup.py
        if os.path.exists("/content/sync_state.sh"):
            cmd = ["bash", "/content/sync_state.sh"]
        elif os.path.exists("/content/execute_backup.py"):
            cmd = [sys.executable, "/content/execute_backup.py"]
        else:
            return False, "Backup scripts not found in /content/"

        env = os.environ.copy()
        if "/usr/local/bin" not in env.get("PATH", ""):
            env["PATH"] = f"/usr/local/bin:/usr/bin:/bin:{env.get('PATH', '')}"

        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=180, env=env)
        duration = time.time() - start_t

        if proc.returncode == 0:
            log(f"Backup succeeded in {duration:.1f}s")
            return True, f"Synced to Google Drive in {duration:.1f}s"
        else:
            log(f"Backup failed (exit {proc.returncode}):\n{proc.stdout}")
            return False, f"Exit code {proc.returncode}"
    except Exception as e:
        log(f"Backup exception: {e}")
        return False, str(e)
    finally:
        with backup_lock:
            is_backing_up = False


def poll_updates_worker():
    """Background thread polling for inline button presses."""
    global BOT_TOKEN, CHAT_ID
    offset = 0
    log("Telegram inline callback listener thread started.")
    while True:
        try:
            if not BOT_TOKEN or not CHAT_ID:
                BOT_TOKEN, CHAT_ID = get_tg_credentials()
            if not BOT_TOKEN:
                time.sleep(10)
                continue
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates?timeout=20&offset={offset}"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            if data and data.get("ok"):
                for update in data.get("result", []):
                    offset = max(offset, update["update_id"] + 1)
                    if "callback_query" in update:
                        cb = update["callback_query"]
                        cb_id = cb["id"]
                        data_val = cb.get("data")
                        user_name = cb.get("from", {}).get("first_name", "Commander")

                        if data_val == "trigger_backup":
                            answer_callback(cb_id, text="⚡ Initiating immediate Google Drive backup...")
                            send_message(f"⚡ *Immediate Backup Triggered by {user_name}!* Archiving `/content/workspace`...")
                            ok, msg = execute_gdrive_backup(trigger_reason=f"Telegram Button by {user_name}")
                            if ok:
                                send_message(
                                    f"✅ *Google Drive Backup Succeeded!*\n"
                                    f"⏱️ {msg}\n"
                                    f"🕒 *Time:* `{get_ist_str()}`\n"
                                    f"📦 Destination: `gdrive:coolify-relay-state/colab-relay-state/colab_bundle.tar.gz`",
                                    reply_markup=get_backup_keyboard()
                                )
                            else:
                                send_message(
                                    f"❌ *Backup Failed:* {msg}\n"
                                    f"🕒 *Time:* `{get_ist_str()}`",
                                    reply_markup=get_backup_keyboard()
                                )
        except Exception as e:
            # Prevent rapid retry spam on network drop
            time.sleep(5)


def run_monitor_loop(account_num, session_name):
    email = ACCOUNTS.get(account_num, f"Account {account_num}")
    log(f"Starting Colab In-VM Supervisor: Account {account_num} ({email}), Session: {session_name}")

    # Determine shift start epoch timestamp
    shift_epoch = None
    epoch_file = "/content/.shift_start_epoch"
    if os.path.exists(epoch_file):
        try:
            with open(epoch_file, "r") as f:
                shift_epoch = float(f.read().strip())
        except Exception:
            pass
    if shift_epoch is None and os.path.exists(NODE_INFO_FILE):
        try:
            with open(NODE_INFO_FILE, "r") as f:
                info = json.load(f)
                if "shift_start_epoch" in info:
                    shift_epoch = float(info["shift_start_epoch"])
        except Exception:
            pass

    if shift_epoch is None:
        # Fallback to shift start uptime anchor
        raw_uptime = get_uptime_seconds()
        shift_anchor_file = "/content/.shift_start_uptime"
        if os.path.exists(shift_anchor_file):
            try:
                with open(shift_anchor_file, "r") as af:
                    shift_start_uptime = float(af.read().strip())
            except Exception:
                shift_start_uptime = raw_uptime
        else:
            shift_start_uptime = raw_uptime
            try:
                with open(shift_anchor_file, "w") as af:
                    af.write(str(shift_start_uptime))
            except Exception:
                pass
        shift_epoch = time.time() - max(0.0, raw_uptime - shift_start_uptime)
        try:
            with open(epoch_file, "w") as f:
                f.write(str(shift_epoch))
        except Exception:
            pass

    current_elapsed = max(0.0, time.time() - shift_epoch)
    raw_uptime = get_uptime_seconds()

    startup_msg = (
        f"🚀 *Colab Relay Node Online!* (Shift Active)\n\n"
        f"👤 *Account:* Account {account_num} (`{email}`)\n"
        f"📦 *Session:* `{session_name}`\n"
        f"🌐 *Web IDE:* https://colab.silver-xf.bond\n"
        f"⏱️ *Shift Elapsed:* `{format_duration(current_elapsed)}` (VM Uptime: `{format_duration(raw_uptime)}`)\n"
        f"🎯 *Shift Target:* 8 Hours (Continuous)\n"
        f"🕒 *Current Time:* `{get_ist_str()}`\n\n"
        f"💡 *Mobile Fail-Safe:* Tap the button below anytime to checkpoint your workspace to Google Drive."
    )
    send_message(startup_msg, reply_markup=get_backup_keyboard())

    # Alert milestones (elapsed seconds since shift start)
    milestones = {
        7200: ("2h", False),
        14400: ("4h", False),
        21600: ("6h", False),
        27000: ("30m_left", False),  # 7h 30m
        28200: ("10m_left", False),  # 7h 50m
        28500: ("5m_left", False),   # 7h 55m
        28680: ("2m_left", False),   # 7h 58m
        28800: ("0m_end", False)     # 8h 00m
    }

    # Catch-up check: If 2h milestone has passed and was never sent, send it immediately
    for threshold, (label, _) in list(milestones.items()):
        flag_file = f"/content/.milestone_{threshold}_sent"
        if os.path.exists(flag_file):
            milestones[threshold] = (label, True)
        elif current_elapsed >= threshold and threshold <= 21600:
            # Milestone has passed but never notified; dispatch catch-up alert
            milestones[threshold] = (label, True)
            try:
                with open(flag_file, "w") as f:
                    f.write(str(time.time()))
            except Exception:
                pass
            rem = max(0, SHIFT_DURATION - current_elapsed)
            send_message(
                f"⏱️ *Colab Shift Checkpoint: {label.upper()} / 8h (Live Status)*\n\n"
                f"👤 *Account:* Account {account_num} (`{email}`)\n"
                f"📈 *Shift Elapsed:* `{format_duration(current_elapsed)}`\n"
                f"⏳ *Time Remaining:* `{format_duration(rem)}`\n"
                f"🌐 *Web IDE:* https://colab.silver-xf.bond\n"
                f"🟢 *Status:* Healthy & Locked in Keepalive",
                reply_markup=get_backup_keyboard()
            )

    while True:
        uptime = max(0.0, time.time() - shift_epoch)
        remaining = max(0, SHIFT_DURATION - uptime)

        # 2-Hour Checkpoint
        if uptime >= 7200 and not milestones[7200][1]:
            milestones[7200] = ("2h", True)
            try:
                with open("/content/.milestone_7200_sent", "w") as f:
                    f.write(str(time.time()))
            except Exception:
                pass
            send_message(
                f"⏱️ *Colab Shift Checkpoint: 2h / 8h*\n\n"
                f"👤 *Account:* Account {account_num} (`{email}`)\n"
                f"📈 *VM Uptime:* `{format_duration(uptime)}`\n"
                f"⏳ *Time Remaining:* `{format_duration(remaining)}`\n"
                f"🌐 *Web IDE:* https://colab.silver-xf.bond\n"
                f"🟢 *Status:* Healthy & Locked in Keepalive",
                reply_markup=get_backup_keyboard()
            )

        # 4-Hour Checkpoint
        if uptime >= 14400 and not milestones[14400][1]:
            milestones[14400] = ("4h", True)
            send_message(
                f"⏱️ *Colab Shift Checkpoint: 4h / 8h (Halfway!)*\n\n"
                f"👤 *Account:* Account {account_num} (`{email}`)\n"
                f"📈 *VM Uptime:* `{format_duration(uptime)}`\n"
                f"⏳ *Time Remaining:* `{format_duration(remaining)}`\n"
                f"🌐 *Web IDE:* https://colab.silver-xf.bond\n"
                f"🟢 *Status:* Healthy & Locked in Keepalive",
                reply_markup=get_backup_keyboard()
            )

        # 6-Hour Checkpoint
        if uptime >= 21600 and not milestones[21600][1]:
            milestones[21600] = ("6h", True)
            send_message(
                f"⏱️ *Colab Shift Checkpoint: 6h / 8h*\n\n"
                f"👤 *Account:* Account {account_num} (`{email}`)\n"
                f"📈 *VM Uptime:* `{format_duration(uptime)}`\n"
                f"⏳ *Time Remaining:* `{format_duration(remaining)}`\n"
                f"🌐 *Web IDE:* https://colab.silver-xf.bond\n"
                f"🟢 *Status:* Healthy & Locked in Keepalive",
                reply_markup=get_backup_keyboard()
            )

        # T-30m Warning
        if uptime >= 27000 and not milestones[27000][1]:
            milestones[27000] = ("30m_left", True)
            send_message(
                f"⚠️ *Colab Handover Warning: 30 Minutes Remaining!*\n\n"
                f"👤 *Account:* Account {account_num} (`{email}`)\n"
                f"⏳ *Time Remaining:* ~30 minutes\n"
                f"💡 *Action:* Wrap up any active downloads or long terminal commands.",
                reply_markup=get_backup_keyboard()
            )

        # T-10m Warning
        if uptime >= 28200 and not milestones[28200][1]:
            milestones[28200] = ("10m_left", True)
            send_message(
                f"⚠️ *Colab Handover Warning: 10 Minutes Remaining!*\n\n"
                f"👤 *Account:* Account {account_num} (`{email}`)\n"
                f"⏳ *Time Remaining:* ~10 minutes\n"
                f"💡 *Action:* Handover sequence will begin shortly.",
                reply_markup=get_backup_keyboard()
            )

        # T-5m Warning
        if uptime >= 28500 and not milestones[28500][1]:
            milestones[28500] = ("5m_left", True)
            send_message(
                f"⚠️ *Colab Handover Alert: 5 Minutes Remaining!*\n\n"
                f"👤 *Account:* Account {account_num} (`{email}`)\n"
                f"⏳ *Time Remaining:* ~5 minutes\n"
                f"💾 *Action:* Save all open files in OpenVSCode now.",
                reply_markup=get_backup_keyboard()
            )

        # T-2m Red Alert
        if uptime >= 28680 and not milestones[28680][1]:
            milestones[28680] = ("2m_left", True)
            send_message(
                f"🚨 *RED ALERT: 2 MINUTES REMAINING!* 🚨\n\n"
                f"👤 *Account:* Account {account_num} (`{email}`)\n"
                f"⏳ *Time Remaining:* ~120 seconds\n"
                f"🔒 *Action:* Container workspace will freeze and auto-sync to Google Drive at T-0!",
                reply_markup=get_backup_keyboard()
            )

        # T-0m Handover Freeze & Backup
        if uptime >= 28800 and not milestones[28800][1]:
            milestones[28800] = ("0m_end", True)
            send_message(
                f"📦 *Colab 8-Hour Shift Complete! (T-0 reached)*\n\n"
                f"👤 *Account:* Account {account_num} (`{email}`)\n"
                f"🛑 Freezing workspace disk writes and executing final state backup to Google Drive..."
            )
            ok, msg = execute_gdrive_backup(trigger_reason="8-Hour Shift Auto Handover")
            if ok:
                send_message(
                    f"✅ *Final Shift Backup Completed Successfully!*\n"
                    f"⏱️ {msg}\n"
                    f"🕒 *Time:* `{get_ist_str()}`\n"
                    f"🔄 Safe for successor node hydration."
                )
            else:
                send_message(
                    f"❌ *Final Shift Backup Warning:* {msg}\n"
                    f"🕒 *Time:* `{get_ist_str()}`"
                )
            log("8-Hour shift concluded and final backup completed.")
            break

        time.sleep(10)


def main():
    parser = argparse.ArgumentParser(description="Google Colab In-VM Telegram Bot Supervisor")
    parser.add_argument("--account", type=int, default=1, choices=[1, 2, 3], help="Account number (1, 2, or 3)")
    parser.add_argument("--session", type=str, default="colab-prod-node1", help="Session name")
    args = parser.parse_args()

    # Read node info from /content/node_info.json if present
    account_num = args.account
    session_name = args.session
    if os.path.exists(NODE_INFO_FILE):
        try:
            with open(NODE_INFO_FILE, "r", encoding="utf-8") as f:
                info = json.load(f)
                account_num = info.get("account", account_num)
                session_name = info.get("session", session_name)
        except Exception as e:
            log(f"Note: Could not parse {NODE_INFO_FILE}: {e}")

    # 1. Start background thread for Telegram inline callback button listener
    t_poll = threading.Thread(target=poll_updates_worker, daemon=True)
    t_poll.start()

    # 2. Run the main monitor and timer loop
    try:
        run_monitor_loop(account_num, session_name)
    except KeyboardInterrupt:
        log("Received KeyboardInterrupt, stopping supervisor.")
    except Exception as e:
        log(f"Supervisor crash: {e}")
        send_message(f"⚠️ *Colab Supervisor Crash Alert:* `{e}`\n🕒 `{get_ist_str()}`")


if __name__ == "__main__":
    main()
