"""herdr.experience.client — Client SDK for Experience Hub.

Used by:
- Local Collector: to upload candidates to Hub.
- Task Launcher (bin/herdr-task): to query approved business rules before dispatch.
- Console: to query candidates, review, publish, and revoke.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from .models import BusinessRuleExperience

LOGGER = logging.getLogger("herdr.experience.client")

DEFAULT_HUB_URL = "http://127.0.0.1:8780"


class ExperienceHubError(RuntimeError):
    """Raised when an Experience Hub operation fails."""

    def __init__(self, message: str, status_code: int = 0, error: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.error = error


class ExperienceHubSecurityError(ExperienceHubError):
    """Raised when transmitting credentials over an insecure transport."""
    pass


class ExperienceClient:
    """HTTP client communicating with Experience Hub."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        token: Optional[str] = None,
        timeout: float = 8.0,
        allow_env_token: bool = True,
    ):
        raw_url = base_url or os.environ.get("HERDR_HUB_URL") or DEFAULT_HUB_URL
        self.base_url = raw_url.rstrip("/")
        if allow_env_token:
            self.token = token or os.environ.get("HERDR_HUB_TOKEN") or ""
        else:
            # Explicit caller identity only: empty stays empty, never inherits
            # server-side env credentials (used by Console per-request path).
            self.token = token or ""
        self.timeout = timeout

        # P1-09: Validate transport security. The opt-out flag accepts only
        # explicit truthy values; "0"/"false"/empty never allow plaintext.
        parsed = urllib.parse.urlparse(self.base_url)
        is_loopback = parsed.hostname in ("127.0.0.1", "localhost", "::1", None)
        allow_insecure = str(os.environ.get("HERDR_ALLOW_INSECURE_HTTP") or "").strip().lower() in {"1", "true", "yes"}
        if parsed.scheme == "http" and not is_loopback and not allow_insecure:
            raise ExperienceHubSecurityError(
                f"Refusing to transmit Bearer Token over plain HTTP to non-loopback host '{parsed.hostname}'. "
                "Configure HTTPS or set HERDR_ALLOW_INSECURE_HTTP=1 for testing."
            )

    def _request(
        self,
        method: str,
        path: str,
        data: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        if params:
            filtered = {k: v for k, v in params.items() if v is not None}
            if filtered:
                url += "?" + urllib.parse.urlencode(filtered)

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        body = json.dumps(data, ensure_ascii=False).encode("utf-8") if data is not None else None
        req = urllib.request.Request(url, data=body, headers=headers, method=method)

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                resp_bytes = resp.read()
                return json.loads(resp_bytes.decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace")
            try:
                err_json = json.loads(err_body)
            except Exception:
                err_json = {"error": f"HTTP {exc.code}", "message": err_body}
            return {"ok": False, "status_code": exc.code, "error": err_json.get("error"), "message": err_json.get("message")}
        except Exception as exc:
            return {"ok": False, "status_code": 0, "error": "network_error", "message": str(exc)}

    def is_healthy(self) -> bool:
        res = self._request("GET", "/api/v1/health")
        return res.get("status") == "ok"

    def submit_candidate(self, exp: BusinessRuleExperience) -> Dict[str, Any]:
        return self._request("POST", "/api/v1/experiences/candidates", data=exp.to_dict())

    def query_rules(
        self,
        customer: Optional[str] = None,
        task_type: Optional[str] = None,
        raise_for_status: bool = False,
    ) -> List[BusinessRuleExperience]:
        """Fetch active/published rules applicable to customer and task_type."""
        res = self._request(
            "GET",
            "/api/v1/experiences/rules",
            params={"customer": customer, "task_type": task_type},
        )
        if not res.get("ok"):
            err_msg = f"query_rules failed (HTTP {res.get('status_code')}): {res.get('error')} - {res.get('message')}"
            LOGGER.warning(err_msg)
            if raise_for_status:
                raise ExperienceHubError(err_msg, status_code=res.get("status_code", 0), error=res.get("error", ""))
            return []
        rules = []
        for r_dict in res.get("rules") or []:
            try:
                rules.append(BusinessRuleExperience.from_dict(r_dict))
            except Exception as e:
                LOGGER.debug("Failed parsing rule dict: %s", e)
                if raise_for_status:
                    raise ExperienceHubError(f"Invalid rule payload: {e}")
        return rules

    def list_candidates(
        self,
        status: Optional[str] = None,
        customer: Optional[str] = None,
        task_type: Optional[str] = None,
        raise_for_status: bool = False,
    ) -> List[BusinessRuleExperience]:
        res = self._request(
            "GET",
            "/api/v1/experiences/candidates",
            params={"status": status, "customer": customer, "task_type": task_type},
        )
        if not res.get("ok"):
            err_msg = f"list_candidates failed (HTTP {res.get('status_code')}): {res.get('error')} - {res.get('message')}"
            LOGGER.warning(err_msg)
            if raise_for_status:
                raise ExperienceHubError(err_msg, status_code=res.get("status_code", 0), error=res.get("error", ""))
            return []
        candidates = []
        for c_dict in res.get("candidates") or []:
            try:
                candidates.append(BusinessRuleExperience.from_dict(c_dict))
            except Exception as e:
                LOGGER.debug("Failed parsing candidate dict: %s", e)
                if raise_for_status:
                    raise ExperienceHubError(f"Invalid candidate payload: {e}")
        return candidates

    def get_experience(self, experience_id: str, raise_for_status: bool = False) -> Optional[BusinessRuleExperience]:
        res = self._request("GET", f"/api/v1/experiences/{experience_id}")
        if not res.get("ok"):
            if raise_for_status:
                raise ExperienceHubError(f"get_experience failed (HTTP {res.get('status_code')}): {res.get('error')}", status_code=res.get("status_code", 0), error=res.get("error", ""))
            return None
        if not res.get("experience"):
            return None
        return BusinessRuleExperience.from_dict(res["experience"])

    def review_candidate(
        self,
        experience_id: str,
        action: str,  # 'approve' | 'reject'
        comment: str = "",
    ) -> Dict[str, Any]:
        return self._request(
            "POST",
            f"/api/v1/experiences/{experience_id}/review",
            data={"action": action, "comment": comment},
        )

    def revoke_rule(self, experience_id: str, reason: str = "") -> Dict[str, Any]:
        return self._request(
            "POST",
            f"/api/v1/experiences/{experience_id}/revoke",
            data={"reason": reason},
        )


def format_rules_for_prompt(rules: List[BusinessRuleExperience]) -> str:
    """Format active business rules into an authoritative prompt injection block."""
    if not rules:
        return ""

    rule_line = "━" * 36
    lines = [
        rule_line,
        "【企业已审核业务规则 (Enterprise Business Rules)】",
        rule_line,
        "以下业务规则经企业审核批准，当前任务必须遵循：\n",
    ]

    for idx, rule in enumerate(rules, 1):
        lines.append(f"[{idx}] 规则标识: {rule.experience_id} (v{rule.version})")
        lines.append(f"    标题: {rule.title}")
        lines.append(f"    适用客户: {rule.applies_to.customer or '全部客户'}")
        lines.append(f"    适用任务类型: {rule.applies_to.task_type or '通用'}")
        lines.append(f"    规则内容:\n{rule.content}\n")

    lines.append("【执行铁律】")
    lines.append("以上为企业正式业务规则，Agent 必须无条件优先遵循，并在产出和自检时严格对齐上述规则。")
    lines.append(rule_line)
    return "\n".join(lines)


def fetch_experience_rules_block(
    task: Dict[str, Any],
    client: Optional[ExperienceClient] = None,
    store: Optional[Any] = None,
) -> str:
    """Fetch active published enterprise business rules and return prompt block.

    Contract: mandatory_experience_rules=True means fail-closed on transport
    or auth failure (Hub unreachable / 401 / 403 raises). An empty rule set
    after a successful Hub query is NOT an error and returns "".
    """
    is_mandatory = bool(task.get("mandatory_experience_rules"))
    try:
        payload = task.get("payload_json")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                payload = {}
        elif not isinstance(payload, dict):
            payload = {}

        context_meta = task.get("context_metadata") or {}
        if isinstance(context_meta, str):
            try:
                context_meta = json.loads(context_meta)
            except Exception:
                context_meta = {}

        raw_cust = (
            task.get("customer")
            or payload.get("customer")
            or payload.get("customer_id")
            or context_meta.get("customer")
            or context_meta.get("customer_id")
            or None
        )
        customer = str(raw_cust).strip() if raw_cust and str(raw_cust).strip() not in {"unknown", "default_customer"} else None

        task_type = (
            task.get("task_type")
            or payload.get("task_type")
            or task.get("stage")
            or task.get("node")
            or None
        )

        hub_client = client or ExperienceClient()
        rules = hub_client.query_rules(customer=customer, task_type=task_type, raise_for_status=is_mandatory)
        if rules:
            if store and hasattr(store, "update_task_metadata"):
                try:
                    applied_refs = [{"experience_id": r.experience_id, "version": r.version, "title": r.title} for r in rules]
                    meta = dict(task.get("metadata") or {})
                    meta["applied_experience_rules"] = applied_refs
                    store.update_task_metadata(task["task_id"], meta)
                except Exception:
                    pass
            block = format_rules_for_prompt(rules)
            return "\n\n" + block + "\n" if block else ""
    except Exception as exc:
        if is_mandatory:
            raise RuntimeError(f"Experience Hub is unreachable but mandatory rules required: {exc}") from exc
        LOGGER.warning("Non-mandatory experience rules query degraded: %s", exc)
    return ""
