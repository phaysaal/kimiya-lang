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
want=(
  "silent_equality|undeclared purpose"
  "self_judgment|J ⋪ C"
  "unguarded_delete|unguarded"
  "retry_effects|snapshot retry over an external world"
  "irreversible_in_retry|inside a retry body"
  "impure_module|must be pure declarations"
  "arity_mismatch|argument(s)"
  "remote_self_judgment|J ⋪ C"
  "missing_url|needs a url"
  "field_typo|has no field 'txet'"
  "field_on_text|cannot read field"
  "iterate_text|cannot iterate a text"
  "select_text|expected a list"
  "unguarded_confirm|irreversible act screen.confirm is unguarded"
  "confirm_in_retry|screen.confirm inside a retry body"
  "screen_arity|screen.click takes 2 argument(s)"
  "vision_no_instrument|needs an instrument"
  "vision_no_purpose|must cite a purpose"
  "blind_locator|is not vision-capable"
  "blind_shows_panel|would vote on an image they never saw"
  "shows_not_screenshot|takes an observed image as its first argument"
  "image_direct_path|expected list<image observation>"
  "image_blind_generator|not vision-capable"
  "image_wrong_arity|takes exactly one path"
  "blind_claude_url|unknown backend"
  "undeclared_actor|not a declared display"
  "actor_on_file|screen surface only"
  "param_bad_default|is not a num"
  "param_dup|duplicate param"
  "param_bad_type|unknown type"
  "commit_in_explore|commit inside explore"
  "irreversible_in_explore|inside explore"
)
for case in "${want[@]}"; do
  name=${case%%|*}
  expected=${case#*|}
  out=$(python3 -m kimiya check "tests/bad/$name.kim" 2>&1 || true)
  if python3 -m kimiya check "tests/bad/$name.kim" >/dev/null 2>&1; then
    echo "FAIL: tests/bad/$name.kim was accepted"; exit 1
  fi
  if ! grep -qF "$expected" <<<"$out"; then
    echo "FAIL: $name did not report '$expected'"; echo "$out"; exit 1
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
   examples/gui_collab.kim examples/collabdb.py \
   examples/counterfactual.kim examples/counterfactual_evolve.kim \
   examples/image_assess.kim \
   examples/bizlib.py examples/business.json \
   "$TMP/"
FIXTURE="$PWD/tests/fixtures/screen.png"
printf '{"W3 Harness Group":{"join_code":"KX7P2M9Q","members":["A","B"],"messages":["m_ab","m_ba"]}}' > "$TMP/collab_state.json"
printf '{"talk_menu_x":300,"talk_menu_y":180,"publish_x":1180,"publish_y":740}' > "$TMP/locators.json"
printf '{"talks":[{"name":"Release notes","published":true}],"status_banner":"Release notes is live"}' > "$TMP/app_state.json"
printf 'The project deadline is Friday.\nBudget unchanged.\nDeadline moved from Monday to Friday by the sponsor.\n' > "$TMP/notes.txt"
printf 'Meeting moved to 3pm.\nInvoice 42 paid.\nServer restarted twice.\n' > "$TMP/inbox.txt"
printf '12\n15\n11\n90\n13\n14\n12\n' > "$TMP/latencies.txt"
mkdir -p "$TMP/tests/fixtures"
cp "$PWD/tests/fixtures/screen.png" "$TMP/tests/fixtures/screen.png"
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

echo "== image observation + multimodal gen =="
iout=$(KIMIYA_MOCK=1 python3 -m kimiya run image_assess.kim)
grep -q "COMMITTED" <<<"$iout" \
  || { echo "FAIL: image_assess"; echo "$iout"; exit 1; }
grep -q "image egress : none" <<<"$iout" \
  || { echo "FAIL: local image egress not reported"; echo "$iout"; exit 1; }
python3 - <<'PY' || { echo "FAIL: image provenance"; exit 1; }
import json
c = json.load(open(".kimiya/certificate.json"))
assert len(c["image_observations"]) == 1, c
assert c["image_egress"] == [], c
assert len(c["image_observations"][0]["sha"]) == 64, c
PY
KIMIYA_MOCK=1 python3 -m kimiya compile image_assess.kim --out ia.py >/dev/null
icout=$(KIMIYA_MOCK=1 python3 ia.py)
grep -q "COMMITTED" <<<"$icout" \
  || { echo "FAIL: compiled image_assess"; echo "$icout"; exit 1; }

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

echo "== locate cache: exact hits, replay, and replay-miss =="
FIXTURE2="$OLDPWD_REPO/tests/fixtures/screen2.png"
# The earlier runs populated the cache; this same-fixture run must be
# served entirely from exact-sha hits.
eout=$(KIMIYA_MOCK=1 KIMIYA_SCREEN=none KIMIYA_SCREEN_FIXTURE="$FIXTURE" \
       python3 -m kimiya run gui_collab.kim)
grep -q "4 exact-cache" <<<"$eout" \
  || { echo "FAIL: exact-sha cache not hit"; echo "$eout"; exit 1; }
grep -q "replayed" <<<"$eout" \
  && { echo "FAIL: exact hits mislabeled as replays"; exit 1; }
# Different pixels + no replay flag: locates run live again. One
# exact-cache hit remains legitimate — the program locates "the + button
# by the groups heading" twice and both captures serve identical bytes,
# so the second is a reading of the same image within the same run.
lout=$(KIMIYA_MOCK=1 KIMIYA_SCREEN=none KIMIYA_SCREEN_FIXTURE="$FIXTURE2" \
       python3 -m kimiya run gui_collab.kim)
grep -q "replayed" <<<"$lout" \
  && { echo "FAIL: changed pixels replayed without --replay"; exit 1; }
grep -q "4 exact-cache" <<<"$lout" \
  && { echo "FAIL: changed pixels served from cache without --replay"; exit 1; }
grep -q "(1 exact-cache)" <<<"$lout" \
  || { echo "FAIL: intra-run duplicate locate not deduped"; echo "$lout"; exit 1; }
# The cache now holds screen2 readings; replaying against screen.png is
# the stale case: served from cache, counted, and disclosed.
rout=$(KIMIYA_MOCK=1 KIMIYA_SCREEN=none KIMIYA_SCREEN_FIXTURE="$FIXTURE" \
       python3 -m kimiya run gui_collab.kim --replay)
grep -q "COMMITTED" <<<"$rout" || { echo "FAIL: replay run"; echo "$rout"; exit 1; }
grep -q "4 replayed" <<<"$rout" \
  || { echo "FAIL: replays not counted"; echo "$rout"; exit 1; }
grep -q "layout stability is assumed" <<<"$rout" \
  || { echo "FAIL: replay not disclosed"; echo "$rout"; exit 1; }
# Replay against an empty cache must abstain, not invent coordinates.
rm .kimiya/locates.json
mout2=$(KIMIYA_MOCK=1 KIMIYA_SCREEN=none KIMIYA_SCREEN_FIXTURE="$FIXTURE" \
        python3 -m kimiya run gui_collab.kim --replay || true)
grep -q "ABSTAINED" <<<"$mout2" \
  || { echo "FAIL: replay miss did not abstain"; echo "$mout2"; exit 1; }
grep -q "no cached locate" <<<"$mout2" \
  || { echo "FAIL: replay-miss reason not stated"; echo "$mout2"; exit 1; }
# Compiled artifact honors KIMIYA_REPLAY: repopulate live, then replay.
KIMIYA_MOCK=1 KIMIYA_SCREEN=none KIMIYA_SCREEN_FIXTURE="$FIXTURE" \
  python3 -m kimiya run gui_collab.kim > /dev/null
KIMIYA_MOCK=1 python3 -m kimiya compile gui_collab.kim --out gcr.py >/dev/null
crout=$(KIMIYA_MOCK=1 KIMIYA_SCREEN=none KIMIYA_SCREEN_FIXTURE="$FIXTURE2" \
        KIMIYA_REPLAY=1 python3 gcr.py)
grep -q "4 replayed" <<<"$crout" \
  || { echo "FAIL: compiled replay"; echo "$crout"; exit 1; }

echo "== actors: per-seat fixtures, certificate table =="
# Two declared displays, each served by its own recording: actor A
# sees fixture 1, actor B sees fixture 2, and the trace must show the
# two seats reading different images.
rm -f .kimiya/locates.json .kimiya/trace.jsonl
aout=$(KIMIYA_MOCK=1 KIMIYA_SCREEN=none \
       KIMIYA_SCREEN_FIXTURE_A="$FIXTURE" \
       KIMIYA_SCREEN_FIXTURE_B="$FIXTURE2" \
       python3 -m kimiya run gui_collab.kim)
grep -q "COMMITTED" <<<"$aout" || { echo "FAIL: actor run"; echo "$aout"; exit 1; }
grep -q "actor  : A →" <<<"$aout" \
  || { echo "FAIL: actor A missing from certificate"; echo "$aout"; exit 1; }
grep -q "actor  : B →" <<<"$aout" \
  || { echo "FAIL: actor B missing from certificate"; echo "$aout"; exit 1; }
python3 - <<'PY' || { echo "FAIL: per-actor fixtures"; exit 1; }
import json
recs = [json.loads(l) for l in open(".kimiya/trace.jsonl")]
obs = [r for r in recs if r.get("kind") == "observe"
       and r.get("surface") == "screen"]
shas = {r["actor"]: r["sha"] for r in obs}
assert set(shas) == {"A", "B"}, shas
assert shas["A"] != shas["B"], "actors served the same fixture"
acts = [r for r in recs if r.get("kind") == "act"]
assert {a.get("actor") for a in acts} == {"A", "B"}, acts
PY

echo "== observe screen: no fixture, no invented screenshot =="
nout=$(KIMIYA_MOCK=1 KIMIYA_SCREEN=none python3 -m kimiya run gui_collab.kim || true)
grep -q "ABSTAINED" <<<"$nout" \
  || { echo "FAIL: ran without a screenshot"; echo "$nout"; exit 1; }

echo "== params: typed interface, refusal before any model =="
cat > greet.kim <<'KIM'
param name: text
param times: num = 2
pool A = "llama3.1:8b"
out := ""
forall i in range(times):
    out := out + "hi " + name + "; "
check len(out) > 0
commit(out)
KIM
python3 -m kimiya check greet.kim | grep -q "name (text) required" \
  || { echo "FAIL: param summary"; exit 1; }
mreq=$(python3 -m kimiya run greet.kim 2>&1 || true)
grep -q "required parameter" <<<"$mreq" \
  || { echo "FAIL: missing required not refused"; echo "$mreq"; exit 1; }
munk=$(python3 -m kimiya run greet.kim name=A bogus=1 2>&1 || true)
grep -q "unknown parameter" <<<"$munk" \
  || { echo "FAIL: unknown param not refused"; echo "$munk"; exit 1; }
pout=$(KIMIYA_MOCK=1 python3 -m kimiya run greet.kim name=Ada times=3)
grep -q '"hi Ada; hi Ada; hi Ada; "' <<<"$pout" \
  || { echo "FAIL: param values not applied"; echo "$pout"; exit 1; }
grep -q "params : name='Ada'" <<<"$pout" \
  || { echo "FAIL: params not in certificate"; echo "$pout"; exit 1; }
# compiled artifact: identical contract
KIMIYA_MOCK=1 python3 -m kimiya compile greet.kim --out gr.py >/dev/null
creq=$(python3 gr.py 2>&1 || true)
grep -q "required parameter" <<<"$creq" \
  || { echo "FAIL: compiled missing-required"; echo "$creq"; exit 1; }
cpout=$(KIMIYA_MOCK=1 python3 gr.py name=Ada times=3)
grep -q '"hi Ada; hi Ada; hi Ada; "' <<<"$cpout" \
  || { echo "FAIL: compiled param values"; echo "$cpout"; exit 1; }
grep -q "params : name='Ada'" <<<"$cpout" \
  || { echo "FAIL: compiled params line"; echo "$cpout"; exit 1; }

echo "== memo + explore: reuse counted once, exploration excluded =="
out=$(python3 -m kimiya check "$OLDPWD_REPO/tests/bad/memo_misplaced.kim" 2>&1 || true)
grep -q "applies to gen" <<<"$out" \
  || { echo "FAIL: memo_misplaced not rejected"; echo "$out"; exit 1; }
cat > srch.kim <<'KIM'
pool A = "llama3.1:8b"
pool B = "gemma2:9b"
pool C = "mistral:7b"
context k_q:
    domain     = "whether a candidate slogan is catchy"
    preserve   = [tone]
    allow_loss = [length]
best := ""
explore:
    forall i in range(4):
        c := gen<Text>("slogan variant " + str(i)) by A
        if judge<3,2/3> (c |= "catchy") under k_q panel [B, C]:
            best := c
check len(best) > 0
if memo judge<3,2/3> (best |= "catchy") under k_q panel [B, C]:
    if memo judge<3,2/3> (best |= "catchy") under k_q panel [B, C]:
        commit(best)
    else:
        abstain
else:
    abstain
KIM
rm -f .kimiya/memo.json
sout=$(KIMIYA_MOCK=1 python3 -m kimiya run srch.kim)
grep -q "COMMITTED" <<<"$sout" || { echo "FAIL: srch"; echo "$sout"; exit 1; }
grep -q "explored : 4" <<<"$sout" \
  || { echo "FAIL: exploration not excluded"; echo "$sout"; exit 1; }
grep -q "memo   : 1 reuse" <<<"$sout" \
  || { echo "FAIL: memo reuse not counted"; echo "$sout"; exit 1; }
python3 - <<'PY' || { echo "FAIL: theta factor counted more than once"; exit 1; }
import json
c = json.load(open(".kimiya/certificate.json"))
assert len(c["theta_factors"]) == 1, c["theta_factors"]
assert c["theta"] == 0.6, c["theta"]
assert c["explored"] == 4 and c["memo_hits"] == 1, c
PY
# compiled: identical accounting, plus cross-run memo persistence
KIMIYA_MOCK=1 python3 -m kimiya compile srch.kim --out srch.py >/dev/null
rm -f .kimiya/memo.json
c1=$(KIMIYA_MOCK=1 python3 srch.py)
grep -q "memo   : 1 reuse" <<<"$c1" \
  || { echo "FAIL: compiled memo"; echo "$c1"; exit 1; }
c2=$(KIMIYA_MOCK=1 python3 srch.py)
grep -q "memo   : 2 reuse" <<<"$c2" \
  || { echo "FAIL: cross-run memo persistence"; echo "$c2"; exit 1; }
grep -q "12 votes" <<<"$c2" \
  || { echo "FAIL: cross-run memo did not save votes"; echo "$c2"; exit 1; }

echo "== counterfactual: kernel space, explored screens, one-factor verdict =="
rm -f .kimiya/memo.json
k1=$(KIMIYA_MOCK=1 python3 -m kimiya run counterfactual.kim)
grep -q "COMMITTED" <<<"$k1" || { echo "FAIL: counterfactual"; echo "$k1"; exit 1; }
grep -q "explored : 8" <<<"$k1" \
  || { echo "FAIL: screens not excluded"; echo "$k1"; exit 1; }
grep -q "change hours" <<<"$k1" \
  || { echo "FAIL: minimal intervention not picked"; echo "$k1"; exit 1; }
python3 - <<'PY' || { echo "FAIL: invoice grew with the search"; exit 1; }
import json
c = json.load(open(".kimiya/certificate.json"))
assert len(c["theta_factors"]) == 1, c["theta_factors"]
assert c["explored"] == 8 and c["params"]["max_changes"] == 1.0, c
PY
k2=$(KIMIYA_MOCK=1 python3 -m kimiya run counterfactual.kim max_changes=2)
grep -q "explored : 32" <<<"$k2" \
  || { echo "FAIL: pairs space"; echo "$k2"; exit 1; }
grep -q "memo   : 1 reuse" <<<"$k2" \
  || { echo "FAIL: verdict not reused across runs"; echo "$k2"; exit 1; }
# compiled artifact: same shape
KIMIYA_MOCK=1 python3 -m kimiya compile counterfactual.kim --out cf.py >/dev/null
k3=$(KIMIYA_MOCK=1 python3 cf.py max_changes=2)
grep -q "explored : 32" <<<"$k3" && grep -q "COMMITTED" <<<"$k3" \
  || { echo "FAIL: compiled counterfactual"; echo "$k3"; exit 1; }

echo "== counterfactual_evolve: free generation, kernel-gated, baseline degrade =="
# Under mock, every invented proposal names a non-mutable factor, so the
# invariant rejects all of them and the enumerated baseline commits.
e1=$(KIMIYA_MOCK=1 python3 -m kimiya run counterfactual_evolve.kim)
grep -q "COMMITTED" <<<"$e1" || { echo "FAIL: evolve"; echo "$e1"; exit 1; }
grep -q "kernel-rejected proposals: 6" <<<"$e1" \
  || { echo "FAIL: invariant did not gate free generation"; echo "$e1"; exit 1; }
grep -q "6 gen" <<<"$e1" \
  || { echo "FAIL: gen population"; echo "$e1"; exit 1; }
python3 - <<'PY' || { echo "FAIL: evolve invoice"; exit 1; }
import json
c = json.load(open(".kimiya/certificate.json"))
assert len(c["theta_factors"]) == 1, c["theta_factors"]
assert "change hours" in str(c["value"]), c["value"]
PY
# population scales; the invoice does not
e2=$(KIMIYA_MOCK=1 python3 -m kimiya run counterfactual_evolve.kim generations=5 children=4)
grep -q "kernel-rejected proposals: 20" <<<"$e2" \
  || { echo "FAIL: evolve params"; echo "$e2"; exit 1; }
python3 - <<'PY' || { echo "FAIL: invoice grew with population"; exit 1; }
import json
c = json.load(open(".kimiya/certificate.json"))
assert len(c["theta_factors"]) == 1, c["theta_factors"]
PY

echo "== grounded screen-read: screenshots feed gen =="
cat > sread.kim <<'KIM'
agent A:
    backend = "ollama"
    model   = "llava:13b"
    vision  = true
schema Reading:
    text: text
shot := observe screen("eDP-1")
check shot.exists
r := gen<Reading>("Read the code shown.", images=[shot]) by A
check len(r.text) > 0
commit(r)
KIM
srout=$(KIMIYA_MOCK=1 KIMIYA_SCREEN=none KIMIYA_SCREEN_FIXTURE="$FIXTURE" \
        python3 -m kimiya run sread.kim)
grep -q "COMMITTED" <<<"$srout" \
  || { echo "FAIL: screen-read"; echo "$srout"; exit 1; }
KIMIYA_MOCK=1 python3 -m kimiya compile sread.kim --out srd.py >/dev/null
srct=$(KIMIYA_MOCK=1 KIMIYA_SCREEN=none KIMIYA_SCREEN_FIXTURE="$FIXTURE" \
       python3 srd.py)
grep -q "COMMITTED" <<<"$srct" \
  || { echo "FAIL: compiled screen-read"; echo "$srct"; exit 1; }

echo "== artifact versioning: stamp + compatibility gate =="
KIMIYA_MOCK=1 python3 -m kimiya compile grounded_summary.kim --out gv.py >/dev/null
grep -q "_COMPILED_WITH = " gv.py \
  || { echo "FAIL: artifact not version-stamped"; exit 1; }
python3 - <<'PY' || { echo "FAIL: compat gate"; exit 1; }
from kimiya.compiled_runtime import check_artifact_compat
from kimiya._version import __version__ as V
assert check_artifact_compat(V) is None                  # same version: silent
assert "recompil" in (check_artifact_compat(None) or "") # pre-stamp: note
maj = int(V.split(".")[0])
for bad in (f"{maj+1}.0.0", f"{maj-1}.9.9", f"{maj}.99.0"):
    try:
        check_artifact_compat(bad)
        raise AssertionError(f"accepted {bad}")
    except SystemExit:
        pass                                             # refused, as promised
assert "recommended" in check_artifact_compat(f"{maj}.0.0")  # older MINOR: note only
PY

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
