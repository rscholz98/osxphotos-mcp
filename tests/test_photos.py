import os
from pathlib import Path

import pytest

from osxphotos_mcp.photos import Library

PUMPKINS2 = "D79B8D77-BFFC-460B-9312-034F2877D35B"
WEDDING = "E9BC5C36-7CD1-40A1-A72B-8B8FAC227D51"
NOT_DOWNLOADED = "8E1D7BC9-9321-44F9-8CFB-4083F6B9232A"  # IMG_2000.JPG
HIDDEN = "A1DD1F98-2ECD-431F-9AC9-5AFEFE2D3A5C"  # Pumpkins4.jpg
TRASHED = "71E3E212-00EB-430D-8A63-5E294B268554"


@pytest.fixture(scope="module")
def lib(library_path) -> Library:
    return Library(library_path)


def filenames(result: dict) -> set[str]:
    return {p["filename"] for p in result["photos"]}


def test_search_without_filters_returns_everything_newest_first(lib):
    result = lib.search()
    assert result["total"] == 13
    assert result["photos"][0]["filename"] == "IMG_3092.heic"
    assert result["photos"][0]["date"].startswith("2020-09-19T")
    assert result["photos"][-1]["filename"] == "IMG_1693.tif"


def test_search_limit_caps_the_list_but_not_the_total(lib):
    result = lib.search(limit=2)
    assert result["total"] == 13
    assert len(result["photos"]) == 2


def test_search_limit_is_capped_at_100(lib):
    assert lib.search(limit=100_000)["total"] == 13
    with pytest.raises(ValueError, match="limit"):
        lib.search(limit=0)


def test_search_by_person_ignores_case_and_hides_unknown_faces(lib):
    result = lib.search(person="katie")
    assert filenames(result) == {"Pumpkins3.jpg", "Pumkins2.jpg", "Pumkins1.jpg"}
    assert all("_UNKNOWN_" not in p["people"] for p in result["photos"])


def test_search_date_range_uses_the_date_shown_in_photos_and_includes_the_end_day(lib):
    # Taken 15:35-16:09 at UTC-4: the 28th where they were taken, whatever this machine's timezone.
    result = lib.search(from_date="2018-09-28", to_date="2018-09-28")
    assert result["total"] == 3


def test_search_date_with_utc_offset_is_compared_as_an_absolute_time(lib):
    result = lib.search(from_date="2018-09-28T19:00:00+00:00", to_date="2018-09-28T20:00:00+00:00")
    assert filenames(result) == {"Pumkins1.jpg"}


def test_search_from_date_excludes_older_photos(lib):
    assert filenames(lib.search(from_date="2020-09-01")) == {"IMG_3092.heic"}


def test_search_rejects_a_date_that_is_not_iso_format(lib):
    with pytest.raises(ValueError, match="ISO"):
        lib.search(from_date="last summer")


def test_search_by_album(lib):
    assert lib.search(album="Pumpkin Farm")["total"] == 3


def test_search_by_keyword(lib):
    assert filenames(lib.search(keyword="wedding")) == {"Tulips.jpg", "wedding.jpg"}


def test_search_by_place(lib):
    assert filenames(lib.search(place="Adelaide")) == {"IMG_4547.jpg"}


def test_search_by_favorite(lib):
    assert filenames(lib.search(favorite=True)) == {"wedding.jpg"}
    assert lib.search(favorite=False)["total"] == 12


def test_search_by_media_type(lib):
    assert lib.search(media_type="photo")["total"] == 13
    assert lib.search(media_type="video")["total"] == 0


def test_search_text_matches_title_description_and_album(lib):
    assert filenames(lib.search(text="TULIPS")) == {"Tulips.jpg"}
    assert filenames(lib.search(text="pumpkin")) == {
        "Pumpkins3.jpg",
        "Pumkins2.jpg",
        "Pumkins1.jpg",
    }


def test_search_combines_filters(lib):
    assert filenames(lib.search(text="pumpkin", person="Suzy")) == {
        "Pumpkins3.jpg",
        "Pumkins1.jpg",
    }


def test_search_result_omits_empty_fields(lib):
    newest = lib.search(limit=1)["photos"][0]
    assert set(newest) == {"uuid", "filename", "date"}


def test_get_photo_returns_details(lib):
    photo = lib.get_photo(WEDDING)
    assert photo["filename"] == "wedding.jpg"
    assert photo["description"] == "Bride Wedding day"
    assert photo["people"] == ["Maria"]
    assert set(photo["albums"]) == {"AlbumInFolder", "I have a deleted twin"}
    assert photo["favorite"] is True
    assert photo["media_type"] == "photo"
    assert Path(photo["path"]).is_file()
    assert Path(photo["path_edited"]).is_file()


