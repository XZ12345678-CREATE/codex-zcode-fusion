#!/usr/bin/env python3
"""Fusion 原型：Codex(Lead)与 ZCode(执行者)的本地协作工具。

零第三方依赖，Python 3.10+。CLI 与 stdio MCP 共用同一核心逻辑。

流程：
  prepare  校验 spec、把 allowed/context 文件复制到独立任务副本，生成 brief.md
  handoff  生成本轮派工包（taskdir/handoff.md + result-template.json，随机 token，
           同一轮重复读取幂等），返回可直接粘贴给 ZCode 的 prompt
  (派工)   Codex 用 Computer Use 把 prompt 实际发给 ZCode 桌面会话后，
           用 dispatch 记录会话标签（工具本身不发送任何 UI 事件）
  (执行)   ZCode 只编辑任务副本 work/ 目录，并把结构化结果原子写入 taskdir/result.json
  result   严格校验 result.json（task_id/round 真整数/token ASCII/白名单/证据覆盖/
           与实际变化完全一致；副本存在越界改动即拒绝），存为不可信执行者证据；
           不改变状态，不替代 verify
  verify   清单比较(改动/越界/既有测试文件)、unified diff、运行 Lead 验收命令
  review   Lead 逐条要求给出 passed/evidence，全部通过才 approved
  retry    记录返工反馈，保留同一副本，清空验证/审查/本轮结果与派工 token；
           下一轮 handoff 换新 token；保留会话标签与历史记录
  apply    仅 approved 且副本与审查一致时应用回原工作区；先持久化 in-progress
           journal（含逐文件备份位置与 before/after 摘要），再逐文件写入并保存
           进度；失败/中断后禁止盲目重试，需人工核查并显式 confirm_recovery 后
           继续（不承诺多文件事务、自动回滚或掉电一致性）；journal 处于活动状态
           时 retry/handoff/dispatch/result/verify/review 一律拒绝
  list     列出所有任务摘要与读取错误（忽略非 ID 目录，单任务损坏不拖垮整体）
  resume   只读恢复视图：路径、原需求、会话标签、最新结果/验证/审查摘要，
           按实际状态与副本一致性给出 next_action/reason

并发：同一任务的写操作（handoff/dispatch/result/verify/review/retry/apply）在
taskdir/task.lock 上使用非阻塞进程文件锁（flock）覆盖完整读-改-保存动作；锁被
占用时明确报忙，持锁进程退出后即可恢复；保存状态始终是原子替换。锁只保护本桥
的并发调用，不约束 ZCode 直接写副本文件。

安全边界（诚实声明）：文件范围控制 = 副本检测 + 应用闸门，不是操作系统沙盒；
不读取、不处理任何凭据；不做网络访问。
"""

from __future__ import annotations

import argparse
import contextlib
import difflib
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import stat as stat_mod
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

VERSION = "0.2.0"
TOOL_DIR = Path(__file__).resolve().parent

MAX_FILE_BYTES = 10 * 1024 * 1024      # 每个可复制文件的大小上限
MAX_RESULT_JSON_BYTES = 1 * 1024 * 1024  # 执行者结构化结果 result.json 的大小上限（1MB）
DEFAULT_CHECK_TIMEOUT = 120.0          # 验收命令默认超时（秒）
MAX_CHECK_OUTPUT_CHARS = 60_000        # 每条验收命令 stdout/stderr 保留上限
MAX_DIFF_LINES = 300                   # 每个文件 unified diff 的行数上限
MAX_DIFF_CHARS = 20_000                # 每个文件 unified diff 的字符上限
MAX_EVENTS = 200                       # task.json 中保留的事件条数上限（超出丢弃最旧）
FORBIDDEN_DIR_NAMES = {".git", ".codex", ".agents", ".zcode"}
TASK_ID_RE = re.compile(r"[0-9a-f]{32}")

STATUS_PREPARED = "prepared"
STATUS_NEEDS_REVISION = "needs_revision"
STATUS_APPROVED = "approved"
STATUS_APPLIED = "applied"

# apply journal 的非完成态：见到即阻止盲目 apply/retry
JOURNAL_ACTIVE_STATES = ("in_progress", "failed", "interrupted")

MCP_INSTRUCTIONS = (
    "Fusion 协作服务。Codex 担任 Lead：用 fusion_prepare 下发任务书；"
    "fusion_handoff 生成本轮派工包（prompt + result 模板，token 同轮幂等）；"
    "Lead 用 Computer Use 把 prompt 实际发送给已登录 ZCode 桌面会话后，"
    "再用 fusion_dispatch 记录会话标签（本工具不发送 UI 事件）。"
    "ZCode 只编辑任务副本 work/ 目录，并把结构化结果原子写入 taskdir/result.json；"
    "fusion_result 严格校验后把它保存为不可信执行者证据（不改变状态、不替代 verify）。"
    "fusion_verify 在副本上运行 Lead 验收命令并检测越界改动；fusion_review 逐条要求审查，"
    "全部通过才 approved；fusion_apply 才把 allowed_files 内的改动应用回原工作区"
    "（带逐文件 journal，失败/中断后其他流程操作被拒绝，需人工核查并显式 confirm_recovery）。"
    "fusion_list / fusion_resume 跨进程找回任务与证据并给出下一步建议（只读，不自动重发）。"
    "范围限制：仅操作 prepare 声明的文件；副本检测与应用闸门不等于操作系统沙盒；"
    "不处理任何凭据；headless 认证未接通，依赖已登录桌面会话。"
)


class FusionError(Exception):
    """业务/校验错误，以稳定消息返回给 CLI 与 MCP，不包含任何敏感信息。"""


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def tasks_dir() -> Path:
    """任务存储目录：默认本工具目录下 .fusion/tasks/，可用 FUSION_TASKS_DIR 覆盖。

    安装到 Codex 插件缓存等不宜写入的位置时，Lead 应把 FUSION_TASKS_DIR
    指向当前项目中的可写目录，避免写插件缓存。
    """
    return Path(os.environ.get("FUSION_TASKS_DIR") or (TOOL_DIR / ".fusion" / "tasks"))


def _task_dir(task_id: str) -> Path:
    return tasks_dir() / task_id


def _result_path(task_id: str) -> Path:
    """执行者结构化结果的固定路径（taskdir/result.json，位于 work/ 之外）。"""
    return _task_dir(task_id) / "result.json"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _save_task(task: dict) -> None:
    tdir = _task_dir(task["id"])
    tmp = tdir / "task.json.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(task, f, ensure_ascii=False, indent=2)
    os.replace(tmp, tdir / "task.json")


def _load_task(task_id: str) -> dict:
    if not isinstance(task_id, str) or not TASK_ID_RE.fullmatch(task_id):
        raise FusionError(f"任务不存在：{task_id}")
    tjson = _task_dir(task_id) / "task.json"
    if not tjson.is_file():
        raise FusionError(f"任务不存在：{task_id}")
    try:
        with open(tjson, encoding="utf-8") as f:
            task = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise FusionError(f"任务数据读取失败（可能已损坏）：{task_id}：{exc}") from exc
    if not isinstance(task, dict) or task.get("id") != task_id:
        raise FusionError(f"任务数据异常：{task_id}")
    return task


def _require_task_dir(task_id: str) -> None:
    """加锁前的廉价存在性检查；真正的读取仍在锁内完成。"""
    if not isinstance(task_id, str) or not TASK_ID_RE.fullmatch(task_id):
        raise FusionError(f"任务不存在：{task_id}")
    if not (_task_dir(task_id) / "task.json").is_file():
        raise FusionError(f"任务不存在：{task_id}")


def _add_event(task: dict, type_: str, **fields) -> None:
    """把重要操作事件追加到 task.json（随原子保存持久化），并限制总条数。"""
    event = {"at": _now(), "type": type_}
    event.update({k: v for k, v in fields.items() if v is not None})
    events = task.setdefault("events", [])
    events.append(event)
    if len(events) > MAX_EVENTS:
        del events[:len(events) - MAX_EVENTS]


def _require_no_active_journal(task: dict, action: str) -> None:
    """apply journal 未完成时，除 apply 的显式恢复路径与只读操作（get/list/resume）外一律拒绝。

    尤其 retry 不得把半应用任务改成 needs_revision，也不得用后续 review/reset 掩盖 journal。
    """
    journal = task.get("apply_journal")
    if journal and journal.get("status") in JOURNAL_ACTIVE_STATES:
        raise FusionError(
            f"上次 apply 未完成（journal 状态 {journal['status']}），{action} 被拒绝："
            f"已确认写入：{journal.get('files_done') or '无'}；"
            f"进度未保存（可能已写入，需核对实际源文件）：{journal.get('files_pending') or '无'}；"
            f"备份目录（保留原备份）：{journal.get('backup_dir') or '无'}。"
            "请先用 resume 人工核查，并通过 apply --confirm-recovery 完成或处理后，"
            "再继续其他流程操作；不得用 retry/review 等操作掩盖 journal")


# ---------------------------------------------------------------------------
# 并发锁：非阻塞进程文件锁（flock）
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _task_lock(task_id: str, action: str):
    """对单个任务的写操作加非阻塞排他锁，覆盖完整读-改-保存动作。

    - 忙时立即报错，绝不阻塞等待；
    - 持锁进程退出后内核自动释放，后续操作即可恢复；
    - 不得嵌套获取（同一进程重复 flock 会自锁）；各 cmd_* 之间互不调用。
    - 锁只约束本桥的并发调用，不约束 ZCode 直接写副本文件。
    """
    lock_path = _task_dir(task_id) / "task.lock"
    try:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError as exc:
        raise FusionError(f"无法打开任务锁文件（{action}）：{task_id}：{exc}") from exc
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise FusionError(
                f"任务 {task_id} 正被其他进程操作（{action}），当前忙，请稍后重试"
            ) from exc
        try:
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# 路径校验
# ---------------------------------------------------------------------------

def _validate_relpath(rel, field: str) -> str:
    """校验相对路径并返回规范化字符串。禁止绝对路径、..、.、空组件与受保护目录。"""
    if not isinstance(rel, str) or not rel.strip():
        raise FusionError(f"{field}：路径必须是非空字符串")
    if "\\" in rel:
        raise FusionError(f"{field}：不支持反斜杠路径：{rel}")
    if rel.startswith("/") or os.path.isabs(rel):
        raise FusionError(f"{field}：不允许绝对路径：{rel}")
    parts = rel.split("/")
    for part in parts:
        if part == "":
            raise FusionError(f"{field}：不允许空路径组件（双斜杠）：{rel}")
        if part in (".", ".."):
            raise FusionError(f"{field}：不允许 '.' 或 '..' 组件：{rel}")
        if part in FORBIDDEN_DIR_NAMES:
            raise FusionError(f"{field}：禁止访问受保护目录 {part}：{rel}")
    return "/".join(parts)


def _assert_no_symlink_chain(base: Path, parts: list[str]) -> None:
    """从 base 自身开始逐级检查：任何已存在组件（含工作区根、目标文件及其祖先路径）是软链接即拒绝。"""
    cur = base
    if cur.is_symlink():
        raise FusionError(f"路径包含软链接（含工作区根/祖先路径）：{cur}")
    for part in parts:
        cur = cur / part
        if cur.is_symlink():
            raise FusionError(f"路径包含软链接（含工作区根/祖先路径）：{cur}")


# ---------------------------------------------------------------------------
# 副本清单
# ---------------------------------------------------------------------------

def _is_ignored_cache(rel: str) -> bool:
    """仅忽略 Python 生成缓存：__pycache__ 目录内容与 .pyc 文件，其余一律不忽略。"""
    parts = rel.split("/")
    return "__pycache__" in parts or rel.endswith(".pyc")


