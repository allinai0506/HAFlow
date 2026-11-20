"""Bidirectional state synchronization and progress feedback for Linear Connector."""

from contextlib import closing
from dataclasses import dataclass
import json
import logging
from pathlib import Path
from typing import Any

from herdr.intake.schema import open_db
from .client import LinearClient
from .credentials import SecretResolutionError, resolve_api_token

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class SyncResult:
    ok: bool
    action: str
    issue_id: str | None = None
    state_id: str | None = None
    comment_id: str | None = None
    error: str | None = None


class LinearSyncDispatcher:
    """Outbound synchronization engine dispatching HAFlow task lifecycle events back to Linear."""

    def __init__(self, db_path: Path, client: LinearClient | None = None):
        self.db_path = Path(db_path)
        self._client = client

    def _get_link_and_object(self, link_id: str) -> dict[str, Any] | None:
        """Fetch link, object, and connection configuration details."""
        with closing(open_db(self.db_path)) as db:
            row = db.execute(
                """
                SELECT
                    l.link_id, l.task_id, l.disposition, l.project_id, l.workspace_id,
                    o.provider, o.external_id, o.object_type, o.snapshot_json,
                    c.auth_ref, c.status AS connection_status
                FROM intake_task_links l
                JOIN intake_objects o ON l.tenant_id = o.tenant_id AND l.object_id = o.object_id
                LEFT JOIN intake_connections c ON l.tenant_id = c.tenant_id AND l.origin_connection_id = c.connection_id
                WHERE l.link_id = ?
                """,
                (link_id,),
            ).fetchone()
            return dict(row) if row else None

    def _get_link_by_task_id(self, task_id: str) -> dict[str, Any] | None:
        """Fetch link by underlying user_inbox_tasks ID."""
        with closing(open_db(self.db_path)) as db:
            row = db.execute(
                """
                SELECT
                    l.link_id, l.task_id, l.disposition, l.project_id, l.workspace_id,
                    o.provider, o.external_id, o.object_type, o.snapshot_json,
                    c.auth_ref, c.status AS connection_status
                FROM intake_task_links l
                JOIN intake_objects o ON l.tenant_id = o.tenant_id AND l.object_id = o.object_id
                LEFT JOIN intake_connections c ON l.tenant_id = c.tenant_id AND l.origin_connection_id = c.connection_id
                WHERE l.task_id = ?
                """,
                (task_id,),
            ).fetchone()
            return dict(row) if row else None

    def _resolve_client(self, auth_ref: str | None) -> LinearClient:
        if self._client is not None:
            return self._client
        if not auth_ref:
            raise SecretResolutionError("Connection auth_ref missing; cannot resolve Linear API token")
        token = resolve_api_token(auth_ref)
        return LinearClient(api_token=token)

    def sync_task_confirmed(
        self,
        link_id: str,
        *,
        user_id: str | None = None,
        project_id: str | None = None,
    ) -> SyncResult:
        """Update Linear Issue state to In Progress ('started') and post confirmation comment."""
        info = self._get_link_and_object(link_id)
        if not info:
            return SyncResult(ok=False, action="confirm", error=f"Link {link_id} not found")

        if info.get("provider") != "linear" or info.get("object_type") != "issue":
            return SyncResult(ok=False, action="confirm", error="Target is not a Linear issue")

        external_id = info.get("external_id")
        if not external_id:
            return SyncResult(ok=False, action="confirm", error="Missing external issue id")

        snapshot = json.loads(info.get("snapshot_json") or "{}")
        team = snapshot.get("team") or {}
        team_id = team.get("id")

        try:
            client = self._resolve_client(info.get("auth_ref"))
        except Exception as exc:
            LOG.warning("Linear sync client resolution failed for confirm: %s", exc)
            return SyncResult(ok=False, action="confirm", issue_id=external_id, error=str(exc))

        if not team_id:
            try:
                team_id = client.get_issue_team_id(external_id)
            except Exception as exc:
                LOG.warning("Failed to resolve team_id for issue %s: %s", external_id, exc)

        state_id = None
        if team_id:
            try:
                state_id = client.resolve_state_id_by_type(team_id, "started")
            except Exception as exc:
                LOG.warning("Failed to resolve 'started' state for team %s: %s", team_id, exc)

        comment_id = None
        if state_id:
            try:
                client.update_issue_state(external_id, state_id)
            except Exception as exc:
                LOG.warning("Failed to update issue %s state to started (%s): %s", external_id, state_id, exc)

        try:
            body = (
                "🤖 **HAFlow Agent 已确认接手该任务**\n\n"
                f"- **执行人**: `{user_id or 'operator'}`\n"
                f"- **绑定项目**: `{project_id or info.get('project_id') or '默认项目'}`\n"
                "- **当前进展**: 已纳入执行队列，准备自动调度排期。\n"
            )
            comment_res = client.create_comment(external_id, body)
            comment_id = comment_res.get("comment", {}).get("id")
        except Exception as exc:
            LOG.warning("Failed to add confirm comment on issue %s: %s", external_id, exc)

        return SyncResult(
            ok=True,
            action="confirm",
            issue_id=external_id,
            state_id=state_id,
            comment_id=comment_id,
        )

    def sync_task_completed(
        self,
        task_id: str,
        *,
        summary: str | None = None,
    ) -> SyncResult:
        """Update Linear Issue state to Done ('completed') and post delivery comment."""
        info = self._get_link_by_task_id(task_id)
        if not info:
            return SyncResult(ok=False, action="complete", error=f"No link found for task {task_id}")

        if info.get("provider") != "linear" or info.get("object_type") != "issue":
            return SyncResult(ok=False, action="complete", error="Target is not a Linear issue")

        external_id = info.get("external_id")
        if not external_id:
            return SyncResult(ok=False, action="complete", error="Missing external issue id")

        snapshot = json.loads(info.get("snapshot_json") or "{}")
        team = snapshot.get("team") or {}
        team_id = team.get("id")

        try:
            client = self._resolve_client(info.get("auth_ref"))
        except Exception as exc:
            LOG.warning("Linear sync client resolution failed for complete: %s", exc)
            return SyncResult(ok=False, action="complete", issue_id=external_id, error=str(exc))

        if not team_id:
            try:
                team_id = client.get_issue_team_id(external_id)
            except Exception as exc:
                LOG.warning("Failed to resolve team_id for issue %s: %s", external_id, exc)

        state_id = None
        if team_id:
            try:
                state_id = client.resolve_state_id_by_type(team_id, "completed")
            except Exception as exc:
                LOG.warning("Failed to resolve 'completed' state for team %s: %s", team_id, exc)

        comment_id = None
        if state_id:
            try:
                client.update_issue_state(external_id, state_id)
            except Exception as exc:
                LOG.warning("Failed to update issue %s state to completed (%s): %s", external_id, state_id, exc)

        try:
            body = (
                "🎉 **HAFlow 任务已顺利完成闭环交付！**\n\n"
                f"- **任务 ID**: `{task_id}`\n"
                f"- **交付总结**: {summary or '所有工作流步骤均已通过验证并成功交付。'}\n"
            )
            comment_res = client.create_comment(external_id, body)
            comment_id = comment_res.get("comment", {}).get("id")
        except Exception as exc:
            LOG.warning("Failed to add complete comment on issue %s: %s", external_id, exc)

        return SyncResult(
            ok=True,
            action="complete",
            issue_id=external_id,
            state_id=state_id,
            comment_id=comment_id,
        )

    def sync_task_failed(
        self,
        task_id: str,
        *,
        error: str | None = None,
    ) -> SyncResult:
        """Post failure comment on Linear Issue."""
        info = self._get_link_by_task_id(task_id)
        if not info:
            return SyncResult(ok=False, action="failed", error=f"No link found for task {task_id}")

        if info.get("provider") != "linear" or info.get("object_type") != "issue":
            return SyncResult(ok=False, action="failed", error="Target is not a Linear issue")

        external_id = info.get("external_id")
        if not external_id:
            return SyncResult(ok=False, action="failed", error="Missing external issue id")

        try:
            client = self._resolve_client(info.get("auth_ref"))
        except Exception as exc:
            LOG.warning("Linear sync client resolution failed for failed: %s", exc)
            return SyncResult(ok=False, action="failed", issue_id=external_id, error=str(exc))

        comment_id = None
        try:
            body = (
                "⚠️ **HAFlow 任务执行异常报告**\n\n"
                f"- **任务 ID**: `{task_id}`\n"
                f"- **异常原因**: `{error or '执行未按预期完成，请登入控制台核对详细日志。'}`\n"
            )
            comment_res = client.create_comment(external_id, body)
            comment_id = comment_res.get("comment", {}).get("id")
        except Exception as exc:
            LOG.warning("Failed to add failure comment on issue %s: %s", external_id, exc)

        return SyncResult(
            ok=True,
            action="failed",
            issue_id=external_id,
            comment_id=comment_id,
        )


