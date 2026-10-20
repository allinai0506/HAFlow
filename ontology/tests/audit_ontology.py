#!/usr/bin/env python3
"""Mechanical audit of the HAFlow candidate ontology against the repository (read-only).

Checks
  E  evidence locators: 'sym Ln' must be defined near n, or n must fall inside the definition
     body, or sym must be mentioned within 20 lines. Markdown sources: mention within 5 lines.
     Bare 'Ln' references must exist in the file.
  R  rules logic_ref 'path:symbol' must resolve to a definition or a mention.
  B  bindings: herdr-task / herdr-factory subcommands (argparse add_parser), dotted python
     functions under herdr/, controller functions in services/herdr-controller.py.
  A  authorization: action.authorization.policies <-> policy.allowed_actions (both ways);
     every action has a policy; read-only projections carry no actions.
  T  state machines: YAML edges equal herdr/transitions.py matrices; declared terminal states
     must be absorbing (no outgoing edges).
  C  CLI coverage: write-like subcommands without any action binding (informational).
Exit status 1 when any MAJOR finding exists.
Usage: audit_ontology.py <repo-root>
"""
from __future__ import annotations

import ast
import re
import sys
from collections import Counter
from pathlib import Path

import yaml

REPO = Path(sys.argv[1]).resolve()
ONT = REPO / "ontology"
TASK_CLI = REPO / "bin/herdr-task"
FACTORY_CLI = REPO / "bin/herdr-factory"
CONTROLLER = REPO / "services/herdr-controller.py"

READ_PREFIXES = (
    "list", "get", "status", "show", "doctor", "templates", "projects", "panes", "slots",
    "agents", "adapters", "note-list", "checkpoint-list", "recovery-status", "stage-status",
    "node-status", "ops-center", "node-config", "delivery-report", "artifacts", "delivery-check",
    "check-", "verify-metrics", "diff", "launch-reconcile", "partial-launch-preview",
    "sync-experience", "e2e", "route-shadow", "shadow-eval", "canary-eval", "eval", "metrics",
    "observe", "compact", "working-context", "replay", "tool-run", "steer-queue",
)

findings: list[tuple[str, str, str, str]] = []
stats: Counter = Counter()


def add(sev: str, check: str, item: str, detail: str) -> None:
    findings.append((sev, check, item, detail))


def load(p: Path):
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def items(cat: str):
    for p in sorted((ONT / cat).glob("*.yaml")):
        yield p, load(p)


def lines_of(p: Path) -> list[str]:
    return p.read_text(encoding="utf-8", errors="ignore").splitlines()


def def_positions(lines: list[str], sym: str) -> list[int]:
    pats = (
        re.compile(rf"^\s*(def|class)\s+{re.escape(sym)}\b"),
        re.compile(rf"^\s*{re.escape(sym)}\s*[:=]"),
    )
    return [i + 1 for i, line in enumerate(lines) if any(p.search(line) for p in pats)]


def body_end(lines: list[str], start: int) -> int:
    indent = len(lines[start - 1]) - len(lines[start - 1].lstrip())
    for j in range(start, len(lines)):
        stripped = lines[j].lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        if len(lines[j]) - len(stripped) <= indent:
            return j + 1
    return len(lines) + 1


def mentions(lines: list[str], sym: str) -> list[int]:
    pat = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(sym)}(?![A-Za-z0-9_])")
    return [i + 1 for i, line in enumerate(lines) if pat.search(line)]


def code_like(sym: str) -> bool:
    return "_" in sym or bool(re.search(r"[a-z][A-Z]", sym)) or (len(sym) >= 8 and sym[0].isupper())


def existing_files(source: str) -> list[Path]:
    out: list[Path] = []
    for tok in re.split(r"[;,\s]+", source):
        tok = tok.strip().strip("()")
        if "/" not in tok and "." not in tok:
            continue
        p = REPO / tok
        if p.is_file():
            out.append(p)
    return out


