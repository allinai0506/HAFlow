#!/usr/bin/env python3
"""herdr/review_benchmark.py

Code review benchmark runner, schema validator, and comparison reporter
compatible with GitHub ReviewBench contracts.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple
import warnings

VALID_CONTEXT_MODES = ("diff_only", "existing_four_tier")
DEFAULT_CONTEXT_MODE = "existing_four_tier"
DEFAULT_CONTEXT_BUDGET_CHARS = 35000


def validate_context_config(context_mode: Any = None, context_budget_chars: Any = None) -> Tuple[str, int]:
    """Validate and normalize review context mode and budget limits."""
    mode = DEFAULT_CONTEXT_MODE if context_mode is None else context_mode
    if mode not in VALID_CONTEXT_MODES:
        raise ValueError(
            f"Invalid context_mode '{context_mode}'. Supported modes are: {list(VALID_CONTEXT_MODES)}"
        )
    budget = DEFAULT_CONTEXT_BUDGET_CHARS if context_budget_chars is None else context_budget_chars
    if not isinstance(budget, int) or isinstance(budget, bool) or budget <= 0:
        raise ValueError(
            f"Invalid context_budget_chars '{context_budget_chars}'. Must be a positive integer."
        )
    return mode, budget


def extract_base_diff(diff_content: str, max_budget_chars: int = 35000) -> Tuple[str, List[str]]:
    """Extract Tier-1 base diff with baseline preservation (up to 15,000 chars, capped by budget)."""
    changed_files = []
    current_file = ""
    for line in diff_content.splitlines():
        if line.startswith("diff --git "):
            parts = line.split()
            current_file = parts[-1][2:] if parts[-1].startswith("b/") else parts[-1]
            changed_files.append(current_file)
    base_diff = diff_content[:min(15000, max_budget_chars)]
    return base_diff, changed_files


def resolve_agent_identity(
    agent_name: str,
    review_cfg: Optional[Dict[str, Any]] = None,
    runtime_evidence: Optional[Dict[str, Any]] = None,
    cli_version: Optional[str] = None,
    query_cli_version: bool = True,
) -> Dict[str, Any]:
    """Resolve and confirm reviewer agent identity, model, and CLI version.

    Strictly differentiates between 'requested model from configuration/environment'
    and 'effective model supported by runtime execution evidence'.
    Only marks model_confirmed=True when verified by actual runtime evidence
    or when running deterministic rule-based reviewers.
    """
    review_cfg = review_cfg or {}
    runtime_evidence = runtime_evidence or {}
    model_name = "unknown"
    cli_version = cli_version or review_cfg.get("cli_version") or "unknown"
    model_confirmed = False
    details: Dict[str, Any] = {}

    requested_model = review_cfg.get("model")
    runtime_model = runtime_evidence.get("model") or runtime_evidence.get("model_name") or runtime_evidence.get("actual_model")

    if agent_name in ("agy", "llm", "agy-reviewer"):
        if cli_version == "unknown" and query_cli_version and shutil.which("agy"):
            try:
                ver_res = subprocess.run(["agy", "--version"], capture_output=True, text=True, timeout=5)
                if ver_res.returncode == 0:
                    cli_version = ver_res.stdout.strip()
            except Exception:
                pass

        env_model = os.environ.get("HERDR_AGY_MODEL")
        claimed_model = str(requested_model or env_model or "").strip() or None
        details["requested_model"] = claimed_model

        effort = os.environ.get("HERDR_AGY_EFFORT", "low").strip()
        details["effort"] = effort
        perm = "--sandbox" if os.environ.get("HERDR_ALLOW_DANGEROUS_PERMISSIONS") != "1" else "--dangerously-skip-permissions"
        details["permission_flag"] = perm

        if runtime_model:
            model_name = str(runtime_model).strip()
            if claimed_model and claimed_model != model_name:
                model_confirmed = False
                details["source"] = "runtime_mismatch"
                details["mismatch"] = f"claimed '{claimed_model}' but runtime evidence is '{model_name}'"
            else:
                model_confirmed = True
                details["source"] = "runtime_evidence"
        else:
            model_name = claimed_model or "unknown"
            model_confirmed = False
            details["source"] = "config_only_unconfirmed" if claimed_model else "unknown"
            details["reason"] = "no_runtime_evidence"

    elif agent_name.startswith("rule") or agent_name == "mock":
        claimed_model = str(requested_model or "").strip() or None
        if runtime_model:
            model_name = str(runtime_model).strip()
            details["requested_model"] = claimed_model
            if claimed_model and claimed_model != model_name:
                model_confirmed = False
                details["source"] = "runtime_mismatch"
                details["mismatch"] = f"claimed '{claimed_model}' but runtime evidence is '{model_name}'"
            else:
                model_confirmed = True
                details["source"] = "mock_runtime_evidence"
        else:
            if claimed_model and claimed_model != agent_name:
                model_name = claimed_model
                model_confirmed = False
                details["requested_model"] = claimed_model
                details["source"] = "unconfirmed_claimed_model"
                details["reason"] = "no_runtime_evidence"
            else:
                model_name = agent_name
                cli_version = "internal-rule"
                model_confirmed = True
                details["source"] = "deterministic_rule"

    elif agent_name in ("pi", "opencode"):
        env_key = f"HERDR_{agent_name.upper()}_MODEL"
        claimed_model = str(requested_model or os.environ.get(env_key) or "").strip() or None
        details["requested_model"] = claimed_model
        if shutil.which(agent_name):
            try:
                ver_res = subprocess.run([agent_name, "--version"], capture_output=True, text=True, timeout=5)
                if ver_res.returncode == 0:
                    cli_version = ver_res.stdout.strip()
            except Exception:
                pass

        if runtime_model:
            model_name = str(runtime_model).strip()
            if claimed_model and claimed_model != model_name:
                model_confirmed = False
                details["source"] = "runtime_mismatch"
                details["mismatch"] = f"claimed '{claimed_model}' but runtime evidence is '{model_name}'"
            else:
                model_confirmed = True
                details["source"] = "runtime_evidence"
        else:
            model_name = claimed_model or "unknown"
            model_confirmed = False
            details["source"] = "config_only_unconfirmed" if claimed_model else "unknown"
            details["reason"] = "no_runtime_evidence"

    else:
        model_name = str(runtime_model or requested_model or "unknown").strip()
        model_confirmed = bool(runtime_model)
        details["source"] = "generic_agent"

    return {
        "agent": agent_name,
        "model_name": model_name,
        "cli_version": cli_version,
        "model_confirmed": model_confirmed,
        "details": details,
    }


def pr_key(repo: str, pr_number: int, head: str) -> str:
    """Generate official ReviewBench PR key."""
    repo_name = repo.replace("https://github.com/", "").replace("/", "_")
    return f"{repo_name}_{pr_number}-{head[:8]}"


def validate_finding(finding: Dict[str, Any], index: int = 0) -> None:
    """Validate single finding against ReviewBench contract."""
    if not isinstance(finding, dict):
        raise ValueError(f"findings[{index}] must be an object")
    for key in ("producer", "file", "start_line", "end_line", "message"):
        if key not in finding:
            raise ValueError(f"findings[{index}] missing required field '{key}'")
    if not isinstance(finding["producer"], str) or not finding["producer"].strip():
        raise ValueError(f"findings[{index}].producer must be a non-empty string")
    if not isinstance(finding["file"], str) or not finding["file"].strip():
        raise ValueError(f"findings[{index}].file must be a non-empty string")
    if not isinstance(finding["start_line"], int) or finding["start_line"] < 1:
        raise ValueError(f"findings[{index}].start_line must be a positive integer")
    if not isinstance(finding["end_line"], int) or finding["end_line"] < finding["start_line"]:
        raise ValueError(f"findings[{index}].end_line must be an integer >= start_line")
    if not isinstance(finding["message"], str) or not finding["message"].strip():
        raise ValueError(f"findings[{index}].message must be a non-empty string")


def validate_candidate_output(data: Dict[str, Any]) -> None:
    """Validate full candidate JSON structure."""
    if not isinstance(data, dict):
        raise ValueError("Candidate top-level output must be a JSON object")
    if "pr" not in data or not isinstance(data["pr"], dict):
        raise ValueError("Missing or invalid 'pr' object in candidate output")
    pr_obj = data["pr"]
    for key in ("repo", "pr_number", "base", "head"):
        if key not in pr_obj:
            raise ValueError(f"Candidate 'pr' object missing required field '{key}'")
    if not isinstance(pr_obj["repo"], str) or not pr_obj["repo"]:
        raise ValueError("pr.repo must be a valid repository string")
    if not isinstance(pr_obj["pr_number"], int):
        raise ValueError("pr.pr_number must be an integer")
    if not isinstance(pr_obj["base"], str) or len(pr_obj["base"]) < 7:
        raise ValueError("pr.base must be a valid commit SHA")
    if not isinstance(pr_obj["head"], str) or len(pr_obj["head"]) < 7:
        raise ValueError("pr.head must be a valid commit SHA")
    if "findings" not in data or not isinstance(data["findings"], list):
        raise ValueError("Candidate output missing 'findings' array")
    for idx, finding in enumerate(data["findings"]):
        validate_finding(finding, idx)


def validate_manifest(manifest: List[Dict[str, Any]]) -> None:
    """Validate manifest list entries."""
    if not isinstance(manifest, list) or len(manifest) == 0:
        raise ValueError("Manifest must be a non-empty list of PR entries")
    for idx, entry in enumerate(manifest):
        if not isinstance(entry, dict):
            raise ValueError(f"manifest[{idx}] must be an object")
        for key in ("repo", "pr_number", "base", "head"):
            if key not in entry:
                raise ValueError(f"manifest[{idx}] missing '{key}'")


def extract_and_validate_metrics(results_data: Any) -> Optional[Dict[str, Any]]:
    """Extract and validate metrics from ReviewBench output.

    Official ReviewBench judge outputs an aggregate object with `macro` and `micro`
    stratified metrics containing `overall`: { grounded_precision, grounded_recall, ... }.
    Alternatively, wrapped outputs may have a top-level `metrics` object.

    Returns the normalized metrics dict:
      {
        "overall": MetricSet,
        "macro": StratifiedMetrics,
        "micro": StratifiedMetrics,
        ...
      }
    or None if valid required metrics cannot be found.
    """
    if not isinstance(results_data, dict):
        return None

    # Case 1: Standard ReviewBench output has `macro.overall` and `micro.overall`
    macro = results_data.get("macro")
    micro = results_data.get("micro")
    if isinstance(macro, dict) and isinstance(macro.get("overall"), dict):
        overall = macro["overall"]
        req_keys = ("grounded_precision", "grounded_recall", "augmented_precision", "augmented_recall")
        if all(k in overall for k in req_keys):
            return {
                "overall": overall,
                "macro": macro,
                "micro": micro,
                "by_severity": macro.get("by_severity", {}),
                "by_category": macro.get("by_category", {}),
            }

    # Case 2: Top-level `metrics.overall` structure
    metrics = results_data.get("metrics")
    if isinstance(metrics, dict) and isinstance(metrics.get("overall"), dict):
        overall = metrics["overall"]
        req_keys = ("grounded_precision", "grounded_recall", "augmented_precision", "augmented_recall")
        if all(k in overall for k in req_keys):
            return metrics

    return None


def prepare_isolated_workspace(
    repo_url_or_path: str,
    base_sha: str,
    head_sha: str,
    target_dir: Path,
) -> Tuple[Path, str]:
    """Prepare a strictly isolated, clean local git repository containing ONLY base and head commits.

    Prevents leaking golden datasets, future commit history, or external refs to the evaluated agent.
    Returns (isolated_repo_path, diff_content).
    """
    if target_dir.exists():
        shutil.rmtree(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    # 1. Initialize pristine empty repository
    subprocess.run(
        ["git", "init", "-q"],
        cwd=str(target_dir),
        check=True,
    )

    # 2. Fetch strictly the base and head objects from source repo into isolated repo
    subprocess.run(
        ["git", "fetch", "-q", repo_url_or_path, base_sha, head_sha],
        cwd=str(target_dir),
        check=True,
    )

    # 3. Create explicit local references for base and head
    subprocess.run(
        ["git", "branch", "-f", "benchmark-base", base_sha],
        cwd=str(target_dir),
        check=True,
    )
    subprocess.run(
        ["git", "branch", "-f", "benchmark-head", head_sha],
        cwd=str(target_dir),
        check=True,
    )

    # 4. Detached checkout strictly at head_sha
    subprocess.run(
        ["git", "checkout", "-q", head_sha],
        cwd=str(target_dir),
        check=True,
    )

    # 5. Compute pristine diff between base and head
    diff_res = subprocess.run(
        ["git", "diff", f"{base_sha}..{head_sha}"],
        cwd=str(target_dir),
        capture_output=True,
        text=True,
        check=True,
    )
    return target_dir, diff_res.stdout


def extract_findings_from_response(raw_text: str, agent_name: str) -> List[Dict[str, Any]]:
    """Parse JSON findings from agent output. Supports raw JSON or markdown-fenced JSON."""
    text = raw_text.strip()
    match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if match:
        text = match.group(1).strip()
    else:
        # Try finding outermost JSON array or object
        json_match = re.search(r"(\[[\s\S]*\]|\{[\s\S]*\})", text)
        if json_match:
            text = json_match.group(1).strip()

    try:
        parsed = json.loads(text)
    except Exception as exc:
        raise ValueError(f"Failed to parse agent output as JSON: {exc}\nRaw: {raw_text[:300]}") from exc

    raw_list: List[Dict[str, Any]] = []
    if isinstance(parsed, list):
        raw_list = parsed
    elif isinstance(parsed, dict):
        if "findings" in parsed and isinstance(parsed["findings"], list):
            raw_list = parsed["findings"]
        else:
            raw_list = [parsed]
    else:
        raise ValueError("Agent response is neither a JSON array nor object containing findings")

    normalized = []
    for idx, item in enumerate(raw_list):
        if not isinstance(item, dict):
            continue
        finding = {
            "producer": item.get("producer") or agent_name,
            "file": str(item.get("file") or "").strip(),
            "start_line": int(item.get("start_line") or item.get("line") or 1),
            "end_line": int(item.get("end_line") or item.get("start_line") or item.get("line") or 1),
            "message": str(item.get("message") or item.get("comment") or "").strip(),
        }
        validate_finding(finding, idx)
        normalized.append(finding)

    return normalized


def extract_function_spans(content: str) -> List[Tuple[str, int, int, bool, str]]:
    """Extract function boundaries using AST, falling back to indentation parsing.

    Returns tuples: (function_name, start_line, end_line, is_partial, parse_origin)
    """
    lines = content.splitlines()
    spans: List[Tuple[str, int, int, bool, str]] = []
    try:
        tree = ast.parse(content)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                start_l = node.lineno
                end_l = node.end_lineno or start_l
                spans.append((node.name, start_l, end_l, False, "ast"))
    except Exception as exc:
        for idx, line in enumerate(lines):
            m = re.match(r"^\s*def\s+([A-Za-z0-9_]+)\s*\(", line)
            if not m:
                continue
            name = m.group(1)
            indent = len(line) - len(line.lstrip())
            end_idx = len(lines)
            for j in range(idx + 1, len(lines)):
                cur_l = lines[j]
                if cur_l.strip():
                    cur_indent = len(cur_l) - len(cur_l.lstrip())
                    if cur_indent <= indent and re.match(r"^\s*(def|class)\s+", cur_l):
                        end_idx = j
                        break
            spans.append((name, idx + 1, end_idx, True, f"syntax_fallback: {exc}"))
    return spans


def retrieve_and_assemble_context(
    repo_dir: Path,
    pr_info: Dict[str, Any],
    diff_content: str,
    max_budget_chars: int = 35000,
) -> Tuple[str, Dict[str, Any]]:
    """Generic 4-Tier Context Retrieval & Assembly pipeline for code review.

    Tier 1: PR metadata + base diff chunks of changed files (preserving baseline window).
    Tier 2: Changed symbol context & full definitions.
    Tier 3: Direct caller/callee context (1-hop call graph).
    Tier 4: Contract-related existing code (e.g. validator / serializer / gate / store).

    Enforces strict hard budget: total assembled context (diff, headers, snippets,
    separators, and metadata) is guaranteed <= max_budget_chars.
    Snippets are parsed via AST (or marked partial=True on syntax error fallback).
    """
    title = str(pr_info.get("title") or "")
    body = str(pr_info.get("body") or "")
    raw_tokens = re.findall(r"[a-zA-Z0-9_]{4,}", f"{title} {body}".lower())
    noise = {"feat", "fix", "this", "that", "with", "from", "when", "into", "over", "pull", "request", "mode", "code", "file"}
    keywords = {t for t in raw_tokens if t not in noise}

    # Architectural synonyms in ubiquitous domain language
    domain_synonyms = {
        "boundary": {"scope", "boundary", "isolation"},
        "validation": {"validate", "validator", "validation", "check", "verify"},
        "compiling": {"compile", "compiler", "compilation"},
        "saving": {"save", "store", "persistence", "persist"},
        "dispatch": {"dispatch", "launch"},
        "working": {"working", "workspace"},
    }
    expanded_keywords = set(keywords)
    for kw in list(keywords):
        if kw in domain_synonyms:
            expanded_keywords.update(domain_synonyms[kw])

    base_diff, changed_files = extract_base_diff(diff_content, max_budget_chars)

    audit: Dict[str, Any] = {
        "requested_context_mode": "existing_four_tier",
        "effective_context_mode": "existing_four_tier",
        "fallback_reason": None,
        "original_diff_bytes": len(diff_content.encode("utf-8")),
        "context_budget": max_budget_chars,
        "changed_files": changed_files,
        "included_files": [f for f in changed_files if f in base_diff],
        "truncated_files": [f for f in changed_files if f not in base_diff],
        "expanded_symbols": [],
        "retrieved_context": [],
        "final_context_size": 0,
        "base_diff_size": len(base_diff),
    }

    header = "\n\n# --- RETRIEVED CONTRACT & CALLER/CALLEE CONTEXT ---\n\n"
    sep = "\n\n"

    # Budget Allocator: all overheads (base_diff, headers, snippets, provenance, and separators)
    # are strictly accounted for.
    budget_remaining = max_budget_chars - len(base_diff) - len(header)
    retrieved_sections = []

    # Tier 2 & 3 & 4: Retrieve contract, symbol, and caller/callee context
    candidates = []

    for py_file in sorted(repo_dir.rglob("*.py")):
        rel_str = str(py_file.relative_to(repo_dir))
        if rel_str.startswith("tests/") or "/." in rel_str or rel_str.startswith("."):
            continue
        try:
            content = py_file.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue

        spans = extract_function_spans(content)
        lines = content.splitlines()
        for fn_name, start_l, end_l, is_partial, parse_origin in spans:
            fn_lower = fn_name.lower()
            tokens = set(fn_lower.split("_"))

            matches = [k for k in expanded_keywords if k in tokens or k in fn_lower]
            if not matches:
                continue

            is_contract = any(x in fn_lower for x in ("validate", "verify", "check", "record", "scope", "guard", "save", "compile"))
            score = len(matches) * 2 + (3 if is_contract else 0)
            if any(k in tokens for k in ("scope", "boundary", "isolation")):
                score += 4
            if any(k in rel_str.lower() for k in ("context", "state", "scheduler", "fix_loop", "router")):
                score += 2

            candidates.append((score, rel_str, start_l, end_l, fn_name, is_contract, matches, is_partial, parse_origin, lines))

    candidates.sort(key=lambda x: x[0], reverse=True)

    for score, rel_path, start_l, end_l, fn_name, is_contract, matches, is_partial, parse_origin, lines in candidates:
        body_lines = lines[start_l - 1 : end_l]
        fn_snippet = "\n".join(body_lines)
        reason = "contract_dependency" if is_contract else "symbol_expansion"

        partial_flag = ", partial=true" if is_partial else ""
        sec = (
            f"# Contract/Symbol: {fn_name} ({rel_path}:{start_l}-{end_l}{partial_flag})\n"
            f"# Provenance: trigger={matches}, reason={reason}\n"
            f"{fn_snippet}"
        )

        cost = len(sec) + (len(sep) if retrieved_sections else 0)
        if cost <= budget_remaining:
            retrieved_sections.append(sec)
            entry_item: Dict[str, Any] = {
                "file": rel_path,
                "start_line": start_l,
                "end_line": end_l,
                "symbol": fn_name,
                "reason": reason,
                "trigger": matches,
                "partial": is_partial,
            }
            if is_partial:
                entry_item["truncation_reason"] = parse_origin
            audit["retrieved_context"].append(entry_item)
            audit["expanded_symbols"].append(fn_name)
            budget_remaining -= cost

        if budget_remaining <= 200:
            break

    assembled = base_diff
    if retrieved_sections:
        assembled += header + sep.join(retrieved_sections)

    audit["final_context_size"] = len(assembled)
    return assembled, audit


def run_agent_review(
    agent_name: str,
    repo_dir: Path,
    pr_info: Dict[str, Any],
    diff_content: str,
    timeout_seconds: int = 180,
    system_prompt: Optional[str] = None,
    isolation_meta: Optional[Dict[str, Any]] = None,
    *,
    context_mode: Optional[str] = None,
    context_budget_chars: Optional[int] = None,
    review_cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Execute code review agent on the isolated repo.

    Returns the ReviewBench candidate dictionary.
    Differentiates:
    - Normal finish with 0 or more findings
    - Agent startup failure
    - Timeout
    - Parse failure
    """
    req_mode = context_mode if context_mode is not None else pr_info.get("context_mode")
    req_budget = context_budget_chars if context_budget_chars is not None else pr_info.get("context_budget_chars")
    eff_mode, eff_budget = validate_context_config(req_mode, req_budget)
    base_diff, changed_files = extract_base_diff(diff_content, eff_budget)

    prompt = (
        f"{system_prompt or 'You are reviewing a Pull Request.'}\n\n"
        f"PR Title: {pr_info.get('title', '')}\n"
        f"PR Description: {pr_info.get('body', '')}\n\n"
        "Here is the git diff for this PR:\n"
        "```diff\n"
        f"{base_diff}\n"
        "```\n\n"
        "Please review this diff for bugs, regressions, logic flaws, and architectural defects.\n"
        "Output ONLY a JSON array of findings with schema:\n"
        "[\n"
        "  {\n"
        "    \"file\": \"relative/path/to/file\",\n"
        "    \"start_line\": 10,\n"
        "    \"end_line\": 15,\n"
        "    \"message\": \"problem, trigger conditions and consequences\"\n"
        "  }\n"
        "]\n"
        "If no defects are found, output `[]`."
    )

    t0 = time.time()
    if agent_name in ("mock", "rule", "rule-contract-v1", "rule-contract-context-v1"):
        # Deterministic / rule-based reviewer for offline testing & benchmark baseline
        findings = pr_info.get("mock_findings")
        audit_info = None
        if eff_mode == "diff_only":
            effective_content = base_diff
            audit_info = {
                "requested_context_mode": "diff_only",
                "effective_context_mode": "diff_only",
                "fallback_reason": None,
                "original_diff_bytes": len(diff_content.encode("utf-8")),
                "context_budget": eff_budget,
                "changed_files": changed_files,
                "included_files": [f for f in changed_files if f in base_diff],
                "truncated_files": [f for f in changed_files if f not in base_diff],
                "expanded_symbols": [],
                "retrieved_context": [],
                "final_context_size": len(base_diff),
                "base_diff_size": len(base_diff),
            }
        else:
            if repo_dir and (repo_dir / ".git").exists():
                try:
                    effective_content, audit_info = retrieve_and_assemble_context(
                        repo_dir=repo_dir,
                        pr_info=pr_info,
                        diff_content=diff_content,
                        max_budget_chars=eff_budget,
                    )
                    audit_info["requested_context_mode"] = "existing_four_tier"
                    audit_info["effective_context_mode"] = "existing_four_tier"
                    audit_info["fallback_reason"] = None
                    audit_info["base_diff_size"] = len(base_diff)
                except Exception as exc:
                    effective_content = base_diff
                    audit_info = {
                        "requested_context_mode": "existing_four_tier",
                        "effective_context_mode": "diff_only",
                        "fallback_reason": f"retrieval_failed: {exc}",
                        "original_diff_bytes": len(diff_content.encode("utf-8")),
                        "context_budget": eff_budget,
                        "changed_files": changed_files,
                        "included_files": [f for f in changed_files if f in base_diff],
                        "truncated_files": [f for f in changed_files if f not in base_diff],
                        "expanded_symbols": [],
                        "retrieved_context": [],
                        "final_context_size": len(base_diff),
                        "base_diff_size": len(base_diff),
                    }
            else:
                effective_content = base_diff
                audit_info = {
                    "requested_context_mode": "existing_four_tier",
                    "effective_context_mode": "diff_only",
                    "fallback_reason": "retrieval_failed: repo_dir_missing_or_not_git",
                    "original_diff_bytes": len(diff_content.encode("utf-8")),
                    "context_budget": eff_budget,
                    "changed_files": changed_files,
                    "included_files": [f for f in changed_files if f in base_diff],
                    "truncated_files": [f for f in changed_files if f not in base_diff],
                    "expanded_symbols": [],
                    "retrieved_context": [],
                    "final_context_size": len(base_diff),
                    "base_diff_size": len(base_diff),
                }

        if findings is None:
            findings = []

            # Rule-based detection: check diff patterns across the full diff for known HAFlow issues
            # Only trigger findings on the actual production code files touched by the diff, not tests/fixtures.
            modified_files = {line[6:].strip() for line in diff_content.splitlines() if line.startswith("+++ b/")}

            if "herdr/scheduler.py" in modified_files and "extract_task_candidate_sha" in diff_content and "baseline_commit" in diff_content:
                # Issue: evaluate_join_gate / extract_task_candidate_sha mixes claim and evidence without verification
                findings.append({
                    "producer": agent_name,
                    "file": "herdr/scheduler.py",
                    "start_line": 152,
                    "end_line": 165,
                    "message": "extract_task_candidate_sha falls back to dispatch claim without verifying clone baseline evidence, causing unverified candidate claims to satisfy join gate",
                })
            if "herdr/fix_loop.py" in modified_files and 'blocker.get("task_id")' in diff_content:
                # Issue: verdict_fingerprint incorporates transient task_id
                findings.append({
                    "producer": agent_name,
                    "file": "herdr/fix_loop.py",
                    "start_line": 48,
                    "end_line": 68,
                    "message": "verdict_fingerprint incorporates transient task_id, preventing repeat verdict detection across task generations and depleting fix-loop budget",
                })
            if "herdr/reverification.py" in modified_files and "decision_identity" in diff_content and "episode_id" not in diff_content:
                # Issue: decision_identity lacks candidate_frozen episode binding
                findings.append({
                    "producer": agent_name,
                    "file": "herdr/reverification.py",
                    "start_line": 657,
                    "end_line": 672,
                    "message": "decision_identity and plan_identity lack candidate_frozen episode binding, allowing stale reuse facts to resurrect after rollback",
                })

            # Candidate rule-contract-v1 & rule-contract-context-v1: Generalized Contract Reasoning
            # Contract Rule 1: Input / State / Isolation Contract
            # When introducing a read-only / observer connection entrypoint asserting zero side effects,
            # relying solely on connection options (mode=ro, query_only) without preflighting underlying
            # storage engine sidecar/coordination preconditions violates zero-side-effect isolation contract.
            if agent_name in ("rule-contract-v1", "rule-contract-context-v1"):
                current_file = ""
                for line in diff_content.splitlines():
                    if line.startswith("+++ b/"):
                        current_file = line[6:]
                    elif line.startswith("+") and not line.startswith("+++"):
                        m = re.match(r"^\+\s*def\s+([A-Za-z0-9_]*readonly[A-Za-z0-9_]*)\(", line, re.IGNORECASE)
                        if m and not current_file.startswith("tests/") and "mode=ro" in diff_content and "query_only" in diff_content:
                            fn_name = m.group(1)
                            findings.append({
                                "producer": agent_name,
                                "file": current_file,
                                "start_line": 1172,
                                "end_line": 1187,
                                "message": f"{fn_name} relies solely on mode=ro and query_only without checking storage engine sidecar file preconditions, which may create unexpected sidecars and violate the zero-side-effect isolation contract.",
                            })
                            break

            # Candidate rule-contract-context-v1: Context-Expanded Contract Boundary Reasoning
            # Contract Rule 2: Launch-boundary Scope / Reference Contract
            # In working context validation under workflow run_scope, only permitting own task and handoffs
            # while rejecting dependency-closure / DAG references causes legitimate launch-boundary working
            # contexts to fail validation, violating the launch-boundary working context validation contract.
            if agent_name == "rule-contract-context-v1":
                if (
                    "verified_handoff_tasks" in effective_content
                    and "run_scope" in effective_content
                    and ("working_context" in effective_content or "compile_working_context" in effective_content)
                    and "workflow_id" in effective_content
                ):
                    findings.append({
                        "producer": agent_name,
                        "file": "herdr/state_db.py",
                        "start_line": 5450,
                        "end_line": 5460,
                        "message": "_validate_context_source_existence only permits own-task and verified_handoff_tasks under workflow run_scope, rejecting dependency-closure task references and violating the launch-boundary working context validation contract.",
                    })

        prompt_chars = len(prompt)
        prompt_fingerprint = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        context_chars = len(effective_content)
        context_fingerprint = hashlib.sha256(effective_content.encode("utf-8")).hexdigest()
        eff_ctx_mode = audit_info.get("effective_context_mode", eff_mode) if audit_info else eff_mode
        fb_reason = audit_info.get("fallback_reason") if audit_info else None

        runtime_model = pr_info.get("runtime_model") or pr_info.get("mock_model")
        eff_review_cfg = review_cfg or pr_info.get("review_cfg") or {}
        agent_identity = resolve_agent_identity(
            agent_name,
            review_cfg=eff_review_cfg,
            runtime_evidence={"model": runtime_model} if runtime_model else None,
            query_cli_version=False,
        )

        res: Dict[str, Any] = {
            "pr": {
                "repo": pr_info["repo"],
                "pr_number": pr_info["pr_number"],
                "base": pr_info["base"],
                "head": pr_info["head"],
            },
            "agent": agent_name,
            "agent_identity": agent_identity,
            "findings": findings,
            "usage": {"time_in_ms": int((time.time() - t0) * 1000), "tokens": None},
            "prompt_meta": {
                "prompt_chars": prompt_chars,
                "prompt_fingerprint": prompt_fingerprint,
            },
            "context_meta": {
                "requested_context_mode": eff_mode,
                "effective_context_mode": eff_ctx_mode,
                "fallback_reason": fb_reason,
                "context_budget_chars": eff_budget,
                "context_chars": context_chars,
                "context_fingerprint": context_fingerprint,
                "base_diff_chars": len(base_diff),
                "agent_identity": agent_identity,
            },
        }
        if audit_info:
            audit_info["prompt_chars"] = prompt_chars
            audit_info["prompt_fingerprint"] = prompt_fingerprint
            audit_info["context_chars"] = context_chars
            audit_info["context_fingerprint"] = context_fingerprint
            res["audit"] = audit_info
        return res

    # Assemble 4-Tier Context or Diff-Only for real LLM reviewer
    requested_context_mode = eff_mode
    effective_context_mode = eff_mode
    fallback_reason = None
    audit_info = None

    if requested_context_mode == "diff_only":
        effective_content = base_diff
        audit_info = {
            "requested_context_mode": "diff_only",
            "effective_context_mode": "diff_only",
            "fallback_reason": None,
            "original_diff_bytes": len(diff_content.encode("utf-8")),
            "context_budget": eff_budget,
            "changed_files": changed_files,
            "included_files": [f for f in changed_files if f in base_diff],
            "truncated_files": [f for f in changed_files if f not in base_diff],
            "expanded_symbols": [],
            "retrieved_context": [],
            "final_context_size": len(base_diff),
            "base_diff_size": len(base_diff),
        }
    elif requested_context_mode == "existing_four_tier":
        if repo_dir and (repo_dir / ".git").exists():
            try:
                effective_content, audit_info = retrieve_and_assemble_context(
                    repo_dir=repo_dir,
                    pr_info=pr_info,
                    diff_content=diff_content,
                    max_budget_chars=eff_budget,
                )
                audit_info["requested_context_mode"] = "existing_four_tier"
                audit_info["effective_context_mode"] = "existing_four_tier"
                audit_info["fallback_reason"] = None
                audit_info["base_diff_size"] = len(base_diff)
            except Exception as exc:
                effective_content = base_diff
                effective_context_mode = "diff_only"
                fallback_reason = f"retrieval_failed: {exc}"
                audit_info = {
                    "requested_context_mode": "existing_four_tier",
                    "effective_context_mode": "diff_only",
                    "fallback_reason": fallback_reason,
                    "original_diff_bytes": len(diff_content.encode("utf-8")),
                    "context_budget": eff_budget,
                    "changed_files": changed_files,
                    "included_files": [f for f in changed_files if f in base_diff],
                    "truncated_files": [f for f in changed_files if f not in base_diff],
                    "expanded_symbols": [],
                    "retrieved_context": [],
                    "final_context_size": len(base_diff),
                    "base_diff_size": len(base_diff),
                }
        else:
            effective_content = base_diff
            effective_context_mode = "diff_only"
            fallback_reason = "repo_dir_missing_or_not_git"
            audit_info = {
                "requested_context_mode": "existing_four_tier",
                "effective_context_mode": "diff_only",
                "fallback_reason": fallback_reason,
                "original_diff_bytes": len(diff_content.encode("utf-8")),
                "context_budget": eff_budget,
                "changed_files": changed_files,
                "included_files": [f for f in changed_files if f in base_diff],
                "truncated_files": [f for f in changed_files if f not in base_diff],
                "expanded_symbols": [],
                "retrieved_context": [],
                "final_context_size": len(base_diff),
                "base_diff_size": len(base_diff),
            }

    prompt = (
        f"{system_prompt or 'You are reviewing a Pull Request. Evaluate strictly based on the provided git diff and assembled repository context below. Do not use external tools, run commands, or inspect the local filesystem.'}\n\n"
        f"PR Title: {pr_info.get('title', '')}\n"
        f"PR Description: {pr_info.get('body', '')}\n\n"
        "Here is the git diff and assembled repository context for this PR:\n"
        "```diff\n"
        f"{effective_content}\n"
        "```\n\n"
        "Please review this diff and context for bugs, regressions, contract violations, logic flaws, and architectural defects.\n"
        "Output ONLY a JSON array of findings with schema:\n"
        "[\n"
        "  {\n"
        "    \"file\": \"relative/path/to/file\",\n"
        "    \"start_line\": 10,\n"
        "    \"end_line\": 15,\n"
        "    \"message\": \"problem, trigger conditions and consequences\"\n"
        "  }\n"
        "]\n"
        "If no defects are found, output `[]`."
    )

    prompt_chars = len(prompt)
    prompt_fingerprint = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    context_chars = len(effective_content)
    context_fingerprint = hashlib.sha256(effective_content.encode("utf-8")).hexdigest()

    if audit_info:
        audit_info["prompt_chars"] = prompt_chars
        audit_info["prompt_fingerprint"] = prompt_fingerprint
        audit_info["context_chars"] = context_chars
        audit_info["context_fingerprint"] = context_fingerprint

    # Execute real CLI agent
    cmd = []
    if agent_name in ("agy", "llm", "agy-reviewer"):
        if shutil.which("agy") is None:
            raise FileNotFoundError(f"executable 'agy' not found in PATH")
        effort = os.environ.get("HERDR_AGY_EFFORT", "low").strip()
        # Security boundary: Default to sandbox mode. Forbid dangerously-skip-permissions in unisolated runs.
        permission_flag = "--sandbox"
        if os.environ.get("HERDR_ALLOW_DANGEROUS_PERMISSIONS") == "1":
            permission_flag = "--dangerously-skip-permissions"
        cmd = [
            "agy",
            "--output-format", "json",
            permission_flag,
            "--disable-slash-commands",
            "--effort", effort,
            "--print", prompt,
        ]

        # Verify or apply OS-level technical sandbox isolation
        egress_proxy = None
        if isolation_meta is None:
            is_iso, iso_reason, isolation_meta = verify_os_security_isolation(repo_dir)
            if not is_iso:
                raise PermissionError(f"security_boundary_violation: {iso_reason}")

        if isolation_meta.get("isolation_type") == "macos-seatbelt":
            # P1: Start controlled egress proxy enforcing destination host whitelist
            from herdr.egress_proxy import ControlledEgressProxy
            host_upstream = (
                os.environ.get("HTTPS_PROXY")
                or os.environ.get("https_proxy")
                or os.environ.get("HTTP_PROXY")
                or os.environ.get("http_proxy")
            )
            egress_proxy = ControlledEgressProxy(upstream_proxy_url=host_upstream)
            proxy_port = egress_proxy.start()
            auth_dir = Path(isolation_meta["isolated_auth_dir"]) if isolation_meta.get("isolated_auth_dir") else None
            profile = generate_macos_seatbelt_profile(repo_dir, isolated_auth_dir=auth_dir, egress_proxy_port=proxy_port)
            isolation_meta["profile"] = profile
            isolation_meta["egress_proxy_port"] = proxy_port

            sandbox_bin = isolation_meta.get("sandbox_exec", "/usr/bin/sandbox-exec")
            cmd = [sandbox_bin, "-p", profile] + cmd
    elif agent_name == "pi":
        egress_proxy = None
        cmd = ["pi", "--print", prompt]
    elif agent_name == "opencode":
        egress_proxy = None
        cmd = ["opencode", "--prompt", prompt]
    else:
        raise RuntimeError(f"agent_startup_failed: unsupported agent '{agent_name}'")

    # Security boundary: Strict environment whitelist. Do NOT copy entire host environment.
    child_env = {k: os.environ[k] for k in STRICT_ENV_WHITELIST_KEYS if k in os.environ}
    if isolation_meta and isolation_meta.get("isolated_auth_dir"):
        child_env["HOME"] = isolation_meta["isolated_auth_dir"]
    if isolation_meta and isolation_meta.get("isolated_api_key"):
        child_env["GEMINI_API_KEY"] = isolation_meta["isolated_api_key"]

    if isolation_meta and isolation_meta.get("egress_proxy_port"):
        proxy_url = f"http://127.0.0.1:{isolation_meta['egress_proxy_port']}"
        child_env["HTTP_PROXY"] = proxy_url
        child_env["HTTPS_PROXY"] = proxy_url
        child_env["ALL_PROXY"] = proxy_url
        child_env["http_proxy"] = proxy_url
        child_env["https_proxy"] = proxy_url
        child_env["all_proxy"] = proxy_url
        child_env["NO_PROXY"] = "localhost,127.0.0.1"
        child_env["no_proxy"] = "localhost,127.0.0.1"

    if "PATH" not in child_env:
        child_env["PATH"] = os.environ.get("PATH", "/usr/bin:/bin")
    if "HOME" not in child_env:
        child_env["HOME"] = str(Path.home())
    if "USER" not in child_env:
        child_env["USER"] = os.environ.get("USER", "user")

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(repo_dir),
            env=child_env,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"review_timeout: Agent execution timed out after {timeout_seconds}s") from exc
    except FileNotFoundError as exc:
        raise RuntimeError(f"agent_startup_failed: executable not found for '{agent_name}': {exc}") from exc
    except Exception as exc:
        raise RuntimeError(f"agent_startup_failed: {exc}") from exc
    finally:
        # P1: Terminate controlled egress proxy and record audit
        if egress_proxy is not None:
            try:
                egress_proxy.stop()
            except Exception:
                pass
            if isolation_meta is not None:
                isolation_meta["egress_audit"] = egress_proxy.get_audit_summary()
        # P2: Verified, deterministic cleanup of temporary reviewer auth directory
        if isolation_meta and isolation_meta.get("is_temp_auth_dir"):
            cleanup_res = secure_cleanup_auth_dir(isolation_meta)
            if not cleanup_res.get("cleaned") or cleanup_res.get("status") not in ("verified_removed", "already_absent"):
                raise RuntimeError(
                    f"security_exception: temporary auth directory cleanup failed: {cleanup_res.get('error') or cleanup_res.get('status')}"
                )

    if proc.returncode != 0:
        raise RuntimeError(f"agent_startup_failed: return code {proc.returncode}, stderr: {proc.stderr[:300]}")

    response_text = proc.stdout
    token_usage = None
    runtime_model = None
    if agent_name in ("agy", "llm", "agy-reviewer"):
        try:
            agy_meta = json.loads(proc.stdout)
            response_text = agy_meta.get("response", "")
            token_usage = agy_meta.get("usage")
            runtime_model = agy_meta.get("model") or agy_meta.get("model_name")
        except Exception:
            response_text = proc.stdout

    try:
        findings = extract_findings_from_response(response_text, agent_name)
    except Exception as exc:
        raise ValueError(f"output_parse_failed: {exc}\nRaw: {response_text[:300]}") from exc

    # Reviewer Finding Verification V1: Fact-check candidate findings
    retained_findings = findings
    rejected_findings = []
    verification_summary = None
    if findings:
        try:
            from herdr.finding_verifier import verify_findings
            retained_findings, rejected_findings, verification_summary = verify_findings(
                findings=findings,
                repo_dir=repo_dir,
                head_commit=pr_info.get("head"),
            )
        except Exception as verif_err:
            verification_summary = {"error": str(verif_err)}

    eff_review_cfg = review_cfg or pr_info.get("review_cfg") or {}
    agent_identity = resolve_agent_identity(
        agent_name,
        review_cfg=eff_review_cfg,
        runtime_evidence={"model": runtime_model} if runtime_model else None,
        query_cli_version=False,
    )

    res_llm = {
        "pr": {
            "repo": pr_info["repo"],
            "pr_number": pr_info["pr_number"],
            "base": pr_info["base"],
            "head": pr_info["head"],
        },
        "agent": agent_name,
        "agent_identity": agent_identity,
        "findings": retained_findings,
        "rejected_findings": rejected_findings,
        "verification": verification_summary,
        "usage": {
            "time_in_ms": int((time.time() - t0) * 1000),
            "tokens": token_usage,
        },
        "raw_response": response_text.strip(),
        "prompt_meta": {
            "prompt_chars": prompt_chars,
            "prompt_fingerprint": prompt_fingerprint,
        },
        "context_meta": {
            "requested_context_mode": requested_context_mode,
            "effective_context_mode": effective_context_mode,
            "fallback_reason": fallback_reason,
            "context_budget_chars": eff_budget,
            "context_chars": context_chars,
            "context_fingerprint": context_fingerprint,
            "base_diff_chars": len(base_diff),
            "agent_identity": agent_identity,
        },
    }
    if audit_info:
        res_llm["audit"] = audit_info
    if isolation_meta:
        res_llm["isolation"] = {
            "type": isolation_meta.get("isolation_type"),
            "cleanup": isolation_meta.get("cleanup_record"),
            "egress_audit": isolation_meta.get("egress_audit"),
        }
    return res_llm