def dispatch_linear_sync_on_confirm(
    db_path: Path,
    link_id: str,
    *,
    user_id: str | None = None,
    project_id: str | None = None,
    client: LinearClient | None = None,
) -> SyncResult:
    """Safe, non-blocking helper for triggering sync when an intake task is confirmed."""
    try:
        dispatcher = LinearSyncDispatcher(db_path, client=client)
        return dispatcher.sync_task_confirmed(link_id, user_id=user_id, project_id=project_id)
    except Exception as exc:
        LOG.warning("dispatch_linear_sync_on_confirm failed safely: %s", exc)
        return SyncResult(ok=False, action="confirm", error=str(exc))


def dispatch_linear_sync_on_task_status(
    db_path: Path,
    task_id: str,
    new_status: str,
    *,
    error: str | None = None,
    summary: str | None = None,
    client: LinearClient | None = None,
) -> SyncResult | None:
    """Safe, non-blocking helper for triggering sync when a task reaches COMPLETED or FAILED."""
    st = str(new_status).upper()
    if st not in ("COMPLETED", "FAILED"):
        return None
    try:
        dispatcher = LinearSyncDispatcher(db_path, client=client)
        if st == "COMPLETED":
            return dispatcher.sync_task_completed(task_id, summary=summary)
        return dispatcher.sync_task_failed(task_id, error=error)
    except Exception as exc:
        LOG.warning("dispatch_linear_sync_on_task_status failed safely: %s", exc)
        return SyncResult(ok=False, action=st.lower(), error=str(exc))
