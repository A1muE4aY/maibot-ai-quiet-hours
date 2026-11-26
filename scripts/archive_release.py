"""版本归档与运行目录同步脚本。

用于：
1. 根据当前独立项目目录打包干净的插件发行 ZIP（排除 .git / __pycache__ / config.toml / data / logs / scripts）；
2. 生成对应版本的归档目录 ``../archives/ai-quiet-hours/v<version>/``（含 ZIP、解压快照 ``src/``、``METADATA.json`` 与 ``SHA256SUMS.txt``）以便溯源；
3. 更新 ``../archives/ai-quiet-hours/VERSIONS.md`` 版本台账；
4. 可选使用 ``--sync-deploy <maibot_plugins_dir>`` 将干净运行时副本与 ``ai_quiet_hours.zip`` 同步到 MaiBot 插件目录。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import zipfile

EXCLUDE_TOP_LEVEL_NAMES = {".git", ".pytest_cache", "__pycache__", "data", "logs", "scripts"}
EXCLUDE_DIR_NAMES = {"__pycache__", ".git", ".pytest_cache", "data", "logs"}
EXCLUDE_FILE_NAMES = {"config.toml", ".DS_Store", "Thumbs.db"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".pyd"}


def _remove_tree_force(path: Path) -> None:
    def _on_error(func, target_path, _exc) -> None:
        os.chmod(target_path, stat.S_IWRITE)
        func(target_path)

    shutil.rmtree(path, onexc=_on_error)


def collect_release_files(project_root: Path) -> List[Path]:
    files: List[Path] = []
    for path in sorted(project_root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(project_root)
        parts = rel.parts
        if parts[0] in EXCLUDE_TOP_LEVEL_NAMES:
            continue
        if any(part in EXCLUDE_DIR_NAMES or part.startswith(".") for part in parts[:-1]):
            continue
        if path.name in EXCLUDE_FILE_NAMES or path.suffix.lower() in EXCLUDE_SUFFIXES:
            continue
        files.append(path)
    return files


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_archive(
    project_root: Path,
    archives_base: Path,
    *,
    sync_deploy_plugins_dir: Path | None = None,
) -> Dict[str, str]:
    manifest = json.loads((project_root / "_manifest.json").read_text(encoding="utf-8"))
    plugin_id = str(manifest["id"])
    version = str(manifest["version"])
    tag = f"v{version}"

    files = collect_release_files(project_root)
    version_dir = archives_base / tag
    src_dir = version_dir / "src"
    if src_dir.exists():
        _remove_tree_force(src_dir)
    src_dir.mkdir(parents=True, exist_ok=True)

    zip_path = version_dir / f"ai_quiet_hours-{tag}.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for file_path in files:
            rel = file_path.relative_to(project_root)
            rel_posix = rel.as_posix()
            zf.write(file_path, arcname=rel_posix)
            dest = src_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file_path, dest)

    try:
        commit_hash = subprocess.check_output(
            ["git", "-C", str(project_root), "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        dirty = bool(
            subprocess.check_output(
                ["git", "-C", str(project_root), "status", "--porcelain"],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
        )
    except Exception:
        commit_hash = "unknown"
        dirty = True

    metadata = {
        "plugin_id": plugin_id,
        "version": version,
        "tag": tag,
        "commit": commit_hash,
        "working_tree_dirty": dirty,
        "archived_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "archive_zip": zip_path.name,
        "zip_sha256": sha256_bytes(zip_path.read_bytes()),
        "files": [f.relative_to(project_root).as_posix() for f in files],
    }
    (version_dir / "METADATA.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    checksums: List[str] = []
    for p in sorted(version_dir.rglob("*")):
        if p.is_file() and p.name != "SHA256SUMS.txt":
            rel_posix = p.relative_to(version_dir).as_posix()
            checksums.append(f"{sha256_bytes(p.read_bytes())}  {rel_posix}")
    (version_dir / "SHA256SUMS.txt").write_text("\n".join(checksums) + "\n", encoding="utf-8")

    # 同步部署副本到 MaiBot 插件目录（保留可能存在的用户 config.toml / data / logs）
    if sync_deploy_plugins_dir is not None:
        deploy_dir = sync_deploy_plugins_dir / "ai_quiet_hours"
        preserved_config = None
        if (deploy_dir / "config.toml").is_file():
            preserved_config = (deploy_dir / "config.toml").read_bytes()
        preserved_data = deploy_dir / "data"
        tmp_data = None
        if preserved_data.is_dir():
            tmp_data = sync_deploy_plugins_dir / f".ai_quiet_hours_data_backup_{version}"
            if tmp_data.exists():
                _remove_tree_force(tmp_data)
            shutil.copytree(preserved_data, tmp_data)

        if deploy_dir.exists():
            _remove_tree_force(deploy_dir)
        shutil.copytree(src_dir, deploy_dir)
        if preserved_config is not None:
            (deploy_dir / "config.toml").write_bytes(preserved_config)
        if tmp_data is not None and tmp_data.is_dir():
            shutil.copytree(tmp_data, deploy_dir / "data", dirs_exist_ok=True)
            _remove_tree_force(tmp_data)
        shutil.copy2(zip_path, sync_deploy_plugins_dir / "ai_quiet_hours.zip")

    _write_versions_index(archives_base)
    return {
        "version": version,
        "zip_path": str(zip_path),
        "src_dir": str(src_dir),
        "zip_sha256": str(metadata["zip_sha256"]),
    }


def _write_versions_index(archives_base: Path) -> None:
    lines = [
        "# AI Quiet Hours 版本归档台账",
        "",
        "| 版本 | Tag | Git Commit | 文件数 | 发行包 ZIP | ZIP SHA-256 | 归档时间 (UTC) |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    meta_files = sorted(archives_base.glob("v*/METADATA.json"))
    for meta_path in meta_files:
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        commit_short = str(meta.get("commit", ""))[:10] + ("*" if meta.get("working_tree_dirty") else "")
        zip_sha_short = str(meta.get("zip_sha256", ""))[:16]
        lines.append(
            f"| {meta.get('version', '')} | {meta.get('tag', '')} | `{commit_short}` | "
            f"{len(meta.get('files', []))} | `{meta.get('archive_zip', '')}` | `{zip_sha_short}...` | "
            f"{meta.get('archived_at', '')} |"
        )
    (archives_base / "VERSIONS.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="归档 AI Quiet Hours 版本并可选同步到 MaiBot 插件目录")
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="独立插件项目目录",
    )
    parser.add_argument(
        "--archives-dir",
        type=Path,
        default=None,
        help="归档目录（默认为 <workspace>/archives/ai-quiet-hours）",
    )
    parser.add_argument(
        "--sync-deploy",
        type=Path,
        default=None,
        help="可选：将干净发行副本与 ai_quiet_hours.zip 同步到目标 plugins 目录",
    )
    args = parser.parse_args()
    project_root = args.project_root.resolve()
    archives_dir = args.archives_dir.resolve() if args.archives_dir else (project_root.parent / "archives" / "ai-quiet-hours")
    sync_dir = args.sync_deploy.resolve() if args.sync_deploy else None
    result = build_archive(project_root, archives_dir, sync_deploy_plugins_dir=sync_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
