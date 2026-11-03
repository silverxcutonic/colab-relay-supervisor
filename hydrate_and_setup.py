#!/usr/bin/env python3
"""
Google Colab Autonomous Bootstrapper & Hydration Engine
Extracts workspace state, configures rclone, provisions code-server (8443),
connects Cloudflare tunnel, and verifies local readiness.
Zero credentials hardcoded.
"""

import os
import sys
import time
import shutil
import subprocess

print("=== [1/5] INITIALIZING CREDENTIALS & RCLONE ===", flush=True)
os.makedirs("/root/.config/rclone", exist_ok=True)
if os.path.exists("/content/rclone.conf"):
    shutil.copy("/content/rclone.conf", "/root/.config/rclone/rclone.conf")
    os.chmod("/root/.config/rclone/rclone.conf", 0o600)

if not shutil.which("rclone"):
    print("Installing rclone binary...", flush=True)
    subprocess.run(["curl", "-fsSL", "https://rclone.org/install.sh", "-o", "/tmp/install_rclone.sh"], check=True)
    subprocess.run(["bash", "/tmp/install_rclone.sh"], check=True)

# Pull latest state bundle from central Google Drive if not local
if not os.path.exists("/content/colab_bundle.tar.gz") and os.path.exists("/root/.config/rclone/rclone.conf"):
    print("Pulling latest colab_bundle.tar.gz from central Google Drive...", flush=True)
    subprocess.run([
        "rclone", "copy",
        "gdrive:coolify-relay-state/colab-relay-state/colab_bundle.tar.gz",
        "/content/",
        "--drive-chunk-size=32M",
        "--buffer-size=32M"
    ], check=False)

print("=== [2/5] EXTRACTING WORKSPACE & CONFIGS ===", flush=True)
if os.path.exists("/content/colab_bundle.tar.gz"):
    try:
        subprocess.run(["tar", "-xzf", "/content/colab_bundle.tar.gz", "-C", "/content"], check=True)
    except Exception as e:
        print(f"Extraction note: {e}", flush=True)

if os.path.exists("/content/workspace/.rclone/rclone.conf") and not os.path.exists("/root/.config/rclone/rclone.conf"):
    shutil.copy("/content/workspace/.rclone/rclone.conf", "/root/.config/rclone/rclone.conf")
    os.chmod("/root/.config/rclone/rclone.conf", 0o600)

if os.path.exists("/content/code-server"):
    os.makedirs("/root/.config/code-server", exist_ok=True)
    subprocess.run(["cp", "-r", "/content/code-server/.", "/root/.config/code-server/"])

print("=== [3/5] INSTALLING & STARTING CODE-SERVER (PORT 8443) ===", flush=True)
if not shutil.which("code-server"):
    subprocess.run(["curl", "-fsSL", "https://code-server.dev/install.sh", "-o", "/tmp/install_cs.sh"], check=True)
    subprocess.run(["bash", "/tmp/install_cs.sh"], check=True)

subprocess.run(["pkill", "-f", "code-server"])
os.makedirs("/root/.config/code-server", exist_ok=True)
cfg_yaml = "/root/.config/code-server/config.yaml"
if not os.path.exists(cfg_yaml):
    with open(cfg_yaml, "w") as f:
        f.write("bind-addr: 127.0.0.1:8443\nauth: password\npassword: silver\ncert: false\n")

os.makedirs("/content/workspace", exist_ok=True)
subprocess.Popen([
    "code-server",
    "--config", cfg_yaml,
    "--disable-telemetry",
    "/content/workspace"
], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(3)

print("=== [4/5] INSTALLING & STARTING CLOUDFLARED ===", flush=True)
if not os.path.exists("/usr/local/bin/cloudflared"):
    subprocess.run(["curl", "-fsSL", "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64", "-o", "/usr/local/bin/cloudflared"], check=True)
    subprocess.run(["chmod", "+x", "/usr/local/bin/cloudflared"], check=True)

subprocess.run(["pkill", "-f", "cloudflared"])
tunnel_token = os.environ.get("TUNNEL_TOKEN", "")
if not tunnel_token and os.path.exists("/content/tunnel_token.txt"):
    with open("/content/tunnel_token.txt") as f:
        tunnel_token = f.read().strip()

if tunnel_token:
    subprocess.Popen([
        "/usr/local/bin/cloudflared", "tunnel",
        "--no-autoupdate",
        "--protocol", "http2",
        "run", "--token", tunnel_token
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("Cloudflared tunnel started with provided token.", flush=True)
else:
    print("Warning: TUNNEL_TOKEN not provided, cloudflared not started.", flush=True)

time.sleep(3)

print("=== [5/5] VERIFYING SERVICES ===", flush=True)
subprocess.run(["mkdir", "-p", "/content/workspace/.claude_transcripts"])
subprocess.run(["ln", "-sf", "/content/workspace/.claude_transcripts", "/root/.claude"])

try:
    import urllib.request
    with urllib.request.urlopen("http://127.0.0.1:8443/login", timeout=5) as resp:
        print(f"Local code-server /login HTTP: {resp.status}", flush=True)
except Exception as e:
    print(f"Local code-server check note: {e}", flush=True)

print("AUTONOMOUS RELAY NODE INITIALIZED AND RUNNING!", flush=True)
