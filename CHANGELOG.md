# Changelog

Versioning is MAJOR.MINOR.PATCH, **pre-stable**: until 2.0 the language
surface may change between MINOR versions. The compatibility contract:

- Within one MAJOR version, the runtime stays backward compatible with
  compiled artifacts; a MINOR mismatch prints a note, never breaks.
- Across MAJOR versions compatibility is **not promised**. Artifacts
  record the compiler version (`_COMPILED_WITH`, since 1.4.0) and refuse
  to run against an incompatible runtime rather than failing ambiguously.
- Every break — of programs, artifacts, or on-disk formats — is listed
  here under a **Breaking** marker. Old releases stay downloadable in the
  kimiya.dev archive.
- Every certificate records the version that produced it
  (`kimiya : vX.Y.Z`, since 1.3.1; compiled runs also record
  `compiled_with` since 1.4.0), so results are attributable to a
  language state.

## 1.8.0 — 2026-08-04
- `param name: secret` — text that computes normally but never appears
  on an audit surface. The certificate (`params` and a committed value),
  the trace echo of `screen.type`/`paste`, and `print` all record only
  `<redacted:sha8>` — a marker plus a SHA-256 prefix, enough for an
  auditor to confirm two runs used the same secret without seeing it.
  Redaction is provably harmless: audit locality already excludes
  runtime values from a certificate's meaning, so no guarantee weakens.
- `name=env:VAR` on a secret param reads the value from the environment
  at resolve time — off the command line, off shell history — refusing
  while refusal is still free if the variable is unset. Works
  identically for the interpreter, compiled artifacts, and embedding
  hosts (the dereference lives in the shared `resolve_params`).
- A secret param cannot have a default: a secret literal in source is
  disclosed to every reader, and the checker rejects it
  (`tests/bad/secret_default.kim`).
- Honest limit, stated in the README: redaction is per-value, not taint
  analysis — strings derived from a secret are plain text, and `paste`
  still leaves the real value on the seat's clipboard.
- Compiled artifacts now route `print` through the runtime
  (`rt.print_value`); artifacts compiled by 1.8 need a ≥1.8 runtime
  (the existing version stamp already gates this).

## 1.7.0 — 2026-08-04
- **Breaking (θ):** text `select` is now a priced instrument, closing the
  one place where θ took a claim instead of a measurement. Its factor
  enters θ under `select:<purpose>` at the datasheet's conservative end
  (prior-grade β≥0.60 until measured), exactly like the vision locate
  path — **not** at the recall written in the source. The declared
  recall stays the programmer's coverage claim and is checked against
  the measured end on every run (the same overclaim warning the locate
  path prints). A select without `under <purpose>` lands at
  `select:unscoped` (prior grade) with a check-time nudge. Certificates
  for existing programs will show lower θ (the §5.2 example drops from
  0.57 to 0.36 on a fresh workspace) — deliberately conservative, never
  overstating; install a measured retrieval sheet
  (`kimiya datasheet sheets.json .kimiya --source "…"`) to earn it back.
  Rationale: the paper defines `select<ρ_r>`'s recall as
  datasheet-stamped (Definition "Retrieval oracle"), and the Coq
  soundness theorem assumes the oracle meets its datasheet
  (`seldist_recall`); carrying an unmeasured claim into θ violated that
  premise. Mechanical recall-1 retrieval remains the kernel path
  (`filter`, `contains`) and stays factor-free.
- `select:<task>` instruments now appear in the certificate's
  `instruments` block with their calibration status and provenance,
  like every other instrument.

## 1.6.0 — 2026-07-27
- **Semantics (θ):** multimodal `gen` is now a *priced read* — a `gen`
  that consumes images contributes one θ factor at its datasheet's
  conservative end under `read:<purpose>`, declared with a new optional
  `under CTX` on `gen`. Unscoped priced reads land at `read:unscoped`
  (prior grade) with a check-time nudge. Text-only `gen` remains
  factor-free. Certificates for existing multimodal programs will show
  lower θ than under 1.5 — deliberately conservative, never overstating.
  `memo` reads count once per run like every reused reading.
- Read-accuracy campaign for the grounded screen-read
  (`tools/read_campaign.py`): 60 present + 20 absent rendered trials
  against kernel-grade ground truth, via the real language path
  (`observe screen` fixture → `gen images=` → `claude_cli` Opus 4.8).
  Result: 60/60 exact reads (β≥0.9398 Wilson95), 0/20 false reads on
  code-free screens (α≤0.1611), p50 6.6 s. Installable sheet with
  embedded provenance in `datasheets/screen_read.json`
  (`kimiya datasheet datasheets/screen_read.json .kimiya`); per-trial
  audit log alongside. The `read:` task family is recorded for the
  instrument; reads remain untrusted gen by design — wiring a read
  factor into θ is an open design question, noted in the README.

