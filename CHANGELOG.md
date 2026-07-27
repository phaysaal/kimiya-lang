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
