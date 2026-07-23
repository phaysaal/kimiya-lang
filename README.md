# kimiya-lang

An interpreter for **Kimiya**, the program logic for semantic computation
and action with language models (the KimiyaPOPL paper) — including the
**world-effecting extension** (`act` / `observe` / `settle` / world-frame
retry). Programs run against a pool of agents — **local by default**
(Ollama at `127.0.0.1`), or remote (a vast.ai pod, OpenRouter) when
declared, with every network egress announced and audited. Programs can
read and write files and drive a GUI (the `screen` surface), with every
world effect announced and audited, and every run ends in an explicit
outcome: a
**certificate** on commit, or a visible **⚡ abstention** — never silence.

Zero dependencies (Python 3.11+ stdlib) — the `anthropic` backend is the one optional extra, and the `claude_cli` backend reaches the same models without it.

## Getting started (first run)

New here? Start with the **offline path** — it validates the whole
toolchain (parser, discipline checker, type checker, compiler, and a full
run) in about a minute, with **no models and no network**:

```bash
git clone git@github.com:phaysaal/kimiya-lang.git
cd kimiya-lang
python3 --version          # must be 3.11+  (see "macOS notes" below)

# 1. parse + discipline checks + type checks — no models, no input files
python3 -m kimiya check examples/grounded_summary.kim

# 2. see the syntax highlighting (core = blue, world extension = magenta)
python3 -m kimiya hl examples/agentic_digest.kim

# 3. compile to a standalone Python file and read it
python3 -m kimiya compile examples/grounded_summary.kim
head -40 examples/grounded_summary.py

# 4. run end-to-end with the offline mock oracle (deterministic, no network)
printf 'The deadline moved to Friday.\nBudget unchanged.\n' > notes.txt
KIMIYA_MOCK=1 python3 -m kimiya run examples/grounded_summary.kim
```

Step 4 should print a **certificate** (`status: COMMITTED`, θ, cost,
`egress: none`). If it does, everything works.

Then a **real run** against local models (Apple Silicon runs these fast
via Metal):

```bash
brew install ollama && ollama serve        # in a separate terminal
ollama pull llama3.1:8b                     # generator  (family: llama)
ollama pull gemma2:9b                        # judge      (family: gemma)
ollama pull mistral:7b                       # judge      (family: mistral)

python3 -m kimiya doctor                     # expect ≥2 families ✓
python3 -m kimiya run examples/grounded_summary.kim
```

`grounded_summary.kim` names three pools `A`/`B`/`C`; pull those model
names or edit the three `pool` lines to match what you have. Two or more
distinct **families** are required — one family cannot certify its own
output (that is a theorem, not a preference). To run against OpenRouter or
a vast.ai pod instead, see **Agents** below.

### macOS notes

- **Python version.** macOS's system `python3` may be 3.9. If
  `python3 --version` is below 3.11, `brew install python@3.12` and use
  that interpreter.
- **Don't start with `tests/smoke_test.sh`.** It uses bash associative
  arrays (`declare -A`, bash 4+), but macOS ships bash 3.2 — the script
  will error. The `python3 -m kimiya …` commands above are pure Python and
  have no such issue. If you want the smoke test, `brew install bash`
  first.
- **No build step.** The interpreter and the compiler are pure-stdlib
  Python with no native code, so nothing to compile or install; clone and
  run. The compiled `.py` artifacts only import `kimiya.compiled_runtime`.

## Commands

```bash
python -m kimiya check   prog.kim   # parse + static discipline checks
python -m kimiya run     prog.kim   # execute; prints the certificate
python -m kimiya compile prog.kim   # transpile to a standalone prog.py
python -m kimiya hl      prog.kim   # ANSI highlight (--html for a page)
python -m kimiya doctor             # local models / family diversity
python -m kimiya calibrate .kimiya  # label judgments; tighten datasheets
python -m kimiya datasheet sheets.json .kimiya --source "…"
                                    # install an externally measured instrument
```

## Compilation

`kimiya compile prog.kim` emits `prog.py`: the program's control flow
inlined as plain Python, calling a small shared runtime for the semantic
operations. It is **not** a speed play — runtime is dominated by model
latency, not interpreter dispatch. What it buys:

- **Distribution** — `python prog.py` runs without the evaluator; only
  `kimiya.compiled_runtime` is imported.