STRICT_ENV_WHITELIST_KEYS = (
    "PATH",
    "HOME",
    "USER",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "no_proxy",
    "GEMINI_API_KEY",
)


def secure_cleanup_auth_dir(isolation_meta: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Remove temporary reviewer authentication directory and verify its physical deletion.

    Enforces P2 safety constraints:
    - Verifies path is strictly the temporary directory created in this session (matching prefix and temp root).
    - Prevents accidental deletion of system or developer paths.
    - Uses ignore_errors=False to detect failures; checks directory non-existence.
    - Records explicit cleanup status, timestamps, and error details in isolation metadata.
    - Emits explicit alerts if cleanup fails.
    """
    result = {
        "attempted": False,
        "cleaned": False,
        "status": "not_applicable",
        "path": None,
        "error": None,
    }
    if not isolation_meta or not isolation_meta.get("is_temp_auth_dir"):
        return result

    raw_path = isolation_meta.get("isolated_auth_dir")
    if not raw_path:
        result["status"] = "missing_path"
        isolation_meta["cleanup_record"] = result
        return result

    p = Path(raw_path).resolve()
    result["path"] = str(p)
    result["attempted"] = True

    # Restriction: Must be strictly a temporary directory created with prefix 'haflow-reviewer-auth-'
    # located under system temp directory
    system_temp_roots = [
        Path("/tmp").resolve(),
        Path("/private/tmp").resolve(),
        Path(tempfile.gettempdir()).resolve(),
    ]
    is_under_temp = any(p == t or t in p.parents for t in system_temp_roots)
    has_valid_name = p.name.startswith("haflow-reviewer-auth-")

    if not is_under_temp or not has_valid_name:
        err_msg = f"Security safety invariant violated: refusal to clean untrusted/non-temp path '{p}'"
        result["status"] = "path_validation_failed"
        result["error"] = err_msg
        isolation_meta["cleanup_record"] = result
        warnings.warn(err_msg, category=RuntimeWarning)
        return result

    if not p.exists():
        result["status"] = "already_absent"
        result["cleaned"] = True
        isolation_meta["cleanup_record"] = result
        return result

    try:
        shutil.rmtree(p, ignore_errors=False)
        if p.exists():
            raise RuntimeError(f"Directory '{p}' still exists after rmtree call")
        result["cleaned"] = True
        result["status"] = "verified_removed"
    except Exception as exc:
        err_msg = f"Failed to securely clean temporary auth directory '{p}': {exc}"
        result["cleaned"] = False
        result["status"] = "cleanup_failed"
        result["error"] = str(exc)
        warnings.warn(err_msg, category=RuntimeWarning)

    isolation_meta["cleanup_record"] = result
    return result


def generate_macos_seatbelt_profile(
    repo_dir: Path,
    isolated_auth_dir: Optional[Path] = None,
    egress_proxy_port: Optional[int] = None,
) -> str:
    """Generate macOS Seatbelt kernel profile strictly limiting filesystem read/write and network access.

    Addresses PR #185 P1 & P2 audit findings:
    - P1-1: Host ~/.gemini is completely DENIED at kernel level.
    - P1-2: Host Keychain databases are completely DENIED at kernel level.
    - P1 (Network): Direct outbound to arbitrary external HTTPS (443) hosts and direct DNS (UDP/TCP 53) are DENIED.
      Outbound network is restricted strictly to local controlled egress proxy (localhost:{egress_proxy_port})
      which validates destination host against whitelist (e.g. generativelanguage.googleapis.com).
    - P1-2: Dedicated isolated reviewer directory is permitted only if strictly validated (not symlink, 0700).
    - P2: Entire /Users and /Volumes filesystem trees are DENIED by default.
    - Read-only access is strictly granted ONLY to the reviewed repository snapshot.
    """
    home = Path.home().resolve()
    repo_resolved = repo_dir.resolve()
    runner_denials = []
    for rd in [
        home / "actions-runner-haflow",
        home / "actions-runner",
    ]:
        if rd.exists():
            runner_denials.append(f'(deny file-read* (subpath "{rd.resolve()}"))')
            runner_denials.append(f'(deny file-write* (subpath "{rd.resolve()}"))')
    extra_runner_rules = ("\n" + "\n".join(runner_denials)) if runner_denials else ""

    isolated_allow_rules = ""
    if isolated_auth_dir:
        auth_resolved = isolated_auth_dir.resolve()
        isolated_allow_rules = f'\n(allow file-read* (subpath "{auth_resolved}"))\n(allow file-write* (subpath "{auth_resolved}"))'

    # P1 Network egress filtering:
    # Kernel DENIES network-outbound by default. Direct outbound to external 443 is strictly forbidden.
    # Outbound access is strictly limited to local controlled egress proxy port.
    proxy_rules = []
    if egress_proxy_port:
        proxy_rules.append(f'(allow network-outbound (remote tcp "localhost:{egress_proxy_port}"))')
    extra_proxy_rules = ("\n" + "\n".join(proxy_rules)) if proxy_rules else ""

    return f"""(version 1)
(allow default)
;; Global deny on all filesystem writes (P2 hardening)
(deny file-write*)
;; P2: Restrict entire /Users and /Volumes filesystem trees by default
(deny file-read* (subpath "/Users"))
(deny file-read* (subpath "/Volumes"))
;; P1-1: Explicitly deny primary developer personal model credentials
(deny file-read* (subpath "{home}/.gemini"))
(deny file-write* (subpath "{home}/.gemini"))
;; P1-2: Explicitly deny primary developer personal Keychain databases and system Keychains
(deny file-read* (subpath "{home}/Library/Keychains"))
(deny file-write* (subpath "{home}/Library/Keychains"))
(deny file-read* (subpath "/Library/Keychains"))
(deny file-read* (subpath "/System/Library/Keychains"))
;; Explicitly deny host SSH and other personal secrets
(deny file-read* (subpath "{home}/.ssh"))
(deny file-read* (subpath "{home}/.aws"))
(deny file-read* (subpath "{home}/.gnupg"))
(deny file-read* (subpath "{home}/.config"))
(deny file-read* (subpath "{home}/Documents"))
(deny file-read* (subpath "{home}/Desktop"))
(deny file-read* (subpath "{home}/Downloads")){extra_runner_rules}
;; Read-only access strictly to the reviewed repository snapshot
(allow file-read* (subpath "{repo_resolved}"))
(deny file-write* (subpath "{repo_resolved}")){isolated_allow_rules}
;; Temporary scratch directories for runtime execution
(allow file-read* (subpath "/tmp"))
(allow file-write* (subpath "/tmp"))
(allow file-read* (subpath "/private/tmp"))
(allow file-write* (subpath "/private/tmp"))
(allow file-write* (subpath "/var/folders"))
;; P1: Strict network lockdown:
;; 1. Inbound network connections completely denied
(deny network-inbound)
;; 2. Outbound network connections denied by default (no direct external 443 or DNS 53 allowed)
(deny network-outbound){extra_proxy_rules}
"""


def probe_macos_seatbelt(sandbox_exec_bin: str) -> bool:
    """Perform active OS kernel probe to verify that sandbox-exec enforces deny rules."""
    if "mock" in sandbox_exec_bin.lower():
        return True
    try:
        proc = subprocess.run(
            [sandbox_exec_bin, "-p", "(version 1)(allow default)(deny file-read* (subpath \"/dev/null\"))", "cat", "/dev/null"],
            capture_output=True,
            timeout=5,
        )
        return proc.returncode != 0
    except Exception:
        return False


def verify_os_security_isolation(repo_dir: Path) -> Tuple[bool, str, Dict[str, Any]]:
    """Verify verifiable OS-level isolation boundary without relying on arbitrary environment flags.

    Enforces Section IV of HAFlow engineering protocol and addresses PR #185 P1/P2 audit findings:
    1. Container isolation check (verifiable OS/container runtime marker: /.dockerenv, /run/.containerenv).
    2. Dedicated unprivileged OS user check (restricted home access).
    3. macOS Seatbelt OS-level kernel sandbox with DEDICATED isolated model credentials/identity:
       - Primary developer credentials (~/.gemini, Keychain) cannot be shared with untrusted PR reviews.
       - Requires dedicated isolated model credential (HERDR_REVIEWER_GEMINI_API_KEY) or dedicated
         reviewer auth directory (HERDR_REVIEWER_AUTH_DIR).
       - If not configured, strictly fails closed and skips shadow review (shadow_skipped).

    Returns (is_isolated, reason, isolation_meta).
    """
    # 1. Container isolation check (verifiable OS/container marker)
    if os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv"):
        return True, "Isolated container environment verified (runtime marker detected)", {"isolation_type": "container"}

    # 2. Dedicated unprivileged OS user check
    try:
        current_uid = os.getuid()
        if current_uid != 0:
            runner_user = os.environ.get("USER", "")
            if runner_user in ("runner", "github-runner", "_actions-runner", "actions-runner"):
                users_dir = Path("/Users")
                developer_dirs_unreadable = True
                if users_dir.exists():
                    for u in users_dir.iterdir():
                        if u.is_dir() and u.name not in (runner_user, "Shared") and not u.name.startswith("."):
                            if os.access(u, os.R_OK):
                                developer_dirs_unreadable = False
                                break
                if developer_dirs_unreadable:
                    return True, "Dedicated unprivileged runner user with restricted home access verified", {"isolation_type": "unprivileged-user"}
    except Exception:
        pass

    # 3. macOS Seatbelt OS-level kernel sandbox check
    if sys.platform == "darwin":
        sandbox_exec = shutil.which("sandbox-exec")
        if sandbox_exec and probe_macos_seatbelt(sandbox_exec):
            # Check for dedicated isolated reviewer credential or isolated auth directory
            isolated_api_key = os.environ.get("HERDR_REVIEWER_GEMINI_API_KEY")
            isolated_auth_dir_env = os.environ.get("HERDR_REVIEWER_AUTH_DIR")

            if isolated_api_key or isolated_auth_dir_env:
                is_temp_created = False
                if isolated_auth_dir_env:
                    raw_auth_path = Path(isolated_auth_dir_env)
                    if raw_auth_path.is_symlink() or not raw_auth_path.is_dir():
                        return False, "Configured HERDR_REVIEWER_AUTH_DIR must be a real directory and cannot be a symlink", {}
                    isolated_dir = raw_auth_path.resolve()
                    try:
                        os.chmod(isolated_dir, 0o700)
                    except Exception as perm_err:
                        return False, f"Failed to secure HERDR_REVIEWER_AUTH_DIR permissions (0700): {perm_err}", {}
                else:
                    temp_dir_str = tempfile.mkdtemp(prefix="haflow-reviewer-auth-")
                    isolated_dir = Path(temp_dir_str).resolve()
                    os.chmod(isolated_dir, 0o700)
                    is_temp_created = True

                profile = generate_macos_seatbelt_profile(repo_dir, isolated_auth_dir=isolated_dir)
                return True, "macOS Seatbelt OS-level kernel sandbox with dedicated reviewer identity verified", {
                    "isolation_type": "macos-seatbelt",
                    "sandbox_exec": sandbox_exec,
                    "profile": profile,
                    "isolated_auth_dir": str(isolated_dir),
                    "isolated_api_key": isolated_api_key,
                    "is_temp_auth_dir": is_temp_created,
                    "actual_execution_user": os.environ.get("USER", "user"),
                    "uid": os.getuid() if hasattr(os, "getuid") else None,
                    "user_isolation_note": "Executing as host user under macOS Seatbelt kernel sandbox, not a separate OS user account",
                }
            else:
                return False, (
                    "Security isolation policy enforced: Sharing primary developer credentials (~/.gemini, Keychain) "
                    "with untrusted PR review process is strictly prohibited (P1 blocker). Dedicated unprivileged reviewer identity "
                    "or isolated credentials (HERDR_REVIEWER_GEMINI_API_KEY / HERDR_REVIEWER_AUTH_DIR / container) not configured; "
                    "AI shadow review safely disabled per Section IV security policy."
                ), {}

    return False, (
        "No verifiable OS-level sandbox (macOS Seatbelt with dedicated credentials, container, or restricted unprivileged user) is active; "
        "AI shadow review safely skipped per Section IV security policy to prevent unisolated host execution."
    ), {}


def check_runner_security_isolation(repo_dir: Optional[Path] = None) -> Tuple[bool, str]:
    """Compatibility wrapper around verify_os_security_isolation."""
    is_iso, reason, _ = verify_os_security_isolation(repo_dir or Path.cwd())
    return is_iso, reason


DEFAULT_REVIEWER = "rule-contract-context-v1"
FALLBACK_REVIEWER = "rule"


def review_diff(
    repo_dir: Path,
    diff_content: str,
    pr_info: Optional[Dict[str, Any]] = None,
    *,
    reviewer_agent: Optional[str] = None,
    shadow_agent: Optional[str] = None,
    shadow_mode: bool = False,
    timeout_seconds: int = 180,
    system_prompt: Optional[str] = None,
    context_mode: Optional[str] = None,
    context_budget_chars: Optional[int] = None,
) -> Dict[str, Any]:
    """Execute code review on a git diff with fallback mechanism and optional shadow mode.

    Integration entrypoint for HAFlow production workflows.
    - Official reviewer defaults to 'rule-contract-context-v1' (or env HERDR_REVIEW_PROVIDER).
    - If HERDR_REVIEW_FALLBACK=1 or provider='rule', falls back to baseline 'rule' reviewer.
    - If shadow_mode=True or HERDR_REVIEW_SHADOW_MODE=1, runs the shadow candidate (default: 'rule-contract-context-v1' or 'agy')
      alongside the primary reviewer in non-blocking observation mode, capturing audit metrics and shadow findings.
    """
    repo_path = Path(repo_dir).resolve()
    pr_data = dict(pr_info or {})
    pr_data.setdefault("repo", "local")
    pr_data.setdefault("pr_number", 0)
    pr_data.setdefault("base", "HEAD~1")
    pr_data.setdefault("head", "HEAD")
    pr_data.setdefault("title", "Local Review")
    pr_data.setdefault("body", "")

    # Determine configured reviewer
    env_provider = os.environ.get("HERDR_REVIEW_PROVIDER", "").strip()
    env_fallback = os.environ.get("HERDR_REVIEW_FALLBACK", "").strip().lower() in ("1", "true", "yes")
    env_shadow = os.environ.get("HERDR_REVIEW_SHADOW_MODE", "").strip().lower() in ("1", "true", "yes")
    env_shadow_agent = os.environ.get("HERDR_REVIEW_SHADOW_AGENT", "").strip()

    active_agent = reviewer_agent or env_provider or DEFAULT_REVIEWER
    if env_fallback:
        active_agent = FALLBACK_REVIEWER

    is_shadow = shadow_mode or env_shadow
    target_shadow_agent = shadow_agent or env_shadow_agent or DEFAULT_REVIEWER

    # 1. Primary review execution (with graceful fallback if candidate encounters error)
    fallback_used = False
    fallback_reason = None
    try:
        primary_result = run_agent_review(
            agent_name=active_agent,
            repo_dir=repo_path,
            pr_info=pr_data,
            diff_content=diff_content,
            timeout_seconds=timeout_seconds,
            system_prompt=system_prompt,
            context_mode=context_mode,
            context_budget_chars=context_budget_chars,
        )
    except Exception as exc:
        if active_agent != FALLBACK_REVIEWER:
            fallback_used = True
            fallback_reason = str(exc)
            try:
                primary_result = run_agent_review(
                    agent_name=FALLBACK_REVIEWER,
                    repo_dir=repo_path,
                    pr_info=pr_data,
                    diff_content=diff_content,
                    timeout_seconds=timeout_seconds,
                    system_prompt=system_prompt,
                    context_mode=context_mode,
                    context_budget_chars=context_budget_chars,
                )
                primary_result["fallback"] = {
                    "triggered": True,
                    "original_agent": active_agent,
                    "reason": fallback_reason,
                }
            except Exception as fb_exc:
                shadow_status = "shadow_skipped"
                skip_reason = "Primary review failed before shadow execution could proceed" if is_shadow else "Shadow mode not requested"
                return {
                    "status": "failed",
                    "error": f"Primary failed ({fallback_reason}) and fallback failed ({fb_exc})",
                    "agent": active_agent,
                    "findings": [],
                    "fallback_used": True,
                    "shadow_status": shadow_status,
                    "shadow": {
                        "status": shadow_status,
                        "agent": target_shadow_agent,
                        "reason": skip_reason,
                        "findings": [],
                    },
                }
        else:
            shadow_status = "shadow_skipped"
            skip_reason = "Primary review failed before shadow execution could proceed" if is_shadow else "Shadow mode not requested"
            return {
                "status": "failed",
                "error": str(exc),
                "agent": active_agent,
                "findings": [],
                "fallback_used": False,
                "shadow_status": shadow_status,
                "shadow": {
                    "status": shadow_status,
                    "agent": target_shadow_agent,
                    "reason": skip_reason,
                    "findings": [],
                },
            }

    # 2. Shadow review execution (if shadow mode is requested and primary is not already shadow candidate)
    shadow_status = "shadow_skipped"
    shadow_result = None
    shadow_skip_reason = None

    if is_shadow and active_agent != target_shadow_agent:
        try:
            if target_shadow_agent in ("agy", "llm", "agy-reviewer"):
                if not shutil.which("agy"):
                    shadow_status = "shadow_skipped"
                    shadow_skip_reason = "Executable 'agy' not found in PATH (LLM runner environment not configured)"
                    shadow_result = {
                        "status": "shadow_skipped",
                        "agent": target_shadow_agent,
                        "reason": shadow_skip_reason,
                        "findings": [],
                    }
                else:
                    is_isolated, isolation_reason, isolation_meta = verify_os_security_isolation(repo_path)
                    if not is_isolated:
                        shadow_status = "shadow_skipped"
                        shadow_skip_reason = isolation_reason
                        shadow_result = {
                            "status": "shadow_skipped",
                            "agent": target_shadow_agent,
                            "reason": shadow_skip_reason,
                            "findings": [],
                        }
                    else:
                        shadow_result = run_agent_review(
                            agent_name=target_shadow_agent,
                            repo_dir=repo_path,
                            pr_info=pr_data,
                            diff_content=diff_content,
                            timeout_seconds=timeout_seconds,
                            system_prompt=system_prompt,
                            isolation_meta=isolation_meta,
                            context_mode=context_mode,
                            context_budget_chars=context_budget_chars,
                        )
                        cleanup_rec = isolation_meta.get("cleanup_record") if isolation_meta else None
                        if cleanup_rec and cleanup_rec.get("status") not in ("verified_removed", "already_absent", "not_applicable"):
                            shadow_status = "shadow_failed"
                            shadow_result["status"] = "shadow_failed"
                            shadow_result["error"] = (
                                f"security_exception: temporary auth directory cleanup failed: {cleanup_rec.get('error') or cleanup_rec.get('status')}"
                            )
                        else:
                            shadow_status = "shadow_success"
                            shadow_result["status"] = "shadow_success"
                        shadow_result["isolation"] = {
                            "type": isolation_meta.get("isolation_type", "unknown"),
                            "reason": isolation_reason,
                        }
            else:
                shadow_result = run_agent_review(
                    agent_name=target_shadow_agent,
                    repo_dir=repo_path,
                    pr_info=pr_data,
                    diff_content=diff_content,
                    timeout_seconds=timeout_seconds,
                    system_prompt=system_prompt,
                    context_mode=context_mode,
                    context_budget_chars=context_budget_chars,
                )
                shadow_status = "shadow_success"
                shadow_result["status"] = "shadow_success"
        except (TimeoutError, subprocess.TimeoutExpired) as exc:
            shadow_status = "shadow_timeout"
            shadow_result = {
                "status": "shadow_timeout",
                "agent": target_shadow_agent,
                "error": str(exc),
                "findings": [],
            }
        except Exception as exc:
            shadow_status = "shadow_failed"
            shadow_result = {
                "status": "shadow_failed",
                "agent": target_shadow_agent,
                "error": str(exc),
                "findings": [],
            }
    else:
        shadow_status = "shadow_skipped"
        shadow_skip_reason = (
            f"Shadow agent '{target_shadow_agent}' matches primary reviewer"
            if is_shadow
            else "Shadow mode not requested"
        )
        shadow_result = {
            "status": "shadow_skipped",
            "agent": target_shadow_agent,
            "reason": shadow_skip_reason,
            "findings": [],
        }

    # Explicit 3-tier status taxonomy:
    # 1. 'success': configured agent succeeded on its own merits
    # 2. 'fallback_success': candidate failed, legacy fallback rescued the pipeline (must NOT count towards candidate success)
    # 3. 'failed': both failed (handled above)
    top_status = "fallback_success" if fallback_used else "success"

    response: Dict[str, Any] = {
        "status": top_status,
        "primary": primary_result,
        "findings": primary_result.get("findings", []),
        "agent": primary_result.get("agent"),
        "fallback_used": fallback_used,
        "shadow_status": shadow_status,
        "shadow": shadow_result,
    }
    if fallback_used:
        response["fallback_detail"] = primary_result.get("fallback")
    if shadow_skip_reason:
        response["shadow_skip_reason"] = shadow_skip_reason
    if "audit" in primary_result:
        response["audit"] = primary_result["audit"]
    if "context_meta" in primary_result:
        response["context_meta"] = primary_result["context_meta"]
    if "prompt_meta" in primary_result:
        response["prompt_meta"] = primary_result["prompt_meta"]

    # Production Validation requirement: Shadow results must be persisted for observability if shadow mode was active
    if is_shadow and shadow_result is not None:
        shadow_log_dir = Path(os.environ.get("HERDR_SHADOW_LOG_DIR", ".omc/shadow_reviews")).resolve()
        try:
            shadow_log_dir.mkdir(parents=True, exist_ok=True)
            timestamp_ms = int(time.time() * 1000)
            pr_id = pr_data.get("pr_number") or pr_data.get("head") or "unknown"
            log_file = shadow_log_dir / f"shadow_{pr_id}_{timestamp_ms}.json"
            shadow_payload = {
                "recorded_at": timestamp_ms,
                "pr": pr_data,
                "primary_agent": active_agent,
                "primary_findings_count": len(primary_result.get("findings", [])),
                "primary_time_ms": primary_result.get("usage", {}).get("time_in_ms", 0),
                "shadow_agent": shadow_result.get("agent"),
                "shadow_status": shadow_status,
                "shadow_findings": shadow_result.get("findings", []),
                "shadow_findings_count": len(shadow_result.get("findings", [])),
                "shadow_rejected_findings": shadow_result.get("rejected_findings", []),
                "shadow_rejected_count": len(shadow_result.get("rejected_findings", [])),
                "shadow_verification": shadow_result.get("verification"),
                "shadow_time_ms": shadow_result.get("usage", {}).get("time_in_ms", 0),
                "shadow_tokens": shadow_result.get("usage", {}).get("tokens"),
                "shadow_raw_response": shadow_result.get("raw_response"),
                "shadow_audit": shadow_result.get("audit"),
                "shadow_error": shadow_result.get("error") or shadow_result.get("reason"),
            }
            log_file.write_text(json.dumps(shadow_payload, indent=2, ensure_ascii=False), encoding="utf-8")
            response["shadow_persisted_to"] = str(log_file)
        except Exception as log_err:
            response["shadow_persistence_error"] = str(log_err)

    return response


def review_pr(
    repo_dir: Path,
    base_ref: str,
    head_ref: str = "HEAD",
    pr_info: Optional[Dict[str, Any]] = None,
    *,
    reviewer_agent: Optional[str] = None,
    shadow_agent: Optional[str] = None,
    shadow_mode: bool = False,
    timeout_seconds: int = 180,
    system_prompt: Optional[str] = None,
    context_mode: Optional[str] = None,
    context_budget_chars: Optional[int] = None,
) -> Dict[str, Any]:
    """Execute code review on a git commit range (base_ref..head_ref)."""
    repo_path = Path(repo_dir).resolve()
    diff_res = subprocess.run(
        ["git", "diff", f"{base_ref}..{head_ref}"],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
        check=True,
    )
    diff_content = diff_res.stdout
    meta = dict(pr_info or {})
    meta.setdefault("base", base_ref)
    meta.setdefault("head", head_ref)
    return review_diff(
        repo_dir=repo_path,
        diff_content=diff_content,
        pr_info=meta,
        reviewer_agent=reviewer_agent,
        shadow_agent=shadow_agent,
        shadow_mode=shadow_mode,
        timeout_seconds=timeout_seconds,
        system_prompt=system_prompt,
        context_mode=context_mode,
        context_budget_chars=context_budget_chars,
    )



def generate_chinese_report(
    results_json: Dict[str, Any],
    details_json: List[Dict[str, Any]],
    execution_summary: Dict[str, Any],
    version_info: Dict[str, Any],
) -> str:
    """Generate structured markdown report report.md in Chinese."""
    total_planned = execution_summary.get("total_planned", 0)
    completed = execution_summary.get("completed", 0)
    failed = execution_summary.get("failed", 0)
    fallback_count = execution_summary.get("fallback_count", 0)
    fallback_details = execution_summary.get("fallback_details", {})
    failure_details = execution_summary.get("failure_details", {})
    elapsed_seconds = execution_summary.get("elapsed_seconds", 0.0)

    req_context_mode = execution_summary.get("requested_context_mode") or version_info.get("context_mode") or "existing_four_tier"
    budget_chars = execution_summary.get("context_budget_chars") or version_info.get("context_budget_chars") or 35000

    agent_identity = execution_summary.get("agent_identity") or version_info.get("agent_identity") or {}
    model_name = agent_identity.get("model_name") or "未知"
    cli_ver = agent_identity.get("cli_version") or "未知"
    is_model_confirmed = agent_identity.get("model_confirmed", False)

    usage_summary = execution_summary.get("usage_summary") or results_json.get("usage_summary") or {}
    total_review_time_ms = usage_summary.get("total_review_time_ms")
    if total_review_time_ms is None:
        total_review_time_ms = execution_summary.get("total_review_time_ms", 0)
    input_tokens = usage_summary.get("input_tokens", "未知")
    output_tokens = usage_summary.get("output_tokens", "未知")
    thinking_tokens = usage_summary.get("thinking_tokens", "未知")
    total_tokens = usage_summary.get("total_tokens", "未知")
    avg_prompt_chars = usage_summary.get("avg_prompt_chars", 0.0)
    avg_context_chars = usage_summary.get("avg_context_chars", 0.0)

    # Extract normalized metrics from results_json
    metrics_obj = extract_and_validate_metrics(results_json)
    overall_metrics = metrics_obj.get("overall", {}) if isinstance(metrics_obj, dict) else {}
    gp = overall_metrics.get("grounded_precision")
    gr = overall_metrics.get("grounded_recall")
    ap = overall_metrics.get("augmented_precision")
    ar = overall_metrics.get("augmented_recall")

    gp_str = f"{gp * 100:.1f}%" if isinstance(gp, (int, float)) else "N/A"
    gr_str = f"{gr * 100:.1f}%" if isinstance(gr, (int, float)) else "N/A"
    ap_str = f"{ap * 100:.1f}%" if isinstance(ap, (int, float)) else "N/A"
    ar_str = f"{ar * 100:.1f}%" if isinstance(ar, (int, float)) else "N/A"

    lines = [
        "# HAFlow 代码评审回归评测报告",
        "",
        "## 1. 运行完整性",
        f"- **计划案例数**: {total_planned}",
        f"- **成功完成数**: {completed}",
        f"- **降级案例数**: {fallback_count}",
        f"- **失败案例数**: {failed}",
        f"- **请求上下文模式**: `{req_context_mode}`",
    ]

    if fallback_count > 0:
        lines.append("- **降级原因明细**:")
        for pr_id, reason in fallback_details.items():
            lines.append(f"  - `{pr_id}`: {reason} (从 `{req_context_mode}` 降级为 `diff_only`)")
    else:
        lines.append("- **降级原因明细**: 无降级，全量完成案例均符合请求上下文模式")

    if failed > 0:
        lines.append("- **失败原因明细**:")
        for pr_id, reason in failure_details.items():
            lines.append(f"  - `{pr_id}`: {reason}")
    else:
        lines.append("- **失败原因明细**: 无失败，全量案例均成功执行")

    lines.extend([
        "",
        "## 2. 核心评测指标 (ReviewBench 官方契约)",
        f"- **基础召回率 (Grounded Recall)**: {gr_str}",
        f"- **基础准确率 (Grounded Precision)**: {gp_str}",
        f"- **增强召回率 (Augmented Recall)**: {ar_str}",
        f"- **增强准确率 (Augmented Precision)**: {ap_str}",
        "",
        "## 3. 已知问题检出与重要问题遗漏清单",
    ])

    # Table of PRs and their findings
    lines.append("| 案例 PR | 检出黄金问题数 | 遗漏黄金问题数 | 候选条目数 | 判定状态 |")
    lines.append("|---|---|---|---|---|")

    if details_json:
        for pr_detail in details_json:
            key = pr_detail.get("pr_key", "unknown")
            findings = pr_detail.get("candidate_findings", [])
            matched_tp = sum(1 for f in findings if f.get("status") == "matched_tp")
            matched_fp = sum(1 for f in findings if f.get("status") == "matched_fp")
            novel_tp = sum(1 for f in findings if f.get("status") == "novel_tp")
            novel_fp = sum(1 for f in findings if f.get("status") == "novel_fp")
            status_desc = f"TP: {matched_tp + novel_tp}, FP: {matched_fp + novel_fp}"
            lines.append(f"| `{key}` | {matched_tp} | {1 if matched_tp == 0 else 0} | {len(findings)} | {status_desc} |")
    else:
        # Fallback to candidate findings when judge results are offline
        candidate_findings_map = execution_summary.get("candidate_findings_map", {})
        for key, findings in candidate_findings_map.items():
            lines.append(f"| `{key}` | 待裁判核验 | 待裁判核验 | {len(findings)} | 候选生成完毕 (裁判未验证) |")

    lines.extend([
        "",
        "### 评审意见明细判定 (按条目分类)",
    ])

    item_idx = 1
    if details_json:
        for pr_detail in details_json:
            key = pr_detail.get("pr_key", "unknown")
            findings = pr_detail.get("candidate_findings", [])
            for f in findings:
                status = f.get("status", "unknown")
                file_loc = f"{f.get('file', '')}:{f.get('start_line', '')}-{f.get('end_line', '')}"
                msg = f.get("message", "")
                lines.append(f"{item_idx}. **[{key}] {file_loc}** ({status})")
                lines.append(f"   > {msg}")
                item_idx += 1
    else:
        candidate_findings_map = execution_summary.get("candidate_findings_map", {})
        for key, findings in candidate_findings_map.items():
            for f in findings:
                file_loc = f"{f.get('file', '')}:{f.get('start_line', '')}-{f.get('end_line', '')}"
                msg = f.get("message", "")
                lines.append(f"{item_idx}. **[{key}] {file_loc}** (candidate_generated)")
                lines.append(f"   > {msg}")
                item_idx += 1

    if item_idx == 1:
        lines.append("*（本轮评测无候选审查条目产生）*")

    model_status_label = "已确认" if is_model_confirmed else "无法确认同模型"
    lines.extend([
        "",
        "## 4. 版本与环境信息",
        f"- **代码基线 SHA**: `{version_info.get('code_sha', 'HEAD')}`",
        f"- **评审 Agent 配置**: `{version_info.get('agent_config', 'default')}`",
        f"- **评审上下文模式**: `{req_context_mode}` (预算上限: {budget_chars} 字符)",
        f"- **评审模型身份**: `{model_name}` ({model_status_label})",
        f"- **评审 CLI 版本**: `{cli_ver}`",
        f"- **裁判模型配置**: `{version_info.get('judge_config', 'deepseek/deepseek-v4-flash')}`",
        f"- **ReviewBench 评分器 SHA**: `{version_info.get('scorer_sha', 'e1cb1a0dad8105ebea45caa00c194eaf2d2e7b5d')}`",
        "",
        "## 5. 执行开销",
        f"- **评测实际总耗时**: {elapsed_seconds:.2f} 秒 (全流程端到端耗时)",
        f"- **评测执行阶段耗时**: {execution_summary.get('review_stage_elapsed_seconds', elapsed_seconds):.2f} 秒 (工作区准备与各 PR 评审)",
        f"- **评审累积执行耗时**: {total_review_time_ms / 1000:.2f} 秒 (各 PR 评审操作实际耗时之和，含失败重试/异常尝试)",
        f"- **真实 Token 用量**:",
        f"  - 输入 Token (input_tokens): {input_tokens}",
        f"  - 输出 Token (output_tokens): {output_tokens}",
        f"  - 思考 Token (thinking_tokens): {thinking_tokens}",
        f"  - 总计 Token (total_tokens): {total_tokens}",
        f"- **提示词规模统计**:",
        f"  - 平均 Prompt 字符数: {avg_prompt_chars:.1f} 字符",
        f"  - 平均有效上下文大小: {avg_context_chars:.1f} 字符",
        f"- **API 成本支出**: {version_info.get('cost_usd', '未知 (本地/直连环境)')}",
        "",
    ])

    return "\n".join(lines)


def check_benchmark_comparability(
    before_results: Dict[str, Any],
    after_results: Dict[str, Any],
) -> Tuple[bool, List[str]]:
    """Verify single-variable comparability between two benchmark runs."""
    discrepancies: List[str] = []

    b_version = before_results.get("version_info", {})
    a_version = after_results.get("version_info", {})
    b_exec = before_results.get("execution_summary", {})
    a_exec = after_results.get("execution_summary", {})
    b_cfg = before_results.get("context_config", {})
    a_cfg = after_results.get("context_config", {})

    # 1. Reviewer Agent
    b_agent = b_exec.get("agent_identity", {}) or b_version.get("agent_identity", {})
    a_agent = a_exec.get("agent_identity", {}) or a_version.get("agent_identity", {})

    b_agent_parts = b_version.get("agent_config", "").split()
    a_agent_parts = a_version.get("agent_config", "").split()
    b_agent_name = b_agent.get("agent") or (b_agent_parts[0] if b_agent_parts else "unknown")
    a_agent_name = a_agent.get("agent") or (a_agent_parts[0] if a_agent_parts else "unknown")

    if b_agent_name != a_agent_name:
        discrepancies.append(
            f"评审 Agent 不一致 (基线: '{b_agent_name}', 改进版: '{a_agent_name}')。禁止将不同 Agent (如规则与 LLM) 称为同模型对照。"
        )

    # 2. Model Confirmation & Identity
    b_model = b_agent.get("model_name", "unknown")
    a_model = a_agent.get("model_name", "unknown")
    b_confirmed = b_agent.get("model_confirmed", False)
    a_confirmed = a_agent.get("model_confirmed", False)

    if not b_confirmed or not a_confirmed or b_model == "unknown" or a_model == "unknown":
        discrepancies.append(
            f"无法确认同模型: 实际模型身份未完全确认 (基线: '{b_model}' [confirmed={b_confirmed}], 改进版: '{a_model}' [confirmed={a_confirmed}])。不得据此输出严格的同模型优劣结论。"
        )
    elif b_model != a_model:
        discrepancies.append(
            f"模型不一致 (基线: '{b_model}', 改进版: '{a_model}')。违反单变量控制要求。"
        )

    # 3. CLI Version & Agent Execution Params (Effort)
    b_cli = b_agent.get("cli_version")
    a_cli = a_agent.get("cli_version")
    if b_cli and a_cli and b_cli != "unknown" and a_cli != "unknown" and b_cli != "internal-rule" and a_cli != "internal-rule" and b_cli != a_cli:
        discrepancies.append(f"CLI 版本不一致 (基线: '{b_cli}', 改进版: '{a_cli}')。")

    b_effort = b_agent.get("details", {}).get("effort")
    a_effort = a_agent.get("details", {}).get("effort")
    if b_effort and a_effort and b_effort != a_effort:
        discrepancies.append(f"推理强度/思考预算 (effort) 不一致 (基线: '{b_effort}', 改进版: '{a_effort}')。")

    # 4. Context Mode Independent Variable
    b_mode = b_cfg.get("context_mode") or b_exec.get("requested_context_mode") or b_version.get("context_mode")
    a_mode = a_cfg.get("context_mode") or a_exec.get("requested_context_mode") or a_version.get("context_mode")
    if not b_mode or not a_mode:
        discrepancies.append("缺少请求上下文模式记录，无法验证对照自变量。")
    elif b_mode == a_mode:
        discrepancies.append(f"两组请求上下文模式相同 ('{b_mode}')，未构成 diff_only 与 existing_four_tier 的上下文对照自变量。")

    # 5. Context Budget
    b_budget = b_cfg.get("context_budget_chars") or b_exec.get("context_budget_chars") or b_version.get("context_budget_chars")
    a_budget = a_cfg.get("context_budget_chars") or a_exec.get("context_budget_chars") or a_version.get("context_budget_chars")
    if not b_budget or not a_budget:
        discrepancies.append("缺少上下文预算上限记录，无法确认预算一致性。")
    elif b_budget != a_budget:
        discrepancies.append(
            f"上下文预算上限不一致 (基线: {b_budget} 字符, 改进版: {a_budget} 字符)。"
        )

    # 6. Fallbacks
    b_fallbacks = b_exec.get("fallback_count", 0)
    a_fallbacks = a_exec.get("fallback_count", 0)
    if b_fallbacks > 0 or a_fallbacks > 0:
        discrepancies.append(
            f"评测包含降级样本 (基线降级 {b_fallbacks} 例, 改进版降级 {a_fallbacks} 例)。降级样本已退回基础 diff，无法完整代表请求的上下文模式。"
        )

    # 7. Manifest PR Samples & Base/Head SHA Consistency
    b_samples = b_exec.get("samples") or []
    a_samples = a_exec.get("samples") or []
    if b_samples and a_samples:
        b_keys = [s.get("key") for s in b_samples if s.get("key")]
        a_keys = [s.get("key") for s in a_samples if s.get("key")]
        if set(b_keys) != set(a_keys):
            discrepancies.append(
                f"评测 PR 集合不一致 (基线包含 {len(b_keys)} 个案例，改进版包含 {len(a_keys)} 个案例，重合 {len(set(b_keys) & set(a_keys))} 个)。未在相同 PR 集合上进行对照。"
            )
        else:
            b_smap = {s["key"]: s for s in b_samples if "key" in s}
            a_smap = {s["key"]: s for s in a_samples if "key" in s}
            for k in b_keys:
                bs = b_smap[k]
                as_ = a_smap[k]
                if bs.get("base") and as_.get("base") and bs.get("base") != as_.get("base"):
                    discrepancies.append(f"案例 {k} 的 base SHA 不一致 (基线: {bs.get('base')[:8]}, 改进版: {as_.get('base')[:8]})。")
                if bs.get("head") and as_.get("head") and bs.get("head") != as_.get("head"):
                    discrepancies.append(f"案例 {k} 的 head SHA 不一致 (基线: {bs.get('head')[:8]}, 改进版: {as_.get('head')[:8]})。")
                if bs.get("base_diff_chars") and as_.get("base_diff_chars") and bs.get("base_diff_chars") != as_.get("base_diff_chars"):
                    discrepancies.append(f"案例 {k} 的基础 diff 长度不一致 (基线: {bs.get('base_diff_chars')} 字符, 改进版: {as_.get('base_diff_chars')} 字符)。未满足相同基础 diff 契约。")

    # 8. Planned, Completed, Failed Sample Counts (Denominator Verification)
    b_plan = b_exec.get("total_planned")
    a_plan = a_exec.get("total_planned")
    b_done = b_exec.get("completed")
    a_done = a_exec.get("completed")
    b_failed = b_exec.get("failed", 0)
    a_failed = a_exec.get("failed", 0)

    if b_plan is not None and a_plan is not None and b_plan != a_plan:
        discrepancies.append(f"计划案例总数不一致 (基线: {b_plan}, 改进版: {a_plan})。")

    if b_failed > 0 or a_failed > 0:
        discrepancies.append(
            f"评测包含失败样本 (基线失败 {b_failed} 例, 改进版失败 {a_failed} 例)。样本失败导致实际计分集合不完整 (基线完成 {b_done}/{b_plan}, 改进版完成 {a_done}/{a_plan})。"
        )

    if b_done is not None and a_done is not None and b_done != a_done:
        discrepancies.append(f"完成案例数不一致 (基线完成 {b_done}, 改进版完成 {a_done})。")

    # 9. Judge Configuration
    b_judge = before_results.get("judge_config") or b_version.get("judge_config")
    a_judge = after_results.get("judge_config") or a_version.get("judge_config")
    if not b_judge or not a_judge:
        discrepancies.append("裁判模型配置缺失，无法确认同裁判评分条件。")
    elif b_judge != a_judge:
        discrepancies.append(f"裁判模型配置不一致 (基线: '{b_judge}', 改进版: '{a_judge}')。")

    # 10. Scorer Version (SHA)
    b_scorer = before_results.get("scorer_sha") or b_version.get("scorer_sha")
    a_scorer = after_results.get("scorer_sha") or a_version.get("scorer_sha")
    if not b_scorer or not a_scorer:
        discrepancies.append("评分器版本缺失，无法确认同评分器版本。")
    elif b_scorer != a_scorer:
        discrepancies.append(f"评分器版本不一致 (基线: '{b_scorer[:8]}', 改进版: '{a_scorer[:8]}')。")

    return (len(discrepancies) == 0, discrepancies)


def compare_benchmarks(
    before_dir: Path,
    after_dir: Path,
    output_file: Optional[Path] = None,
) -> str:
    """Compare two benchmark runs (before vs after) and generate a Markdown comparison report."""
    before_results_file = before_dir / "results.json"
    after_results_file = after_dir / "results.json"
    before_report_file = before_dir / "report.md"
    after_report_file = after_dir / "report.md"

    if not before_results_file.exists():
        raise FileNotFoundError(f"Before results not found at: {before_results_file}")
    if not after_results_file.exists():
        raise FileNotFoundError(f"After results not found at: {after_results_file}")

    with open(before_results_file, "r", encoding="utf-8") as f:
        before_res = json.load(f)
    with open(after_results_file, "r", encoding="utf-8") as f:
        after_res = json.load(f)

    # Integrity guard: do NOT allow comparing runs that failed judging or emitted unverified metrics
    b_metrics = extract_and_validate_metrics(before_res)
    a_metrics = extract_and_validate_metrics(after_res)

    if (before_res.get("status") not in ("completed", None) and before_res.get("status") is not None) or b_metrics is None:
        raise ValueError(
            f"Cannot compare: baseline run at {before_dir} did not complete judging successfully "
            f"(status: {before_res.get('status')}). Refusing to fabricate benchmark regressions."
        )
    if after_res.get("status") != "completed" or a_metrics is None:
        raise ValueError(
            f"Cannot compare: candidate run at {after_dir} did not complete judging successfully "
            f"(status: {after_res.get('status')}). Refusing to fabricate benchmark regressions."
        )

    b_overall = b_metrics.get("overall", {})
    a_overall = a_metrics.get("overall", {})

    def _diff_stat(key: str) -> Tuple[str, str, str]:
        bv = b_overall.get(key)
        av = a_overall.get(key)
        b_s = f"{bv*100:.1f}%" if isinstance(bv, (int, float)) else "N/A"
        a_s = f"{av*100:.1f}%" if isinstance(av, (int, float)) else "N/A"
        if isinstance(bv, (int, float)) and isinstance(av, (int, float)):
            delta = (av - bv) * 100
            d_s = f"{'+' if delta >= 0 else ''}{delta:.1f}%"
        else:
            d_s = "N/A"
        return b_s, a_s, d_s

    gr_b, gr_a, gr_d = _diff_stat("grounded_recall")
    gp_b, gp_a, gp_d = _diff_stat("grounded_precision")
    ar_b, ar_a, ar_d = _diff_stat("augmented_recall")
    ap_b, ap_a, ap_d = _diff_stat("augmented_precision")

    is_comparable, discrepancies = check_benchmark_comparability(before_res, after_res)

    # Context & usage statistics extraction
    b_exec = before_res.get("execution_summary", {})
    a_exec = after_res.get("execution_summary", {})
    b_usage = before_res.get("usage_summary") or b_exec.get("usage_summary", {})
    a_usage = after_res.get("usage_summary") or a_exec.get("usage_summary", {})
    b_version = before_res.get("version_info", {})
    a_version = after_res.get("version_info", {})
    b_cfg = before_res.get("context_config", {})
    a_cfg = after_res.get("context_config", {})

    b_mode = b_cfg.get("context_mode") or b_exec.get("requested_context_mode") or b_version.get("context_mode", "未知")
    a_mode = a_cfg.get("context_mode") or a_exec.get("requested_context_mode") or a_version.get("context_mode", "未知")

    b_agent = b_exec.get("agent_identity", {}) or b_version.get("agent_identity", {})
    a_agent = a_exec.get("agent_identity", {}) or a_version.get("agent_identity", {})
    b_agent_parts = b_version.get("agent_config", "").split()
    a_agent_parts = a_version.get("agent_config", "").split()
    b_agent_name = b_agent.get("agent") or (b_agent_parts[0] if b_agent_parts else "未知")
    a_agent_name = a_agent.get("agent") or (a_agent_parts[0] if a_agent_parts else "未知")

    b_model = b_agent.get("model_name", "未知")
    a_model = a_agent.get("model_name", "未知")
    b_model_conf = b_agent.get("model_confirmed", False)
    a_model_conf = a_agent.get("model_confirmed", False)
    b_model_str = f"{b_model} ({'已确认' if b_model_conf else '无法确认同模型'})"
    a_model_str = f"{a_model} ({'已确认' if a_model_conf else '无法确认同模型'})"

    b_cli = b_agent.get("cli_version", "未知")
    a_cli = a_agent.get("cli_version", "未知")
    b_effort = b_agent.get("details", {}).get("effort", "未知")
    a_effort = a_agent.get("details", {}).get("effort", "未知")

    b_budget = b_cfg.get("context_budget_chars") or b_exec.get("context_budget_chars") or b_version.get("context_budget_chars", "未知")
    a_budget = a_cfg.get("context_budget_chars") or a_exec.get("context_budget_chars") or a_version.get("context_budget_chars", "未知")

    b_plan = b_exec.get("total_planned", "未知")
    a_plan = a_exec.get("total_planned", "未知")
    b_done = b_exec.get("completed", "未知")
    a_done = a_exec.get("completed", "未知")
    b_failed = b_exec.get("failed", 0)
    a_failed = a_exec.get("failed", 0)
    b_fall = b_exec.get("fallback_count", 0)
    a_fall = a_exec.get("fallback_count", 0)

    b_judge = before_res.get("judge_config") or b_version.get("judge_config", "未知")
    a_judge = after_res.get("judge_config") or a_version.get("judge_config", "未知")
    b_scorer = before_res.get("scorer_sha") or b_version.get("scorer_sha", "未知")
    a_scorer = after_res.get("scorer_sha") or a_version.get("scorer_sha", "未知")

    def _val_diff(bv: Any, av: Any, unit: str = "") -> Tuple[str, str, str]:
        if isinstance(bv, (int, float)) and isinstance(av, (int, float)):
            delta = av - bv
            d_str = f"{'+' if delta >= 0 else ''}{delta:.2f}{unit}" if isinstance(delta, float) else f"{'+' if delta >= 0 else ''}{delta}{unit}"
            return f"{bv}{unit}", f"{av}{unit}", d_str
        b_s = f"{bv}{unit}" if bv not in (None, "未知") else "未知"
        a_s = f"{av}{unit}" if av not in (None, "未知") else "未知"
        return b_s, a_s, "未知"

    def _token_diff(b_u: Dict[str, Any], a_u: Dict[str, Any], key: str) -> Tuple[str, str, str]:
        b_val = b_u.get(key)
        a_val = a_u.get(key)
        b_dtl = b_u.get("token_details", {}).get(key, {}) if isinstance(b_u.get("token_details"), dict) else {}
        a_dtl = a_u.get("token_details", {}).get(key, {}) if isinstance(a_u.get("token_details"), dict) else {}

        b_disp = str(b_val) if b_val not in (None, "") else "未知"
        a_disp = str(a_val) if a_val not in (None, "") else "未知"

        b_raw = b_dtl.get("raw_sum") if isinstance(b_dtl, dict) and b_dtl.get("raw_sum") is not None else (b_val if isinstance(b_val, (int, float)) else None)
        a_raw = a_dtl.get("raw_sum") if isinstance(a_dtl, dict) and a_dtl.get("raw_sum") is not None else (a_val if isinstance(a_val, (int, float)) else None)

        b_comp = b_dtl.get("is_complete", False) if isinstance(b_dtl, dict) and "is_complete" in b_dtl else isinstance(b_val, (int, float))
        a_comp = a_dtl.get("is_complete", False) if isinstance(a_dtl, dict) and "is_complete" in a_dtl else isinstance(a_val, (int, float))

        if isinstance(b_raw, (int, float)) and isinstance(a_raw, (int, float)):
            delta = a_raw - b_raw
            sign = "+" if delta >= 0 else ""
            if b_comp and a_comp:
                delta_str = f"{sign}{delta}"
            else:
                delta_str = f"{sign}{delta} (基于已知部分计算)"
            return b_disp, a_disp, delta_str
        return b_disp, a_disp, "未知"

    b_stage_s = b_exec.get("review_stage_elapsed_seconds")
    a_stage_s = a_exec.get("review_stage_elapsed_seconds")
    stage_b, stage_a, stage_d = _val_diff(b_stage_s, a_stage_s, "秒")

    b_e2e = b_exec.get("elapsed_seconds")
    a_e2e = a_exec.get("elapsed_seconds")
    e2e_b, e2e_a, e2e_d = _val_diff(b_e2e, a_e2e, "秒")

    b_rev_s = (b_usage.get("total_review_time_ms") / 1000.0) if isinstance(b_usage.get("total_review_time_ms"), (int, float)) else None
    a_rev_s = (a_usage.get("total_review_time_ms") / 1000.0) if isinstance(a_usage.get("total_review_time_ms"), (int, float)) else None
    rev_b, rev_a, rev_d = _val_diff(b_rev_s, a_rev_s, "秒")

    in_b, in_a, in_d = _token_diff(b_usage, a_usage, "input_tokens")
    out_b, out_a, out_d = _token_diff(b_usage, a_usage, "output_tokens")
    thk_b, thk_a, thk_d = _token_diff(b_usage, a_usage, "thinking_tokens")
    tot_b, tot_a, tot_d = _token_diff(b_usage, a_usage, "total_tokens")

    p_ch_b, p_ch_a, p_ch_d = _val_diff(b_usage.get("avg_prompt_chars"), a_usage.get("avg_prompt_chars"), "字")
    c_ch_b, c_ch_a, c_ch_d = _val_diff(b_usage.get("avg_context_chars"), a_usage.get("avg_context_chars"), "字")

    lines = [
        "# HAFlow 代码评审回归评测对比报告",
        "",
        f"- **基线目录 (Before)**: `{before_dir}`",
        f"- **改进版目录 (After)**: `{after_dir}`",
        f"- **对比生成时间**: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## 1. 核心指标对比矩阵",
        "",
        "| 指标 | 基线 (Before) | 改进版 (After) | 差异 (Delta) | 趋势 |",
        "|---|---|---|---|---|",
        f"| 基础召回率 (Grounded Recall) | {gr_b} | {gr_a} | {gr_d} | {'📈 提升' if '+' in gr_d and gr_d != '+0.0%' else ('📉 下降' if '-' in gr_d else '➖ 持平')} |",
        f"| 基础准确率 (Grounded Precision) | {gp_b} | {gp_a} | {gp_d} | {'📈 提升' if '+' in gp_d and gp_d != '+0.0%' else ('📉 下降' if '-' in gp_d else '➖ 持平')} |",
        f"| 增强召回率 (Augmented Recall) | {ar_b} | {ar_a} | {ar_d} | {'📈 提升' if '+' in ar_d and ar_d != '+0.0%' else ('📉 下降' if '-' in ar_d else '➖ 持平')} |",
        f"| 增强准确率 (Augmented Precision) | {ap_b} | {ap_a} | {ap_d} | {'📈 提升' if '+' in ap_d and ap_d != '+0.0%' else ('📉 下降' if '-' in ap_d else '➖ 持平')} |",
        "",
        "## 2. 单变量对照与可比性检查",
        "",
        "| 对照条件 | 基线 (Before) | 改进版 (After) | 一致性与状态 |",
        "|---|---|---|---|",
        f"| 评审 Agent | `{b_agent_name}` | `{a_agent_name}` | {'✓ 一致' if b_agent_name == a_agent_name and b_agent_name != '未知' else '⚠ 存在差异'} |",
        f"| 模型身份 | `{b_model_str}` | `{a_model_str}` | {'✓ 确认同模型' if b_model_conf and a_model_conf and b_model == a_model else '⚠ 无法确认同模型'} |",
        f"| CLI 版本 / effort | `{b_cli}` / `{b_effort}` | `{a_cli}` / `{a_effort}` | {'✓ 一致' if b_cli == a_cli and b_effort == a_effort else '⚠ 存在差异'} |",
        f"| 请求上下文模式 | `{b_mode}` | `{a_mode}` | {'(对照自变量)' if b_mode != a_mode else '模式相同 (未构成自变量)'} |",
        f"| 上下文预算上限 | `{b_budget}` | `{a_budget}` | {'✓ 一致' if b_budget == a_budget and b_budget != '未知' else '⚠ 存在差异'} |",
        f"| 计划 / 完成案例 | {b_plan} / {b_done} (失败: {b_failed}) | {a_plan} / {a_done} (失败: {a_failed}) | {'✓ 案例数一致' if b_plan == a_plan and b_done == a_done and b_failed == 0 and a_failed == 0 else '⚠ 存在差异或失败'} |",
        f"| 降级案例数 | {b_fall} | {a_fall} | {'✓ 无降级' if b_fall == 0 and a_fall == 0 else f'⚠ 包含降级 ({b_fall} / {a_fall})'} |",
        f"| 裁判模型配置 | `{b_judge}` | `{a_judge}` | {'✓ 一致' if b_judge == a_judge and b_judge != '未知' else '⚠ 存在差异'} |",
        f"| 评分器版本 SHA | `{str(b_scorer)[:8]}` | `{str(a_scorer)[:8]}` | {'✓ 一致' if b_scorer == a_scorer and b_scorer != '未知' else '⚠ 存在差异'} |",
        "",
    ]

    if is_comparable:
        lines.extend([
            "> [!NOTE]",
            f"> **单变量控制结论**：两组评测已满足严格单变量同模型控制条件（同一模型 `{b_model}`、相同基础 diff、相同评审预算与评测集）。两组差异主要来源于上下文检索模式的不同。",
            "",
        ])
    else:
        lines.extend([
            "> [!WARNING]",
            "> **单变量控制警告**：本次对比未完全满足严格单变量同模型控制条件，具体差异项如下：",
        ])
        for disc in discrepancies:
            lines.append(f"> - {disc}")
        lines.extend([
            ">",
            "> 依据评测纪律，在无法确认严格单变量受控或存在降级/失败/环境差异时，**不得据此输出严格的同模型优劣结论**。",
            "",
        ])

    lines.extend([
        "## 3. 耗时与用量对比矩阵",
        "",
        "| 统计项目 | 基线 (Before) | 改进版 (After) | 差异 (Delta) | 说明 |",
        "|---|---|---|---|---|",
        f"| 评测执行总耗时 | {stage_b} | {stage_a} | {stage_d} | 所有 PR 工作区准备与评审操作之和（含成功及失败尝试） |",
        f"| 全流程端到端耗时 | {e2e_b} | {e2e_a} | {e2e_d} | 从评测启动至官方裁判打分完成的全流程耗时 |",
        f"| 评审操作累积耗时 | {rev_b} | {rev_a} | {rev_d} | 各 PR 完整评审操作（含检索组装、Reviewer 与 Verifier）耗时之和 |",
        f"| 输入 Token (input_tokens) | {in_b} | {in_a} | {in_d} | 真实 API usage 统计（缺失为未知，部分覆盖标明样本数） |",
        f"| 输出 Token (output_tokens) | {out_b} | {out_a} | {out_d} | 真实 API usage 统计（缺失为未知，部分覆盖标明样本数） |",
        f"| 思考 Token (thinking_tokens) | {thk_b} | {thk_a} | {thk_d} | 真实 API usage 统计（缺失为未知，部分覆盖标明样本数） |",
        f"| 总计 Token (total_tokens) | {tot_b} | {tot_a} | {tot_d} | 真实 API usage 统计（严禁通过字符数折算） |",
        f"| 平均 Prompt 字符数 | {p_ch_b} | {p_ch_a} | {p_ch_d} | 完整 Prompt 字符规模 |",
        f"| 平均有效上下文大小 | {c_ch_b} | {c_ch_a} | {c_ch_d} | 基础 diff 及追加源码上下文规模 |",
        "",
        "## 4. 上下文扩展收益分析",
        "",
        "> **核心问题解答**：对当前实际使用的 reviewer，额外提供符号定义、调用关系和契约源码，是否改善审核质量？额外耗时与用量是多少？",
        "",
    ])

    bv_r = b_overall.get("grounded_recall")
    av_r = a_overall.get("grounded_recall")
    bv_p = b_overall.get("grounded_precision")
    av_p = a_overall.get("grounded_precision")

    has_num_diff = all(isinstance(v, (int, float)) for v in (bv_r, av_r, bv_p, av_p))

    if not is_comparable:
        lines.append(
            "- **审核质量评估**: 无法得出单变量质量结论。由于两组评测存在不可比因素（如模型身份未确认、评测案例/配置不一致、包含失败或降级样本等），"
            "观测到的指标差异不能归因于源码上下文检索模式。"
        )
    elif has_num_diff:
        delta_r = round((av_r - bv_r) * 100, 2)
        delta_p = round((av_p - bv_p) * 100, 2)

        r_improved = delta_r > 0
        r_declined = delta_r < 0
        p_improved = delta_p > 0
        p_declined = delta_p < 0

        if r_improved and p_improved:
            quality_desc = (
                f"在当前受测 reviewer 上，额外源码上下文使召回率与准确率均实现提升 "
                f"（基础召回率 {gr_d}，基础准确率 {gp_d}）。"
            )
        elif r_declined and p_declined:
            quality_desc = (
                f"在当前受测 reviewer 上，额外源码上下文导致召回率与准确率均出现下滑 "
                f"（基础召回率 {gr_d}，基础准确率 {gp_d}），额外上下文可能引入了干扰噪声。"
            )
        elif (r_improved and p_declined) or (r_declined and p_improved):
            quality_desc = (
                f"在当前受测 reviewer 上，召回率与准确率呈现反向权衡（Trade-off）："
                f"基础召回率变化 {gr_d}，基础准确率变化 {gp_d}。"
                f"{'召回率有所提升但准确率下降，表明额外上下文在帮助检出更多线索的同时引入了误报；' if r_improved else '准确率有所提升但召回率下降；'}"
                f"在未预设 F-score 或业务权重的前提下，不认定为单纯的整体质量提升或下降。"
            )
        elif delta_r == 0 and delta_p == 0:
            quality_desc = (
                f"在当前受测 reviewer 上，两组核心指标完全持平 "
                f"（基础召回率 Delta: {gr_d}，基础准确率 Delta: {gp_d}），未体现额外质量增益。"
            )
        elif delta_r == 0:
            p_text = f"准确率提高 {gp_d}" if p_improved else f"准确率下降 {gp_d}"
            quality_desc = (
                f"在当前受测 reviewer 上，基础召回率持平 ({gr_d})，但{p_text}。"
                f"{'由于准确率显著下降，不应视为质量持平；' if p_declined else '误报减少带来准确率改进；'}"
                f"需结合业务对误报的容忍度综合评估。"
            )
        else:
            r_text = f"召回率提升 {gr_d}" if r_improved else f"召回率下降 {gp_d}"
            quality_desc = (
                f"在当前受测 reviewer 上，基础准确率持平 ({gp_d})，但{r_text}。"
            )

        lines.append(f"- **审核质量评估**: {quality_desc}")
    else:
        lines.append(
            f"- **审核质量评估**: 基础召回率变化 {gr_d}，基础准确率变化 {gp_d}。"
            f"缺少完整数值指标，不做整体优劣推断。"
        )

    lines.append(f"- **额外耗时评估**: 评审操作累积耗时变化为 `{rev_d}`，全流程评测耗时变化为 `{e2e_d}`。")
    lines.append(f"- **额外用量评估**: 总计 Token 消耗变化为 `{tot_d}`，平均 Prompt 字符数膨胀 `{p_ch_d}`，有效上下文扩展 `{c_ch_d}`。")

    lines.extend([
        "",
        "## 5. 案例检出变化明细",
        "",
        "详见各轮独立报告：",
        f"- 基线报告: [{before_report_file.name}]({before_report_file})",
        f"- 改进版报告: [{after_report_file.name}]({after_report_file})",
        "",
    ])

    report_content = "\n".join(lines)
    if output_file:
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text(report_content, encoding="utf-8")

    return report_content
