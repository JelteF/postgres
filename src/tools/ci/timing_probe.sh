#!/bin/sh
# TEMPORARY: interleave each TAP test with its pytest port and report both
# durations, so the speedups quoted in the port commit messages come from one
# runner under identical conditions.
set -u

PAIRS="test_aio/001_aio:test_aio/test_001_aio
amcheck/001_verify_heapam:amcheck/test_001_verify_heapam
bloom/001_wal:bloom/test_001_wal
recovery/049_wait_for_lsn:recovery/test_049_wait_for_lsn
test_json_parser/002_inline:test_json_parser/test_002_inline
recovery/029_stats_restart:recovery/test_029_stats_restart
recovery/031_recovery_conflict:recovery/test_031_recovery_conflict
subscription/031_column_list:subscription/test_031_column_list"

run_one() {
    # meson reports each test's duration; --num-processes 1 keeps the two
    # forms from competing with anything else for CPU.
    out=$(meson test -C build --no-rebuild --num-processes 1 "$1" 2>&1)
    secs=$(printf '%s\n' "$out" | sed -n 's/.*OK  *\([0-9.]*\)s.*/\1/p' | head -1)
    if [ -z "$secs" ]; then
        secs=FAILED
    fi
    printf 'TIMING %s %s %s\n' "$2" "$1" "$secs"
}

round=1
while [ "$round" -le "${ROUNDS:-3}" ]; do
    printf '%s\n' "$PAIRS" | while IFS=: read -r perl py; do
        run_one "$perl" "$round"
        run_one "$py" "$round"
    done
    round=$((round + 1))
done
