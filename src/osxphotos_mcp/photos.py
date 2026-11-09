"""Read the Photos library through the osxphotos Python API and return small, plain dicts."""

from __future__ import annotations

import datetime as dt
import threading
import time
from pathlib import Path
from typing import Any, Literal

import osxphotos

MAX_LIMIT = 100
RELOAD_AFTER_SECONDS = 300
UNKNOWN_PERSON = "_UNKNOWN_"

_NO_ACCESS = (
    "Cannot read the Photos library. Grant Full Disk Access to the app that runs this server "
    "(System Settings > Privacy & Security > Full Disk Access), then restart it. Details: {error}"
)


class Library:
    """A Photos library, loaded on first use and reloaded when it has changed on disk.

    Photos touches its database several times a minute and loading a large library takes
    seconds, so a loaded copy is kept for at least `reload_after` seconds.
    """

    def __init__(
        self,
        path: str | None = None,
        *,
        include_hidden: bool = False,
        reload_after: float = RELOAD_AFTER_SECONDS,
    ):
        self.path = path
        self.include_hidden = include_hidden
        self.reload_after = reload_after
        self._db: osxphotos.PhotosDB | None = None
        self._stamp: float | None = None
        self._loaded_at = 0.0
        self._lock = threading.Lock()

    def db(self) -> osxphotos.PhotosDB:
        with self._lock:
            if self._db is None or self._is_outdated(self._db):
                try:
                    self._db = osxphotos.PhotosDB(dbfile=self.path)
                except FileNotFoundError:
                    raise
                except OSError as error:
                    raise RuntimeError(_NO_ACCESS.format(error=error)) from error
                self._stamp = _modified(self._db.db_path)
                self._loaded_at = time.monotonic()
            return self._db

    def refresh(self) -> None:
        """Reload the library the next time it is used, for example after changing it."""
        with self._lock:
            self._db = None

    def _is_outdated(self, db: osxphotos.PhotosDB) -> bool:
        old_enough = time.monotonic() - self._loaded_at >= self.reload_after
        return old_enough and self._stamp != _modified(db.db_path)

    def search(
        self,
        *,
        text: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
        album: str | None = None,
        person: str | None = None,
        keyword: str | None = None,
        place: str | None = None,
        favorite: bool | None = None,
        media_type: Literal["photo", "video"] | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        if offset < 0:
            raise ValueError("offset must not be negative")
        start, end = _parse_date(from_date), _parse_date(to_date, end=True)
        db = self.db()
        options = osxphotos.QueryOptions(
            person=[person] if person else None,
            keyword=[keyword] if keyword else None,
            place=[place] if place else None,
            favorite=True if favorite else None,
            not_favorite=True if favorite is False else None,
            photos=media_type != "video",
            movies=media_type != "photo",
            not_hidden=None if self.include_hidden else True,
            ignore_case=True,
        )
        photos = db.query(options)
        if album:
            members = _album_members(db, album)
            photos = [p for p in photos if p.uuid in members]
        if start:
            photos = [p for p in photos if _comparable(p.date, start) >= start]
        if end:
            photos = [p for p in photos if _comparable(p.date, end) < end]
        words = text.casefold().split() if text else []
        if words:
            photos = [p for p in photos if _contains_all(_searchable_text(p), words)]
        photos.sort(key=lambda p: p.date, reverse=True)
        shown = photos[offset : offset + min(limit, MAX_LIMIT)]
        return {"total": len(photos), "photos": [_summary(p) for p in shown]}

    def get_photo(self, uuid: str) -> dict[str, Any]:
        photo = self.db().get_photo(uuid)
        if photo is None:
            raise ValueError(f"No photo with UUID '{uuid}'.")
        return _details(photo)

    def list_albums(self) -> list[dict[str, Any]]:
        db = self.db()
        albums = [
            _compact(
                {
                    "title": album.title,
                    "folder": "/".join(album.folder_names),
                    "count": len(album.photos),
                    "shared": shared or None,
                }
            )
            for shared, infos in ((False, db.album_info), (True, db.album_info_shared))
            for album in infos
        ]
        return sorted(albums, key=lambda a: (a.get("folder", ""), a["title"]))

    def list_people_and_keywords(self) -> dict[str, dict[str, int]]:
        db = self.db()
        people = {k: v for k, v in db.persons_as_dict.items() if k != UNKNOWN_PERSON}
        return {"people": _used(people), "keywords": _used(db.keywords_as_dict)}

    def export(self, uuids: list[str], destination: str) -> dict[str, Any]:
        folder = Path(destination).expanduser()
        if not folder.is_absolute():
            raise ValueError("destination must be an absolute path (it may start with ~).")
        folder.mkdir(parents=True, exist_ok=True)
        db = self.db()
        exported: list[str] = []
        skipped: list[dict[str, str]] = []
        for uuid in uuids:
            photo = db.get_photo(uuid)
            if photo is None:
                skipped.append({"uuid": uuid, "reason": "not found"})
            elif not (photo.path_edited or photo.path):
                skipped.append({"uuid": uuid, "reason": "not downloaded from iCloud"})
            else:
                try:
                    exported += photo.export(str(folder), edited=bool(photo.path_edited))
                except Exception as error:  # osxphotos raises its own, unrelated error types
                    skipped.append({"uuid": uuid, "reason": f"failed: {error}"})
        return {"exported": exported, "skipped": skipped}


def _album_members(db: osxphotos.PhotosDB, name: str) -> set[str]:
    """UUIDs of the photos in every album with this title or folder path, ignoring case."""
    wanted = name.casefold()
    albums = [
        album
        for album in (*db.album_info, *db.album_info_shared)
        if wanted
        in (album.title.casefold(), "/".join([*album.folder_names, album.title]).casefold())
    ]
    if not albums:
        raise ValueError(f"No album named '{name}'. Call list_albums to see the album names.")
    return {photo.uuid for album in albums for photo in album.photos}


def _contains_all(text: str, words: list[str]) -> bool:
    return all(word in text for word in words)


def _modified(db_path: str) -> float:
    """Latest change to the database, including its write-ahead log."""
    files = (Path(db_path), Path(f"{db_path}-wal"))
    return max((f.stat().st_mtime for f in files if f.exists()), default=0.0)


def _parse_date(value: str | None, *, end: bool = False) -> dt.datetime | None:
    if not value:
        return None
    try:
        date = dt.datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(
            f"'{value}' is not an ISO date; use YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS."
        ) from None
    if end and len(value) == 10:  # a bare end date means "through the end of that day"
        date += dt.timedelta(days=1)
    return date


def _comparable(taken: dt.datetime, bound: dt.datetime) -> dt.datetime:
    """A bound without UTC offset means local time where the photo was taken, as Photos shows it."""
    return taken if bound.tzinfo else taken.replace(tzinfo=None)


def _people(photo: osxphotos.PhotoInfo) -> list[str]:
    return [name for name in photo.persons if name != UNKNOWN_PERSON]


def _searchable_text(photo: osxphotos.PhotoInfo) -> str:
    parts = [
        photo.title,
        photo.description,
        photo.original_filename,
        photo.place.name if photo.place else None,
        *_people(photo),
        *photo.keywords,
        *photo.albums,
        *photo.labels,
    ]
    return "\n".join(part for part in parts if part).casefold()


def _compact(data: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in data.items() if v is not None and v != "" and v != [] and v != {}}


def _used(counts: dict[str, int]) -> dict[str, int]:
    """Drop unused names and put the most used first."""
    return dict(sorted(((k, v) for k, v in counts.items() if v), key=lambda kv: (-kv[1], kv[0])))


def _summary(photo: osxphotos.PhotoInfo) -> dict[str, Any]:
    return _compact(
        {
            "uuid": photo.uuid,
            "filename": photo.original_filename,
            "date": photo.date.isoformat(timespec="seconds"),
            "title": photo.title,
            "people": _people(photo),
            "keywords": photo.keywords,
            "place": photo.place.name if photo.place else None,
            "favorite": photo.favorite or None,
            "media_type": "video" if photo.ismovie else None,
        }
    )


def _details(photo: osxphotos.PhotoInfo) -> dict[str, Any]:
    exif = photo.exif_info
    camera = _compact(
        {
            "make": exif.camera_make,
            "model": exif.camera_model,
            "lens": exif.lens_model,
            "iso": exif.iso,
            "aperture": exif.aperture,
            "focal_length": exif.focal_length,
            "shutter_speed": exif.shutter_speed,
        }
        if exif
        else {}
    )
    return _compact(
        {
            "uuid": photo.uuid,
            "filename": photo.original_filename,
            "date": photo.date.isoformat(timespec="seconds"),
            "title": photo.title,
            "description": photo.description,
            "people": _people(photo),
            "keywords": photo.keywords,
            "albums": photo.albums,
            "labels": photo.labels,
            "place": photo.place.name if photo.place else None,
            "address": photo.place.address_str if photo.place else None,
            "location": list(photo.location) if photo.latitude is not None else None,
            "favorite": photo.favorite,
            "hidden": photo.hidden,
            "media_type": "video" if photo.ismovie else "photo",
            "width": photo.width,
            "height": photo.height,
            "size_bytes": photo.original_filesize,
            "camera": camera,
            "edited": photo.hasadjustments,
            "downloaded": not photo.ismissing,
            "in_trash": photo.intrash or None,
            "path": photo.path,
            "path_edited": photo.path_edited,
        }
    )
