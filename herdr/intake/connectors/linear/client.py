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


class LinearClient:
    """Read-only Linear GraphQL client for authorization validation and health monitoring."""

    def __init__(self, api_token: str, endpoint: str = "https://api.linear.app/graphql"):
        if not api_token or not isinstance(api_token, str):
            raise ValueError("api_token must be a non-empty string")
        self._token = api_token.strip()
        self._endpoint = endpoint

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
