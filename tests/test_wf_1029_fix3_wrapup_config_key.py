"""Regression tests for wf-haflow-1029-02 Fix 3:
Wrapup delivery mode recognizes workflow_template / template_name / template in addition to name.
"""

from herdr import wrapup_delivery


def test_wrapup_delivery_enabled_with_workflow_template_key():
    task = {
        "task_id": "wrapup-1",
        "node": "wrapup",
        "integration_mode": "none",
        "execution_mode": "git",
    }
    # Case 1: Real workflow with workflow_template
    config_1 = {
        "workflow_template": "software-development-v1",
        "nodes": [{"id": "wrapup", "review_closure_required": True}],
    }
    assert wrapup_delivery.enabled(task, config_1) is True

    # Case 2: Config with template_name
    config_2 = {
        "template_name": "software-development-v1",
        "nodes": [{"id": "wrapup", "review_closure_required": True}],
    }
    assert wrapup_delivery.enabled(task, config_2) is True

    # Case 3: Config with template
    config_3 = {
        "template": "spec-development-v1",
        "nodes": [{"id": "wrapup", "review_closure_required": True}],
    }
    assert wrapup_delivery.enabled(task, config_3) is True

    # Case 4: Unrelated template
    config_unrelated = {
        "workflow_template": "custom-pipeline",
        "nodes": [{"id": "wrapup", "review_closure_required": True}],
    }
    assert wrapup_delivery.enabled(task, config_unrelated) is False
