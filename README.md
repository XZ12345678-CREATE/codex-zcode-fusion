# Codex ZCode Fusion

**Codex 规划与审查，ZCode 执行**——把「Lead / 执行者」双代理协作固化成可验收的本地工作流的原型插件。
零第三方依赖的 Python 3.10+ 本地工具，CLI 与 stdio MCP 服务共用同一套核心逻辑（`fusion.py`）。
本版使用 Unix `flock`；实际验证环境为 macOS，Linux CI 尚未运行，Windows 暂不支持。

- 实际阶段：**v0.2.0，本地工作流原型**（非生产工具）
- **核心区别**：Codex 负责规划与审查，执行者是**你已安装的 ZCode 桌面代理**——不新起一个
  agent 运行时、不需要额外 API Key；脚本只做三件事：保存任务副本、桥接派工与结构化结果、
  执行验收与应用闸门。
- 安装与调用见 [INSTALL.md](INSTALL.md)，独立验收与真实联调记录见 [VALIDATION.md](VALIDATION.md)，
  完整流程与接口 schema 见 [docs/WORKFLOW.md](docs/WORKFLOW.md)

已提供 Codex 本地插件 manifest 和 `codex-zcode-fusion` 技能。技能指导当前 Codex 通过 Computer Use
向已登录的 ZCode 桌面会话派工，再调用本工具导入执行者结果、验收、审查和应用。

## 快速开始

```bash
# 从源码构建分发包（仅标准库、不联网、不安装依赖）
python3 scripts/build_release.py
# 注册生成的 dist/local-marketplace，安装 codex-zcode-fusion@local-zcode-fusion，
# 然后在新的 Codex 对话中输入 $codex-zcode-fusion —— 详细步骤见 INSTALL.md
```

每次使用都应把 `FUSION_TASKS_DIR` 指向当前项目中的可写目录，避免把任务副本写进插件缓存。

## 原理

```
Codex (Lead)                       ZCode (执行者, 原生桌面会话)
    |                                       |
    | fusion_prepare(spec)                  |
    |   校验 spec、复制 allowed/context 文件  |
    |   到独立任务副本, 生成 brief.md         |
    | fusion_handoff(id)                    |
    |   生成本轮派工包(随机 token,幂等)       |
    |   返回可直接粘贴的 prompt ------------>|--> 按 brief 只编辑副本 work/
    |   (Computer Use 真实发送后)            |    并把结构化结果原子写入
    | fusion_dispatch(id, session_label)    |    taskdir/result.json
    |   仅记录真实发送后的会话标签            |
    | fusion_result(id)                     |
    |   严格校验 result.json, 存为不可信证据  |
    | fusion_verify(id)                     |
    |   清单比较(改动/越界/既有测试文件修改)   |
    |   unified diff + 运行 Lead 验收命令     |
    |                                       |
    | fusion_review(id, decisions)          |
    |   每条要求 passed+evidence, 恰好覆盖    |
    |   全部通过 -> approved                 |
    |        | 未通过: fusion_retry(id, feedback) 同一副本, 清空记录, 下轮换 token
    | fusion_apply(id)                      |
    |   先持久化逐文件 journal, 再原子应用回   |
    |   原工作区, 保留 before 备份            |
    | fusion_list() / fusion_resume(id)     |
    |   跨进程找回任务与证据, 给出下一步(只读) |
```

关键性质：

- **原文件在应用前不变**。ZCode 全程只操作任务副本；`apply` 前做完整预检（副本摘要与审查时
  一致、每个目标文件当前哈希与 prepare 记录一致、任何路径组件不得是软链接），任何一项不过即拒绝。
- **范围是白名单**。只复制 spec 声明的单个文件（不复制整个目录），每文件上限 10 MB；禁止绝对
  路径、`..`、重复、软链接（含祖先路径与工作区根本身）以及 `.git`/`.codex`/`.agents`/`.zcode`
  内的文件；apply 前若工作区根被替换为软链接同样拒绝。
- **派工可追踪**。`handoff` 每轮生成随机 token，同轮重复读取幂等（approved/applied 后拒绝
  再派工）；`dispatch` 须在真实 UI 发送并核对输入全文（含中文路径）与会话消息后记录，
  需先有本轮 handoff，同 token+会话标签幂等、多标签可追踪；执行者把结构化结果原子写入
  固定的 `taskdir/result.json`（唯一允许的副本外输出，上限 1MB）；`result` 严格校验
  task_id/round/handoff_token（ASCII）/白名单/证据覆盖/与实际变化完全一致（副本存在
  越界改动直接拒绝导入），且只作为**不可信证据**保存，绝不替代 verify/review。
  旧轮次或伪造的结果一律拒绝；恢复视图对不可导入的残留文件给出 `result_error`
  与有用下一步，不造成导入循环。
