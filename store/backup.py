"""备份数据库与制品目录。

用 SQLite 的在线备份 API 复制数据库（WAL 下也能拿到一致快照），再把
`blobs/` 整个拷过去。备份目录里留一份 `backup.json`，写明来源、时间与
两份产物的相对路径，恢复时按它核对。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import time
from pathlib import Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _checksums(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): _sha256(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def backup(db_path: Path, blob_dir: Path, dest: Path) -> dict:
    """把 db 与 blobs 备份到 dest，返回 backup.json 的内容。

    目标目录必须不存在或为空，避免把两次备份混在一起。
    """
    db_path = Path(db_path)
    blob_dir = Path(blob_dir)
    dest = Path(dest)
    if not db_path.is_file():
        raise FileNotFoundError(f"数据库不存在：{db_path}")
    if dest.exists() and any(dest.iterdir()):
        raise FileExistsError(f"备份目录不是空的：{dest}")
    dest.mkdir(parents=True, exist_ok=True)

    target_db = dest / "qilin.db"
    source = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        target = sqlite3.connect(target_db)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()

    if blob_dir.is_dir():
        shutil.copytree(blob_dir, dest / "blobs", dirs_exist_ok=True)

    manifest = {
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "source_db": str(db_path),
        "source_blobs": str(blob_dir),
        "database": "qilin.db",
        "blobs": "blobs",
        "restore": "停掉 store serve，把 qilin.db 与 blobs/ 放回 --db、--blobs 指向的位置。",
    }
    manifest["checksums"] = _checksums(dest)
    (dest / "backup.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def restore(backup_dir: Path, db_path: Path, blob_dir: Path, *, force: bool = False) -> dict:
    """把备份放回目标位置。

    先核对 `backup.json` 里的所有校验和，再写目标；任何一项对不上就整体拒绝。
    目标已存在时默认拒绝，避免覆盖正在用的库；`force=True` 才覆盖。
    """
    backup_dir = Path(backup_dir)
    db_path = Path(db_path)
    blob_dir = Path(blob_dir)
    manifest_path = backup_dir / "backup.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"不是备份目录：{backup_dir}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    checksums = manifest.get("checksums") or {}
    if not checksums:
        raise ValueError("备份缺少校验和")
    for relative, expected in checksums.items():
        source = backup_dir / relative
        if not source.is_file():
            raise FileNotFoundError(f"备份文件缺失：{relative}")
        if _sha256(source) != expected:
            raise ValueError(f"校验和不符：{relative}")
    if db_path.exists() and not force:
        raise FileExistsError(f"目标数据库已存在：{db_path}")
    if blob_dir.exists() and any(blob_dir.iterdir()) and not force:
        raise FileExistsError(f"目标制品目录不是空的：{blob_dir}")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(backup_dir / manifest["database"], db_path)
    source_blobs = backup_dir / manifest["blobs"]
    if source_blobs.is_dir():
        shutil.copytree(source_blobs, blob_dir, dirs_exist_ok=True)
    return manifest
