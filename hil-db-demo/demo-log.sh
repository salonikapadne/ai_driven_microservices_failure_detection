#!/bin/sh
# Stays running (no exit) so Docker does not restart the container.
# Phase A: healthy-style lines — avoid dashboard _FAILURE_KEYWORDS (error, fatal, timeout, critical, …).
# Phase B: app-level schema mismatch — [HIL_DB_DEMO] + db_app_escalate: (classifier → db_app_escalate → HIL).
set -u
HIL_ESCALATE_AFTER_SEC="${HIL_ESCALATE_AFTER_SEC:-60}"
HIL_DUMMY_INTERVAL_SEC="${HIL_DUMMY_INTERVAL_SEC:-10}"
HIL_DEMO_INTERVAL_SEC="${HIL_DEMO_INTERVAL_SEC:-120}"

echo "hil-db-demo: healthy telemetry for ${HIL_ESCALATE_AFTER_SEC}s (every ${HIL_DUMMY_INTERVAL_SEC}s), then schema mismatch escalation"

start_ts=$(date +%s)
tick=0
while [ $(($(date +%s) - start_ts)) -lt "$HIL_ESCALATE_AFTER_SEC" ]; do
  tick=$((tick + 1))
  ts="$(date -Iseconds 2>/dev/null || date)"
  echo "[hil-db-demo] tick=${tick} ts=${ts} pool=steady conns=4 idle=2 cap=32"
  echo "[hil-db-demo] tick=${tick} ledger_revision=11 storage_ping_ms=2 probe=pass"
  sleep "$HIL_DUMMY_INTERVAL_SEC"
done

echo "hil-db-demo: emitting application-level schema mismatch (human-in-the-loop); repeating every ${HIL_DEMO_INTERVAL_SEC}s"
while true; do
  ts="$(date -Iseconds 2>/dev/null || date)"
  echo "[HIL_DB_DEMO] ${ts} migration error: schema mismatch — expected ledger revision 12, store reports 11"
  echo "db_app_escalate: review DDL and apply manual migration; do not rely on container restart alone"
  sleep "$HIL_DEMO_INTERVAL_SEC"
done