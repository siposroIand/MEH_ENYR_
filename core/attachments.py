"""Staged attachments.

The database, attachment root and their parents must be writable only by trusted
OS users. Application roles are not a sandbox against direct filesystem access.
Symlinks are rejected; concurrent hostile filesystem changes are outside that
trusted-directory boundary.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
import uuid

from core.auth import ValidationError


MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
ALLOWED_EXTENSIONS = frozenset({
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".csv", ".txt",
    ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".odt", ".ods",
})


def _component(value):
    if not isinstance(value, str) or not value or value in {".", ".."}:
        raise ValidationError("Érvénytelen fájlnév vagy azonosító.")
    if (len(value) > 240 or any(c in value for c in "/\\:")
            or any(ord(c) < 32 for c in value) or value != value.strip()
            or value.endswith(".") or value.startswith(".")):
        raise ValidationError("Érvénytelen fájlnév vagy azonosító.")
    reserved = {"CON", "PRN", "AUX", "NUL"} | {f"{p}{i}" for p in ("COM", "LPT") for i in range(1, 10)}
    if value.split(".")[0].upper() in reserved:
        raise ValidationError("Fenntartott fájlnév.")
    return value


def validate_filename(name):
    name = _component(name)
    if Path(name).suffix.lower() not in ALLOWED_EXTENSIONS:
        raise ValidationError("Nem támogatott csatolmánytípus.")
    return name


def _no_symlinks(path):
    path = Path(path).absolute()
    for candidate in (*reversed(path.parents), path):
        if candidate.is_symlink():
            raise ValidationError("Szimbolikus hivatkozás nem használható.")
    return path


def safe_attachment_path(root, permit_id, filename=None):
    root = _no_symlinks(root)
    path = root / _component(str(permit_id))
    if filename is not None:
        path /= validate_filename(filename)
    return _no_symlinks(path)


class AttachmentPlan:
    """Dialog-local plan: add/delete never modify existing attachments."""

    def __init__(self, root, permit_id=None, *, read_guard=None, write_guard=None):
        self.root = _no_symlinks(root)
        self.permit_id = _component(str(permit_id)) if permit_id is not None else None
        self.root.mkdir(parents=True, exist_ok=True)
        self._staging_root = _no_symlinks(self.root / ".staging")
        self._staging_root.mkdir(exist_ok=True)
        self._stage = self._staging_root / uuid.uuid4().hex
        self._stage.mkdir(mode=0o700)
        self._adds = {}
        self._deletes = set()
        self._published = []
        self._moved = []
        self._closed = False
        self._prepared = False
        self._read_guard = read_guard
        self._write_guard = write_guard

    @property
    def has_changes(self):
        return bool(self._adds or self._deletes)

    def _open(self, *, write=False, guard=True):
        if self._closed or self._prepared:
            raise ValidationError("A csatolmányterv már lezárult.")
        if guard:
            callback = self._write_guard if write else self._read_guard
            if callback is not None:
                callback()
        _no_symlinks(self._stage)

    def list_names(self):
        self._open()
        names = set()
        if self.permit_id:
            directory = safe_attachment_path(self.root, self.permit_id)
            if directory.exists():
                for path in directory.iterdir():
                    if path.is_symlink():
                        continue
                    if path.is_file():
                        try:
                            names.add(validate_filename(path.name))
                        except ValidationError:
                            continue
        return sorted((names - self._deletes) | set(self._adds), key=str.casefold)

    def add(self, source, filename=None):
        self._open(write=True)
        source = _no_symlinks(source)
        validate_filename(source.name)
        name = validate_filename(filename if filename is not None else source.name)
        existing = {n.casefold() for n in self.list_names()}
        if name.casefold() in existing:
            raise ValidationError("Ilyen nevű csatolmány már létezik.")
        destination = self._stage / (uuid.uuid4().hex + Path(name).suffix)
        fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_ATTACHMENT_BYTES:
                raise ValidationError("Csak legfeljebb 20 MiB méretű normál fájl csatolható.")
            total = 0
            with os.fdopen(fd, "rb", closefd=False) as src, destination.open("xb") as dst:
                while True:
                    chunk = src.read(65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_ATTACHMENT_BYTES:
                        raise ValidationError("A csatolmány meghaladja a 20 MiB korlátot.")
                    dst.write(chunk)
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
        finally:
            os.close(fd)
        self._adds[name] = destination
        return name

    stage_add = add

    def delete(self, filename):
        self._open(write=True)
        name = validate_filename(filename)
        if name in self._adds:
            self._adds.pop(name).unlink()
        elif name not in self.list_names():
            raise ValidationError("A csatolmány nem található.")
        else:
            self._deletes.add(name)

    stage_delete = delete

    def path_for(self, filename):
        self._open()
        name = validate_filename(filename)
        if name not in self.list_names():
            raise ValidationError("A csatolmány nem található.")
        return self._adds.get(name) or safe_attachment_path(self.root, self.permit_id, name)

    def prepare(self, permit_id):
        self._open(guard=False)
        permit_id = _component(str(permit_id))
        if self.permit_id is not None and permit_id != self.permit_id:
            raise ValidationError("A csatolmányterv másik engedélyhez tartozik.")
        directory = safe_attachment_path(self.root, permit_id)
        directory.mkdir(exist_ok=True)
        self._prepared = True
        try:
            for name in sorted(self._deletes):
                original = safe_attachment_path(self.root, permit_id, name)
                if not original.is_file():
                    raise ValidationError("A csatolmány időközben megváltozott.")
                saved = self._stage / uuid.uuid4().hex
                original.rename(saved)
                self._moved.append((original, saved))
            names = {p.name.casefold() for p in directory.iterdir()}
            for name, staged in self._adds.items():
                if name.casefold() in names:
                    raise ValidationError("A csatolmány időközben létrejött.")
                destination = safe_attachment_path(self.root, permit_id, name)
                _no_symlinks(staged)
                os.link(staged, destination)
                self._published.append(destination)
                names.add(name.casefold())
        except BaseException:
            self.rollback()
            raise

    def rollback(self):
        for path in reversed(self._published):
            _no_symlinks(path).unlink(missing_ok=True)
        self._published.clear()
        for original, saved in reversed(self._moved):
            _no_symlinks(saved).rename(_no_symlinks(original))
        self._moved.clear()
        self._prepared = False

    def finalize(self):
        # Called only after DB commit. Failure to clean staging must not turn a
        # successful database write into an apparent failure.
        self._closed = True
        self._published.clear()
        self._moved.clear()
        try:
            _no_symlinks(self._stage)
            shutil.rmtree(self._stage)
        except OSError:
            pass

    def cancel(self):
        if self._closed:
            return
        if self._prepared:
            self.rollback()
        self.finalize()

    cleanup = cancel

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.cancel()
