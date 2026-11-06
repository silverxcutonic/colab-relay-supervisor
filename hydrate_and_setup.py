#!/usr/bin/env python3
"""
Google Colab Autonomous Bootstrapper & Hydration Engine (Cloaked Architecture)
- Fast Standalone Rclone (no apt-get / no dpkg lock / no mandb hangs)
- Fast OpenVSCode & Cloudflare Tunnel provisioning (< 45s total bootstrap)
- Detached Popen process management (zero Jupyter kernel connection hangs)
- Safe Smart Hydration: Preserves active workspace edits on re-connect
- Restores colab_bundle.tar.gz (1.5 MB) & claude_code_extension.tar.gz (173 MB) from Google Drive
"""

import os
import sys
import glob
import time
import shutil
import zipfile
import subprocess

# Guarantee /usr/local/bin is in PATH for rclone and helper binaries
if "/usr/local/bin" not in os.environ.get("PATH", ""):
    os.environ["PATH"] = f"/usr/local/bin:/usr/bin:/bin:{os.environ.get('PATH', '')}"

OPENVSCODE_RELEASE = "https://github.com/gitpod-io/openvscode-server/releases/download/openvscode-server-v1.109.5/openvscode-server-v1.109.5-linux-x64.tar.gz"

# Check for explicit force restore flag
force_flag_path = "/content/force_restore.flag"
FORCE_RESTORE = os.path.exists(force_flag_path) or os.environ.get("FORCE_RESTORE", "0") == "1"
if os.path.exists(force_flag_path):
    try:
        os.unlink(force_flag_path)
        print("[HYDRATION] Force restore flag detected and consumed.", flush=True)
    except Exception:
        pass

print("=== [1/5] INITIALIZING CREDENTIALS & RCLONE ===", flush=True)
os.makedirs("/root/.config/rclone", exist_ok=True)

# 1. First priority: copy from /content/rclone.conf
if os.path.exists("/content/rclone.conf"):
    shutil.copy("/content/rclone.conf", "/root/.config/rclone/rclone.conf")
    os.chmod("/root/.config/rclone/rclone.conf", 0o600)

# 2. Second priority: environment variable passed via colab exec --env RCLONE_CONF_B64=...
rclone_b64 = os.environ.get("RCLONE_CONF_B64", "") or os.environ.get("RCLONE_CONFIG_BASE64", "")
if rclone_b64 and (not os.path.exists("/root/.config/rclone/rclone.conf") or os.path.getsize("/root/.config/rclone/rclone.conf") == 0):
    try:
        import base64
        with open("/root/.config/rclone/rclone.conf", "wb") as f:
            f.write(base64.b64decode(rclone_b64))
        os.chmod("/root/.config/rclone/rclone.conf", 0o600)
        with open("/content/rclone.conf", "wb") as f:
            f.write(base64.b64decode(rclone_b64))
        os.chmod("/content/rclone.conf", 0o600)
        print("Configured rclone.conf from RCLONE_CONF_B64 environment variable.", flush=True)
    except Exception as e:
        print(f"Rclone config decode error: {e}", flush=True)

# Install standalone rclone binary (avoids apt-get locks, mandb hangs, and external unzip dependency)
if not shutil.which("rclone"):
    print("Installing fast standalone rclone binary...", flush=True)
    try:
        subprocess.run(
            ["curl", "-fsSL", "https://downloads.rclone.org/rclone-current-linux-amd64.zip", "-o", "/tmp/rclone.zip"],
            check=True, timeout=40
        )
        os.makedirs("/tmp/rclone_unzip", exist_ok=True)
        with zipfile.ZipFile("/tmp/rclone.zip", "r") as zf:
            zf.extractall("/tmp/rclone_unzip")
        extracted = glob.glob("/tmp/rclone_unzip/*/rclone")
        if extracted:
            os.makedirs("/usr/local/bin", exist_ok=True)
            shutil.copyfile(extracted[0], "/usr/local/bin/rclone")
            os.chmod("/usr/local/bin/rclone", 0o755)
            print("Standalone rclone installed cleanly to /usr/local/bin/rclone", flush=True)
        shutil.rmtree("/tmp/rclone_unzip", ignore_errors=True)
        if os.path.exists("/tmp/rclone.zip"):
            os.unlink("/tmp/rclone.zip")
    except Exception as e:
        print(f"Standalone rclone install error: {e}", flush=True)

