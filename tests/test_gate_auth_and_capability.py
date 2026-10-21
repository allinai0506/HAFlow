"""Regression and authorization tests for Issue #307:
Gate Exemption and Privileged Operations Enterprise Credential Authentication & Capability Binding.

Verifies:
1. HMAC Token and Bearer Token authentication:
   - issue_operator_hmac_token and verify_operator_hmac_token
   - Tampered tokens, expired tokens, subject mismatches, wrong signing secrets rejected.
2. State DB Token authentication:
   - Token lookup via state_db.get_user_token
   - Constant-time comparison, mismatch rejection.
3. Capability & Role Authorization:
   - Role enforcement (human_operator)
   - Capability enforcement (bypass_gate, force_pass_gate, force_review)
4. Anti-impersonation and Isolation:
   - Worker independence (task agent cannot self-bypass)
   - Credentials file cannot reside inside worker clone or project workspace.
   - Unauthenticated callers blocked from using privileged registered operators.
5. End-to-end integration:
   - herdr.kernel.bypass_task_gate & herdr.kernel.force_pass_gate
   - console.workflow_policy & console.task_api
   - bin/herdr-task bypass-gate & bin/herdr-factory force-pass CLI
"""

import os
import subprocess
import time
from pathlib import Path

import pytest

from herdr import kernel, state_db
from herdr.operator_auth import (
    OperatorAuthenticationError,
    OperatorAuthorizationError,
    OperatorImpersonationError,
    authenticate_and_authorize_operator,
    issue_operator_hmac_token,
    verify_operator_hmac_token,
)
from herdr.state_store import SQLiteStateStore


@pytest.fixture(autouse=True)
def isolate_credentials_environment(tmp_path, monkeypatch):
    """B2 isolation: ensure tests never touch real ~/.herdr-controller/credentials."""
    real_path = Path.home() / ".herdr-controller" / "credentials"
    before_files = set(real_path.iterdir()) if real_path.is_dir() else set()

    fake_home = tmp_path / "test_home"
    fake_cred_dir = fake_home / ".herdr-controller" / "credentials"
    fake_cred_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(state_db, "_get_credentials_dir", lambda: fake_cred_dir)

    yield fake_home

    after_files = set(real_path.iterdir()) if real_path.is_dir() else set()
    assert before_files == after_files, f"Test polluted real credentials dir: {after_files - before_files}"


class TestHMACAuthentication:
    """Unit tests for signed HMAC token generation and validation."""

    def test_issue_and_verify_valid_token(self):
        token = issue_operator_hmac_token(
            "lead_alice",
            role="human_operator",
            capabilities=["bypass_gate", "force_pass_gate"],
            secret="test_secret_key_123",
            expires_in=3600,
        )
        assert isinstance(token, str)
        assert token.startswith("haflow_v1.")

        principal = verify_operator_hmac_token(
            token,
            expected_operator="lead_alice",
            secret="test_secret_key_123",
        )
        assert principal is not None
        assert principal.operator_id == "lead_alice"
        assert principal.role == "human_operator"
        assert "bypass_gate" in principal.capabilities
        assert "force_pass_gate" in principal.capabilities
        assert principal.authenticated is True
        assert principal.auth_method == "hmac_token"

    def test_expired_token_rejected(self):
        token = issue_operator_hmac_token(
            "lead_alice",
            secret="test_secret_key_123",
            expires_in=-10,  # Already expired
        )
        with pytest.raises(OperatorAuthenticationError, match="expired"):
            verify_operator_hmac_token(
                token,
                expected_operator="lead_alice",
                secret="test_secret_key_123",
            )

    def test_tampered_token_rejected(self):
        token = issue_operator_hmac_token(
            "lead_alice",
            secret="test_secret_key_123",
        )
        # Tamper payload
        parts = token.split(".")
        tampered_token = f"{parts[0]}.{parts[1][:-2]}ab.{parts[2]}"
        with pytest.raises(OperatorAuthenticationError, match="invalid"):
            verify_operator_hmac_token(
                tampered_token,
                expected_operator="lead_alice",
                secret="test_secret_key_123",
            )

    def test_operator_subject_mismatch_rejected(self):
        token = issue_operator_hmac_token(
            "lead_alice",
            secret="test_secret_key_123",
        )
        with pytest.raises(OperatorAuthenticationError, match="subject mismatch"):
            verify_operator_hmac_token(
                token,
                expected_operator="attacker_bob",
                secret="test_secret_key_123",
            )

    def test_wrong_secret_rejected(self):
        token = issue_operator_hmac_token(
            "lead_alice",
            secret="test_secret_key_123",
        )
        with pytest.raises(OperatorAuthenticationError, match="signature"):
            verify_operator_hmac_token(
                token,
                expected_operator="lead_alice",
                secret="wrong_secret_456",
            )