def scan_manifest(root: Path) -> dict:
    """扫描目录，返回 {相对路径: sha256 或 'symlink:...' 标记}，不跟随目录软链接。"""
    manifest: dict[str, str] = {}

    def walk(directory: str, prefix: str) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name)
        except OSError as exc:
            raise FusionError(f"扫描副本目录失败：{directory}：{exc}") from exc
        for entry in entries:
            rel = prefix + entry.name
            if entry.is_symlink():
                if not _is_ignored_cache(rel):
                    manifest[rel] = "symlink:" + os.readlink(entry.path)
            elif entry.is_dir(follow_symlinks=False):
                walk(entry.path, rel + "/")
            elif entry.is_file(follow_symlinks=False):
                if not _is_ignored_cache(rel):
                    manifest[rel] = sha256_file(Path(entry.path))
            else:
                manifest[rel] = "special"

    walk(str(root), "")
    return manifest


def manifest_digest(manifest: dict) -> str:
    payload = json.dumps(sorted(manifest.items()), separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# spec 校验
# ---------------------------------------------------------------------------

def _validate_spec(spec: dict) -> dict:
    if not isinstance(spec, dict):
        raise FusionError("spec 必须是 JSON 对象")

    ws = spec.get("workspace")
    if not isinstance(ws, str) or not ws.strip():
        raise FusionError("workspace 必须是非空字符串（绝对路径）")
    if not os.path.isabs(ws):
        raise FusionError(f"workspace 必须是绝对路径：{ws}")
    ws_path = Path(ws)
    if ws_path.is_symlink():
        raise FusionError(
            f"workspace 根路径本身是软链接，拒绝：{ws}（请提供真实目录路径，而不是指向它的别名/链接）"
        )
    if not ws_path.is_dir():
        raise FusionError(f"workspace 不是有效目录：{ws}")

    request = spec.get("request")
    if not isinstance(request, str) or not request.strip():
        raise FusionError("request（用户原话）必须是非空字符串")

    instructions = spec.get("instructions", "")
    if not isinstance(instructions, str):
        raise FusionError("instructions 必须是字符串")

    reqs_raw = spec.get("requirements")
    if not isinstance(reqs_raw, list) or not reqs_raw:
        raise FusionError("requirements 必须是非空列表")
    requirements = []
    for r in reqs_raw:
        if not isinstance(r, str) or not r.strip():
            raise FusionError("每条 requirement 必须是非空字符串")
        requirements.append(r.strip())
    if len(set(requirements)) != len(requirements):
        raise FusionError("requirements 存在重复项")

    allowed_raw = spec.get("allowed_files")
    if not isinstance(allowed_raw, list) or not allowed_raw:
        raise FusionError("allowed_files 必须是非空列表")
    context_raw = spec.get("context_files", [])
    if not isinstance(context_raw, list):
        raise FusionError("context_files 必须是列表")

    allowed: list[str] = []
    context: list[str] = []
    seen: set[str] = set()
    for raw, field, bucket in ((allowed_raw, "allowed_files", allowed),
                               (context_raw, "context_files", context)):
        for f in raw:
            norm = _validate_relpath(f, field)
            if norm in seen:
                raise FusionError(f"路径重复出现（allowed_files 与 context_files 合并去重）：{norm}")
            seen.add(norm)
            bucket.append(norm)

    checks_raw = spec.get("checks", [])
    if not isinstance(checks_raw, list):
        raise FusionError("checks 必须是列表")
    checks = []
    for c in checks_raw:
        if not isinstance(c, dict):
            raise FusionError("每项 check 必须是对象")
        name = c.get("name")
        if not isinstance(name, str) or not name.strip():
            raise FusionError("check.name 必须是非空字符串")
        argv = c.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a for a in argv):
            raise FusionError(f"check「{name}」的 argv 必须是非空字符串数组")
        timeout = c.get("timeout", DEFAULT_CHECK_TIMEOUT)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise FusionError(f"check「{name}」的 timeout 必须是正数（秒）")
        checks.append({"name": name.strip(), "argv": list(argv), "timeout": float(timeout)})

    return {
        "workspace": ws,
        "request": request.strip(),
        "instructions": instructions,
        "requirements": requirements,
        "allowed_files": allowed,
        "context_files": context,
        "checks": checks,
    }


# ---------------------------------------------------------------------------
# brief.md 生成
# ---------------------------------------------------------------------------

def _write_brief(task: dict, feedback: str | None = None) -> None:
    spec = task["spec"]
    tdir = _task_dir(task["id"])
    lines: list[str] = []
    lines.append(f"# Fusion 任务简报 {task['id']}")
    lines.append("")
    lines.append(f"- 状态：{task['status']}（返工第 {task['rework_rounds']} 轮）")
    lines.append(f"- 任务副本工作目录（只在这里编辑）：{task['work_path']}")
    lines.append(f"- 原始工作区（应用前保持不变）：{task['workspace']}")
    lines.append(f"- 任务数据目录：{tdir}")
    lines.append("")
    lines.append("## 用户原话")
    lines.append("")
    lines.append("> " + spec["request"].replace("\n", "\n> "))
    lines.append("")
    lines.append("## 指示")
    lines.append("")
    lines.append(spec["instructions"] or "（无）")
    lines.append("")
    lines.append("## 硬性要求（审查时逐条验收）")
    lines.append("")
    for i, r in enumerate(spec["requirements"], 1):
        lines.append(f"{i}. {r}")
    lines.append("")
    lines.append("## 可写文件（仅限以下相对路径；新增文件也必须已列入本清单）")
    lines.append("")
    for f in spec["allowed_files"]:
        lines.append(f"- {f}")
    lines.append("")
    lines.append("## 只读参考文件（不得修改）")
    lines.append("")
    if spec["context_files"]:
        for f in spec["context_files"]:
            lines.append(f"- {f}")
    else:
        lines.append("（无）")
    lines.append("")
    lines.append("## 验收命令（verify 将在副本工作目录中运行）")
    lines.append("")
    if spec["checks"]:
        for c in spec["checks"]:
            timeout = int(c["timeout"]) if float(c["timeout"]).is_integer() else c["timeout"]
            argv = " ".join(c["argv"])
            lines.append(f"- {c['name']}：`{argv}`（超时 {timeout} 秒，非零退出或超时即验收失败）")
    else:
        lines.append("（无，仅做范围检查）")
    lines.append("")
    lines.append("## 完成后应返回的总结")
    lines.append("")
    lines.append("按 handoff 派工包的要求，先把结构化结果 JSON 原子写入 taskdir/result.json"
                 "（本轮唯一允许的副本外输出），再在回复中说明：每条硬性要求如何满足"
                 "（附证据：文件/行为/输出）、修改或新增了哪些文件、验收命令的运行结果。")
    lines.append("")
    lines.append("## 边界与注意")
    lines.append("")
    lines.append("- 不要修改可写清单之外的任何文件；越界改动会导致验收失败。")
    lines.append("- 测试或脚本运行产生的新文件同样必须在可写清单内（Python `__pycache__`/`.pyc` 缓存除外）。")
    lines.append("- 改写既有测试文件会被明确标记，需要 Lead 显式确认后才能通过审查。")
    lines.append("- 本轮改动只允许发生在任务副本 work/ 目录内；除 work/ 内文件外，唯一允许的额外输出是把"
                 "结构化结果 JSON 原子写入任务数据目录的 result.json（先写完整临时文件再替换，路径与 schema 见 handoff 派工包）。")
    if feedback:
        lines.append("")
        lines.append(f"## 本轮返工反馈（第 {task['rework_rounds']} 轮）")
        lines.append("")
        lines.append("> " + feedback.replace("\n", "\n> "))
        lines.append("")
        lines.append("之前的验证与审查记录已清空；请基于以上反馈在同一副本上继续修改，完成后等待 verify。")
    brief_path = tdir / "brief.md"
    brief_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    task["brief_path"] = str(brief_path)


# ---------------------------------------------------------------------------
# prepare
# ---------------------------------------------------------------------------

def cmd_prepare(spec: dict) -> dict:
    validated = _validate_spec(spec)
    ws = Path(validated["workspace"])
    task_id = uuid.uuid4().hex
    tdir = tasks_dir() / task_id
    work = tdir / "work"
    orig = tdir / "orig"
    work.mkdir(parents=True)
    orig.mkdir()

    context_set = set(validated["context_files"])
    original_hashes: dict[str, str] = {}
    try:
        for rel in validated["context_files"] + validated["allowed_files"]:
            parts = rel.split("/")
            src = ws.joinpath(*parts)
            _assert_no_symlink_chain(ws, parts)
            exists = os.path.lexists(src)
            if rel in context_set and not exists:
                raise FusionError(f"context_files 文件不存在：{rel}")
            if exists:
                if src.is_symlink() or not src.is_file():
                    raise FusionError(f"只能复制常规文件（拒绝目录/软链接/特殊文件）：{rel}")
                if src.stat().st_size > MAX_FILE_BYTES:
                    raise FusionError(f"文件超过大小限制（10MB）：{rel}")
                dst_work = work.joinpath(*parts)
                dst_orig = orig.joinpath(*parts)
                dst_work.parent.mkdir(parents=True, exist_ok=True)
                dst_orig.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dst_work)
                shutil.copyfile(src, dst_orig)
                original_hashes[rel] = sha256_file(dst_work)
            else:
                # 新建 allowed 文件可以不存在，但已存在的祖先必须是真实目录
                cur = ws
                for part in parts[:-1]:
                    cur = cur / part
                    if os.path.lexists(cur) and not cur.is_dir():
                        raise FusionError(f"路径祖先不是目录：{rel}（{cur}）")
    except (FusionError, OSError) as exc:
        shutil.rmtree(tdir, ignore_errors=True)
        if isinstance(exc, OSError):
            raise FusionError(f"复制任务副本失败：{exc}") from exc
        raise

    task = {
        "id": task_id,
        "created_at": _now(),
        "status": STATUS_PREPARED,
        "rework_rounds": 0,
        "spec": validated,
        "workspace": validated["workspace"],
        "work_path": str(work),
        "brief_path": None,
        "original_hashes": original_hashes,
        "initial_manifest": scan_manifest(work),
        "verification_counter": 0,
        "verifications": [],
        "latest_verification": None,
        "review": None,
        "retry_feedbacks": [],
        "applied": None,
        # v0.2.0 流程字段（旧 v0.1.0 任务缺少这些键时按空值处理，仍可走 verify/review/apply）
        "handoff": None,
        "dispatches": [],
        "executor_result": None,
        "apply_journal": None,
        "events": [],
    }
    _add_event(task, "prepared", requirements=len(validated["requirements"]),
               allowed_files=len(validated["allowed_files"]))
    _write_brief(task)
    _save_task(task)
    return {
        "task_id": task_id,
        "status": task["status"],
        "brief_path": task["brief_path"],
        "work_path": task["work_path"],
        "workspace": task["workspace"],
        "requirements": validated["requirements"],
        "allowed_files": validated["allowed_files"],
        "context_files": validated["context_files"],
        "checks": validated["checks"],
    }


# ---------------------------------------------------------------------------
# handoff：生成本轮派工包（幂等）
# ---------------------------------------------------------------------------