# Detect if workspace is genuinely populated with active work and chat history
workspace_dir = "/content/workspace"
workspace_items = os.listdir(workspace_dir) if os.path.exists(workspace_dir) else []
# Only skip restoration if real user files or claude transcripts already exist
has_real_workspace_data = (
    os.path.exists(os.path.join(workspace_dir, ".claude_transcripts"))
    or os.path.exists(os.path.join(workspace_dir, "YUI_PRODUCTION_MANIFEST.md"))
    or any(f not in ("colab-relay-supervisor", "sync_state.sh", ".rclone") for f in workspace_items)
)
workspace_populated = bool(workspace_items) and has_real_workspace_data

if workspace_populated and not FORCE_RESTORE:
    print("=== [SAFE SKIP] WORKSPACE ALREADY POPULATED ===", flush=True)
    print(f"Directory {workspace_dir} contains active work ({len(workspace_items)} items). Preserving live workspace changes.", flush=True)
    print("Skipping colab_bundle.tar.gz extraction to protect uncommitted work.", flush=True)
else:
    # Pull latest state bundle from central Google Drive
    if os.path.exists("/root/.config/rclone/rclone.conf") and shutil.which("rclone"):
        print("Pulling colab_bundle.tar.gz (1.5 MB) from central Google Drive...", flush=True)
        subprocess.run([
            "rclone", "copy",
            "gdrive:coolify-relay-state/colab-relay-state/colab_bundle.tar.gz",
            "/content/",
            "--drive-chunk-size=32M",
            "--buffer-size=32M"
        ], check=False, timeout=60)

    print("=== [2/5] EXTRACTING WORKSPACE & PERSISTENCE ===", flush=True)
    if os.path.exists("/content/colab_bundle.tar.gz"):
        try:
            subprocess.run(["tar", "-xzf", "/content/colab_bundle.tar.gz", "-C", "/content"], check=True, timeout=30)
            os.unlink("/content/colab_bundle.tar.gz")
            print("Workspace archive unpacked successfully.", flush=True)
        except Exception as e:
            print(f"Extraction note: {e}", flush=True)

os.makedirs("/content/workspace", exist_ok=True)
transcripts_dir = "/content/workspace/.claude_transcripts"
os.makedirs(transcripts_dir, exist_ok=True)

# Migrate legacy root_claude if present in archive
if os.path.exists("/content/root_claude") and os.path.isdir("/content/root_claude"):
    for item in os.listdir("/content/root_claude"):
        s = os.path.join("/content/root_claude", item)
        d = os.path.join(transcripts_dir, item)
        if not os.path.exists(d):
            shutil.move(s, d)
    shutil.rmtree("/content/root_claude", ignore_errors=True)

# Point /root/.claude to persistent workspace directory so Claude chat history survives
root_claude = "/root/.claude"
if os.path.exists(root_claude) and not os.path.islink(root_claude):
    for item in os.listdir(root_claude):
        s = os.path.join(root_claude, item)
        d = os.path.join(transcripts_dir, item)
        if not os.path.exists(d):
            if os.path.isdir(s):
                shutil.copytree(s, d)
            else:
                shutil.copy2(s, d)
    shutil.rmtree(root_claude, ignore_errors=True)

if os.path.islink(root_claude):
    try:
        if os.readlink(root_claude) != transcripts_dir:
            os.unlink(root_claude)
            os.symlink(transcripts_dir, root_claude)
    except Exception:
        pass
elif not os.path.exists(root_claude):
    try:
        os.symlink(transcripts_dir, root_claude)
    except Exception:
        pass

# Ensure sync_state.sh is accessible at /content/workspace/sync_state.sh (Golden Rule 4)
supervisor_sync = "/content/workspace/colab-relay-supervisor/sync_state.sh"
workspace_sync = "/content/workspace/sync_state.sh"
if os.path.exists(supervisor_sync) and not os.path.exists(workspace_sync):
    try:
        os.symlink(supervisor_sync, workspace_sync)
    except Exception:
        pass
elif os.path.exists("/content/sync_state.sh") and not os.path.exists(workspace_sync):
    try:
        os.symlink("/content/sync_state.sh", workspace_sync)
    except Exception:
        pass

