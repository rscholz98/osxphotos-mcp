"""Read the Photos library through the osxphotos Python API and return small, plain dicts."""

from __future__ import annotations

import datetime as dt
import threading
import time
from collections import Counter
from contextlib import suppress
from pathlib import Path
from typing import Any, Literal

import osxphotos
from osxphotos.unicode import normalize_unicode

MAX_LIMIT = 100
RELOAD_AFTER_SECONDS = 300
UNKNOWN_PERSON = "_UNKNOWN_"
_PREPARE_AT_ONCE = 200

_HIDDEN = "That photo is hidden in Photos. Start the server with --include-hidden to see it."
_ORIGINAL_ONLY = "edited version not downloaded from iCloud; exported the original"
_NO_ACCESS = (
    "Cannot read the Photos library. Grant Full Disk Access to the app that runs this server "
    "(System Settings > Privacy & Security > Full Disk Access), then restart it. Details: {error}"
)


class _Loaded:
    """One loaded copy of the library, with what has been worked out from it so far."""

    def __init__(self, db: osxphotos.PhotosDB, stamp: float, include_hidden: bool):
        self.db = db
        self.stamp = stamp
        self.loaded_at = time.monotonic()
        options = osxphotos.QueryOptions(not_hidden=None if include_hidden else True)
        self.photos = sorted(db.query(options), key=lambda photo: photo.date, reverse=True)
        """The photos this server may show, newest first."""
        self.counts: dict[str, dict[str, int]] | None = None
        self._texts: dict[str, str] = {}

    def text(self, photo: osxphotos.PhotoInfo) -> str:
        """The photo's searchable text. Kept, because reading a photo's place is slow."""
        text = self._texts.get(photo.uuid)
        if text is None:
            text = self._texts[photo.uuid] = _searchable_text(photo)
        return text