LOC_RE = re.compile(r"(?:(?P<sym>[A-Za-z_][A-Za-z0-9_.]*)\s+)?L(?P<a>\d+)(?:-(?P<b>\d+))?")


def audit_evidence(origin: str, ev: dict) -> None:
    stats["evidence_items"] += 1
    source, locator = ev.get("source", ""), ev.get("locator", "")
    files = existing_files(source)
    if not files:
        add("MAJOR", "E-source", origin, f"source not found in repo: {source!r}")
        return
    for part in re.split(r"\s+/\s+|;\s*", locator):
        for m in LOC_RE.finditer(part):
            sym, a = m.group("sym"), int(m.group("a"))
            if sym is None or not code_like(sym):
                if any(a <= len(lines_of(f)) for f in files):
                    stats["line_refs_in_range"] += 1
                else:
                    add("MINOR", "E-range", origin, f"L{a} beyond end of {source}")
                continue
            ok = False
            elsewhere: list[tuple[str, int]] = []
            for f in files:
                lines = lines_of(f)
                if f.suffix == ".md":
                    if any(abs(x - a) <= 5 for x in mentions(lines, sym)):
                        ok = True
                    continue
                for d in def_positions(lines, sym):
                    elsewhere.append((f.name, d))
                    if abs(d - a) <= 20 or d <= a < body_end(lines, d):
                        ok = True
                if not ok and any(abs(x - a) <= 20 for x in mentions(lines, sym)):
                    ok = True
                if ok:
                    break
            if ok:
                stats["symbol_refs_ok"] += 1
            elif elsewhere:
                add("MINOR", "E-drift", origin, f"{sym} cited L{a}; defined at {elsewhere[0][0]}:{elsewhere[0][1]}")
            else:
                add("MAJOR", "E-symbol", origin, f"{sym} cited L{a}: not defined or mentioned near that line in {source}")


def audit_logic_ref(origin: str, ref: str) -> None:
    stats["logic_ref"] += 1
    if ":" not in ref:
        add("MINOR", "R-format", origin, f"logic_ref is not path:symbol: {ref!r}")
        return
    path_s, sym = ref.split(":", 1)
    p = REPO / path_s
    if not p.is_file():
        add("MAJOR", "R-file", origin, f"logic_ref file missing: {path_s}")
        return
    lines = lines_of(p)
    if def_positions(lines, sym) or (code_like(sym) and mentions(lines, sym)):
        stats["logic_ref_ok"] += 1
    else:
        add("MAJOR", "R-symbol", origin, f"logic_ref symbol {sym!r} not found in {path_s}")


def subcommands(p: Path) -> set[str]:
    """Collect subcommands from the entrypoint and (after the #269 split) its parser modules."""
    texts = [p.read_text(encoding="utf-8", errors="ignore")]
    if p.name == "herdr-task":
        texts += [q.read_text(encoding="utf-8", errors="ignore")
                  for q in sorted((REPO / "herdr").glob("task_cli_parser*.py"))]
    return set().union(*[set(re.findall(r"add_parser\(\s*['\"]([A-Za-z0-9_\-]+)['\"]", t)) for t in texts])


def python_op_resolves(dotted: str) -> bool:
    parts = dotted.split(".")
    if len(parts) < 3 or parts[0] != "herdr":
        return False
    func = parts[-1]
    base = REPO.joinpath(*parts[:-1])
    for cand in (base.with_suffix(".py"), base / "__init__.py"):
        if cand.is_file() and def_positions(lines_of(cand), func):
            return True
    return False


