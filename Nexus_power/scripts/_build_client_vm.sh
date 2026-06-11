#!/usr/bin/env bash
# Detached client rebuild on the VM. Logs to /tmp/cb.log. Survives SSH drop.
cd /home/harik/nexus/Nexus_power || { echo "===CD FAIL==="; exit 9; }
echo "===BUILD START $(date -u)==="
docker compose build client
rc=$?
echo "===BUILD RC=$rc==="
if [ "$rc" -eq 0 ]; then
  docker compose up -d client
  echo "===UP RC=$?==="
fi
echo "===ALL DONE==="
