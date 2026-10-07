"""Selective Reverification v1 controller-integration tests (HAFlow PR #108).

验证真实调用链:controller sweep -> freeze rotation -> reverification plan
-> 持久事实 -> 复用节点不派发 -> join gate 消费 reuse。git 使用真实临时仓库,
非 git 的 subprocess 被拦截以便断言 launch argv。
"""
import importlib.machinery
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERDR_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERDR_ROOT))

from herdr import reverification as reverification_mod  # noqa: E402
from herdr import reverification as rv  # noqa: E402
from herdr import scheduler_facts as facts  # noqa: E402

REPO_ROOT = HERDR_ROOT
TEMPLATE = "software-development-v1"


def _load_module(name, path):
    spec = importlib.util.spec_from_loader(
        name, importlib.machinery.SourceFileLoader(name, str(path)))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_ctl = _load_module(
    "herdr_controller_reverification_integration_test",
    REPO_ROOT / "services" / "herdr-controller.py",
)

WF = "wf-rever-ctl"


def _reuse_fact(workflow_id, verifier, candidate_sha, db_path, episode_id=None):
    """Look up a reuse fact under the policy AND freeze episode that made it.

    ``find_reuse_fact`` is fail-closed on both, so a call that omits one asks
    "is there a fact under *no* policy / *no* episode?", which is correctly
    None. Tests asking "is the current episode covered?" pass both.
    """
    from herdr.workflow import load_template
    if episode_id is None:
        freezes = facts.list_candidate_frozen_events(workflow_id, db_path=db_path)
        episode_id = freezes[-1].get("id") if freezes else None
    return facts.find_reuse_fact(
        workflow_id, verifier, candidate_sha,
        policy_identity=reverification_mod.policy_identity(
            reverification_mod.policy_from_workflow(load_template(
                str(REPO_ROOT / "workflow_templates"
                    / "software-development-v1.yaml")))),
        episode_id=episode_id,
        db_path=db_path)


def _git(repo, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(repo)] + list(args), check=check,
        text=True, capture_output=True)


