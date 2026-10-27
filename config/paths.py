from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Dict

from .app_config import APP_NAME, default_dirs


def get_resource_path(relative_path: str) -> Path:
    """
    PyInstaller-barát erőforrás feloldás.
    """
    candidates: list[Path] = []

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / relative_path)

    if getattr(sys, "frozen", False):
        try:
            exe_dir = Path(sys.executable).resolve().parent
            candidates.append(exe_dir / relative_path)
        except Exception:
            pass

    try:
        pkg_root = Path(__file__).resolve().parents[1]
        candidates.append(pkg_root / relative_path)
    except Exception:
        pass

    candidates.append(Path.cwd() / relative_path)

    for p in candidates:
        if p.exists():
            return p
    return candidates[0]


def get_app_root(network_root: Path | None = None) -> Path:
    if network_root:
        try:
            candidate = Path(network_root).expanduser().resolve()
            candidate.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", prefix=".writetest_", dir=candidate
            ) as test_file:
                test_file.write("ok")
            return candidate
        except Exception as e:
            raise RuntimeError(
                f"Hálózati gyökér nem hozható létre vagy nem írható: {network_root}. "
                f"Részlet: {e}"
            ) from e

    if getattr(sys, "frozen", False):
        root = Path(sys.executable).resolve().parent
    else:
        root = Path(__file__).resolve().parents[1]

    if os.access(root, os.W_OK):
        return root

    appdata = os.getenv("APPDATA") or str(Path.home())
    return Path(appdata) / APP_NAME


def ensure_dirs(dirs: Dict[str, Path]) -> None:
    for p in dirs.values():
        try:
            Path(p).mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise RuntimeError(
                f"Könyvtár nem hozható létre: {p}. Részlet: {e}"
            ) from e


def build_app_dirs(network_root: Path | None) -> Dict[str, Path]:
    root = get_app_root(network_root)
    dirs = default_dirs(root)
    ensure_dirs(dirs)
    return dirs