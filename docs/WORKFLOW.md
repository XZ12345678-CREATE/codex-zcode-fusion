# Fusion 工作流（v0.2.0）

本文是流程与接口的详细说明：派工包、结构化结果 schema、并发锁、apply journal、
resume 决策与兼容性。技能（`skills/codex-zcode-fusion/SKILL.md`）保持简短并指向本文；
工具实现见 `fusion.py`（CLI 与 stdio MCP 共用同一核心，MCP 工具共 11 个）。

角色约定：**Codex 是 Lead**（规划、派工、验收、审查、应用），**ZCode 是执行者**
（原生桌面会话，只在任务副本内改动）。桌面控制由 Codex 的 Computer Use 完成；
`fusion.py` 不包含任何 GUI 自动化代码。

## 生命周期

```
prepare ─→ handoff ─→ (Lead 用 Computer Use 真实发送 prompt) ─→ dispatch(记录标签)
    │                                                        │
    │                                       ZCode 只编辑 work/，并把结构化结果
    │                                       原子写入 taskdir/result.json
    │                                                        │
    │                                                  result(校验导入,不可信)
    │                                                        │
    └── retry(≤2轮,换token) ◄── review ◄── verify ◄──────────┘
                │                  │
                │             approved ─→ apply(journal,逐文件)
                │                            │
                └──────────────────────► applied ─→ (list/resume 随时可查)
```

任务数据都在 `FUSION_TASKS_DIR/<task_id>/`：`work/`（执行副本）、`orig/`（基线镜像）、
`brief.md`（任务书）、`task.json`（唯一权威状态：事件、派工历史、结果证据、journal）、
`handoff.md` + `result-template.json`（派工包）、`result.json`（执行者结果，固定路径）、
`task.lock`（进程锁）、`backup/`（apply 前备份）。

## 派工：handoff 与 dispatch

`handoff <task_id>`（MCP `fusion_handoff`）：

- 生成 `taskdir/handoff.md`（执行规则 + result schema 说明）与
  `taskdir/result-template.json`（预填 task_id/round/token 的填写模板）；
- 每轮随机 `token`（`secrets.token_hex(16)`）；轮次 `round = rework_rounds + 1`
  （首发为 1，每次 retry 后 +1 并换新 token）；
- **同一轮重复调用幂等**：返回同一 token 与路径，文件丢失时按原 token 重建；
- 返回 `prompt`（可直接粘贴给 ZCode）、`handoff_path`、`result_path`、`token`、`round`。

`dispatch <task_id> --session-label LABEL`（MCP `fusion_dispatch`）：

- **前置条件：本轮 handoff 已生成**（否则拒绝，避免把派工按轮次误记到尚未生成的新包）；
- **仅在 Lead（人类或 Codex UI）实际发送之后调用**，记录 ZCode 会话标签与派工历史；
  发送前须核对输入框全文（含中文路径等非 ASCII 内容）确实生效、且会话中已出现消息并
  开始读取 brief——click 成功本身不构成派工；
- 相同 token + 相同会话标签的重复记录幂等（不新增历史）；同轮不同会话标签会追加，
  保留可追踪的多会话历史；
- 工具本身不发送任何 UI 事件，也不核实发送是否发生——这是调用者声明的事实记录；
- approved/applied 任务拒绝生成 handoff（避免无故再次派工）。

## 结构化结果：result.json schema

执行者唯一允许的 work 外输出。**必须原子写入**（先写完整临时文件再 `os.replace`），
写完再报告。固定路径 `taskdir/result.json`，大小上限 1MB。

| 字段 | 类型 | 校验规则 |
| --- | --- | --- |
| `task_id` | string | 必须等于本任务 ID（伪造/串任务拒绝） |
| `round` | 真整数 | 不接受布尔或字符串；必须等于本轮 round（旧轮次拒绝） |
| `handoff_token` | ASCII string | 非空且须为 ASCII（非 ASCII 直接拒绝，绝不进入比较）；必须与本轮 token 一致（常量时间比较） |
| `summary` | string | 非空 |
| `changed_files` | string[] | 合法相对路径、无重复、全部在 allowed_files 内，且**与副本实际变化完全一致**；副本存在白名单外的实际改动时**直接拒绝导入**（verify 仍是独立后续闸门） |
| `requirements` | `{requirement, evidence}[]` | 恰好覆盖 spec 全部硬性要求（不缺失/不多余/不重复），evidence 非空 |
| `checks` | `{name, result}[]` | name 与 result 均为非空字符串；**仅为执行者声称**，Codex 独立复验 |
| `issues` | string[] | 可为空 |

