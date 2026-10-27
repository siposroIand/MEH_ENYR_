import errno
import importlib.util
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from config.app_config import DB_FILENAME, default_dirs
from config.paths import build_app_dirs, get_app_root
from core.db import Database


class DirectoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "missing" / "parents" / "#adatbazis"

    def test_missing_nested_root_and_children_created_before_database(self):
        dirs = build_app_dirs(self.root)
        self.assertEqual(dirs, default_dirs(self.root))
        for directory in dirs.values():
            self.assertTrue(directory.is_dir())
        self.assertEqual(list(self.root.glob(".writetest_*")), [])
        db = Database(dirs["db"] / DB_FILENAME, dirs["backup"])
        with db.get_connection() as conn:
            self.assertIsNotNone(conn.execute(
                "SELECT name FROM sqlite_master WHERE name='telephelyek'"
            ).fetchone())

    def test_existing_data_preserved_on_repeated_initialization(self):
        dirs = build_app_dirs(self.root)
        attachment = dirs["attachments"] / "existing.txt"
        attachment.write_text("megőrzendő", encoding="utf-8")
        db = Database(dirs["db"] / DB_FILENAME, dirs["backup"])
        with db.get_connection() as conn:
            conn.execute("INSERT INTO telephelyek (id, nev) VALUES ('1', 'Meglévő')")
        for _ in range(2):
            self.assertEqual(build_app_dirs(self.root), dirs)
            db = Database(dirs["db"] / DB_FILENAME, dirs["backup"])
            with db.get_connection() as conn:
                self.assertEqual(conn.execute(
                    "SELECT nev FROM telephelyek WHERE id='1'"
                ).fetchone()[0], "Meglévő")
            self.assertEqual(attachment.read_text(encoding="utf-8"), "megőrzendő")

    def test_concurrent_write_probes_are_unique_and_cleaned(self):
        original = tempfile.NamedTemporaryFile
        names = []

        def record_probe(*args, **kwargs):
            probe = original(*args, **kwargs)
            names.append(probe.name)
            return probe

        with patch("config.paths.tempfile.NamedTemporaryFile", side_effect=record_probe):
            with ThreadPoolExecutor(max_workers=4) as executor:
                roots = list(executor.map(get_app_root, [self.root] * 8))
        self.assertEqual(roots, [self.root] * 8)
        self.assertEqual(len(set(names)), 8)
        self.assertTrue(all(not Path(name).exists() for name in names))

    def test_root_mkdir_failure_never_falls_back(self):
        for error in (
            PermissionError(errno.EACCES, "Permission denied", str(self.root)),
            OSError(errno.ENODEV, "No such device", str(self.root)),
        ):
            with self.subTest(error=error):
                with patch.object(Path, "mkdir", side_effect=error):
                    with self.assertRaises(RuntimeError) as caught:
                        get_app_root(self.root)
                self.assertIn(str(self.root), str(caught.exception))
                self.assertIn(str(error), str(caught.exception))
                self.assertIs(caught.exception.__cause__, error)

    def test_write_probe_failure_reports_path_and_os_error(self):
        error = PermissionError(errno.EACCES, "Permission denied")
        with patch("config.paths.tempfile.NamedTemporaryFile", side_effect=error):
            with self.assertRaises(RuntimeError) as caught:
                build_app_dirs(self.root)
        self.assertIn(str(self.root), str(caught.exception))
        self.assertIn(str(error), str(caught.exception))
        self.assertIs(caught.exception.__cause__, error)

    def test_failed_probe_write_is_cleaned(self):
        original = tempfile.NamedTemporaryFile
        error = OSError(errno.ENOSPC, "No space left on device")

        def failing_probe(*args, **kwargs):
            probe = original(*args, **kwargs)
            probe.write = Mock(side_effect=error)
            return probe

        with patch("config.paths.tempfile.NamedTemporaryFile", side_effect=failing_probe):
            with self.assertRaises(RuntimeError) as caught:
                build_app_dirs(self.root)
        self.assertIs(caught.exception.__cause__, error)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_file_in_place_of_root_or_child_is_not_overwritten(self):
        for name in ("root", "backup", "attachments", "db"):
            with self.subTest(directory=name):
                root = self.root / name
                blocked = default_dirs(root)[name]
                blocked.parent.mkdir(parents=True, exist_ok=True)
                blocked.write_text("unchanged", encoding="utf-8")
                with self.assertRaises(RuntimeError) as caught:
                    build_app_dirs(root)
                self.assertIn(str(blocked), str(caught.exception))
                self.assertIsInstance(caught.exception.__cause__, FileExistsError)
                self.assertIn(str(caught.exception.__cause__), str(caught.exception))
                self.assertEqual(blocked.read_text(encoding="utf-8"), "unchanged")

    def test_child_permission_failure_is_not_swallowed(self):
        blocked = self.root / "attachment"
        error = PermissionError(errno.EACCES, "Permission denied", str(blocked))
        original = Path.mkdir

        def mkdir(path, *args, **kwargs):
            if path == blocked:
                raise error
            return original(path, *args, **kwargs)

        with patch.object(Path, "mkdir", mkdir):
            with self.assertRaises(RuntimeError) as caught:
                build_app_dirs(self.root)
        self.assertIn(str(blocked), str(caught.exception))
        self.assertIn(str(error), str(caught.exception))
        self.assertIs(caught.exception.__cause__, error)

    def test_direct_database_initialization_reports_directory_failure(self):
        blocked = self.root / "backup"
        blocked.parent.mkdir(parents=True)
        blocked.write_text("unchanged", encoding="utf-8")
        with patch.object(Database, "init_db") as init_db:
            with self.assertRaises(RuntimeError) as caught:
                Database(self.root / "db" / DB_FILENAME, blocked)
        init_db.assert_not_called()
        self.assertIn(str(blocked), str(caught.exception))
        self.assertIsInstance(caught.exception.__cause__, FileExistsError)