def test_get_photo_includes_camera_data(lib):
    newest = lib.search(limit=1)["photos"][0]
    camera = lib.get_photo(newest["uuid"])["camera"]
    assert camera["model"] == "iPhone 6s"
    assert camera["iso"] == 25


def test_get_photo_reports_a_photo_that_is_not_downloaded(lib):
    photo = lib.get_photo(NOT_DOWNLOADED)
    assert photo["downloaded"] is False
    assert "path" not in photo


def test_get_photo_with_unknown_uuid_raises(lib):
    with pytest.raises(ValueError, match="No photo"):
        lib.get_photo("nope")


def test_list_albums_reports_folder_and_count(lib):
    albums = lib.list_albums()
    assert {"title": "AlbumInFolder", "folder": "Folder1/SubFolder2", "count": 2} in albums
    assert {"title": "Pumpkin Farm", "count": 3} in albums


def test_people_and_keywords_are_counted_without_unknown_or_unused(lib):
    result = lib.list_people_and_keywords()
    assert result["people"]["Katie"] == 3
    assert "_UNKNOWN_" not in result["people"]
    assert result["keywords"]["Kids"] == 3
    assert result["keywords"]["wedding"] == 2
    assert "Birthday" not in result["keywords"]


def test_export_copies_files_and_reports_what_it_skipped(lib, tmp_path):
    destination = tmp_path / "new folder"
    result = lib.export([PUMPKINS2, NOT_DOWNLOADED, "nope"], str(destination))
    assert [Path(p).name for p in result["exported"]] == ["Pumkins2.jpg"]
    assert (destination / "Pumkins2.jpg").is_file()
    assert result["skipped"] == [
        {"uuid": NOT_DOWNLOADED, "reason": "not downloaded from iCloud"},
        {"uuid": "nope", "reason": "not found"},
    ]


def test_export_uses_the_edited_version_when_there_is_one(lib, tmp_path):
    result = lib.export([WEDDING], str(tmp_path))
    assert len(result["exported"]) == 1
    exported = Path(result["exported"][0])
    assert exported.is_file()
    assert exported.read_bytes() == Path(lib.get_photo(WEDDING)["path_edited"]).read_bytes()


def test_export_does_not_overwrite_existing_files(lib, tmp_path):
    first = lib.export([PUMPKINS2], str(tmp_path))["exported"]
    second = lib.export([PUMPKINS2], str(tmp_path))["exported"]
    assert first != second
    assert len(list(tmp_path.iterdir())) == 2


@pytest.fixture
def touch_database(library_path):
    """Make the library look changed on disk, and undo that afterwards."""
    database = Path(Library(library_path).db().db_path)
    stat = database.stat()

    def touch():
        os.utime(database, (stat.st_atime, database.stat().st_mtime + 10))

    yield touch
    os.utime(database, (stat.st_atime, stat.st_mtime))


def test_database_is_not_reloaded_for_every_change_on_disk(library_path, touch_database):
    # Photos touches its database several times a minute; reloading each time costs seconds.
    lib = Library(library_path)
    first = lib.db()
    touch_database()
    assert lib.db() is first


def test_database_is_reloaded_once_the_loaded_copy_is_old_enough(library_path, touch_database):
    lib = Library(library_path, reload_after=0)
    first = lib.db()
    assert lib.db() is first
    touch_database()
    assert lib.db() is not first


def test_refresh_reloads_the_database_on_next_use(library_path):
    lib = Library(library_path)
    first = lib.db()
    lib.refresh()
    assert lib.db() is not first


def test_unreadable_library_error_explains_full_disk_access(monkeypatch):
    def denied(*args, **kwargs):
        raise OSError("Error copying Photos.sqlite")

    monkeypatch.setattr("osxphotos.PhotosDB", denied)
    with pytest.raises(RuntimeError, match="Full Disk Access"):
        Library(None).db()


def test_list_albums_includes_shared_albums_and_marks_them(shared_library_path):
    assert Library(shared_library_path).list_albums() == [
        {"title": "Photo Shoot", "count": 1},
        {"title": "osxphotos", "count": 2, "shared": True},
    ]


def test_search_leaves_out_hidden_photos(lib):
    assert "Pumpkins4.jpg" not in filenames(lib.search(limit=100))


def test_search_includes_hidden_photos_when_the_library_is_opened_that_way(library_path):
    result = Library(library_path, include_hidden=True).search(limit=100)
    assert result["total"] == 14
    assert "Pumpkins4.jpg" in filenames(result)


