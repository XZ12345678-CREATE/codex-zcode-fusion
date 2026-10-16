# 自行发布到 GitHub

以下步骤由你（项目所有者）在自己机器上执行。当前仓库尚未创建 git 仓库，本页命令仅作说明，
没有在这次整理中运行过。

## 1. 构建并取出源码包

```bash
python3 scripts/build_release.py
```

从 `dist/` 取 `codex-zcode-fusion-0.2.0-source.zip`，解压到任意独立目录。解压出的
`codex-zcode-fusion/` 就是完整、可再次构建的公开项目目录（含构建脚本、测试、CI 与文档）。

## 2. 只上传独立项目目录

上传解压出的 `codex-zcode-fusion/` 目录内容；**不要上传它的上级工作区**，也不要上传任何包含
个人路径、任务副本或内部记录的目录。

方式一：GitHub 网页新建仓库后用 "uploading an existing file" 直接拖入文件。
同时上传 `.codex-plugin/`、`.github/` 和 `.gitignore` 等隐藏内容，提交后检查它们是否出现在仓库中。

方式二：用 `git` + `gh` 手动初始化并推送。`gh` 需已安装并登录，用法见
[官方手册](https://cli.github.com/manual/gh_repo_create)：

```bash
cd codex-zcode-fusion
git init -b main
git add .
git commit -m "Release codex-zcode-fusion 0.2.0"
gh repo create codex-zcode-fusion --public --source . --remote origin --push \
  --description 'Codex plans and reviews; ZCode executes. A local Fusion workflow plugin.'
```

没有安装 `gh` 时，用方式一（网页建仓）创建空仓库后，自行添加 remote 并推送即可。

仓库建议：

- 名称：`codex-zcode-fusion`
- 描述：`Codex plans and reviews; ZCode executes. A local Fusion workflow plugin.`
- topics 建议：`codex`、`zcode`、`mcp`、`llm-workflow`、`agent-workflow`、`automation`、
  `developer-tools`

## 3. 许可证

当前为 MIT License，署名 "Codex ZCode Fusion contributors"。公开发布前可按你的意愿修改署名
或更换协议；改完后重新运行 `scripts/build_release.py` 生成分发包。

## 4. CI

`.github/workflows/ci.yml` 已包含在源码包中，push 后 GitHub Actions 会自动运行 unittest 与
打包脚本。该工作流此前从未实际运行过，首次运行如遇环境差异可能需要微调；它不发布 release、
不上传任何凭据。
