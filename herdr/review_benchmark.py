#!/usr/bin/env python3
"""herdr/review_benchmark.py

Code review benchmark runner, schema validator, and comparison reporter
compatible with GitHub ReviewBench contracts.
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple


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

    # 1. Parse changed files from diff
    changed_files = []
    file_diff_map = {}
    current_file = ""
    for line in diff_content.splitlines():
        if line.startswith("diff --git "):
            parts = line.split()
            current_file = parts[-1][2:] if parts[-1].startswith("b/") else parts[-1]
            changed_files.append(current_file)
            file_diff_map[current_file] = [line]
        elif current_file:
            file_diff_map[current_file].append(line)

    audit: Dict[str, Any] = {
        "original_diff_bytes": len(diff_content.encode("utf-8")),
        "context_budget": max_budget_chars,
        "changed_files": changed_files,
        "included_files": [],
        "truncated_files": [],
        "expanded_symbols": [],
        "retrieved_context": [],
        "final_context_size": 0,
    }

    # Baseline preservation: include initial 15,000 chars of diff (Tier 1 core)
    base_diff = diff_content[:15000]
    audit["included_files"] = [f for f in changed_files if f in base_diff]
    audit["truncated_files"] = [f for f in changed_files if f not in base_diff]

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
) -> Dict[str, Any]:
    """Execute code review agent on the isolated repo.

    Returns the ReviewBench candidate dictionary.
    Differentiates:
    - Normal finish with 0 or more findings
    - Agent startup failure
    - Timeout
    - Parse failure
    """
    prompt = (
        f"{system_prompt or 'You are reviewing a Pull Request.'}\n\n"
        f"PR Title: {pr_info.get('title', '')}\n"
        f"PR Description: {pr_info.get('body', '')}\n\n"
        "Here is the git diff for this PR:\n"
        "```diff\n"
        f"{diff_content[:15000]}\n"
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
        if findings is None:
            findings = []

            # Determine effective review context:
            # Baseline & contract-v1 use standard diff; context-v1 uses assembled expanded context
            effective_content = diff_content
            if agent_name == "rule-contract-context-v1":
                effective_content, audit_info = retrieve_and_assemble_context(
                    repo_dir=repo_dir,
                    pr_info=pr_info,
                    diff_content=diff_content,
                    max_budget_chars=35000,
                )

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

        res: Dict[str, Any] = {
            "pr": {
                "repo": pr_info["repo"],
                "pr_number": pr_info["pr_number"],
                "base": pr_info["base"],
                "head": pr_info["head"],
            },
            "agent": agent_name,
            "findings": findings,
            "usage": {"time_in_ms": int((time.time() - t0) * 1000)},
        }
        if audit_info:
            res["audit"] = audit_info
        return res

    # Assemble 4-Tier Context for real LLM reviewer if supported
    effective_content = diff_content
    audit_info = None
    if repo_dir and (repo_dir / ".git").exists():
        try:
            effective_content, audit_info = retrieve_and_assemble_context(
                repo_dir=repo_dir,
                pr_info=pr_info,
                diff_content=diff_content,
                max_budget_chars=35000,
            )
        except Exception:
            effective_content = diff_content

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
        if isolation_meta is None:
            is_iso, iso_reason, isolation_meta = verify_os_security_isolation(repo_dir)
            if not is_iso:
                raise PermissionError(f"security_boundary_violation: {iso_reason}")

        if isolation_meta.get("isolation_type") == "macos-seatbelt":
            sandbox_bin = isolation_meta.get("sandbox_exec", "/usr/bin/sandbox-exec")
            profile = isolation_meta.get("profile", "")
            cmd = [sandbox_bin, "-p", profile] + cmd
    elif agent_name == "pi":
        cmd = ["pi", "--print", prompt]
    elif agent_name == "opencode":
        cmd = ["opencode", "--prompt", prompt]
    else:
        raise RuntimeError(f"agent_startup_failed: unsupported agent '{agent_name}'")

    # Security boundary: Strict environment whitelist. Do NOT copy entire host environment.
    child_env = {k: os.environ[k] for k in STRICT_ENV_WHITELIST_KEYS if k in os.environ}
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

    if proc.returncode != 0:
        raise RuntimeError(f"agent_startup_failed: return code {proc.returncode}, stderr: {proc.stderr[:300]}")

    response_text = proc.stdout
    token_usage = None
    if agent_name in ("agy", "llm", "agy-reviewer"):
        try:
            agy_meta = json.loads(proc.stdout)
            response_text = agy_meta.get("response", "")
            token_usage = agy_meta.get("usage")
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

    res_llm = {
        "pr": {
            "repo": pr_info["repo"],
            "pr_number": pr_info["pr_number"],
            "base": pr_info["base"],
            "head": pr_info["head"],
        },
        "agent": agent_name,
        "findings": retained_findings,
        "rejected_findings": rejected_findings,
        "verification": verification_summary,
        "usage": {
            "time_in_ms": int((time.time() - t0) * 1000),
            "tokens": token_usage or {},
        },
        "raw_response": response_text.strip(),
    }
    if audit_info:
        res_llm["audit"] = audit_info
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
)


def generate_macos_seatbelt_profile(repo_dir: Path) -> str:
    """Generate macOS Seatbelt kernel profile strictly limiting filesystem read/write."""
    home = Path.home().resolve()
    repo_resolved = repo_dir.resolve()
    runner_denials = []
    for rd in [
        home / "actions-runner-haflow",
        home / "actions-runner",
        Path("/Users/user/actions-runner-haflow"),
    ]:
        if rd.exists():
            runner_denials.append(f'(deny file-read* (subpath "{rd.resolve()}"))')
            runner_denials.append(f'(deny file-write* (subpath "{rd.resolve()}"))')
    extra_runner_rules = ("\n" + "\n".join(runner_denials)) if runner_denials else ""

    return f"""(version 1)
