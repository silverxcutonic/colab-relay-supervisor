import time
import sys

print("[PING-B] Starting 40s duty cycle...", flush=True)
for i in range(8):
    time.sleep(5)
    print(f"[PING-B] Active tick {i+1}/8 | Kernel BUSY", flush=True)
print("[PING-B] Finished duty cycle.", flush=True)
