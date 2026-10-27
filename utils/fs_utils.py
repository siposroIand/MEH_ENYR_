from __future__ import annotations

import os
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import re

_FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9_\-\.]")


def sanitize_filename(name: str, max_length: int = 180) -> str:
    if not name:
        return ""
    base = os.path.basename(name)
    safe = _FILENAME_SAFE_RE.sub("_", base)
    if len(safe) > max_length:
        name_part, ext = os.path.splitext(safe)
        safe = name_part[: max_length - len(ext)] + ext
    return safe or f"file_{int(datetime.utcnow().timestamp())}"


def atomic_replace(src: Path, dst: Path) -> None:
    src = Path(src)
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.replace(str(src), str(dst))


def create_lockfile(lock_path: Path, timeout_sec: int = 30) -> int:
    start = time.time()
    lock_path = Path(lock_path)
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, f"{os.getpid()} {datetime.utcnow().isoformat()}".encode("utf-8"))
            except Exception:
                pass
            return fd
        except FileExistsError:
            try:
                mtime = lock_path.stat().st_mtime
                if (time.time() - mtime) > 3600:
                    lock_path.unlink(missing_ok=True)
            except Exception:
                pass
            if (time.time() - start) > timeout_sec:
                raise RuntimeError(f"Nem sikerült lock-ot szerezni: {lock_path}")
            time.sleep(0.2)
        except Exception:
            raise


def release_lockfile(fd: int, lock_path: Path) -> None:
    try:
        os.close(fd)
    except Exception:
        pass
    try:
        Path(lock_path).unlink(missing_ok=True)
    except Exception:
        pass


def safe_copy_to_dir(src: Path, dst_dir: Path, name_hint: Optional[str] = None, overwrite: bool = False) -> Path:
    src = Path(src)
    dst_dir = Path(dst_dir)
    if not src.exists() or not src.is_file():
        raise FileNotFoundError(str(src))
    dst_dir.mkdir(parents=True, exist_ok=True)

    candidate_name = name_hint or src.name
    safe_name = sanitize_filename(candidate_name)
    target = dst_dir / safe_name

    if target.exists() and not overwrite:
        stem, ext = os.path.splitext(safe_name)
        counter = 1
        while True:
            new_name = f"{stem}_{counter}{ext}"
            target = dst_dir / new_name
            if not target.exists():
                break
            counter += 1

    shutil.copy2(str(src), str(target))
    return target