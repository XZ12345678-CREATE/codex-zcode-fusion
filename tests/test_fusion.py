"""Fusion 原型单元测试：只验证本地可观察行为，不调用模型、不处理凭据、不联网。

覆盖任务书要求的场景：
- 阶段副本不影响原文件；原文件随后改动拒绝 apply；副本审查后变动拒绝 apply
- 越界改动失败；遗漏要求失败；测试失败/超时阻止批准
- 软链接与路径穿越拒绝；apply 目标被换成软链接拒绝
- 完整 prepare→verify→review→apply 成功；既有测试文件修改需显式确认
- 返工流程与轮次上限；删除暂不支持；__pycache__ 缓存忽略
- MCP 子进程 initialize/list/call（含请求 id=0）；CLI 子进程 JSON 输出
"""

from __future__ import annotations

import hashlib
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


def sha_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class FusionTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.ws = self.root / "ws"
        (self.ws / "tests").mkdir(parents=True)
        (self.ws / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        (self.ws / "notes.md").write_text("project notes\n", encoding="utf-8")
        (self.ws / "scratch.txt").write_text("scratch\n", encoding="utf-8")
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
        result = fusion.cmd_prepare(self.make_spec(**overrides))
        return result["task_id"]

    def work_path(self, task_id: str, rel: str) -> Path:
        work = fusion._load_task(task_id)["work_path"]
        return Path(work).joinpath(*rel.split("/"))


class TestPrepare(FusionTestCase):
    def test_tasks_dir_env_and_default(self):
        os.environ.pop("FUSION_TASKS_DIR", None)
        self.assertEqual(fusion.tasks_dir(), fusion.TOOL_DIR / ".fusion" / "tasks")
        os.environ["FUSION_TASKS_DIR"] = str(self.root / "elsewhere")
        self.assertEqual(fusion.tasks_dir(), self.root / "elsewhere")

    def test_prepare_creates_copy_without_touching_originals(self):
        before = {p: sha_of(p) for p in self.ws.rglob("*") if p.is_file()}
        task_id = self.prepare()

        after = {p: sha_of(p) for p in self.ws.rglob("*") if p.is_file()}
        self.assertEqual(before, after, "prepare 不得改动原工作区任何文件")

        info = fusion.cmd_get(task_id)
        self.assertEqual(info["status"], "prepared")
        self.assertTrue(Path(info["brief_path"]).is_file())
        work = Path(info["work_path"])
        self.assertEqual((work / "app.py").read_text(encoding="utf-8"),
                         (self.ws / "app.py").read_text(encoding="utf-8"))
        self.assertEqual((work / "notes.md").read_text(encoding="utf-8"),
                         (self.ws / "notes.md").read_text(encoding="utf-8"))
        # 基线镜像存在，供 verify 生成 diff
        tdir = fusion._task_dir(task_id)
        self.assertTrue((tdir / "orig" / "app.py").is_file())

        brief = Path(info["brief_path"]).read_text(encoding="utf-8")
        for needle in ("请给 app.py 增加 multiply 函数", "实现 multiply 函数",
                       "app.py", "feature.py", "notes.md", "smoke", "应返回的总结"):
            self.assertIn(needle, brief)

    def test_prepare_rejects_bad_paths_and_files(self):
        cases = []
        # 路径穿越与绝对路径
        cases.append({"allowed_files": ["../evil.txt"]})
        cases.append({"allowed_files": ["a/../../evil.txt"]})
        cases.append({"allowed_files": ["/etc/hosts"]})
        # 重复
        cases.append({"allowed_files": ["app.py", "app.py"]})
        # 受保护目录
        (self.ws / ".git").mkdir()
        (self.ws / ".git" / "config").write_text("[core]\n", encoding="utf-8")
        cases.append({"allowed_files": [".git/config"]})
        # 目录不是常规文件
        cases.append({"allowed_files": ["tests"]})
        # 缺失的 context 文件
        cases.append({"context_files": ["missing.md"]})
        # requirements / allowed_files 为空
        cases.append({"requirements": []})
        cases.append({"allowed_files": []})
        # requirements 重复
        cases.append({"requirements": ["唯一要求", "唯一要求"]})
        # 非法 check
        cases.append({"checks": [{"name": "", "argv": ["true"]}]})
        cases.append({"checks": [{"name": "x", "argv": []}]})
        cases.append({"checks": [{"name": "x", "argv": ["true"], "timeout": 0}]})
        # 非绝对 workspace
        cases.append({"workspace": "relative/path"})
        # 空请求
        cases.append({"request": "   "})

        for i, override in enumerate(cases):
            with self.subTest(case=i, spec=override):
                with self.assertRaises(fusion.FusionError):
                    fusion.cmd_prepare(self.make_spec(**override))
        self.assertFalse(fusion.tasks_dir().exists() and any(fusion.tasks_dir().iterdir()),
                         "失败的 prepare 不应留下任务数据")

    def test_prepare_rejects_symlinked_files_and_ancestors(self):
        # 文件本身是软链接
        (self.ws / "link.py").symlink_to(self.ws / "app.py")
        with self.assertRaises(fusion.FusionError):
            fusion.cmd_prepare(self.make_spec(allowed_files=["link.py"], context_files=[]))
        # 祖先目录是软链接
        real_dir = self.root / "real_dir"
        real_dir.mkdir()
        (real_dir / "mod.py").write_text("x = 1\n", encoding="utf-8")
        (self.ws / "pkg").symlink_to(real_dir)
        with self.assertRaises(fusion.FusionError):
            fusion.cmd_prepare(self.make_spec(allowed_files=["pkg/mod.py"], context_files=[]))
        # 指向副本外的 context 软链接同样拒绝
        with self.assertRaises(fusion.FusionError):
            fusion.cmd_prepare(self.make_spec(allowed_files=["app.py"],
                                              context_files=["link.py"]))

    def test_prepare_rejects_symlinked_workspace_root(self):
        real = self.root / "real_ws"
        real.mkdir()
        (real / "app.py").write_text("x = 1\n", encoding="utf-8")
        alias = self.root / "alias_ws"
        alias.symlink_to(real)
        with self.assertRaises(fusion.FusionError) as ctx:
            fusion.cmd_prepare(self.make_spec(workspace=str(alias)))
        self.assertIn("workspace", str(ctx.exception))
        self.assertIn("软链接", str(ctx.exception))

    def test_prepare_rejects_oversized_file(self):
        big = self.ws / "big.bin"
        big.write_bytes(b"0" * 64)
        with unittest.mock.patch.object(fusion, "MAX_FILE_BYTES", 10):
            with self.assertRaises(fusion.FusionError):
                fusion.cmd_prepare(self.make_spec(allowed_files=["big.bin"], context_files=[]))


class TestVerify(FusionTestCase):
    def test_verify_fails_on_out_of_scope_change(self):
        task_id = self.prepare()
        work = self.work_path(task_id, "rogue.txt")
        work.write_text("越界产物\n", encoding="utf-8")
        record = fusion.cmd_verify(task_id)
        self.assertFalse(record["passed"])
        self.assertIn("rogue.txt", record["changes"]["out_of_scope"]["added"])
        self.assertTrue(any("越界" in f for f in record["failures"]))
        info = fusion.cmd_get(task_id)
        self.assertFalse(info["verification"]["passed"])

    def test_verify_ignores_python_cache_files_only(self):
        task_id = self.prepare()
        app = self.work_path(task_id, "app.py")
        app.write_text("def add(a, b):\n    return a + b\n\n\ndef multiply(a, b):\n    return a * b\n",
                       encoding="utf-8")
        cache = self.work_path(task_id, "__pycache__/app.cpython-313.pyc")
        cache.parent.mkdir(parents=True)
        cache.write_bytes(b"\x00pyc-garbage")
        record = fusion.cmd_verify(task_id)
        self.assertTrue(record["passed"], f"__pycache__ 应被忽略：{record['failures']}")
        self.assertNotIn("__pycache__/app.cpython-313.pyc", record["post_manifest"])

    def test_verify_fails_on_nonzero_exit_check(self):
        task_id = self.prepare(
            checks=[{"name": "must-fail", "argv": [sys.executable, "-c", "raise SystemExit(3)"]}])
        record = fusion.cmd_verify(task_id)
        self.assertFalse(record["passed"])
        self.assertEqual(record["checks"][0]["exit_code"], 3)
        self.assertTrue(any("must-fail" in f for f in record["failures"]))

    def test_verify_fails_on_timeout(self):
        task_id = self.prepare(checks=[{
            "name": "slow",
            "argv": [sys.executable, "-c", "import time; time.sleep(10)"],
            "timeout": 1,
        }])
        record = fusion.cmd_verify(task_id)
        self.assertFalse(record["passed"])
        self.assertTrue(record["checks"][0]["timed_out"])
        self.assertLess(record["checks"][0]["duration_s"], 8)

    def test_verify_fails_on_fifo_without_blocking(self):
        task_id = self.prepare(allowed_files=["app.py", "pipe"])
        os.mkfifo(self.work_path(task_id, "pipe"))
        env = dict(os.environ, FUSION_TASKS_DIR=str(self.tasks_root))
        try:
            proc = subprocess.run(
                [sys.executable, str(FUSION_ROOT / "fusion.py"), "verify", task_id],
                capture_output=True, text=True, env=env, timeout=30)
        except subprocess.TimeoutExpired:
            self.fail("verify 在 FIFO 上阻塞（回归：特殊文件内容不得被读取）")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        record = json.loads(proc.stdout)
        self.assertFalse(record["passed"])
        self.assertTrue(any("pipe" in f and "特殊" in f for f in record["failures"]),
                        record["failures"])
        self.assertTrue(record["diffs"]["pipe"]["binary"])
        self.assertIn("不读取内容", record["diffs"]["pipe"]["note"])

    def test_verify_reports_diff_and_changes(self):
        task_id = self.prepare()
        app = self.work_path(task_id, "app.py")
        app.write_text("def add(a, b):\n    return a + b\n\n\ndef multiply(a, b):\n    return a * b\n",
                       encoding="utf-8")
        feat = self.work_path(task_id, "feature.py")
        feat.write_text("FEATURE = True\n", encoding="utf-8")
        record = fusion.cmd_verify(task_id)
        self.assertTrue(record["passed"])
        self.assertEqual(record["changes"]["allowed"]["modified"], ["app.py"])
        self.assertEqual(record["changes"]["allowed"]["added"], ["feature.py"])
        self.assertIn("+def multiply", record["diffs"]["app.py"]["diff"])
        self.assertEqual(record["number"], 1)

        # 原文件在 verify 阶段仍保持不变
        self.assertEqual((self.ws / "app.py").read_text(encoding="utf-8"),
                         "def add(a, b):\n    return a + b\n")


class TestReview(FusionTestCase):
    def review_decisions(self, task_id: str, **per_req_overrides):
        task = fusion._load_task(task_id)
        return [{"requirement": r, "passed": True, "evidence": f"证据：{r}"}
                for r in task["spec"]["requirements"]]

    def test_review_rejects_missing_unknown_empty_evidence(self):
        task_id = self.prepare()
        fusion.cmd_verify(task_id)
        good = self.review_decisions(task_id)
        with self.assertRaises(fusion.FusionError):  # 遗漏要求
            fusion.cmd_review(task_id, good[:1])
        with self.assertRaises(fusion.FusionError):  # 多余要求
            fusion.cmd_review(task_id, good + [{"requirement": "不存在的", "passed": True,
                                                "evidence": "x"}])
        with self.assertRaises(fusion.FusionError):  # 重复要求
            fusion.cmd_review(task_id, good + [dict(good[0])])
        bad_evidence = [dict(d, evidence="   ") for d in good]
        with self.assertRaises(fusion.FusionError):  # 空证据
            fusion.cmd_review(task_id, bad_evidence)
        self.assertEqual(fusion.cmd_get(task_id)["status"], "prepared",
                         "被拒绝的审查不得改变状态")

    def test_failed_check_blocks_approval(self):
        task_id = self.prepare(
            checks=[{"name": "must-fail", "argv": [sys.executable, "-c", "raise SystemExit(1)"]}])
        record = fusion.cmd_verify(task_id)
        self.assertFalse(record["passed"])
        with self.assertRaises(fusion.FusionError):
            fusion.cmd_review(task_id, self.review_decisions(task_id))
        self.assertEqual(fusion.cmd_get(task_id)["status"], "prepared")

    def test_review_without_verify_is_rejected(self):
        task_id = self.prepare()
        with self.assertRaises(fusion.FusionError):
            fusion.cmd_review(task_id, self.review_decisions(task_id))

    def test_review_marks_needs_revision_when_requirement_fails(self):
        task_id = self.prepare()
        fusion.cmd_verify(task_id)
        decisions = [{"requirement": "实现 multiply 函数", "passed": False,
                      "evidence": "副本中没有 multiply 的实现"},
                     {"requirement": "不修改清单外文件", "passed": True, "evidence": "清单一致"}]
        result = fusion.cmd_review(task_id, decisions)
        self.assertEqual(result["status"], "needs_revision")
        self.assertEqual(result["failed_requirements"], ["实现 multiply 函数"])
        self.assertEqual(fusion.cmd_get(task_id)["status"], "needs_revision")

    def test_review_requires_ack_for_existing_test_changes(self):
        task_id = self.prepare(allowed_files=["app.py", "tests/test_app.py"])
        test = self.work_path(task_id, "tests/test_app.py")
        test.write_text("from app import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
                        "# 放宽断言\n", encoding="utf-8")
        record = fusion.cmd_verify(task_id)
        self.assertTrue(record["passed"], "清单内的测试文件修改本身不导致验证失败")
        self.assertEqual(record["test_changes"], ["tests/test_app.py"])
        decisions = self.review_decisions(task_id)
        with self.assertRaises(fusion.FusionError):  # 未显式确认 → 拒绝
            fusion.cmd_review(task_id, decisions, test_changes_acknowledged=False)
        result = fusion.cmd_review(task_id, decisions, test_changes_acknowledged=True)
        self.assertEqual(result["status"], "approved")

    def test_review_rejects_non_boolean_ack(self):
        task_id = self.prepare()
        decisions = [{"requirement": r, "passed": True, "evidence": "x"}
                     for r in fusion.cmd_get(task_id)["requirements"]]
        # MCP 曾把字符串 'false' 经 bool() 当成确认；必须只接受真布尔值
        for bad in ("false", "true", 1, None):
            with self.assertRaises(fusion.FusionError) as ctx:
                fusion.cmd_review(task_id, decisions, test_changes_acknowledged=bad)
            self.assertIn("布尔值", str(ctx.exception))


class TestApply(FusionTestCase):
    def edit_work_copy(self, task_id: str):
        app = self.work_path(task_id, "app.py")
        app.write_text("def add(a, b):\n    return a + b\n\n\ndef multiply(a, b):\n    return a * b\n",
                       encoding="utf-8")
        feat = self.work_path(task_id, "feature.py")
        feat.write_text("FEATURE = True\n", encoding="utf-8")

    def test_full_flow_prepare_verify_review_apply(self):
        old_app = (self.ws / "app.py").read_text(encoding="utf-8")
        task_id = self.prepare()

        self.edit_work_copy(task_id)
        record = fusion.cmd_verify(task_id)
        self.assertTrue(record["passed"], record["failures"])
        self.assertIn("print('ok')", record["checks"][0]["argv"])

        review = fusion.cmd_review(
            task_id,
            [{"requirement": "实现 multiply 函数", "passed": True, "evidence": "work/app.py 已包含 multiply"},
             {"requirement": "不修改清单外文件", "passed": True, "evidence": "verify 无越界改动"}],
        )
        self.assertEqual(review["status"], "approved")

        result = fusion.cmd_apply(task_id)
        self.assertEqual(result["status"], "applied")
        self.assertEqual(fusion.cmd_get(task_id)["status"], "applied")

        new_app = (self.ws / "app.py").read_text(encoding="utf-8")
        self.assertIn("def multiply", new_app)
        self.assertTrue((self.ws / "feature.py").is_file())
        self.assertEqual((self.ws / "feature.py").read_text(encoding="utf-8"), "FEATURE = True\n")
        # 原有文件未被破坏，notes.md 未动
        self.assertIn("def add", new_app)
        self.assertEqual((self.ws / "notes.md").read_text(encoding="utf-8"), "project notes\n")
        # before 备份保留旧内容
        backup_dir = Path(result["backup_dir"])
        backup_app = backup_dir / "app.py"
        self.assertTrue(backup_app.is_file())
        self.assertEqual(backup_app.read_text(encoding="utf-8"), old_app)
        # 基线镜像仍为原始内容
        tdir = fusion._task_dir(task_id)
        self.assertEqual((tdir / "orig" / "app.py").read_text(encoding="utf-8"), old_app)

    def test_apply_rejects_when_original_changed_after_prepare(self):
        task_id = self.prepare()
        self.edit_work_copy(task_id)
        fusion.cmd_verify(task_id)
        fusion.cmd_review(task_id, [
            {"requirement": "实现 multiply 函数", "passed": True, "evidence": "已实现"},
            {"requirement": "不修改清单外文件", "passed": True, "evidence": "无越界"}])

        # 用户在 prepare 之后、apply 之前改了原文件
        (self.ws / "app.py").write_text("def add(a, b):\n    return a + b  # 用户手改\n",
                                        encoding="utf-8")
        user_content = (self.ws / "app.py").read_text(encoding="utf-8")
        with self.assertRaises(fusion.FusionError) as ctx:
            fusion.cmd_apply(task_id)
        self.assertIn("外部修改", str(ctx.exception))
        # 不静默覆盖：原文件保持用户的新内容，状态未变 applied
        self.assertEqual((self.ws / "app.py").read_text(encoding="utf-8"), user_content)
        self.assertEqual(fusion.cmd_get(task_id)["status"], "approved")
        self.assertFalse((self.ws / "feature.py").exists(), "预检失败时任何文件都不应写入")

    def test_apply_rejects_work_changed_after_review(self):
        task_id = self.prepare()
        self.edit_work_copy(task_id)
        fusion.cmd_verify(task_id)
        fusion.cmd_review(task_id, [
            {"requirement": "实现 multiply 函数", "passed": True, "evidence": "已实现"},
            {"requirement": "不修改清单外文件", "passed": True, "evidence": "无越界"}])
        # 审查之后副本又被改动
        self.work_path(task_id, "feature.py").write_text("FEATURE = False\n", encoding="utf-8")
        with self.assertRaises(fusion.FusionError) as ctx:
            fusion.cmd_apply(task_id)
        self.assertIn("审查时不一致", str(ctx.exception))
        self.assertFalse((self.ws / "feature.py").exists())

    def test_apply_refuses_symlink_target(self):
        task_id = self.prepare()
        self.edit_work_copy(task_id)
        fusion.cmd_verify(task_id)
        fusion.cmd_review(task_id, [
            {"requirement": "实现 multiply 函数", "passed": True, "evidence": "已实现"},
            {"requirement": "不修改清单外文件", "passed": True, "evidence": "无越界"}])
        # 原文件被替换为指向外部的软链接
        outside = self.root / "outside.py"
        outside.write_text("outside\n", encoding="utf-8")
        (self.ws / "app.py").unlink()
        (self.ws / "app.py").symlink_to(outside)
        with self.assertRaises(fusion.FusionError):
            fusion.cmd_apply(task_id)
        self.assertEqual(outside.read_text(encoding="utf-8"), "outside\n", "软链接目标不得被写入")

    def test_apply_rejects_workspace_root_replaced_by_symlink(self):
        task_id = self.prepare()
        self.edit_work_copy(task_id)
        fusion.cmd_verify(task_id)
        fusion.cmd_review(task_id, [
            {"requirement": "实现 multiply 函数", "passed": True, "evidence": "已实现"},
            {"requirement": "不修改清单外文件", "passed": True, "evidence": "无越界"}])
        # prepare 之后整个工作区根被替换为指向别处的软链接
        real = self.root / "ws_real"
        os.rename(self.ws, real)
        os.symlink(real, self.ws)
        with self.assertRaises(fusion.FusionError) as ctx:
            fusion.cmd_apply(task_id)
        self.assertIn("软链接", str(ctx.exception))
        # 真实目录未被写入任何变化
        self.assertNotIn("def multiply", (real / "app.py").read_text(encoding="utf-8"))
        self.assertFalse((real / "feature.py").exists())
        self.assertEqual(fusion.cmd_get(task_id)["status"], "approved")

    def test_apply_refuses_deletion(self):
        task_id = self.prepare(allowed_files=["app.py", "scratch.txt"])
        self.work_path(task_id, "scratch.txt").unlink()
        fusion.cmd_verify(task_id)
        fusion.cmd_review(task_id, [
            {"requirement": "实现 multiply 函数", "passed": True, "evidence": "已实现"},
            {"requirement": "不修改清单外文件", "passed": True, "evidence": "无越界"}])
        with self.assertRaises(fusion.FusionError) as ctx:
            fusion.cmd_apply(task_id)
        self.assertIn("删除", str(ctx.exception))
        self.assertTrue((self.ws / "scratch.txt").exists())


class TestRetry(FusionTestCase):
    def test_retry_updates_brief_and_clears_state(self):
        task_id = self.prepare()
        fusion.cmd_verify(task_id)
        fusion.cmd_review(task_id, [
            {"requirement": "实现 multiply 函数", "passed": False, "evidence": "未见实现"},
            {"requirement": "不修改清单外文件", "passed": True, "evidence": "无越界"}])
        self.assertEqual(fusion.cmd_get(task_id)["status"], "needs_revision")

        result = fusion.cmd_retry(task_id, "multiply 缺少对负数的处理，请补充。")
        self.assertEqual(result["rework_rounds"], 1)
        info = fusion.cmd_get(task_id)
        self.assertIsNone(info["verification"], "retry 必须清空验证记录")
        self.assertIsNone(info["review"], "retry 必须清空审查记录")
        self.assertIn("multiply 缺少对负数的处理", Path(info["brief_path"]).read_text(encoding="utf-8"))
        # 同一副本保留：之前的（若有）修改仍在 work 中
        self.assertTrue(Path(info["work_path"]).is_dir())
        # 清空后未重新 verify 不能 review
        with self.assertRaises(fusion.FusionError):
            fusion.cmd_review(task_id, [{"requirement": r, "passed": True, "evidence": "x"}
                                        for r in info["requirements"]])

    def test_retry_rejects_empty_feedback(self):
        task_id = self.prepare()
        with self.assertRaises(fusion.FusionError):
            fusion.cmd_retry(task_id, "  ")

    def test_retry_limit_is_two_rounds(self):
        task_id = self.prepare()
        fusion.cmd_retry(task_id, "第一轮反馈")
        fusion.cmd_retry(task_id, "第二轮反馈")
        with self.assertRaises(fusion.FusionError) as ctx:
            fusion.cmd_retry(task_id, "第三轮反馈")
        self.assertIn("上限", str(ctx.exception))
        self.assertEqual(fusion.cmd_get(task_id)["rework_rounds"], 2)


class TestSubprocessCLI(FusionTestCase):
    def run_cli(self, *args):
        env = dict(os.environ, FUSION_TASKS_DIR=str(self.tasks_root))
        return subprocess.run([sys.executable, str(FUSION_ROOT / "fusion.py"), *args],
                              capture_output=True, text=True, env=env, timeout=120)

    def test_cli_json_output_success_and_error(self):
        spec_file = self.root / "spec.json"
        spec_file.write_text(json.dumps(self.make_spec(), ensure_ascii=False), encoding="utf-8")
        proc = self.run_cli("prepare", "--spec", str(spec_file))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["status"], "prepared")
        task_id = payload["task_id"]
        # CLI 子进程同样遵守 FUSION_TASKS_DIR
        self.assertTrue((self.tasks_root / task_id / "task.json").is_file())
        self.assertFalse((fusion.TOOL_DIR / ".fusion" / "tasks" / task_id).exists(),
                         "任务数据不得写入脚本默认目录")

        proc = self.run_cli("get", task_id)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["task_id"], task_id)

        bad_spec = self.root / "bad.json"
        bad_spec.write_text(json.dumps(self.make_spec(allowed_files=[]), ensure_ascii=False),
                            encoding="utf-8")
        proc = self.run_cli("prepare", "--spec", str(bad_spec))
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, "", "错误时 stdout 不应有内容")
        err = json.loads(proc.stderr)
        self.assertIn("allowed_files", err["error"]["message"])

        proc = self.run_cli("get", "0" * 32)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("任务不存在", json.loads(proc.stderr)["error"]["message"])


