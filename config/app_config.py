from pathlib import Path
from typing import Dict, List
import os

# hálózati gyökér (szerkeszthető) 
NETWORK_ROOT = Path(os.environ.get(
    "MEH_DATA_ROOT",
    r"M:\home\IT apps\MEH_Programok\MEH_Engedelynyilvantarto\#adatbazis",
))

APP_NAME = "MEH - Engedely Nyilvantarto Rendszer"
DB_FILENAME = "engedelyek.db"
LOGO_FILENAME = "logo.png"
FAVICON_FILENAME = "logo.ico"

CATEGORIES = [
    "Fémkereskedelmi engedély",
    "Hulladékgazdálkodási engedély",
    "Koncesszori engedély",
    "Környezetvédelmi engedély",
    "Mérleg hitelesítés",
    "Kereskedelmi működési engedély",
    "Telepengedély",
    "Vízjogi üzemeltetési engedély",
    "Vízjogi létesítési engedély",
    "Zajkibocsátási engedély",
    "Egyéb engedély",
    "Hulladéktárolóhely szabályzat",
    "Üzemi gyűjtőhely szabályzat",
]

ADJACENT_PAIR = ("Fémkereskedelmi engedély", "Koncesszori engedély")


def default_dirs(app_root: Path) -> Dict[str, Path]:
    app_root = Path(app_root)
    return {
        "root": app_root,
        "backup": app_root / "backup",
        "attachments": app_root / "attachment",
        "db": app_root / "db",
    }


def get_ordered_categories() -> List[str]:
    base = list(dict.fromkeys(CATEGORIES))
    a, b = ADJACENT_PAIR
    others = [c for c in base if c not in (a, b)]
    others_sorted = sorted(others, key=lambda s: (s or "").casefold())
    pair_present = [p for p in (a, b) if p in base]
    return pair_present + others_sorted