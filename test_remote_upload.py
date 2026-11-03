import requests
import time
from pathlib import Path

csv_path = Path("data/benchmarks/events_unclean_50mb.csv")
print(f"Testing upload of {csv_path.name} ({csv_path.stat().st_size / (1024*1024):.2f} MB)...")

url = "https://datamorph.duckdns.org/api/jobs/upload"
t0 = time.perf_counter()

try:
    with open(csv_path, "rb") as f:
        files = {"file": (csv_path.name, f, "text/csv")}
        data = {"privacy_mode": "false"}
        headers = {"X-User-Id": "test-large-upload"}
        print("Sending POST request to DuckDNS...")
        resp = requests.post(url, files=files, data=data, headers=headers, timeout=120)
        print(f"Status Code: {resp.status_code}")
        print(f"Response: {resp.text[:300]}")
        print(f"Total time: {time.perf_counter() - t0:.2f}s")
except Exception as e:
    print(f"Upload failed with exception: {type(e).__name__}: {e}")
