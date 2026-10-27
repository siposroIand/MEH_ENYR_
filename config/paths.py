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


def get_app_root(network_root: Path | None = None, raise_on_fail: bool = False) -> Path:
    if network_root:
        try:
            if not Path(network_root).expanduser().is_absolute():
                raise ValueError("Az adatkönyvtárnak abszolút útvonalnak kell lennie.")
            candidate = Path(network_root).expanduser().resolve()
            if not candidate.is_dir():
                raise FileNotFoundError("Az adatgyökér nem létezik; hozza létre az üzemeltető.")
            with tempfile.TemporaryFile(dir=candidate) as probe:
                probe.write(b"ok")
            return candidate
        except Exception:
            if raise_on_fail:
                raise RuntimeError(f"Hálózati gyökér nem elérhető/írható: {network_root}")

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
        Path(p).mkdir(parents=True, exist_ok=True)


def build_app_dirs(network_root: Path | None) -> Dict[str, Path]:
    root = get_app_root(network_root, raise_on_fail=True)
    dirs = default_dirs(root)
    ensure_dirs(dirs)
    return dirs