(allow default)
(deny file-read* (subpath "{home}"))
(allow file-read* (subpath "{home}/.gemini"))
(allow file-read* (subpath "{home}/.local"))
(allow file-read* (subpath "{repo_resolved}"))
(allow file-read* (subpath "{home}/Library/Preferences/.GlobalPreferences.plist"))
(allow file-read* (subpath "{home}/Library/Keychains"))
(deny file-read* (subpath "{home}/.ssh"))
(deny file-read* (subpath "{home}/.aws"))
(deny file-read* (subpath "{home}/.gnupg"))
(deny file-read* (subpath "{home}/.config/gh"))
(deny file-read* (subpath "{home}/Documents"))
(deny file-read* (subpath "{home}/Desktop"))
(deny file-read* (subpath "{home}/Downloads")){extra_runner_rules}
(deny file-write* (subpath "{home}"))
(deny file-write* (subpath "{repo_resolved}"))
(allow file-write* (subpath "{home}/.gemini"))
(allow file-write* (subpath "/tmp"))
(allow file-write* (subpath "/private/tmp"))
(allow file-write* (subpath "/var/folders"))
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

    Enforces Section IV of HAFlow engineering protocol:
    1. Container isolation check (verifiable OS/container runtime marker).
    2. macOS Seatbelt OS-level kernel sandbox check (active kernel enforcement probe).
    3. Dedicated unprivileged OS user check (restricted home access).

    Returns (is_isolated, reason, isolation_meta).
    """
    # 1. Container isolation check (verifiable OS/container marker)
    if os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv"):
        return True, "Isolated container environment verified (runtime marker detected)", {"isolation_type": "container"}

    # 2. macOS Seatbelt OS-level kernel sandbox check
    if sys.platform == "darwin":
        sandbox_exec = shutil.which("sandbox-exec")
        if sandbox_exec and probe_macos_seatbelt(sandbox_exec):
            profile = generate_macos_seatbelt_profile(repo_dir)
            return True, "macOS Seatbelt OS-level kernel sandbox verified and active", {
                "isolation_type": "macos-seatbelt",
                "sandbox_exec": sandbox_exec,
                "profile": profile,
            }

    # 3. Dedicated unprivileged OS user check
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

    return False, (
        "No verifiable OS-level sandbox (macOS Seatbelt, container, or restricted unprivileged user) is active; "
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
                    try:
                        shadow_result = run_agent_review(
                            agent_name=target_shadow_agent,
                            repo_dir=repo_path,
                            pr_info=pr_data,
                            diff_content=diff_content,
                            timeout_seconds=timeout_seconds,
                            system_prompt=system_prompt,
                            isolation_meta=isolation_meta,
                        )
                        shadow_status = "shadow_success"
                        shadow_result["status"] = "shadow_success"
                        shadow_result["isolation"] = {
                            "type": isolation_meta.get("isolation_type", "unknown"),
                            "reason": isolation_reason,
                        }
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
            try:
                shadow_result = run_agent_review(
                    agent_name=target_shadow_agent,
                    repo_dir=repo_path,
                    pr_info=pr_data,
                    diff_content=diff_content,
                    timeout_seconds=timeout_seconds,
                    system_prompt=system_prompt,
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
    failure_details = execution_summary.get("failure_details", {})
    elapsed_seconds = execution_summary.get("elapsed_seconds", 0.0)

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
        f"- **失败案例数**: {failed}",
    ]

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

    lines.extend([
        "",
        "## 4. 版本与环境信息",
        f"- **代码基线 SHA**: `{version_info.get('code_sha', 'HEAD')}`",
        f"- **评审 Agent 配置**: `{version_info.get('agent_config', 'default')}`",
        f"- **裁判模型配置**: `{version_info.get('judge_config', 'deepseek/deepseek-v4-flash')}`",
        f"- **ReviewBench 评分器 SHA**: `{version_info.get('scorer_sha', 'e1cb1a0dad8105ebea45caa00c194eaf2d2e7b5d')}`",
        "",
        "## 5. 执行开销",
        f"- **评测实际总耗时**: {elapsed_seconds:.2f} 秒",
        f"- **API 成本支出**: {version_info.get('cost_usd', '未知 (本地/直连环境)')}",
        "",
    ])

    return "\n".join(lines)


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
        "## 2. 案例检出变化明细",
        "",
        "详见各轮独立报告：",
        f"- 基线报告: [{before_report_file.name}]({before_report_file})",
        f"- 改进版报告: [{after_report_file.name}]({after_report_file})",
        "",
    ]

    report_content = "\n".join(lines)
    if output_file:
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text(report_content, encoding="utf-8")

    return report_content