- **验收是真实验收**。`verify` 用 `subprocess(argv, shell=False, cwd=work)` 运行 Lead 提供的
  命令，带超时与输出截断；越界改动、超时、非零退出、软链接或 FIFO 等特殊文件都会导致
  verification failed。检查结束后再次计算副本清单，测试运行中产生的文件也计入范围
  （除 Python `__pycache__`/`.pyc` 缓存外不自动原谅任何生成文件）。
- **审查必须逐条覆盖**。`review` 的 decisions 必须恰好覆盖所有硬性要求且证据非空；改写既有测试
  文件必须显式 `--ack-test-changes` 确认（只接受真正的布尔值）。
- **返工不无限循环**。`retry` 保留同一副本、重新生成 brief、清空验证/审查/本轮结果并换新
  token，保留会话标签与历史；超过 2 轮返工即拒绝，由 Lead 决定后续。
- **并发有进程锁**。同一任务的写操作（handoff/dispatch/result/verify/review/retry/apply）在
  `taskdir/task.lock` 上使用非阻塞 `flock`，锁被占用立即报忙，持锁进程退出后自动恢复；
  状态保存始终原子替换；锁只保护本桥调用，不约束 ZCode 直接写副本文件。
- **应用不做多文件事务承诺**。先持久化 in-progress journal（含逐文件备份位置与
  before/after 摘要），再逐文件临时文件 + `os.replace` 原子替换并保存进度；IO 错误/
  中断后 journal 阻止盲目重试并**拒绝期间的一切流程写操作**（retry/handoff/dispatch/
  result/verify/review），需人工核查再 `--confirm-recovery` 幂等补完；删除文件暂不支持
  （明确报错说明）。

任务数据保存在 `.fusion/tasks/<uuid>/`（`work/` 执行副本、`orig/` 基线镜像供 diff、`brief.md` 任务书、
`task.json` 状态（含事件与派工历史）、`handoff.md` + `result-template.json` 派工包、`result.json`
执行者结果、`task.lock` 进程锁、`backup/` 应用前备份），默认位于脚本所在目录的 `.fusion/tasks/`。
可用环境变量 `FUSION_TASKS_DIR` 把任务存储目录指向别处：**安装到 Codex 插件缓存后，脚本目录通常
不宜写入，Lead 应把 `FUSION_TASKS_DIR` 指向当前项目中的可写目录，避免写插件缓存**；未设置时默认值不变。
测试即通过该变量指向临时目录。

## CLI 示例

```bash
# 1. Codex 准备任务（spec 见下方示例）
python3 fusion.py prepare --spec spec.json
# -> {"task_id": "<32位id>", "status": "prepared", "brief_path": "...", "work_path": "...", ...}

# 2. 生成本轮派工包（prompt 可直接粘贴给 ZCode；同轮重复调用幂等）
python3 fusion.py handoff <task_id>
# -> {"round": 1, "token": "...", "prompt": "...", "handoff_path": "...", "result_path": "...", ...}

# 3. 用 Computer Use 把 prompt 真实发送给 ZCode 会话后，记录会话标签
python3 fusion.py dispatch <task_id> --session-label "zcode-main-chat"

# 4. ZCode 完成后已把结构化结果原子写入 taskdir/result.json；Codex 导入校验（不可信证据）
python3 fusion.py result <task_id>

# 5. Codex 独立验收
python3 fusion.py verify <task_id>
# -> {"passed": true/false, "changes": {...}, "diffs": {...}, "checks": [...], "failures": [...]}

# 6. 逐条审查（decisions.json 为数组，每项 {requirement, passed, evidence}）
python3 fusion.py review <task_id> --decisions decisions.json
# 既有测试文件被改写时需显式确认：
python3 fusion.py review <task_id> --decisions decisions.json --ack-test-changes

# 7a. 未通过 -> 返工（保留同一副本，清空记录，下轮 handoff 换新 token，最多 2 轮）
python3 fusion.py retry <task_id> --feedback "multiply 缺少负数处理，请补充。"

# 7b. 全部通过 -> 应用回原工作区（逐文件 journal；上次失败/中断后需 --confirm-recovery）
python3 fusion.py apply <task_id>
python3 fusion.py apply <task_id> --confirm-recovery

# 跨进程找回任务与下一步建议（只读）
python3 fusion.py list [--workspace /abs/path]
python3 fusion.py resume <task_id>

# 随时查询
python3 fusion.py get <task_id>
```