class Library:
    """A Photos library, loaded on first use and reloaded when it has changed on disk.

    Photos touches its database several times a minute and loading a large library takes
    seconds, so a loaded copy is kept for at least `reload_after` seconds. After that a changed
    library is loaded again in the background, while calls are answered from the copy at hand.

    osxphotos does not promise that one library can be read from several threads at once, and
    tool calls arrive on several, so every method holds the lock for as long as it runs.
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
        self._loaded: _Loaded | None = None
        self._db_path: str | None = None
        self._reloading = False
        self._refreshes = 0
        self._lock = threading.RLock()

    def db(self) -> osxphotos.PhotosDB:
        with self._lock:
            return self._current().db

    def preload(self) -> threading.Thread:
        """Start loading now, on a thread of its own, so the first call need not wait for it."""

        def load() -> None:
            with suppress(Exception):  # the first tool call tries again and reports the problem
                self.db()

        return _start(load)

    def refresh(self) -> None:
        """Load the library again the next time it is used, for example after changing it."""
        with self._lock:
            self._loaded = None
            self._refreshes += 1

    def _current(self) -> _Loaded:
        """The loaded library. Call with the lock held."""
        loaded = self._loaded
        if loaded is None:
            loaded = self._loaded = self._load()
            _start(self._prepare_search, loaded)
        elif not self._reloading and self._is_outdated(loaded):
            self._reloading = True
            _start(self._reload, loaded, self._refreshes)
        return loaded

    def _load(self) -> _Loaded:
        # Taken before loading where possible, so a change made meanwhile is noticed later.
        stamp = _modified(self._db_path) if self._db_path else None
        try:
            db = osxphotos.PhotosDB(dbfile=self.path)
        except FileNotFoundError:
            raise
        except OSError as error:
            raise RuntimeError(_NO_ACCESS.format(error=error)) from error
        self._db_path = db.db_path
        if stamp is None:
            stamp = _modified(db.db_path)
        return _Loaded(db, stamp, self.include_hidden)

    def _is_outdated(self, loaded: _Loaded) -> bool:
        old_enough = time.monotonic() - loaded.loaded_at >= self.reload_after
        return old_enough and loaded.stamp != _modified(loaded.db.db_path)

    def _reload(self, outdated: _Loaded, refreshes: int) -> None:
        """Replace the loaded library with a fresh one. Runs on a thread of its own."""
        try:
            fresh = self._load()
            self._prepare_search(fresh)
            with self._lock:
                if refreshes == self._refreshes:  # else it may predate a change just made
                    self._loaded = fresh
        except Exception:
            outdated.loaded_at = time.monotonic()  # keep the copy at hand and try again later
        finally:
            self._reloading = False

    def _prepare_search(self, loaded: _Loaded) -> None:
        """Work out every photo's searchable text ahead of the first text search.

        Runs on a thread of its own, a few photos at a time so tool calls are not held up.
        """
        with suppress(Exception):  # a search that needs the text reports the problem
            for start in range(0, len(loaded.photos), _PREPARE_AT_ONCE):
                with self._lock:
                    for photo in loaded.photos[start : start + _PREPARE_AT_ONCE]:
                        loaded.text(photo)
                time.sleep(0.001)  # or a waiting tool call would not get the lock in between

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
        words = _fold(text).split() if text else []
        with self._lock:
            loaded = self._current()
            photos = loaded.photos
            if favorite is not None:
                photos = [p for p in photos if p.favorite == favorite]
            if media_type:
                photos = [p for p in photos if p.ismovie == (media_type == "video")]
            if album:
                members = _album_members(loaded.db, album)
                photos = [p for p in photos if p.uuid in members]
            if start:
                photos = [p for p in photos if _comparable(p.date, start) >= start]
            if end:
                photos = [p for p in photos if _comparable(p.date, end) < end]
            if person:
                name = _fold(person)
                photos = [p for p in photos if name in map(_fold, _people(p))]
            if keyword:
                name = _fold(keyword)
                photos = [p for p in photos if name in map(_fold, p.keywords)]
            if place:
                name = _fold(place)
                photos = [p for p in photos if _is_at(p, name)]
            if words:
                photos = [p for p in photos if _contains_all(loaded.text(p), words)]
            shown = photos[offset : offset + min(limit, MAX_LIMIT)]
            return {"total": len(photos), "photos": [_summary(p) for p in shown]}

    def get_photo(self, uuid: str) -> dict[str, Any]:
        with self._lock:
            photo = self._current().db.get_photo(uuid)
            if photo is None:
                raise ValueError(f"No photo with UUID '{uuid}'.")
            if photo.hidden and not self.include_hidden:
                raise ValueError(_HIDDEN)
            return _details(photo)

    def list_albums(self) -> list[dict[str, Any]]:
        with self._lock:
            db = self._current().db
            albums = [
                entry
                for shared, infos in ((False, db.album_info), (True, db.album_info_shared))
                for album in infos
                if (entry := _album_entry(album, shared=shared, include_hidden=self.include_hidden))
            ]
            return sorted(albums, key=lambda a: (a.get("folder", ""), a["title"]))

    def list_people_and_keywords(self) -> dict[str, dict[str, int]]:
        with self._lock:
            loaded = self._current()
            if loaded.counts is None:
                people: Counter[str] = Counter()
                keywords: Counter[str] = Counter()
                for photo in loaded.photos:
                    people.update(set(_people(photo)))
                    keywords.update(set(photo.keywords))
                loaded.counts = {"people": _used(people), "keywords": _used(keywords)}
            return loaded.counts

    def export(self, uuids: list[str], destination: str) -> dict[str, Any]:
        folder = Path(destination).expanduser()
        if not folder.is_absolute():
            raise ValueError("destination must be an absolute path (it may start with ~).")
        folder.mkdir(parents=True, exist_ok=True)
        exported: list[str] = []
        skipped: list[dict[str, str]] = []
        warnings: list[dict[str, str]] = []
        with self._lock:
            db = self._current().db
            for uuid in dict.fromkeys(uuids):  # each photo once, in the order given
                photo = db.get_photo(uuid)
                if photo is None:
                    skipped.append({"uuid": uuid, "reason": "not found"})
                elif photo.hidden and not self.include_hidden:
                    skipped.append({"uuid": uuid, "reason": "hidden"})
                elif photo.intrash:
                    skipped.append({"uuid": uuid, "reason": "in Recently Deleted"})
                elif not (photo.path_edited or photo.path):
                    skipped.append({"uuid": uuid, "reason": "not downloaded from iCloud"})
                else:
                    try:
                        exported += photo.export(str(folder), edited=bool(photo.path_edited))
                    except Exception as error:  # osxphotos raises its own, unrelated error types
                        skipped.append({"uuid": uuid, "reason": f"failed: {error}"})
                        continue
                    if photo.hasadjustments and not photo.path_edited:
                        warnings.append({"uuid": uuid, "warning": _ORIGINAL_ONLY})
        result: dict[str, Any] = {"exported": exported, "skipped": skipped}
        if warnings:
            result["warnings"] = warnings
        return result


def _start(work: Any, *args: Any) -> threading.Thread:
    """Run `work` in the background, without keeping the server from shutting down."""
    thread = threading.Thread(target=work, args=args, daemon=True)
    thread.start()
    return thread


def _fold(text: str) -> str:
    """The form in which names are compared: one Unicode form, and no upper or lower case."""
    return normalize_unicode(text).casefold()


def _is_at(photo: osxphotos.PhotoInfo, place: str) -> bool:
    """Whether any of the photo's place names (street, city, country, ...) contains `place`."""
    names = photo.place.names if photo.place else ()
    return any(place in _fold(name) for kind in names for name in kind)


def _album_entry(album: Any, *, shared: bool, include_hidden: bool) -> dict[str, Any] | None:
    """One line of the album list, or None for an album that holds nothing but hidden photos."""
    photos = album.photos
    shown = photos if include_hidden else [photo for photo in photos if not photo.hidden]
    if photos and not shown:
        return None
    return _compact(
        {
            "title": album.title,
            "folder": "/".join(album.folder_names),
            "count": len(shown),
            "shared": shared or None,
        }
    )


def _album_members(db: osxphotos.PhotosDB, name: str) -> set[str]:
    """UUIDs of the photos in every album with this title or folder path, ignoring case."""
    wanted = _fold(name)
    albums = [
        album
        for album in (*db.album_info, *db.album_info_shared)
        if wanted in (_fold(album.title), _fold("/".join([*album.folder_names, album.title])))
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
    if end and _is_whole_day(value):  # an end date means "through the end of that day"
        date += dt.timedelta(days=1)
    return date


def _is_whole_day(value: str) -> bool:
    try:
        dt.date.fromisoformat(value)
    except ValueError:
        return False
    return True


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
    return _fold("\n".join(part for part in parts if part))


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
