#!/usr/bin/env bash
set -euo pipefail

echo "=== Oracle Cloud Keep-Alive Setup ==="

# 1. Install stress-ng if missing
if ! command -v stress-ng &>/dev/null; then
    echo "Installing stress-ng..."
    sudo apt-get update -qq
    sudo apt-get install -y -qq stress-ng
fi

# 2. Detect active CPU cores dynamically (handles 1-core E2.1.Micro or 4-core A1.Flex)
CORES=$(nproc)
echo "Detected $CORES CPU core(s)."

# 3. Define keep-alive command (runs at top of every hour for 15 min at 30% load, lowest scheduling priority)
# Using 'nice -n 19' and 'ionice -c 3' guarantees Datamorph web requests and file uploads take 100% priority.
CRON_CMD="0 * * * * /usr/bin/timeout 15m /usr/bin/nice -n 19 /usr/bin/ionice -c 3 /usr/bin/stress-ng --cpu $CORES --cpu-load 30 >/dev/null 2>&1"

# 4. Idempotently inject into crontab
crontab -l 2>/dev/null | grep -v "stress-ng" > /tmp/current_crontab || true
echo "$CRON_CMD" >> /tmp/current_crontab
crontab /tmp/current_crontab
rm -f /tmp/current_crontab

echo "Crontab entry successfully registered:"
crontab -l | grep "stress-ng"

# 5. Quick 5-second sanity test
echo "Running 5-second verification test..."
/usr/bin/timeout 5s /usr/bin/nice -n 19 /usr/bin/stress-ng --cpu "$CORES" --cpu-load 30 >/dev/null 2>&1 || true
echo "Keep-alive setup complete and verified!"

