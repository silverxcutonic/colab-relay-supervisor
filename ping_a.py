import time
import sys

print("[PING-A] Starting 40s duty cycle...", flush=True)
for i in range(8):
    time.sleep(5)
    print(f"[PING-A] Active tick {i+1}/8 | Kernel BUSY", flush=True)
print("[PING-A] Finished duty cycle.", flush=True)
