"""AST node definitions for Kimiya (core + world extension)."""

from __future__ import annotations

from dataclasses import dataclass, field


# ---------------- expressions ----------------

@dataclass
class Lit:
    value: object
    line: int = 0


@dataclass
class Var:
    name: str
    line: int = 0


@dataclass
class ListExpr:
    items: list
    line: int = 0


@dataclass
class RecordExpr:
    fields: list  # [(name, expr)]
    line: int = 0


@dataclass
class Field:
    obj: object
    name: str
    line: int = 0


@dataclass
class Index:
    obj: object
    index: object
    line: int = 0


@dataclass
class Call:
    func: str
    args: list
    line: int = 0


@dataclass
class BinOp:
    op: str
    left: object
    right: object
    line: int = 0


@dataclass
class UnOp:
    op: str
    operand: object
    line: int = 0


@dataclass
class ObserveExpr:
    surface: str          # "file" | "screen"
    args: list
    actor: str | None = None   # `observe screen<A>(...)` — a declared display
    line: int = 0


# ---------------- guards ----------------

@dataclass
class CheckGuard:
    expr: object
    line: int = 0


@dataclass
class JudgeGuard:
    k: int
    tau: float
    relation: str         # "entails" | "equiv" | "contradicts" | "rubric"
    left: object
    right: object | None
    context: str
    panel: list | None    # pool names, or None for default
    paraphrases: int
    memo: bool = False    # exact-input reuse: one reading, counted once
    line: int = 0


# ---------------- declarations ----------------

@dataclass
class PoolDecl:
    name: str
    model: str
    line: int = 0


@dataclass
class ParamDecl:
    """A program's typed CLI/embedding interface: `param name: text = "x"`.

    Backend-neutral by design: the interpreter maps params to `name=value`
    argv pairs, the Python compiler emits the same contract into the
    artifact, and a future native backend maps the same table to its own
    CLI or FFI. Resolved values are recorded in the certificate.
    """

    name: str
    type: str             # "text" | "num" | "bool"
    default: object = None
    required: bool = False
    line: int = 0


@dataclass
class DisplayDecl:
    name: str
    fields: dict          # x11, ssh, monitor
    line: int = 0


@dataclass
class AgentDecl:
    name: str
    fields: dict          # backend, model, url, key_env, family
    line: int = 0


@dataclass
class ContextDecl:
    name: str
    domain: str
    preserve: list
    allow_loss: list
    line: int = 0


@dataclass
class SchemaDecl:
    name: str
    fields: list          # [(name, type)]
    line: int = 0


@dataclass
class EffectDecl:
    surface: str
    action: str
    klass: str            # "irreversible" | "recoverable"
    line: int = 0


# ---------------- statements ----------------

@dataclass
class Assign:
    name: str
    rhs: object
    line: int = 0


@dataclass
class GenExpr:
    schema: str
    prompt: object
    by: str | None
    memo: bool = False    # exact-input reuse: same prompt -> same artifact
    line: int = 0


@dataclass
class SelectExpr:
    recall: float
    query: object
    store: object
    context: str | None
    by: str | None = None     # instrument agent, required for a screen store
    line: int = 0


@dataclass
class CheckStmt:
    expr: object
    line: int = 0


@dataclass
class PrintStmt:
    expr: object
    line: int = 0


@dataclass
class CommitStmt:
    expr: object
    line: int = 0


@dataclass
class AbstainStmt:
    line: int = 0


@dataclass
class IfStmt:
    guard: object
    then: list
    els: list | None
    line: int = 0


@dataclass
class ForallStmt:
    var: str
    iterable: object
    body: list
    line: int = 0


@dataclass
class RetryStmt:
    budget: int
    body: list
    guard: object
    inv: object | None = None
    compensate: list | None = None
    line: int = 0


@dataclass
class ActStmt:
    surface: str
    action: str
    args: list
    actor: str | None = None    # `act<A> screen...` — a declared display
    line: int = 0
    gated_by_if: bool = False   # set by the checker's verified-gate pass


@dataclass
class ExploreStmt:
    """`explore:` — a search region whose judged/select factors are
    trace-recorded but excluded from θ. Exploration gates progress, never
    the verdict: anything the block finds must be re-established by the
    gates outside it, so its instrument error converts to wasted budget,
    not certificate weakness. The checker forbids commit and irreversible
    acts inside (K14) — a verdict cannot rest on unaccounted judgments.
    """

    body: list
    line: int = 0


@dataclass
class SettleStmt:
    guard: object
    within: float
    actor: str | None = None    # `settle<A> ...` — whose world settled
    line: int = 0


@dataclass
class FnDecl:
    name: str
    params: list
    body: list
    line: int = 0


@dataclass
class ReturnStmt:
    expr: object | None
    line: int = 0


@dataclass
class UseDecl:
    path: str
    python: bool
    line: int = 0


@dataclass
class PyFnDecl:
    name: str
    target: str          # dotted path, e.g. "statistics.mean"
    line: int = 0


@dataclass
class Program:
    decls: list = field(default_factory=list)
    body: list = field(default_factory=list)
