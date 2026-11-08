from __future__ import annotations

import os
import shutil
import time
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

import re

from config import settings

_FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9_\-\.]")


def validate_attachment(src: Path, current_total: int = 0) -> None:
    if not src.is_file():
        raise FileNotFoundError(str(src))
    if src.suffix.lower() in settings.FORBIDDEN_EXTENSIONS:
        raise ValueError("Tiltott fájltípus.")
    size = src.stat().st_size
    if size > settings.MAX_ATTACHMENT_SIZE:
        raise ValueError(f"A fájl mérete meghaladja a {settings.MAX_ATTACHMENT_SIZE // (1024 * 1024)} MB-os korlátot.")
    if current_total + size > settings.MAX_TOTAL_ATTACHMENTS_PER_RECORD:
        raise ValueError("A csatolmányok összmérete meghaladná a megengedett korlátot.")


def sanitize_filename(name: str, max_length: int = 180) -> str:
    if not name:
        return ""
    base = os.path.basename(name)
    safe = _FILENAME_SAFE_RE.sub("_", base)
    if len(safe) > max_length:
        name_part, ext = os.path.splitext(safe)
        safe = name_part[: max_length - len(ext)] + ext
    return safe or f"file_{int(datetime.utcnow().timestamp())}"


def unique_sheet_name(name: str, used_names: set[str]) -> str:
    """Reserve an Excel-compatible sheet name without case/truncation collisions."""
    base = re.sub(r"[\x00-\x1f\\/*?:\[\]]", "_", name).strip("'")[:31].rstrip("'") or "Sheet"
    existing = {value.casefold() for value in used_names}
    candidate = base
    index = 2
    while candidate.casefold() in existing:
        suffix = f" ({index})"
        candidate = base[:31 - len(suffix)] + suffix
        index += 1
    used_names.add(candidate)
    return candidate


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
    with tempfile.NamedTemporaryFile(dir=dst_dir, prefix=".copy_", delete=False) as stream:
        temporary = Path(stream.name)
    reserved = False
    try:
        shutil.copy2(src, temporary)
        if not overwrite:
            stem, ext = os.path.splitext(safe_name)
            counter = 0
            while True:
                try:
                    # Reserve the name exclusively, including across concurrent processes.
                    with target.open("xb"):
                        pass
                    reserved = True
                    break
                except FileExistsError:
                    counter += 1
                    target = dst_dir / f"{stem}_{counter}{ext}"
        os.replace(temporary, target)
        reserved = False
        return target
    finally:
        if reserved:
            target.unlink(missing_ok=True)
        temporary.unlink(missing_ok=True)