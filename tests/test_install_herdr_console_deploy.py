#!/usr/bin/env python3
"""`scripts/install-herdr-console.sh` 必须是**真正能生效**的部署脚本。

历史问题（lessons §108）：console 与 controller 的 launchd plist 指向
`~/.herdr-controller/releases/<commit>` 冻结快照，而旧脚本只把 console 脚本
复制到 `~/.herdr-console/` 并 `kickstart`。那份副本**永远不会被执行**，
plist 也不会被重读 —— 于是「脚本成功、进程重启、文案一个字没变」。

本文件把当前部署拓扑固化成回归门禁：
  1. release 快照按 commit 重建（`git archive`），plist 指向它；
  2. 改 plist 后必须 `bootout` + `bootstrap`（`kickstart` 不重读 plist）；
  3. 部署后自检：运行中进程的实际加载路径 == 目标 commit；
  4. 仓库内文档（service-management.md）不得再给出失效指引。
"""

import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "install-herdr-console.sh"
OPS_DOC = ROOT / "docs" / "operations" / "service-management.md"
PLIST_DIR = Path.home() / "Library" / "LaunchAgents"

# 只跑 launchd 与不改 plist 的服务（sentinel / notifier 直跑工作区源码，
# plist 指向不变，所以 kickstart 足够）
KICKSTART_ONLY = ("com.user.herdr-sentinel", "com.user.herdr-notifier")

# 需要走 release 快照的服务：改代码后工作区不生效，必须重建快照 + 重载 plist
SNAPSHOT_SERVICES = (
    "com.user.herdr-factory-console",
    "com.user.herdr-controller",
)


def _script_text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


class TestInstallScriptRebuildsReleaseSnapshot(unittest.TestCase):
    """脚本必须真正让服务加载新代码，而不是复制一份没人执行的文件。"""

    def test_script_exists_and_is_executable(self):
        self.assertTrue(SCRIPT.exists(), f"缺少部署脚本: {SCRIPT}")
        self.assertTrue(
            SCRIPT.stat().st_mode & 0o111, f"部署脚本不可执行: {SCRIPT}"
        )

    def test_rebuilds_snapshot_with_git_archive(self):
        text = _script_text()
        self.assertIn("git archive", text, "必须用 git archive 重建 release 快照")
        self.assertRegex(
            text, r'release_dir="\$\{releases_dir\}/\$\{target_sha\}"',
            "快照目录必须由 commit 变量推导（releases/<commit>），不能硬编码",
        )

    def test_points_plist_at_the_new_snapshot(self):
        text = _script_text()
        for service in SNAPSHOT_SERVICES:
            with self.subTest(service=service):
                self.assertIn(
                    service, text,
                    f"脚本未处理 {service} 的 plist；该服务跑冻结快照，"
                    f"不改 plist 就不生效",
                )
        self.assertIn(
            "plutil", text,
            "改 plist 应用 plutil（无需交互式 PlistBuddy）",
        )

    def test_console_plist_sets_herdr_root(self):
        """console 通过 HERDR_ROOT 决定 import 哪个 herdr/ 包，漏了它必然加载旧包。"""
        text = _script_text()
        self.assertRegex(
            text, r"HERDR_ROOT",
            "console plist 必须同步 HERDR_ROOT，否则 import 的是旧快照的 herdr/",
        )


class TestInstallScriptReloadsJobsCorrectly(unittest.TestCase):
    """`kickstart` 不重读 plist —— 这是 §108 实测踩了两次的坑。"""

    def test_uses_bootout_and_bootstrap_for_snapshot_services(self):
        text = _script_text()
        for verb in ("bootout", "bootstrap"):
            with self.subTest(verb=verb):
                self.assertIn(
                    f"launchctl {verb}", text,
                    f"缺少 launchctl {verb}：改 plist 后仅 kickstart 不会重读配置",
                )

    def test_does_not_rely_on_kickstart_for_snapshot_services(self):
        """对跑快照的服务，`kickstart` 单独使用等于没改。"""
        text = _script_text()
        self.assertRegex(
            text, r"bootout[\s\S]{0,400}?bootstrap",
            "bootout 与 bootstrap 必须成对出现（可隔少量其它命令）",
        )

    def test_kickstart_only_services_are_still_covered(self):
        text = _script_text()
        for service in KICKSTART_ONLY:
            with self.subTest(service=service):
                self.assertIn(
                    service, text,
                    f"{service} 直跑工作区源码，改 plist 后仍需被重载",
                )


