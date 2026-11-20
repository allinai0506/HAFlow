"""Linear GraphQL API client for connection preflight and health checks."""

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ConnectionTestReport:
    ok: bool
    organization_name: str | None = None
    organization_id: str | None = None
    viewer_name: str | None = None
    viewer_email: str | None = None
    team_keys: tuple[str, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class HealthStatus:
    status: str  # "OK", "DEGRADED", "AUTH_REQUIRED"
    message: str | None = None


QUERY_PREFLIGHT = """
query PreflightCheck {
  viewer {
    id
    name
    email
    active
  }
  organization {
    id
    name
    urlKey
  }
  teams {
    nodes {
      id
      key
      name
    }
  }
}
"""

QUERY_TEAM_STATES = """
query TeamStates($teamId: String!) {
  team(id: $teamId) {
    id
    key
    name
    states {
      nodes {
        id
        name
        type
        position
      }
    }
  }
}
"""

MUTATION_ISSUE_UPDATE = """
mutation IssueUpdate($id: String!, $input: IssueUpdateInput!) {
  issueUpdate(id: $id, input: $input) {
    success
    issue {
      id
      state {
        id
        name
        type
      }
    }
  }
}
"""

MUTATION_COMMENT_CREATE = """
mutation CommentCreate($input: CommentCreateInput!) {
  commentCreate(input: $input) {
    success
    comment {
      id
      body
      createdAt
    }
  }
}
"""


class LinearClient:
    """Linear GraphQL client for authorization validation, health monitoring, and bidirectional sync."""

    def __init__(
        self,
        api_token: str,
        endpoint: str = "https://api.linear.app/graphql",
        bot_user_id: str | None = None,
    ):
        if not api_token or not isinstance(api_token, str):
            raise ValueError("api_token must be a non-empty string")
        self._token = api_token.strip()
        self._endpoint = endpoint
        self._bot_user_id = bot_user_id
        self._states_cache: dict[str, list[dict[str, Any]]] = {}

    def _post_graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        headers = {
            "Content-Type": "application/json",
            "Authorization": self._token if self._token.startswith("Bearer ") or self._token.startswith("lin_api_") else f"Bearer {self._token}",
            "User-Agent": "HAFlow-Linear-Connector/1.0",
        }
        data = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
        req = urllib.request.Request(self._endpoint, data=data, headers=headers, method="POST")

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8")
                return json.loads(body)
            except Exception:
                return {"errors": [{"message": f"HTTP {exc.code}: {exc.reason}"}]}
        except Exception as exc:
            return {"errors": [{"message": str(exc)}]}

    def test_connection(
        self,
        expected_org_id: str | None = None,
        expected_team_keys: list[str] | None = None,
    ) -> ConnectionTestReport:
        """Query viewer, organization, and teams to verify authentication and scope."""
        res = self._post_graphql(QUERY_PREFLIGHT)
        errors = res.get("errors")
        if errors:
            parts = []
            for e in errors:
                msg = e.get("message", "unknown error")
                code = e.get("extensions", {}).get("code") if isinstance(e.get("extensions"), dict) else None
                parts.append(f"{code}: {msg}" if code else msg)
            err_msg = "; ".join(parts)
            return ConnectionTestReport(ok=False, error=err_msg)

        data = res.get("data") or {}
        viewer = data.get("viewer") or {}
        org = data.get("organization") or {}
        teams_data = data.get("teams", {}).get("nodes", [])

        org_id = org.get("id")
        if expected_org_id and org_id != expected_org_id:
            return ConnectionTestReport(
                ok=False,
                error=f"ORGANIZATION_MISMATCH: expected {expected_org_id}, got {org_id}",
                organization_id=org_id,
                organization_name=org.get("name"),
            )

        found_team_keys = tuple(t.get("key") for t in teams_data if t.get("key"))
        if expected_team_keys:
            missing = [k for k in expected_team_keys if k not in found_team_keys]
            if missing:
                return ConnectionTestReport(
                    ok=False,
                    error=f"MISSING_TEAMS: configured teams not found: {missing}",
                    team_keys=found_team_keys,
                )

        return ConnectionTestReport(
            ok=True,
            organization_id=org_id,
            organization_name=org.get("name"),
            viewer_name=viewer.get("name"),
            viewer_email=viewer.get("email"),
            team_keys=found_team_keys,
        )

    def health_check(self, expected_org_id: str | None = None) -> HealthStatus:
        """Check live connection status and classify health code."""
        report = self.test_connection(expected_org_id=expected_org_id)
        if report.ok:
            return HealthStatus(status="OK")
        if "AUTHENTICAT" in (report.error or "").upper() or "401" in (report.error or ""):
            return HealthStatus(status="AUTH_REQUIRED", message=report.error)
        return HealthStatus(status="DEGRADED", message=report.error)

    def get_team_workflow_states(self, team_id: str, *, refresh: bool = False) -> list[dict[str, Any]]:
        """Fetch and cache workflow states for a Linear team."""
        if not team_id or not isinstance(team_id, str):
            raise ValueError("team_id must be a non-empty string")
        if not refresh and team_id in self._states_cache:
            return self._states_cache[team_id]

        res = self._post_graphql(QUERY_TEAM_STATES, {"teamId": team_id})
        errors = res.get("errors")
        if errors:
            err_msg = "; ".join(e.get("message", "unknown error") for e in errors)
            raise RuntimeError(f"Failed to fetch team workflow states: {err_msg}")

        nodes = res.get("data", {}).get("team", {}).get("states", {}).get("nodes", [])
        self._states_cache[team_id] = nodes
        return nodes

    def resolve_state_id_by_type(self, team_id: str, state_type: str) -> str | None:
        """Find the matching state ID for a given type (e.g. 'started' or 'completed')."""
        states = self.get_team_workflow_states(team_id)
        target = state_type.strip().lower()
        matches = [s for s in states if str(s.get("type", "")).strip().lower() == target]
        if not matches:
            return None
        matches.sort(key=lambda s: (s.get("position", 0), s.get("id", "")))
        return matches[0].get("id")

    def update_issue_state(self, issue_id: str, state_id: str) -> dict[str, Any]:
        """Update an issue's workflow state in Linear."""
        if not issue_id or not isinstance(issue_id, str):
            raise ValueError("issue_id must be a non-empty string")
        if not state_id or not isinstance(state_id, str):
            raise ValueError("state_id must be a non-empty string")

        res = self._post_graphql(MUTATION_ISSUE_UPDATE, {"id": issue_id, "input": {"stateId": state_id}})
        errors = res.get("errors")
        if errors:
            err_msg = "; ".join(e.get("message", "unknown error") for e in errors)
            raise RuntimeError(f"Linear issueUpdate failed: {err_msg}")

        update_data = res.get("data", {}).get("issueUpdate", {})
        if not update_data.get("success"):
            raise RuntimeError(f"Linear issueUpdate unsuccessful: {res}")
        return update_data

    def create_comment(self, issue_id: str, body: str) -> dict[str, Any]:
        """Create a markdown comment on a Linear issue."""
        if not issue_id or not isinstance(issue_id, str):
            raise ValueError("issue_id must be a non-empty string")
        if not body or not isinstance(body, str):
            raise ValueError("body must be a non-empty string")

        res = self._post_graphql(MUTATION_COMMENT_CREATE, {"input": {"issueId": issue_id, "body": body}})
        errors = res.get("errors")
        if errors:
            err_msg = "; ".join(e.get("message", "unknown error") for e in errors)
            raise RuntimeError(f"Linear commentCreate failed: {err_msg}")

        comment_data = res.get("data", {}).get("commentCreate", {})
        if not comment_data.get("success"):
            raise RuntimeError(f"Linear commentCreate unsuccessful: {res}")
        return comment_data
