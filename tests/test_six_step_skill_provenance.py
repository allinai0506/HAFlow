"""Verify six-step-finish skill vendoring integrity (AC1, T2 §7).

Guards that the repository copy exists, keeps the upstream sha256 registered
in PROVENANCE.md, stays executable, and that the vendored script's --help
smoke test succeeds.
"""
import hashlib
import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_DIR = REPO_ROOT / ".agents" / "skills" / "six-step-finish"

EXPECTED_SHA256 = {
    "scripts/finish-task.sh":
        "51c73e5bd8b91b3132b20158b13f26511b3d209ba9735de49084f8bf4da835b6",
    "SKILL.md":
        "1efd5aafb5b8226d363e903612433409a3492454dd993a150c1adaa77f213df1",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_skill_files_exist():
    assert (SKILL_DIR / "SKILL.md").is_file()
    assert (SKILL_DIR / "scripts" / "finish-task.sh").is_file()
    assert (SKILL_DIR / "skill-metadata.yml").is_file()
    assert (SKILL_DIR / "PROVENANCE.md").is_file()


def test_finish_task_sha256():
    actual = _sha256(SKILL_DIR / "scripts" / "finish-task.sh")
    assert actual == EXPECTED_SHA256["scripts/finish-task.sh"]


def test_skill_md_sha256():
    actual = _sha256(SKILL_DIR / "SKILL.md")
    assert actual == EXPECTED_SHA256["SKILL.md"]


def test_finish_task_is_executable():
    assert os.access(SKILL_DIR / "scripts" / "finish-task.sh", os.X_OK)


def test_provenance_registers_hashes():
    provenance = (SKILL_DIR / "PROVENANCE.md").read_text(encoding="utf-8")
    for digest in EXPECTED_SHA256.values():
        assert digest in provenance


def test_finish_task_help():
    result = subprocess.run(
        ["bash", str(SKILL_DIR / "scripts" / "finish-task.sh"), "--help"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "六步" in result.stdout or "step" in result.stdout.lower()
    assert "--base" in result.stdout