- **Ahead-of-time discipline** — the static checks run at compile time, so
  a compiled artifact is *guaranteed well-formed*: an ill-formed program
  never compiles.
- **Audit** — the emitted Python is the straight-line spine of the
  program; a reviewer sees exactly what executes (retry loops, the
  verified gate, egress banners, and the same certificate all appear
  literally in the source).

Python extensions are re-loaded by the compiled artifact under the same
audit rules (announced, SHA-checked). The interpreter and the compiled
runtime are separate code paths, kept honest by the smoke test, which
runs both and checks they commit with the same result.

Environment: `KIMIYA_OLLAMA_PORT` (default 11434), `KIMIYA_TIMEOUT`
(seconds per model call), `KIMIYA_MOCK=1` (offline deterministic oracle,
used by the test suite), `KIMIYA_SCREEN` (`xdotool` | `none` — see the
screen surface below).

## A complete program

```
pool A = "llama3.1:8b"          -- generator
pool B = "gemma4:e4b"           -- judge family 1
pool C = "mistral:7b"           -- judge family 2

context k_ev:
    domain     = "grounded entailment of a summary against source notes"
    preserve   = [evidential_support, scope]
    allow_loss = [style, ordering]

schema Summary:
    text: text

notes := observe file("notes.txt")
e := select<0.95>("the project deadline", lines(notes.text)) under k_ev

s := retry budget 3 until judge<5,4/5> (join(e, "\n") |= s.text) under k_ev panel [B, C]:
    s := gen<Summary>("Summarize only what these notes state:\n" + join(e, "\n")) by A

commit(s)
```

`run` prints, and writes to `.kimiya/certificate.json`:

```
── certificate ──────────────────────────────
  status : COMMITTED
  θ      : 0.57   (factors: [('select<0.95>', 0.95), ('entails:k_ev', 0.6)])
  instrument entails:k_ev: α≤0.25 β≥0.60 [prior-grade]
  cost   : 1 gen, 5 votes, 0 acts, 1 observes, 84.2s
  trace  : 9 records (.kimiya/trace.jsonl)
─────────────────────────────────────────────
```

θ multiplies the conservative datasheet ends along the executed path;
instruments start **prior-grade** and tighten as you label judgments with
`calibrate`. Judgments without a cross-provenance panel run but are
flagged UNCERTIFIED (self-judgment never certifies).

## Agents: local, vast.ai pods, and OpenRouter

A **pool member is an agent** — a declared model instance with a backend,
a model id, and (for remote agents) an endpoint and a key. `pool A =
"model"` is sugar for a local Ollama agent; the full form is:

```
agent A:                              -- local (the default)
    backend = "ollama"
    model   = "llama3.1:8b"

agent B:                              -- a vast.ai pod running vLLM
    backend = "openai"                -- any OpenAI-compatible /v1 server
    model   = "Qwen/Qwen3-32B"
    url     = "http://<pod-ip>:8000/v1"
    key_env = "VAST_API_KEY"          -- optional bearer token

agent C:                              -- OpenRouter
    backend = "openrouter"
    model   = "mistralai/mistral-large"
    key_env = "OPENROUTER_API_KEY"
    family  = "mistral"               -- optional J⋪C family override
```

Backends: `ollama` (local unless `url` says otherwise), `openai` (any
OpenAI-compatible endpoint — vLLM, llama.cpp server, LM Studio, a rented
pod), `openrouter`, `anthropic`, `claude_cli`. Provider families are inferred for cross-provenance
panels: `anthropic/…`, `meta-llama/…`, `mistralai/…`, `Qwen/…` all get
the right family so `judge … panel [B, C]` is checked for J⋪C across
*providers*, not just local weight families.

### Claude backends: `claude_cli` and `anthropic`

```
agent L:                          -- headless Claude Code
    backend = "claude_cli"        -- no API key, no SDK, nothing installed
    model   = "claude-opus-4-8"

agent V:                          -- the Anthropic API
    backend = "anthropic"         -- needs ANTHROPIC_API_KEY + `pip install anthropic`
    model   = "claude-opus-4-8"
```

Both are vision-capable, so either can drive a `select` over a screenshot
or sit on a `shows` panel.

