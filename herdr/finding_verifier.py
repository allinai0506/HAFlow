"""Evidence Verifier for Reviewer Findings (Finding Verifier Safety V1).

Performs deterministic, read-only fact-checking on candidate findings produced
by LLM Reviewers before submission. Enforces a strict Tri-State taxonomy:

1. 'verified':
   There is full, closed-form static evidence proving both the trigger condition
   and the defect consequence (e.g. an AST-verifiable empty test function with
   no assertions or calls to the tested unit).

2. 'rejected':
   The finding is directly and indisputably refuted by counter-evidence that matches
   in the SAME code revision, SAME scope (module or enclosing function), and
   REACHABLE execution conditions (excluding TYPE_CHECKING, dead branches, local imports
   in unrelated scopes, different receivers, or comments).

3. 'uncertain':
   Evidence is insufficient or discusses runtime conditions, data consequences,
   timing, error handling policies, or architectural trade-offs that static analysis
   cannot prove or refute. All uncertain findings are safely RETAINED.
"""

from __future__ import annotations

import ast
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def _read_file_at_commit(repo_dir: Path, file_path: str, head_commit: Optional[str] = None) -> Optional[str]:
    """Read file content from repo at head_commit (or working tree if head_commit is None)."""
    repo = Path(repo_dir).resolve()
    target = repo / file_path

    if head_commit:
        try:
            res = subprocess.run(
                ["git", "-C", str(repo), "show", f"{head_commit}:{file_path}"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if res.returncode == 0:
                return res.stdout
        except Exception:
            pass

    if target.exists() and target.is_file():
        try:
            return target.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return None
    return None


def _build_ast_parent_map(tree: ast.AST) -> Dict[ast.AST, ast.AST]:
    """Build a mapping from child AST node to parent AST node."""
    parents: Dict[ast.AST, ast.AST] = {}
    for p in ast.walk(tree):
        for c in ast.iter_child_nodes(p):
            parents[c] = p
    return parents


def _find_enclosing_scope(tree: ast.AST, line_no: int) -> Optional[ast.AST]:
    """Find the innermost FunctionDef, AsyncFunctionDef, or ClassDef containing line_no."""
    best: Optional[ast.AST] = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            s = getattr(node, "lineno", 0)
            e = getattr(node, "end_lineno", s)
            if s <= line_no <= e:
                if best is None or (e - s < getattr(best, "end_lineno", 0) - getattr(best, "lineno", 0)):
                    best = node
    return best


def _is_unreachable_or_guarded(node: ast.AST, parents: Dict[ast.AST, ast.AST]) -> bool:
    """Check if an AST node is inside an unreachable branch or conditional type guard.

    Guards include:
    - if TYPE_CHECKING:
    - if False: / if 0:
    - try: ... except ImportError: (guarded / fallback import)
    """
    curr = node
    while curr in parents:
        parent = parents[curr]
        if isinstance(parent, ast.If):
            test_repr = ast.unparse(parent.test) if hasattr(ast, "unparse") else ""
            if "TYPE_CHECKING" in test_repr or test_repr in ("False", "0"):
                return True
        elif isinstance(parent, ast.Try):
            for handler in parent.handlers:
                if handler.type:
                    type_repr = ast.unparse(handler.type) if hasattr(ast, "unparse") else ""
                    if "ImportError" in type_repr or "ModuleNotFoundError" in type_repr:
                        return True
        curr = parent
    return False


def _check_import_absence_refutation(
    tree: ast.AST,
    parents: Dict[ast.AST, ast.AST],
    module_name: str,
    target_scope: Optional[ast.AST],
    lines: List[str],
) -> Optional[Dict[str, Any]]:
    """Check if an import absence claim is refuted by reachable, in-scope imports."""
    # 1. Check unconditional top-level imports in tree.body
    for stmt in getattr(tree, "body", []):
        if _is_unreachable_or_guarded(stmt, parents):
            continue
        if isinstance(stmt, ast.Import):
            for alias in stmt.names:
                if alias.name == module_name or alias.name.split(".")[0] == module_name or alias.asname == module_name:
                    idx = stmt.lineno
                    return {
                        "type": "import_exists",
                        "module": module_name,
                        "line": idx,
                        "scope": "module",
                        "snippet": lines[idx - 1].strip() if 1 <= idx <= len(lines) else "",
                    }
        elif isinstance(stmt, ast.ImportFrom):
            if stmt.module == module_name or (stmt.module and stmt.module.split(".")[0] == module_name):
                idx = stmt.lineno
                return {
                    "type": "import_exists",
                    "module": module_name,
                    "line": idx,
                    "scope": "module",
                    "snippet": lines[idx - 1].strip() if 1 <= idx <= len(lines) else "",
                }
            for alias in stmt.names:
                if alias.name == module_name or alias.asname == module_name:
                    idx = stmt.lineno
                    return {
                        "type": "import_exists",
                        "module": module_name,
                        "line": idx,
                        "scope": "module",
                        "snippet": lines[idx - 1].strip() if 1 <= idx <= len(lines) else "",
                    }

    # 2. Check unconditional imports inside target_scope
    if target_scope is not None:
        for node in ast.walk(target_scope):
            if _is_unreachable_or_guarded(node, parents):
                continue
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == module_name or alias.name.split(".")[0] == module_name or alias.asname == module_name:
                        idx = node.lineno
                        return {
                            "type": "import_exists",
                            "module": module_name,
                            "line": idx,
                            "scope": getattr(target_scope, "name", "local"),
                            "snippet": lines[idx - 1].strip() if 1 <= idx <= len(lines) else "",
                        }
            elif isinstance(node, ast.ImportFrom):
                if node.module == module_name or (node.module and node.module.split(".")[0] == module_name):
                    idx = node.lineno
                    return {
                        "type": "import_exists",
                        "module": module_name,
                        "line": idx,
                        "scope": getattr(target_scope, "name", "local"),
                        "snippet": lines[idx - 1].strip() if 1 <= idx <= len(lines) else "",
                    }
                for alias in node.names:
                    if alias.name == module_name or alias.asname == module_name:
                        idx = node.lineno
                        return {
                            "type": "import_exists",
                            "module": module_name,
                            "line": idx,
                            "scope": getattr(target_scope, "name", "local"),
                            "snippet": lines[idx - 1].strip() if 1 <= idx <= len(lines) else "",
                        }

    return None


def _check_symbol_absence_refutation(
    tree: ast.AST,
    parents: Dict[ast.AST, ast.AST],
    symbol_name: str,
    target_scope: Optional[ast.AST],
    lines: List[str],
) -> Optional[Dict[str, Any]]:
    """Check if a symbol definition absence claim is refuted in valid scope."""
    # 1. Check top-level module scope (FunctionDef, AsyncFunctionDef, ClassDef, top-level assignment)
    for stmt in getattr(tree, "body", []):
        if _is_unreachable_or_guarded(stmt, parents):
            continue
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if stmt.name == symbol_name:
                idx = stmt.lineno
                return {
                    "type": "definition_exists",
                    "symbol": symbol_name,
                    "line": idx,
                    "scope": "module",
                    "snippet": lines[idx - 1].strip() if 1 <= idx <= len(lines) else "",
                }
        elif isinstance(stmt, ast.Assign):
            for t in stmt.targets:
                if isinstance(t, ast.Name) and t.id == symbol_name:
                    idx = stmt.lineno
                    return {
                        "type": "definition_exists",
                        "symbol": symbol_name,
                        "line": idx,
                        "scope": "module",
                        "snippet": lines[idx - 1].strip() if 1 <= idx <= len(lines) else "",
                    }

    # 2. Check local definition inside target_scope
    if target_scope is not None:
        for node in ast.walk(target_scope):
            if _is_unreachable_or_guarded(node, parents):
                continue
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node != target_scope and node.name == symbol_name:
                    idx = node.lineno
                    return {
                        "type": "definition_exists",
                        "symbol": symbol_name,
                        "line": idx,
                        "scope": getattr(target_scope, "name", "local"),
                        "snippet": lines[idx - 1].strip() if 1 <= idx <= len(lines) else "",
                    }
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id == symbol_name:
                        idx = node.lineno
                        return {
                            "type": "definition_exists",
                            "symbol": symbol_name,
                            "line": idx,
                            "scope": getattr(target_scope, "name", "local"),
                            "snippet": lines[idx - 1].strip() if 1 <= idx <= len(lines) else "",
                        }

    return None


def _check_assignment_absence_refutation(
    tree: ast.AST,
    parents: Dict[ast.AST, ast.AST],
    raw_target: str,
    target_scope: Optional[ast.AST],
    lines: List[str],
) -> Optional[Dict[str, Any]]:
    """Check if an assignment absence claim is refuted in the exact scope and receiver."""
    # Scope to search: target_scope if within a function/class, else top-level module body
    search_nodes = [target_scope] if target_scope is not None else getattr(tree, "body", [])

    is_attr = "." in raw_target
    receiver, attr_name = ("", "")
    if is_attr:
        parts = raw_target.split(".")
        receiver = parts[0]
        attr_name = parts[1]
    else:
        attr_name = raw_target

    for root in search_nodes:
        if root is None:
            continue
        for node in ast.walk(root):
            if _is_unreachable_or_guarded(node, parents):
                continue
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    if is_attr:
                        if isinstance(t, ast.Attribute) and t.attr == attr_name:
                            val_repr = ast.unparse(t.value) if hasattr(ast, "unparse") else ""
                            if val_repr == receiver:
                                idx = node.lineno
                                return {
                                    "type": "assignment_exists",
                                    "target": raw_target,
                                    "line": idx,
                                    "scope": getattr(target_scope, "name", "module") if target_scope else "module",
                                    "snippet": lines[idx - 1].strip() if 1 <= idx <= len(lines) else "",
                                }
                    else:
                        if isinstance(t, ast.Name) and t.id == attr_name:
                            idx = node.lineno
                            return {
                                "type": "assignment_exists",
                                "target": raw_target,
                                "line": idx,
                                "scope": getattr(target_scope, "name", "module") if target_scope else "module",
                                "snippet": lines[idx - 1].strip() if 1 <= idx <= len(lines) else "",
                            }
    return None


def _check_literal_recording_refutation(
    tree: ast.AST,
    parents: Dict[ast.AST, ast.AST],
    literal: str,
    target_scope: Optional[ast.AST],
    lines: List[str],
) -> Optional[Dict[str, Any]]:
    """Check if a literal absence claim is refuted by active code calls or assignments."""
    for node in ast.walk(tree):
        if _is_unreachable_or_guarded(node, parents):
            continue
        # Only inspect active Call, Dict, or Assign nodes (excluding docstrings and comments)
        if isinstance(node, ast.Call):
            matched = False
            for arg in node.args:
                if isinstance(arg, ast.Constant) and arg.value == literal:
                    matched = True
                    break
            if not matched:
                for kw in node.keywords:
                    if isinstance(kw.value, ast.Constant) and kw.value == literal:
                        matched = True
                        break
            if matched:
                idx = node.lineno
                func_name = ast.unparse(node.func) if hasattr(ast, "unparse") else "call"
                return {
                    "type": "record_exists",
                    "literal": literal,
                    "line": idx,
                    "call": func_name,
                    "snippet": lines[idx - 1].strip() if 1 <= idx <= len(lines) else "",
                }
        elif isinstance(node, ast.Assign):
            if isinstance(node.value, ast.Constant) and node.value.value == literal:
                idx = node.lineno
                return {
                    "type": "record_exists",
                    "literal": literal,
                    "line": idx,
                    "snippet": lines[idx - 1].strip() if 1 <= idx <= len(lines) else "",
                }
    return None


def _check_verified_defect(
    tree: ast.AST,
    target_scope: Optional[ast.AST],
    msg: str,
    lines: List[str],
    start_line: int,
    end_line: int,
) -> Optional[Dict[str, Any]]:
    """Strictly verify defects that have complete closed-form static evidence.

    If the defect relies on runtime state, multiple generation interactions,
    data drift, timeout/timing, or unproven consequences, returns None
    (demoting to 'uncertain').
    """
    if target_scope is None:
        return None

    # Test false green / missing assertion check:
    # If the target is a test function (test_*) and the finding claims it has no assertions
    # or never calls the tested function, check if the AST body literally contains 0 assertions
    if isinstance(target_scope, (ast.FunctionDef, ast.AsyncFunctionDef)) and target_scope.name.startswith("test_"):
        claims_no_call_or_assert = bool(
            re.search(r"(?:never calls|not calling|without calling|no assertions?|assert.*?missing)", msg, re.IGNORECASE)
        )
        if claims_no_call_or_assert:
            has_assertions = False
            for node in ast.walk(target_scope):
                if isinstance(node, ast.Assert):
                    has_assertions = True
                    break
                if isinstance(node, ast.Call):
                    fn_name = ast.unparse(node.func) if hasattr(ast, "unparse") else ""
                    if "assert" in fn_name.lower():
                        has_assertions = True
                        break
            if not has_assertions:
                return {
                    "type": "test_lacks_assertions",
                    "function": target_scope.name,
                    "reason": "AST body of test function contains zero assertions or assert calls.",
                }

    return None


def verify_finding(
    finding: Dict[str, Any],
    repo_dir: Path,
    head_commit: Optional[str] = None,
) -> Dict[str, Any]:
    """Verify a single finding against code facts in the repository.

    Returns an enriched dictionary with:
    - verification_status: 'verified' | 'rejected' | 'uncertain'
    - verification_reason: explanation of the decision
    - counter_evidence: details of refuting evidence if rejected
    - grounding: details of file and line range validity
    """
    file_rel = str(finding.get("file") or "").strip()
    msg = str(finding.get("message") or "")
    start_line = int(finding.get("start_line") or 1)
    end_line = int(finding.get("end_line") or start_line)

    repo_path = Path(repo_dir).resolve()
    content = _read_file_at_commit(repo_path, file_rel, head_commit)

    # 1. Grounding check: file existence
    if content is None:
        return {
            **finding,
            "verification_status": "rejected",
            "verification_reason": f"file_not_found: The referenced file '{file_rel}' does not exist in the target revision.",
            "counter_evidence": {"error": "file_not_found", "file": file_rel},
        }

    lines = content.splitlines()
    total_lines = len(lines)

    # 2. Grounding check: line boundary sanity
    if start_line > total_lines + 50:
        return {
            **finding,
            "verification_status": "rejected",
            "verification_reason": f"line_out_of_bounds: start_line {start_line} exceeds file length ({total_lines} lines).",
            "counter_evidence": {"error": "line_out_of_bounds", "file": file_rel, "total_lines": total_lines},
        }

    clamped_start = max(1, min(start_line, total_lines))
    clamped_end = max(clamped_start, min(end_line, total_lines))
    target_snippet = "\n".join(lines[clamped_start - 1 : clamped_end])

    # 3. Parse AST for Python files
    tree: Optional[ast.AST] = None
    parents: Dict[ast.AST, ast.AST] = {}
    target_scope: Optional[ast.AST] = None

    if file_rel.endswith(".py") or (lines and lines[0].startswith("#!") and "python" in lines[0]):
        try:
            tree = ast.parse(content, filename=file_rel)
            parents = _build_ast_parent_map(tree)
            target_scope = _find_enclosing_scope(tree, start_line)
        except Exception:
            # If target file has syntax errors, cannot safely refute absence
            tree = None

    # If AST is not available, we cannot safely perform scope-aware counter-evidence refutations
    if tree is not None:
        # -------------------------------------------------------------
        # Refutation Check A: Module/Package Import Absence Claim
        # -------------------------------------------------------------
        import_match = re.search(
            r"(?:module|package)?\s*[`']?([A-Za-z0-9_]+)['`]?\s*(?:is|are)?\s*(?:not|neither)\s*(?:ensured to be\s*)?(?:imported|in scope|in the global namespace)",
            msg,
            re.IGNORECASE,
        ) or re.search(
            r"without ensuring\s*[`']?([A-Za-z0-9_]+)['`]?\s*is imported",
            msg,
            re.IGNORECASE,
        )
        if import_match:
            mod_name = import_match.group(1).strip()
            ce = _check_import_absence_refutation(tree, parents, mod_name, target_scope, lines)
            if ce:
                return {
                    **finding,
                    "verification_status": "rejected",
                    "verification_reason": f"counter_evidence_found: Claimed module '{mod_name}' is not imported, but reachable import was found in scope '{ce.get('scope')}' at line {ce.get('line')}: {ce.get('snippet')}",
                    "counter_evidence": ce,
                }

        # -------------------------------------------------------------
        # Refutation Check B: Function/Class/Symbol Not Defined Claim
        # -------------------------------------------------------------
        def_match = re.search(
            r"[`']?([A-Za-z0-9_]+)['`]?\s*is\s*(?:neither|not)\s*(?:defined|declared|implemented)",
            msg,
            re.IGNORECASE,
        ) or re.search(
            r"[`']?([A-Za-z0-9_]+)['`]?\s*(?:is undefined|does not exist)",
            msg,
            re.IGNORECASE,
        )
        if def_match:
            sym_name = def_match.group(1).strip()
            if len(sym_name) > 2 and sym_name not in ("None", "True", "False", "this", "that"):
                ce = _check_symbol_absence_refutation(tree, parents, sym_name, target_scope, lines)
                if ce:
                    return {
                        **finding,
                        "verification_status": "rejected",
                        "verification_reason": f"counter_evidence_found: Claimed symbol '{sym_name}' is not defined, but reachable definition was found in scope '{ce.get('scope')}' at line {ce.get('line')}: {ce.get('snippet')}",
                        "counter_evidence": ce,
                    }

        # -------------------------------------------------------------
        # Refutation Check C: Attribute/Variable Never Set/Assigned Claim
        # -------------------------------------------------------------
        attr_match = re.search(
            r"(?:variable|attribute|field|property)?\s*[`']?([A-Za-z0-9_\.]+)['`]?\s*(?:is|was)?\s*(?:never|not)\s*(?:set|assigned|initialized|populated)",
            msg,
            re.IGNORECASE,
        ) or re.search(
            r"never setting\s*[`']?([A-Za-z0-9_\.]+)['`]?",
            msg,
            re.IGNORECASE,
        )
        if attr_match:
            raw_target = attr_match.group(1).strip()
            short = raw_target.split(".")[-1]
            if len(short) > 2 and short not in ("None", "True", "False", "int", "str"):
                ce = _check_assignment_absence_refutation(tree, parents, raw_target, target_scope, lines)
                if ce:
                    return {
                        **finding,
                        "verification_status": "rejected",
                        "verification_reason": f"counter_evidence_found: Claimed '{raw_target}' is never set, but reachable assignment was found in scope '{ce.get('scope')}' at line {ce.get('line')}: {ce.get('snippet')}",
                        "counter_evidence": ce,
                    }

        # -------------------------------------------------------------
        # Refutation Check D: String/Key "Never Written/Recorded Anywhere"
        # -------------------------------------------------------------
        lit_match = re.search(
            r"(?:nowhere|never)\s*(?:in|recorded|written|used)\s*.*[`']([A-Za-z0-9_\-:]+)['`]",
            msg,
            re.IGNORECASE,
        ) or re.search(
            r"[`']([A-Za-z0-9_\-:]+)['`]\s*is never\s*(?:recorded|written|persisted)",
            msg,
            re.IGNORECASE,
        )
        if lit_match:
            literal = lit_match.group(1).strip()
            if len(literal) >= 4 and not literal.startswith("http"):
                ce = _check_literal_recording_refutation(tree, parents, literal, target_scope, lines)
                if ce:
                    return {
                        **finding,
                        "verification_status": "rejected",
                        "verification_reason": f"counter_evidence_found: Claimed '{literal}' is never recorded, but active recording was found at line {ce.get('line')}: {ce.get('snippet')}",
                        "counter_evidence": ce,
                    }

        # -------------------------------------------------------------
        # Refutation Check E: Syntax Error / Incomplete Syntax Claim
        # -------------------------------------------------------------
        syntax_err_match = re.search(
            r"(?:SyntaxError|invalid syntax|unexpected EOF|Incomplete function definition|unclosed function call|missing closing statement)",
            msg,
            re.IGNORECASE,
        )
        if syntax_err_match:
            # tree is not None means ast.parse(content) parsed the whole file with zero syntax errors!
            return {
                **finding,
                "verification_status": "rejected",
                "verification_reason": (
                    f"counter_evidence_found: Finding claims syntax defect ({syntax_err_match.group(0)}), "
                    f"but target file '{file_rel}' parses successfully with zero AST SyntaxErrors in the target revision."
                ),
                "counter_evidence": {
                    "type": "valid_ast_syntax",
                    "file": file_rel,
                    "ast_parsed": True,
                },
            }

        # -------------------------------------------------------------
        # Strictly Closed-Form Verified Check
        # -------------------------------------------------------------
        verified_proof = _check_verified_defect(tree, target_scope, msg, lines, start_line, end_line)
        if verified_proof:
            return {
                **finding,
                "verification_status": "verified",
                "verification_reason": f"grounding_verified: Closed-form static proof confirmed: {verified_proof.get('reason')}",
                "grounding": {
                    "file_exists": True,
                    "lines_valid": True,
                    "proof": verified_proof,
                },
            }

    # -------------------------------------------------------------
    # 4. Default: Uncertain
    # Cannot be proven or refuted with full static confidence.
    # Preserved for human reviewer or runtime assessment.
    # -------------------------------------------------------------
    return {
        **finding,
        "verification_status": "uncertain",
        "verification_reason": (
            "unverifiable_statically: Runtime trigger conditions, dynamic data consequences, "
            "or architectural trade-offs cannot be fully proven or refuted statically."
        ),
        "grounding": {
            "file_exists": True,
            "lines_valid": True,
            "snippet": target_snippet[:150],
        },
    }


def verify_findings(
    findings: List[Dict[str, Any]],
    repo_dir: Path,
    head_commit: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    """Verify a list of candidate findings.

    Returns:
    - retained_findings: findings with status 'verified' or 'uncertain'
    - rejected_findings: findings with status 'rejected'
    - summary: verification counts and elapsed milliseconds
    """
    t0 = time.time()
    retained: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    counts = {"total": len(findings), "verified": 0, "rejected": 0, "uncertain": 0}

    for f in findings:
        checked = verify_finding(f, repo_dir=repo_dir, head_commit=head_commit)
        st = checked.get("verification_status")
        if st == "rejected":
            rejected.append(checked)
            counts["rejected"] += 1
        elif st == "verified":
            retained.append(checked)
            counts["verified"] += 1
        else:
            retained.append(checked)
            counts["uncertain"] += 1

    summary = {
        "counts": counts,
        "elapsed_ms": int((time.time() - t0) * 1000),
    }
    return retained, rejected, summary
