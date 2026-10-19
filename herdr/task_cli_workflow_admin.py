"""Workflow migration and recovery administrative commands."""

import json
import sqlite3
import sys
import time
from pathlib import Path


def cmd_workflow_migration(args):
    from herdr import workflow_repair_migration as migration, state_db

    try:
        db_path = state_db.resolve_state_db_path()
        if args.output and Path(args.output).exists():
            raise ValueError("output file already exists")
        if args.action == "plan":
            result = migration.plan(db_path, args.workflow_id)
        else:
            if not args.input:
                raise ValueError("--input reviewed plan/receipt is required")
            data = json.loads(Path(args.input).read_text())
            if data.get("workflow_id") != args.workflow_id:
                raise ValueError("workflow scope mismatch")
            result = (
                migration.apply(db_path, data)
                if args.action == "apply"
                else migration.rollback(db_path, data)
            )
        output = json.dumps(result, ensure_ascii=False, indent=2)
        if args.output:
            path = Path(args.output)
            with path.open("x") as stream:
                stream.write(output + "\n")
        else:
            print(output)
    except (OSError, ValueError, TypeError, sqlite3.Error) as exc:
        print(f"[MIGRATION REJECTED] {exc}", file=sys.stderr)
        sys.exit(2)


def cmd_workflow_recovery(args):
    from herdr import recovery_store, state_db

    try:
        db_path = state_db.resolve_state_db_path()
        if args.command == "recovery-status":
            result = {
                "workflow_id": args.workflow_id,
                "operations": recovery_store.list_operations(db_path, args.workflow_id),
            }
        else:
            result = recovery_store.decide_operation(
                db_path,
                args.operation_id,
                args.expected_version,
                args.operator,
                args.action,
                args.reason,
                time.time(),
                until=args.until,
            )
        print(json.dumps(result, ensure_ascii=False))
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"[RECOVERY REJECTED] {exc}", file=sys.stderr)
        sys.exit(2)