class StartupTests(unittest.TestCase):
    def test_directory_and_database_errors_reach_dialog(self):
        modules = {
            "PySide6": Mock(),
            "PySide6.QtWidgets": Mock(),
            "PySide6.QtGui": Mock(),
            "gui.app_ui": Mock(),
        }
        source = Path(__file__).resolve().parents[1] / "main.py"
        with patch.dict(sys.modules, modules):
            spec = importlib.util.spec_from_file_location("startup_under_test", source)
            main = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(main)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "configured" / "nested"
            for stage in ("directories", "database"):
                with self.subTest(stage=stage):
                    blocked = root if stage == "directories" else root / "backup"
                    blocked.parent.mkdir(parents=True, exist_ok=True)
                    blocked.write_text("unchanged", encoding="utf-8")
                    with self.assertRaises(OSError) as os_error:
                        blocked.mkdir(parents=True, exist_ok=True)
                    with patch.object(main, "_setup_logging"), \
                            patch.object(main, "NETWORK_ROOT", root), \
                            patch.object(main.QMessageBox, "critical") as critical, \
                            patch.object(main, "Database", wraps=Database) as database:
                        if stage == "database":
                            dirs = default_dirs(root)
                            with patch.object(main, "build_app_dirs", return_value=dirs):
                                result = main.run_app()
                            database.assert_called_once()
                        else:
                            result = main.run_app()
                            database.assert_not_called()
                    self.assertEqual(result, 1)
                    message = critical.call_args.args[2]
                    self.assertIn(str(blocked), message)
                    self.assertIn("Részlet:", message)
                    self.assertIn("Könyvtár" if stage == "database" else "Hálózati gyökér", message)
                    self.assertIn(str(os_error.exception), message)
                    blocked.unlink()


if __name__ == "__main__":
    unittest.main()
