from pathlib import Path
from typing import List, Optional

def perform_backup(db) -> Optional[Path]:
    return db.make_backup(reason="manual")

def available_backups(db, limit: int = 100) -> List[Path]:
    return db.list_backups(limit=limit)

def restore_from_backup(db, backup_path: Path) -> bool:
    return db.restore_backup(Path(backup_path))