#!/usr/bin/env bash
# bench-regression-check.sh — Run benchmarks and compare against latest baseline.
# Exit 0 if all within thresholds, exit 1 if any regression detected.
set -euo pipefail

BENCH_DIR="${HOME}/.duo/bench-results"
THRESHOLD_DEFAULT=25  # percent

# Find latest baseline
BASELINE=$(ls -1 "${BENCH_DIR}"/*.json 2>/dev/null | sort | tail -1)
if [[ -z "${BASELINE:-}" ]]; then
    echo "⚠️  No baseline found in ${BENCH_DIR}/"
    echo "   Run: duo bench all --json-output --save"
    exit 1
fi

echo "📊 Baseline: ${BASELINE}"
echo "🔄 Running benchmarks..."

# Run current benchmarks
CURRENT=$(mktemp)
trap 'rm -f "${CURRENT}"' EXIT
duo bench all --json-output > "${CURRENT}" 2>/dev/null

# Compare using Python
python3 << PYEOF
import json, sys

with open("${BASELINE}") as f:
    baseline = json.load(f)
with open("${CURRENT}") as f:
    current = json.load(f)

thresholds = {
    "dialog-detection": 25,
    "file-protocol": 30,
    "journal-append": 30,
}

regressions = []
for b_suite, c_suite in zip(baseline, current):
    suite_name = b_suite["suite"]
    threshold = thresholds.get(suite_name, ${THRESHOLD_DEFAULT})
    for bench_name in b_suite["results"]:
        if bench_name not in c_suite["results"]:
            continue
        b_ops = b_suite["results"][bench_name]["ops_per_sec"]
        c_ops = c_suite["results"][bench_name]["ops_per_sec"]
        change_pct = ((c_ops - b_ops) / b_ops) * 100
        status = "✅" if change_pct >= -threshold else "❌"
        if change_pct < -threshold:
            regressions.append(f"  {suite_name}/{bench_name}: {change_pct:+.1f}% (threshold: -{threshold}%)")
        print(f"  {status} {suite_name}/{bench_name}: {b_ops:.0f} → {c_ops:.0f} ops/sec ({change_pct:+.1f}%)")

if regressions:
    print(f"\n❌ {len(regressions)} regression(s) detected:")
    for r in regressions:
        print(r)
    sys.exit(1)
else:
    print("\n✅ All benchmarks within thresholds")
    sys.exit(0)
PYEOF