`claude_cli` shells out to `claude -p` with `--allowedTools Read`, so the
CLI opens the screenshot off disk itself rather than the interpreter
inlining it. It needs no key and keeps the zero-dependency promise —
**and it is the same path the seenslide GUI harness measured its locator
datasheet through**, which is what makes an imported β≥.975 a measurement
of *this* instrument rather than an assertion about a similar one.

`anthropic` is the one optional dependency (`pip install anthropic`).
It uses **structured outputs** for locates, so a control list comes back
valid by construction instead of by regex salvage, and requests adaptive
thinking for `shows` judgments but not for locates — a locate is a
perception call, and the shipped datasheet was measured without it.

> **Two Claude voters are not a panel.** `family_of` puts every `claude-*`
> model in the `anthropic` family, so a `shows` panel of two Opus agents
> is one pair of eyes certifying its own reading — it runs, and it is
> flagged UNCERTIFIED. `examples/gui_collab.kim` pairs the Opus locator
> with a Gemini judge for exactly this reason.

**Screenshots are egress too.** A program that captures the screen and
sends it to a remote agent is disclosing whatever happened to be on the
display — a different thing from a prompt the program composed. That gets
its own line before the run:

```
⚠ network egress: this program sends prompts to remote agents —
    L → claude-opus-4-8 @ api.anthropic.com (via claude CLI) (claude_cli)
    J2 → google/gemini-2.5-flash @ openrouter.ai (openrouter)
  ⚠ this program also captures the screen — those screenshots leave the
    machine for the agents above
```

**Keys never appear in source.** `key_env` names an environment variable;
the interpreter reads it at call time. Nothing secret is ever parsed,
logged, or written to a certificate.

**Egress is a declared, audited property.** A program that declares only
local agents keeps the nothing-leaves-the-machine guarantee. A program
with remote agents says so in the source, is announced before it runs —

```
⚠ network egress: this program sends prompts to remote agents —
    B → Qwen/Qwen3-32B @ <pod-ip>:8000 (openai)
    C → mistralai/mistral-large @ openrouter.ai (openrouter)
```

— and the certificate carries an `egress` list of every non-local host
its prompts reached. See `examples/hybrid_pool.kim`: local generation, a
remote cross-provenance judge panel.

## The world extension

```
src := observe file("inbox.txt")            -- the only door for world-truth
check len(d.text) > 0                       -- the verified gate...
act file.overwrite("digest.txt", d.text)    -- ...for an irreversible effect
settle until check file_exists("digest.txt") within 5
```

File actions and their effect classes: `file.create` / `file.append` /
`file.mkdir` (recoverable), `file.overwrite` / `file.delete`
(**irreversible**). Classes can be re-declared:
`effect file.append irreversible`.

### The `screen` surface — seeing and driving a GUI

```
shot := observe screen("eDP-1")      -- the only door for what is displayed
hits := select<0.97>("the Create Group button", shot) under k_ui by L
b    := first(hits)
act screen.click(b.x, b.y)           -- the only door for input effects
```

```
act screen.click(x, y)               -- navigate, open, focus  (recoverable)
act screen.type("Release notes")
act screen.key("Return")             -- keysym or chord: "ctrl+a"
act screen.drag(x1, y1, x2, y2)
act screen.scroll(x, y, ticks)       -- negative ticks scroll up

check row_count("talks", name) == 1  -- the verified gate...
act screen.confirm(x, y)             -- ...for the click that commits
```

GUI input is a world effect, so it belongs in `act` — which is the whole
point: K4 (no irreversible act in a retry), K5 (no unguarded irreversible
act) and K6 (no unframed world effect in a retry body) then apply to
clicking exactly as they do to files. See `examples/gui_publish.kim`.

**Why two clicking actions.** Irreversibility is a property of the
control, not the coordinates: whether clicking (900, 412) can be undone
depends on whether that pixel says "Cancel" or "Delete forever", and the
language cannot know which. Classifying *every* click as irreversible
would make K5 unusable noise. So the surface splits clicking in two and
the program states its own claim — `screen.click` recoverable,
`screen.confirm` irreversible — and a reviewer sees that claim in the
source. Override in the usual way when a program knows better:
`effect screen.confirm recoverable`.