class TestInstallScriptVerifiesDeployment(unittest.TestCase):
    """「部署成功」必须有证据，不能只看脚本 exit 0 或 PID 变化。"""

    def test_verifies_running_process_matches_target_commit(self):
        text = _script_text()
        self.assertRegex(
            text, r"ps -o command=",
            "必须核对运行中进程的实际命令行（PID 变化证明不了加载了新代码）",
        )
        self.assertRegex(
            text, r"rev-parse",
            "必须与 git rev-parse 得到的 commit 比对",
        )

    def test_keeps_previous_snapshot_for_rollback(self):
        text = _script_text()
        self.assertNotRegex(
            text, r"rm -rf[^\n]*releases/\*",
            "禁止清空 releases/ 目录：旧快照是唯一的回滚路径",
        )


class TestOpsDocMatchesReality(unittest.TestCase):
    """文档给出的是别人照着执行的命令，错了就是事故。"""

    def test_ops_doc_documents_the_real_topology(self):
        text = OPS_DOC.read_text(encoding="utf-8")
        self.assertIn(
            "releases/", text,
            "service-management.md 必须说明 console/controller 跑冻结快照",
        )
        self.assertRegex(
            text, r"bootout[\s\S]{0,400}?bootstrap",
            "service-management.md 必须给出 bootout + bootstrap 流程",
        )

    def test_ops_doc_does_not_present_stale_kickstart_only_flow(self):
        """旧的「install 脚本 + kickstart 即生效」指引必须已被替换或标注失效。

        install 脚本现在**真的会**重建快照并重载（见本文件的脚本门禁），
        所以文档不能再把它描述成"只复制副本"的失效路径，也不能只教 kickstart。
        """
        text = OPS_DOC.read_text(encoding="utf-8")
        # 若文档仍以 install 脚本为部署入口，必须同时给出 bootout+bootstrap 流程
        if "install-herdr-console.sh" in text:
            self.assertRegex(
                text, r"bootout[\s\S]{0,600}?bootstrap",
                "文档以 install 脚本为入口时，必须写明 bootout+bootstrap 流程",
            )
        # 不得出现"复制副本到 ~/.herdr-console 即生效"这类陈述
        self.assertNotRegex(
            text,
            r"只把 console 脚本复制到[^\n]*生效|install-herdr-console\.sh[^\n]*即生效",
            "文档仍把 install 脚本描述为拷贝即生效（当前拓扑下不成立）",
        )

    def test_lessons_learned_records_the_bootstrap_gotcha(self):
        text = (ROOT / "docs" / "lessons" / "lessons-learned.md").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "bootout", text, "lessons-learned 必须沉淀 kickstart 不重载 plist 的教训"
        )


@unittest.skipUnless(
    PLIST_DIR.is_dir() and any(PLIST_DIR.glob("com.user.herdr-*.plist")),
    "launchd agent 目录不可用（非 macOS 开发机或未安装）",
)
class TestInstalledPlistsMatchScriptContract(unittest.TestCase):
    """真机校验：plist 的实际结构与脚本假设的一致。"""

    def test_console_plist_has_herdr_root_and_program_arguments(self):
        plist = PLIST_DIR / "com.user.herdr-factory-console.plist"
        self.assertTrue(plist.exists(), f"未安装 console agent: {plist}")
        args = subprocess.run(
            ["plutil", "-extract", "ProgramArguments", "json", "-o", "-", str(plist)],
            capture_output=True, text=True, check=True,
        ).stdout
        parsed = __import__("json").loads(args)
        self.assertGreaterEqual(len(parsed), 2, "ProgramArguments 应为 [python, script]")
        self.assertIn("console/herdr_factory_console.py", parsed[1])

        root = subprocess.run(
            ["plutil", "-extract", "EnvironmentVariables.HERDR_ROOT", "raw",
             "-o", "-", str(plist)],
            capture_output=True, text=True,
        )
        if root.returncode == 0:
            self.assertIn(
                "releases/", root.stdout.strip(),
                "console 的 HERDR_ROOT 指向 release 快照时，脚本必须同步改它",
            )

    def test_controller_plist_points_into_a_release_snapshot(self):
        plist = PLIST_DIR / "com.user.herdr-controller.plist"
        self.assertTrue(plist.exists(), f"未安装 controller agent: {plist}")
        args = subprocess.run(
            ["plutil", "-extract", "ProgramArguments", "json", "-o", "-", str(plist)],
            capture_output=True, text=True, check=True,
        ).stdout
        parsed = __import__("json").loads(args)
        self.assertIn("services/herdr-controller.py", parsed[1])


if __name__ == "__main__":
    unittest.main()