`result <task_id>`（MCP `fusion_result`）读取并严格校验后，把结果存入 task.json 作为
**不可信执行者证据**（`trusted: false`）。它不修改 review/status 为 approved，不代替
verify。读取层面拒绝：文件缺失、软链接、FIFO/设备等特殊文件（`O_NONBLOCK` + `fstat`
防护，绝不阻塞）、超过 1MB、非 UTF-8、无效 JSON、非对象。旧轮次或 token 不符的
result.json 留在盘上也不会被认可；retry 清除本轮结果，旧文件需按新 handoff 重写。
`resume`/`list` 对该文件只做**有界（≤1MB）读取**判断，绝不用无界哈希读取大文件：
不可导入（旧轮次残留/无效/过期/特殊文件）时在恢复视图给出明确的 `result_error`
与有用下一步（生成最新 handoff、等待执行者纠正、重新提交），不会建议一个会被
拒绝的 `import_result` 造成错误循环，也不自动重发。

## 并发锁

- 同一任务的写操作（handoff/dispatch/result/verify/review/retry/apply）在
  `taskdir/task.lock` 上用 `flock(LOCK_EX | LOCK_NB)` 加**非阻塞**进程文件锁，
  覆盖完整「读 task.json → 修改 → 原子保存」动作；
- 锁被其他进程占用时立即报「忙」，不等待；持锁进程退出后内核自动释放，即可恢复；
- 各命令绝不嵌套获取同一把锁（避免自锁）；`get/list/resume` 只读，不加锁；
- **锁只保护本桥的并发调用，不约束 ZCode 直接写副本文件**；
- task.json 保存始终是「写临时文件 + `os.replace`」原子替换。

## apply journal（部分应用恢复）

`apply` 在写任何文件**之前**先把 in-progress journal 持久化进 task.json（计划、
`files_done`、`files_pending`、备份目录），之后逐文件「备份 → 写临时文件 →
`os.replace` → 保存进度」。因此：

- `journal.plan` 在写文件前即包含每个文件的**备份位置**与预期 **before/after sha256**
  （modify：before=prepare 记录的原始哈希；create：before 为空），供人工核对；
- IO 错误：journal 记为 `failed` 并保存错误信息与已写入清单；
- 进程中断：journal 留在 `in_progress`，下次持锁调用时检测并标记 `interrupted`；
- `files_done` = **已确认写入**；`files_pending` = **进度未保存、崩溃窗口内可能已写入**，
  需核对实际源文件——journal 不把 pending 表述成确定未写入；原备份保留在备份目录；
- journal 处于 `in_progress/failed/interrupted` 时：`apply` 直接拒绝**盲目重试**，
  且 `retry/handoff/dispatch/result/verify/review` 一律拒绝（只读 `get/list/resume`
  可用），不得把半应用任务改成 needs_revision 或用后续 review/reset 掩盖 journal；
  `resume` 给出 `manual_check`（列出已确认写入/待核对文件/备份路径，要求人工核查）；
- 人工核查后以 `apply --confirm-recovery`（MCP `confirm_recovery: true`）继续：
  重新做完整预检，目标哈希已等于副本哈希的文件幂等跳过，只补写剩余文件；
- `retry` 永不抹掉 journal；**不承诺多文件事务、自动回滚或掉电一致性**；
- 旧 v0.1.0 任务没有 journal 也能正常 apply（视为无恢复需求）。

## resume 决策表

`resume <task_id>`（MCP `fusion_resume`）为**只读**操作：返回全部路径、原需求、
会话标签、最新结果/验证/审查摘要、副本摘要一致性（`work_digest_matches_*`）与
`next_action`/`reason`。判定顺序（前者优先）：

