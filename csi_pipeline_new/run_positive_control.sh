#!/usr/bin/env bash
# Positive control: can this setup detect a strong obstruction reproducibly?
#
# Records alternating empty/object blocks hands-free. You place or remove the
# object when told, then step out of the room; recording starts only after a
# settle delay and stops on its own. The point is that your body is never in
# the room while CSI is being captured, and is in the same place (out) for
# every block — otherwise the largest scatterer in the setup varies with the
# label.
#
# Block order is counterbalanced (E,O / O,E / E,O) so neither class is
# systematically first, second, earlier, or later.
#
#   ./run_positive_control.sh                    # 3 rounds, 45 s blocks
#   ./run_positive_control.sh --rounds 3 --duration 45 --settle 20
#   ./run_positive_control.sh --prefix pc2       # a second attempt
#
# Then:  uv run --group csi python probe_check.py --like 'pc_%'

set -euo pipefail
set -m  # job control: each capture gets its own process group, so we can
        # signal the whole uv -> python chain, not just the wrapper.

ROOT="$(cd "$(dirname "$0")" && pwd)"

ROUNDS=3
DURATION=45
SETTLE=20
PREFIX="pc"
OBJECT="the water container"
CHANNEL=11
METHOD="4.3"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --rounds)   ROUNDS="$2"; shift 2 ;;
    --duration) DURATION="$2"; shift 2 ;;
    --settle)   SETTLE="$2"; shift 2 ;;
    --prefix)   PREFIX="$2"; shift 2 ;;
    --object)   OBJECT="$2"; shift 2 ;;
    --channel)  CHANNEL="$2"; shift 2 ;;
    --method)   METHOD="$2"; shift 2 ;;
    -h|--help)  sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

announce() {
  echo ""
  echo "=== $* ==="
  command -v say >/dev/null 2>&1 && say "$*" || true
}

countdown() {
  local n="$1"
  for ((i = n; i > 0; i--)); do
    printf "\r  starting in %2ds — stand clear of the link  " "$i"
    sleep 1
  done
  printf "\r  recording...                                 \n"
}

capture() {
  local label="$1"
  "$ROOT/run_ingest.sh" --method "$METHOD" --channel "$CHANNEL" --label "$label" \
    >"/tmp/${label}.log" 2>&1 &
  local pid=$!
  sleep "$DURATION"
  # Negative PID signals the process group. ingest_serial.py traps SIGINT and
  # runs a final flush, so no packets are lost.
  kill -INT -"$pid" 2>/dev/null || pkill -INT -f ingest_serial.py || true
  wait "$pid" 2>/dev/null || true
  # ingest prints "flushed total=N" as it goes; the last one is the count.
  local n
  n=$(grep -o 'total=[0-9]*' "/tmp/${label}.log" 2>/dev/null | tail -1 | cut -d= -f2)
  echo "  stored: $label  packets=${n:-0}"
  if [[ -z "${n:-}" || "${n:-0}" -lt 100 ]]; then
    echo "  WARNING: very few packets — check the link before continuing." >&2
    echo "  see /tmp/${label}.log" >&2
  fi
}

echo "positive control: $ROUNDS rounds, ${DURATION}s blocks, ${SETTLE}s settle"
echo "object: $OBJECT"
echo "labels: ${PREFIX}_empty_N / ${PREFIX}_object_N"
echo ""
echo "Before starting, confirm the checklist in POSITIVE_CONTROL.md."
read -r -p "Press Enter when the setup is built and you are ready. " _

for ((r = 1; r <= ROUNDS; r++)); do
  # Counterbalance: even rounds run object-first.
  if (( r % 2 == 1 )); then order=("empty" "object"); else order=("object" "empty"); fi

  for state in "${order[@]}"; do
    if [[ "$state" == "empty" ]]; then
      announce "Round $r. Remove $OBJECT from the link, then leave the room."
    else
      announce "Round $r. Place $OBJECT on the taped mark, then leave the room."
    fi
    countdown "$SETTLE"
    capture "${PREFIX}_${state}_${r}"
  done
done

announce "Positive control complete."
echo ""
echo "Now run:"
echo "  uv run --group csi python probe_check.py --like '${PREFIX}_%'"
