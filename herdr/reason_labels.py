"""User-facing text for stable machine reason codes.

Routing, status history, and event types keep the code. Screens,
notifications, and coordinator prompts use the Chinese label.
"""

INNER_LOOP_EXHAUSTED = "inner_loop_exhausted"
INNER_LOOP_EXHAUSTED_TEXT = (
    "工位内循环已耗尽全部重试次数，无法自愈，需要总指挥仲裁"
)

HARD_TIMEOUT_TEXT = "执行超时后被看门狗停下，需要你回复后继续"
ROUTER_ISOLATION_TEXT = "派发时被隔离检查拒绝，任务没有真正开始"
REBASE_CONFLICT_TEXT = "合并时遇到代码冲突，需要你决定怎么继续"

REASON_LABELS = {
    INNER_LOOP_EXHAUSTED: INNER_LOOP_EXHAUSTED_TEXT,
    "hard_timeout_watchdog": HARD_TIMEOUT_TEXT,
    "router_isolation_rejected": ROUTER_ISOLATION_TEXT,
    "integrate_rebase_conflict": REBASE_CONFLICT_TEXT,
}


def human_reason(value):
    """Return the Chinese label when value is a known reason code."""
    if value is None:
        return ""
    if not isinstance(value, str):
        return value
    return REASON_LABELS.get(value.strip(), value)
