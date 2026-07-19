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
last keys file_exists`.

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