def _write_handoff_files(task: dict, token: str, round_no: int) -> tuple[Path, Path, Path]:
    """写入 handoff.md 与 result-template.json（taskdir 内、work/ 外），返回三个关键路径。"""
    tdir = _task_dir(task["id"])
    spec = task["spec"]
    handoff_path = tdir / "handoff.md"
    template_path = tdir / "result-template.json"
    result_path = tdir / "result.json"
    work = task["work_path"]
    ws = task["workspace"]
    brief = task.get("brief_path") or str(tdir / "brief.md")

    lines = [
        f"# Fusion 派工包（第 {round_no} 轮）",
        "",
        f"- 任务 ID：{task['id']}",
        f"- 轮次 round：{round_no}（result.json 必须携带同一真整数值）",
        f"- 本轮 handoff_token：{token}（result.json 必须携带同一值；旧轮次 token 不被认可）",
        f"- 任务书：{brief}",
        f"- 任务副本工作目录（只在这里编辑）：{work}",
        f"- 原始工作区（应用前保持不变）：{ws}",
        f"- 结构化结果输出路径（本轮唯一允许的副本外输出）：{result_path}",
        f"- 结果模板：{template_path}",
        "",
        "## 执行规则",
        "",
        "1. 先完整阅读任务书（brief），仅在任务副本 work/ 目录内修改 allowed_files 清单内的文件。",
        "2. 除 work/ 内的改动外，唯一允许的额外输出是把结构化结果 JSON 写入上面的 result_path；"
        "必须原子写入：先写完整临时文件（例如 `result_path + '.tmp'`），再 `os.replace` 到 result_path，"
        "完整写完后再向会话报告完成；不要留下半写的 result.json。",
        "3. Codex 会独立 verify/review；result.json 只是执行者声明，不会自动批准任务。",
        "",
        "## result.json 字段（全部必填）",
        "",
        "- task_id：字符串，必须等于本任务 ID",
        "- round：真整数（不接受布尔或字符串），必须等于本轮 round",
        "- handoff_token：字符串，必须等于本轮 token",
        "- summary：非空字符串，总结本轮完成内容",
        "- changed_files：相对路径字符串数组，必须与 work/ 内实际发生变化的文件完全一致，且全部在 allowed_files 内",
        "- requirements：数组，每项 {\"requirement\": str, \"evidence\": str}，恰好覆盖任务书全部硬性要求"
        "（不缺失、不多余、不重复），evidence 非空",
        "- checks：数组，每项 {\"name\": str, \"result\": str}，为执行者声称的检查结果（仅为声称，Codex 将独立复验）",
        "- issues：字符串数组，遗留问题（无则空数组）",
        "",
        "## 允许修改的文件（与任务书一致）",
        "",
    ]
    lines.extend(f"- {f}" for f in spec["allowed_files"])
    lines.append("")
    handoff_path.write_text("\n".join(lines), encoding="utf-8")

    template = {
        "task_id": task["id"],
        "round": round_no,
        "handoff_token": token,
        "summary": "（必填）非空：总结本轮完成内容",
        "changed_files": ["（必填）与 work/ 内实际变化完全一致且在 allowed_files 内的相对路径"],
        "requirements": [
            {"requirement": "（必填）逐字使用任务书中的硬性要求原文",
             "evidence": "（必填）非空证据说明"}
        ],
        "checks": [
            {"name": "（执行过的检查/命令名）", "result": "（声称的结果，例如 passed: ...）"}
        ],
        "issues": [],
    }
    template_path.write_text(
        json.dumps(template, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return handoff_path, template_path, result_path


def _handoff_prompt(task: dict, token: str, round_no: int,
                    handoff_path: Path, result_path: Path) -> str:
    brief = task.get("brief_path") or str(_task_dir(task["id"]) / "brief.md")
    return (
        f"【Fusion 派工】任务 {task['id']}（第 {round_no} 轮）\n"
        f"请先完整阅读任务书：{brief}\n"
        f"规则：\n"
        f"1. 只在任务副本目录内修改文件：{task['work_path']}"
        f"（仅任务书 allowed_files 清单内的文件；原始工作区 {task['workspace']} 在应用前保持不变）。\n"
        f"2. 完成后把结构化结果 JSON 原子写入：{result_path}"
        f"（先写完整临时文件再 os.replace；这是本轮唯一允许的副本外输出；schema 与模板见 {handoff_path}）。\n"
        f"3. result.json 必须携带：task_id={task['id']}、round={round_no}（真整数）、handoff_token={token}；"
        f"changed_files 必须与副本内实际变化完全一致且在 allowed_files 内。\n"
        f"4. 完成后回到本会话返回摘要、执行过的检查与遗留问题；Codex 将独立 verify/review，不会直接采信该结果。"
    )


def cmd_handoff(task_id: str) -> dict:
    _require_task_dir(task_id)
    with _task_lock(task_id, "handoff"):
        task = _load_task(task_id)
        _require_no_active_journal(task, "handoff")
        if task["status"] in (STATUS_APPROVED, STATUS_APPLIED):
            raise FusionError(
                f"任务状态为 {task['status']}，无需再派工（避免对已审批/已应用任务无故再次派工）；"
                "如需继续改动请先 retry 返工或 prepare 新任务")
        expected_round = task.get("rework_rounds", 0) + 1
        current = task.get("handoff")
        tdir = _task_dir(task_id)
        handoff_file = tdir / "handoff.md"
        template_file = tdir / "result-template.json"

        if current and current.get("round") == expected_round and current.get("token"):
            # 同一轮重复读取：幂等，复用已持久化的 token，只在文件丢失时原样重建
            token = current["token"]
            round_no = expected_round
            reused = True
            if not handoff_file.is_file() or not template_file.is_file():
                _write_handoff_files(task, token, round_no)
        else:
            token = secrets.token_hex(16)
            round_no = expected_round
            reused = False
            handoff_path, template_path, result_path = _write_handoff_files(task, token, round_no)
            task["handoff"] = {
                "round": round_no,
                "token": token,
                "handoff_path": str(handoff_path),
                "result_template_path": str(template_path),
                "result_path": str(result_path),
                "created_at": _now(),
            }
            _add_event(task, "handoff", round=round_no)
            _save_task(task)

        handoff = task["handoff"]
        return {
            "task_id": task_id,
            "round": round_no,
            "token": token,
            "prompt": _handoff_prompt(task, token, round_no,
                                      Path(handoff["handoff_path"]),
                                      Path(handoff["result_path"])),
            "handoff_path": handoff["handoff_path"],
            "result_template_path": handoff["result_template_path"],
            "result_path": handoff["result_path"],
            "brief_path": task.get("brief_path"),
            "work_path": task["work_path"],
            "workspace": task["workspace"],
            "idempotent_reuse": reused,
            "note": "prompt 可直接粘贴给 ZCode；实际 UI 发送由 Lead（Computer Use/人工）完成，"
                    "发送成功后再用 dispatch 记录会话标签",
        }


# ---------------------------------------------------------------------------
# dispatch：记录真实发送后的会话标签（本工具不发送 UI 事件）
# ---------------------------------------------------------------------------

def cmd_dispatch(task_id: str, session_label) -> dict:
    if not isinstance(session_label, str) or not session_label.strip():
        raise FusionError("session_label 必须是非空字符串（ZCode 会话标签）")
    _require_task_dir(task_id)
    with _task_lock(task_id, "dispatch"):
        task = _load_task(task_id)
        _require_no_active_journal(task, "dispatch")
        if task["status"] == STATUS_APPLIED:
            raise FusionError("任务已应用，无需再记录派工")
        handoff = task.get("handoff")
        if not handoff or not handoff.get("token"):
            raise FusionError(
                "尚未生成本轮 handoff 派工包，拒绝记录派工：必须先运行 handoff 并真实发送后"
                "再 dispatch（否则会按轮次把派工误记到尚未生成的新包上）")
        label = session_label.strip()
        dispatches = task.setdefault("dispatches", [])
        for d in dispatches:
            if (d.get("round") == handoff["round"]
                    and d.get("handoff_token") == handoff["token"]
                    and d.get("session_label") == label):
                # 相同 token + 相同会话标签：幂等返回，不重复记录历史
                return {
                    "task_id": task_id,
                    "session_label": label,
                    "round": d["round"],
                    "recorded_at": d["at"],
                    "dispatch_count": len(dispatches),
                    "duplicate": True,
                    "note": "该 token 与会话标签的派工此前已记录，幂等返回不新增历史；"
                            "仅记录调用者声明已完成的真实 UI 发送；本工具不发送任何 UI 事件",
                }
        # 同轮不同会话标签：追加记录，保留可追踪的多会话历史
        entry = {
            "at": _now(),
            "session_label": label,
            "round": handoff["round"],
            "handoff_token": handoff["token"],
        }
        dispatches.append(entry)
        _add_event(task, "dispatch", session_label=label, round=entry["round"])
        _save_task(task)
        return {
            "task_id": task_id,
            "session_label": label,
            "round": entry["round"],
            "recorded_at": entry["at"],
            "dispatch_count": len(dispatches),
            "duplicate": False,
            "note": "仅记录调用者声明已完成的真实 UI 发送；本工具不发送任何 UI 事件，也不核实发送是否发生",
        }


# ---------------------------------------------------------------------------
# result：读取并严格校验固定路径的结构化结果（不可信执行者证据）
# ---------------------------------------------------------------------------

def _read_result_document(task_id: str) -> tuple[dict, str]:
    """读取 taskdir/result.json：拒绝缺失、软链接、FIFO/特殊文件、超过 1MB、非 UTF-8 JSON。"""
    path = _result_path(task_id)
    if not os.path.lexists(path):
        raise FusionError(f"结果文件尚未写入：{path}（执行者应按 handoff 派工包原子写入该固定路径）")
    if path.is_symlink():
        raise FusionError(f"结果文件是软链接，拒绝读取：{path}")
    try:
        # O_NONBLOCK 防止路径在检查与打开之间被换成 FIFO 导致阻塞；常规文件不受影响
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError as exc:
        raise FusionError(f"结果文件打开失败：{exc}") from exc
    try:
        st = os.fstat(fd)
        if not stat_mod.S_ISREG(st.st_mode):
            raise FusionError("结果文件不是常规文件（FIFO/设备等特殊文件），拒绝读取")
        if st.st_size > MAX_RESULT_JSON_BYTES:
            raise FusionError(f"结果文件超过大小限制（1MB）：当前 {st.st_size} 字节")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_RESULT_JSON_BYTES:
                raise FusionError(f"结果文件超过大小限制（1MB）：已读 {total} 字节仍未结束")
            chunks.append(chunk)
    finally:
        os.close(fd)
    data = b"".join(chunks)
    digest = hashlib.sha256(data).hexdigest()
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FusionError(f"结果文件不是有效的 UTF-8 JSON：{exc}") from exc
    if not isinstance(doc, dict):
        raise FusionError("结果必须是 JSON 对象")
    return doc, digest


def _result_file_state(task: dict) -> dict:
    """有界（≤1MB）检查 result.json 与本轮 handoff 的匹配关系，绝不无界读取大文件。

    返回 {exists, pending_import, result_error}：
    - pending_import 仅在存在有效本轮 handoff、文件可解析、round/token 匹配、
      且尚未导入（或导入后内容又变化）时为 True；
    - 文件存在但不可用（旧轮次残留/软链接/FIFO/超大/无效 JSON/round 或 token 过期）
      时给出 result_error 说明，供恢复视图给出有用下一步（生成最新 handoff、等待
      执行者纠正、重新提交），而不是建议一个会被拒绝的 import_result 造成错误循环。
    """
    state = {"exists": False, "pending_import": False, "result_error": None}
    path = _result_path(task["id"])
    if not os.path.lexists(path):
        return state
    state["exists"] = True
    handoff = task.get("handoff")
    if not handoff or not handoff.get("token"):
        state["result_error"] = (
            "磁盘上存在 result.json，但当前没有有效的本轮 handoff（多为旧轮次残留或伪造文件）："
            "不会被导入；如需执行者重新提交，请先生成最新 handoff 并派发")
        return state
    try:
        doc, digest = _read_result_document(task["id"])
    except FusionError as exc:
        state["result_error"] = (
            f"result.json 无法读取或解析（有界读取 ≤1MB）：{exc}；"
            "等待执行者按 handoff 规则原子重写后重新提交（不会自动重发）")
        return state
    r = doc.get("round")
    token = doc.get("handoff_token")
    round_ok = isinstance(r, int) and not isinstance(r, bool) and r == handoff.get("round")
    token_ok = isinstance(token, str) and token == handoff.get("token")
    if not (round_ok and token_ok):
        state["result_error"] = (
            "result.json 与本轮 handoff 不匹配（round/handoff_token 过期或无效）：不会被导入；"
            "请基于最新 handoff 重新提交")
        return state
    er = task.get("executor_result")
    state["pending_import"] = (not er) or er.get("file_sha256") != digest
    return state


def _validate_result_document(task: dict, doc: dict, digest: str) -> dict:
    """按接口约定严格校验执行者结果，返回规范化记录；任何不符合都明确拒绝。"""
    task_id = task["id"]
    handoff = task.get("handoff")
    if not handoff or not handoff.get("token"):
        raise FusionError(
            "本任务尚未生成派工包（handoff），无法导入结果；"
            "旧 v0.1.0 任务可继续直接使用 verify/review/apply 流程")

    if doc.get("task_id") != task_id:
        raise FusionError(f"结果 task_id 不匹配：期望 {task_id}，得到 {doc.get('task_id')!r}（伪造或串任务的结果被拒绝）")

    round_no = doc.get("round")
    if isinstance(round_no, bool) or not isinstance(round_no, int):
        raise FusionError("结果 round 必须是真整数（不接受布尔或字符串）")
    if round_no != handoff["round"]:
        raise FusionError(
            f"结果轮次无效或过期：期望本轮 round={handoff['round']}，结果为 {round_no}；"
            "旧轮次留在盘上的 result.json 不被认可，请基于最新 handoff 重新提交")

    token = doc.get("handoff_token")
    if not isinstance(token, str) or not token:
        raise FusionError("结果 handoff_token 必须是非空字符串")
    try:
        token.encode("ascii")
    except UnicodeEncodeError as exc:
        # 先校验格式再比较：非 ASCII 字符串会让 compare_digest 抛 TypeError
        raise FusionError(
            "结果 handoff_token 含非 ASCII 字符，格式非法（token 应为 ASCII 十六进制字符串）"
        ) from exc
    if not secrets.compare_digest(token, handoff["token"]):
        raise FusionError(
            "结果 handoff_token 不匹配（旧轮次或伪造的结果不被认可）；"
            "请读取本轮 handoff 派工包后重新生成 result.json")

    summary = doc.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise FusionError("结果 summary 必须是非空字符串")

    # changed_files：相对路径数组，逐条校验，且必须与副本实际变化一致、全部在白名单内
    cf_raw = doc.get("changed_files")
    if not isinstance(cf_raw, list) or not all(isinstance(x, str) for x in cf_raw):
        raise FusionError("结果 changed_files 必须是相对路径字符串数组")
    changed: list[str] = []
    for x in cf_raw:
        try:
            changed.append(_validate_relpath(x, "changed_files"))
        except FusionError as exc:
            raise FusionError(f"结果 changed_files 含非法路径：{exc}") from exc
    if len(set(changed)) != len(changed):
        raise FusionError("结果 changed_files 存在重复项")
    allowed = set(task["spec"]["allowed_files"])
    outside = sorted(set(changed) - allowed)
    if outside:
        raise FusionError(f"结果 changed_files 含 allowed_files 白名单外的文件：{outside}")

    work = Path(task["work_path"])
    if not work.is_dir():
        raise FusionError("任务副本目录缺失，任务数据可能已损坏")
    current = scan_manifest(work)
    initial = task["initial_manifest"]
    actual_changed = {
        rel for rel in set(initial) | set(current)
        if initial.get(rel) != current.get(rel)
    }
    actual_allowed = actual_changed & allowed
    out_of_scope = sorted(actual_changed - allowed)
    if out_of_scope:
        # 结果必须与全部实际变化完全一致且全部在白名单内：副本存在越界改动即拒绝导入；
        # verify 仍是独立后续闸门
        raise FusionError(
            "副本存在 allowed_files 白名单外的实际改动：" + ", ".join(out_of_scope)
            + "；结果导入被拒绝。请先清理越界改动后重新提交（changed_files 必须与全部实际变化"
              "完全一致且全部在白名单内）；verify 仍会作为独立闸门复核范围")
    if set(changed) != actual_allowed:
        raise FusionError(
            "结果 changed_files 与副本实际变化不一致（必须如实申报）："
            f"结果声明={sorted(set(changed))}；副本实际（白名单内）={sorted(actual_allowed)}")

    # requirements：每项 {requirement, evidence}，恰好覆盖 spec 全部硬性要求
    reqs_doc = doc.get("requirements")
    if not isinstance(reqs_doc, list):
        raise FusionError("结果 requirements 必须是数组，每项 {requirement, evidence}")
    norm_reqs: list[dict] = []
    for item in reqs_doc:
        if not isinstance(item, dict):
            raise FusionError("结果 requirements 每项必须是 {requirement, evidence} 对象")
        r_, e_ = item.get("requirement"), item.get("evidence")
        if not isinstance(r_, str) or not r_.strip():
            raise FusionError("结果 requirements.requirement 必须是非空字符串")
        if not isinstance(e_, str) or not e_.strip():
            raise FusionError(f"结果 requirements.evidence 必须是非空字符串（要求：{r_}）")
        norm_reqs.append({"requirement": r_, "evidence": e_.strip()})
    spec_reqs = list(task["spec"]["requirements"])
    counts: dict[str, int] = {}
    for d in norm_reqs:
        counts[d["requirement"]] = counts.get(d["requirement"], 0) + 1
    missing = [r for r in spec_reqs if counts.get(r, 0) == 0]
    unknown = [r for r in counts if r not in spec_reqs]
    duplicated = [r for r, c in counts.items() if c > 1]
    if missing or unknown or duplicated:
        raise FusionError(
            f"结果 requirements 未恰好覆盖所有硬性要求（缺失：{missing}；多余：{unknown}；重复：{duplicated}）")

    # checks：每项 {name, result} 字符串，仅为声称
    checks_doc = doc.get("checks")
    if not isinstance(checks_doc, list):
        raise FusionError("结果 checks 必须是数组，每项 {name, result}")
    norm_checks: list[dict] = []
    for c in checks_doc:
        if not isinstance(c, dict):
            raise FusionError("结果 checks 每项必须是 {name, result} 对象")
        n_, res_ = c.get("name"), c.get("result")
        if not isinstance(n_, str) or not n_.strip():
            raise FusionError("结果 checks.name 必须是非空字符串")
        if not isinstance(res_, str) or not res_.strip():
            raise FusionError(f"结果 checks.result 必须是非空字符串（检查：{n_}）")
        norm_checks.append({"name": n_.strip(), "result": res_.strip()})

    issues = doc.get("issues")
    if not isinstance(issues, list) or not all(isinstance(i, str) for i in issues):
        raise FusionError("结果 issues 必须是字符串数组（可为空）")

    return {
        "at": _now(),
        "round": round_no,
        "handoff_token": handoff["token"],
        "summary": summary.strip(),
        "changed_files": sorted(set(changed)),
        "requirements": norm_reqs,
        "checks": norm_checks,
        "issues": list(issues),
        "file_sha256": digest,
        # 存在实际越界改动时导入已被直接拒绝，成功导入的记录该字段恒为空
        "out_of_scope_detected": [],
        "trusted": False,
    }


def cmd_result(task_id: str) -> dict:
    _require_task_dir(task_id)
    with _task_lock(task_id, "result"):
        task = _load_task(task_id)
        _require_no_active_journal(task, "result")
        if task["status"] == STATUS_APPLIED:
            raise FusionError("任务已应用，不再导入执行者结果")
        doc, digest = _read_result_document(task_id)
        record = _validate_result_document(task, doc, digest)
        task["executor_result"] = record
        _add_event(task, "result_imported", round=record["round"],
                   changed_files=len(record["changed_files"]))
        _save_task(task)
        return {
            "task_id": task_id,
            "imported": True,
            "round": record["round"],
            "summary": record["summary"],
            "changed_files": record["changed_files"],
            "requirements": record["requirements"],
            "checks": record["checks"],
            "issues": record["issues"],
            "out_of_scope_detected": record["out_of_scope_detected"],
            "trusted": False,
            "note": "结果仅作为不可信的执行者证据保存：不修改 review/status，不代替 verify；"
                    "Codex 必须独立 verify + review 后才能 apply",
        }


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------

def _clip_text(text: str) -> tuple[str, bool]:
    if len(text) > MAX_CHECK_OUTPUT_CHARS:
        return text[:MAX_CHECK_OUTPUT_CHARS] + "\n...[输出已截断]", True
    return text, False


def _run_check(check: dict, cwd: Path) -> dict:
    argv = list(check["argv"])
    timeout = float(check.get("timeout", DEFAULT_CHECK_TIMEOUT))
    started = time.monotonic()
    timed_out = False
    launch_error = None
    return_code = None
    out_bytes = b""
    err_bytes = b""
    try:
        proc = subprocess.run(
            argv, shell=False, cwd=str(cwd), input=b"",
            capture_output=True, timeout=timeout,
        )
        return_code = proc.returncode
        out_bytes = proc.stdout or b""
        err_bytes = proc.stderr or b""
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        out_bytes = exc.stdout if isinstance(exc.stdout, bytes) else b""
        err_bytes = exc.stderr if isinstance(exc.stderr, bytes) else b""
    except (FileNotFoundError, PermissionError, OSError) as exc:
        launch_error = f"无法启动：{exc}"
    duration = round(time.monotonic() - started, 3)
    stdout, trunc_out = _clip_text(out_bytes.decode("utf-8", "replace"))
    stderr, trunc_err = _clip_text(err_bytes.decode("utf-8", "replace"))
    result = {
        "name": check["name"],
        "argv": argv,
        "timeout": timeout,
        "exit_code": return_code,
        "timed_out": timed_out,
        "duration_s": duration,
        "stdout": stdout,
        "stderr": stderr,
        "truncated": trunc_out or trunc_err,
    }
    if launch_error:
        result["error"] = launch_error
    return result


def _is_test_path(rel: str) -> bool:
    parts = rel.split("/")
    name = parts[-1]
    if any(p in ("tests", "test") for p in parts[:-1]):
        return True
    return name == "conftest.py" or name.startswith("test_") or name.endswith("_test.py")


def _classify_changes(initial: dict, post: dict, allowed: set) -> tuple[dict, list]:
    changes = {
        "allowed": {"modified": [], "added": [], "deleted": []},
        "out_of_scope": {"modified": [], "added": [], "deleted": []},
    }
    for rel in sorted(set(initial) | set(post)):
        ini = initial.get(rel)
        poi = post.get(rel)
        if ini is not None and poi is not None:
            if ini == poi:
                continue
            kind = "modified"
        elif poi is not None:
            kind = "added"
        else:
            kind = "deleted"
        scope = "allowed" if rel in allowed else "out_of_scope"
        changes[scope][kind].append(rel)
    test_changed = sorted({
        rel
        for scope in ("allowed", "out_of_scope")
        for kind in ("modified", "deleted")
        for rel in changes[scope][kind]
        if rel in initial and _is_test_path(rel)
    })
    return changes, test_changed


def _is_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]


def _build_diffs(orig_dir: Path, work: Path, initial: dict, post: dict) -> dict:
    diffs: dict[str, dict] = {}
    for rel in sorted(set(initial) | set(post)):
        ini = initial.get(rel)
        poi = post.get(rel)
        if poi is None or (ini is not None and ini == poi):
            continue  # 只对新增/修改的文件出 diff，删除单独报告
        work_file = work.joinpath(*rel.split("/"))
        if isinstance(poi, str) and poi.startswith("symlink:"):
            diffs[rel] = {"binary": True, "note": f"副本中为软链接：{poi}"}
            continue
        if poi == "special":
            # FIFO/设备等特殊文件：不打开读取（读取会阻塞），仅报告存在
            diffs[rel] = {"binary": True, "note": "特殊文件（FIFO/设备等），不读取内容"}
            continue
        orig_file = orig_dir.joinpath(*rel.split("/"))
        a_bytes = orig_file.read_bytes() if (ini is not None and orig_file.is_file()) else b""
        b_bytes = work_file.read_bytes()
        if _is_binary(a_bytes) or _is_binary(b_bytes):
            diffs[rel] = {
                "binary": True,
                "sha256_before": hashlib.sha256(a_bytes).hexdigest(),
                "sha256_after": hashlib.sha256(b_bytes).hexdigest(),
            }
            continue
        try:
            a_text = a_bytes.decode("utf-8")
            b_text = b_bytes.decode("utf-8")
        except UnicodeDecodeError:
            diffs[rel] = {
                "binary": True,
                "sha256_before": hashlib.sha256(a_bytes).hexdigest(),
                "sha256_after": hashlib.sha256(b_bytes).hexdigest(),
                "note": "非 UTF-8 文本，仅报告哈希",
            }
            continue
        diff_lines = list(difflib.unified_diff(
            a_text.splitlines(keepends=True), b_text.splitlines(keepends=True),
            fromfile=f"a/{rel}", tofile=f"b/{rel}",
        ))
        if not diff_lines:
            continue
        truncated = False
        if len(diff_lines) > MAX_DIFF_LINES:
            diff_lines = diff_lines[:MAX_DIFF_LINES]
            truncated = True
        text = "".join(diff_lines)
        if len(text) > MAX_DIFF_CHARS:
            text = text[:MAX_DIFF_CHARS]
            truncated = True
        if truncated:
            text += "\n...[diff 已截断]\n"
        diffs[rel] = {"binary": False, "diff": text}
    return diffs


def _manifest_delta(before: dict, after: dict) -> dict:
    return {
        "added": sorted(set(after) - set(before)),
        "removed": sorted(set(before) - set(after)),
        "changed": sorted(rel for rel in set(before) & set(after) if before[rel] != after[rel]),
    }


def cmd_verify(task_id: str) -> dict:
    _require_task_dir(task_id)
    with _task_lock(task_id, "verify"):
        task = _load_task(task_id)
        _require_no_active_journal(task, "verify")
        if task["status"] in (STATUS_APPROVED, STATUS_APPLIED):
            raise FusionError(f"任务已 {task['status']}，不能再次验证；如需返工请先 retry")
        tdir = _task_dir(task_id)
        work = Path(task["work_path"])
        orig_dir = tdir / "orig"
        if not work.is_dir():
            raise FusionError("任务副本目录缺失，任务数据可能已损坏")

        pre_manifest = scan_manifest(work)
        check_results = [_run_check(c, work) for c in task["spec"].get("checks", [])]
        # 检查结束后再次计算副本清单，测试运行中出现的变化也计入范围
        post_manifest = scan_manifest(work)

        initial = task["initial_manifest"]
        allowed = set(task["spec"]["allowed_files"])
        changes, test_changed = _classify_changes(initial, post_manifest, allowed)
        diffs = _build_diffs(orig_dir, work, initial, post_manifest)

        failures: list[str] = []
        oos = changes["out_of_scope"]
        oos_all = sorted(oos["modified"] + oos["added"] + oos["deleted"])
        if oos_all:
            failures.append("越界改动（不在 allowed_files 内）：" + ", ".join(oos_all))
        anomalous = sorted(
            rel for rel, value in post_manifest.items()
            if value == "special" or (isinstance(value, str) and value.startswith("symlink:"))
        )
        if anomalous:
            failures.append("副本中存在软链接或特殊文件（FIFO/设备等），不予验收并要求清理：" + ", ".join(anomalous))
        for c in check_results:
            if c["timed_out"]:
                failures.append(f"验收命令超时：{c['name']}（{c['timeout']} 秒）")
            elif c.get("error"):
                failures.append(f"验收命令无法启动：{c['name']}（{c['error']}）")
            elif c["exit_code"] != 0:
                failures.append(f"验收命令失败：{c['name']}（退出码 {c['exit_code']}）")

        number = task.get("verification_counter", 0) + 1
        record = {
            "number": number,
            "at": _now(),
            "passed": not failures,
            "failures": failures,
            "changes": changes,
            "test_changes": test_changed,
            "diffs": diffs,
            "checks": check_results,
            "pre_digest": manifest_digest(pre_manifest),
            "post_digest": manifest_digest(post_manifest),
            "post_manifest": post_manifest,
            "changes_during_checks": _manifest_delta(pre_manifest, post_manifest),
        }
        task["verification_counter"] = number
        task["verifications"].append(record)
        task["latest_verification"] = {
            "number": number,
            "at": record["at"],
            "passed": record["passed"],
            "failures": failures,
            "test_changes": test_changed,
            "digest": record["post_digest"],
        }
        _add_event(task, "verified", number=number, passed=record["passed"])
        _save_task(task)
        return record


# ---------------------------------------------------------------------------
# review
# ---------------------------------------------------------------------------

def cmd_review(task_id: str, decisions, test_changes_acknowledged: bool = False) -> dict:
    if not isinstance(test_changes_acknowledged, bool):
        raise FusionError("test_changes_acknowledged 必须是布尔值（不接受字符串 'true'/'false' 等其他类型）")
    _require_task_dir(task_id)
    with _task_lock(task_id, "review"):
        task = _load_task(task_id)
        _require_no_active_journal(task, "review")
        if task["status"] not in (STATUS_PREPARED, STATUS_NEEDS_REVISION):
            raise FusionError(f"任务状态为 {task['status']}，不能审查")
        reqs = list(task["spec"]["requirements"])

        if not isinstance(decisions, list):
            raise FusionError("decisions 必须是数组，每项含 requirement/passed/evidence")
        norm = []
        for d in decisions:
            if not isinstance(d, dict):
                raise FusionError("每条 decision 必须是对象")
            r, p, e = d.get("requirement"), d.get("passed"), d.get("evidence")
            if not isinstance(r, str):
                raise FusionError("decision.requirement 必须是字符串")
            if not isinstance(p, bool):
                raise FusionError("decision.passed 必须是布尔值")
            if not isinstance(e, str) or not e.strip():
                raise FusionError(f"decision.evidence 必须是非空字符串（要求：{r}）")
            norm.append({"requirement": r, "passed": p, "evidence": e.strip()})

        counts: dict[str, int] = {}
        for d in norm:
            counts[d["requirement"]] = counts.get(d["requirement"], 0) + 1
        missing = [r for r in reqs if counts.get(r, 0) == 0]
        unknown = [r for r in counts if r not in reqs]
        duplicated = [r for r, c in counts.items() if c > 1]
        if missing or unknown or duplicated:
            raise FusionError(
                f"decisions 未恰好覆盖所有硬性要求（缺失：{missing}；多余：{unknown}；重复：{duplicated}）"
            )

        v = task.get("latest_verification")
        if not v:
            raise FusionError("尚无验证记录；review 前必须先 verify（返工后验证记录已清空）")
        if not v["passed"]:
            raise FusionError(f"最近的验证（#{v['number']}）未通过：{'；'.join(v['failures'])}")
        work = Path(task["work_path"])
        current_digest = manifest_digest(scan_manifest(work))
        if current_digest != v["digest"]:
            raise FusionError("副本内容与最近验证时不一致（验证后副本被改动），请重新 verify")
        if v["test_changes"] and not test_changes_acknowledged:
            raise FusionError(
                "既有测试文件被修改：" + ", ".join(v["test_changes"])
                + "；需显式确认（--ack-test-changes / test_changes_acknowledged=true）"
            )

        failed = [d["requirement"] for d in norm if not d["passed"]]
        result = STATUS_APPROVED if not failed else STATUS_NEEDS_REVISION
        review = {
            "at": _now(),
            "verification_number": v["number"],
            "digest": current_digest,
            "result": result,
            "failed_requirements": failed,
            "decisions": norm,
            "test_changes_acknowledged": bool(v["test_changes"]) and bool(test_changes_acknowledged),
        }
        task["review"] = review
        task["status"] = result
        _add_event(task, "reviewed", result=result,
                   verification_number=v["number"], failed=len(failed))
        _save_task(task)
        return {
            "task_id": task_id,
            "status": result,
            "verification_number": v["number"],
            "failed_requirements": failed,
            "decisions": norm,
            "test_changes": v["test_changes"],
        }


# ---------------------------------------------------------------------------
# retry
# ---------------------------------------------------------------------------

def cmd_retry(task_id: str, feedback) -> dict:
    if not isinstance(feedback, str) or not feedback.strip():
        raise FusionError("feedback 必须是非空文本")
    _require_task_dir(task_id)
    with _task_lock(task_id, "retry"):
        task = _load_task(task_id)
        _require_no_active_journal(task, "retry")
        if task["status"] == STATUS_APPLIED:
            raise FusionError("任务已应用，无法返工；请 prepare 新任务")
        if task["rework_rounds"] >= 2:
            raise FusionError("已达到 2 轮返工上限；后续由 Lead 决定：放弃本任务，或重新 prepare 新任务")

        text = feedback.strip()
        task["rework_rounds"] += 1
        task["status"] = STATUS_NEEDS_REVISION
        task["review"] = None
        task["latest_verification"] = None
        task["verifications"] = []
        # 清除本轮结果与派工 token（下一轮 handoff 换新 token）；
        # 保留会话标签与派工历史；apply_journal 永不因 retry 抹掉
        task["executor_result"] = None
        task["handoff"] = None
        task["retry_feedbacks"].append({"round": task["rework_rounds"], "feedback": text, "at": _now()})
        _add_event(task, "retry", round=task["rework_rounds"])
        _write_brief(task, feedback=text)
        _save_task(task)
        return {
            "task_id": task_id,
            "status": task["status"],
            "rework_rounds": task["rework_rounds"],
            "brief_path": task["brief_path"],
            "work_path": task["work_path"],
            "feedback": text,
            "note": "同一副本保留；下一轮 handoff 将生成新 token；旧轮次留在盘上的 result.json 不会被认可",
        }


# ---------------------------------------------------------------------------
# apply（带逐文件 journal）
# ---------------------------------------------------------------------------

def _apply_journal_summary(journal: dict | None) -> dict | None:
    if not journal:
        return None
    return {
        "status": journal.get("status"),
        "started_at": journal.get("started_at"),
        "completed_at": journal.get("completed_at"),
        "recovering": journal.get("recovering", False),
        "plan": [
            {"file": i.get("file"), "action": i.get("action"),
             "backup_path": i.get("backup_path"),
             "before_sha256": i.get("before_sha256"),
             "after_sha256": i.get("after_sha256")}
            for i in (journal.get("plan") or [])
        ],
        "files_done": list(journal.get("files_done") or []),
        "files_pending": list(journal.get("files_pending") or []),
        "backup_dir": journal.get("backup_dir"),
        "error": journal.get("error"),
        "note": "files_done=已确认写入；files_pending=进度未保存，崩溃窗口内可能已写入，"
                "需核对实际源文件（不承诺事务或掉电一致性）",
    }


def cmd_apply(task_id: str, confirm_recovery: bool = False) -> dict:
    if not isinstance(confirm_recovery, bool):
        raise FusionError("confirm_recovery 必须是布尔值（不接受其他类型）")
    _require_task_dir(task_id)
    with _task_lock(task_id, "apply"):
        task = _load_task(task_id)
        if task["status"] != STATUS_APPROVED:
            raise FusionError(f"任务状态为 {task['status']}，只有 approved 的任务才能应用")
        work = Path(task["work_path"])
        ws = Path(task["workspace"])
        if ws.is_symlink():
            raise FusionError(f"工作区根路径已被替换为软链接，拒绝应用：{ws}")
        current = scan_manifest(work)
        if manifest_digest(current) != task["review"]["digest"]:
            raise FusionError("副本内容与审查时不一致（审查后副本被改动），拒绝应用；请重新 verify + review")

        # 上次 apply 未完成：禁止盲目重试；必须人工核查后显式确认才能继续
        journal = task.get("apply_journal")
        recovering = False
        if journal and journal.get("status") in JOURNAL_ACTIVE_STATES:
            if journal["status"] == "in_progress":
                # 持有锁仍见到 in_progress → 上一次持锁进程已中止
                journal["status"] = "interrupted"
                _add_event(task, "apply_interrupted_detected")
                _save_task(task)
            if not confirm_recovery:
                raise FusionError(
                    f"上次 apply 未完成（journal 状态 {journal['status']}），禁止盲目重试。"
                    f"已确认写入：{journal.get('files_done') or '无'}；"
                    f"进度未保存（可能已写入，需核对实际源文件）：{journal.get('files_pending') or '无'}；"
                    f"备份目录（保留原备份）：{journal.get('backup_dir') or '无'}。"
                    "请先用 resume 人工核查原工作区实际状态；确认后通过 "
                    "--confirm-recovery / confirm_recovery=true 再次调用。"
                    "本工具不提供多文件事务、自动回滚或掉电一致性保证。")
            recovering = True

        backup_dir = _task_dir(task_id) / "backup"
        plan: list[dict] = []
        for rel in task["spec"]["allowed_files"]:
            parts = rel.split("/")
            wpath = work.joinpath(*parts)
            orig_hash = task["original_hashes"].get(rel)
            if os.path.lexists(wpath):
                if wpath.is_symlink() or not wpath.is_file():
                    raise FusionError(f"副本文件不是常规文件（可能是软链接）：{rel}")
                whash = sha256_file(wpath)
                if orig_hash is None:
                    action = "create"
                elif whash != orig_hash:
                    action = "modify"
                else:
                    continue  # 未变化
            else:
                if orig_hash is not None:
                    raise FusionError(
                        f"副本中的文件被删除：{rel}；当前版本暂不支持应用删除，"
                        "请在副本中恢复该文件，或由 Lead 重新准备任务"
                    )
                continue  # 声明过的新文件最终也没有创建
            # journal.plan 在写任何文件前持久化：每项含备份位置与预期 before/after 摘要
            plan.append({
                "file": rel,
                "action": action,
                "backup_path": str(backup_dir.joinpath(*parts)) if action == "modify" else None,
                "before_sha256": orig_hash if action == "modify" else None,
                "after_sha256": whash,
            })

        if not plan and not recovering:
            task["status"] = STATUS_APPLIED
            task["applied"] = {"at": _now(), "changes": [], "backup_dir": None,
                               "note": "没有需要应用的文件变化"}
            _add_event(task, "apply_completed", files=[])
            _save_task(task)
            return {"task_id": task_id, "status": STATUS_APPLIED, "changes": [], "backup_dir": None}

        files_done: list[str] = []       # 恢复场景：上次已成功写入的文件（幂等跳过）
        pending: list[dict] = []

        # ---- 完整预检：所有目标路径检查通过后才开始写入 ----
        for item in plan:
            parts = item["file"].split("/")
            target = ws.joinpath(*parts)
            _assert_no_symlink_chain(ws, parts)
            if item["action"] == "modify":
                if target.is_symlink() or not target.is_file():
                    raise FusionError(f"目标文件已不存在或被替换（含软链接）：{item['file']}")
                thash = sha256_file(target)
                if thash == task["original_hashes"][item["file"]]:
                    pending.append(item)
                elif recovering and thash == sha256_file(work.joinpath(*parts)):
                    files_done.append(item["file"])  # 上次 apply 已写入，幂等跳过
                else:
                    raise FusionError(
                        f"原文件在 prepare 之后被外部修改，拒绝覆盖：{item['file']}"
                        "（当前哈希与 prepare 记录不一致；如需继续请重新准备任务）"
                    )
            else:  # create
                if os.path.lexists(target):
                    if (recovering and not target.is_symlink() and target.is_file()
                            and sha256_file(target) == sha256_file(work.joinpath(*parts))):
                        files_done.append(item["file"])
                    else:
                        raise FusionError(f"新增文件的目标路径已存在：{item['file']}")
                else:
                    cur = ws
                    for part in parts[:-1]:
                        cur = cur / part
                        if os.path.lexists(cur) and not cur.is_dir():
                            raise FusionError(f"目标父路径不是目录：{cur}")
                    pending.append(item)

        if not pending and not files_done:
            task["status"] = STATUS_APPLIED
            task["applied"] = {"at": _now(), "changes": [], "backup_dir": None,
                               "note": "没有需要应用的文件变化"}
            _add_event(task, "apply_completed", files=[])
            _save_task(task)
            return {"task_id": task_id, "status": STATUS_APPLIED, "changes": [], "backup_dir": None}

        # ---- 先持久化 in-progress journal，再写任何文件 ----
        journal = {
            "status": "in_progress",
            "started_at": _now(),
            "completed_at": None,
            "recovering": recovering,
            "plan": plan,
            "files_done": list(files_done),
            "files_pending": [i["file"] for i in pending],
            "backup_dir": None,
            "error": None,
        }
        task["apply_journal"] = journal
        _add_event(task, "apply_started", recovering=recovering,
                   files=[i["file"] for i in plan])
        _save_task(task)

        applied = [
            {"file": rel,
             "action": next(i["action"] for i in plan if i["file"] == rel),
             "backup": str(backup_dir.joinpath(*rel.split("/")))}
            for rel in files_done
        ]

        # ---- 备份与逐文件原子替换（不承诺多文件事务），逐文件保存进度 ----
        try:
            for item in pending:
                parts = item["file"].split("/")
                target = ws.joinpath(*parts)
                wpath = work.joinpath(*parts)
                backup_path = None
                if item["action"] == "modify":
                    backup_path = backup_dir.joinpath(*parts)
                    backup_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(target, backup_path)
                    journal["backup_dir"] = str(backup_dir)
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.parent / f".fusion-tmp-{uuid.uuid4().hex[:12]}"
                data = wpath.read_bytes()
                with open(tmp, "wb") as f:
                    f.write(data)
                if item["action"] == "modify":
                    os.chmod(tmp, os.stat(target).st_mode & 0o7777)
                os.replace(tmp, target)
                journal["files_done"].append(item["file"])
                journal["files_pending"].remove(item["file"])
                _save_task(task)  # 每写完一个文件即保存进度
                applied.append({"file": item["file"], "action": item["action"],
                                "backup": str(backup_path) if backup_path else None})
        except OSError as exc:
            journal["status"] = "failed"
            journal["error"] = str(exc)
            _add_event(task, "apply_failed", error=str(exc),
                       files_done=list(journal["files_done"]),
                       files_pending=list(journal["files_pending"]))
            _save_task(task)
            raise FusionError(
                f"应用过程中发生 IO 错误（无多文件事务保证）：{exc}；"
                f"已确认写入：{journal['files_done'] or '无'}；"
                f"进度未保存（可能已写入，需核对实际源文件）：{journal['files_pending'] or '无'}；"
                f"备份目录（保留原备份）：{journal.get('backup_dir') or '无'}。"
                "请先用 resume 人工核查，再以 --confirm-recovery 继续；不要盲目重试"
            ) from exc

        journal["status"] = "completed"
        journal["completed_at"] = _now()
        task["status"] = STATUS_APPLIED
        task["applied"] = {
            "at": _now(),
            "changes": applied,
            "backup_dir": str(backup_dir) if any(a["backup"] for a in applied) else None,
        }
        _add_event(task, "apply_completed", files=[a["file"] for a in applied])
        _save_task(task)
        return {
            "task_id": task_id,
            "status": STATUS_APPLIED,
            "changes": applied,
            "backup_dir": task["applied"]["backup_dir"],
            "journal": _apply_journal_summary(journal),
        }


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------

def cmd_get(task_id: str) -> dict:
    task = _load_task(task_id)
    spec = task["spec"]
    v = task.get("latest_verification")
    r = task.get("review")
    a = task.get("applied")
    handoff = task.get("handoff")
    dispatches = task.get("dispatches") or []
    er = task.get("executor_result")
    return {
        "task_id": task_id,
        "status": task["status"],
        "created_at": task["created_at"],
        "rework_rounds": task["rework_rounds"],
        "workspace": task["workspace"],
        "brief_path": task.get("brief_path"),
        "work_path": task["work_path"],
        "request": spec["request"],
        "requirements": spec["requirements"],
        "allowed_files": spec["allowed_files"],
        "context_files": spec["context_files"],
        "checks": spec["checks"],
        "verification": (
            {"number": v["number"], "passed": v["passed"], "failures": v["failures"],
             "test_changes": v["test_changes"], "digest": v["digest"]}
            if v else None
        ),
        "review": (
            {"result": r["result"], "failed_requirements": r["failed_requirements"],
             "verification_number": r["verification_number"],
             "test_changes_acknowledged": r["test_changes_acknowledged"]}
            if r else None
        ),
        "applied": a,
        # v0.2.0 流程字段（保留以上全部既有键不变）
        "handoff": (
            {"round": handoff["round"], "token": handoff["token"],
             "handoff_path": handoff.get("handoff_path"),
             "result_template_path": handoff.get("result_template_path"),
             "result_path": handoff.get("result_path") or str(_result_path(task_id)),
             "created_at": handoff.get("created_at")}
            if handoff else None
        ),
        "dispatches": [
            {"at": d.get("at"), "session_label": d.get("session_label"), "round": d.get("round")}
            for d in dispatches
        ],
        "executor_result": (
            {"at": er.get("at"), "round": er.get("round"), "summary": er.get("summary"),
             "changed_files": er.get("changed_files"), "issues": er.get("issues"),
             "checks": er.get("checks"), "trusted": False}
            if er else None
        ),
        "apply_journal": _apply_journal_summary(task.get("apply_journal")),
        "events": list(task.get("events") or [])[-10:],
    }


# ---------------------------------------------------------------------------
# list：跨进程找回任务摘要（不因单个损坏任务失败）
# ---------------------------------------------------------------------------

def cmd_list(workspace=None) -> dict:
    if workspace is not None and (not isinstance(workspace, str) or not workspace.strip()):
        raise FusionError("workspace 过滤参数必须是非空字符串（绝对路径）或省略")
    root = tasks_dir()
    tasks: list[dict] = []
    errors: list[dict] = []
    if not root.is_dir():
        return {"tasks": tasks, "errors": errors, "count": 0}
    try:
        entries = sorted(os.scandir(root), key=lambda e: e.name)
    except OSError as exc:
        return {"tasks": tasks,
                "errors": [{"task_id": None, "error": f"无法扫描任务目录 {root}：{exc}"}],
                "count": 0}
    for entry in entries:
        try:
            if not entry.is_dir(follow_symlinks=False):
                continue
            if not TASK_ID_RE.fullmatch(entry.name):
                continue  # 忽略非任务 ID 目录
        except OSError as exc:
            errors.append({"task_id": entry.name, "error": f"无法访问目录：{exc}"})
            continue
        try:
            task = _load_task(entry.name)
        except FusionError as exc:
            errors.append({"task_id": entry.name, "error": str(exc)})
            continue
        if workspace is not None and task.get("workspace") != workspace:
            continue
        try:
            dispatches = task.get("dispatches") or []
            handoff = task.get("handoff")
            # 有界（≤1MB）检查 result.json，绝不用无界 sha256_file 读取可能很大的结果文件
            rstate = _result_file_state(task)
            tasks.append({
                "task_id": entry.name,
                "workspace": task.get("workspace"),
                "status": task.get("status"),
                "created_at": task.get("created_at"),
                "rework_rounds": task.get("rework_rounds", 0),
                "request": task.get("spec", {}).get("request"),
                "work_path": task.get("work_path"),
                "brief_path": task.get("brief_path"),
                "handoff_round": handoff.get("round") if handoff else None,
                "dispatch_count": len(dispatches),
                "last_session_label": dispatches[-1].get("session_label") if dispatches else None,
                "has_pending_result": rstate["pending_import"],
                "result_error": rstate["result_error"],
                "applied": bool(task.get("applied")),
            })
        except (FusionError, OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            # 单个任务 schema 损坏只计入 errors，不让整个 list 崩溃
            errors.append({"task_id": entry.name, "error": f"任务摘要构建失败：{exc}"})
    return {"tasks": tasks, "errors": errors, "count": len(tasks)}


# ---------------------------------------------------------------------------
# resume：只读恢复视图与下一步建议
# ---------------------------------------------------------------------------

def _resume_next_action(task: dict, current_digest: str | None, work_exists: bool,
                        pending_result: bool) -> tuple[str, str]:
    status = task["status"]
    journal = task.get("apply_journal")
    v = task.get("latest_verification")
    rv = task.get("review")
    er = task.get("executor_result")
    handoff = task.get("handoff")
    dispatches = task.get("dispatches") or []

    # 1. 部分 apply / apply 失败或中断：最高优先级阻止盲目 apply
    if journal and journal.get("status") in JOURNAL_ACTIVE_STATES:
        state = journal.get("status")
        detail = f"错误：{journal.get('error')}。" if journal.get("error") else ""
        return ("manual_check",
                f"上次 apply 未完成（journal 状态 {state}）。{detail}"
                f"已确认写入：{journal.get('files_done') or '无'}；"
                f"进度未保存（可能已写入，需核对实际源文件）：{journal.get('files_pending') or '无'}；"
                f"备份目录（保留原备份）：{journal.get('backup_dir') or '无'}。"
                "请人工核查原工作区实际状态后再决定；确认无误后用 apply 的 "
                "--confirm-recovery / confirm_recovery=true 继续。"
                "在此之前 apply 会拒绝执行；本工具不提供多文件事务、自动回滚或掉电一致性保证。")

    # 2. 已应用：不重复
    if status == STATUS_APPLIED:
        return ("done", "任务已应用，不会重复 apply；如需继续改动请 prepare 新任务")

    if not work_exists:
        return ("manual_check", "任务副本目录缺失，任务数据可能已损坏，请人工核查任务数据目录")

    # 3. approved：应用前检查副本一致性
    if status == STATUS_APPROVED:
        if current_digest != (rv or {}).get("digest"):
            detail = ("副本内容与审查时不一致（审查后副本被改动），apply 会被拒绝；"
                      "verify 不接受 approved 任务（原审批摘要保留在本视图 review 字段）")
            if task.get("rework_rounds", 0) >= 2:
                return ("reassess",
                        detail + "；且已达 2 轮返工上限，retry 会被拒绝：请 Lead 人工重新评估——"
                        "核对副本与原工作区后放弃本任务或 prepare 新任务")
            return ("retry",
                    detail + "；可执行的下一步：先 retry 重开验证与审查（保留同一副本），再 verify + review")
        return ("apply", "已 approved 且副本与审查摘要一致；在用户原始请求授权这些文件改动时可 apply")

    # 4. prepared / needs_revision
    if rv and rv.get("result") == STATUS_NEEDS_REVISION:
        failed = rv.get("failed_requirements") or []
        if task.get("rework_rounds", 0) >= 2:
            return ("reassess",
                    f"审查未通过（未满足：{failed}），且已达到 2 轮返工上限，retry 会被拒绝："
                    "由 Lead 人工重新评估：放弃本任务或重新 prepare 新任务")
        return ("retry",
                f"审查未通过（未满足：{failed}）；请用 retry 记录反馈并交回执行者，"
                "或修正审查判断后重新 review")
    if v:
        if v.get("passed") and current_digest == v.get("digest"):
            return ("review",
                    f"最近验证 #{v['number']} 已通过且副本摘要未变；请逐条审查（review）后决定 approve 或返工")
        if not v.get("passed"):
            if task.get("rework_rounds", 0) >= 2:
                return ("reassess",
                        f"最近验证 #{v['number']} 未通过，且已达到 2 轮返工上限，retry 会被拒绝："
                        "由 Lead 人工重新评估：放弃本任务或重新 prepare 新任务")
            return ("retry",
                    f"最近验证 #{v['number']} 未通过：{'；'.join(v.get('failures') or [] or ['（无明细）'])}；"
                    "请用 retry 记录反馈并交回执行者")
        return ("verify", "副本内容与最近验证时不一致（验证后副本被改动），请重新 verify")

    if pending_result:
        return ("import_result",
                "检测到尚未导入（或导入后又变化）的 result.json；请运行 result 导入并校验执行者证据")
    if er:
        return ("verify", "已导入执行者结构化结果（不可信证据）；请运行 verify 独立验收")

    if handoff:
        dispatched = any(d.get("round") == handoff.get("round") for d in dispatches)
        if dispatched:
            labels = [d.get("session_label") for d in dispatches
                      if d.get("round") == handoff.get("round")]
            return ("wait_zcode",
                    f"本轮（round={handoff.get('round')}）已派工到会话：{labels}；"
                    "请等待或人工检查 ZCode 会话进展；resume/list 只读，不会自动重新派工")
        return ("dispatch",
                f"handoff 派工包已生成（round={handoff.get('round')}）；"
                "请在实际通过 UI 发送给 ZCode 之后调用 dispatch 记录会话标签")
    return ("handoff",
            "尚未生成派工包；请运行 handoff 生成 prompt 与 result 模板"
            "（旧 v0.1.0 任务也可以不经过 handoff/result，直接走 verify/review/apply）")


def cmd_resume(task_id: str) -> dict:
    task = _load_task(task_id)  # 只读操作：不加锁、不改状态、不重新派工
    spec = task["spec"]
    work = Path(task["work_path"])
    work_exists = work.is_dir()
    current_digest = manifest_digest(scan_manifest(work)) if work_exists else None
    handoff = task.get("handoff")
    dispatches = task.get("dispatches") or []
    er = task.get("executor_result")
    v = task.get("latest_verification")
    rv = task.get("review")

    rstate = _result_file_state(task)  # 有界（≤1MB）检查，不做无界读取
    pending_result = rstate["pending_import"]

    session_labels = [d.get("session_label") for d in dispatches if d.get("session_label")]
    next_action, reason = _resume_next_action(task, current_digest, work_exists, pending_result)
    if rstate["result_error"] and next_action not in ("import_result", "manual_check"):
        reason += f"（注意：{rstate['result_error']}）"

    return {
        "task_id": task_id,
        "status": task["status"],
        "rework_rounds": task.get("rework_rounds", 0),
        "created_at": task.get("created_at"),
        "workspace": task["workspace"],
        "work_path": task["work_path"],
        "brief_path": task.get("brief_path"),
        "task_dir": str(_task_dir(task_id)),
        "handoff_path": handoff.get("handoff_path") if handoff else None,
        "result_template_path": handoff.get("result_template_path") if handoff else None,
        "result_path": str(_result_path(task_id)),
        "handoff_round": handoff.get("round") if handoff else None,
        "dispatched_this_round": bool(
            handoff and any(d.get("round") == handoff.get("round") for d in dispatches)),
        "session_labels": session_labels,
        "request": spec["request"],
        "requirements": spec["requirements"],
        "allowed_files": spec["allowed_files"],
        "executor_result": (
            {"at": er.get("at"), "round": er.get("round"), "summary": er.get("summary"),
             "changed_files": er.get("changed_files"), "issues": er.get("issues"),
             "checks": er.get("checks"), "trusted": False}
            if er else None
        ),
        "verification": (
            {"number": v["number"], "passed": v["passed"], "failures": v["failures"],
             "test_changes": v["test_changes"]}
            if v else None
        ),
        "review": (
            {"result": rv["result"], "failed_requirements": rv["failed_requirements"],
             "verification_number": rv["verification_number"]}
            if rv else None
        ),
        "apply_journal": _apply_journal_summary(task.get("apply_journal")),
        "applied": task.get("applied"),
        "work_digest": current_digest,
        "work_digest_matches_verification": bool(v and current_digest == v.get("digest")),
        "work_digest_matches_review": bool(rv and current_digest == rv.get("digest")),
        "has_pending_result_file": pending_result,
        "result_error": rstate["result_error"],
        "next_action": next_action,
        "reason": reason,
    }


# ---------------------------------------------------------------------------
# MCP stdio 服务（换行分隔 JSON-RPC 2.0，stdout 仅输出协议消息）
# ---------------------------------------------------------------------------

def _mcp_tools() -> list[dict]:
    requirement_obj = {
        "type": "object",
        "required": ["requirement", "passed", "evidence"],
        "properties": {
            "requirement": {"type": "string", "description": "对应的硬性要求原文"},
            "passed": {"type": "boolean"},
            "evidence": {"type": "string", "description": "非空证据说明"},
        },
    }
    spec_obj = {
        "type": "object",
        "required": ["workspace", "request", "requirements", "allowed_files"],
        "properties": {
            "workspace": {"type": "string", "description": "绝对路径的原始工作区目录"},
            "request": {"type": "string", "description": "用户原话"},
            "instructions": {"type": "string", "description": "给执行者的指示"},
            "requirements": {"type": "array", "items": {"type": "string"},
                             "minItems": 1, "description": "非空且唯一的硬性要求"},
            "allowed_files": {"type": "array", "items": {"type": "string"},
                              "minItems": 1, "description": "非空相对路径白名单（新文件也须列入）"},
            "context_files": {"type": "array", "items": {"type": "string"},
                              "description": "只读参考文件相对路径"},
            "checks": {"type": "array", "items": {
                "type": "object",
                "required": ["name", "argv"],
                "properties": {
                    "name": {"type": "string"},
                    "argv": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                    "timeout": {"type": "number", "description": "秒，默认 120"},
                },
            }},
        },
    }
    task_id_obj = {"type": "string", "description": "prepare 返回的任务 ID"}
    result_requirement_obj = {
        "type": "object",
        "required": ["requirement", "evidence"],
        "properties": {
            "requirement": {"type": "string", "description": "硬性要求原文，须逐字一致"},
            "evidence": {"type": "string", "description": "非空证据说明（执行者声称）"},
        },
    }
    return [
        {
            "name": "fusion_prepare",
            "description": "创建 Fusion 任务：校验 spec，复制 allowed/context 文件到独立任务副本，生成 brief.md。原文件保持不变。",
            "inputSchema": {"type": "object", "required": ["spec"],
                            "properties": {"spec": spec_obj}},
            "annotations": {"readOnlyHint": False, "destructiveHint": False},
        },
        {
            "name": "fusion_get",
            "description": "查询任务状态、brief/work 路径、验收与审查摘要，以及 handoff/派工/结果/journal 等流程字段。",
            "inputSchema": {"type": "object", "required": ["task_id"],
                            "properties": {"task_id": task_id_obj}},
            "annotations": {"readOnlyHint": True},
        },
        {
            "name": "fusion_verify",
            "description": "对任务副本做清单比较（改动/越界/既有测试文件）与 unified diff，并运行 Lead 验收命令；越界、超时、非零退出即验证失败。",
            "inputSchema": {"type": "object", "required": ["task_id"],
                            "properties": {"task_id": task_id_obj}},
            "annotations": {"readOnlyHint": False, "destructiveHint": False},
        },
        {
            "name": "fusion_review",
            "description": "Lead 逐条要求审查：decisions 必须恰好覆盖所有硬性要求且证据非空；全部通过才 approved。",
            "inputSchema": {"type": "object", "required": ["task_id", "decisions"],
                            "properties": {
                                "task_id": task_id_obj,
                                "decisions": {"type": "array", "items": requirement_obj},
                                "test_changes_acknowledged": {"type": "boolean",
                                    "description": "既有测试文件被改写时的显式确认，默认 false"},
                            }},
            "annotations": {"readOnlyHint": False, "destructiveHint": False},
        },
        {
            "name": "fusion_retry",
            "description": "记录 Lead 返工反馈，保留同一任务副本，重新生成 brief 并清空验证/审查/本轮结果与派工 token（下一轮 handoff 换新 token）。最多 2 轮。",
            "inputSchema": {"type": "object", "required": ["task_id", "feedback"],
                            "properties": {"task_id": task_id_obj,
                                           "feedback": {"type": "string"}}},
            "annotations": {"readOnlyHint": False, "destructiveHint": False},
        },
        {
            "name": "fusion_apply",
            "description": "仅 approved 任务可调用：先持久化逐文件 journal，再把 allowed_files 内的变化原子应用回原工作区，保留 before 备份；原文件被外部改动则拒绝。上次 apply 失败/中断后需人工核查并传 confirm_recovery=true 才能继续。",
            "inputSchema": {"type": "object", "required": ["task_id"],
                            "properties": {
                                "task_id": task_id_obj,
                                "confirm_recovery": {"type": "boolean",
                                    "description": "人工核查部分应用后显式确认继续，默认 false"},
                            }},
            "annotations": {"readOnlyHint": False, "destructiveHint": True},
        },
        {
            "name": "fusion_handoff",
            "description": "生成本轮派工包（taskdir/handoff.md + result-template.json，随机 token，同一轮重复调用幂等），返回可直接粘贴给 ZCode 的 prompt、handoff/result 路径、token 与 round。实际 UI 发送由 Lead 完成。",
            "inputSchema": {"type": "object", "required": ["task_id"],
                            "properties": {"task_id": task_id_obj}},
            "annotations": {"readOnlyHint": False, "destructiveHint": False},
        },
        {
            "name": "fusion_dispatch",
            "description": "Lead 已通过 UI（Computer Use 或人工）实际把派工 prompt 发送给 ZCode 会话后，记录该会话标签。仅做记录，不发送任何 UI 事件，也不核实发送是否发生。",
            "inputSchema": {"type": "object", "required": ["task_id", "session_label"],
                            "properties": {
                                "task_id": task_id_obj,
                                "session_label": {"type": "string",
                                    "description": "ZCode 会话标签（非空）"},
                            }},
            "annotations": {"readOnlyHint": False, "destructiveHint": False},
        },
        {
            "name": "fusion_result",
            "description": "读取固定路径 taskdir/result.json 并严格校验（task_id/round 真整数/handoff_token/summary/changed_files 与实际变化及白名单一致/requirements 恰好覆盖/checks 声明/issues），保存为不可信执行者证据；拒绝软链接、FIFO、超 1MB、无效 JSON 与旧轮次结果；不修改 review/status，不替代 verify。",
            "inputSchema": {"type": "object", "required": ["task_id"],
                            "properties": {"task_id": task_id_obj}},
            "annotations": {"readOnlyHint": False, "destructiveHint": False},
        },
        {
            "name": "fusion_list",
            "description": "列出 FUSION_TASKS_DIR 下所有任务摘要与读取错误（忽略非 ID 目录，单个损坏任务不影响其余）；可用 workspace 过滤。",
            "inputSchema": {"type": "object",
                            "properties": {"workspace": {"type": "string",
                                "description": "可选：仅返回该绝对路径 workspace 的任务"}}},
            "annotations": {"readOnlyHint": True},
        },
        {
            "name": "fusion_resume",
            "description": "只读恢复视图：路径、原需求、会话标签、最新结果/验证/审查摘要、副本一致性与 apply journal 状态，并给出 next_action/reason；不自动重新派工或重复 apply。",
            "inputSchema": {"type": "object", "required": ["task_id"],
                            "properties": {"task_id": task_id_obj}},
            "annotations": {"readOnlyHint": True},
        },
    ]


def _require_str(args: dict, key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise FusionError(f"缺少有效参数：{key}")
    return value


def _mcp_tool_call(params: dict) -> dict:
    def error_result(text: str) -> dict:
        return {"content": [{"type": "text", "text": text}], "isError": True}

    name = params.get("name")
    if not isinstance(name, str) or not name:
        return error_result("tools/call 缺少 name")
    args = params.get("arguments") or {}
    if not isinstance(args, dict):
        return error_result("arguments 必须是对象")
    try:
        if name == "fusion_prepare":
            payload = cmd_prepare(args.get("spec"))
        elif name == "fusion_get":
            payload = cmd_get(_require_str(args, "task_id"))
        elif name == "fusion_verify":
            payload = cmd_verify(_require_str(args, "task_id"))
        elif name == "fusion_review":
            payload = cmd_review(_require_str(args, "task_id"), args.get("decisions"),
                                 args.get("test_changes_acknowledged", False))
        elif name == "fusion_retry":
            payload = cmd_retry(_require_str(args, "task_id"), args.get("feedback"))
        elif name == "fusion_apply":
            payload = cmd_apply(_require_str(args, "task_id"),
                                args.get("confirm_recovery", False))
        elif name == "fusion_handoff":
            payload = cmd_handoff(_require_str(args, "task_id"))
        elif name == "fusion_dispatch":
            payload = cmd_dispatch(_require_str(args, "task_id"), args.get("session_label"))
        elif name == "fusion_result":
            payload = cmd_result(_require_str(args, "task_id"))
        elif name == "fusion_list":
            payload = cmd_list(args.get("workspace"))
        elif name == "fusion_resume":
            payload = cmd_resume(_require_str(args, "task_id"))
        else:
            return error_result(f"未知工具：{name}")
    except FusionError as exc:
        return error_result(f"Fusion 错误：{exc}")
    except Exception as exc:  # 防御：未预期异常转为 isError 结果，绝不让 MCP 进程退出
        return error_result(f"内部错误：{type(exc).__name__}")
    return {"content": [{"type": "text",
                         "text": json.dumps(payload, ensure_ascii=False)}],
            "isError": False}


def serve_mcp() -> int:
    def send(obj: dict) -> None:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    def send_error(req_id, code: int, message: str) -> None:
        send({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})

    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            send_error(None, -32700, "Parse error")
            continue
        if not isinstance(msg, dict) or not isinstance(msg.get("method"), str):
            # 对端响应（我们从不发请求）与无 method 的消息一律忽略；仅带 id 的非响应按非法请求回应
            if isinstance(msg, dict) and "id" in msg and "result" not in msg and "error" not in msg:
                send_error(msg.get("id"), -32600, "Invalid Request")
            continue
        method = msg["method"]
        if "id" not in msg or msg["id"] is None:
            continue  # 通知（含 notifications/initialized）不回应
        req_id = msg["id"]  # id=0 是合法请求 id
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": req_id, "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fusion", "version": VERSION},
                "instructions": MCP_INSTRUCTIONS,
            }})
        elif method == "ping":
            send({"jsonrpc": "2.0", "id": req_id, "result": {}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": req_id, "result": {"tools": _mcp_tools()}})
        elif method == "tools/call":
            send({"jsonrpc": "2.0", "id": req_id, "result": _mcp_tool_call(params)})
        else:
            send_error(req_id, -32601, f"Method not found: {method}")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="fusion", description="Codex→ZCode Fusion 原型（本地，零依赖）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("prepare", help="从 spec JSON 创建任务")
    p.add_argument("--spec", required=True, help="spec 文件路径（JSON）")

    p = sub.add_parser("get", help="查询任务状态")
    p.add_argument("task_id")

    p = sub.add_parser("handoff", help="生成本轮派工包（prompt + result 模板，同轮幂等）")
    p.add_argument("task_id")

    p = sub.add_parser("dispatch", help="记录真实 UI 发送后的 ZCode 会话标签（不发送 UI 事件）")
    p.add_argument("task_id")
    p.add_argument("--session-label", required=True, help="ZCode 会话标签（非空）")

    p = sub.add_parser("result", help="校验并导入 taskdir/result.json（不可信执行者证据）")
    p.add_argument("task_id")

    p = sub.add_parser("verify", help="范围检查 + 运行验收命令")
    p.add_argument("task_id")

    p = sub.add_parser("review", help="逐条要求审查")
    p.add_argument("task_id")
    p.add_argument("--decisions", required=True, help="decisions JSON 文件路径")
    p.add_argument("--ack-test-changes", action="store_true",
                   help="显式确认既有测试文件的修改")

    p = sub.add_parser("retry", help="记录返工反馈并重新生成 brief")
    p.add_argument("task_id")
    p.add_argument("--feedback", required=True)

    p = sub.add_parser("apply", help="把 approved 的变化应用回原工作区（带逐文件 journal）")
    p.add_argument("task_id")
    p.add_argument("--confirm-recovery", action="store_true",
                   help="上次 apply 失败/中断后，人工核查过部分应用状态再显式确认继续")

    p = sub.add_parser("list", help="列出任务摘要与读取错误（可按 workspace 过滤）")
    p.add_argument("--workspace", default=None, help="可选：仅返回该 workspace 的任务")

    p = sub.add_parser("resume", help="只读恢复视图：路径、证据摘要与下一步建议")
    p.add_argument("task_id")

    sub.add_parser("mcp", help="以 stdio MCP 服务方式运行（换行分隔 JSON-RPC 2.0）")

    args = parser.parse_args(argv)
    try:
        if args.cmd == "mcp":
            return serve_mcp()
        if args.cmd == "prepare":
            spec_path = Path(args.spec)
            try:
                spec = json.loads(spec_path.read_text(encoding="utf-8"))
            except OSError as exc:
                raise FusionError(f"无法读取 spec 文件：{exc}") from exc
            except json.JSONDecodeError as exc:
                raise FusionError(f"spec 文件不是有效 JSON：{exc}") from exc
            result = cmd_prepare(spec)
        elif args.cmd == "get":
            result = cmd_get(args.task_id)
        elif args.cmd == "handoff":
            result = cmd_handoff(args.task_id)
        elif args.cmd == "dispatch":
            result = cmd_dispatch(args.task_id, args.session_label)
        elif args.cmd == "result":
            result = cmd_result(args.task_id)
        elif args.cmd == "verify":
            result = cmd_verify(args.task_id)
        elif args.cmd == "review":
            try:
                decisions = json.loads(Path(args.decisions).read_text(encoding="utf-8"))
            except OSError as exc:
                raise FusionError(f"无法读取 decisions 文件：{exc}") from exc
            except json.JSONDecodeError as exc:
                raise FusionError(f"decisions 文件不是有效 JSON：{exc}") from exc
            result = cmd_review(args.task_id, decisions, args.ack_test_changes)
        elif args.cmd == "retry":
            result = cmd_retry(args.task_id, args.feedback)
        elif args.cmd == "apply":
            result = cmd_apply(args.task_id, args.confirm_recovery)
        elif args.cmd == "list":
            result = cmd_list(args.workspace)
        elif args.cmd == "resume":
            result = cmd_resume(args.task_id)
        else:  # pragma: no cover
            parser.error(f"未知命令：{args.cmd}")
    except FusionError as exc:
        print(json.dumps({"error": {"message": str(exc)}}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
