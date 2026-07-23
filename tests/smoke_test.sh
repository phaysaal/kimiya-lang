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
  [unguarded_confirm]="irreversible act screen.confirm is unguarded"
  [confirm_in_retry]="screen.confirm inside a retry body"
  [screen_arity]="screen.click takes 2 argument(s)"
  [vision_no_instrument]="needs an instrument"
  [vision_no_purpose]="must cite a purpose"
  [blind_locator]="is not vision-capable"
  [blind_shows_panel]="would vote on a screenshot they never saw"
  [shows_not_screenshot]="takes a screenshot as its first argument"
  [blind_claude_url]="unknown backend"
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
   examples/data_pipeline.kim examples/textlib.kim examples/pystats.py \
   examples/gui_publish.kim examples/guiprobe.py \
   examples/gui_collab.kim examples/collabdb.py "$TMP/"
FIXTURE="$PWD/tests/fixtures/screen.png"
printf '{"W3 Harness Group":{"join_code":"KX7P2M9Q","members":["A","B"],"messages":["m_ab","m_ba"]}}' > "$TMP/collab_state.json"
printf '{"talk_menu_x":300,"talk_menu_y":180,"publish_x":1180,"publish_y":740}' > "$TMP/locators.json"
printf '{"talks":[{"name":"Release notes","published":true}],"status_banner":"Release notes is live"}' > "$TMP/app_state.json"
printf 'The project deadline is Friday.\nBudget unchanged.\nDeadline moved from Monday to Friday by the sponsor.\n' > "$TMP/notes.txt"
printf 'Meeting moved to 3pm.\nInvoice 42 paid.\nServer restarted twice.\n' > "$TMP/inbox.txt"
printf '12\n15\n11\n90\n13\n14\n12\n' > "$TMP/latencies.txt"
export OLDPWD_REPO="$PWD"
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

echo "== screen surface: acts recorded, nothing delivered =="
# KIMIYA_MOCK already implies driver=none, but be explicit: this test must
# never be able to touch a real cursor.
gout=$(KIMIYA_MOCK=1 KIMIYA_SCREEN=none python3 -m kimiya run gui_publish.kim)
grep -q "COMMITTED" <<<"$gout" || { echo "FAIL: gui_publish"; echo "$gout"; exit 1; }
grep -q "GUI control" <<<"$gout" \
  || { echo "FAIL: screen acts not announced"; exit 1; }
grep -q "1 irreversible" <<<"$gout" \
  || { echo "FAIL: irreversible screen act not counted"; exit 1; }
grep -q "screen : 4 act(s) via none" <<<"$gout" \
  || { echo "FAIL: certificate omits the screen line"; echo "$gout"; exit 1; }
python3 - <<'PY' || { echo "FAIL: screen acts missing from trace"; exit 1; }
import json, sys
acts = [json.loads(l) for l in open(".kimiya/trace.jsonl")]
acts = [a for a in acts if a.get("surface") == "screen"]
assert len(acts) == 4, acts
assert [a["action"] for a in acts] == ["click", "type", "key", "confirm"], acts
assert all(a["delivered"] is False for a in acts), acts
PY

echo "== vision instrument: locate, shows, and a measured datasheet =="
export KIMIYA_SCREEN_FIXTURE="$FIXTURE"
vout=$(KIMIYA_MOCK=1 KIMIYA_SCREEN=none python3 -m kimiya run gui_collab.kim)
grep -q "COMMITTED" <<<"$vout" || { echo "FAIL: gui_collab"; echo "$vout"; exit 1; }
grep -q "4 locate(s)" <<<"$vout" \
  || { echo "FAIL: locates not counted"; echo "$vout"; exit 1; }
# prior-grade instruments: the declared recall overclaims, and says so
grep -q "declared recall 0.97 exceeds the measured" <<<"$vout" \
  || { echo "FAIL: overclaim not reported"; echo "$vout"; exit 1; }
python3 - <<'PY' || { echo "FAIL: locate trace"; exit 1; }
import json
recs = [json.loads(l) for l in open(".kimiya/trace.jsonl")]
loc = [r for r in recs if r.get("kind") == "locate"]
assert len(loc) == 4, len(loc)
# boxes must be absolute: DP-1 captures carry a non-zero origin
assert all(r["hits"] for r in loc), "a locate returned nothing"
shows = [r for r in recs if r.get("task", "").startswith("shows:")]
assert len(shows) == 2, shows
assert all("images" in r for r in shows), "shows judged without an image"
PY