**Delivery is verified, not assumed.** `xdotool` synthesizes events at
the X server, so the application cannot distinguish them from a human.
But a multi-monitor X screen is a *bounding box, not a union*: an
L-shaped layout leaves uncovered regions, and X silently clamps a pointer
sent there to the nearest valid pixel — the click lands somewhere the
program never named while the trace records what it asked for. Every
move is therefore read back before any button is pressed, and a
divergence is an error, not a warning:

```
✗ pointer did not reach (640, 480) — it is at (1920, 480). That
  coordinate is outside every connected monitor …
```

**Screen acts are announced**, like network egress, before the program
runs — GUI control is an effect on the user's own machine:

```
⚠ GUI control: this program synthesizes real input on your display —
    4 screen act(s), 1 irreversible
    driver: xdotool on display :0   (KIMIYA_SCREEN=none records without delivering)
```

and the certificate carries `screen: 4 act(s) via xdotool on screen::0`.

Drivers: `KIMIYA_SCREEN=xdotool` (default) delivers; `KIMIYA_SCREEN=none`
records the acts and delivers nothing. `KIMIYA_MOCK=1` implies `none`, so
the test suite can never touch a real cursor.

### Actors: `display` declarations and `act<A>` / `observe screen<A>` / `settle<A>`

A two-user scenario needs two *seats* — places where a screen exists and
input can land. Declare them, then index the world constructs:

```
display A:
    monitor = "eDP-1"        -- a region of the ambient X display

display B:
    x11 = ":1"               -- another X server on this machine
    -- or: ssh = "user@lab"  -- another machine entirely

a := observe screen<A>()                  -- A's screen (its monitor default)
act<A> screen.click(plus.x, plus.y)       -- input lands on A's seat
settle<A> until check group_exists(g) within 12
b := observe screen<B>()                  -- B's seat: independent world
act<B> screen.type(code)
```

Display fields: `x11` (X display; remote seats default to `:0`), `ssh`
(a remote host — xdotool and the screenshot tool run there over
BatchMode ssh, and the PNG streams back), `monitor` (default capture
region by xrandr name). An unindexed `act screen...` / `observe
screen(...)` is the ambient local display, unchanged.

What the index buys:

- **Per-seat coordinate spaces.** Every capture carries its seat and
  origin; a box found in B's capture can only produce a click delivered
  to B. Cross-seat confusion is unrepresentable, not just unlikely.
- **Per-seat freshness.** Each seat is its own world in the stale-read
  ledger; acting on A never marks B's observations stale. A successful
  `settle<A>` counts as an observation of A's world.
- **K13.** An actor index must name a declared display, and only the
  screen surface has seats — `act<A> file.delete(...)` is rejected at
  check time.
- **Disclosure.** The pre-run banner lists every actor and where it
  resolves (ssh seats are called out — input and screenshots travel to
  that machine), and the certificate carries the actor table:

  ```
    screen : 8 act(s) via none on screen::0, 4 locate(s)
    actor  : A → :0 (local)
    actor  : B → user@lab :0 (ssh)
  ```

**One honest caveat:** two actors that resolve to the same X server
share one pointer and one keyboard focus — the names then label intent,
not independence, and interleaving their acts can interfere. Distinct
`x11`/`ssh` seats are genuinely independent (verified live: a click and
typing on a nested `:7` server left the ambient pointer untouched).
Per-actor recordings for offline runs: `KIMIYA_SCREEN_FIXTURE_A=...`
falls back to the shared `KIMIYA_SCREEN_FIXTURE`.

### Looking: `observe screen(...)` and the vision locator

```
shot := observe screen()             -- the whole X screen
shot := observe screen("eDP-1")      -- one monitor, by xrandr name
shot := observe screen(0, 561, 1920, 1200)   -- an explicit region
```

The record is `{path, sha, width, height, x, y, display, region, driver,
exists}`. `x`/`y` are the **capture origin**, and they are the reason a
two-monitor test works: a box found inside a capture is mapped through
that origin, so `b.x`/`b.y` are absolute screen coordinates that `act
screen.click` can use directly. Capture uses whichever of `maim`,
`import` (ImageMagick), `scrot` or `gnome-screenshot` is installed.

`select` over a screenshot is the same construct as `select` over a list
— retrieval, best first — with a vision model behind it instead of a
keyword filter. It returns controls: `{x, y, w, h, left, top, label,
confidence}`, where `x, y` is the centre.

