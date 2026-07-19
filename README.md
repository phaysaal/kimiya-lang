# kimiya-lang

An interpreter for **Kimiya**, the program logic for semantic computation
and action with language models (the KimiyaPOPL paper) — including the
**world-effecting extension** (`act` / `observe` / `settle` / world-frame
retry). Programs run against a pool of **local models only** (Ollama at
`127.0.0.1`), can read and write files, and every run ends in an explicit
outcome: a **certificate** on commit, or a visible **⚡ abstention** —
never silence.

Zero dependencies (Python 3.11+ stdlib).

## Commands

```bash
python -m kimiya check prog.kim     # parse + static discipline checks
python -m kimiya run   prog.kim     # execute; prints the certificate
python -m kimiya hl    prog.kim     # ANSI highlight (--html for a page)
python -m kimiya doctor             # local models / family diversity
python -m kimiya calibrate .kimiya  # label judgments; tighten datasheets
```

Environment: `KIMIYA_OLLAMA_PORT` (default 11434), `KIMIYA_TIMEOUT`
(seconds per model call), `KIMIYA_MOCK=1` (offline deterministic oracle,
used by the test suite).

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
pod), `openrouter`. Provider families are inferred for cross-provenance
panels: `anthropic/…`, `meta-llama/…`, `mistralai/…`, `Qwen/…` all get
the right family so `judge … panel [B, C]` is checked for J⋪C across
*providers*, not just local weight families.

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

## The checker IS the paper's discipline

`kimiya check` rejects, before any model runs:

| rule | rejected program shape |
|---|---|
| K1 | a judged relation citing no declared purpose (silent semantic equality) |
| K2 | a retry whose judge panel shares the generator's model family (J ⋪ C) |
| K3 | `select` with recall outside (0,1] — coverage claims need a stated recall |
| K4 | an **irreversible act inside a retry body** |
| K5 | an **unguarded irreversible act** (no check/judged gate before it) |
| K6 | a retry whose body touches the world with **no `inv`/`compensate`** — snapshot retry over an external world is unsound |
| K7–K9 | undeclared schemas/names, non-positive budgets and deadlines |

`tests/bad/` contains one minimal program per rule; the smoke test asserts
each is rejected **with the right diagnosis**.

## Grammar sketch

```
program  := (decl | stmt)*
decl     := pool NAME = STRING
          | context NAME: (domain|preserve|allow_loss = ...)+
          | schema NAME: (field: type)+
          | effect SURFACE.ACTION (irreversible|recoverable)
stmt     := NAME := rhs | check E | print E | commit(E) | abstain
          | if GUARD: BLOCK [else: BLOCK]
          | forall NAME in E: BLOCK
          | retry budget N until GUARD: BLOCK [inv E] [compensate: BLOCK]
          | act SURFACE.ACTION(args)
          | settle until GUARD within SECONDS
rhs      := gen<SCHEMA>(E) [by POOL]
          | select<RECALL>(E, E) [under CTX]
          | observe file(E)
          | retry ...            -- value = body's last assignment
          | E
GUARD    := check E
          | judge<K,TAU> (E |= E | E ~ E | E contradicts E) under CTX
            [panel [P,...]] [paraphrase_prompts N]
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
- One surface (`file`). Sockets, processes, and clipboard are future
  surfaces; each will need its own effect classes and delivery contracts.
- `case` and store invariants elaborate away in the paper; here, spell
  them as nested `if`.

## Tests

```bash
bash tests/smoke_test.sh
```
