"""Run the real installer with isolated services under the system Bash."""
import json
import os
from pathlib import Path
import plistlib
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


def managed_scripts():
    """纳管清单只有一个权威：herdr.service_release.SCRIPTS。

    本测试曾自备一份副本，于是「加了服务但清单没同步」在这里根本测不出来。
    """
    from herdr.service_release import SCRIPTS
    return {label: spec[0] for label, spec in SCRIPTS.items()}


def test_service_authority_is_not_copied_into_this_test():
    from herdr.service_release import SCRIPTS
    assert set(managed_scripts()) == set(SCRIPTS)
    assert "com.user.herdr-experience-hub" in SCRIPTS, \
        "带参数的服务被排除出纳管清单：它会被永久钉在旧 release 上"


@pytest.mark.parametrize("layout", ["release", "workspace", "mixed", "none"])
@pytest.mark.parametrize("restart", [True, False])
def test_installer_handles_empty_service_groups(tmp_path, layout, restart):
    tmp_path = tmp_path / "home with spaces and 'quote'"
    tmp_path.mkdir()
    sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    agents = tmp_path / "Library/LaunchAgents"
    agents.mkdir(parents=True)
    services = managed_scripts()
    expected = {}
    for index, (label, relative) in enumerate(services.items()):
        if layout == "none":
            continue
        release = layout == "release" or (layout == "mixed" and index % 2 == 0)
        old_root = tmp_path / ("releases/old" if release else "workspace")
        script = old_root / relative
        script.parent.mkdir(parents=True, exist_ok=True)
        script.touch()
        (agents / (label + ".plist")).write_bytes(plistlib.dumps({
            "ProgramArguments": ["/usr/bin/python3", str(script)],
        }))
        expected[label] = (
            tmp_path / ".herdr-controller/releases" / sha if release else old_root
        )

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    log = tmp_path / "launchctl.jsonl"
    launchctl = fake_bin / "launchctl"
    launchctl.write_text("""#!/usr/bin/env python3
import json, os, sys
with open(os.environ['FAKE_LAUNCHCTL_LOG'], 'a') as log:
    log.write(json.dumps(sys.argv[1:]) + '\\n')
if sys.argv[1:] == ['list']:
    for label in json.loads(os.environ['FAKE_SERVICE_LABELS']):
        print('777 0 ' + label)
""")
    launchctl.chmod(0o755)
    for name, content in {
        "sleep": "#!/bin/sh\nexit 0\n",
        "ps": "#!/bin/sh\nprintf '%s\\n' \"$FAKE_PROCESS_SHA\"\n",
    }.items():
        executable = fake_bin / name
        executable.write_text(content)
        executable.chmod(0o755)
    env = dict(os.environ, HOME=str(tmp_path),
               PATH=str(fake_bin) + os.pathsep + os.environ["PATH"],
               FAKE_LAUNCHCTL_LOG=str(log),
               FAKE_SERVICE_LABELS=json.dumps(list(expected)),
               FAKE_PROCESS_SHA=sha)
    result = subprocess.run(
        ["/bin/bash", str(ROOT / "scripts/install-herdr-console.sh"), "--sha", sha]
        + ([] if restart else ["--no-restart"]),
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    domain = "gui/" + str(os.getuid())
    release_labels = [label for label, root in expected.items() if root.name == sha]
    assert [call for call in calls if call[0] == "bootout"] == [
        ["bootout", domain + "/" + label] for label in release_labels if restart
    ]
    assert sorted(call[2] for call in calls if call[0] == "bootstrap") == sorted(
        str(agents / (label + ".plist")) for label in expected if restart
    )
    assert not any(call[0] == "kickstart" for call in calls)
    if not restart:
        assert not calls
    for label, root in expected.items():
        data = plistlib.loads((agents / (label + ".plist")).read_bytes())
        assert data["ProgramArguments"] == ["/usr/bin/python3", str(root / services[label])]
        assert data["EnvironmentVariables"]["HERDR_ROOT"] == str(root)