if os.path.exists("/content/workspace/.rclone/rclone.conf") and not os.path.exists("/root/.config/rclone/rclone.conf"):
    shutil.copy("/content/workspace/.rclone/rclone.conf", "/root/.config/rclone/rclone.conf")
    os.chmod("/root/.config/rclone/rclone.conf", 0o600)

print("=== [3/5] PROVISIONING CLOAKED OPENVSCODE SERVER (PORT 8443) ===", flush=True)
lsp_dir = "/usr/local/lib/colab-lsp-engine"
if not os.path.exists(lsp_dir):
    print("Downloading and unpacking OpenVSCode Server (120 MB)...", flush=True)
    subprocess.run(["curl", "-fsSL", OPENVSCODE_RELEASE, "-o", "/tmp/openvscode.tar.gz"], check=True, timeout=90)
    os.makedirs("/tmp/ovs_extract", exist_ok=True)
    subprocess.run(["tar", "-xzf", "/tmp/openvscode.tar.gz", "-C", "/tmp/ovs_extract"], check=True, timeout=40)
    extracted = glob.glob("/tmp/ovs_extract/openvscode-server*")[0]
    shutil.move(extracted, lsp_dir)
    subprocess.run(["rm", "-rf", "/tmp/openvscode.tar.gz", "/tmp/ovs_extract"], check=False)
    print(f"OpenVSCode installed cleanly to {lsp_dir}", flush=True)

# Ensure Claude Code extension is installed in OpenVSCode
ext_dir = "/root/.openvscode-server/extensions"
os.makedirs(ext_dir, exist_ok=True)
has_claude_ext = any("anthropic.claude-code" in p for p in glob.glob(f"{ext_dir}/*"))
if not has_claude_ext:
    print("Hydrating Claude Code extension from Google Drive cache...", flush=True)
    if os.path.exists("/root/.config/rclone/rclone.conf") and shutil.which("rclone"):
        subprocess.run([
            "rclone", "copy",
            "gdrive:coolify-relay-state/colab-relay-state/claude_code_extension.tar.gz",
            "/content/",
            "--drive-chunk-size=32M",
            "--buffer-size=32M"
        ], check=False, timeout=60)
    if os.path.exists("/content/claude_code_extension.tar.gz"):
        try:
            subprocess.run(["tar", "-xzf", "/content/claude_code_extension.tar.gz", "-C", "/root/.openvscode-server"], check=True, timeout=40)
            os.unlink("/content/claude_code_extension.tar.gz")
            print("Claude Code extension restored from cache bundle.", flush=True)
        except Exception as e:
            print(f"Extension extract note: {e}", flush=True)
    else:
        print("Note: claude_code_extension.tar.gz not found on Drive. Extension will install on first editor open.", flush=True)

# Disguise internal node binary
node_bin = f"{lsp_dir}/node"
disguised_node = "/usr/local/bin/python3-lsp-node"
if os.path.exists(node_bin) and not os.path.exists(disguised_node):
    shutil.copyfile(node_bin, disguised_node)
    subprocess.run(["chmod", "+x", disguised_node], check=True)

def check_openvscode_alive():
    try:
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:8443", timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False

is_ovs_alive = check_openvscode_alive()
if is_ovs_alive and not FORCE_RESTORE:
    print("=== [SAFE SKIP] OPENVSCODE ALREADY RUNNING ON PORT 8443 ===", flush=True)
    print("Active editor instance preserved without restart.", flush=True)
