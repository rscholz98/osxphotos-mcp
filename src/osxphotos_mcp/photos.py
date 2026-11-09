"""Read the Photos library through the osxphotos Python API and return small, plain dicts."""

from __future__ import annotations

import datetime as dt
import threading
from pathlib import Path
from typing import Any, Literal

import osxphotos

MAX_LIMIT = 100
UNKNOWN_PERSON = "_UNKNOWN_"

_NO_ACCESS = (
    "Cannot read the Photos library. Grant Full Disk Access to the app that runs this server "
    "(System Settings > Privacy & Security > Full Disk Access), then restart it. Details: {error}"
)


class Library:
    """A Photos library that is loaded on first use and reloaded when it changes on disk."""

    def __init__(self, path: str | None = None):
        self.path = path
        self._db: osxphotos.PhotosDB | None = None
        self._stamp: float | None = None
        self._lock = threading.Lock()

    def db(self) -> osxphotos.PhotosDB:
        with self._lock:
            if self._db is None or self._stamp != _modified(self._db.db_path):
                try:
                    self._db = osxphotos.PhotosDB(dbfile=self.path)
                except FileNotFoundError:
                    raise
                except OSError as error:
                    raise RuntimeError(_NO_ACCESS.format(error=error)) from error
                self._stamp = _modified(self._db.db_path)
            return self._db

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
    ) -> dict[str, Any]:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        start, end = _parse_date(from_date), _parse_date(to_date, end=True)
        options = osxphotos.QueryOptions(
            album=[album] if album else None,
            person=[person] if person else None,
            keyword=[keyword] if keyword else None,
            place=[place] if place else None,
            favorite=True if favorite else None,
            not_favorite=True if favorite is False else None,
            photos=media_type != "video",
            movies=media_type != "photo",
            ignore_case=True,
        )
        photos = self.db().query(options)
        if start:
            photos = [p for p in photos if _comparable(p.date, start) >= start]
        if end:
            photos = [p for p in photos if _comparable(p.date, end) < end]
        if text:
            needle = text.casefold()
            photos = [p for p in photos if needle in _searchable_text(p)]
        photos.sort(key=lambda p: p.date, reverse=True)
        shown = photos[: min(limit, MAX_LIMIT)]
        return {"total": len(photos), "photos": [_summary(p) for p in shown]}

    def get_photo(self, uuid: str) -> dict[str, Any]:
        photo = self.db().get_photo(uuid)
        if photo is None:
            raise ValueError(f"No photo with UUID '{uuid}'.")
        return _details(photo)

    def list_albums(self) -> list[dict[str, Any]]:
        albums = [
            _compact(
                {
                    "title": album.title,
                    "folder": "/".join(album.folder_names),
                    "count": len(album.photos),
                }
            )
            for album in self.db().album_info
        ]
        return sorted(albums, key=lambda a: (a.get("folder", ""), a["title"]))

    def list_people_and_keywords(self) -> dict[str, dict[str, int]]:
        db = self.db()
        people = {k: v for k, v in db.persons_as_dict.items() if k != UNKNOWN_PERSON}
        return {"people": _used(people), "keywords": _used(db.keywords_as_dict)}

    def export(self, uuids: list[str], destination: str) -> dict[str, Any]:
        folder = Path(destination).expanduser()
        folder.mkdir(parents=True, exist_ok=True)
        db = self.db()
        exported: list[str] = []
        skipped: list[dict[str, str]] = []
        for uuid in uuids:
            photo = db.get_photo(uuid)
            if photo is None:
                skipped.append({"uuid": uuid, "reason": "not found"})
            elif photo.path_edited:
                exported += photo.export(str(folder), edited=True)
            elif photo.path:
                exported += photo.export(str(folder))
            else:
                skipped.append({"uuid": uuid, "reason": "not downloaded from iCloud"})
        return {"exported": exported, "skipped": skipped}


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
            "path": photo.path,
            "path_edited": photo.path_edited,
        }
    )
