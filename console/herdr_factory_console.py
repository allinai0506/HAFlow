#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
from http.server import ThreadingHTTPServer
_HERDR_ROOT=Path(__file__).resolve().parent.parent
if str(_HERDR_ROOT) not in sys.path:sys.path.insert(0,str(_HERDR_ROOT))
from console import common, inventory, runtime, read_api, workflow_api, control_api, task_api, http_server, workflow_policy, page
from console.page import HTML_TEMPLATE, HTML
from herdr import workflow as herdr_workflow
from herdr import workflow_graph as herdr_workflow_graph
from herdr import human_decisions as herdr_human_decisions
from herdr import projects as herdr_projects
from herdr import kernel as herdr_kernel
from herdr.kernel import state_db_path
from herdr import steering as herdr_steering
from herdr import projection as herdr_projection
from herdr import deliverables as herdr_deliverables
from herdr import archive as herdr_archive
from herdr import controller_actions as herdr_controller_actions
from herdr import delivery_pipeline as herdr_delivery_pipeline
from herdr import dashboard as herdr_dashboard
from herdr.reason_labels import human_reason
from herdr import workflow_docs as herdr_workflow_docs
from herdr import delivery_record as herdr_delivery_record
from herdr import interaction_loop as herdr_interaction_loop
from herdr.agent_binary import resolve_agent_binary
from console.common import HOME, HERDR_ROOT, ROOT, PROJECTS_FILE, WORKFLOWS_FILE, TASKS_FILE, POOLS_FILE, SLOTS_FILE, LOG_DIR, CONSOLE_STATIC_DIR, HOST, PORT, PRODUCT_NAME, PRODUCT_TAGLINE, HERDR_TASK, ACTIVE, load_json, save_json, run, run_json, tail_log
from console.inventory import ops_center, projects, workflows, tasks, project_by_id, workflow_by_id, project_for_workflow, _with_subject, workflows_for_project, tasks_for_workflow, workflow_node_types, workflow_definition_for, workflow_graph_for, enrich_workflow_tasks, stage_summary, STAGES
from console.runtime import panes, tabs, agent_runtime, pool, agent_loads, preflight, deep_preflight, slots, service_status, herdr_workspaces, infer_space_root, spaces, overview, AGENTS, AUTH_HINTS, IN_FLIGHT_STATUSES
from console.read_api import _project_workflow_template, project_detail, project_runtime, workflow_stages, _referenced_task_inventory, candidate_publication_times, attach_delivery, workflow_detail, task_detail, read_pane, _workflow_nodes, _load_deliverable_observations, _load_deliverable_pr, _tasks_for_deliverables, api_workflow_deliverables, api_read_deliverable, TraceQueryError, api_task_trajectory, api_task_projection, api_workflow_projection, api_workflows, archive_query, _dashboard_pane_runtime, dashboard_inventory, dashboard_data, _deliverable_observation_loader, _deliverable_pr_loader
from console.workflow_api import run_workflow, _is_builtin_path, _derive_template_category, templates_summary, template_detail, save_template, _run_workflow_job, start_workflow_job, workflow_job_status, set_agent_override, update_workflow_title, bind_slot, ask_coordinator, _blocked_verdict_tasks, api_workflow_recovery, api_workflow_recovery_decide, manual_advance, create_candidate, RUN_JOBS, RUN_JOBS_LOCK, TEMPLATE_NAME_RE
from console.control_api import api_kernel_pause, api_kernel_resume, api_kernel_step, api_kernel_rollback, api_kernel_force_pass, api_kernel_checkpoint_create, api_kernel_checkpoint_list, api_kernel_checkpoint_restore, api_workflow_retry_advance, api_workflow_controller_actions, pipeline_step_commands, _find_action, _run_action_command, _run_task_commands, api_workflow_decisions, _append_decision_note, api_workflow_decide_raise, api_workflow_decide_resolve, _launch_experience_inherit, api_controller_execute_action, ACTION_BINARIES
from console.workflow_policy import _resolve_target_node, _validate_force_pass_params, DEFAULT_DISALLOWED_NOTES
from console.task_api import api_task_steer, api_task_halt, api_task_steer_queue, api_agent_adapters, _check_task_interaction, api_task_interaction_get, api_task_interaction_respond, api_task_force_review, api_task_signoff
from console.http_server import _get_console_hub_client, _hub_error_status, api_experience_candidates, api_experience_candidate, api_experience_rules, api_experience_review, api_experience_revoke, Handler
from console import controller_cockpit_api

def get_controller_cockpit(workflow_id):
    return controller_cockpit_api.get_controller_cockpit(workflow_id)

def execute_controller_action(payload):
    return controller_cockpit_api.execute_controller_action(payload)

def resolve_controller_action(workflow_id, action_id, *, snapshot=None):
    return control_api.resolve_controller_action(workflow_id, action_id, snapshot=snapshot)

def main():
    try:
        from herdr import service_release
        import_root=Path(service_release.__file__).resolve().parent.parent
        fingerprint=service_release.runtime_fingerprint(import_root,[Path(__file__).resolve(),Path(service_release.__file__).resolve(),import_root/'herdr'/'state_store.py',import_root/'herdr'/'kernel.py'])
    except Exception:
        fingerprint={'running_sha':'unknown','running_import_root':'unknown','component_versions':{}}
    print('HERDR_RUNTIME_FINGERPRINT='+json.dumps({'service':'com.user.herdr-factory-console','pid':os.getpid(),**fingerprint},sort_keys=True),flush=True)
    ROOT.mkdir(parents=True,exist_ok=True)
    print(f'{PRODUCT_NAME}控制台: http://{HOST}:{PORT}',flush=True)
    ThreadingHTTPServer((HOST,PORT),Handler).serve_forever()

if __name__=='__main__':
    read_api._deliverable_observation_loader=read_api._load_deliverable_observations
    read_api._deliverable_pr_loader=read_api._load_deliverable_pr
    main()