class TestStateDBAuthentication:
    """Unit tests for state_db persistent credentials authentication."""

    def test_matching_state_db_token_authenticated(self, tmp_path):
        db_path = tmp_path / "state.db"
        SQLiteStateStore(db_path)
        state_db.save_user_token("lead_bob", "operator", "bob_secret_token", db_path=db_path)

        principal = authenticate_and_authorize_operator(
            operator_id="lead_bob",
            action="bypass_gate",
            token="bob_secret_token",
            db_path=db_path,
        )
        assert principal.operator_id == "lead_bob"
        assert principal.authenticated is True
        assert principal.auth_method == "state_db_token"
        assert principal.role == "human_operator"

    def test_mismatched_state_db_token_rejected(self, tmp_path):
        db_path = tmp_path / "state.db"
        SQLiteStateStore(db_path)
        state_db.save_user_token("lead_bob", "operator", "bob_secret_token", db_path=db_path)

        with pytest.raises(OperatorAuthenticationError, match="invalid operator token"):
            authenticate_and_authorize_operator(
                operator_id="lead_bob",
                action="bypass_gate",
                token="wrong_token_guess",
                db_path=db_path,
            )

    def test_missing_token_for_registered_operator_rejected(self, tmp_path):
        db_path = tmp_path / "state.db"
        SQLiteStateStore(db_path)
        state_db.save_user_token("lead_bob", "operator", "bob_secret_token", db_path=db_path)

        with pytest.raises(OperatorAuthenticationError, match="missing operator token"):
            authenticate_and_authorize_operator(
                operator_id="lead_bob",
                action="bypass_gate",
                token=None,
                db_path=db_path,
            )


class TestCapabilityAuthorization:
    """Unit tests for role and capability enforcement."""

    def test_missing_capability_rejected(self):
        token = issue_operator_hmac_token(
            "lead_carol",
            role="human_operator",
            capabilities=["force_review"],  # lacks bypass_gate
            secret="test_secret",
        )
        with pytest.raises(OperatorAuthorizationError, match="lacks capability 'bypass_gate'"):
            authenticate_and_authorize_operator(
                operator_id="lead_carol",
                action="bypass_gate",
                token=token,
                secret="test_secret",
            )

    def test_unauthorized_role_rejected(self):
        token = issue_operator_hmac_token(
            "guest_dave",
            role="guest_viewer",
            capabilities=["bypass_gate"],
            secret="test_secret",
        )
        with pytest.raises(OperatorAuthorizationError, match="unauthorized role 'guest_viewer'"):
            authenticate_and_authorize_operator(
                operator_id="guest_dave",
                action="bypass_gate",
                token=token,
                secret="test_secret",
            )


