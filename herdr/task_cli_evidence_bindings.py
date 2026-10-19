"""Bind evidence and PR delivery handlers to CLI-owned dependencies."""


def build_evidence_bindings(namespace):
    def task_head_sha(task):
        from herdr.task_cli_evidence import task_head_sha

        return task_head_sha(task)

    def note_add(args):
        from herdr import task_cli_evidence

        return task_cli_evidence.note_add(
            args,
            deps={
                "find_task": namespace["find_task"],
                "load_tasks": namespace["load_tasks"],
                "task_head_sha": namespace["_task_head_sha"],
            },
        )

    def note_list(args):
        from herdr.task_cli_evidence import note_list

        return note_list(args)

    def capture_verified_candidate(task):
        from herdr.task_cli_evidence import _capture_verified_candidate

        return _capture_verified_candidate(task, deps={"get_store": namespace["_get_store"]})

    def record_gate_note(task, verdict, note):
        from herdr.task_cli_evidence import _record_gate_note

        return _record_gate_note(
            task, verdict, note, deps={"task_head_sha": namespace["_task_head_sha"]}
        )

    def record_delivery_note(
        workflow_id,
        delivery_branch,
        candidate_sha,
        review_task,
        test_gate,
        base="",
        node="",
        agent="",
        supersedes=None,
    ):
        from herdr.task_cli_evidence import record_delivery_note

        return record_delivery_note(
            workflow_id,
            delivery_branch,
            candidate_sha,
            review_task,
            test_gate,
            base,
            node,
            agent,
            supersedes,
        )

    def check_delivery_for_pr(
        workflow_id,
        head_sha,
        head_ref_name="",
        merged_prs=None,
        cherry_lines=None,
        head_tree=None,
        candidate_tree=None,
        repo_path=None,
    ):
        from herdr.task_cli_evidence import check_delivery_for_pr

        return check_delivery_for_pr(
            workflow_id,
            head_sha,
            head_ref_name,
            merged_prs,
            cherry_lines,
            head_tree,
            candidate_tree,
            repo_path,
            deps={"subprocess": namespace["subprocess"]},
        )

    def create_pull_request(args):
        from herdr.task_cli_delivery import create_pull_request

        return create_pull_request(args, get_store=namespace["_get_store"])

    def merge_pull_request(args):
        from herdr.task_cli_delivery import merge_pull_request

        return merge_pull_request(args, get_store=namespace["_get_store"])

    return {
        "_task_head_sha": task_head_sha,
        "note_add": note_add,
        "note_list": note_list,
        "_capture_verified_candidate": capture_verified_candidate,
        "_record_gate_note": record_gate_note,
        "record_delivery_note": record_delivery_note,
        "check_delivery_for_pr": check_delivery_for_pr,
        "create_pull_request": create_pull_request,
        "merge_pull_request": merge_pull_request,
    }