class ControllerReverificationTest(unittest.TestCase):
    """A -> B rotation driven through the real sweep."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="herdr-rever-ctl-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q", ".")
        _git(self.repo, "config", "user.email", "t@t")
        _git(self.repo, "config", "user.name", "t")
        _git(self.repo, "commit", "-q", "--allow-empty", "-m", "root",
             "--no-gpg-sign")
        # A second commit so `A~1` exists: the non-linear-history test needs a
        # real parent to branch away from.
        self.write("seed.txt", "seed")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "seed", "--no-gpg-sign")

        self.docs = self.root / "wdocs"
        self.docs.mkdir()
        self.db = self.root / "state.db"
        self.state_file = self.root / "stage-state.json"
        # Must keep the real suffix: herdr.workflow._load_file picks its parser
        # from the extension, so a YAML template named *.json would be parsed
        # as JSON and the test would fail for the wrong reason.
        self.workflow_file = self.root / "workflow.yaml"
        self._write_workflow_file()

        env = patch.dict(os.environ, {
            "HERDR_WORKFLOW_DOCS_DIR": str(self.docs),
            "HERDR_STATE_DB": str(self.db),
            "TASKS_FILE": str(self.root / "tasks.json"),
            "WORKFLOWS_FILE": str(self.workflow_file),
        })
        env.start()
        self.addCleanup(env.stop)

        # STAGE_STATE_FILE is read into a module constant at *import* time
        # (services/herdr-controller.py), so setting it in os.environ above has
        # no effect. The latch path must be redirected on the controller module
        # itself; otherwise a real sweep writes this test's workflow into the
        # operator's ~/.herdr-controller/stage-state.json. Patched here rather
        # than relying on the stubbed latch helpers so the isolation holds even
        # if a test stops stubbing them.
        self._patch("_ctl.STAGE_STATE_FILE", str(self.state_file))
        policies = self.root / "policies.json"
        policies.write_text("{}", encoding="utf-8")
        self._patch("_ctl.STAGE_POLICIES_FILE", str(policies))

        from herdr.state_store import get_state_store
        self.store = get_state_store(self.db)
        self.store.save_workflow({"workflow_id": WF, "status": "running", "execution_id": "gen", "config": self._workflow_cfg()})

        self.launches = []
        self.queue = []
        real_run = subprocess.run

        def fake_run(cmd, **kwargs):
            if cmd and cmd[0] == "git":
                return real_run(cmd, **kwargs)
            argv = [str(c) for c in (cmd or [])]
            if len(argv) > 1 and argv[1] == "launch":
                self.launches.append(argv)
                # External transport is substituted; dispatch evidence must remain real.
                from herdr.task_resources import begin_launch_intent
                def arg(name, default=None):
                    return argv[argv.index(name) + 1] if name in argv else default
                task_id = arg('--task-id')
                node_id = arg('--node')
                run_id = 'run-' + task_id + '-' + (arg('--candidate-sha', '') or '')[:12]
                op_id = int(arg('--dispatch-operation-id')) if '--dispatch-operation-id' in argv else None
                claim = begin_launch_intent(self.store, workflow_id=WF, node_id=node_id,
                    task_id=task_id, role=arg('--dispatch-role', 'worker'),
                    dispatch_round=int(arg('--dispatch-round', '1')),
                    supersedes=arg('--supersedes'), candidate_sha=arg('--candidate-sha', ''),
                    dispatch_operation_id=op_id, execution_id='gen', run_id=run_id)
                assert 'intent' in claim, (argv, claim)
                intent = claim['intent']
                self.store.save_task({'task_id': task_id, 'workflow_id': WF, 'node': node_id,
                    'stage': node_id, 'status': 'pending', 'run_id': run_id, 'execution_id': 'gen',
                    'dispatch_operation_id': op_id, 'launch_intent_id': intent['intent_id'],
                    'candidate_sha': arg('--candidate-sha'), 'dispatch_role': arg('--dispatch-role', 'worker'),
                    'dispatch_round': int(arg('--dispatch-round', '1')), 'supersedes': arg('--supersedes')})
                from herdr.task_resources import finish_launch_intent
                finish_launch_intent(self.store, intent)
                if arg('--supersedes'):
                    self.store.transition_task(arg('--supersedes'), 'superseded',
                        'replacement registered and delivered',
                        metadata={'superseded_by': task_id, 'replacement_pending': True}, force=True)
            return subprocess.CompletedProcess(cmd, 0, "Task dispatched: x", "")

        # Replace the controller's `subprocess` binding with a shim, not with a
        # bare function: the controller also reaches for subprocess.TimeoutExpired
        # and friends, so the module surface has to stay intact. Only `run` is
        # intercepted; git still executes for real.
        self._patch("_ctl.subprocess", _SubprocessShim(fake_run))

        self._patched = []
        self._patch("_ctl.coordinator_queue", _Queue(self.queue))
        self._patch("_ctl.project_for_workflow", lambda wf: {
            "execution_id": "gen",
            "project_root": str(self.repo),
            "base_branch": _git(self.repo, "rev-parse",
                                "--abbrev-ref", "HEAD").stdout.strip(),
            "coordinator_pane_id": "1:1",
            "startup_ready": True,
            "requirement": "reverification",
        })
        self._patch("_ctl.coordinator_pane_for_workflow", lambda wf: "1:1")
        self._patch("_ctl.workflow_config_for", lambda wf: self._workflow_cfg())
        self._patch("_ctl.load_tasks", lambda: self.store.list_tasks())
        self._patch("_ctl.maybe_dispatch_node_handoffs",
                    lambda **kwargs: [])
        self._patch("_ctl.maybe_compact_coordinator", lambda *a, **k: None)
        self._patch("_ctl.shared_docs_block", lambda *a, **k: "")
        self._patch("_ctl.node_is_gate", lambda wf, node: False)
        self._patch("_ctl.notify_attention", lambda *a, **k: None)
        self._patch("_ctl.attention_get", lambda key: None)
        self._patch("_ctl.attention_note", lambda *a, **k: None)
        self._patch("_ctl.attention_blocks_retry", lambda key: False)
        self._patch("_ctl.workflow_closed", lambda wf: False)
        self._patch("_ctl._workflow_entry",
                    lambda wf: {"status": "running"})
        self._patch("_ctl.is_workflow_completed", lambda cfg, done: False)
        self._patch("_ctl.maybe_close_completed_workflow", lambda wf: None)
        self._patch("_ctl._dispatch_candidate_ready",
                    lambda root, base, specs, workflow_id=None: True)
        self._patch("_ctl._fix_loop_latch_blocks", lambda *a: False)
        self._patch("_ctl.blocked_gate_dependency", lambda *a: None)
        self._patch("_ctl.blocked_verdict_dep", lambda *a: None)

    def _patch(self, target, value):
        owner, _, attr = target.rpartition(".")
        module = _ctl if owner == "_ctl" else __import__(owner, fromlist=["_"])
        patcher = patch.object(module, attr, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write_workflow_file(self):
        import shutil
        shutil.copy(
            REPO_ROOT / "workflow_templates" / f"{TEMPLATE}.yaml",
            self.workflow_file,
        )

    def _workflow_cfg(self):
        from herdr.workflow import load_template
        return load_template(str(self.workflow_file))

    # -- repo helpers ----------------------------------------------------

    def write(self, rel, text="x"):
        target = self.repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def commit(self, message="c"):
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", message, "--no-gpg-sign")
        return self.head

    @property
    def head(self):
        return _git(self.repo, "rev-parse", "HEAD").stdout.strip()

    def save_task(self, **overrides):
        """Create or update a task, MERGING into any existing record.

        ``state_db.save_task`` upserts the columns it is given and rebuilds
        ``payload_json`` from the rest — it does not merge. A partial update
        would therefore silently drop ``workflow_id`` (landing the row under
        "default", where no query would find it) and every later assertion would
        fail for a reason that has nothing to do with the behaviour under test.
        """
        task_id = overrides.get("task_id")
        record = dict(self.store.get_task(task_id) or {}) if task_id else {}
        record.update({
            "workflow_id": WF, "status": "completed", "stage_verdict": "pass",
            "node": "implementation", "stage": "implementation",
        })
        record.update(overrides)
        record.setdefault('execution_id', 'gen')
        record.setdefault('run_id', 'seed-' + task_id)
        record.setdefault('dispatch_role', 'worker')
        record.setdefault('dispatch_round', 1)
        self.store.save_task(record)

    def supersede(self, task_id, by):
        """Model fix-loop: the previous verifier task stops being active.

        Required for a faithful A -> B test. In the real loop
        ``invalidate_for_fix_loop`` supersedes the old test/review tasks, which
        is what makes the test node ready again. Leaving them completed would
        make the node permanently satisfied and the sweep would never re-freeze
        a candidate, so no rotation would ever be observed.

        Every other field is preserved, because superseding is a status change,
        not a rewrite: the record of *what that task verified* must survive,
        which is exactly what makes it usable as reuse evidence later.
        """
        self.store.transition_task(task_id, "superseded", "fix-loop invalidation",
                                   metadata={"superseded_by": None, "replacement_pending": True}, force=True)

    def rotate(self, rel, text="x"):
        """Start a rework round the way fix-loop does; returns the new SHA.

        Three effects, all load-bearing for a faithful test:

        1. The test node's tasks are superseded — each rework round invalidates
           the current conclusion, so an older test task left active would keep
           the node looking complete.
        2. Implementation goes back to ``running``. This is what actually
           re-arms the stage latch: ``reconcile_stage_advance_states`` revokes a
           ``notified`` node only when a *predecessor* regresses. Modelling a
           rework as "instantly completed" would leave the latch from the
           previous round in place and the rotation would never dispatch
           anything — a test that passes only because the latch was stubbed out.
        3. A new implementation task is created but left pending; call
           :meth:`finish_rework` to complete it.
        """
        if rel:
            self.write(rel, text)
            new = self.commit(f"change:{rel}")
        else:
            new = self.head
        self._begin_rework()
        return new

    def _begin_rework(self):
        impl_id = f"{WF}-impl-{_bump()}"
        for task in self.store.list_tasks(workflow_id=WF):
            node = str(task.get("node") or task.get("stage") or "")
            if node in ("test", "review") and task.get("status") != "superseded":
                self.supersede(str(task.get("task_id") or ""), impl_id)
            if node == "implementation" and task.get("status") in (
                    "completed", "committed", "integrated", "cleanup_ready",
                    "cleaned"):
                self.save_task(task_id=str(task.get("task_id")),
                               status="running", stage_verdict="")
        self.save_task(task_id=impl_id, node="implementation",
                       stage="implementation", status="working",
                       branch="main", updated_at=_bump())
        self._pending_impl = impl_id
        # The controller sweeps every 2s, so it observes the regression while
        # the rework is still running — that observation is what revokes the
        # stage latch. Skipping it here would model a rework that never took
        # effect, because the latch would still be set when the rework finished.
        self._sweep()

    def rollback_to(self, sha):
        """Reset the delivery branch to an earlier commit and rework from there.

        Models the rollback the spec names explicitly: the branch is moved back
        so the frozen candidate becomes a SHA that was already frozen in an
        earlier episode. No new commit is made — the point is that the candidate
        is byte-identical to one already in the ledger.
        """
        _git(self.repo, "reset", "-q", "--hard", sha)
        self._begin_rework()

    def finish_rework(self):
        """The rework agent finishes: implementation is complete again.

        Every active implementation task is completed, not just the newest one,
        because node completion is an AND over all active tasks — leaving an
        earlier one at ``running`` would keep the node permanently incomplete
        and no candidate would ever be frozen.
        """
        for task in self.store.list_tasks(workflow_id=WF):
            node = str(task.get("node") or task.get("stage") or "")
            if (node == "implementation"
                    and task.get("status") not in ("superseded",)
                    and not task.get("superseded_by")):
                self.save_task(task_id=str(task.get("task_id")),
                               status="completed", stage_verdict="pass",
                               updated_at=_bump())

        facts.record_candidate_frozen(WF, self.head, db_path=self.db)

    def _policy_file(self, test_scope):
        """A workflow file whose `test` scope is `test_scope` (None = no block)."""
        path = self.root / f"policy-{abs(hash(tuple(test_scope or ())))}.yaml"
        block = ""
        if test_scope is not None:
            block = ("reverification:\n"
                     "  version: selective-reverification-v1\n"
                     "  test:\n    reusable_only_if_changes_within:\n"
                     + "".join(f"      - \"{p}\"\n" for p in test_scope)
                     + "  review:\n    reusable_only_if_changes_within: []\n")
        path.write_text(
            "name: software-development-v1\n"
            "nodes:\n"
            "  - id: requirements\n    depends_on: []\n"
            "  - id: plan\n    depends_on: [requirements]\n"
            "  - id: implementation\n    depends_on: [plan]\n"
            "  - id: test\n    depends_on: [implementation]\n"
            "  - id: review\n    depends_on: [implementation]\n"
            "  - id: wrapup\n    depends_on: [test, review]\n"
            + block,
            encoding="utf-8")
        return path

    @staticmethod
    def _load_cfg(path):
        from herdr.workflow import load_template
        return load_template(str(path))

    def launched_nodes(self):
        nodes = []
        for argv in self.launches:
            if "--node" in argv:
                nodes.append(argv[argv.index("--node") + 1])
        return nodes

    # -- scenarios -------------------------------------------------------

    def _seed_implementation_done(self):
        for node in ("requirements", "plan"):
            self.save_task(task_id=f"{WF}-{node}", node=node, stage=node)
        self.save_task(task_id=f"{WF}-impl", branch="main")

    def _bootstrap_passed_candidate(self):
        """Freeze candidate A and record a genuine test(A) PASS.

        Models the state the rework starts from: implementation done, candidate
        A frozen, test(A) proved A.
        """
        self._seed_implementation_done()
        a = self.head
        facts.record_candidate_frozen(WF, a, db_path=self.db)
        self._sweep()
        self.assertEqual(_ctl._scheduler_freeze_candidate(
            WF, str(self.repo), "implementation", ["implementation"]), a)
        self.save_task(task_id=f"{WF}-test-auto", node="test", stage="test",
                       candidate_sha=a, verified_candidate_sha=a,
                       status="completed", stage_verdict="pass",
                       updated_at=_bump())
        return a

    def _sweep(self):
        """One full scheduling cycle: sweep, then drain what it queued.

        The sweep only decides *which nodes are ready* and enqueues them; the
        real Task creation happens in the coordinator's direct-dispatch path.
        Driving both is what makes "no test Task was created" a statement about
        the actual launch path rather than about a queue length.
        """
        self.launches.clear()
        self.queue.clear()
        _ctl.check_workflow_stage_advance(WF)
        for item in list(self.queue):
            if item.get("kind") == "stage_advance":
                _ctl._handle_coordinator_item(item)

    def test_first_freeze_creates_no_reverification_plan(self):
        """首个候选没有 A -> B episode,不应产生任何重新验证决策。"""
        self._seed_implementation_done()
        _ctl.check_workflow_stage_advance(WF)
        self.assertEqual(
            facts.list_reverification_decisions(WF, db_path=self.db), [])

    def test_docs_only_rotation_reuses_test_and_dispatches_review(self):
        """§19/§31 Case 1: 只改 docs -> 不建 test Task,只派 review。"""
        a = self._bootstrap_passed_candidate()
        b = self.rotate("docs/user-guide.md", "guide")
        self.finish_rework()
        self._sweep()

        decisions = {
            e["payload"]["verifier"]: e["payload"]["decision"]
            for e in facts.list_reverification_decisions(WF, db_path=self.db)
        }
        self.assertEqual(decisions.get("test"), rv.DECISION_REUSE)
        self.assertEqual(decisions.get("review"), rv.DECISION_RERUN)
        self.assertNotIn("test", self.launched_nodes())
        self.assertIn("review", self.launched_nodes())
        self.assertEqual(
            _reuse_fact(WF, "test", b, self.db)[
                "source_verified_candidate_sha"], a)

    def test_code_rotation_reruns_both_verifiers(self):
        """§31 Case 2: 代码变化 -> test 也必须真的派发。"""
        a = self._bootstrap_passed_candidate()
        b = self.rotate("herdr/scheduler.py", "code")
        self.finish_rework()
        self._sweep()

        self.assertIsNone(_reuse_fact(WF, "test", b, self.db))
        self.assertIn("test", self.launched_nodes())
        del a

    def test_reused_node_counts_as_complete_and_gate_passes(self):
        """§20: 复用节点没有 Task,但必须算完成并让门禁放行。"""
        a = self._bootstrap_passed_candidate()
        b = self.rotate("docs/a.md", "d")
        self.finish_rework()
        self._sweep()
        current_review = next(argv[argv.index('--task-id') + 1] for argv in self.launches
                              if argv[argv.index('--node') + 1] == 'review')
        self.save_task(task_id=current_review, node="review",
                       stage="review", status="completed", stage_verdict="pass",
                       candidate_sha=b, verified_candidate_sha=b,
                       updated_at=_bump())

        self.assertTrue(_ctl.is_node_complete(WF, "test"))
        self.assertTrue(_ctl._scheduler_join_gate_allows(
            WF, {"id": "wrapup", "depends_on": ["test", "review"]},
            self.store.list_tasks()))
        del a

    def test_steady_state_sweeps_do_not_recompute_the_plan(self):
        """§4.8: a settled episode must not re-derive itself every 2 seconds.

        A rotation stays the current rotation until a *third* candidate is
        frozen, so without a settled-episode check the controller would re-run
        canonicalise + merge-base + diff on every sweep forever for a decision
        that is already immutable on disk.
        """
        self._bootstrap_passed_candidate()
        self.rotate("docs/a.md", "d")
        self.finish_rework()
        self._sweep()
        baseline = len(facts.list_reverification_decisions(WF, db_path=self.db))
        self.assertEqual(baseline, 2)

        calls = []
        real = reverification_mod.collect_candidate_changes

        def counting(repo, from_sha, to_sha, timeout=10):
            calls.append((from_sha, to_sha))
            return real(repo, from_sha, to_sha, timeout=timeout)

        self._patch("_ctl.reverification_mod", _CountingModule(counting))
        for _ in range(5):
            self._sweep()

        self.assertEqual(calls, [],
                         "a settled episode must not re-run the git diff")
        self.assertEqual(
            len(facts.list_reverification_decisions(WF, db_path=self.db)),
            baseline,
            "steady-state sweeps must not add facts either",
        )

    def test_narrowing_the_scope_revokes_an_existing_reuse_fact(self):
        """A fact is a claim made *under* a policy, so tightening revokes it.

        The version string is a human label and does not change when the scope
        does. Keying reuse on it would make narrowing purely cosmetic: the fact
        recorded under `docs/**/*.md` would keep being honoured after the
        operator narrowed it to `docs/adr/**`, and the corrective decision could
        not even be written.
        """
        self._bootstrap_passed_candidate()
        b = self.rotate("docs/a.md", "d")
        self.finish_rework()
        self._sweep()
        self.assertTrue(_ctl.is_node_complete(WF, "test"),
                        "precondition: the wide policy reuses test")

        self._patch("_ctl.workflow_config_for",
                    lambda wf: self._load_cfg(self._policy_file(
                        ["docs/adr/**"])))
        _ctl._reset_reverification_memo(WF)

        self.assertFalse(_ctl.is_node_complete(WF, "test"),
                         "narrowing the scope must revoke the reuse fact")
        self.assertFalse(_ctl._reverification_reused_node(WF, "test"))
        self.assertIsNone(_ctl._reverification_gate_facts(WF) or None)
        del b

    def test_removing_the_policy_block_revokes_reuse(self):
        """Turning the feature off must actually turn it off."""
        self._bootstrap_passed_candidate()
        self.rotate("docs/a.md", "d")
        self.finish_rework()
        self._sweep()
        self.assertTrue(_ctl.is_node_complete(WF, "test"))

        self._patch("_ctl.workflow_config_for",
                    lambda wf: self._load_cfg(self._policy_file(None)))
        _ctl._reset_reverification_memo(WF)
        self.assertFalse(_ctl.is_node_complete(WF, "test"))

    def test_absent_scheduler_does_not_crash_node_completion(self):
        """#107's degradation contract: the scheduler is optional.

        ``is_node_complete`` used to be pure task-status logic with no
        scheduler dependency. Reading ``scheduler_core.EFFECTIVE_REUSE``
        unguarded would turn "herdr.scheduler failed to import" into an
        AttributeError on the hot path, aborting the sweep every 2 seconds.
        """
        self._seed_implementation_done()
        self._patch("_ctl.scheduler_core", None)
        self.assertFalse(_ctl.is_node_complete(WF, "test"))
        # The whole sweep must still run rather than raising.
        _ctl.check_workflow_stage_advance(WF)

    def test_case13_second_sweep_creates_nothing_new(self):
        """§24/§31 Case 13: 崩溃重启后不得重复建 Task 或重复写事实。"""
        self._bootstrap_passed_candidate()
        b = self.rotate("docs/a.md", "d")
        self.finish_rework()
        self._sweep()
        first = len(facts.list_reverification_decisions(WF, db_path=self.db))
        self.assertEqual(first, 2)

        self._sweep()
        self.assertEqual(
            len(facts.list_reverification_decisions(WF, db_path=self.db)),
            first,
            "a second sweep must not write a duplicate reuse fact",
        )
        self.assertNotIn("test", self.launched_nodes())
        self.assertIsNotNone(_reuse_fact(WF, "test", b, self.db))

    def test_rotation_to_a_third_candidate_drops_the_old_reuse_fact(self):
        """§23: B -> C 之后 A -> B 的复用不再满足当前候选。"""
        self._bootstrap_passed_candidate()
        b = self.rotate("docs/a.md", "1")
        self.finish_rework()
        self._sweep()
        self.assertIsNotNone(_reuse_fact(WF, "test", b, self.db))
        self.assertTrue(_ctl.is_node_complete(WF, "test"))

        c = self.rotate("docs/a.md", "2")
        self.finish_rework()
        self._sweep()
        self.assertIsNotNone(
            facts.latest_frozen_candidate_sha(WF, db_path=self.db))
        self.assertIsNone(
            _reuse_fact(WF, "test", c, self.db),
            "an A -> B fact must not satisfy candidate C",
        )
        self.assertIn("test", self.launched_nodes(),
                      "candidate C must re-run the reused verifier")
        self.assertFalse(_ctl.is_node_complete(WF, "test"))

    def test_completed_workflow_still_closes_when_freeze_is_unresolvable(self):
        """§38 "旧 Workflow 保持兼容": completion must not be blocked by #108.

        Hoisting the freeze above `is_workflow_completed` made an unresolvable
        candidate identity abort the whole sweep, so a fully finished workflow
        could never close and printed FREEZE DEFERRED every 2s forever. The
        freeze is a dispatcher concern: once every node is done there is nothing
        left to dispatch, so it must not gate the terminal transition.
        """
        self._bootstrap_passed_candidate()
        closed = []
        self._patch("_ctl.maybe_close_completed_workflow",
                    lambda wf: closed.append(wf))
        self._patch("_ctl.is_workflow_completed", lambda cfg, done: True)
        self._patch("_ctl.resolve_gate_config", lambda node, nid: None)

        broken = self.root / "not-a-repo"
        broken.mkdir()
        self._patch("_ctl.project_for_workflow", lambda wf: {
            "project_root": str(broken), "base_branch": "main",
            "coordinator_pane_id": "1:1", "startup_ready": True,
            "requirement": "reverification",
        })
        _ctl.check_workflow_stage_advance(WF)
        self.assertEqual(closed, [WF],
                         "a completed workflow must still reach close")

    def test_unresolvable_candidate_defers_every_ready_node_this_sweep(self):
        """§8: with no provable candidate, nothing is latched or dispatched.

        Behaviour-preserving against #107, where a freeze failure also skipped
        every ready node in that pass. Asserted explicitly so the documented
        contract matches the code instead of being inferred from a sweep log.
        """
        self._seed_implementation_done()
        broken = self.root / "not-a-repo"
        broken.mkdir()
        self._patch("_ctl.project_for_workflow", lambda wf: {
            "project_root": str(broken), "base_branch": "main",
            "coordinator_pane_id": "1:1", "startup_ready": True,
            "requirement": "reverification",
        })
        self._sweep()
        self.assertEqual(self.launches, [])
        self.assertEqual(
            facts.list_reverification_decisions(WF, db_path=self.db), [])
        # Nothing was latched either, so the next sweep retries rather than
        # staying silently suppressed.
        self.assertTrue(_ctl.mark_stage_advance_queued(WF, "test"),
                        "a deferred stage must not already be latched")

    def test_unreadable_repository_defers_instead_of_reusing(self):
        """§8: 仓库不可读时既不产生 reuse 事实,也不放行派发。

        读不到候选身份时控制器走 #107 已有的 FREEZE DEFERRED 路径:不闩、
        不派发,下轮 sweep 重估。测试代码变化时不会跑在「随便某个 revision」上。
        """
        self._bootstrap_passed_candidate()
        self.rotate("docs/a.md", "d")
        self.finish_rework()

        broken = self.root / "not-a-repo"
        broken.mkdir()
        self._patch("_ctl.project_for_workflow", lambda wf: {
            "project_root": str(broken), "base_branch": "main",
            "coordinator_pane_id": "1:1", "startup_ready": True,
            "requirement": "reverification",
        })
        self._sweep()

        self.assertEqual(
            facts.list_reverification_decisions(WF, db_path=self.db), [],
            "no decision may be recorded when git facts are unavailable",
        )
        self.assertEqual(self.launches, [],
                         "no verifier may be dispatched without a candidate")

    def test_non_linear_history_reruns_everything(self):
        """§16/§31 Case 8: 非线性候选历史一律 RERUN。"""
        a = self._bootstrap_passed_candidate()
        # Branch from a's PARENT and commit there, so the new candidate's
        # history does not contain A at all. This is what a force push, a branch
        # switch, or a rollback onto a divergent branch looks like.
        _git(self.repo, "checkout", "-q", "-b", "side", f"{a}~1")
        self.write("herdr/right.py", "r")
        right = self.commit("right")
        _git(self.repo, "checkout", "-q", "-")
        # Sanity: git itself must report the two as unrelated, otherwise the
        # test would be asserting RERUN for the wrong reason.
        self.assertNotEqual(
            subprocess.run(["git", "-C", str(self.repo), "merge-base",
                            "--is-ancestor", a, right]).returncode, 0)
        self.supersede(f"{WF}-test-auto", f"{WF}-impl-side")
        self.save_task(task_id=f"{WF}-impl-side", node="implementation",
                       stage="implementation", branch="side", updated_at=_bump())
        facts.record_candidate_frozen(WF, right, db_path=self.db)

        self._sweep()
        self.assertIsNone(
            _reuse_fact(WF, "test", right, self.db))
        reasons = {
            e["payload"]["verifier"]: e["payload"]["reason"]
            for e in facts.list_reverification_decisions(WF, db_path=self.db)
            if e["payload"]["from_candidate_sha"] == a
        }
        self.assertEqual(reasons, {
            "test": rv.REASON_RERUN_NON_LINEAR,
            "review": rv.REASON_RERUN_NON_LINEAR,
        })

    def test_case14_aba_rotation_is_recomputed(self):
        """§26/§31 Case 14: A -> B -> A 必须按新 episode 重算。"""
        a = self._bootstrap_passed_candidate()
        b = self.rotate("docs/a.md", "1")
        self.finish_rework()
        self._sweep()
        self.assertIsNotNone(_reuse_fact(WF, "test", b, self.db))

        # test(B) really ran, then a further change produces C.
        self.save_task(task_id=f"{WF}-test-auto-2", node="test", stage="test",
                       candidate_sha=b, verified_candidate_sha=b,
                       status="completed", stage_verdict="pass",
                       updated_at=_bump())
        c = self.rotate("docs/a.md", "0")
        self.finish_rework()
        self._sweep()

        events = facts.list_reverification_decisions(WF, db_path=self.db)
        pairs = {(e["payload"]["from_candidate_sha"],
                  e["payload"]["to_candidate_sha"],
                  e["payload"]["verifier"]) for e in events}
        self.assertIn((a, b, "test"), pairs)
        self.assertIn((b, c, "test"), pairs)
        self.assertEqual(
            len({e["payload"]["decision_identity"] for e in events}),
            len(events),
            "every episode must have its own decision identity",
        )

    def test_rollback_to_a_previous_candidate_cannot_resurrect_old_reuse(self):
        """P1: a reuse fact must not survive into a different candidate episode.

        A -> B reuses test. B -> C re-runs it. Then the implementation branch
        is reset so the candidate is B *again* — the rollback case the spec
        names explicitly. The C -> B episode is non-linear, so every verifier
        must re-run. But the ledger still holds the first A -> B fact, and a
        lookup keyed only on (verifier, to_candidate_sha) would find it: B is
        B, so the stale fact would resurrect, mark test complete, and B would
        never actually be tested.

        Binding the fact to the candidate-freeze episode — not to the SHA — is
        what makes "facts do not cross episodes" true.
        """
        # Round 1: A -> B, docs only, test reuses.
        a = self._bootstrap_passed_candidate()
        b = self.rotate("docs/a.md", "1")
        self.finish_rework()
        self._sweep()
        self.assertIsNotNone(_reuse_fact(WF, "test", b, self.db))
        self.assertTrue(_ctl.is_node_complete(WF, "test"))
        first_episode = self._current_episode_id()

        # Round 2: B -> C, code change, so test must re-run.
        self.save_task(task_id=f"{WF}-test-c", node="test", stage="test",
                       candidate_sha=b, verified_candidate_sha=b,
                       status="completed", stage_verdict="pass",
                       supersedes=f"{WF}-test-auto", dispatch_round=2,
                       updated_at=_bump())
        # The fresh B verification replaces the prior A lineage; it is not a
        # second worker with the same role/round dispatch identity.
        self.store.transition_task(f"{WF}-test-auto", "superseded", "fresh B verification",
                                   metadata={"superseded_by": f"{WF}-test-c"}, force=True)
        c = self.rotate("herdr/scheduler.py", "code")
        self.finish_rework()
        self._sweep()
        self.assertIsNone(_reuse_fact(WF, "test", c, self.db),
                          "precondition: C decided RERUN, so no reuse fact")
        second_episode = self._current_episode_id()
        self.assertNotEqual(first_episode, second_episode)
        self.assertFalse(_ctl.is_node_complete(WF, "test"))

        # Round 3: the implementation branch is reset to B, so the candidate is
        # B again. C is not an ancestor of B, so this rotation is non-linear
        # and every verifier must re-run — including test.
        self.rollback_to(b)
        self.finish_rework()
        self._sweep()
        self.assertEqual(
            self._current_candidate(), b,
            "precondition: the candidate really is B again")
        # The A -> B fact is still in the ledger — it was correctly *ignored*,
        # not deleted. A test that passed because the fact had vanished would not
        # be testing episode binding at all.
        stale = [e["payload"] for e in
                 facts.list_reverification_decisions(WF, db_path=self.db)
                 if e["payload"]["decision"] == "reuse"
                 and e["payload"]["from_candidate_sha"] == a
                 and e["payload"]["to_candidate_sha"] == b]
        self.assertEqual(len(stale), 1,
                         "precondition: the A -> B reuse fact still exists")
        self.assertNotEqual(
            stale[0]["candidate_frozen_event_id"],
            self._current_episode_id(),
            "the fact must belong to the earlier freeze")
        self.assertNotEqual(self._current_episode_id(), first_episode)
        self.assertIn(
            "test", self.launched_nodes(),
            "test(B) must be re-dispatched: the A->B fact belongs to a "
            "different candidate episode and must not resurrect",
        )

    def _current_candidate(self):
        return facts.latest_frozen_candidate_sha(WF, db_path=self.db)

    def _current_episode_id(self):
        events = facts.list_candidate_frozen_events(WF, db_path=self.db)
        self.assertTrue(events, "precondition: a candidate has been frozen")
        return events[-1].get("id")

    def test_case9_blocked_previous_verdict_cannot_be_reused(self):
        """§31 Case 9: test(A) BLOCKED 时 docs-only 也不得复用。"""
        self._seed_implementation_done()
        a = self.head
        self._sweep()
        _ctl._scheduler_freeze_candidate(
            WF, str(self.repo), "implementation", ["implementation"])
        self.save_task(task_id=f"{WF}-test-auto", node="test", stage="test",
                       candidate_sha=a, verified_candidate_sha=a,
                       status="completed", stage_verdict="blocked",
                       updated_at=_bump())
        b = self.rotate("docs/a.md", "d")
        self.finish_rework()
        self._sweep()

        self.assertIsNone(_reuse_fact(WF, "test", b, self.db))
        for event in facts.list_reverification_decisions(WF, db_path=self.db):
            if event["payload"]["verifier"] == "test":
                self.assertEqual(event["payload"]["reason"],
                                 rv.REASON_RERUN_SOURCE_NOT_PASS)
        self.assertIn("test", self.launched_nodes())


class _CountingModule:
    """Wraps the planner module, counting real calls to its git fact layer."""

    def __init__(self, counter):
        self._counter = counter
        self._real = reverification_mod

    def __getattr__(self, name):
        return getattr(self._real, name)

    def collect_candidate_changes(self, *args, **kwargs):
        return self._counter(*args, **kwargs)


class _SubprocessShim:
    """Module-shaped stand-in for `subprocess` with only `run` intercepted.

    A bare function would break the controller's other subprocess uses
    (TimeoutExpired, CompletedProcess, Popen), which is how the fake ended up
    masking real behaviour instead of just faking launches.
    """

    def __init__(self, run_impl):
        self._run_impl = run_impl

    def run(self, cmd, **kwargs):
        return self._run_impl(cmd, **kwargs)

    def __getattr__(self, name):
        return getattr(subprocess, name)


class _Queue:
    def __init__(self, sink):
        self.sink = sink

    def put(self, item):
        self.sink.append(item)


_COUNTER = {"n": 0}


def _bump():
    _COUNTER["n"] += 1
    return 1_700_000_000.0 + _COUNTER["n"]


if __name__ == "__main__":
    unittest.main()