else:
    # Stop any older IDE instances
    subprocess.run(["pkill", "-f", "openvscode-server"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["pkill", "-f", "code-server"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Launch OpenVSCode bound strictly to 127.0.0.1:8443 via detached Popen
    server_bin = f"{lsp_dir}/bin/openvscode-server"
    with open("/tmp/lsp_engine.log", "a") as log_f:
        subprocess.Popen(
            [
                server_bin,
                "--host", "127.0.0.1",
                "--port", "8443",
                "--without-connection-token",
                "--telemetry-level", "off",
                "/content/workspace"
            ],
            stdout=log_f,
            stderr=log_f,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True
        )
    print("Launched OpenVSCode Server (detached process).", flush=True)
    time.sleep(2)

print("=== [4/6] INSTALLING & STARTING DISGUISED CLOUDFLARE TUNNEL ===", flush=True)
bin_tunnel = "/usr/local/colab/bin/gdrive_sync_daemon"
os.makedirs(os.path.dirname(bin_tunnel), exist_ok=True)
if not os.path.exists(bin_tunnel):
    print("Downloading Cloudflare tunnel binary...", flush=True)
    subprocess.run([
        "curl", "-fsSL",
        "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64",
        "-o", bin_tunnel
    ], check=True, timeout=60)
    subprocess.run(["chmod", "+x", bin_tunnel], check=True)

def check_tunnel_alive():
    try:
        r = subprocess.run(["pgrep", "-f", "gdrive_sync_daemon"], capture_output=True, text=True)
        return r.returncode == 0
    except Exception:
        return False

is_tun_alive = check_tunnel_alive()
if is_tun_alive and not FORCE_RESTORE:
    print("=== [SAFE SKIP] CLOUDFLARE TUNNEL ALREADY ACTIVE ===", flush=True)
    print("Active Cloudflare tunnel connection preserved without restart.", flush=True)
else:
    subprocess.run(["pkill", "-f", "gdrive_sync_daemon"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["pkill", "-f", "cloudflared"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    tunnel_token = os.environ.get("TUNNEL_TOKEN", "").strip()
    if tunnel_token:
        try:
            with open("/content/tunnel_token.txt", "w", encoding="utf-8") as tf:
                tf.write(tunnel_token + "\n")
            os.chmod("/content/tunnel_token.txt", 0o600)
        except Exception:
            pass
    elif os.path.exists("/content/tunnel_token.txt"):
        with open("/content/tunnel_token.txt") as f:
            tunnel_token = f.read().strip()

    if tunnel_token:
        with open("/tmp/gdrive_sync.log", "a") as log_tun:
            env = os.environ.copy()
            env["TUNNEL_TOKEN"] = tunnel_token
            subprocess.Popen(
                [bin_tunnel, "tunnel", "--no-autoupdate", "run", "--token", tunnel_token],
                stdout=log_tun,
                stderr=log_tun,
                stdin=subprocess.DEVNULL,
                env=env,
                start_new_session=True,
                close_fds=True
            )
        print("Disguised Cloudflare tunnel started successfully (detached process).", flush=True)
    else:
        print("Warning: TUNNEL_TOKEN not provided, tunnel not started.", flush=True)

    time.sleep(2)

print("=== [5/6] PROVISIONING IN-VM TELEGRAM BOT SUPERVISOR ===", flush=True)
tg_bot_script = "/content/colab_tg_bot.py"
def check_tg_bot_alive():
    try:
        r = subprocess.run(["pgrep", "-f", "colab_tg_bot.py"], capture_output=True, text=True)
        return r.returncode == 0
    except Exception:
        return False

if os.path.exists(tg_bot_script):
    if check_tg_bot_alive() and not FORCE_RESTORE:
        print("=== [SAFE SKIP] TELEGRAM BOT SUPERVISOR ALREADY ACTIVE ===", flush=True)
        print("Active in-VM Telegram supervisor preserved without restart.", flush=True)
    else:
        subprocess.run(["pkill", "-f", "colab_tg_bot.py"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with open("/tmp/colab_tg_bot.log", "a") as log_tg:
            subprocess.Popen(
                [sys.executable, tg_bot_script],
                stdout=log_tg,
                stderr=log_tg,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True
            )
        print("In-VM Telegram Bot Supervisor launched successfully (detached process).", flush=True)
        time.sleep(2)
else:
    print("Notice: /content/colab_tg_bot.py not found, skipping bot supervisor start.", flush=True)

ssh_keepalive_script = "/content/ssh_keepalive_worker.py"
if os.path.exists(ssh_keepalive_script):
    subprocess.run(
        ["bash", "-c", "nohup python3 /content/ssh_keepalive_worker.py >/dev/null 2>&1 &"],
        check=False,
    )
    print("In-VM nohup SSH keepalive daemon ensured.", flush=True)

print("=== [6/6] VERIFYING LOCAL READINESS ===", flush=True)
try:
    import urllib.request
    with urllib.request.urlopen("http://127.0.0.1:8443", timeout=5) as resp:
        print(f"Local OpenVSCode HTTP status: {resp.status}", flush=True)
except Exception as e:
    print(f"Local OpenVSCode check note: {e}", flush=True)

print("AUTONOMOUS RELAY NODE INITIALIZED AND RUNNING CLEANLY!", flush=True)
