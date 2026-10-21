"""Console composition and encapsulation acceptance, independent of file count."""
from pathlib import Path

from console import page

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / 'console/static'


def test_cockpit_uses_shared_light_palette():
    html = page.HTML_TEMPLATE
    cockpit_css = (STATIC / 'console/styles/controller-cockpit.css').read_text()
    assert 'var(--bg-page)' in html
    assert 'var(--bg-surface)' in html
    assert 'var(--primary)' in html
    assert '#101828' not in cockpit_css
    assert '.controller-cockpit-' in cockpit_css


def test_console_sources_fit_review_budget():
    paths = [STATIC / 'console.html', *sorted((STATIC / 'console').rglob('*'))]
    sources = [p for p in paths if p.suffix in {'.html', '.js', '.css'}]
    assert sources
    assert not [(str(p.relative_to(ROOT)), len(p.read_text().splitlines()))
                for p in sources if len(p.read_text().splitlines()) > 500]


def test_page_still_has_one_ordered_main_script_and_enhancement():
    import re
    scripts = re.findall(r'<script>(.*?)</script>', page.HTML_TEMPLATE, re.S)
    assert len(scripts) == 2
    assert scripts[0].count('initFromUrlOrState();') == 2  # popstate + startup
    assert scripts[0].index('function api(') < scripts[0].rindex('initFromUrlOrState();')
    assert 'window.renderWorkflowTabs' in scripts[1]


def test_all_original_function_bodies_survive_encapsulation():
    """Inspect actual function objects inside every factory, including private helpers."""
    import hashlib
    import json
    import re
    import subprocess
    from tests.test_console_cockpit_runtime import PRELUDE

    baseline = json.loads((ROOT / 'tests/fixtures/console-ui-baseline.json').read_text())['functions']
    # These functions intentionally changed under the Cockpit projection contract.
    replaced = {'openControllerCockpitModal', 'renderSchedulerAttribution', 'renderPipelineActions',
                'loadWorkflow', 'loadWorkflowExtras', 'clearWorkflow', 'currentRecovery', 'openRecoveryDecision', 'submitDecision',
                'loadProject', 'dashOpenWorkflowOf', 'dashOpenWorkflow', 'renderDashboard', 'dashExec',
                'openWorkflowTab', 'closeWorkflowTab', 'executeControllerAction', 'updateAttentionHub',
                'renderTaskRow', 'flowOverviewHtml', 'renderFlowWorkbench', 'renderNodeInspector',
                'taskDrawerMenuHtml', 'showPane', 'askCoordinator',
                'dashSignoff', 'openSignoffChamber', 'submitSignoffDecision', 'toggleWorkflowPause',
                'stepWorkflow', 'showRollbackModal', 'executeRollback', 'showCheckpointsModal',
                'createWorkflowCheckpoint', 'restoreWorkflowCheckpoint', 'advanceStage',
                'controllerActionCard', 'catMeta', 'canSteerTask', 'canForceReviewTask', 'canForcePassTask'}
    replaced.add('runOpsAnomalyAction')
    replaced.add('visibleAlerts')
    replaced.add('renderOverview')
    replaced.add('paintCrumb')
    replaced.add('renderShellAlerts')
    replaced.add('renderShellWorkflows')
    baseline = {name: entry for name, entry in baseline.items() if name not in replaced}
    script = re.findall(r'<script>(.*?)</script>', page.HTML_TEMPLATE, re.S)[0]
    for owner in {entry['owner'] for entry in baseline.values()}:
        source = (STATIC / 'console/scripts' / (owner + '.js')).read_text()
        names = [name for name, entry in baseline.items() if entry['owner'] == owner]
        capture = '\n'.join('if(Object.hasOwn(__captured,' + json.dumps(name) + '))throw new Error("duplicate function: "+' + json.dumps(name) + ');__captured[' + json.dumps(name) + ']=' + name + '.toString();' for name in names)
        if owner == 'bootstrap':
            replacement = source + '\n' + capture
        else:
            before, after = source.rsplit('\nreturn {', 1)
            replacement = before + '\n' + capture + '\nreturn {' + after
        script = script.replace(source, replacement)
    script = script.replace('\ninitFromUrlOrState();', '\n')
    result = subprocess.run(['node', '-e', PRELUDE + '\nconst __captured={};\n' + script +
                             '\nconsole.log(JSON.stringify(__captured));'], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    actual = json.loads(result.stdout)
    assert actual.keys() == baseline.keys()
    for name, body in actual.items():
        if name == 'closeWorkflowTab':
            body = body.replace('stopLogAutoRefresh();',
                "if(typeof logState!=='undefined'&&logState.autoRefreshTimer){clearInterval(logState.autoRefreshTimer);logState.autoRefreshTimer=null;}")
        assert hashlib.sha256(body.encode()).hexdigest() == baseline[name]['sha256'], name


def test_private_lifecycle_data_is_not_in_the_page_namespace():
    import json
    import re
    import subprocess
    from tests.test_console_cockpit_runtime import PRELUDE

    script = re.findall(r'<script>(.*?)</script>', page.HTML_TEMPLATE, re.S)[0]
    script = script.replace('\ninitFromUrlOrState();', '\n')
    names = ['apiReads','pageReads','workflowExtraRequests','opsTimer','opsCountdown',
             'dashTimer','dashLinearKeyboardIdx','preflightState','preflightRunId',
             'logState','archiveQuery','TEMPLATE_SCAFFOLD','CTL_CAT_META']
    probe = '\n'.join("if(typeof " + name + " !== 'undefined')throw new Error(" + json.dumps(name) + ');' for name in names)
    result = subprocess.run(['node','-e',PRELUDE+'\n'+script+probe],text=True,capture_output=True,timeout=10)
    assert result.returncode == 0, result.stderr


def test_tab_renderer_decoration_and_log_timer_ownership():
    """A module-local render must still honor the existing late window decorator."""
    import re
    import subprocess
    from tests.test_console_cockpit_runtime import PRELUDE

    main, enhancement = re.findall(r'<script>(.*?)</script>', page.HTML_TEMPLATE, re.S)
    main = main.replace('\ninitFromUrlOrState();', '\n')
    setup = PRELUDE + "\nObject.assign(globalThis, window); window=globalThis;\n"
    probe = r"""
let updates=0;
setTimeout=(_fn,ms)=>{if(ms===10)updates++};
state.workflowId='keep';state.openWorkflowTabIds=['keep','other','__logs__'];
state.project={workflows:[{workflow_id:'keep',title:'保留'},{workflow_id:'other',title:'关闭'}]};
closeWorkflowTab('other');
if(updates!==1)throw new Error('module-local render bypassed the late decorator');
const stopped=[];let next=10;
setInterval=()=>next++;
clearInterval=id=>stopped.push(id);
toggleLogAutoRefresh(true);
closeWorkflowTab('__logs__');
closeWorkflowTab('__logs__');
if(JSON.stringify(stopped)!=='[10]')throw new Error('log timer must stop exactly once');
"""
    result=subprocess.run(['node','-e',setup+main+enhancement+probe],text=True,capture_output=True,timeout=10)
    assert result.returncode == 0, result.stderr


def test_each_capability_and_style_is_composed_exactly_once():
    import re
    from collections import Counter

    source = (STATIC / 'console.html').read_text()
    paths = re.findall(r'<!-- console:include (.*?) -->', source)
    assert [name for name, count in Counter(paths).items() if count != 1] == []
    assets = {str(path.relative_to(STATIC)) for path in (STATIC / 'console').rglob('*')
              if path.suffix in {'.css', '.js'}}
    assert set(paths) == assets
