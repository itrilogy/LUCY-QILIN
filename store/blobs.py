"""制品落盘。校验失败的包不写进来。"""

from __future__ import annotations

import fnmatch
import hashlib
import zipfile
from pathlib import Path

from store.safety import PathEscape, safe_join


MAX_UNPACKED = 64 * 1024 * 1024

SYSTEM_IGNORE_NAMES = frozenset({
    ".DS_Store",
    "Thumbs.db",
    "desktop.ini",
    ".storeignore",
})

SYSTEM_IGNORE_DIRS = frozenset({
    "__MACOSX",
    ".git",
    ".svn",
    ".hg",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
})


def load_storeignore(root: Path) -> list[str]:
    """读取包根目录下的 .storeignore 忽略模式（可选）。"""
    ignore_file = root / ".storeignore"
    if not ignore_file.is_file():
        return []
    try:
        lines = ignore_file.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []
    patterns: list[str] = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        patterns.append(line.strip("/"))
    return patterns


def is_ignored_path(rel_posix: str, ignore_patterns: list[str] | None = None) -> bool:
    """判断相对路径是否属于 macOS/系统尾巴文件或 .storeignore 忽略项。"""
    clean = rel_posix.replace("\\", "/").strip("/")
    if not clean or clean == ".":
        return False
    parts = [part for part in clean.split("/") if part and part != "."]
    if not parts:
        return False
    for part in parts[:-1]:
        if part in SYSTEM_IGNORE_DIRS or part.startswith("._"):
            return True
    name = parts[-1]
    if name in SYSTEM_IGNORE_NAMES or name in SYSTEM_IGNORE_DIRS or name.startswith("._"):
        return True
    if ignore_patterns:
        for pattern in ignore_patterns:
            if fnmatch.fnmatch(clean, pattern) or fnmatch.fnmatch(name, pattern):
                return True
            for part in parts:
                if fnmatch.fnmatch(part, pattern):
                    return True
    return False


def write_zip(root: Path, dest: Path) -> str:
    dest.parent.mkdir(parents=True, exist_ok=True)
    root_resolved = root.resolve()
    dest_resolved = dest.resolve()
    ignore_patterns = load_storeignore(root_resolved)
    files = sorted(path for path in root_resolved.rglob("*") if path.is_file())
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            if path.resolve() == dest_resolved:
                continue
            rel_posix = path.relative_to(root_resolved).as_posix()
            if is_ignored_path(rel_posix, ignore_patterns):
                continue
            archive.write(path, rel_posix)
    return hashlib.sha256(dest.read_bytes()).hexdigest()


def safe_extract(archive: Path, dest: Path) -> list[str]:
    """把压缩包解到 dest。成员路径统一走 `store.safety.safe_join` 守卫。

    不合法时抛 `ValueError("压缩包路径不合法")`，调用方按既有约定处理。
    """
    dest.mkdir(parents=True, exist_ok=True)
    names: list[str] = []
    with zipfile.ZipFile(archive) as zipped:
        total = sum(info.file_size for info in zipped.infolist())
        if total > MAX_UNPACKED:
            raise ValueError("解压后过大")
        for info in zipped.infolist():
            filename = info.filename.replace("\\", "/")
            try:
                target = safe_join(dest, filename)
            except PathEscape as exc:
                raise ValueError("压缩包路径不合法") from exc
            if is_ignored_path(filename):
                continue
            names.append(info.filename)
            # 同一包里先出现文件 `x`、再出现 `x/y` 时，建父目录会抛 FileExistsError。
            # 这是包本身的问题，按「压缩包不合法」退回，不能让它冒成 500。
            try:
                if info.is_dir():
                    if target.is_file():
                        raise ValueError(f"压缩包路径冲突：{filename}")
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if target.is_dir():
                    raise ValueError(f"压缩包路径冲突：{filename}")
                target.parent.mkdir(parents=True, exist_ok=True)
            except (FileExistsError, NotADirectoryError) as exc:
                raise ValueError(f"压缩包路径冲突：{filename}") from exc
            with zipped.open(info) as src, target.open("wb") as dst:
                dst.write(src.read())
    return names


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
