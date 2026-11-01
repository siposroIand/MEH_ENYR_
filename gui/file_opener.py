from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices


def open_local_path(path: Path) -> None:
    """Use a local URL so spaces, accents and network paths remain intact."""
    resolved = path.resolve(strict=True)
    if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(resolved))):
        raise OSError(
            "Az operációs rendszer nem tudta megnyitni a fájlt vagy mappát. "
            "Ellenőrizze az alapértelmezett alkalmazást és a hozzáférési jogosultságot."
        )
