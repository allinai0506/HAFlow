"""ego-browser V1 obligation policy. No I/O, scheduling or state transitions."""
import hashlib
import json
import re

IDENTITY = ('workflow_id', 'task_id', 'run_id', 'candidate_sha')
REQUIREMENT = {'tool': 'ego-browser', 'source': 'native-session-v1'}
PROOFS = ('evidence_verified', 'browser_actions_verified', 'runtime_candidate_verified',
          'e2e_verified', 'state_readback_verified')


def freeze_contract(task, requirements):
    """Bind a declaration to one registered execution, without legacy run guesses."""
    content = {key: task.get(key) for key in IDENTITY}
    content.update(version=1, mode='observe', requirements=requirements)
    encoded = json.dumps(content, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return {**content, 'contract_sha256': hashlib.sha256(encoded.encode()).hexdigest()}


def verify_executions(contract, facts, *, available=True):
    """Judge normalized trusted facts; callers own provenance and content checks.

    A shell receipt can prove invocation/exit only. None of the current native
    adapters supplies the semantic proof flags required for satisfied.
    """
    def result(status, reason, refs=()):
        return {'status': status, 'reason': reason, 'mode': 'observe',
                'contract_sha256': contract.get('contract_sha256'),
                'evidence_refs': list(dict.fromkeys(refs))}

    content = {k: v for k, v in contract.items() if k != 'contract_sha256'}
    digest = hashlib.sha256(json.dumps(content, sort_keys=True, separators=(',', ':'),
                                      ensure_ascii=False).encode()).hexdigest()
    if (contract.get('version') != 1 or contract.get('mode') != 'observe'
            or contract.get('requirements') != [REQUIREMENT]
            or contract.get('contract_sha256') != digest
            or any(not isinstance(contract.get(k), str) or not contract[k] for k in IDENTITY)
            or not re.fullmatch('[0-9a-f]{40}', contract['candidate_sha'])):
        return result('unverifiable', 'invalid_contract')
    if not available:
        return result('unverifiable', 'source_unavailable')
    scoped = [f for f in facts if isinstance(f, dict)
              and all(f.get(k) == contract[k] for k in IDENTITY)
              and all(f.get(k) == v for k, v in REQUIREMENT.items())]
    matching = [f for f in scoped if f.get('invoked') is True]
    if not matching:
        return result('unverifiable' if scoped else 'missing',
                      'native_receipt_unverified' if scoped else 'no_trusted_invocation')
    refs = [ref for f in matching for ref in f.get('evidence_refs', []) if isinstance(ref, str) and ref]
    for fact in matching:
        if (fact.get('outcome') == 'success' and fact.get('evidence_refs')
                and all(isinstance(ref, str) and ref for ref in fact['evidence_refs'])
                and all(fact.get(k) is True for k in PROOFS)):
            return result('satisfied', 'verified_e2e', fact['evidence_refs'])
    # A failed invocation is an execution result, never a code-test verdict.
    if any(f.get('outcome') == 'failed' and f.get('evidence_refs') for f in matching):
        return result('failed', 'tool_execution_failed', refs)
    return result('unverifiable', 'e2e_proof_incomplete', refs)