class TestAntiImpersonationAndIsolation:
    """Security tests against agent spoofing and workspace credential traversal."""

    def test_worker_cannot_self_bypass(self, tmp_path):
        db_path = tmp_path / "state.db"
        task = {"task_id": "T1", "agent": "claude"}

        with pytest.raises(OperatorImpersonationError, match="independent of the task worker"):
            authenticate_and_authorize_operator(
                operator_id="claude",
                action="bypass_gate",
                task=task,
                db_path=db_path,
            )

    def test_credentials_file_inside_worker_clone_rejected(self, tmp_path):
        clone_dir = tmp_path / "worker_clone"
        clone_dir.mkdir(parents=True)
        fake_token_file = clone_dir / "spoofed.token"
        fake_token_file.write_text("evil_token")

        task = {"task_id": "T1", "agent": "gemini", "clone_path": str(clone_dir)}

        with pytest.raises(OperatorAuthenticationError, match="cannot reside in worker clone"):
            authenticate_and_authorize_operator(
                operator_id="lead_alice",
                action="bypass_gate",
                credentials_path=str(fake_token_file),
                task=task,
            )

    def test_strict_mode_blocks_unauthenticated_operator(self, tmp_path):
        db_path = tmp_path / "state.db"
        with pytest.raises(OperatorAuthenticationError, match="missing operator credentials"):
            authenticate_and_authorize_operator(
                operator_id="random_user",
                action="bypass_gate",
                db_path=db_path,
                strict_auth=True,
            )