`spec.json` 示例：

```json
{
  "workspace": "/absolute/path/to/project",
  "request": "用户原话……",
  "instructions": "给执行者的指示……",
  "requirements": ["实现 multiply 函数", "不修改清单外文件"],
  "allowed_files": ["app.py", "feature.py"],
  "context_files": ["notes.md"],
  "checks": [
    {"name": "unittest", "argv": ["python3", "-m", "unittest", "discover", "-s", "tests"], "timeout": 120}
  ]
}
```

`decisions.json` 示例：

```json
[
  {"requirement": "实现 multiply 函数", "passed": true, "evidence": "work/app.py:5 已实现并通过测试"},
  {"requirement": "不修改清单外文件", "passed": true, "evidence": "verify 报告无越界改动"}
]
```

`result.json`（执行者写入，schema 细节见 [docs/WORKFLOW.md](docs/WORKFLOW.md)）示例：

```json
{
  "task_id": "<32位id>", "round": 1, "handoff_token": "<本轮token>",
  "summary": "已实现 multiply 并新增 feature.py",
  "changed_files": ["app.py", "feature.py"],
  "requirements": [
    {"requirement": "实现 multiply 函数", "evidence": "work/app.py 新增 multiply"},
    {"requirement": "不修改清单外文件", "evidence": "仅改动清单内两个文件"}
  ],
  "checks": [{"name": "unittest", "result": "passed: 2 tests"}],
  "issues": []
}
```

所有命令输出 JSON；业务错误以 JSON 写到 stderr 并以非零码退出，不打印密钥或用户配置。

## MCP 注册

`python3 fusion.py mcp` 以 stdio 方式运行 MCP 服务（换行分隔 JSON-RPC 2.0，stdout 仅输出协议
消息），暴露 `fusion_prepare/get/handoff/dispatch/result/verify/review/retry/apply/list/resume`
十一个工具，`inputSchema` 完整，annotations 区分只读（`fusion_get/fusion_list/fusion_resume`）
与修改类操作（`fusion_apply` 另标 `destructiveHint`），业务失败返回 `isError: true`，支持请求
id=0，未知方法返回标准 `-32601`。

在支持 stdio MCP 的客户端中注册的**示例**（路径按实际安装位置调整；是否注册、注册到哪个客户端
由用户自行决定，本工具不会自动注册或安装任何东西）。若脚本安装在插件缓存等不宜写入的位置，
注册时通过 `env` 设置 `FUSION_TASKS_DIR` 指向当前项目的可写目录：

```json
{
  "mcpServers": {
    "fusion": {
      "command": "python3",
      "args": ["<fusion.py 绝对路径>", "mcp"],
      "env": {"FUSION_TASKS_DIR": "<当前项目可写目录>/.fusion-tasks"}
    }
  }
}
```

## 测试

```bash
python3 -m unittest discover -s tests -v
```

55 个用例覆盖：v0.1.0 的 29 项（任务存储目录 `FUSION_TASKS_DIR` 重定向、副本隔离不碰原文件、
原文件事后改动拒绝应用、审查后副本变动拒绝应用、越界改动失败、遗漏/重复要求与空证据拒绝、
验收命令失败与超时阻止批准、软链接与路径穿越拒绝（含工作区根本身）、
`test_changes_acknowledged` 仅接受真布尔值、FIFO 等特殊文件导致验收失败且内容不被读取、
apply 目标被换成软链接拒绝、完整 prepare→verify→review→apply 成功（含备份）、既有测试文件
修改需显式确认、返工流程与 2 轮上限、删除拒绝、`__pycache__` 缓存忽略、MCP 子进程
initialize/list/call（含 id=0 与错误路径）、CLI 子进程 JSON 输出）；v0.2.0 新增 26 项
（handoff 派工包幂等、retry 换 token、approved 后拒绝派工、dispatch 需先 handoff 且
同 token+标签幂等/多标签可追踪、result 的伪造/旧轮次/实际越界改动/schema 违规/非 ASCII
token/软链接/FIFO/超大/无效 JSON 拒绝与合法导入不批准任务、真实子进程持锁报忙与终止后
恢复、只读操作不受锁影响、注入第二文件替换失败后的 journal 部分进度记录与 resume 阻止
盲目重试、journal 活动期间全部流程操作拒绝、`--confirm-recovery` 幂等补完、模拟崩溃窗口
（首文件已写、进度未保存）的 plan/恢复行为、中断 journal 检测、v0.1.0 旧任务兼容、
resume 决策树各状态（含返工上限 reassess）与只读性、retry 后旧 result.json 不造成导入
循环、list 摘要/损坏任务容忍/workspace 过滤、CLI 与 MCP 新接口端到端、MCP 进程在坏结果
负载下存活）。只做本地模拟，不调用模型、不处理凭据、不联网。

