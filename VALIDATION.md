# Codex 独立验收记录

日期：2026-10-01。执行者：本机 ZCode 桌面会话（GLM-5.3）；任务拆分、审查、验收与包装：Codex。
这是工作流原型的本地验收记录，不代表生产环境安全审计。

## 0.2.0 独立验收与原生联调

Codex 制定任务书，ZCode 在独立副本中实现；经一次具体返工后，Codex 独立检查并应用新版。

- **自动测试**：Codex 运行 `python3 -m unittest discover -s tests -v`，55 项通过（3.092s）。
  原有 29 项测试方法全部保留；既有文件仅调整 MCP 工具发现断言，仍要求原六项存在。
  新增 26 项覆盖派工与结果、进程锁、恢复决策、部分应用、旧任务兼容、CLI/MCP。
- **九组独立行为检查**：新 CLI 进程恢复已派工任务不建议重发；结果不能批准、旧轮次与
  布尔轮次拒绝；非 ASCII token 和实际越界修改明确报错；审批后副本变化阻止应用；
  独立失败检查覆盖执行者声称；真实 CLI 锁竞争与终止后释放；旧 0.1.0 已应用任务恢复；
  损坏任务 schema 不隐藏健康任务；真实子进程在首文件 `os.replace` 后 `os._exit(42)`，
  恢复识别未完成 journal，重复 apply/retry 拒绝。
- **真实原生联调**：Codex 创建未实现的 `parse_duration` 与只读独立测试，用新版
  prepare/handoff 生成派工包，在已登录 ZCode 桌面会话发送并确认开始后记录 dispatch。
  新进程 resume 显示 wait_zcode；ZCode 仅修改副本的 duration.py，原子交回 result.json。
  Codex 导入结果（状态仍 prepared、trusted=false），独立 verify 通过，逐条 review 后 apply。
  7 组合法输入、9 组非法格式、4 组类型错误共 20 组输入通过；unittest 为 2 个测试方法。
  只读测试哈希一致，无越界；应用前原文件仍未实现，应用后原目录测试再次通过，resume 为 done。
- **技能检查**：系统 Ruby YAML 解析 frontmatter 与 openai.yaml 通过。skill-creator 的
  quick_validate.py 因环境缺少 PyYAML 未能运行；未安装依赖，未声称该校验器通过。
- **接口与版本**：原六项 MCP 工具保留，新增五项，总数 11；三个版本标记一致为 0.2.0。
  构建白名单纳入 docs/WORKFLOW.md 与 tests/test_workflow.py。

桌面协作仍由当前 Codex 会话执行 Computer Use；CLI 的 dispatch 只记录真实发送后的标签。
进程锁只保护桥调用，不能约束 ZCode 直接写文件。journal 提供核查与显式继续流程，
不提供多文件事务、自动回滚或掉电一致性。实际桌面与 CLI 验证在 macOS；Linux CI 未运行，
本版使用 Unix flock，Windows 暂不支持。未接通 headless 认证，未测量成本或质量提升。

本机路径、任务标识、任务副本和详细运行记录只留开发目录，不进入公开分发包。
下方保留 0.1.0 历史验收事实。

## 实现与返工（0.1.0 历史）

Codex 下发任务书后，ZCode 实现 `fusion.py`、unittest 与中文说明。
Codex 阅读实现并复现工作区根软链接被接受的问题，同时发现审查确认参数会把字符串 `false`
当作确认、FIFO diff 读取可能阻塞。上述三个问题交回 ZCode 修复并补充真实行为测试。

Codex 在修复后独立运行：

```text
python3 -m unittest discover -s tests -v
Ran 29 tests in 1.715s
OK
```

覆盖副本隔离、源文件冲突、验收失败和超时、越界修改、要求逐条覆盖、证据非空、改写测试须确认、
软链接和路径穿越、FIFO 不阻塞、两轮返工上限、备份与应用、CLI 和真实 MCP 子进程协议。
安装后任务目录通过 `FUSION_TASKS_DIR` 指向项目，避免写插件缓存。

## 原生 ZCode 真实联调

示例为一个故意未完成的 `clamp` 数值截断函数及其独立验收测试。

1. Codex 创建该示例及独立验收测试。初始 verify 失败符合预期。
2. Codex 通过本机 ZCode 原生界面发送工具生成的 `brief.md`，只允许修改任务副本中的 `clamp.py`。
3. ZCode 完成副本实现并返回总结；Codex 阅读实际 diff 并再次运行独立验收。
4. 八组合法区间数据（含上下界、负数、浮点数、相同上下界）及非法区间抛出 ValueError 均通过，
   只有 `clamp.py` 改动；只读测试哈希一致，无越界修改，原示例在 apply 前仍未变化。
5. Codex 为两条硬性要求分别写入证据，review 标记 approved，再 apply 到原示例，保留原文件备份。
6. Codex 在应用后的示例目录再次运行 `python3 -m unittest -v test_clamp`：2 项测试通过。

开发机上的完整过程记录（任务副本与验收/审查/应用 JSON）保留在开发目录，不随分发包发布；
本文只保留可复查的事实结论，不含本机路径或任务标识。

## 包装与限制

公开包整理后，Codex 在独立临时目录中解压源码和安装包，确认 marketplace 的 `source.path`
指向存在的插件 manifest，所需技能、许可证、构建脚本与文档齐全；从解压后的插件启动 MCP，
initialize 成功并发现六个工具。从解压后的源码运行 29 项测试全部通过，再构建出的安装包
与交付包逐条目内容一致。构建脚本拒绝白名单路径的祖先软链接，两个归档均通过完整性检查，
并检查了发布文件清单和内容，未纳入本机个人目录或任务记录。

本机已通过 Codex CLI 注册本地 marketplace 并安装
`codex-zcode-fusion@local-zcode-fusion` 0.1.0；安装列表确认 `installed: true`、`enabled: true`。
Codex 对安装缓存中的实现、技能与 manifest 做了内容一致性检查，并从安装路径启动 MCP：
initialize（含请求 id=0）成功，六个工具全部被发现。没有为此改动账号、认证或模型配置。

插件包含 portable manifest、Codex 兼容 manifest、技能和本地工具。技能 frontmatter 与 openai.yaml
使用系统 Ruby YAML 解析校验；自带 quick_validate.py 所需的 PyYAML 在当前环境中缺失，因此未声称
通过该校验器，也未为此安装依赖。

当前确实使用 Codex 的 Computer Use 控制已登录 ZCode 桌面应用。ZCode headless 模型认证尚未接通，
没有后台自主调度、DSH 模型菜单或 token 节省测量。范围白名单提供副本检测和应用闸门，
不提供操作系统级权限隔离；同一任务不应并发执行工具调用，apply 不承诺多文件事务。

`.github/workflows/ci.yml` 配置了 Linux（Python 3.10/3.14）与 macOS（3.14）的 CLI 矩阵，
运行 unittest 与打包脚本。该 CI 属拟配置，GitHub 工作流此前没有实际运行过；
桌面协作只在 macOS 实际验证，其他 UI 平台未验证，也不承诺 Windows 桌面支持。
