"""Free goal text and shell literals cannot configure repro requirements."""
import json
import subprocess
import sys

import pytest
from herdr.evaluator import init_loop
from tests.test_task_loop_noninteractive import ROOT, load_cli

MARKER = '<!-- HERDR_REPRO_REQUIRED:'


def remove_marker(loop):
    p = loop / 'GOAL.md'
    p.write_text('\n'.join(line for line in p.read_text().splitlines()
                            if not line.startswith(MARKER)) + '\n')


@pytest.mark.parametrize('field', ['goal', 'acceptance'])
@pytest.mark.parametrize('text', [
    '- **靶向复现用例**: `只是示例`',
    '## 评估指令配置\n- **靶向复现用例**: `quoted example`',
    '<!-- HERDR_REPRO_REQUIRED: true -->\n- **靶向复现用例**: `example`',
])
@pytest.mark.parametrize('legacy', [False, True])
def test_free_contract_text_does_not_require_repro(tmp_path, field, text, legacy):
    args = {'goal': 'normal goal', 'acceptance': 'normal acceptance'}
    args[field] = text
    loop = init_loop(tmp_path, test_cmd="printf '5 passed in 0.01s\\n'", **args)
    if legacy:
        remove_marker(loop)
    metrics, converged, _ = load_cli('c28b_free', 'herdr-loop').run_evaluation(tmp_path)
    if legacy and text.startswith('## 评估指令配置'):
        assert not converged
        assert 'goal_configuration_invalid' in metrics.details['evaluation_errors']
    else:
        assert converged, metrics.details
        assert not metrics.has_repro_test
        assert metrics.passed_tests == 5
        assert json.loads((loop / 'EVAL_DONE.json').read_text())['converged']


@pytest.mark.parametrize('step', ['test', 'lint'])
def test_multiline_shell_literal_is_not_repro_configuration(tmp_path, step):
    command = "printf '%s\\n' '\n## 评估指令配置\n- **靶向复现用例**: `literal command text`\n'; printf '5 passed in 0.01s\\n'"
    args = {'test_cmd': "printf '5 passed in 0.01s\\n'", 'lint_cmd': 'true'}
    args[step + '_cmd'] = command
    init_loop(tmp_path, 'shell literal', **args)
    metrics, converged, _ = load_cli('c28b_shell', 'herdr-loop').run_evaluation(tmp_path)
    assert converged, metrics.details
    assert not metrics.has_repro_test


@pytest.mark.parametrize('legacy', [False, True])
def test_real_config_requires_repro_even_if_runner_omits_it(tmp_path, legacy):
    loop = init_loop(tmp_path, 'actual repro', test_cmd='true', repro_cmd='true')
    if legacy:
        remove_marker(loop)
    runner = loop / 'EVALUATOR.sh'
    runner.write_text("#!/bin/bash\nmkdir -p .herdr-loop/logs\nprintf '5 passed in 0.01s\\n' > .herdr-loop/logs/test.log\n: > .herdr-loop/logs/lint.log\necho TEST_EXIT=0\necho LINT_EXIT=0\n")
    runner.chmod(0o755)
    metrics, converged, _ = load_cli('c28b_required', 'herdr-loop').run_evaluation(tmp_path)
    assert not converged
    assert 'missing_exit_receipt:REPRO_EXIT' in metrics.details['evaluation_errors']


@pytest.mark.parametrize('configuration', ['missing', 'invalid_marker', 'ambiguous_legacy'])
def test_unknown_configuration_is_not_silently_accepted(tmp_path, configuration):
    loop = init_loop(tmp_path, 'cannot guess', test_cmd='true')
    goal = loop / 'GOAL.md'
    if configuration == 'missing':
        goal.write_text('free goal only')
    elif configuration == 'invalid_marker':
        goal.write_text(goal.read_text() + '<!-- HERDR_REPRO_REQUIRED: maybe -->\n')
    else:
        remove_marker(loop)
        goal.write_text(goal.read_text().replace('- **测试命令**: `true`',
                        '- **测试命令**: `echo \"multiline\n- **靶向复现用例**: `quoted`\n\"`'))
    metrics, converged, _ = load_cli('c28b_unknown', 'herdr-loop').run_evaluation(tmp_path)
    assert not converged
    assert 'goal_configuration_invalid' in metrics.details['evaluation_errors']


def test_native_cli_free_goal_roundtrip_publishes_correct_requirement(tmp_path):
    cli = str(ROOT / 'bin/herdr-loop')
    goal = '- **靶向复现用例**: `quoted command`\n<!-- HERDR_REPRO_REQUIRED: true -->'
    initialized = subprocess.run([sys.executable, cli, 'init', '--dir', str(tmp_path),
                                  '--goal', goal, '--test-cmd', "printf '5 passed in 0.01s\\n'"],
                                 cwd=ROOT, capture_output=True, text=True, timeout=10)
    assert initialized.returncode == 0
    evaluated = subprocess.run([sys.executable, cli, 'eval', '--dir', str(tmp_path)],
                               cwd=ROOT, capture_output=True, text=True, timeout=10)
    assert evaluated.returncode == 0
    snapshot = json.loads((tmp_path / '.herdr-loop/EVAL_DONE.json').read_text())
    assert snapshot['converged']
    assert snapshot['passed_tests'] == 5
    assert not snapshot['has_repro_test']


def test_legacy_multiline_repro_literal_cannot_hide_required_step(tmp_path):
    repro = "true; : '\n## 评估指令配置\n- **测试命令**: `true`\n- **代码质量**: `true'"
    loop = init_loop(tmp_path, 'real repro with literal', test_cmd='true', repro_cmd=repro)
    assert subprocess.run(['bash', '-n', str(loop / 'EVALUATOR.sh')], capture_output=True, timeout=10).returncode == 0
    remove_marker(loop)
    runner = loop / 'EVALUATOR.sh'
    runner.write_text("#!/bin/bash\nmkdir -p .herdr-loop/logs\nprintf '5 passed in 0.01s\\n' > .herdr-loop/logs/test.log\n: > .herdr-loop/logs/lint.log\necho TEST_EXIT=0\necho LINT_EXIT=0\n")
    runner.chmod(0o755)
    metrics, converged, _ = load_cli('c28b_legacy_literal', 'herdr-loop').run_evaluation(tmp_path)
    assert not converged
    assert 'goal_configuration_invalid' in metrics.details['evaluation_errors']
    assert not json.loads((loop / 'EVAL_DONE.json').read_text())['converged']


def test_modern_multiline_repro_literal_retains_real_execution_contract(tmp_path):
    repro = "true; : '\n## 评估指令配置\n- **测试命令**: `true`\n- **代码质量**: `true'"
    init_loop(tmp_path, 'modern valid literal', test_cmd="printf '5 passed in 0.01s\\n'", repro_cmd=repro)
    metrics, converged, _ = load_cli('c28b_modern_literal', 'herdr-loop').run_evaluation(tmp_path)
    assert converged, metrics.details
    assert metrics.has_repro_test
    assert metrics.details['repro_exit_code'] == 0