## 1.5.0 — 2026-07-27
- `observe image(path)` adds content-addressed image evidence with source and
  preview SHA tracking.
- `gen<Schema>(prompt, images=[...])` adds first-class multimodal generation;
  image arguments must come from successful image observations.
- JPEG and PNG use the standard-library observation provider. macOS `sips`
  supplies the initial external Fujifilm RAF/RAW preview provider.
- Certificates distinguish image observations from actual remote image
  egress; interpreter and compiled artifacts share the same freshness checks.
- Screenshots feed `gen` directly: `gen<Schema>(prompt,
  images=[observe screen<A>()])` — the grounded screen-read. Freshness
  compares the screenshot's recorded SHA; remote routing is disclosed as
  image egress with `surface: screen` and the seat.

## 1.4.0 — 2026-07-25
- Compiled artifacts are version-stamped (`_COMPILED_WITH`) and checked
  at startup: MAJOR mismatch or artifact-newer-than-runtime refuses with
  a recompile instruction; older-MINOR artifacts run with a note.
- Certificates from compiled runs record `compiled_with`.
- This CHANGELOG introduced; versioned release archive published at
  kimiya.dev.

## 1.3.3 — 2026-07-25
- `examples/counterfactual_evolve.kim`: evolutionary counterfactual
  search — model-invented interventions, schema-checkable kernel
  invariant over free generation, baseline-degrade.

## 1.3.2 — 2026-07-24
- `examples/counterfactual.kim` (+ `bizlib.py`, `business.json`):
  enumerated counterfactual search; one θ factor regardless of space.

## 1.3.1 — 2026-07-24
- Version machinery: `kimiya/_version.py`, `--version`, certificates
  stamp the producing version.

## 1.3.0 — 2026-07-24
- `explore:` — judged/select factors inside are trace-recorded but
  excluded from θ; K14 forbids `commit` and irreversible acts inside.
- `memo` on `gen`/`judge` — exact-input reuse; a reading's θ factor is
  counted once per run; persisted in `.kimiya/memo.json`.

## 1.2.0 — 2026-07-24
- `param` declarations: a program's typed CLI/embedding interface;
  resolution refuses before any model runs; values recorded in the
  certificate.
- **Breaking (artifact CLI):** compiled artifacts no longer take a bare
  positional model list (`python prog.py m1,m2`); use `--models m1,m2`.
  Positional arguments are now `name=value` parameter pairs.

## 1.1.0 — 2026-07-24
- `screen.paste` consistency pass: remote seats fall back xclip → xsel;
  clipboard-persistence disclosure documented; derived-layer tests.

## 1.0.0 — 2026-07-24
- Agent-indexed actors: `display` declarations, `act<A>`,
  `observe screen<A>()`, `settle<A>`; per-seat coordinate and freshness
  worlds; K13. Remote (ssh) seats.
- Behavioral: `xdotool mousemove --sync` dropped (hangs on nested X
  servers); pointer read-back remains the delivery guarantee.

## 0.9.0 — 2026-07-24
- Locate cache with two grades: exact (same-SHA, silent, free) and
  replay (`--replay` / `KIMIYA_REPLAY=1`, disclosed in the certificate).

## 0.8.0 — 2026-07-24
- `claude_cli` and `anthropic` backends (vision-capable); screenshots
  disclosed as their own egress class.
- **Breaking (format):** locate coordinates changed from normalized
  0–1000 boxes to image pixels; locate caches and box conventions from
  0.7.0 are invalid.

## 0.7.0 — 2026-07-24
- The vision instrument: `observe screen(...)`, `select` over
  screenshots (`by` instrument, `under` purpose), `shows` relation,
  K10–K12, `kimiya datasheet` import with mandatory provenance.

## 0.6.0 — 2026-07-24
- The `screen` surface: `act screen.click/confirm/type/key/drag/scroll`
  with verified delivery; K4–K6 applied to GUI effects; `KIMIYA_SCREEN`
  drivers.

## 0.1.0 – 0.5.x — 2026-07-19
- Core language: pools/agents/contexts/schemas, `gen`/`select`/`judge`/
  `check`/`retry`/`commit`/`abstain`, world extension (`act`/`observe`/
  `settle`, world-frame retry), discipline checker K1–K9, gradual type
  checker, compiler to standalone Python, datasheets and calibration,
  functions/modules/pyfn.