**A screen store carries obligations a text store does not** (K10–K12),
because it stops being a mechanical filter and becomes an instrument:

| rule | rejected program shape |
|---|---|
| K10 | `select` over a screenshot with no `by <agent>` (nothing named what read the image) or no `under <purpose>` (nothing to key the datasheet by) |
| K11 | `by` a model that cannot see — it would answer about an image it never saw |
| K12 | `shows(...)` over a non-screenshot, or with panel members that cannot see |

Vision capability is inferred from the model id and can be stated:

```
agent L:
    backend = "ollama"
    model   = "llama3.2-vision:11b"
    vision  = true
```

### `shows` — judging a screenshot

```
if judge<3,2/3> shows(shot, "a success view with an 8-character join code")
        under k_state panel [J1, J2]:
```

A fourth judged relation alongside `|=`, `~` and `contradicts`, whose
left side is an image. The panel votes on the screenshot itself, and the
cross-provenance rule (J ⋪ C) applies unchanged — one family still
cannot certify its own output, and now one *pair of eyes* cannot either.

### θ takes the measured rate, not the claim

This is the point of routing a locate through the language rather than
calling a vision API. A vision `select` contributes its datasheet's
conservative end to θ under the task `locate:<context>` — exactly like a
judged relation — **not** the recall written in the source. The declared
recall stays as the programmer's coverage claim, and it is checked
against the measured β on every run:

```
⚠ line 46: declared recall 0.97 exceeds the measured β≥0.600 of
  instrument locate:k_ui — θ uses the measured end, not the claim
```

Instruments start prior-grade. An instrument measured by a separate
campaign can be installed, and provenance is mandatory — a sheet with no
source is a number with no history:

```bash
python -m kimiya datasheet sheets.json .kimiya \
  --source "harness campaign, 58 runs / 343 locate calls"
```

The certificate then cites where the numbers came from, and θ moves:

```
  θ      : 0.8599   (factors: [('locate:k_ui', 0.9746), … ('shows:k_state', 0.9763)])
  instrument locate:k_ui: α≤0.16 β≥0.97 [measured: harness campaign, 58 runs …]
  screen : 8 act(s) via xdotool on screen::0, 4 locate(s)
```

The same program against prior-grade sheets commits at θ = 0.047. Both
runs pass; they are not equally strong evidence, and the certificate is
what says so. See `examples/gui_collab.kim` — a two-user collaboration
test (create a group, carry the join code, verify the chat) where the two
actors are two monitors of one X screen.

### Replay: cached locates

Every live locate is stored in `.kimiya/locates.json`, keyed by (task,
capture size, description). The cache has two grades, and the difference
is epistemic, not mechanical:

- An **exact** hit — the current screenshot's SHA matches the cached
  one — is a reading of this very image. Byte-identical pixels,
  identical answer; it is free, silent, and enters θ at the datasheet
  rate. Always on.
- A **replay** hit (`kimiya run prog.kim --replay`, or `KIMIYA_REPLAY=1`
  for compiled artifacts) reuses cached boxes although the pixels have
  changed. Zero locate model calls; a scenario that took minutes reruns
  in seconds.

Replay is sound *only because the locate never carries the verdict*: a
stale coordinate produces a wrong click, the world diverges, and the
live gates — kernel checks and `shows` judges, which are **never**
cached — refuse to commit. The certificate counts replays and says the
assumption out loud:

```
  screen : 8 act(s) via none on screen::0, 4 locate(s) (4 replayed)
  ⚠ 4 locate(s) replayed from a prior run against changed pixels —
    layout stability is assumed, not measured; the verdict gates
    (checks, judges) still ran live
```

Replay against a cache that has no entry for a description **abstains**
rather than inventing coordinates — run live once first. Judgments are
never cached under either mode: a `shows` is a claim about the current
state of the world, and yesterday's screen is not evidence about
today's.

### Driving a GUI

**Do not smuggle clicks through a Python extension.** `pyfn click =
"gui.click"` would run, but `pyfn` is declared **kernel-grade, certainty
1**, and K4/K5/K6 only inspect `act` statements — every effect hidden
that way escapes the irreversibility discipline while the certificate
goes on claiming certainty for the path. Python is the kernel extension
mechanism for deterministic computation; effects go through `act`.

