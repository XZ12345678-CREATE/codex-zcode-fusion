# 安装和使用

这是一个本地 Codex 插件：技能负责安排 Codex → ZCode 协作，Python 工具保存任务副本并执行验收和应用。
需要 Python 3.10+、已登录且能正常工作的 ZCode 桌面应用，以及 Codex 的 Computer Use 功能。
本版的进程锁依赖 Unix `flock`，在 macOS 实际验证；Linux CI 尚未运行，Windows 暂不支持。
无需新增 OpenAI API Key；不会配置 ZCode 的 headless 认证。

## 从源码构建分发包

在源码根目录运行（仅标准库、不联网、不安装依赖）：

```bash
python3 scripts/build_release.py
```

产物全部在 `dist/`：本地 marketplace 目录、marketplace 压缩包、公开源码包和带 sha256 的
产物清单 `release-manifest.json`。脚本可重复运行，只会重写这些明确的输出。

## 调用

安装后在新的 Codex 对话中输入，例如：

> 使用 $codex-zcode-fusion。请你规划和验收，让 ZCode 为这个项目实现……。允许修改的文件是……。

Codex 会读取技能，准备任务副本，通过 ZCode 原生界面派工，再独立审查结果。没有足够项目上下文时，
它会先查明文件范围与验收方式。需要能读取源码、运行本地工具并控制 ZCode 的当前会话。
任务期间保持 ZCode 的执行对话可用；切换应用状态可能使 UI 派工暂停，需要重新观察界面。

已经安装的技能可能需要新对话才能出现在技能列表中。如果新对话仍未发现技能，再重启 Codex。
当前对话不会通过安装操作自动获得新增工具。

## 手动安装分发包

`dist/local-marketplace/` 是可被 Codex CLI 识别的本地 marketplace，包含插件目录。
把它移动到长期保存的位置后再注册；注册之后保留该目录。

```bash
codex plugin marketplace add '/绝对路径/local-marketplace' --json
codex plugin add codex-zcode-fusion@local-zcode-fusion --json
```

`dist/codex-zcode-fusion-0.2.0.zip` 是同一个 marketplace 的压缩包，先解压再使用上述步骤。
`dist/codex-zcode-fusion-0.2.0-source.zip` 是公开源码包，解压后即完整项目，可再次运行
`scripts/build_release.py` 重新构建。分发包需要解压后本地安装；本插件未配置从 GitHub 仓库
根目录直接 marketplace 安装，不提供这样的命令。发布到 GitHub 的步骤见 `docs/GITHUB_PUBLISH.md`，
完整工作流与接口 schema（派工包、result.json 契约、锁、apply journal、resume 决策表）见
`docs/WORKFLOW.md`。
插件不包含服务器配置、自动启动钩子、模型 API 或认证信息。此次使用的是技能 + CLI，MCP 是可选接口，
需要时再按照 README 手动配置。

## 不安装也可使用

让 Codex 读取本目录的 `skills/codex-zcode-fusion/SKILL.md` 即可采用这套流程。
每次执行工具都应把任务数据指向当前项目中的可写目录：

```bash
FUSION_TASKS_DIR='/绝对路径/当前项目/.fusion-tasks' \
  python3 '/绝对路径/插件/fusion.py' prepare --spec '/绝对路径/spec.json'
```

后续 handoff/dispatch/result/verify/review/retry/apply/list/resume 必须使用相同的
`FUSION_TASKS_DIR`。派工流程：`handoff` 生成可直接粘贴给 ZCode 的 prompt；真实发送后用
`dispatch --session-label` 记录会话标签；ZCode 把结构化结果原子写入 taskdir/result.json 后用
`result` 导入校验。不要在 Codex 插件缓存中存放项目副本或任务数据。

## 0.1.0 历史安装记录

2026-10-01 已在本机完成注册、安装与验证的是 **0.1.0**：

- 插件 ID：`codex-zcode-fusion@local-zcode-fusion`
- 版本：`0.1.0`
- Codex CLI 返回 `installed: true`、`enabled: true`
- 已验证安装副本的实现与技能内容一致，并完成 MCP initialize 和六工具发现

**0.2.0 尚未安装**：截至本文更新，0.2.0 仍在等待 Codex 验收，未构建分发包、未注册安装、
也未做新版本的真实联调；安装后 MCP 应发现十一个工具。上述 0.1.0 记录属于开发机器历史事实，
予以保留；分发包在其他机器上仍需自行安装。

## 卸载

在 Codex 插件管理界面卸载 `codex-zcode-fusion`，或者使用：

```bash
codex plugin remove codex-zcode-fusion@local-zcode-fusion
```

源码和项目中的任务副本不会随插件卸载自动删除。
