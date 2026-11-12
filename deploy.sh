#!/bin/bash
# Update the server without killing a report in progress.
# Run from your Mac:  ./deploy.sh   (needs SSH access to the server)
set -eu
HOST=root@209.250.232.171
DIR=/root/market-research-agent-swarm
ssh "$HOST" "cd $DIR && git pull -q --ff-only origin main && venv/bin/python -c 'import server'"
for i in $(seq 1 90); do
  running=$(ssh "$HOST" "curl -s -m 5 http://127.0.0.1:5000/jobs/active" | python3 -c "import json,sys;print(json.load(sys.stdin).get('running',0))" 2>/dev/null || echo 0)
  [ "$running" = 0 ] && break
  [ "$i" = 1 ] && echo "Waiting for $running report(s) in progress to finish before restarting..."
  sleep 10
done
[ "$running" = 0 ] || { echo "Still busy after 15 minutes; not restarting. Try again later."; exit 1; }
ssh "$HOST" "pm2 restart swarm-research >/dev/null && sleep 3 && curl -s -m 8 -o /dev/null -w 'site: %{http_code}\n' http://127.0.0.1:5000/"
echo "Deployed $(git rev-parse --short HEAD)."
