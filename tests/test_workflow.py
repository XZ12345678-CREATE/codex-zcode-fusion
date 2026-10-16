"""Fusion v0.2.0 工作流单元测试：只验证本地可观察行为，不调用模型、不处理凭据、不联网。

覆盖任务书要求的 v0.2.0 场景（在 test_fusion.py 的 29 项既有覆盖之外）：
- handoff 派工包生成与同轮幂等；retry 后换 token/换轮次；approved 后拒绝再派工
- dispatch 记录真实发送后的会话标签与派工历史；需先 handoff；同 token+标签幂等、多标签可追踪
- result 固定路径结构化结果的严格校验：伪造/旧轮次/越界（含实际越界改动即拒绝）/
  类型错误（含非 ASCII token）/特殊文件/超大/无效 JSON
- 真实子进程持锁时并发调用报忙；持锁者终止后恢复；只读操作不受锁影响
- apply 逐文件 journal：注入第二文件 os.replace 失败后记录首文件已写、resume 阻止盲目重试、
  显式 confirm_recovery 后幂等补完；journal 活动期间 retry/handoff/dispatch/result/
  verify/review 全部拒绝；模拟崩溃窗口（首文件已写、进度未保存）的 plan/恢复行为
- v0.1.0 旧任务（无新字段）仍可走 verify/review/apply
- resume 决策树各状态（含返工上限 reassess）与只读性；retry 后旧 result.json 不造成导入循环
- list 摘要/损坏任务容忍/workspace 过滤
- CLI 与 stdio MCP 新接口（handoff/dispatch/result/list/resume）；MCP 进程在坏结果负载下存活
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

FUSION_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(FUSION_ROOT))

import fusion  # noqa: E402


class WorkflowTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.ws = self.root / "ws"
        (self.ws / "tests").mkdir(parents=True)
        (self.ws / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        (self.ws / "notes.md").write_text("project notes\n", encoding="utf-8")
        (self.ws / "feature.py").unlink(missing_ok=True)
        (self.ws / "tests" / "test_app.py").write_text(
            "from app import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n", encoding="utf-8"
        )
        self.tasks_root = self.root / "fusion-tasks"
        self._old_env = os.environ.get("FUSION_TASKS_DIR")
        os.environ["FUSION_TASKS_DIR"] = str(self.tasks_root)

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("FUSION_TASKS_DIR", None)
        else:
            os.environ["FUSION_TASKS_DIR"] = self._old_env
        self._tmp.cleanup()

    def make_spec(self, **overrides):
        spec = {
            "workspace": str(self.ws),
            "request": "请给 app.py 增加 multiply 函数，并新增 feature.py",
            "instructions": "保持现有风格，不引入第三方依赖。",
            "requirements": ["实现 multiply 函数", "不修改清单外文件"],
            "allowed_files": ["app.py", "feature.py"],
            "context_files": ["notes.md"],
            "checks": [{"name": "smoke", "argv": [sys.executable, "-c", "print('ok')"]}],
        }
        spec.update(overrides)
        return spec

    def prepare(self, **overrides) -> str:
        return fusion.cmd_prepare(self.make_spec(**overrides))["task_id"]

    def work_path(self, task_id: str, rel: str) -> Path:
        work = fusion._load_task(task_id)["work_path"]
        return Path(work).joinpath(*rel.split("/"))

    def do_edit(self, task_id: str):
        """执行者标准改动：修改 app.py 并新建 feature.py。"""
        self.work_path(task_id, "app.py").write_text(
            "def add(a, b):\n    return a + b\n\n\ndef multiply(a, b):\n    return a * b\n",
            encoding="utf-8")
        self.work_path(task_id, "feature.py").write_text("FEATURE = True\n", encoding="utf-8")

    def decisions(self, task_id: str, passed=True):
        task = fusion._load_task(task_id)
        return [{"requirement": r, "passed": passed, "evidence": f"证据：{r}"}
                for r in task["spec"]["requirements"]]

    def approve(self, task_id: str):
        """编辑副本并 verify + review 通过，使任务进入 approved。"""
        self.do_edit(task_id)
        record = fusion.cmd_verify(task_id)
        self.assertTrue(record["passed"], record["failures"])
        fusion.cmd_review(task_id, self.decisions(task_id))

    def build_result_doc(self, task_id: str, **overrides) -> dict:
        task = fusion._load_task(task_id)
        handoff = task["handoff"]
        doc = {
            "task_id": task_id,
            "round": handoff["round"],
            "handoff_token": handoff["token"],
            "summary": "已实现 multiply 并新增 feature.py",
            "changed_files": ["app.py", "feature.py"],
            "requirements": [
                {"requirement": r, "evidence": f"执行者证据：{r}"}
                for r in task["spec"]["requirements"]
            ],
            "checks": [{"name": "smoke", "result": "passed: ok"}],
            "issues": [],
        }
        doc.update(overrides)
        return doc

    def write_result(self, task_id: str, doc) -> Path:
        """按 handoff 约定的原子方式写入 result.json。"""
        path = fusion._result_path(task_id)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return path

    def run_cli(self, *args):
        env = dict(os.environ, FUSION_TASKS_DIR=str(self.tasks_root))
        return subprocess.run([sys.executable, str(FUSION_ROOT / "fusion.py"), *args],
                              capture_output=True, text=True, env=env, timeout=120)


class TestHandoff(WorkflowTestCase):
    def test_handoff_creates_idempotent_package_outside_work(self):
        task_id = self.prepare()
        first = fusion.cmd_handoff(task_id)
        self.assertEqual(first["round"], 1)
        self.assertTrue(first["token"])
        tdir = fusion._task_dir(task_id)
        # 接口约定：result 路径固定为 taskdir/result.json；派工包在 taskdir 内、work 外
        self.assertEqual(first["result_path"], str(tdir / "result.json"))
        handoff_file = Path(first["handoff_path"])
        template_file = Path(first["result_template_path"])
        self.assertEqual(handoff_file.parent, tdir)
        self.assertTrue(handoff_file.is_file())
        self.assertTrue(template_file.is_file())
        work = Path(fusion.cmd_get(task_id)["work_path"])
        self.assertNotIn(handoff_file, list(work.rglob("*")))
        self.assertFalse((work / "handoff.md").exists())

        # prompt 可直接粘贴：包含 brief/work/result 路径与 token
        for needle in (first["token"], str(tdir / "result.json"), work.as_posix(),
                       task_id, "handoff_token"):
            self.assertIn(needle, first["prompt"])
        handoff_text = handoff_file.read_text(encoding="utf-8")
        for needle in (first["token"], "round", "changed_files", "原子写入", "allowed_files"):
            self.assertIn(needle, handoff_text)
        template = json.loads(template_file.read_text(encoding="utf-8"))
        self.assertEqual(template["task_id"], task_id)
        self.assertEqual(template["round"], 1)
        self.assertEqual(template["handoff_token"], first["token"])

        # brief 告知执行者：仅 work 内改文件、结构化结果为唯一额外输出
        brief = Path(fusion.cmd_get(task_id)["brief_path"]).read_text(encoding="utf-8")
        self.assertIn("result.json", brief)
        self.assertIn("唯一允许的额外输出", brief)

        # 同轮重复读取幂等：token 不变
        second = fusion.cmd_handoff(task_id)
        self.assertEqual(second["token"], first["token"])
        self.assertEqual(second["round"], 1)
        self.assertTrue(second["idempotent_reuse"])
        # 文件丢失后按同一 token 原样重建
        handoff_file.unlink()
        third = fusion.cmd_handoff(task_id)
        self.assertEqual(third["token"], first["token"])
        self.assertTrue(handoff_file.is_file())

        # 事件已记录
        events = fusion._load_task(task_id)["events"]
        self.assertTrue(any(e["type"] == "handoff" for e in events))

    def test_handoff_round_and_token_rotate_after_retry(self):
        task_id = self.prepare()
        first = fusion.cmd_handoff(task_id)
        fusion.cmd_retry(task_id, "需要返工：补充负数处理。")
        # retry 清除本轮结果与派工 token
        self.assertIsNone(fusion._load_task(task_id)["handoff"])
        second = fusion.cmd_handoff(task_id)
        self.assertEqual(second["round"], 2)
        self.assertNotEqual(second["token"], first["token"])


class TestDispatch(WorkflowTestCase):
    def test_dispatch_records_session_label_and_rejects_empty(self):
        task_id = self.prepare()
        fusion.cmd_handoff(task_id)
        for bad in ("", "   ", None, 123):
            with self.assertRaises(fusion.FusionError):
                fusion.cmd_dispatch(task_id, bad)
        result = fusion.cmd_dispatch(task_id, "zcode-main-chat")
        self.assertEqual(result["session_label"], "zcode-main-chat")
        self.assertEqual(result["round"], 1)
        # 工具不声称发送 UI
        self.assertIn("不发送", result["note"])

        fusion.cmd_dispatch(task_id, "zcode-followup")
        info = fusion.cmd_get(task_id)
        labels = [d["session_label"] for d in info["dispatches"]]
        self.assertEqual(labels, ["zcode-main-chat", "zcode-followup"])
        events = fusion._load_task(task_id)["events"]
        self.assertEqual(sum(1 for e in events if e["type"] == "dispatch"), 2)

        # 相同 token + 相同会话标签：幂等返回，不新增历史
        dup = fusion.cmd_dispatch(task_id, "zcode-main-chat")
        self.assertTrue(dup["duplicate"])
        self.assertEqual(dup["dispatch_count"], 2)
        self.assertEqual([d["session_label"] for d in fusion.cmd_get(task_id)["dispatches"]],
                         ["zcode-main-chat", "zcode-followup"])
        # 同轮不同会话标签：可追踪的多会话历史
        multi = fusion.cmd_dispatch(task_id, "zcode-third-session")
        self.assertFalse(multi["duplicate"])
        self.assertEqual(multi["dispatch_count"], 3)

        # 未生成本轮 handoff：拒绝记录派工（避免按轮次误记到尚未生成的新包）
        bare = self.prepare()
        with self.assertRaises(fusion.FusionError) as ctx:
            fusion.cmd_dispatch(bare, "no-handoff-session")
        self.assertIn("handoff", str(ctx.exception))
        self.assertEqual(fusion.cmd_get(bare)["dispatches"], [])

    def test_handoff_rejected_after_approval(self):
        task_id = self.prepare()
        fusion.cmd_verify(task_id)  # 无改动且检查通过
        fusion.cmd_review(task_id, self.decisions(task_id))
        self.assertEqual(fusion.cmd_get(task_id)["status"], "approved")
        with self.assertRaises(fusion.FusionError) as ctx:
            fusion.cmd_handoff(task_id)
        self.assertIn("approved", str(ctx.exception))
        self.assertIsNone(fusion._load_task(task_id)["handoff"])


class TestResult(WorkflowTestCase):
    def test_result_imports_valid_evidence_without_approving(self):
        task_id = self.prepare()
        fusion.cmd_handoff(task_id)
        self.do_edit(task_id)
        self.write_result(task_id, self.build_result_doc(task_id))

        result = fusion.cmd_result(task_id)
        self.assertTrue(result["imported"])
        self.assertFalse(result["trusted"])
        self.assertEqual(result["changed_files"], ["app.py", "feature.py"])
        self.assertEqual(result["out_of_scope_detected"], [])

        info = fusion.cmd_get(task_id)
        # 结果只是不可信证据：不批准任务、不代替 verify
        self.assertEqual(info["status"], "prepared")
        self.assertIsNone(info["review"])
        self.assertFalse(info["executor_result"] is None)
        self.assertFalse(info["executor_result"]["trusted"])
        events = fusion._load_task(task_id)["events"]
        self.assertTrue(any(e["type"] == "result_imported" for e in events))

    def test_result_rejects_stale_round_and_token(self):
        task_id = self.prepare()
        first = fusion.cmd_handoff(task_id)
        self.do_edit(task_id)
        self.write_result(task_id, self.build_result_doc(task_id))
        self.assertTrue(fusion.cmd_result(task_id)["imported"])

        fusion.cmd_retry(task_id, "返工第一轮。")
        second = fusion.cmd_handoff(task_id)
        self.assertEqual(second["round"], 2)
        fusion.cmd_dispatch(task_id, "zcode-round-2")
        # 旧轮次结果留盘：不被认可
        with self.assertRaises(fusion.FusionError) as ctx:
            fusion.cmd_result(task_id)
        self.assertIn("轮次", str(ctx.exception))
        self.assertIsNone(fusion.cmd_get(task_id)["executor_result"])

        # 本轮但 token 错误
        self.do_edit(task_id)
        doc = self.build_result_doc(task_id, handoff_token="0" * 32)
        self.write_result(task_id, doc)
        with self.assertRaises(fusion.FusionError) as ctx:
            fusion.cmd_result(task_id)
        self.assertIn("handoff_token", str(ctx.exception))

        # 轮次数值错误
        self.write_result(task_id, self.build_result_doc(task_id, round=99))
        with self.assertRaises(fusion.FusionError) as ctx:
            fusion.cmd_result(task_id)
        self.assertIn("轮次", str(ctx.exception))

    def test_result_rejects_forged_and_schema_invalid_documents(self):
        task_id = self.prepare()
        fusion.cmd_handoff(task_id)
        self.do_edit(task_id)
        good = self.build_result_doc(task_id)

        cases = [
            ("伪造任务", dict(good, task_id="f" * 32), "task_id"),
            ("round 为布尔", dict(good, round=True), "真整数"),
            ("round 为字符串", dict(good, round="1"), "真整数"),
            ("round 缺失", {k: v for k, v in good.items() if k != "round"}, "真整数"),
            ("token 缺失", {k: v for k, v in good.items() if k != "handoff_token"}, "handoff_token"),
            ("token 非 ASCII", dict(good, handoff_token="令牌-非ASCII"), "ASCII"),
            ("summary 为空", dict(good, summary="   "), "summary"),
            ("summary 缺失", {k: v for k, v in good.items() if k != "summary"}, "summary"),
            ("changed_files 非数组", dict(good, changed_files="app.py"), "changed_files"),
            ("changed_files 含路径穿越", dict(good, changed_files=["app.py", "../evil.py", "feature.py"]), "非法路径"),
            ("changed_files 白名单外", dict(good, changed_files=["app.py", "feature.py", "rogue.txt"]), "白名单"),
            ("changed_files 重复", dict(good, changed_files=["app.py", "app.py", "feature.py"]), "重复"),
            ("changed_files 与实际不符", dict(good, changed_files=["app.py"]), "实际变化不一致"),
            ("requirements 非数组", dict(good, requirements={"a": 1}), "requirements"),
            ("requirements 遗漏", dict(good, requirements=good["requirements"][:1]), "未恰好覆盖"),
            ("requirements 多余", dict(good, requirements=good["requirements"] + [
                {"requirement": "不存在的", "evidence": "x"}]), "未恰好覆盖"),
            ("requirements 空证据", dict(good, requirements=[
                {"requirement": r, "evidence": "  "} for r in
                [d["requirement"] for d in good["requirements"]]]), "evidence"),
            ("checks 非数组", dict(good, checks="none"), "checks"),
            ("checks 项非对象", dict(good, checks=["passed"]), "对象"),
            ("checks result 非字符串", dict(good, checks=[{"name": "s", "result": True}]), "checks.result"),
            ("issues 非字符串数组", dict(good, issues=[1, 2]), "issues"),
            ("issues 缺失", {k: v for k, v in good.items() if k != "issues"}, "issues"),
        ]
        for name, doc, fragment in cases:
            with self.subTest(case=name):
                self.write_result(task_id, doc)
                with self.assertRaises(fusion.FusionError) as ctx:
                    fusion.cmd_result(task_id)
                self.assertIn(fragment, str(ctx.exception))
        self.assertIsNone(fusion.cmd_get(task_id)["executor_result"],
                          "所有被拒绝的结果都不得进入状态")

        # 声明了变化但副本没有变化：同样拒绝
        bare_id = self.prepare()
        fusion.cmd_handoff(bare_id)
        self.write_result(bare_id, self.build_result_doc(bare_id))
        with self.assertRaises(fusion.FusionError) as ctx:
            fusion.cmd_result(bare_id)
        self.assertIn("实际变化不一致", str(ctx.exception))

    def test_result_rejects_actual_out_of_scope_changes(self):
        task_id = self.prepare()
        fusion.cmd_handoff(task_id)
        self.do_edit(task_id)
        # 执行者在副本里留下越界产物，但 result 只申报白名单内文件
        self.work_path(task_id, "rogue.txt").write_text("越界产物\n", encoding="utf-8")
        self.write_result(task_id, self.build_result_doc(task_id))
        with self.assertRaises(fusion.FusionError) as ctx:
            fusion.cmd_result(task_id)
        self.assertIn("白名单外", str(ctx.exception))
        self.assertIn("rogue.txt", str(ctx.exception))
        self.assertIsNone(fusion.cmd_get(task_id)["executor_result"],
                          "存在实际越界改动的结果不得导入")
        # 清理越界改动后同一文件可导入；verify 仍是独立后续闸门
        self.work_path(task_id, "rogue.txt").unlink()
        self.assertTrue(fusion.cmd_result(task_id)["imported"])

    def test_result_rejects_symlink_fifo_oversized_invalid_and_missing(self):
        task_id = self.prepare()
        fusion.cmd_handoff(task_id)
        path = fusion._result_path(task_id)

        # 尚未写入
        with self.assertRaises(fusion.FusionError) as ctx:
            fusion.cmd_result(task_id)
        self.assertIn("尚未写入", str(ctx.exception))

        # 无效 JSON
        path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(fusion.FusionError) as ctx:
            fusion.cmd_result(task_id)
        self.assertIn("JSON", str(ctx.exception))

        # 合法 JSON 但不是对象
        path.write_text("[]", encoding="utf-8")
        with self.assertRaises(fusion.FusionError) as ctx:
            fusion.cmd_result(task_id)
        self.assertIn("JSON 对象", str(ctx.exception))

        # 超过 1MB
        path.write_bytes(b'{"pad":"' + b"x" * (fusion.MAX_RESULT_JSON_BYTES + 10) + b'"}')
        with self.assertRaises(fusion.FusionError) as ctx:
            fusion.cmd_result(task_id)
        self.assertIn("1MB", str(ctx.exception))

        # FIFO：不得阻塞（lstat/O_NONBLOCK 防护），明确拒绝
        path.unlink()
        os.mkfifo(path)
        with self.assertRaises(fusion.FusionError) as ctx:
            fusion.cmd_result(task_id)
        self.assertIn("常规文件", str(ctx.exception))
        path.unlink()

        # 软链接：即使指向合法 JSON 也拒绝
        real = self.root / "real-result.json"
        self.do_edit(task_id)
        real.write_text(json.dumps(self.build_result_doc(task_id), ensure_ascii=False),
                        encoding="utf-8")
        path.symlink_to(real)
        with self.assertRaises(fusion.FusionError) as ctx:
            fusion.cmd_result(task_id)
        self.assertIn("软链接", str(ctx.exception))


class TestConcurrency(WorkflowTestCase):
    HOLDER_SCRIPT = (
        "import fcntl, os, sys, time\n"
        "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o644)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "print('locked', flush=True)\n"
        "time.sleep(60)\n"
    )

    def start_lock_holder(self, task_id: str) -> subprocess.Popen:
        lock_path = fusion._task_dir(task_id) / "task.lock"
        holder = subprocess.Popen(
            [sys.executable, "-c", self.HOLDER_SCRIPT, str(lock_path)],
            stdout=subprocess.PIPE, text=True)
        self.assertEqual(holder.stdout.readline().strip(), "locked")
        return holder

    def test_lock_busy_with_real_holder_and_recovery_after_termination(self):
        task_id = self.prepare()
        holder = self.start_lock_holder(task_id)
        try:
            # 真实子进程持锁：并发写操作必须报忙
            with self.assertRaises(fusion.FusionError) as ctx:
                fusion.cmd_handoff(task_id)
            self.assertIn("忙", str(ctx.exception))
            with self.assertRaises(fusion.FusionError):
                fusion.cmd_verify(task_id)
        finally:
            holder.terminate()
            holder.wait(timeout=10)
            holder.stdout.close()
        # 持锁进程退出后即可恢复
        result = fusion.cmd_handoff(task_id)
        self.assertEqual(result["round"], 1)
        record = fusion.cmd_verify(task_id)
        self.assertTrue(record["passed"], record["failures"])

    def test_readonly_ops_work_while_lock_held_by_other_process(self):
        task_id = self.prepare()
        holder = self.start_lock_holder(task_id)
        try:
            info = fusion.cmd_get(task_id)          # 只读操作不加锁
            self.assertEqual(info["status"], "prepared")
            view = fusion.cmd_resume(task_id)
            self.assertEqual(view["next_action"], "handoff")
            listing = fusion.cmd_list()
            self.assertEqual(listing["count"], 1)
        finally:
            holder.terminate()
            holder.wait(timeout=10)
            holder.stdout.close()


class TestApplyJournal(WorkflowTestCase):
    def inject_replace_failure(self, failing_target: Path):
        """让指向 failing_target 的 os.replace 失败；其余（含 task.json 原子保存）照常。"""
        real_replace = os.replace

        def flaky_replace(src, dst, *args, **kwargs):
            if Path(dst) == failing_target:
                raise OSError(f"injected replace failure: {dst}")
            return real_replace(src, dst, *args, **kwargs)

        return unittest.mock.patch.object(fusion.os, "replace", flaky_replace)

    def test_apply_failure_journals_partial_progress_and_resume_blocks(self):
        task_id = self.prepare()
        self.approve(task_id)
        old_app = "def add(a, b):\n    return a + b\n"

        with self.inject_replace_failure(self.ws / "feature.py"):
            with self.assertRaises(fusion.FusionError) as ctx:
                fusion.cmd_apply(task_id)
        self.assertIn("IO 错误", str(ctx.exception))

        # 首文件已真实写入，第二文件未写入
        self.assertIn("def multiply", (self.ws / "app.py").read_text(encoding="utf-8"))
        self.assertFalse((self.ws / "feature.py").exists())

        task = fusion._load_task(task_id)
        journal = task["apply_journal"]
        self.assertEqual(journal["status"], "failed")
        self.assertEqual(journal["files_done"], ["app.py"])
        self.assertEqual(journal["files_pending"], ["feature.py"])
        self.assertIn("injected", journal["error"])
        self.assertIsNotNone(journal["backup_dir"])

        # 状态未变 applied；resume 明确提示人工核查并阻止盲目重试
        self.assertEqual(fusion.cmd_get(task_id)["status"], "approved")
        view = fusion.cmd_resume(task_id)
        self.assertEqual(view["next_action"], "manual_check")
        self.assertIn("人工核查", view["reason"])
        with self.assertRaises(fusion.FusionError) as ctx:
            fusion.cmd_apply(task_id)
        self.assertIn("confirm", str(ctx.exception))

        # journal 活动期间 retry 被拒绝：不得把半应用任务改成 needs_revision
        with self.assertRaises(fusion.FusionError) as ctx:
            fusion.cmd_retry(task_id, "应用中断后的返工反馈。")
        self.assertIn("journal", str(ctx.exception))
        self.assertEqual(fusion.cmd_get(task_id)["status"], "approved")
        # journal 保持失败记录，未被掩盖或清除
        self.assertEqual(fusion._load_task(task_id)["apply_journal"]["status"], "failed")

    def test_apply_recovery_completes_after_confirmation(self):
        task_id = self.prepare()
        self.approve(task_id)
        with self.inject_replace_failure(self.ws / "feature.py"):
            with self.assertRaises(fusion.FusionError):
                fusion.cmd_apply(task_id)
        self.assertIn("def multiply", (self.ws / "app.py").read_text(encoding="utf-8"))

        result = fusion.cmd_apply(task_id, confirm_recovery=True)
        self.assertEqual(result["status"], "applied")
        self.assertEqual(fusion.cmd_get(task_id)["status"], "applied")
        self.assertEqual((self.ws / "feature.py").read_text(encoding="utf-8"), "FEATURE = True\n")
        journal = fusion._load_task(task_id)["apply_journal"]
        self.assertEqual(journal["status"], "completed")
        self.assertEqual(sorted(journal["files_done"]), ["app.py", "feature.py"])
        self.assertEqual(journal["files_pending"], [])
        applied_files = sorted(c["file"] for c in result["changes"])
        self.assertEqual(applied_files, ["app.py", "feature.py"])
        view = fusion.cmd_resume(task_id)
        self.assertEqual(view["next_action"], "done")

    def test_apply_interrupted_journal_detected_under_lock(self):
        task_id = self.prepare()
        self.approve(task_id)
        # 模拟持锁进程中途崩溃：journal 留在 in_progress
        task = fusion._load_task(task_id)
        task["apply_journal"] = {
            "status": "in_progress", "started_at": fusion._now(),
            "plan": [{"file": "app.py", "action": "modify"}],
            "files_done": [], "files_pending": ["app.py"],
            "backup_dir": None, "error": None,
        }
        fusion._save_task(task)
        view = fusion.cmd_resume(task_id)
        self.assertEqual(view["next_action"], "manual_check")
        self.assertIn("in_progress", view["reason"])  # 只读视图如实报告中断前状态
        with self.assertRaises(fusion.FusionError) as ctx:
            fusion.cmd_apply(task_id)
        # 持锁检测到上次进程已中止：journal 标记为 interrupted 并阻止盲目重试
        self.assertIn("interrupted", str(ctx.exception))
        self.assertEqual(fusion._load_task(task_id)["apply_journal"]["status"], "interrupted")

    def test_journal_blocks_flow_operations_until_recovered(self):
        task_id = self.prepare()
        self.approve(task_id)
        with self.inject_replace_failure(self.ws / "feature.py"):
            with self.assertRaises(fusion.FusionError):
                fusion.cmd_apply(task_id)

        # journal 活动：全部流程写操作拒绝，不允许用 retry/review 掩盖半应用状态
        for action, fn in (
            ("retry", lambda: fusion.cmd_retry(task_id, "反馈")),
            ("handoff", lambda: fusion.cmd_handoff(task_id)),
            ("dispatch", lambda: fusion.cmd_dispatch(task_id, "s")),
            ("result", lambda: fusion.cmd_result(task_id)),
            ("verify", lambda: fusion.cmd_verify(task_id)),
            ("review", lambda: fusion.cmd_review(task_id, self.decisions(task_id))),
        ):
            with self.subTest(action=action):
                with self.assertRaises(fusion.FusionError) as ctx:
                    fn()
                self.assertIn("journal", str(ctx.exception))
        self.assertEqual(fusion.cmd_get(task_id)["status"], "approved",
                         "半应用任务不得被改成 needs_revision")

        # 只读操作始终可用；apply 的显式恢复路径不受影响
        self.assertEqual(fusion.cmd_resume(task_id)["next_action"], "manual_check")
        fusion.cmd_get(task_id)
        fusion.cmd_list()
        fusion.cmd_apply(task_id, confirm_recovery=True)
        self.assertEqual(fusion.cmd_get(task_id)["status"], "applied")
        # journal 完成后恢复正常状态规则（retry 因 applied 被拒，而非 journal）
        with self.assertRaises(fusion.FusionError) as ctx:
            fusion.cmd_retry(task_id, "恢复后的反馈")
        self.assertIn("已应用", str(ctx.exception))
        self.assertNotIn("journal", str(ctx.exception))

    def test_apply_crash_window_files_pending_may_be_written(self):
        """复现真实 os._exit 场景：首文件已替换、逐文件进度未保存。"""
        task_id = self.prepare()
        self.approve(task_id)
        real_replace = os.replace

        def crashing_replace(src, dst, *args, **kwargs):
            result = real_replace(src, dst, *args, **kwargs)
            if Path(dst) == self.ws / "app.py":
                # 模拟进程在 os.replace 之后、进度保存之前死亡（KeyboardInterrupt
                # 不会被 except OSError 捕获，也不会触发 journal failed 保存）
                raise KeyboardInterrupt("simulated crash after first replace")
            return result

        with unittest.mock.patch.object(fusion.os, "replace", crashing_replace):
            with self.assertRaises(KeyboardInterrupt):
                fusion.cmd_apply(task_id)

        # 首文件实际已写入，但 journal 进度没来得及保存
        self.assertIn("def multiply", (self.ws / "app.py").read_text(encoding="utf-8"))
        journal = fusion._load_task(task_id)["apply_journal"]
        self.assertEqual(journal["status"], "in_progress")
        self.assertEqual(journal["files_done"], [])
        self.assertEqual(journal["files_pending"], ["app.py", "feature.py"])

        # plan 在写文件前已持久化：含备份位置与预期 before/after 摘要
        plan = {i["file"]: i for i in journal["plan"]}
        tdir = fusion._task_dir(task_id)
        self.assertTrue(plan["app.py"]["backup_path"].endswith("backup/app.py"))
        self.assertEqual(plan["app.py"]["before_sha256"],
                         fusion.sha256_file(tdir / "orig" / "app.py"))
        self.assertEqual(plan["app.py"]["after_sha256"],
                         fusion.sha256_file(self.work_path(task_id, "app.py")))
        self.assertIsNone(plan["feature.py"]["backup_path"])
        self.assertIsNone(plan["feature.py"]["before_sha256"])

        # resume 明确提示需核对，不把 files_pending 说成确定未写入
        view = fusion.cmd_resume(task_id)
        self.assertEqual(view["next_action"], "manual_check")
        self.assertIn("可能已写入", view["reason"])
        self.assertIn("需核对", view["reason"])
        self.assertIn("保留原备份", view["reason"])
        summary_plan = {i["file"]: i for i in view["apply_journal"]["plan"]}
        self.assertIn("before_sha256", summary_plan["app.py"])

        # 盲目 apply 被拒；人工确认后幂等补完（已写入文件按哈希跳过）
        with self.assertRaises(fusion.FusionError):
            fusion.cmd_apply(task_id)
        result = fusion.cmd_apply(task_id, confirm_recovery=True)
        self.assertEqual(result["status"], "applied")
        self.assertEqual((self.ws / "feature.py").read_text(encoding="utf-8"), "FEATURE = True\n")
        self.assertEqual(fusion._load_task(task_id)["apply_journal"]["status"], "completed")

    def test_old_style_task_without_new_fields_still_works(self):
        task_id = self.prepare()
        tjson = fusion._task_dir(task_id) / "task.json"
        data = json.loads(tjson.read_text(encoding="utf-8"))
        for key in ("events", "handoff", "dispatches", "executor_result", "apply_journal"):
            data.pop(key, None)
        tjson.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

        # 旧任务没有 handoff：result 明确拒绝，但不影响旧流程继续可用
        self.write_result(task_id, {
            "task_id": task_id, "round": 1, "handoff_token": "x",
            "summary": "s", "changed_files": [], "requirements": [],
            "checks": [], "issues": []})
        with self.assertRaises(fusion.FusionError) as ctx:
            fusion.cmd_result(task_id)
        self.assertIn("handoff", str(ctx.exception))

        self.do_edit(task_id)
        record = fusion.cmd_verify(task_id)
        self.assertTrue(record["passed"], record["failures"])
        fusion.cmd_review(task_id, self.decisions(task_id))
        result = fusion.cmd_apply(task_id)
        self.assertEqual(result["status"], "applied")
        self.assertIn("def multiply", (self.ws / "app.py").read_text(encoding="utf-8"))
        view = fusion.cmd_resume(task_id)
        self.assertEqual(view["next_action"], "done")


class TestResume(WorkflowTestCase):
    def test_resume_before_handoff_and_after_dispatch(self):
        task_id = self.prepare()
        tjson = fusion._task_dir(task_id) / "task.json"

        view = fusion.cmd_resume(task_id)
        self.assertEqual(view["next_action"], "handoff")
        self.assertEqual(view["result_path"], str(fusion._result_path(task_id)))
        self.assertEqual(view["request"], "请给 app.py 增加 multiply 函数，并新增 feature.py")
        self.assertIn("实现 multiply 函数", view["requirements"])
        for key in ("brief_path", "work_path", "workspace", "task_dir"):
            self.assertTrue(view[key])

        fusion.cmd_handoff(task_id)
        self.assertEqual(fusion.cmd_resume(task_id)["next_action"], "dispatch")

        fusion.cmd_dispatch(task_id, "zcode-chat-01")
        snapshot = tjson.read_bytes()  # 已派工状态下拍照，resume 不得改动任何字节
        view = fusion.cmd_resume(task_id)
        self.assertEqual(view["next_action"], "wait_zcode")
        self.assertEqual(view["session_labels"], ["zcode-chat-01"])
        self.assertTrue(view["dispatched_this_round"])
        # resume 只读：不自动重新派工，也不改任何状态
        self.assertEqual(tjson.read_bytes(), snapshot)
        self.assertEqual(len(fusion._load_task(task_id)["dispatches"]), 1)

    def test_resume_with_pending_and_imported_result(self):
        task_id = self.prepare()
        fusion.cmd_handoff(task_id)
        fusion.cmd_dispatch(task_id, "zcode-chat-01")
        self.do_edit(task_id)
        self.write_result(task_id, self.build_result_doc(task_id))

        view = fusion.cmd_resume(task_id)
        self.assertEqual(view["next_action"], "import_result")
        self.assertTrue(view["has_pending_result_file"])

        fusion.cmd_result(task_id)
        view = fusion.cmd_resume(task_id)
        self.assertEqual(view["next_action"], "verify")
        self.assertFalse(view["has_pending_result_file"])

        fusion.cmd_verify(task_id)
        view = fusion.cmd_resume(task_id)
        self.assertEqual(view["next_action"], "review")
        self.assertTrue(view["work_digest_matches_verification"])

        # 验证后副本又变动：摘要不一致，建议重新 verify
        self.work_path(task_id, "feature.py").write_text("FEATURE = False\n", encoding="utf-8")
        view = fusion.cmd_resume(task_id)
        self.assertEqual(view["next_action"], "verify")
        self.assertFalse(view["work_digest_matches_verification"])

    def test_resume_stale_result_after_retry_no_import_loop(self):
        task_id = self.prepare()
        fusion.cmd_handoff(task_id)
        fusion.cmd_dispatch(task_id, "s1")
        self.do_edit(task_id)
        self.write_result(task_id, self.build_result_doc(task_id))
        self.assertTrue(fusion.cmd_result(task_id)["imported"])

        # retry 清空 handoff 后旧 result.json 留在盘上：不得建议会被拒绝的 import_result
        fusion.cmd_retry(task_id, "返工第一轮。")
        view = fusion.cmd_resume(task_id)
        self.assertNotEqual(view["next_action"], "import_result")
        self.assertFalse(view["has_pending_result_file"])
        self.assertIsNotNone(view["result_error"])
        self.assertIn("handoff", view["result_error"])
        self.assertEqual(view["next_action"], "handoff")  # 有用下一步：生成最新 handoff

        # 生成了新 handoff，但旧文件 round/token 过期：仍不导入，给出明确错误
        fusion.cmd_handoff(task_id)
        view = fusion.cmd_resume(task_id)
        self.assertEqual(view["next_action"], "dispatch")
        self.assertIn("不匹配", view["result_error"])

        # 已派工：等待执行者按新 token 重新提交，不自动重发、不进入导入循环
        fusion.cmd_dispatch(task_id, "s2")
        view = fusion.cmd_resume(task_id)
        self.assertEqual(view["next_action"], "wait_zcode")
        self.assertIn(view["result_error"], view["reason"])

        # 执行者按新 handoff 原子重写后才可导入
        self.write_result(task_id, self.build_result_doc(task_id))
        view = fusion.cmd_resume(task_id)
        self.assertEqual(view["next_action"], "import_result")
        self.assertTrue(view["has_pending_result_file"])
        self.assertIsNone(view["result_error"])
        self.assertTrue(fusion.cmd_result(task_id)["imported"])

    def test_resume_review_rejection_and_rework_limit(self):
        task_id = self.prepare()
        self.do_edit(task_id)
        fusion.cmd_verify(task_id)
        fusion.cmd_review(task_id, self.decisions(task_id, passed=False))
        view = fusion.cmd_resume(task_id)
        self.assertEqual(view["next_action"], "retry")
        self.assertIn("实现 multiply 函数", view["reason"])

        # 验证失败 + 已到返工上限：建议返工并提示上限
        limited = self.prepare(
            checks=[{"name": "must-fail", "argv": [sys.executable, "-c", "raise SystemExit(1)"]}])
        fusion.cmd_retry(limited, "第一轮反馈")
        fusion.cmd_retry(limited, "第二轮反馈")
        record = fusion.cmd_verify(limited)
        self.assertFalse(record["passed"])
        view = fusion.cmd_resume(limited)
        # 到 2 轮上限：不再建议会被拒绝的 retry，改由 Lead 重新评估
        self.assertEqual(view["next_action"], "reassess")
        self.assertIn("上限", view["reason"])
        self.assertIn("retry 会被拒绝", view["reason"])
        with self.assertRaises(fusion.FusionError):
            fusion.cmd_retry(limited, "第三轮反馈")  # 确认 retry 确实会被拒绝

    def test_resume_approved_apply_and_done(self):
        task_id = self.prepare()
        self.approve(task_id)
        view = fusion.cmd_resume(task_id)
        self.assertEqual(view["next_action"], "apply")
        self.assertTrue(view["work_digest_matches_review"])

        # 审查后副本变动：阻止 apply；verify 不接受 approved 任务，建议 retry 重开
        self.work_path(task_id, "feature.py").write_text("FEATURE = False\n", encoding="utf-8")
        view = fusion.cmd_resume(task_id)
        self.assertEqual(view["next_action"], "retry")
        self.assertFalse(view["work_digest_matches_review"])
        with self.assertRaises(fusion.FusionError):
            fusion.cmd_apply(task_id)

        # retry 重开验证与审查（同一副本保留改动），再走完整链路应用
        fusion.cmd_retry(task_id, "副本在审查后被改动，重开验证。")
        fusion.cmd_verify(task_id)
        fusion.cmd_review(task_id, self.decisions(task_id))
        fusion.cmd_apply(task_id)
        view = fusion.cmd_resume(task_id)
        self.assertEqual(view["next_action"], "done")
        with self.assertRaises(fusion.FusionError):
            fusion.cmd_apply(task_id)


class TestList(WorkflowTestCase):
    def test_list_summarizes_and_survives_corrupted_tasks(self):
        task_a = self.prepare()
        task_b = self.prepare()
        # 非任务 ID 目录：忽略
        (self.tasks_root / "garbage-dir").mkdir()
        # 形似任务 ID 但数据损坏：计入 errors，不影响其余任务
        broken_id = "deadbeef" * 4
        (self.tasks_root / broken_id).mkdir()
        (self.tasks_root / broken_id / "task.json").write_text("{ broken", encoding="utf-8")

        listing = fusion.cmd_list()
        ids = [t["task_id"] for t in listing["tasks"]]
        self.assertEqual(ids, sorted([task_a, task_b]))
        self.assertEqual(listing["count"], 2)
        self.assertEqual(len(listing["errors"]), 1)
        self.assertEqual(listing["errors"][0]["task_id"], broken_id)
        summary = listing["tasks"][0]
        for key in ("task_id", "workspace", "status", "request", "work_path",
                    "handoff_round", "has_pending_result", "applied"):
            self.assertIn(key, summary)

        # workspace 过滤
        filtered = fusion.cmd_list(workspace=str(self.ws))
        self.assertEqual({t["task_id"] for t in filtered["tasks"]}, {task_a, task_b})
        empty = fusion.cmd_list(workspace=str(self.root / "nowhere"))
        self.assertEqual(empty["tasks"], [])
        with self.assertRaises(fusion.FusionError):
            fusion.cmd_list(workspace="  ")

        # 未导入的 result.json 反映为 has_pending_result；导入后清除
        fusion.cmd_handoff(task_a)
        self.do_edit(task_a)
        self.write_result(task_a, self.build_result_doc(task_a))
        pending = {t["task_id"]: t["has_pending_result"] for t in fusion.cmd_list()["tasks"]}
        self.assertTrue(pending[task_a])
        self.assertFalse(pending[task_b])
        fusion.cmd_result(task_a)
        pending = {t["task_id"]: t["has_pending_result"] for t in fusion.cmd_list()["tasks"]}
        self.assertFalse(pending[task_a])


class TestCliNewCommands(WorkflowTestCase):
    def test_cli_new_subcommands_json(self):
        spec_file = self.root / "spec.json"
        spec_file.write_text(json.dumps(self.make_spec(), ensure_ascii=False), encoding="utf-8")
        proc = self.run_cli("prepare", "--spec", str(spec_file))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        task_id = json.loads(proc.stdout)["task_id"]

        proc = self.run_cli("handoff", task_id)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        handoff = json.loads(proc.stdout)
        self.assertEqual(handoff["round"], 1)
        self.assertIn("prompt", handoff)
        self.assertTrue(handoff["token"])

        self.do_edit(task_id)
        self.write_result(task_id, self.build_result_doc(task_id))

        proc = self.run_cli("dispatch", task_id, "--session-label", "zcode-cli-session")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["session_label"], "zcode-cli-session")

        proc = self.run_cli("result", task_id)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(json.loads(proc.stdout)["imported"])

        proc = self.run_cli("resume", task_id)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["next_action"], "verify")

        proc = self.run_cli("list")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        listing = json.loads(proc.stdout)
        self.assertIn(task_id, [t["task_id"] for t in listing["tasks"]])

        proc = self.run_cli("list", "--workspace", str(self.ws))
        self.assertIn(task_id, [t["task_id"] for t in json.loads(proc.stdout)["tasks"]])

        # 错误路径：stdout 为空、stderr 为 JSON、退出码 1
        other = self.prepare()
        fusion.cmd_handoff(other)
        proc = self.run_cli("result", other)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, "")
        self.assertIn("尚未写入", json.loads(proc.stderr)["error"]["message"])


class TestMcpNewTools(WorkflowTestCase):
    def test_mcp_process_survives_bad_result_payload(self):
        """非 ASCII handoff_token 曾让 compare_digest 抛 TypeError 并退出 MCP 进程。"""
        task_id = self.prepare()
        fusion.cmd_handoff(task_id)
        self.do_edit(task_id)
        self.write_result(task_id, self.build_result_doc(task_id, handoff_token="令牌-非ASCII"))

        env = dict(os.environ, FUSION_TASKS_DIR=str(self.tasks_root))
        proc = subprocess.Popen(
            [sys.executable, str(FUSION_ROOT / "fusion.py"), "mcp"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, env=env,
        )

        def send(obj):
            proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
            proc.stdin.flush()

        def recv():
            line = proc.stdout.readline()
            self.assertTrue(line.strip(), "MCP 未返回响应（可能已退出）")
            return json.loads(line)

        try:
            send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2024-11-05",
                             "capabilities": {}, "clientInfo": {"name": "unittest"}}})
            recv()
            send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                  "params": {"name": "fusion_result",
                             "arguments": {"task_id": task_id}}})
            err = recv()
            self.assertEqual(err["id"], 2)
            self.assertTrue(err["result"]["isError"], "非 ASCII token 必须是业务错误")
            self.assertIn("ASCII", err["result"]["content"][0]["text"])
            # 进程仍然存活：后续 ping 与 tools/list 正常响应
            send({"jsonrpc": "2.0", "id": 3, "method": "ping"})
            self.assertEqual(recv()["result"], {})
            send({"jsonrpc": "2.0", "id": 4, "method": "tools/list"})
            self.assertEqual(len({t["name"] for t in recv()["result"]["tools"]}), 11)
        finally:
            proc.stdin.close()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            stderr = proc.stderr.read()
            proc.stderr.close()
            proc.stdout.close()
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(stderr, "")

    def test_mcp_new_tools_listed_and_callable(self):
        env = dict(os.environ, FUSION_TASKS_DIR=str(self.tasks_root))
        proc = subprocess.Popen(
            [sys.executable, str(FUSION_ROOT / "fusion.py"), "mcp"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, env=env,
        )
        responses = []

        def send(obj):
            proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
            proc.stdin.flush()

        def recv():
            line = proc.stdout.readline()
            self.assertTrue(line.strip(), "MCP 未返回响应（可能已退出）")
            msg = json.loads(line)
            responses.append(msg)
            return msg

        def call(req_id, name, arguments):
            send({"jsonrpc": "2.0", "id": req_id, "method": "tools/call",
                  "params": {"name": name, "arguments": arguments}})
            msg = recv()
            self.assertFalse(msg["result"]["isError"], msg["result"]["content"][0]["text"])
            return json.loads(msg["result"]["content"][0]["text"])

        try:
            send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2024-11-05",
                             "capabilities": {}, "clientInfo": {"name": "unittest"}}})
            self.assertEqual(recv()["result"]["serverInfo"]["version"], fusion.VERSION)

            send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            tools = recv()["result"]["tools"]
            names = {t["name"] for t in tools}
            self.assertEqual(len(names), 11)
            self.assertEqual(names, {
                "fusion_prepare", "fusion_get", "fusion_verify", "fusion_review",
                "fusion_retry", "fusion_apply", "fusion_handoff", "fusion_dispatch",
                "fusion_result", "fusion_list", "fusion_resume"})
            by_name = {t["name"]: t for t in tools}
            self.assertTrue(by_name["fusion_list"]["annotations"]["readOnlyHint"])
            self.assertTrue(by_name["fusion_resume"]["annotations"]["readOnlyHint"])
            self.assertFalse(by_name["fusion_dispatch"]["annotations"]["readOnlyHint"])
            # schema 给出真实参数
            self.assertEqual(
                by_name["fusion_dispatch"]["inputSchema"]["required"],
                ["task_id", "session_label"])
            self.assertIn("workspace",
                          by_name["fusion_list"]["inputSchema"]["properties"])
            self.assertIn("confirm_recovery",
                          by_name["fusion_apply"]["inputSchema"]["properties"])

            task_id = call(3, "fusion_prepare", {"spec": self.make_spec()})["task_id"]

            handoff = call(4, "fusion_handoff", {"task_id": task_id})
            self.assertEqual(handoff["round"], 1)
            self.assertTrue(handoff["token"])

            self.do_edit(task_id)
            self.write_result(task_id, self.build_result_doc(task_id))

            dispatched = call(5, "fusion_dispatch",
                              {"task_id": task_id, "session_label": "zcode-mcp-session"})
            self.assertEqual(dispatched["session_label"], "zcode-mcp-session")

            imported = call(6, "fusion_result", {"task_id": task_id})
            self.assertTrue(imported["imported"])
            self.assertFalse(imported["trusted"])

            resumed = call(7, "fusion_resume", {"task_id": task_id})
            self.assertEqual(resumed["next_action"], "verify")

            listing = call(8, "fusion_list", {})
            self.assertIn(task_id, [t["task_id"] for t in listing["tasks"]])
            filtered = call(9, "fusion_list", {"workspace": str(self.ws)})
            self.assertIn(task_id, [t["task_id"] for t in filtered["tasks"]])

            # 五个新工具均已真实调用（3-9 中的 4,5,6,7,8/9）；再验证一次错误路径
            send({"jsonrpc": "2.0", "id": 10, "method": "tools/call",
                  "params": {"name": "fusion_result",
                             "arguments": {"task_id": "0" * 32}}})
            miss = recv()
            self.assertTrue(miss["result"]["isError"])
            self.assertIn("任务不存在", miss["result"]["content"][0]["text"])

            self.assertEqual(len(responses), 10)
        finally:
            proc.stdin.close()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            stderr = proc.stderr.read()
            proc.stderr.close()
            proc.stdout.close()
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(stderr, "", "MCP 进程不应向 stderr 输出内容")


if __name__ == "__main__":
    unittest.main()
