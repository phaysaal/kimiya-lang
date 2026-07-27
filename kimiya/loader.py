"""Module and Python-extension loading.

- `use "lib.kim"`: a module is a PURE declaration file (pools, contexts,
  schemas, effects, fns, further uses). Top-level statements in a module
  are a load error. Declarations merge flat; duplicate names collide
  loudly. Cycles are detected.
- `use python "helpers.py"`: loads a Python file and exposes its public
  callables as kernel functions. The file's SHA-256 is recorded — it is
  part of the audit surface, cited in the certificate like a datasheet.
- `pyfn name = "pkg.attr"`: binds one dotted Python callable.

Python extensions run arbitrary code and escape the language's
guarantees; the CLI prints them loudly and the certificate lists them.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import sys
from pathlib import Path

from . import ast_nodes as A
from .parser import parse


class LoadError(Exception):
    pass


def load_program(path: Path):
    """Returns (program, py_funcs, py_exts).

    program: the entry file's Program with all used modules' declarations
    merged in. py_funcs: {name: callable}. py_exts: audit records.
    """
    path = path.resolve()
    prog = parse(path.read_text())
    py_funcs: dict = {}
    py_exts: list[dict] = []
    seen: set[Path] = {path}
    _resolve_uses(prog, path.parent, py_funcs, py_exts, seen)
    _check_dupes(prog)
    return prog, py_funcs, py_exts


def _resolve_uses(prog: A.Program, base: Path, py_funcs, py_exts, seen):
    remaining = []
    for d in prog.decls:
        if not isinstance(d, A.UseDecl):
            remaining.append(d)
            continue
        target = (base / d.path).resolve()
        if d.python:
            _load_python(target, py_funcs, py_exts, d.line)
        else:
            if target in seen:
                raise LoadError(f"line {d.line}: module cycle at {d.path}")
            seen.add(target)
            if not target.exists():
                raise LoadError(f"line {d.line}: module not found: {d.path}")
            mod = parse(target.read_text())
            if mod.body:
                first = mod.body[0]
                raise LoadError(
                    f"{d.path}: modules must be pure declarations; found a "
                    f"top-level statement at line {first.line}")
            _resolve_uses(mod, target.parent, py_funcs, py_exts, seen)
            remaining.extend(mod.decls)
    for d in remaining:
        if isinstance(d, A.PyFnDecl):
            _load_dotted(d, py_funcs, py_exts)
    prog.decls = [d for d in remaining if not isinstance(d, A.PyFnDecl)] + \
                 [d for d in remaining if isinstance(d, A.PyFnDecl)]


def _load_python(target: Path, py_funcs, py_exts, line: int):
    if not target.exists():
        raise LoadError(f"line {line}: python extension not found: {target}")
    sha = hashlib.sha256(target.read_bytes()).hexdigest()[:16]
    spec = importlib.util.spec_from_file_location(
        f"kimiya_ext_{sha}", target)
    if spec is None or spec.loader is None:
        raise LoadError(f"cannot load python extension {target}")
    module = importlib.util.module_from_spec(spec)
    # Standard decorators such as dataclasses.dataclass resolve annotations
    # through sys.modules while the module body executes.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        sys.modules.pop(spec.name, None)
        raise LoadError(f"python extension {target.name} failed to load: "
                        f"{e}") from e
    exported = []
    for name in dir(module):
        if name.startswith("_"):
            continue
        obj = getattr(module, name)
        if callable(obj) and getattr(obj, "__module__", "") == module.__name__:
            py_funcs[name] = obj
            exported.append(name)
    py_exts.append({"kind": "file", "path": str(target), "sha": sha,
                    "functions": exported})


def _load_dotted(d: A.PyFnDecl, py_funcs, py_exts):
    mod_name, _, attr = d.target.rpartition(".")
    if not mod_name:
        raise LoadError(f"line {d.line}: pyfn target must be dotted: "
                        f"{d.target!r}")
    try:
        obj = getattr(importlib.import_module(mod_name), attr)
    except Exception as e:
        raise LoadError(f"line {d.line}: cannot import {d.target!r}: {e}") \
            from e
    if not callable(obj):
        raise LoadError(f"line {d.line}: {d.target!r} is not callable")
    py_funcs[d.name] = obj
    py_exts.append({"kind": "dotted", "name": d.name, "target": d.target})


def _check_dupes(prog: A.Program):
    seen: dict[tuple, int] = {}
    for d in prog.decls:
        key = None
        if isinstance(d, A.PoolDecl):
            key = ("pool", d.name)
        elif isinstance(d, A.ContextDecl):
            key = ("context", d.name)
        elif isinstance(d, A.SchemaDecl):
            key = ("schema", d.name)
        elif isinstance(d, A.FnDecl):
            key = ("fn", d.name)
        if key:
            if key in seen:
                raise LoadError(
                    f"duplicate {key[0]} '{key[1]}' (lines {seen[key]} "
                    f"and {d.line})")
            seen[key] = d.line