# install the measured datasheet: theta must rise and the overclaim clear
cat > sheets.json <<'JSON'
{"locate:k_ui":  {"alpha_hi": 0.1579, "beta_lo": 0.9746, "n_true": 343, "n_false": 19},
 "shows:k_state":{"alpha_hi": 0.1579, "beta_lo": 0.9763, "n_true": 158, "n_false": 19}}
JSON
python3 -m kimiya datasheet sheets.json .kimiya --source "harness campaign" \
  | grep -q "imported" || { echo "FAIL: datasheet import"; exit 1; }
mout=$(KIMIYA_MOCK=1 KIMIYA_SCREEN=none python3 -m kimiya run gui_collab.kim)
grep -q "measured: harness campaign" <<<"$mout" \
  || { echo "FAIL: measured source not cited"; echo "$mout"; exit 1; }
grep -q "declared recall" <<<"$mout" \
  && { echo "FAIL: overclaim persists at beta=0.975"; exit 1; }
python3 - <<'PY' || { echo "FAIL: theta did not rise with the datasheet"; exit 1; }
import json
c = json.load(open(".kimiya/certificate.json"))
assert c["theta"] > 0.8, c["theta"]
PY
unset KIMIYA_SCREEN_FIXTURE

echo "== claude backends: declared, vision-capable, non-local =="
python3 "$OLDPWD_REPO/tests/check_backends.py" \
  || { echo "FAIL: claude backend wiring"; exit 1; }
cout=$(KIMIYA_MOCK=1 KIMIYA_SCREEN=none KIMIYA_SCREEN_FIXTURE="$FIXTURE" python3 -m kimiya run gui_collab.kim)
grep -q "via claude CLI" <<<"$cout" \
  || { echo "FAIL: claude CLI egress not reported"; echo "$cout"; exit 1; }
grep -q "screenshots leave the machine" <<<"$cout" \
  || { echo "FAIL: screenshot egress not announced"; echo "$cout"; exit 1; }

echo "== observe screen: no fixture, no invented screenshot =="
nout=$(KIMIYA_MOCK=1 KIMIYA_SCREEN=none python3 -m kimiya run gui_collab.kim || true)
grep -q "ABSTAINED" <<<"$nout" \
  || { echo "FAIL: ran without a screenshot"; echo "$nout"; exit 1; }

echo "== compile: emit standalone python and run it =="
KIMIYA_MOCK=1 python3 -m kimiya compile grounded_summary.kim --out gs.py >/dev/null
KIMIYA_MOCK=1 python3 gs.py | grep -q "COMMITTED" \
  || { echo "FAIL: compiled grounded_summary did not commit"; exit 1; }
KIMIYA_MOCK=1 python3 -m kimiya compile data_pipeline.kim --out dp.py >/dev/null
cout=$(KIMIYA_MOCK=1 python3 dp.py)
grep -q "COMMITTED" <<<"$cout" || { echo "FAIL: compiled data_pipeline"; exit 1; }
grep -q "p95=90" <<<"$cout" || { echo "FAIL: compiled pystats math"; exit 1; }
KIMIYA_MOCK=1 python3 -m kimiya compile gui_publish.kim --out gp.py >/dev/null
gcout=$(KIMIYA_MOCK=1 KIMIYA_SCREEN=none python3 gp.py)
grep -q "COMMITTED" <<<"$gcout" || { echo "FAIL: compiled gui_publish"; exit 1; }
grep -q "screen : 4 act(s)" <<<"$gcout" \
  || { echo "FAIL: compiled certificate omits screen"; echo "$gcout"; exit 1; }
KIMIYA_MOCK=1 python3 -m kimiya compile gui_collab.kim --out gc.py >/dev/null
ccout=$(KIMIYA_MOCK=1 KIMIYA_SCREEN=none KIMIYA_SCREEN_FIXTURE="$FIXTURE" \
        python3 gc.py)
grep -q "COMMITTED" <<<"$ccout" || { echo "FAIL: compiled gui_collab"; echo "$ccout"; exit 1; }
grep -q "4 locate(s)" <<<"$ccout" \
  || { echo "FAIL: compiled locates not counted"; echo "$ccout"; exit 1; }
echo "SMOKE TEST PASS"