def controller_definitions(entry: Path) -> set[str]:
    """Resolve only operations actually registered by the service assembly."""
    tree = ast.parse(entry.read_text(encoding="utf-8"))
    definitions = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
    for node in tree.body:
        if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id == "install_modules"):
            continue
        for name in ast.literal_eval(node.value.args[1]):
            if not re.fullmatch(r"[a-z_]+", name):
                continue
            path = entry.parent / "controller" / f"{name}.py"
            if not path.is_file():
                continue
            module = ast.parse(path.read_text(encoding="utf-8"))
            implemented = {part.name for part in module.body if isinstance(part, ast.FunctionDef)}
            for part in module.body:
                if (isinstance(part, ast.Assign)
                        and any(isinstance(target, ast.Name) and target.id == "DEPENDENCIES"
                                for target in part.targets)):
                    definitions.update(implemented.intersection(ast.literal_eval(part.value)))
    return definitions


def main() -> int:
    sys.path.insert(0, str(REPO))
    from herdr.transitions import TASK_TRANSITIONS, WORKFLOW_TRANSITIONS

    connectors = {c["id"]: c for _, c in items("connectors")}
    task_subs = subcommands(TASK_CLI)
    factory_subs = subcommands(FACTORY_CLI)
    controller_defs = controller_definitions(CONTROLLER)

    # E, R
    for cat in ("objects", "relations", "states", "rules", "actions", "policies",
                "mappings", "workflows", "projections", "connectors"):
        for p, d in items(cat):
            origin = f"{cat}/{p.name}"
            for ev in d.get("evidence") or []:
                audit_evidence(origin, ev)
            for w in d.get("out_of_matrix_writers") or []:
                stats["out_of_matrix_writers"] += 1
                for ev in w.get("evidence") or []:
                    audit_evidence(origin + "#out_of_matrix_writers", ev)
            if cat == "rules" and d.get("logic_ref"):
                audit_logic_ref(origin, d["logic_ref"])

    # B, A-presence
    action_map: dict[str, dict] = {}
    mapped: dict[str, set[str]] = {cid: set() for cid in connectors}
    for p, a in items("actions"):
        action_map[a["id"]] = a
        origin = f"actions/{p.name}"
        if not (a.get("authorization") or {}).get("policies"):
            add("MAJOR", "A-no-policy", origin, "action has no authorization policy")
        for b in a.get("bindings") or []:
            stats["bindings"] += 1
            cid, op = b.get("connector"), str(b.get("operation", ""))
            base = op.split()[0] if op else ""
            if cid not in connectors:
                add("MAJOR", "B-connector", origin, f"unknown connector {cid!r}")
                continue
            mapped[cid].add(base)
            if cid == "herdr_task_cli":
                ok, detail = base in task_subs, f"herdr-task has no subcommand {base!r}"
            elif cid == "herdr_factory_cli":
                ok, detail = base in factory_subs, f"herdr-factory has no subcommand {base!r}"
            elif cid == "herdr_core_lib":
                ok, detail = python_op_resolves(op), f"python binding does not resolve to a def: {op!r}"
            elif cid == "herdr_controller_service":
                ok, detail = op in controller_defs, f"controller has no def {op!r}"
            else:
                ok, detail = True, ""
            if ok:
                stats["bindings_ok"] += 1
            else:
                add("MAJOR", "B-binding", origin, detail)

    for cid, c in connectors.items():
        for op in c.get("operations") or []:
            name = op.get("id") if isinstance(op, dict) else str(op)
            base = name.split()[0] if name else ""
            if cid == "herdr_task_cli":
                ok = base in task_subs
            elif cid == "herdr_factory_cli":
                ok = base in factory_subs
            elif cid == "herdr_core_lib":
                ok = python_op_resolves(name or "")
            elif cid == "herdr_controller_service":
                ok = name in controller_defs
            else:
                ok = True
            stats["connector_ops"] += 1
            if not ok:
                add("MAJOR", "B-connector-op", f"connectors/{cid}.yaml", f"listed operation {name!r} does not resolve")

    # A: authorization both ways
    policy_map = {pp["id"]: pp for _, pp in items("policies")}
    for aid, a in action_map.items():
        for pid in (a.get("authorization") or {}).get("policies") or []:
            pol = policy_map.get(pid)
            if pol is None:
                add("MAJOR", "A-missing-policy", f"actions/{aid}.yaml", f"policy {pid!r} undefined")
            elif aid not in (pol.get("allowed_actions") or []):
                add("MAJOR", "A-not-listed", f"actions/{aid}.yaml", f"cites {pid}, which does not list {aid}")
    for pid, pol in policy_map.items():
        for aid in pol.get("allowed_actions") or []:
            a = action_map.get(aid)
            if a is None:
                add("MAJOR", "A-unknown-action", f"policies/{pid}.yaml", f"grants undefined action {aid!r}")
            elif pid not in ((a.get("authorization") or {}).get("policies") or []):
                add("MAJOR", "A-grant-not-cited", f"policies/{pid}.yaml", f"grants {aid}, which does not cite {pid}")
        mode = (pol.get("agent_execution") or {}).get("mode")
        if mode not in ("inherit_user", "service_identity"):
            add("MINOR", "A-vocab", f"policies/{pid}.yaml",
                f"agent_execution.mode {mode!r} is outside references/05 (inherit_user | service_identity)")

    for p, pr in items("projections"):
        if "只读" in (pr.get("description") or "") and pr.get("actions"):
            add("MAJOR", "A-readonly-projection", f"projections/{p.name}",
                f"read-only projection lists actions {pr['actions']}")

    # T, S: state machines against code
    for p, s in items("states"):
        origin = f"states/{p.name}"
        matrix = {"task_lifecycle": TASK_TRANSITIONS, "workflow_lifecycle": WORKFLOW_TRANSITIONS}.get(s["id"])
        edges = {(tr["from"], tr["to"]) for tr in s.get("transitions") or []}
        if matrix is not None:
            code_edges = {(f, t) for f, tos in matrix.items() for t in tos}
            if edges == code_edges:
                stats["state_edges_equal_code"] += len(edges)
            else:
                add("MAJOR", "T-edges", origin,
                    f"missing={sorted(code_edges - edges)} extra={sorted(edges - code_edges)}")
            if set(s.get("states") or []) != set(matrix):
                add("MAJOR", "T-states", origin, "declared states differ from code matrix keys")
        outgoing = {tr["from"] for tr in s.get("transitions") or []}
        absorbing = {st for st in s.get("states") or [] if st not in outgoing}
        for t in s.get("terminal") or []:
            if t not in absorbing:
                add("MINOR", "T-terminal", origin, f"declared terminal {t!r} has outgoing edges (not absorbing)")
        for w in s.get("out_of_matrix_writers") or []:
            stats["out_of_matrix_writers"] += 0  # counted above

    # C: CLI coverage
    for label, subs, key in (("herdr-task", task_subs, "herdr_task_cli"),
                             ("herdr-factory", factory_subs, "herdr_factory_cli")):
        unmapped = sorted(s for s in subs if s not in mapped[key] and not s.startswith(READ_PREFIXES))
        stats[f"{label}_write_like_unmapped"] = len(unmapped)
        add("INFO", "C-coverage", label,
            f"{len(unmapped)} write-like subcommands without action binding: {', '.join(unmapped)}")

    print(f"Audit root: {REPO}")
    print("Stats: " + ", ".join(f"{k}={v}" for k, v in sorted(stats.items())))
    order = {"BLOCKER": 0, "MAJOR": 1, "MINOR": 2, "INFO": 3}
    for sev, check, item, detail in sorted(findings, key=lambda x: (order.get(x[0], 9), x[1], x[2])):
        print(f"[{sev}] {check} {item}: {detail}")
    counts = Counter(f[0] for f in findings)
    print("Totals: " + ", ".join(f"{k}={counts.get(k, 0)}" for k in ("MAJOR", "MINOR", "INFO")))
    return 1 if counts.get("MAJOR") else 0


if __name__ == "__main__":
    raise SystemExit(main())
