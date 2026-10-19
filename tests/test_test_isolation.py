"""Guard: the suite must resolve HAFlow controller state under a throwaway HOME, never the real one.

Import-time constants (herdr.projects.ROOT, console.common.ROOT, ...) read Path.home(), so a
test run under the developer's real HOME writes projects, state and logs into the production
~/.herdr-controller. This guard fails if that isolation is lost.
"""
import os
import pwd
import tempfile
from pathlib import Path


def _tmp_root():
    return Path(tempfile.gettempdir()).resolve()


def test_home_is_a_throwaway_directory_not_the_real_home():
    real_home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    home = Path(os.environ["HOME"]).resolve()
    assert home != real_home
    assert home.is_relative_to(_tmp_root())


def test_controller_state_paths_resolve_under_throwaway_home():
    from herdr import projects
    from console import common

    assert projects.ROOT.resolve().is_relative_to(_tmp_root())
    assert common.ROOT.resolve().is_relative_to(_tmp_root())
