#!/usr/bin/env python3
"""Structural checks the compiler cannot do for us.

The bug this exists for: the definePlugin callback is a plain factory, not a
React component. A hook called from it throws at runtime and takes the whole
panel down, with nothing in the log and nothing in the type checker to say why.
That happened once already.

Run standalone, and via tools/smoke_test.py before packaging.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path
from typing import List

SRC = Path(__file__).resolve().parent.parent / "src"

#: React's own hooks. Fixed set, so written out.
REACT_HOOKS = (
    "useState",
    "useEffect",
    "useLayoutEffect",
    "useCallback",
    "useMemo",
    "useRef",
    "useContext",
    "useReducer",
)

#: Anything else starting with `use` that src/ exports is ours by convention.
#:
#: This was a hand-written list, and it went stale the moment a new hook was
#: added: useChoice, useClampSlider, useCoalesced and useSettledCommit were all
#: missing from it, so the one check that catches a hook called from
#: definePlugin's factory -- a mistake that blanks the entire panel with nothing
#: in the log -- silently did not cover any of them. Derived now, so a new hook
#: is covered the moment it is exported.
_DECLARED_HOOK = re.compile(r"export\s+function\s+(use[A-Z]\w*)")

def _own_hooks() -> tuple:
    declared = set()
    for path in SRC.glob("*.ts*"):
        declared.update(_DECLARED_HOOK.findall(path.read_text(encoding="utf-8")))
    return tuple(sorted(declared))

HOOKS = tuple(REACT_HOOKS) + _own_hooks()

#: Files that are not React components and therefore must not call hooks.
NON_COMPONENT = {"index.tsx"}

#: The backend event subscription belongs in index.tsx, torn down by
#: onDismount -- the pattern the official plugin template uses. Registering it
#: inside a component works but ties its lifetime to the panel's mount/unmount
#: cycle instead of the plugin's, and the panel is recreated every time the user
#: opens it while the plugin stays loaded.
EVENT_SUBSCRIPTION_FILES = {"index"}  # Path.stem, no extension

#: Matches a *call* to the Decky addEventListener. Not preceded by a dot, since
#: that would be window.addEventListener (a DOM subscription, a different
#: thing). Import lines are removed before this runs.
_DECKY_LISTENER_CALL = re.compile(r"(?<![\w.])addEventListener\s*[(<]")

failures: List[str] = []


#: Module specifiers we must keep verbatim: "./Content", "@decky/ui", ...
_IMPORT_PATH = re.compile(r'((?:from|import)\s*\(?\s*)(["\'])((?:\./)?[A-Za-z0-9_@./-]*)(\2)')


def strip_imports(src: str) -> str:
    """Blank out import statements.

    ``import { addEventListener } from "@decky/api"`` must not read as a call.
    """
    return re.sub(r"^\s*import\b[^;]*;?", lambda m: "\n" * m.group(0).count("\n"),
                  src, flags=re.M)


def strip_comments_and_strings(src: str) -> str:
    """Blank comments and string contents, but keep module specifiers.

    Import paths are strings, so blanking every string would erase the very
    thing the reachability check needs. Those are restored afterwards.
    """
    kept: dict[int, str] = {}

    def _stash(m: re.Match) -> str:
        kept[len(kept)] = m.group(0)
        return f"\x00{len(kept) - 1}\x00"

    src = _IMPORT_PATH.sub(_stash, src)

    out = []
    i = 0
    n = len(src)
    while i < n:
        c = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if c == "/" and nxt == "/":
            while i < n and src[i] != "\n":
                out.append(" ")
                i += 1
            continue
        if c == "/" and nxt == "*":
            i += 2
            while i + 1 < n and not (src[i] == "*" and src[i + 1] == "/"):
                out.append(" ")
                i += 1
            i += 2
            out.append("  ")
            continue
        if c in "\"'`":
            quote = c
            out.append(c)
            i += 1
            while i < n:
                if src[i] == "\\":
                    out.append(" ")
                    i += 2
                    continue
                if src[i] == quote:
                    break
                out.append(" ")
                i += 1
            out.append(quote)
            i += 1
            continue
        out.append(c)
        i += 1

    text = "".join(out)
    for idx, original in kept.items():
        text = text.replace(f"\x00{idx}\x00", original)
    return text


def check_event_subscription_location() -> None:
    """addEventListener belongs in index.tsx, paired with onDismount.

    Registering it in a component ties the subscription to the panel's mount
    cycle. The panel is created and destroyed as the user opens and closes it,
    while the plugin itself stays loaded, so that is the wrong lifetime.
    """
    for path in sorted(SRC.glob("*.ts")) + sorted(SRC.glob("*.tsx")):
        if path.stem in EVENT_SUBSCRIPTION_FILES:
            continue
        src = strip_imports(path.read_text(encoding="utf-8"))
        src = strip_comments_and_strings(src)
        # window.addEventListener is a DOM subscription (the global error
        # handlers) and has nothing to do with the Decky event bus; only the
        # imported @decky/api one counts.
        for m in _DECKY_LISTENER_CALL.finditer(src):
            failures.append(
                f"src/{path.name} calls the Decky addEventListener(); it "
                f"belongs in index.tsx with an onDismount() that removes it"
            )
            break

    entry = strip_imports((SRC / "index.tsx").read_text(encoding="utf-8"))
    entry = strip_comments_and_strings(entry)
    if not _DECKY_LISTENER_CALL.search(entry):
        failures.append("index.tsx does not subscribe to the backend event")
    if "onDismount" not in (SRC / "index.tsx").read_text(encoding="utf-8"):
        failures.append(
            "index.tsx has no onDismount(); the event listener would leak"
        )


def check_no_hooks_outside_components() -> None:
    """No hook may be called in a non-component module scope.

    index.tsx holds the definePlugin callback, which is a factory. Calling a
    hook there throws "Invalid hook call" and the panel renders nothing.
    """
    for name in sorted(NON_COMPONENT):
        path = SRC / name
        if not path.exists():
            failures.append(f"{name} is missing")
            continue
        src = strip_comments_and_strings(path.read_text(encoding="utf-8"))
        for hook in HOOKS:
            if re.search(rf"\b{hook}\s*\(", src):
                failures.append(
                    f"{name} calls {hook}(), but it is not a component "
                    f"(definePlugin takes a factory, not a render function)"
                )


#: Functions the Decky loader calls by attribute name, so nothing in the tree
#: mentions them.
LIFECYCLE = {"_main", "_unload", "_uninstall"}


def _rpc_names() -> set:
    """RPC method names, called from the frontend by *string*.

    They are invisible to a reference count, which is exactly why the check
    below needs to know about them: everything else that nothing refers to is
    dead, and three such methods sat here unnoticed (RayNeoDevice.ping,
    RayNeoDevice.supported_features, Plugin._require_device) because a grep for
    a def is not a grep for a use.
    """
    names = set()
    for path in sorted(SRC.glob("*.ts*")):
        src = path.read_text(encoding="utf-8")
        names.update(re.findall(r'callable<[^>]*>\(\s*"([a-z_]+)"', src))
    return names


def check_python_dead_code() -> None:
    """Every Python def must be referenced somewhere, or be reachable by name.

    Two allowlists, both narrow: Decky's lifecycle hooks, and the RPC methods
    the frontend invokes by string. Everything else has to earn its place.
    """
    root = SRC.parent
    files = [root / "main.py"] + sorted((root / "py_modules").glob("*.py"))
    corpus = "\n".join(
        q.read_text(encoding="utf-8")
        for q in files + sorted((root / "tools").glob("*.py"))
    )
    allowed = LIFECYCLE | _rpc_names()
    for path in files:
        src = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(src)
        except SyntaxError as exc:
            failures.append(f"{path.name} does not parse: {exc}")
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            name = node.name
            if name.startswith("__") or name in allowed:
                continue
            if len(re.findall(rf"\b{re.escape(name)}\b", corpus)) <= 1:
                failures.append(
                    f"{path.name}:{node.lineno} defines {name}(), which nothing "
                    f"refers to"
                )


def check_component_imports() -> None:
    """Every module under src/ must be reachable from index.tsx.

    An orphaned file is dead weight that will rot without anyone noticing.
    """

    def imports_of(path: Path) -> List[str]:
        src = strip_comments_and_strings(path.read_text(encoding="utf-8"))
        return re.findall(r'from\s+"\./([A-Za-z0-9_]+)"', src)

    seen = {"index"}
    queue = list(imports_of(SRC / "index.tsx"))
    while queue:
        mod = queue.pop()
        if mod in seen:
            continue
        seen.add(mod)
        for ext in (".ts", ".tsx"):
            candidate = SRC / f"{mod}{ext}"
            if candidate.exists():
                queue.extend(imports_of(candidate))
                break
        else:
            failures.append(f"index.tsx imports .{mod}, which does not exist")

    for path in sorted(SRC.glob("*.ts")) + sorted(SRC.glob("*.tsx")):
        mod = path.stem
        if mod not in seen and mod != "index":
            failures.append(f"src/{path.name} is never imported -- dead file")


def check_no_import_cycles() -> None:
    """Module cycles break bundlers and hide runtime ordering bugs."""
    graph: dict[str, List[str]] = {}
    for path in sorted(SRC.glob("*.ts")) + sorted(SRC.glob("*.tsx")):
        src = strip_comments_and_strings(path.read_text(encoding="utf-8"))
        graph[path.stem] = re.findall(r'from\s+"\./([A-Za-z0-9_]+)"', src)

    visiting: set[str] = set()
    done: set[str] = set()

    def walk(node: str, trail: List[str]) -> None:
        if node in visiting:
            cycle = " -> ".join(trail + [node])
            failures.append(f"import cycle: {cycle}")
            return
        if node in done:
            return
        visiting.add(node)
        for nxt in graph.get(node, []):
            if nxt in graph:
                walk(nxt, trail + [node])
        visiting.discard(node)
        done.add(node)

    for mod in graph:
        walk(mod, [])


def check_file_sizes() -> None:
    """A component file that keeps growing is a component file being split."""
    for path in sorted(SRC.glob("*.ts")) + sorted(SRC.glob("*.tsx")):
        lines = len(path.read_text(encoding="utf-8").splitlines())
        if lines > 400:
            failures.append(
                f"src/{path.name} is {lines} lines; state belongs in a hook, "
                f"not in a component file"
            )


def check_no_dead_exports() -> None:
    """An exported name nothing imports is dead weight.

    Cheap to check, and dead code is worse than no code: it implies a contract
    that does not exist. `src/useDevice.ts` carries a documentation list of
    hook names that structcheck.py itself owns, so it is exempt -- but the
    exemption is stated rather than implicit.
    """
    modules = {
        p.stem: p for p in
        list(SRC.glob("*.ts")) + list(SRC.glob("*.tsx"))
    }
    for stem, path in sorted(modules.items()):
        src = path.read_text(encoding="utf-8")
        others = "\n".join(
            q.read_text(encoding="utf-8")
            for s, q in modules.items() if s != stem
        )
        tools_text = "".join(
            q.read_text(encoding="utf-8")
            for q in sorted((SRC.parent / "tools").glob("*.py"))
        )
        for m in re.finditer(
            r"^export (?:const|function|class|type|interface) (\w+)", src, re.M
        ):
            name = m.group(1)
            if re.search(rf"\b{name}\b", others):
                continue
            if re.search(rf"\b{name}\b", tools_text):
                continue
            # Only flag if the name is not used again inside its own module.
            # The whole module, not just the part after the declaration:
            # useDebounced is defined at the bottom of hooks.ts and used by
            # useSettledCommit above it, so searching only forwards missed the
            # use and called a live helper dead.
            if len(re.findall(rf"\b{name}\b", src)) > 1:
                continue
                failures.append(
                    f"src/{path.name} exports {name}, which nothing imports"
                )


def main() -> int:
    for check in (
        check_no_hooks_outside_components,
        check_event_subscription_location,
        check_component_imports,
        check_no_dead_exports,
        check_no_import_cycles,
        check_file_sizes,
        check_python_dead_code,
    ):
        check()

    if failures:
        print("structure check failed:")
        for f in sorted(set(failures)):
            print(f"  FAIL {f}")
        return 1
    print("  OK  structure: no hooks outside components, no cycles, "
          "no dead files/exports, no oversized components")
    return 0


if __name__ == "__main__":
    sys.exit(main())