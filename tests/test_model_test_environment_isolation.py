"""Unit-test credentials cannot escape their case or enable a real model call."""
import os
import unittest

import pytest
from tests import test_supervisor_failsafe as _failsafe
from tests import test_trajectory_observer as _trajectory


@pytest.mark.parametrize('old', [None, 'preexisting-test-only-key'])
def test_real_unittest_credential_case_restores_exact_original_environment(monkeypatch, old):
    keys = ['JEV_API_KEY', 'TYPESAFE_API_KEY']
    for key in keys:
        if old is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, old)
    before = {key: os.environ.get(key) for key in keys}
    case = _failsafe.JevKillSwitchTests('test_api_key_never_appears_in_config_or_events')
    result = unittest.TestResult()
    case.run(result)
    assert not result.errors and not result.failures
    assert {key: os.environ.get(key) for key in keys} == before


def test_deterministic_gateway_test_cannot_call_a_model_with_injected_key(tmp_path, monkeypatch):
    from herdr.decision.providers import jev
    calls = []
    monkeypatch.setenv('JEV_API_KEY', 'test-only-key-c34')
    # Even inherited enablement is explicitly overridden by this gateway case.
    monkeypatch.setenv('HERDR_OBSERVER_JEV_ENABLED', '1')
    def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError('deterministic gateway attempted model judgment')
    monkeypatch.setattr(jev.JevDecisionProvider, 'judge_many', forbidden)
    _trajectory.TestDoneGatewayTerminalCheckpoint().test_gateway_terminal_observation_produces_verification_failure(tmp_path, monkeypatch)
    assert calls == []