class TestKernelIntegrationWithAuth:
    """Integration verification in kernel.bypass_task_gate and kernel.force_pass_gate."""

    def test_kernel_bypass_task_gate_with_hmac_token(self, tmp_path):
        db_path = tmp_path / "state.db"
        store = SQLiteStateStore(db_path)

        store.save_workflow({
            "workflow_id": "wf-auth-1",
            "title": "Auth Test WF",
            "status": "running",
            "created_at": time.time(),
            "updated_at": time.time(),
        })
        task = {
            "task_id": "task-gate-1",
            "workflow_id": "wf-auth-1",
            "run_id": "run-auth-1",
            "node": "gate_test",
            "stage": "gate_test",
            "status": "blocked",
            "stage_verdict": "blocked",
            "agent": "gemini",
            "version": 1,
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        store.save_task(task)

        secret = "kernel_test_secret"
        token = issue_operator_hmac_token("sec_lead", secret=secret)

        res = kernel.bypass_task_gate(
            "task-gate-1",
            reason="Approved emergency bypass",
            operator="sec_lead",
            token=token,
            store=store,
            secret=secret,
        )
        assert res.get("status") == "completed"
        assert res.get("operator") == "sec_lead"
        receipt = res.get("receipt", {})
        assert receipt.get("operator") == "sec_lead"
        assert receipt.get("authenticated") is True
        assert receipt.get("role") == "human_operator"

    def test_kernel_force_pass_gate_with_state_db_token(self, tmp_path):
        db_path = tmp_path / "state.db"
        store = SQLiteStateStore(db_path)

        store.save_workflow({
            "workflow_id": "wf-auth-2",
            "title": "Auth Test WF 2",
            "status": "running",
            "config": {"nodes": [{"id": "gate_node", "label": "Gate"}]},
            "created_at": time.time(),
            "updated_at": time.time(),
        })
        store.save_task({
            "task_id": "task-gate-2",
            "workflow_id": "wf-auth-2",
            "run_id": "run-auth-2",
            "node": "gate_node",
            "stage": "gate_node",
            "status": "blocked",
            "stage_verdict": "blocked",
            "agent": "gemini",
            "version": 1,
            "created_at": time.time(),
            "updated_at": time.time(),
        })

        state_db.save_user_token("lead_admin", "operator", "admin_secret_999", db_path=db_path)

        res = kernel.force_pass_gate(
            "wf-auth-2",
            "gate_node",
            note="Manual force pass by admin",
            operator="lead_admin",
            token="admin_secret_999",
            store=store,
        )
        assert res.get("ok") is True
        wf = store.get_workflow("wf-auth-2")
        override = wf.get("gate_overrides", {}).get("gate_node", {})
        assert override.get("operator") == "lead_admin"
        assert override.get("authenticated") is True
        assert override.get("role") == "human_operator"

    def test_kernel_force_pass_gate_invalid_token_rejected(self, tmp_path):
        db_path = tmp_path / "state.db"
        store = SQLiteStateStore(db_path)

        store.save_workflow({
            "workflow_id": "wf-auth-3",
            "status": "running",
            "config": {"nodes": [{"id": "gate_node", "label": "Gate"}]},
            "created_at": time.time(),
            "updated_at": time.time(),
        })
        state_db.save_user_token("lead_admin", "operator", "admin_secret_999", db_path=db_path)

        with pytest.raises(OperatorAuthenticationError):
            kernel.force_pass_gate(
                "wf-auth-3",
                "gate_node",
                operator="lead_admin",
                token="wrong_token",
                store=store,
            )


class TestConsoleIntegrationWithAuth:
    """Integration verification in Web Console endpoints."""

    def test_console_validate_force_pass_params_with_token(self, tmp_path, monkeypatch):
        from console.workflow_policy import _validate_force_pass_params

        db_path = tmp_path / "state.db"
        monkeypatch.setenv("HERDR_STATE_DB", str(db_path))
        store = SQLiteStateStore(db_path)
        store.save_workflow({
            "workflow_id": "wf-c1",
            "status": "running",
            "config": {"nodes": [{"id": "review_gate"}]},
            "created_at": time.time(),
            "updated_at": time.time(),
        })
        state_db.save_user_token("console_reviewer", "operator", "reviewer_token_777", db_path=db_path)

        payload = {
            "confirmed": True,
            "reason": "Console force pass",
            "gate_node_id": "review_gate",
            "expected_task_versions": {},
            "operator": "console_reviewer",
            "token": "reviewer_token_777",
        }
        wf = {
            "workflow_id": "wf-c1",
            "config": {"nodes": [{"id": "review_gate"}]},
        }
        _gate, _reason, op = _validate_force_pass_params(payload, "wf-c1", wf=wf, db_path=db_path)
        assert op == "console_reviewer"

    def test_console_validate_force_pass_params_invalid_token_rejected(self, tmp_path, monkeypatch):
        from console.workflow_policy import _validate_force_pass_params

        db_path = tmp_path / "state.db"
        monkeypatch.setenv("HERDR_STATE_DB", str(db_path))
        store = SQLiteStateStore(db_path)
        store.save_workflow({
            "workflow_id": "wf-c1",
            "status": "running",
            "config": {"nodes": [{"id": "review_gate"}]},
            "created_at": time.time(),
            "updated_at": time.time(),
        })
        state_db.save_user_token("console_reviewer", "operator", "reviewer_token_777", db_path=db_path)

        payload = {
            "confirmed": True,
            "reason": "Console force pass",
            "gate_node_id": "review_gate",
            "expected_task_versions": {},
            "operator": "console_reviewer",
            "token": "wrong_token",
        }
        wf = {
            "workflow_id": "wf-c1",
            "config": {"nodes": [{"id": "review_gate"}]},
        }
        with pytest.raises(RuntimeError, match="鉴权失败"):
            _validate_force_pass_params(payload, "wf-c1", wf=wf, db_path=db_path)


class TestCLITokenIntegration:
    """CLI end-to-end verification of --token and --credentials flags."""

    def test_cli_bypass_gate_token_flag(self, tmp_path, monkeypatch):
        repo_root = Path(__file__).resolve().parent.parent
        db_path = tmp_path / "state.db"
        store = SQLiteStateStore(db_path)
        monkeypatch.setenv("HERDR_STATE_DB", str(db_path))

        store.save_workflow({
            "workflow_id": "wf-cli-1",
            "title": "CLI Auth WF",
            "status": "running",
            "created_at": time.time(),
            "updated_at": time.time(),
        })
        store.save_task({
            "task_id": "task-cli-gate-1",
            "workflow_id": "wf-cli-1",
            "run_id": "run-cli-1",
            "node": "cleanroom",
            "stage": "cleanroom",
            "status": "blocked",
            "stage_verdict": "blocked",
            "agent": "gemini",
            "version": 1,
            "created_at": time.time(),
            "updated_at": time.time(),
        })

        state_db.save_user_token("lead_alice", "operator", "alice_token_secret", db_path=db_path)

        env = dict(os.environ, HERDR_STATE_DB=str(db_path))

        # 1. Invalid token fails
        proc_fail = subprocess.run(
            [
                str(repo_root / "bin" / "herdr-task"),
                "bypass-gate",
                "task-cli-gate-1",
                "--reason", "CLI bypass test",
                "--operator", "lead_alice",
                "--token", "wrong_token",
            ],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert proc_fail.returncode != 0
        assert "authentication failed" in (proc_fail.stdout + proc_fail.stderr).lower()

        # 2. Valid token succeeds
        proc_ok = subprocess.run(
            [
                str(repo_root / "bin" / "herdr-task"),
                "bypass-gate",
                "task-cli-gate-1",
                "--reason", "CLI bypass test",
                "--operator", "lead_alice",
                "--token", "alice_token_secret",
            ],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert proc_ok.returncode == 0, proc_ok.stdout + proc_ok.stderr
        task = store.get_task("task-cli-gate-1")
        assert task["status"] == "completed"

    def test_cli_force_pass_token_flag(self, tmp_path, monkeypatch):
        repo_root = Path(__file__).resolve().parent.parent
        db_path = tmp_path / "state.db"
        store = SQLiteStateStore(db_path)
        monkeypatch.setenv("HERDR_STATE_DB", str(db_path))

        store.save_workflow({
            "workflow_id": "wf-cli-fp",
            "title": "CLI FP WF",
            "status": "running",
            "config": {"nodes": [{"id": "cleanroom", "label": "Cleanroom"}]},
            "created_at": time.time(),
            "updated_at": time.time(),
        })
        store.save_task({
            "task_id": "task-cli-fp-1",
            "workflow_id": "wf-cli-fp",
            "run_id": "run-cli-fp",
            "node": "cleanroom",
            "stage": "cleanroom",
            "status": "blocked",
            "stage_verdict": "blocked",
            "agent": "gemini",
            "version": 1,
            "created_at": time.time(),
            "updated_at": time.time(),
        })

        state_db.save_user_token("lead_alice", "operator", "alice_token_secret", db_path=db_path)
        env = dict(os.environ, HERDR_STATE_DB=str(db_path))

        # 1. Invalid token fails
        proc_fail = subprocess.run(
            [
                str(repo_root / "bin" / "herdr-factory"),
                "force-pass",
                "wf-cli-fp",
                "cleanroom",
                "--operator", "lead_alice",
                "--token", "bad_token",
            ],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert proc_fail.returncode != 0

        # 2. Valid token succeeds
        proc_ok = subprocess.run(
            [
                str(repo_root / "bin" / "herdr-factory"),
                "force-pass",
                "wf-cli-fp",
                "cleanroom",
                "--operator", "lead_alice",
                "--token", "alice_token_secret",
            ],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert proc_ok.returncode == 0, proc_ok.stdout + proc_ok.stderr


class TestCleanroomFindingsRegression:
    """Regression tests for cleanroom review findings B1 through B5."""

    def test_b1_default_unauthenticated_bypass_rejected(self, monkeypatch):
        """B1: Verify default fail-closed behavior: unauthenticated operator cannot bypass gate."""
        monkeypatch.delenv("HERDR_OPERATOR_AUTH_STRICT", raising=False)
        monkeypatch.delenv("HERDR_ALLOW_UNAUTHENTICATED_OPERATOR", raising=False)
        monkeypatch.delenv("HERDR_OPERATOR_TOKEN", raising=False)
        with pytest.raises(OperatorAuthenticationError, match="missing operator credentials|missing operator token"):
            authenticate_and_authorize_operator(
                operator_id="human",
                action="bypass_gate",
                token=None,
            )

    def test_b2_worker_self_issued_hmac_rejected(self, monkeypatch):
        """B2: Worker / untrusted caller cannot self-issue HMAC token without explicit secret."""
        monkeypatch.delenv("HERDR_OPERATOR_SIGNING_SECRET", raising=False)
        monkeypatch.delenv("HERDR_HMAC_SECRET", raising=False)
        monkeypatch.setattr("herdr.operator_auth.is_worker_environment", lambda: False)
        with pytest.raises(OperatorAuthorizationError, match="explicit secret|HERDR_OPERATOR_SIGNING_SECRET"):
            issue_operator_hmac_token("human", role="admin")

        monkeypatch.setattr("herdr.operator_auth.is_worker_environment", lambda: True)
        with pytest.raises(OperatorImpersonationError, match="worker environment is not authorized"):
            issue_operator_hmac_token("human", role="admin", secret="some_secret")

    def test_b3_signing_secret_fallback_to_hardcoded_prohibited(self, monkeypatch):
        """B3: If signing secret is unreadable or credentials dir raises OSError, hardcoded fallback is forbidden."""
        from herdr.operator_auth import _get_signing_secret
        monkeypatch.delenv("HERDR_OPERATOR_SIGNING_SECRET", raising=False)
        monkeypatch.delenv("HERDR_HMAC_SECRET", raising=False)
        monkeypatch.setattr(state_db, "_get_credentials_dir", lambda: (_ for _ in ()).throw(OSError("Disk failed")))
        with pytest.raises(OperatorAuthenticationError, match="unavailable"):
            _get_signing_secret()

    def test_b4_console_execute_action_forwards_credentials(self, tmp_path, monkeypatch):
        """B4: Console execute-action and signoff must forward token to herdr_kernel.force_pass_gate."""
        from console import control_api, task_api
        from herdr.state_store import SQLiteStateStore

        db_path = tmp_path / "b4_test.db"
        monkeypatch.setenv("HERDR_STATE_DB", str(db_path))
        store = SQLiteStateStore(db_path)
        store.save_workflow({
            "workflow_id": "wf-b4",
            "title": "B4 Test",
            "status": "running",
            "config": {"nodes": [{"id": "craft", "label": "Craft"}]},
            "created_at": time.time(),
            "updated_at": time.time(),
        })
        store.save_task({
            "task_id": "task-b4-1",
            "workflow_id": "wf-b4",
            "run_id": "run-b4",
            "node": "craft",
            "stage": "craft",
            "status": "blocked",
            "stage_verdict": "blocked",
            "agent": "gemini",
            "version": 1,
            "created_at": time.time(),
            "updated_at": time.time(),
        })

        state_db.save_user_token("lead_op", "operator", "lead_secret_tok_456", db_path=db_path)
        from herdr import kernel
        orig_store = kernel._get_store
        kernel._get_store = lambda s=None: store
        try:
            payload = {
                "action_type": "force_pass",
                "workflow_id": "wf-b4",
                "task_id": "task-b4-1",
                "gate_node_id": "craft",
                "reason": "Legitimate verified pass",
                "confirmed": True,
                "expected_version": 1,
                "operator": "lead_op",
                "auth_token": "lead_secret_tok_456",
            }
            res = control_api.api_controller_execute_action(payload)
            assert res.get("ok") is True

            t = store.get_task("task-b4-1")
            t["status"] = "blocked"
            t["stage_verdict"] = "blocked"
            store.save_task(t)
            fresh_t = store.get_task("task-b4-1")

            signoff_payload = {
                "action": "approve",
                "workflow_id": "wf-b4",
                "task_id": "task-b4-1",
                "node": "craft",
                "reason": "Legitimate signoff pass",
                "confirm": True,
                "expected_version": fresh_t["version"],
                "operator": "lead_op",
                "token": "lead_secret_tok_456",
            }
            s_res = task_api.api_task_signoff(signoff_payload)
            assert s_res.get("ok") is True
        finally:
            kernel._get_store = orig_store

    def test_b5_console_http_server_does_not_inject_hub_token(self):
        """B5: Console HTTP server must not inject experience_hub token into operator auth."""
        from console.http_server import Handler

        state_db.save_user_token("local-console-reviewer", "experience_hub", "hub_token_xyz")

        class FakeHandler(Handler):
            def __init__(self):
                self.headers = {"Authorization": "Bearer hub_token_xyz"}

        handler = FakeHandler.__new__(FakeHandler)
        handler.headers = {"Authorization": "Bearer hub_token_xyz"}

        assert getattr(handler, "get_operator_token", lambda: None)() is None

        handler.headers = {
            "Authorization": "Bearer hub_token_xyz",
            "X-HAFlow-Operator-Token": "op_token_123",
        }
        assert getattr(handler, "get_operator_token", lambda: None)() == "op_token_123"

