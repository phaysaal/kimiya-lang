#!/usr/bin/env bash
# Interpreter smoke test: checker accepts the good, rejects the bad,
# and both examples run end-to-end under the mock oracle (no network).
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="$PWD"

echo "== checker: examples must pass =="
for f in examples/*.kim; do
  python3 -m kimiya check "$f" || { echo "FAIL: $f rejected"; exit 1; }
done

echo "== hybrid pool: egress is declared and reported =="
python3 -m kimiya check examples/hybrid_pool.kim | grep -q "checks pass" \
  || { echo "FAIL: hybrid_pool rejected"; exit 1; }

echo "== checker: bad programs must be rejected with the right rule =="
declare -A want=(
  [silent_equality]="undeclared purpose"
  [self_judgment]="J ⋪ C"
  [unguarded_delete]="unguarded"
  [retry_effects]="snapshot retry over an external world"
  [irreversible_in_retry]="inside a retry body"
  [impure_module]="must be pure declarations"
  [arity_mismatch]="argument(s)"
  [remote_self_judgment]="J ⋪ C"
  [missing_url]="needs a url"
  [field_typo]="has no field 'txet'"
  [field_on_text]="cannot read field"
  [iterate_text]="cannot iterate a text"
  [select_text]="expected a list"
)
for name in "${!want[@]}"; do
  out=$(python3 -m kimiya check "tests/bad/$name.kim" 2>&1 || true)
  if python3 -m kimiya check "tests/bad/$name.kim" >/dev/null 2>&1; then
    echo "FAIL: tests/bad/$name.kim was accepted"; exit 1
  fi
  if ! grep -qF "${want[$name]}" <<<"$out"; then
    echo "FAIL: $name did not report '${want[$name]}'"; echo "$out"; exit 1
  fi
done

echo "== highlighter =="
python3 -m kimiya hl examples/agentic_digest.kim >/dev/null
python3 -m kimiya hl examples/agentic_digest.kim --html >/dev/null
rm -f examples/agentic_digest.html

echo "== mock runs =="
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
cp examples/grounded_summary.kim examples/agentic_digest.kim \
   examples/data_pipeline.kim examples/textlib.kim examples/pystats.py "$TMP/"
printf 'The project deadline is Friday.\nBudget unchanged.\nDeadline moved from Monday to Friday by the sponsor.\n' > "$TMP/notes.txt"
printf 'Meeting moved to 3pm.\nInvoice 42 paid.\nServer restarted twice.\n' > "$TMP/inbox.txt"
printf '12\n15\n11\n90\n13\n14\n12\n' > "$TMP/latencies.txt"
cd "$TMP"
KIMIYA_MOCK=1 python3 -m kimiya run grounded_summary.kim | grep -q "COMMITTED" \
  || { echo "FAIL: grounded_summary did not commit"; exit 1; }
KIMIYA_MOCK=1 python3 -m kimiya run agentic_digest.kim | grep -q "COMMITTED" \
  || { echo "FAIL: agentic_digest did not commit"; exit 1; }
test -f digest.txt || { echo "FAIL: act did not write digest.txt"; exit 1; }
out=$(KIMIYA_MOCK=1 python3 -m kimiya run data_pipeline.kim)
grep -q "COMMITTED" <<<"$out" || { echo "FAIL: data_pipeline"; exit 1; }
grep -q "python extension loaded" <<<"$out" \
  || { echo "FAIL: python extension not announced"; exit 1; }
grep -q "p95=90" <<<"$out" || { echo "FAIL: pystats math wrong"; echo "$out"; exit 1; }
test -f reading.txt || { echo "FAIL: reading.txt not written"; exit 1; }
grep -q "^- n=" reading.txt || { echo "FAIL: bulletize (module fn) output"; exit 1; }

echo "== compile: emit standalone python and run it =="
KIMIYA_MOCK=1 python3 -m kimiya compile grounded_summary.kim --out gs.py >/dev/null
KIMIYA_MOCK=1 python3 gs.py | grep -q "COMMITTED" \
  || { echo "FAIL: compiled grounded_summary did not commit"; exit 1; }
KIMIYA_MOCK=1 python3 -m kimiya compile data_pipeline.kim --out dp.py >/dev/null
cout=$(KIMIYA_MOCK=1 python3 dp.py)
grep -q "COMMITTED" <<<"$cout" || { echo "FAIL: compiled data_pipeline"; exit 1; }
grep -q "p95=90" <<<"$cout" || { echo "FAIL: compiled pystats math"; exit 1; }
echo "SMOKE TEST PASS"