def test_search_finds_an_album_inside_a_folder_by_title_or_by_path(lib):
    assert lib.search(album="AlbumInFolder")["total"] == 2
    assert lib.search(album="folder1/subfolder2/albuminfolder")["total"] == 2


def test_search_finds_an_album_with_a_slash_in_its_name(lib):
    assert lib.search(album="2019-10/11 Paris Clermont")["total"] == 1


def test_search_finds_photos_in_all_albums_that_share_a_title(lib):
    assert filenames(lib.search(album="Test Album")) == {"Pumkins2.jpg", "Pumkins1.jpg"}


def test_search_by_unknown_album_says_so(lib):
    with pytest.raises(ValueError, match="No album named 'Nope'"):
        lib.search(album="Nope")


def test_search_by_shared_album(shared_library_path):
    assert Library(shared_library_path).search(album="osxphotos")["total"] == 2


def test_search_text_with_several_words_requires_all_of_them(lib):
    assert filenames(lib.search(text="girl pumpkin")) == {"Pumkins2.jpg", "Pumkins1.jpg"}


def test_search_text_of_only_spaces_is_no_filter(lib):
    assert lib.search(text="   ")["total"] == 13


def test_search_offset_pages_through_results(lib):
    assert lib.search(limit=2, offset=2)["photos"] == lib.search(limit=4)["photos"][2:]
    assert lib.search(offset=13) == {"total": 13, "photos": []}
    with pytest.raises(ValueError, match="offset"):
        lib.search(offset=-1)


def test_get_photo_says_when_a_photo_is_in_recently_deleted(lib):
    assert lib.get_photo(TRASHED)["in_trash"] is True
    assert "in_trash" not in lib.get_photo(WEDDING)


def test_export_reports_photos_that_could_not_be_written_and_carries_on(lib, tmp_path):
    read_only = tmp_path / "read-only"
    read_only.mkdir(mode=0o500)
    result = lib.export([PUMPKINS2, WEDDING], str(read_only))
    assert result["exported"] == []
    assert [s["uuid"] for s in result["skipped"]] == [PUMPKINS2, WEDDING]
    assert all(s["reason"].startswith("failed: ") for s in result["skipped"])


def test_export_needs_an_absolute_destination(lib):
    with pytest.raises(ValueError, match="absolute"):
        lib.export([PUMPKINS2], "exports")


def test_get_photo_refuses_a_hidden_photo(lib):
    with pytest.raises(ValueError, match="hidden"):
        lib.get_photo(HIDDEN)


def test_get_photo_returns_a_hidden_photo_when_hidden_photos_are_included(library_path):
    photo = Library(library_path, include_hidden=True).get_photo(HIDDEN)
    assert photo["filename"] == "Pumpkins4.jpg"
    assert photo["hidden"] is True


def test_export_skips_a_hidden_photo(lib, tmp_path):
    result = lib.export([HIDDEN], str(tmp_path))
    assert result == {"exported": [], "skipped": [{"uuid": HIDDEN, "reason": "hidden"}]}


def test_people_and_keyword_counts_include_hidden_photos_only_on_request(library_path):
    result = Library(library_path, include_hidden=True).list_people_and_keywords()
    assert result["keywords"]["Kids"] == 4


def album(title, *hidden_flags):
    """A stand-in for an osxphotos album; the test libraries have no hidden photo in an album."""
    from types import SimpleNamespace

    photos = [SimpleNamespace(hidden=flag) for flag in hidden_flags]
    return SimpleNamespace(title=title, folder_names=[], photos=photos)


def test_album_count_leaves_out_hidden_photos():
    from osxphotos_mcp.photos import _album_entry

    mixed = album("Trip", False, True, False)
    assert _album_entry(mixed, shared=False, include_hidden=False) == {"title": "Trip", "count": 2}
    assert _album_entry(mixed, shared=False, include_hidden=True) == {"title": "Trip", "count": 3}


def test_album_with_only_hidden_photos_is_not_listed():
    from osxphotos_mcp.photos import _album_entry

    private = album("Private", True, True)
    assert _album_entry(private, shared=False, include_hidden=False) is None
    assert _album_entry(private, shared=False, include_hidden=True) == {
        "title": "Private",
        "count": 2,
    }


def test_empty_album_is_still_listed():
    from osxphotos_mcp.photos import _album_entry

    assert _album_entry(album("New"), shared=True, include_hidden=False) == {
        "title": "New",
        "count": 0,
        "shared": True,
    }
