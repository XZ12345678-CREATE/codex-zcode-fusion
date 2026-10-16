#!/usr/bin/env python3
"""构建 codex-zcode-fusion 的公开分发包到 dist/（仅标准库，不联网）。

产物：
  dist/local-marketplace/                       本地 marketplace（可直接注册）
  dist/codex-zcode-fusion-<version>.zip         marketplace 压缩包（顶层 local-marketplace/）
  dist/codex-zcode-fusion-<version>-source.zip  公开源码包（顶层 codex-zcode-fusion/，可再次构建）
  dist/release-manifest.json                    产物清单与 sha256

原则：只纳入下方显式白名单文件；不递归打包整个项目，不自动发现其他文件；
拒绝到白名单文件的任何目录组件（含祖先目录）是软链接，防止意外打包其他位置的内容；
绝不纳入 .git/.fusion/.zcode/__pycache__、凭据或含本机路径的内部文档。
脚本可重复运行，只会重写上述明确输出，不删除 dist 目录本身。
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
MARKETPLACE_DIR = DIST / "local-marketplace"
MANIFEST_OUT = DIST / "release-manifest.json"
PLUGIN_NAME = "codex-zcode-fusion"
MARKETPLACE_NAME = "local-zcode-fusion"

MARKETPLACE_JSON = {
    "name": MARKETPLACE_NAME,
    "interface": {"displayName": "Local ZCode Fusion"},
    "plugins": [
        {
            "name": PLUGIN_NAME,
            "source": {"source": "local", "path": f"./plugins/{PLUGIN_NAME}"},
            "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
            "category": "Productivity",
        }
    ],
}

# 公开发布文件集（源码根相对路径 -> 包内相对路径）。
# 同一文件集用于本地 marketplace 插件目录与公开源码包：安装包内 README/INSTALL 引用的
# 构建脚本、CHANGELOG、发布文档等必须同样存在，避免安装后出现缺失文件与断链。
PACKAGE_FILES = [
    ("fusion.py", "fusion.py"),
    ("plugin.json", "plugin.json"),
    (".codex-plugin/plugin.json", ".codex-plugin/plugin.json"),
    (".gitignore", ".gitignore"),
    ("LICENSE", "LICENSE"),
    ("README.md", "README.md"),
    ("INSTALL.md", "INSTALL.md"),
    ("VALIDATION.md", "VALIDATION.md"),
    ("CHANGELOG.md", "CHANGELOG.md"),
    ("docs/GITHUB_PUBLISH.md", "docs/GITHUB_PUBLISH.md"),
    ("docs/WORKFLOW.md", "docs/WORKFLOW.md"),
    ("scripts/build_release.py", "scripts/build_release.py"),
    ("tests/test_fusion.py", "tests/test_fusion.py"),
    ("tests/test_workflow.py", "tests/test_workflow.py"),
    ("demo_workspace/clamp.py", "demo_workspace/clamp.py"),
    ("demo_workspace/test_clamp.py", "demo_workspace/test_clamp.py"),
    ("skills/codex-zcode-fusion/SKILL.md", "skills/codex-zcode-fusion/SKILL.md"),
    ("skills/codex-zcode-fusion/agents/openai.yaml", "skills/codex-zcode-fusion/agents/openai.yaml"),
    (".github/workflows/ci.yml", ".github/workflows/ci.yml"),
]


class BuildError(Exception):
    """构建校验失败；消息不包含任何敏感信息。"""


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_version() -> str:
    try:
        plugin = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
        compat = json.loads((ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BuildError(f"manifest 读取或解析失败：{exc}") from exc
    fusion_text = (ROOT / "fusion.py").read_text(encoding="utf-8")
    m = re.search(r'^VERSION = "([^"]+)"$', fusion_text, re.MULTILINE)
    versions = {
        "plugin.json": plugin.get("version"),
        ".codex-plugin/plugin.json": compat.get("version"),
        "fusion.py": m.group(1) if m else None,
    }
    if None in versions.values() or len(set(versions.values())) != 1:
        raise BuildError(f"版本不一致：{versions}")
    if plugin.get("name") != PLUGIN_NAME or compat.get("name") != PLUGIN_NAME:
        raise BuildError("manifest 插件名与预期不符")
    return versions["plugin.json"]


def assert_no_symlink_ancestors(rel: str) -> None:
    """从源码根到白名单文件逐级检查：任何已存在组件是软链接即拒绝，防止打包到其他位置。"""
    cur = ROOT
    for part in rel.split("/"):
        cur = cur / part
        if cur.is_symlink():
            raise BuildError(f"白名单路径包含软链接（含祖先目录），拒绝打包：{rel}（{cur}）")


def validate_inputs() -> None:
    for src, _ in PACKAGE_FILES:
        assert_no_symlink_ancestors(src)
        p = ROOT / src
        if not p.exists():
            raise BuildError(f"缺少白名单文件：{src}")
        if p.is_symlink() or not p.is_file():
            raise BuildError(f"白名单文件不是常规文件（或为软链接）：{src}")


def write_zip(zip_path: Path, entries: list[tuple[Path, str]], top: str) -> None:
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for abs_path, rel in sorted(entries, key=lambda e: e[1]):
            zf.write(abs_path, f"{top}/{rel}")


def main() -> int:
    try:
        version = read_version()
        validate_inputs()

        # 只清理明确输出物；不动 dist 目录内的其他内容
        if MARKETPLACE_DIR.exists():
            shutil.rmtree(MARKETPLACE_DIR)
        marketplace_zip = DIST / f"{PLUGIN_NAME}-{version}.zip"
        source_zip = DIST / f"{PLUGIN_NAME}-{version}-source.zip"
        for p in (marketplace_zip, source_zip, MANIFEST_OUT):
            if p.exists():
                p.unlink()
        DIST.mkdir(parents=True, exist_ok=True)

        # 1) 本地 marketplace 插件目录（完整发布文件集）
        plugin_dir = MARKETPLACE_DIR / "plugins" / PLUGIN_NAME
        for src, rel in PACKAGE_FILES:
            target = plugin_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / src, target)

        # 2) marketplace.json（生成后回读校验 JSON 有效）
        mjson_path = MARKETPLACE_DIR / ".agents" / "plugins" / "marketplace.json"
        mjson_path.parent.mkdir(parents=True, exist_ok=True)
        mjson_path.write_text(
            json.dumps(MARKETPLACE_JSON, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        json.loads(mjson_path.read_text(encoding="utf-8"))

        # 3) marketplace 压缩包（顶层 local-marketplace/）。
        #    插件文件的归档名必须带 plugins/<插件名>/ 前缀，与 marketplace.json 的 source.path 对应。
        plugin_prefix = f"plugins/{PLUGIN_NAME}"
        marketplace_entries = [(mjson_path, ".agents/plugins/marketplace.json")] + [
            (plugin_dir / rel, f"{plugin_prefix}/{rel}") for _, rel in PACKAGE_FILES
        ]
        write_zip(marketplace_zip, marketplace_entries, "local-marketplace")

        # 4) 公开源码包（顶层 codex-zcode-fusion/，可从解压目录再次构建）
        source_entries = [(ROOT / src, rel) for src, rel in PACKAGE_FILES]
        write_zip(source_zip, source_entries, PLUGIN_NAME)

        # 5) 产物清单与 sha256
        artifacts = []
        for p in (marketplace_zip, source_zip):
            artifacts.append({"path": p.name, "bytes": p.stat().st_size, "sha256": sha256_file(p)})
        manifest = {
            "name": PLUGIN_NAME,
            "version": version,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "marketplace": {"name": MARKETPLACE_NAME, "dir": "local-marketplace",
                            "entries": len(marketplace_entries)},
            "source_package": {"top": PLUGIN_NAME, "entries": len(source_entries)},            "artifacts": artifacts,
        }
        MANIFEST_OUT.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

        print(f"version {version}")
        print(f"marketplace dir : {MARKETPLACE_DIR.relative_to(ROOT)} ({len(marketplace_entries)} files)")
        for a in artifacts:
            print(f"artifact        : {a['path']} ({a['bytes']} bytes, sha256 {a['sha256']})")
        print(f"manifest        : {MANIFEST_OUT.relative_to(ROOT)}")
    except (BuildError, OSError) as exc:
        print(f"构建失败：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
