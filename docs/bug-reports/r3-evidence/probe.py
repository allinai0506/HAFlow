"""Real CLI reproduction; all databases are temporary and fixtures local."""
import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from tests.test_quality_assessment import QualityReadTest

root = Path(__file__).resolve().parents[3]
results = {}
fixture = QualityReadTest()
fixture.setUp()
try:
    for name, options in (
        ('pairs20', {'pairs': 20}),
        ('pairs21', {'pairs': 21}),
        ('corrupt_saturation', {'pairs': 1, 'paired': False, 'corrupt': True}),
    ):
        task = 'r3-cli-' + name
        run = fixture._saturated_history(task, **options)
        before = hashlib.sha256(fixture.db.read_bytes()).hexdigest()
        cmd = [sys.executable, str(root / 'bin/herdr-quality'), 'assess',
               '--task-id', task, '--run-id', run, '--db-path', str(fixture.db)]
        completed = subprocess.run(cmd, capture_output=True, text=True, check=True)
        report = json.loads(completed.stdout)
        results[name] = {
            'anomalies': len(report['test_anomalies']),
            'process_quality': report['process_quality'],
            'problem_codes': report['problem_codes'],
            'database_unchanged': before == hashlib.sha256(fixture.db.read_bytes()).hexdigest(),
        }
    conn = sqlite3.connect(fixture.db)
    results['query_plan'] = conn.execute(
        "EXPLAIN QUERY PLAN SELECT id FROM events WHERE task_id=? AND run_id=? "
        "AND event_type='verification_completed' AND CASE WHEN json_valid(payload_json) "
        "THEN json_extract(payload_json, '$.evidence_id')=? ELSE 0 END "
        "ORDER BY timestamp,id LIMIT 10", (task, run, 'z000'),
    ).fetchall()
    conn.close()
    print(json.dumps(results, ensure_ascii=False, indent=2))
finally:
    fixture.doCleanups()