## The checker IS the paper's discipline

`kimiya check` rejects, before any model runs:

| rule | rejected program shape |
|---|---|
| K1 | a judged relation citing no declared purpose (silent semantic equality) |
| K2 | a retry whose judge panel shares the generator's model family (J ⋪ C) |
| K3 | `select` with recall outside (0,1] — coverage claims need a stated recall |
| K4 | an **irreversible act inside a retry body** (`file.delete`, `screen.confirm`) |
| K5 | an **unguarded irreversible act** (no check/judged gate before it) |
| K6 | a retry whose body touches the world with **no `inv`/`compensate`** — snapshot retry over an external world is unsound |
| K7–K9 | undeclared schemas/names, non-positive budgets and deadlines |

`tests/bad/` contains one minimal program per rule; the smoke test asserts
each is rejected **with the right diagnosis**.

## Lightweight type checking

A gradual type checker runs alongside the discipline checker (on `check`,
`run`, and `compile`) and catches shape bugs *before* a model call is
spent — without false positives. Anything it cannot pin down is
`Unknown`, and `Unknown` never errors. It knows that:

- `gen<Schema>` yields a record with the schema's fields; `gen<Text>`
  yields text; `observe file(...)` yields `{text, path, exists, mtime,
  sha}`;
- `select` / `lines` / `keys` / `range` yield lists; `join` / `lower` /
  `trim` / `str` yield text; `len` / `num` yield num; and so on.

So it rejects, at compile time:

| shape bug | example |
|---|---|
| field typo on a schema record | `s.txet` where `schema` has `text` |
| field access on a scalar | `t.text` where `t := gen<Text>(…)` |
| `forall` over a text | `forall c in notes.text:` (needs `lines(...)`) |
| `select` over a non-list | `select<…>(q, notes.text)` (needs `lines(...)`) |

It is deliberately *not* the paper's purpose/tolerance type system (the
graded-effect discipline for substitution safety, supplement A.9): that
one earns its keep only once the language has a substitution construct,
which this version does not. This checker is the ergonomic layer — it
turns a class of "paid for a model call, then crashed on a typo" bugs
into instant compile errors.

## Grammar sketch

```
program  := (decl | stmt)*
decl     := pool NAME = STRING
          | display NAME: (x11|ssh|monitor = STRING)*
          | context NAME: (domain|preserve|allow_loss = ...)+
          | schema NAME: (field: type)+
          | effect SURFACE.ACTION (irreversible|recoverable)
stmt     := NAME := rhs | check E | print E | commit(E) | abstain
          | if GUARD: BLOCK [else: BLOCK]
          | forall NAME in E: BLOCK
          | retry budget N until GUARD: BLOCK [inv E] [compensate: BLOCK]
          | act[<ACTOR>] SURFACE.ACTION(args)
          | settle[<ACTOR>] until GUARD within SECONDS
rhs      := gen<SCHEMA>(E) [by POOL]
          | select<RECALL>(E, E) [under CTX]
          | observe file(E)
          | retry ...            -- value = body's last assignment
          | E
GUARD    := check E
          | judge<K,TAU> (E |= E | E ~ E | E contradicts E) under CTX
            [panel [P,...]] [paraphrase_prompts N]
          | judge<K,TAU> shows(E, E) under CTX [panel [P,...]]
rhs      := ... | select<RECALL>(E, E) [under CTX] [by POOL]
                                       -- `by` required for a screen store
          | observe screen[<ACTOR>]([NAME | x, y, w, h])
```

Comments `--`; indentation is significant (spaces only). Builtins: `len
contains starts_with lower trim lines join str num hash now range first
last keys file_exists map filter sort_by sum`.

## Functions, modules, and Python interop

```
fn bulletize(items):                 -- functions are first-class values
    out := ""
    forall i in items:
        out := out + "- " + str(i) + "\n"
    return out

use "textlib.kim"                    -- module: pure declarations only
use python "pystats.py"              -- python file: kernel functions
pyfn mean = "statistics.mean"        -- one dotted python callable

xs := map(num, some_lines)           -- functions/builtins pass to map/filter
ys := sort_by(last, pairs)
```

Design rules, and why:

- **Functions see only their parameters** (and other functions) — no
  global capture. Explicit data flow keeps the audit story simple.
