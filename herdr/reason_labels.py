"""User-facing text for stable machine reason codes.

Routing, status history, and event types keep the code. Screens,
notifications, and coordinator prompts use the Chinese label.
"""

INNER_LOOP_EXHAUSTED = "inner_loop_exhausted"
INNER_LOOP_EXHAUSTED_TEXT = (
    "工位内循环已耗尽全部重试次数，无法自愈，需要总指挥仲裁"
)

REASON_LABELS = {
    INNER_LOOP_EXHAUSTED: INNER_LOOP_EXHAUSTED_TEXT,
}


def human_reason(value):
    """Return the Chinese label when value is a known reason code."""
    if value is None:
        return ""
    if not isinstance(value, str):
        return value
    return REASON_LABELS.get(value.strip(), value)
