# 更新日志

## 0.2.0 - 2026-10-01

原生应用协作增强版：Codex 在 Codex 中规划与验收，ZCode 在已登录桌面会话执行。

- 新增 handoff/dispatch/result/list/resume：可追踪派工、绑定任务与轮次的结构化结果、
  跨进程找回与恢复；结果只作执行者证据，仍需 Codex 独立验收与逐条审查。
- 同一任务写操作增加非阻塞进程锁；记录事件与会话历史，兼容 0.1.0 任务。
- apply 写前保存计划、备份位置和摘要，逐文件记录进度；中断后阻止盲目覆盖，
  提供核查后的显式继续，不承诺事务或自动回滚。
- 修复独立审查发现的恢复循环、陈旧结果误用、半应用任务返工、非 ASCII token
  错误及越界结果导入问题；技能加入输入全文与真实消息核对，以及 ASCII 路径交接方式。
- Codex 独立运行 55 项测试与九组行为检查通过；完成 20 组输入的 ZCode 原生联调。
  完整事实与限制见 VALIDATION.md；GitHub CI 尚未实际运行。
- 更新流程文档、技能与分发包白名单；版本统一为 0.2.0。

## 0.1.0 - 2026-10-01

首个公开原型。

- `fusion.py`：`prepare / verify / review / retry / apply` 闭环；CLI 与 stdio MCP 共用核心；
  白名单副本与应用闸门、逐条要求审查、返工上限、软链接与特殊文件防护、`FUSION_TASKS_DIR`
  任务目录重定向。
- `tests/test_fusion.py`：29 项本地 unittest 全部通过。
- 在 macOS 上完成一次原生 ZCode 真实联调（`clamp` 数值截断示例，2 项独立验收测试通过）。
- `scripts/build_release.py`：生成本地 marketplace、marketplace 压缩包与公开源码包。
- `.github/workflows/ci.yml`：Linux（Python 3.10/3.14）与 macOS（3.14）CLI 矩阵，
  属拟配置，尚未实际运行。
