"""Safe archive extraction helpers for Pi update packages."""

from __future__ import annotations

import os
import shutil
import stat
import tarfile
import zipfile


class UnsafeArchiveError(ValueError):
    """Raised when an update archive contains unsafe paths or members."""


def _real(path: str) -> str:
    return os.path.realpath(os.path.abspath(path))


def path_is_under(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([_real(path), _real(root)]) == _real(root)
    except ValueError:
        return False


def _target_for_member(root: str, name: str) -> str:
    if not name:
        raise UnsafeArchiveError("archive member without a name")
    target = os.path.join(root, name)
    if not path_is_under(target, root):
        raise UnsafeArchiveError(f"archive member escapes extraction dir: {name}")
    return target


def _safe_mode(mode: int, executable_default: bool = False) -> int:
    if not mode:
        return 0o755 if executable_default else 0o644
    return 0o755 if mode & 0o111 else 0o644


def safe_extract_tar(archive_path: str, extract_dir: str) -> None:
    with tarfile.open(archive_path, "r:*") as tar:
        for member in tar.getmembers():
            target = _target_for_member(extract_dir, member.name)
            if member.isdir():
                os.makedirs(target, exist_ok=True)
                os.chmod(target, 0o755)
                continue
            if not member.isfile():
                raise UnsafeArchiveError(f"unsupported tar member: {member.name}")

            src = tar.extractfile(member)
            if src is None:
                raise UnsafeArchiveError(f"unreadable tar member: {member.name}")
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
            os.chmod(target, _safe_mode(member.mode))


def safe_extract_zip(archive_path: str, extract_dir: str) -> None:
    with zipfile.ZipFile(archive_path) as zf:
        for info in zf.infolist():
            target = _target_for_member(extract_dir, info.filename)
            mode = info.external_attr >> 16
            mode_type = stat.S_IFMT(mode)
            if stat.S_ISLNK(mode) or (
                mode_type and not stat.S_ISREG(mode) and not stat.S_ISDIR(mode)
            ):
                raise UnsafeArchiveError(f"unsupported zip member: {info.filename}")

            if info.is_dir():
                os.makedirs(target, exist_ok=True)
                os.chmod(target, 0o755)
                continue

            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
            os.chmod(target, _safe_mode(mode))


def safe_extract_archive(
    archive_path: str, original_filename: str, extract_dir: str,
) -> None:
    lower = (original_filename or "").lower()
    if lower.endswith((".tar.gz", ".tgz")):
        safe_extract_tar(archive_path, extract_dir)
    elif lower.endswith(".zip"):
        safe_extract_zip(archive_path, extract_dir)
    else:
        raise UnsafeArchiveError(f"unsupported archive format: {original_filename}")


def select_source_root(extract_dir: str) -> str:
    entries = os.listdir(extract_dir)
    if len(entries) == 1:
        candidate = os.path.join(extract_dir, entries[0])
        if os.path.isdir(candidate) and not os.path.islink(candidate):
            if not path_is_under(candidate, extract_dir):
                raise UnsafeArchiveError("extracted source root escapes extraction dir")
            return candidate
    return extract_dir


def _validate_no_symlinks(root: str) -> None:
    for current, dirs, files in os.walk(root):
        if not path_is_under(current, root):
            raise UnsafeArchiveError(f"source path escapes source dir: {current}")
        for name in list(dirs) + list(files):
            path = os.path.join(current, name)
            if os.path.islink(path):
                raise UnsafeArchiveError(f"symlink not allowed in update package: {path}")
            if not path_is_under(path, root):
                raise UnsafeArchiveError(f"source path escapes source dir: {path}")


def copy_update_items(
    src_dir: str,
    code_dir: str,
    preserve_items: set[str],
    normalize_permissions=None,
    logger=None,
) -> None:
    src_root = _real(src_dir)
    dst_root = _real(code_dir)
    _validate_no_symlinks(src_root)

    for item in os.listdir(src_root):
        if item in preserve_items or item.startswith(".upgrade_backup_"):
            if logger:
                logger.info("Preserving runtime item during update: %s", item)
            continue

        src = os.path.join(src_root, item)
        dst = os.path.join(dst_root, item)
        if not path_is_under(src, src_root) or not path_is_under(dst, dst_root):
            raise UnsafeArchiveError(f"update target escapes code dir: {item}")
        if os.path.lexists(dst) and os.path.islink(dst):
            raise UnsafeArchiveError(f"refusing to replace symlink target: {item}")

        if os.path.isdir(src):
            if os.path.exists(dst):
                shutil.rmtree(dst)
            shutil.copytree(src, dst, symlinks=False)
        else:
            shutil.copy2(src, dst)

        if normalize_permissions:
            normalize_permissions(dst)