class TestSubprocessMCP(FusionTestCase):
    def test_mcp_initialize_list_call_and_errors(self):
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

        try:
            send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2024-11-05",
                             "capabilities": {}, "clientInfo": {"name": "unittest"}}})
            init = recv()
            self.assertEqual(init["id"], 1)
            self.assertEqual(init["result"]["serverInfo"]["name"], "fusion")
            self.assertIn("Lead", init["result"]["instructions"])

            send({"jsonrpc": "2.0", "method": "notifications/initialized"})  # 通知：不回应

            send({"jsonrpc": "2.0", "id": 0, "method": "ping"})  # id=0 是合法请求
            pong = recv()
            self.assertEqual(pong["id"], 0)
            self.assertEqual(pong["result"], {})

            send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            listing = recv()
            tools = listing["result"]["tools"]
            names = {t["name"] for t in tools}
            # 既有六项必须仍然存在（v0.2.0 另有五个新工具，在 test_workflow.py 中逐一实际调用）
            original_six = {"fusion_prepare", "fusion_get", "fusion_verify",
                            "fusion_review", "fusion_retry", "fusion_apply"}
            self.assertTrue(original_six <= names,
                            f"既有六项工具缺失：{original_six - names}")
            self.assertEqual(len(names), len(tools), "工具名不得重复")
            for t in tools:
                self.assertIn("inputSchema", t)
                self.assertIn("annotations", t)
            get_tool = next(t for t in tools if t["name"] == "fusion_get")
            self.assertTrue(get_tool["annotations"]["readOnlyHint"])
            apply_tool = next(t for t in tools if t["name"] == "fusion_apply")
            self.assertTrue(apply_tool["annotations"]["destructiveHint"])

            # 业务失败：查询不存在的任务 → isError
            send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                  "params": {"name": "fusion_get", "arguments": {"task_id": "0" * 32}}})
            miss = recv()
            self.assertEqual(miss["id"], 3)
            self.assertTrue(miss["result"]["isError"])
            self.assertIn("任务不存在", miss["result"]["content"][0]["text"])

            # 通过 MCP prepare 一个真实任务
            send({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                  "params": {"name": "fusion_prepare",
                             "arguments": {"spec": self.make_spec()}}})
            created = recv()
            self.assertFalse(created["result"]["isError"])
            task_id = json.loads(created["result"]["content"][0]["text"])["task_id"]

            # MCP 子进程遵守 FUSION_TASKS_DIR：任务数据落在指定目录，而非脚本目录默认位置
            task_dir = self.tasks_root / task_id
            self.assertTrue((task_dir / "brief.md").is_file(),
                            "brief.md 应写入 FUSION_TASKS_DIR 指定目录")
            self.assertTrue((task_dir / "work" / "app.py").is_file())
            self.assertFalse((fusion.TOOL_DIR / ".fusion" / "tasks" / task_id).exists(),
                             "MCP 子进程不得把任务写入脚本默认目录")

            send({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                  "params": {"name": "fusion_get", "arguments": {"task_id": task_id}}})
            got = recv()
            self.assertFalse(got["result"]["isError"])
            self.assertEqual(json.loads(got["result"]["content"][0]["text"])["status"],
                             "prepared")

            # 未知方法 → 标准 -32601
            send({"jsonrpc": "2.0", "id": 6, "method": "no/such/method"})
            err = recv()
            self.assertEqual(err["id"], 6)
            self.assertEqual(err["error"]["code"], -32601)

            # 未知工具 → isError 结果
            send({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                  "params": {"name": "fusion_nope", "arguments": {}}})
            unknown = recv()
            self.assertTrue(unknown["result"]["isError"])
            self.assertIn("未知工具", unknown["result"]["content"][0]["text"])

            # 字符串 'false' 不是布尔确认，必须是参数错误而不是被当成已确认
            send({"jsonrpc": "2.0", "id": 8, "method": "tools/call",
                  "params": {"name": "fusion_review", "arguments": {
                      "task_id": task_id,
                      "decisions": [
                          {"requirement": "实现 multiply 函数", "passed": True, "evidence": "x"},
                          {"requirement": "不修改清单外文件", "passed": True, "evidence": "y"},
                      ],
                      "test_changes_acknowledged": "false"}}})
            bad_ack = recv()
            self.assertTrue(bad_ack["result"]["isError"])
            self.assertIn("布尔值", bad_ack["result"]["content"][0]["text"])

            # 非法 JSON 行 → -32700
            proc.stdin.write("{not json\n")
            proc.stdin.flush()
            parse_err = recv()
            self.assertIsNone(parse_err["id"])
            self.assertEqual(parse_err["error"]["code"], -32700)

            # 每条 stdout 行都是协议消息：requests(9) + parse_error(1) = 10 条响应
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