CI 矩阵见 `.github/workflows/ci.yml`（Linux Python 3.10/3.14、macOS 3.14，运行 unittest 与打包
脚本）：属拟配置，GitHub 工作流尚未实际运行过，详见 [VALIDATION.md](VALIDATION.md)。

## 致谢与说明

本项目的 Lead / 执行者分工受
[aa2246740/dsh-model-fusion](https://github.com/aa2246740/dsh-model-fusion) 启发。
本项目源码与文档为独立实现，未复制其代码、文案或测量数据；与 Codex、ZCode、DSH 的官方实现
无关联，也不是它们的官方产品。

## 当前实际限制（务必阅读）

- 这是**通过当前 Codex 会话 + 原生 ZCode 桌面界面的工作流原型**。技能指导 Codex 使用 Computer Use
  把派工 prompt 发给 ZCode 并观察结果；`fusion.py` 本身不含 GUI 控制，`dispatch` 只记录调用者
  声明已完成的真实发送，不发送也不核实 UI 事件。离开支持桌面控制的 Codex 会话时不会自行派工，
  也不是后台常驻调度器，也没有程序化手段锁定 Lead 只读。
- **没有 DSH 模型菜单**，**没有 Token 节省实测**数据，**没有操作系统级沙盒**：文件范围控制 =
  副本检测 + 应用闸门，ZCode 进程仍遵从其既有权限；进程锁只串行化本桥调用，不约束 ZCode
  直接写副本文件。
- **headless 认证尚未接通**：原生 CLI 0.16.9 能启动，但 headless 新会话没有可用模型；本原型依赖
  已登录桌面会话，也未修复 CLI 认证，不声称完成过 headless 联调。
- `apply` 不提供多文件事务保证；IO 错误/中断后靠 journal + 人工核查 + `--confirm-recovery`
  幂等补完，不做自动回滚，也不承诺掉电一致性；删除文件暂被拒绝并说明原因。
- `verify`/`review` 的一致性基于内容哈希摘要；任务目录（`.fusion/tasks/`）本身未加密、未做权限
  隔离，请勿在其中放置敏感内容。
- 分发包包含本地插件 manifest、技能和 Python 工具。没有账号设置、凭据配置或自动启动钩子；
  `fusion.py` 不自行注册 MCP 或安装插件。安装状态与步骤见 INSTALL.md。
- 桌面协作只在 macOS 上实际验证过，其他 UI 平台未验证，不承诺 Windows 桌面支持。

## 文件清单

- `fusion.py` — 全部实现（核心逻辑 + CLI + stdio MCP，11 个工具）
- `tests/test_fusion.py` — v0.1.0 行为覆盖 29 项 unittest
- `tests/test_workflow.py` — v0.2.0 工作流行为覆盖 26 项 unittest
- `scripts/build_release.py` — 分发包构建脚本（产物在 `dist/`，含 sha256 清单 `release-manifest.json`）
- `skills/codex-zcode-fusion/` — Codex 派工、审查与返工技能
- `plugin.json` / `.codex-plugin/plugin.json` — 插件 manifest
- `demo_workspace/` — `clamp` 联调示例（含独立验收测试）
- `docs/WORKFLOW.md` — v0.2.0 完整工作流与接口 schema（派工包/result 契约/锁/journal/resume 决策表）
- `docs/GITHUB_PUBLISH.md` — 自行发布到 GitHub 的步骤
- `README.md` / `INSTALL.md` / `VALIDATION.md` / `CHANGELOG.md` / `LICENSE`（MIT）