- **Modules are pure**: a used `.kim` file may contain only declarations
  and `fn`s; a top-level statement in a module is a load error.
  Declarations merge flat; duplicate names collide loudly.
- **A Python function is a kernel instrument.** Deterministic code is
  exactly what `check`-grade computation is, so Python is the kernel
  extension mechanism — for data-heavy work (parsing, statistics,
  hashing), not for smuggling semantics. The price is honesty about the
  audit surface: every loaded extension is announced at check/run time
  and cited in the certificate by **file SHA** (or dotted path), exactly
  like a datasheet:

  ```
  ⚠ python extension loaded: pystats.py (sha afb752e7…) — kernel-grade,
    audit this file
  ...
  "python_extensions": [{"kind": "file", "path": "pystats.py",
                         "sha": "afb752e7...", "functions": [...]}]
  ```

  A Python extension escapes the language's guarantees (it can do
  anything Python can); the language's job is to make that visible, not
  to pretend otherwise. See `examples/data_pipeline.kim`: Python does
  the arithmetic at certainty 1, models do only judged interpretation.

## Editor support

`editors/vscode-kimiya/` is a VS Code extension (syntax highlighting for
`.kim`, core keywords vs the magenta world extension, matching the
paper's typography). Install with:

```bash
cp -r editors/vscode-kimiya ~/.vscode/extensions/
```

## Honest limitations (v0.1)

- `select` is a mechanical keyword filter over a list; the declared
  recall is carried into the certificate but is the *programmer's* claim
  about this retrieval, not a measured property of the filter.
- θ accounting is the simple per-step conservative product; abstention
  probability is not separately bounded.
- Retry snapshot semantics restores *program* state only (correct per the
  paper — which is exactly why K6 forbids unframed world effects).
- Freshness is tracked per file path and violations are trace warnings,
  not type errors (the paper's full obligation needs taint analysis).
- Two surfaces (`file`, `screen`). Sockets, processes, and clipboard are
  future surfaces; each will need its own effect classes and delivery
  contracts.
- `select` over a **text** store is still a mechanical keyword filter and
  its declared recall is still the programmer's claim. Only the vision
  path is instrument-backed.
- Remote (`ssh`) seats need key-based auth (BatchMode — the harness
  fails fast rather than hanging on a password prompt), xdotool, and
  maim or ImageMagick on the remote host. Latency per act is one ssh
  round trip.
- Actors sharing one X server share one pointer — see the caveat under
  Actors above.
- `observe screen(...)` under `KIMIYA_SCREEN=none` serves
  `KIMIYA_SCREEN_FIXTURE` if set and otherwise reports `exists: false`;
  a program that does not check `.exists` will abstain rather than judge
  a screenshot that was never taken.
- Vision capability is inferred from model-id patterns, which will lag
  new releases; `vision = true` is the override.
- Boxes are read as **pixels of the captured image** (`[x0, y0, x1, y1]`),
  the convention the measured datasheet was collected under. A model that
  answers in normalized 0–1000 coordinates will be misread — there is no
  unit sniffing.
- `claude_cli` latency is ~6–15s per call (p50 ≈ 9s in the harness
  campaign), so a *first* run of a scenario with a dozen locates takes
  minutes. Later runs hit the locate cache: free when the screen is
  byte-identical, free-and-disclosed under `--replay` when it isn't.
- The locate cache keys on capture size, not on app version or theme —
  replaying after a UI redesign reuses coordinates for a layout that no
  longer exists. The gates catch the divergence (the run abstains), but
  the cache won't warn you first; delete `.kimiya/locates.json` after
  UI changes.
- `screen.type` text is echoed into the trace (truncated at 200 chars)
  because that is what makes a run auditable — so do not type secrets;
  there is no `key_env` indirection for typed input yet.
- Effect classes are per action name, not per call site: declaring
  `effect screen.click irreversible` gates *every* click in the program.
  Use `screen.confirm` for the ones that commit.
- xdotool drives X11. Under Wayland it cannot synthesize input to native
  clients; run the app in an Xorg session or an Xwayland-backed nested
  server.
- `case` and store invariants elaborate away in the paper; here, spell
  them as nested `if`.

## Tests

```bash
bash tests/smoke_test.sh
```
