"""Run a real Console/SQLite fixture for browser acceptance without real Agents.

Usage: python tests/support/console_ui_fixture.py
Only terminal inventory/probes are replaced; HTTP, workflow reads and Kernel
pause/resume/rename persistence use production implementations in a temporary HOME.
"""
import json
import os
from pathlib import Path
import sys
import tempfile
from http.server import ThreadingHTTPServer


def main():
    with tempfile.TemporaryDirectory(prefix='haflow-console-ui-') as temporary:
        home = Path(temporary)
        os.environ.update(HOME=str(home), HERDR_STATE_DB=str(home / 'state.db'),
                          WORKFLOWS_FILE=str(home / 'workflows.json'),
                          TASKS_FILE=str(home / 'tasks.json'),
                          HERDR_OBSERVER_ENABLED='0', HERDR_OBSERVER_JEV_ENABLED='0')
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from console import common, runtime
        from console.http_server import Handler
        from herdr import state_db

        project = {'project_id':'ui-project', 'project_name':'模块迁移验收',
                   'workspace_id':'ui-space', 'project_root':str(home),
                   'workflow_file':str(home / 'workflow.json')}
        common.PROJECTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        common.PROJECTS_FILE.write_text(json.dumps({'projects':{'ui-project':project}}))
        config = {'name':'ui-fixture', 'nodes':[
            {'id':'build','label':'实现','node_type':'agent','depends_on':[]},
            {'id':'verify','label':'验收','node_type':'agent','depends_on':['build']}]}
        (home / 'workflow.json').write_text(json.dumps(config))
        for wid, title in [('ui-owned','模块迁移验收'), ('ui-other','旁侧工作流')]:
            state_db.save_workflow({'workflow_id':wid,'project_id':'ui-project',
                                   'title':title,'status':'running','config':config},
                                  db_path=home / 'state.db')
        # Replace only physical terminal inventory and health probes.
        runtime.spaces = lambda: [{'workspace_id':'ui-space','label':'模块迁移验收',
                                   'relation':'current_factory','project_id':'ui-project'}]
        runtime.preflight = lambda *args, **kwargs: []
        runtime.panes = lambda *args, **kwargs: []
        runtime.tabs = lambda *args, **kwargs: []
        runtime.slots = lambda *args, **kwargs: []
        runtime.agent_runtime = lambda *args, **kwargs: None
        with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
            print(json.dumps({'url':f'http://127.0.0.1:{server.server_port}/?workflow_id=ui-owned',
                              'db':str(home / 'state.db')}), flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass


if __name__ == '__main__':
    main()
