"""Independent syntax and semantic validator for gate commands and shell/awk scripts.

Validates:
1. Shell syntax and balanced quotes/brackets.
2. Tautological self-comparisons ($col == $col, $col != $col).
3. Out-of-bounds column accesses against TSV Schema.
4. Semantic domain incompatibilities (comparing disjoint enums as != creating tautology).
"""

import re
import shlex
from typing import Any, Dict, List, Optional, Tuple, Union


def validate_shell_syntax(command_str: str) -> Tuple[bool, str]:
    """Validate shell syntax including quotes and basic tokens."""
    if not command_str or not command_str.strip():
        return False, "Command is empty"
    try:
        shlex.split(command_str)
    except ValueError as e:
        return False, f"Shell syntax error: {e}"
    
    # Check balanced braces in awk blocks if awk is present
    if "awk" in command_str:
        braces = 0
        in_single_quote = False
        in_double_quote = False
        escape = False
        for ch in command_str:
            if escape:
                escape = False
                continue
            if ch == '\\':
                escape = True
                continue
            if ch == "'" and not in_double_quote:
                in_single_quote = not in_single_quote
            elif ch == '"' and not in_single_quote:
                in_double_quote = not in_double_quote
            elif not in_single_quote and not in_double_quote:
                if ch == '{':
                    braces += 1
                elif ch == '}':
                    braces -= 1
                    if braces < 0:
                        return False, "Unbalanced braces in command syntax"
        if braces != 0:
            return False, "Unbalanced braces in command syntax"

    return True, ""


def extract_column_comparisons(command_str: str) -> List[Dict[str, Any]]:
    """Extract comparisons between columns, e.g. $4 != $5 or $2 == $2."""
    comparisons = []
    # Pattern: $1 == $2, $1 != $2, etc.
    col_cmp_pattern = re.compile(r'\$(\d+)\s*(==|!=|<=|>=|<|>)\s*\$(\d+)')
    for match in col_cmp_pattern.finditer(command_str):
        col1 = int(match.group(1))
        op = match.group(2)
        col2 = int(match.group(3))
        comparisons.append({
            "col1": col1,
            "op": op,
            "col2": col2,
            "raw": match.group(0),
        })
    return comparisons


def extract_column_indices(command_str: str) -> List[int]:
    """Extract all referenced column numbers ($1, $2, etc.)."""
    indices = set()
    for match in re.finditer(r'\$(\d+)', command_str):
        indices.add(int(match.group(1)))
    return sorted(list(indices))


def validate_gate_command(
    command_str: str,
    tsv_schema: Optional[Union[List[str], Dict[Union[int, str], Any]]] = None
) -> Dict[str, Any]:
    """Perform syntax and semantic verification of gate command against TSV schema."""
    issues: List[str] = []
    warnings: List[str] = []

    # 1. Syntax check
    ok, err = validate_shell_syntax(command_str)
    if not ok:
        issues.append(err)
        return {"valid": False, "issues": issues, "warnings": warnings}

    # 2. Extract comparisons
    col_cmps = extract_column_comparisons(command_str)
    for cmp_info in col_cmps:
        c1 = cmp_info["col1"]
        c2 = cmp_info["col2"]
        op = cmp_info["op"]

        # Self comparison check
        if c1 == c2:
            if op == "==":
                issues.append(f"Self-comparison tautology detected: ${c1} == ${c2} is always true.")
            elif op == "!=":
                issues.append(f"Self-comparison contradiction detected: ${c1} != ${c2} is always false.")

    # 3. TSV Schema Checks
    if tsv_schema is not None:
        normalized_schema: Dict[int, Dict[str, Any]] = {}
        if isinstance(tsv_schema, list):
            for idx, name in enumerate(tsv_schema, start=1):
                normalized_schema[idx] = {"name": name, "type": "string"}
        elif isinstance(tsv_schema, dict):
            for k, v in tsv_schema.items():
                normalized_schema[int(k)] = v if isinstance(v, dict) else {"name": str(v), "type": "string"}

        max_col = max(normalized_schema.keys()) if normalized_schema else 0

        # Check out of bounds column references
        referenced_indices = extract_column_indices(command_str)
        for idx in referenced_indices:
            if idx > max_col:
                issues.append(f"Column index out of bounds: index {idx} exceeds schema column count ({max_col}).")

        # Check disjoint domains and type incompatibilities
        for cmp_info in col_cmps:
            c1 = cmp_info["col1"]
            c2 = cmp_info["col2"]
            op = cmp_info["op"]
            if c1 in normalized_schema and c2 in normalized_schema and c1 != c2:
                info1 = normalized_schema[c1]
                info2 = normalized_schema[c2]

                vals1 = set(info1.get("allowed_values") or [])
                vals2 = set(info2.get("allowed_values") or [])

                if vals1 and vals2 and not (vals1 & vals2):
                    # Disjoint domains
                    if op == "!=":
                        issues.append(
                            f"Semantic tautology: columns ${c1} ('{info1.get('name')}') and ${c2} ('{info2.get('name')}') "
                            f"have disjoint allowed values ({vals1} vs {vals2}) and will always be !=."
                        )
                    elif op == "==":
                        issues.append(
                            f"Semantic contradiction: columns ${c1} ('{info1.get('name')}') and ${c2} ('{info2.get('name')}') "
                            f"have disjoint allowed values ({vals1} vs {vals2}) and can never be ==."
                        )

    return {
        "valid": len(issues) == 0,
        "issues": issues,
        "warnings": warnings,
    }