| 实际状态 | next_action | 说明 |
| --- | --- | --- |
| journal 处于 in_progress/failed/interrupted | `manual_check` | 最高优先级阻止盲目 apply/重试；期间流程写操作全部拒绝 |
| 已 applied | `done` | 不重复 apply |
| approved 且副本与审查摘要不一致 | `retry`（未到上限）/ `reassess`（已到 2 轮上限） | apply 会被拒绝；verify 不接受 approved 任务；原审批摘要保留在 review 字段 |
| approved 且摘要一致 | `apply` | 用户原始请求授权时可应用 |
| 审查未通过（needs_revision） | `retry` / `reassess`（已到上限） | 优先给返工反馈，不建议会被拒绝的重复 review |
| 最近验证通过且副本摘要未变 | `review` | |
| 最近验证未通过 | `retry` / `reassess`（已到上限） | 上限时不再建议会被拒绝的 retry，由 Lead 重新评估 |
| 验证后副本又被改动 | `verify` | 摘要不一致，重新验证 |
| 有可导入的 result.json（本轮 handoff 匹配且未导入/已变化） | `import_result` | 运行 `result` 导入 |
| result.json 不可导入（旧轮次/无效/过期/特殊文件） | 由后续规则给出 | 在 `result_error` 说明原因并给出有用下一步，不造成导入循环 |
| 已导入有效结果 | `verify` | 独立验收，不采信执行者声明 |
| 本轮已派工 | `wait_zcode` | 等待或人工检查 ZCode；**不自动重新派工** |
| 已有 handoff 未记录派工 | `dispatch` | 真实 UI 发送（核对输入全文与会话消息）后记录标签 |
| 尚无 handoff | `handoff` | 旧 v0.1.0 任务也可不经 handoff 直接 verify/review/apply |

所有给出的 next_action 都必须可以执行：会被当前状态拒绝的工具（如对 approved 任务
verify、达到上限后的 retry）不会被建议，改给 `reassess` 等人工决策动作。

`list`（MCP `fusion_list`，可按 `workspace` 过滤）返回所有任务摘要与读取错误数组：
忽略非任务 ID 目录；单个任务数据损坏（含 schema 异常）只计入 errors，不影响其余任务，
不让整个 list 崩溃；摘要含 `has_pending_result`（本轮 handoff 匹配且未导入/已变化）与
`result_error`，便于跨进程找回待处理任务。

## CLI 与 MCP 对照

| 操作 | CLI | MCP 工具 | 锁 |
| --- | --- | --- | --- |
| 准备任务 | `prepare --spec spec.json` | `fusion_prepare` | —（新任务） |
| 查询 | `get ID` | `fusion_get` | 只读 |
| 派工包 | `handoff ID` | `fusion_handoff` | 写锁 |
| 记录派工 | `dispatch ID --session-label LABEL` | `fusion_dispatch` | 写锁 |
| 导入结果 | `result ID` | `fusion_result` | 写锁 |
| 验收 | `verify ID` | `fusion_verify` | 写锁 |
| 审查 | `review ID --decisions d.json [--ack-test-changes]` | `fusion_review` | 写锁 |
| 返工 | `retry ID --feedback "…"` | `fusion_retry` | 写锁 |
| 应用 | `apply ID [--confirm-recovery]` | `fusion_apply` | 写锁 |
| 列表 | `list [--workspace PATH]` | `fusion_list` | 只读 |
| 恢复视图 | `resume ID` | `fusion_resume` | 只读 |

所有命令输出 JSON；业务错误以 JSON 写 stderr、退出码 1。`get` 在保留 v0.1.0 全部
键的基础上增补 `handoff` / `dispatches` / `executor_result` / `apply_journal` /
`events`（最近 10 条）流程字段。

## 兼容性与边界

- v0.1.0 任务（task.json 无新字段）可直接继续 verify/review/apply；`result` 对无
  handoff 的任务明确拒绝；retry 保留会话标签与历史，只清本轮结果与 token。
- 事件与派工历史只存 task.json（上限 200 条，超出丢弃最旧），随原子保存更新，
  不存在双文件事务假设；`handoff.md`/模板是可由 task.json 幂等重建的产物。
- 范围控制 = 副本检测 + 应用闸门，不是操作系统沙盒；不处理凭据、不联网；
  headless 认证未接通，桌面派工依赖已登录的 ZCode 会话与 Codex Computer Use。
