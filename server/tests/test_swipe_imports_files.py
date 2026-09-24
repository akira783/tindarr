"""Reading the three export formats, and refusing everything else.

The rules under test are the ones a real five-month Netflix export was used to find:
the French colon, the counting that settles an ambiguous row, and the trailers. They are
the difference between an import somebody can trust and one that quietly writes down the
wrong film — so they are tested on the exact strings that broke the prototype.
"""

import io
import zipfile

import pytest

from tindarr.swipe.imports.files import detect_and_parse
from tindarr.swipe.imports.netflix import (
    NetflixRow,
    is_supplemental,
    netflix_items,
    normalized,
    parse_title,
    read_date,
)
from tindarr.swipe.imports.records import (
    MAX_FIELD_LENGTH,
    MAX_TITLES,
    MAX_UNPACKED_BYTES,
    UnreadableImportError,
)


def netflix_csv(*rows: str) -> bytes:
    """A short Netflix viewing history, the one the account page downloads."""
    body = "\n".join(f'"{row}","9/10/26"' for row in rows)
    return f"Title,Date\n{body}\n".encode()


class TestNetflixTitles:
    """Splitting one row of Netflix prose into a title and an episode."""

    @pytest.mark.parametrize(
        ("row", "show", "series"),
        [
            ("Perdus dans l'espace: Saison 1: Infestés", "Perdus dans l'espace", True),
            ("Overlord: Overlord III: PVP", "Overlord", True),
            ("Shangri-La Frontier: 1st Saison: Le seul et l'unique", "Shangri-La Frontier", True),
            ("Sur tes traces: Mini-série: Épisode 4", "Sur tes traces", True),
            ("GIGN: Épisode 1", "GIGN", True),
            ("The Truman Show", "The Truman Show", False),
            ("Away: Le point de non-retour", "Away", False),
        ],
    )
    def test_splits_on_netflix_own_separator(self, row: str, show: str, series: bool) -> None:
        parsed_show, _, certain = parse_title(row)
        assert parsed_show == show
        assert certain is series

    @pytest.mark.parametrize(
        "row",
        [
            "Avatar : Le dernier maître de l'air",
            "The Witcher : Les sirènes des abysses",
            "Freefall : Boeing au banc des accusés",
        ],
    )
    def test_never_splits_a_french_colon(self, row: str) -> None:
        # The whole point: a space before the colon means the colon is in the title.
        assert parse_title(row) == (row, None, False)

    def test_never_splits_a_french_no_break_colon(self) -> None:
        # Real rows use U+00A0 before the colon, which is what French typography asks
        # for and what a plain-space lookbehind misses.
        title = "The Handmaid's Tale\u00a0: La Servante écarlate"
        show, part, certain = parse_title(f"{title}: Saison 1: Épisode 3")
        assert show == title
        assert part == "Saison 1: Épisode 3"
        assert certain

    def test_a_french_subtitle_and_a_netflix_season_in_one_row(self) -> None:
        row = "Les Meurtres de l'Idaho : Cauchemar sur le campus: Adresse : 1122 King Road"
        show, part, _ = parse_title(row)
        assert show == "Les Meurtres de l'Idaho : Cauchemar sur le campus"
        assert part == "Adresse : 1122 King Road"

    def test_ampersand_reads_as_the_french_word(self) -> None:
        assert normalized("Ducks & Co") == normalized("Ducks et Co")

    def test_accents_and_punctuation_do_not_distinguish_two_titles(self) -> None:
        assert normalized("Perdus dans l'espace") == normalized("PERDUS DANS L ESPACE")


class TestNetflixGrouping:
    """One item per title, with its episodes counted."""

    def test_counts_distinct_episodes_per_series(self) -> None:
        items, _ = netflix_items(
            [
                NetflixRow("Heroes: Saison 3: Le monde à l'envers"),
                NetflixRow("Heroes: Saison 3: L'Effet papillon"),
                NetflixRow("Heroes: Saison 3: L'Effet papillon"),
                NetflixRow("Heroes: Saison 1: Genesis"),
            ]
        )
        assert [(item.query, item.kind_hint, item.episodes) for item in items] == [
            ("Heroes", "tv", 3)
        ]

    def test_two_different_trailing_parts_settle_an_ambiguous_title(self) -> None:
        # "Away" has no season marker anywhere, and is a series all the same: nothing
        # but a series has two different things after the colon.
        items, _ = netflix_items(
            [NetflixRow("Away: Le point de non-retour"), NetflixRow("Away: OK")]
        )
        assert [(item.query, item.kind_hint, item.episodes) for item in items] == [
            ("Away", "tv", 2)
        ]

    def test_one_trailing_part_stays_a_film_until_something_says_otherwise(self) -> None:
        items, _ = netflix_items([NetflixRow("Event Horizon: Le vaisseau de l'au-delà")])
        assert items[0].kind_hint is None
        assert items[0].episodes == 0

    def test_keeps_the_most_recent_date_per_title(self) -> None:
        items, _ = netflix_items(
            [
                NetflixRow("Heroes: Saison 1: A", watched_at=read_date("9/2/26")),
                NetflixRow("Heroes: Saison 1: B", watched_at=read_date("9/4/26")),
            ]
        )
        assert items[0].last_watched_at == read_date("9/4/26")

    def test_a_row_with_no_readable_date_is_kept(self) -> None:
        items, skipped = netflix_items([NetflixRow("Dune", watched_at=read_date("not a date"))])
        assert [item.query for item in items] == ["Dune"]
        assert not skipped

    def test_an_empty_title_is_dropped_and_counted(self) -> None:
        _, skipped = netflix_items([NetflixRow("   "), NetflixRow(": Saison 1: A")])
        assert skipped == {"empty": 2}

    def test_a_title_longer_than_a_title_is_cut(self) -> None:
        items, _ = netflix_items([NetflixRow("x" * (MAX_FIELD_LENGTH + 500))])
        assert len(items[0].query) == MAX_FIELD_LENGTH


class TestTrailers:
    """A trailer is not a viewing, in either export."""

    def test_the_long_export_says_so_in_its_own_column(self) -> None:
        assert is_supplemental(NetflixRow("Dune", supplemental="HOOK"))

    @pytest.mark.parametrize(
        "row",
        [
            "Grand Theft Auto VI: un large aperçu",
            "Wednesday: Bande-annonce officielle",
            "Stranger Things: Behind the scenes",
        ],
    )
    def test_the_short_export_needs_the_words(self, row: str) -> None:
        assert is_supplemental(NetflixRow(row))

    def test_a_film_whose_own_title_is_one_of_those_words_survives(self) -> None:
        # The heuristic reads the trailing part, never the title.
        assert not is_supplemental(NetflixRow("Teaser"))
        assert not is_supplemental(NetflixRow("Bande-annonce"))

    def test_they_are_dropped_and_counted(self) -> None:
        parsed = detect_and_parse(netflix_csv("Dune", "Dune: un large aperçu"))
        assert parsed.skipped == {"supplemental": 1}
        assert parsed.rows_kept == 1


class TestDetection:
    """The format is recognised, never asked for."""

    def test_the_short_netflix_export(self) -> None:
        parsed = detect_and_parse(netflix_csv("Dune"))
        assert parsed.format == "netflix"

    def test_the_long_gdpr_export(self) -> None:
        payload = (
            b"Profile Name,Start Time,Duration,Title,Supplemental Video Type,Device Type\n"
            b'Someone,2026-09-10 20:11:00,00:52:03,"Heroes: Saison 1: Genesis",,TV\n'
            b'Someone,2026-09-10 21:05:00,00:00:31,"Heroes: Bande-annonce",TRAILER,TV\n'
        )
        parsed = detect_and_parse(payload)
        assert parsed.format == "netflix"
        assert parsed.skipped == {"supplemental": 1}
        assert parsed.items[0].last_watched_at is not None

    def test_an_imdb_ratings_export(self) -> None:
        payload = (
            b"Const,Your Rating,Date Rated,Title,Original Title,URL,Title Type,"
            b"IMDb Rating,Runtime (mins),Year,Genres,Num Votes,Release Date,Directors\n"
            b"tt0816692,9,2026-01-04,Interstellar,Interstellar,https://x,movie,8.7,169,2014,"
            b"Adventure,2000000,2014-11-05,Christopher Nolan\n"
            b"tt0944947,10,2026-01-05,Game of Thrones,Game of Thrones,https://x,tvSeries,"
            b"9.2,57,2011,Action,2200000,2011-04-17,\n"
            b"tt2178784,8,2026-01-06,The Rains of Castamere,,https://x,tvEpisode,9.7,51,2013,"
            b"Action,120000,2013-06-02,\n"
            b"tt0000001,3,2026-01-07,Some Game,,https://x,videoGame,5.0,,2013,Action,10,,\n"
        )
        parsed = detect_and_parse(payload)
        assert parsed.format == "imdb"
        assert [(item.imdb_id, item.kind_hint, item.episodes) for item in parsed.items] == [
            ("tt0816692", "movie", 0),
            ("tt0944947", "tv", 0),
            ("tt2178784", "tv", 1),
        ]
        assert parsed.items[0].rating == 9.0
        assert parsed.skipped == {"not_a_title": 1}

    def test_a_letterboxd_csv(self) -> None:
        payload = (
            b"Date,Name,Year,Letterboxd URI,Rating\n2026-02-01,Arrival,2016,https://boxd.it/x,4.5\n"
        )
        parsed = detect_and_parse(payload)
        assert parsed.format == "letterboxd"
        # Letterboxd's five stars, on TMDb's ten.
        assert parsed.items[0].rating == 9.0
        assert parsed.items[0].kind_hint == "movie"
        assert parsed.items[0].year == 2016

    def test_a_letterboxd_archive_prefers_its_ratings(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(
                "watched.csv", "Date,Name,Year,Letterboxd URI\n2026-01-01,Dune,2021,u\n"
            )
            archive.writestr(
                "ratings.csv", "Date,Name,Year,Letterboxd URI,Rating\n2026-01-01,Arrival,2016,u,5\n"
            )
        parsed = detect_and_parse(buffer.getvalue())
        assert parsed.format == "letterboxd"
        assert [item.query for item in parsed.items] == ["Arrival"]

    def test_an_archive_with_the_export_in_a_folder(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(
                "letterboxd-someone/watched.csv",
                "Date,Name,Year,Letterboxd URI\n1,Dune,2021,u\n",
            )
        assert detect_and_parse(buffer.getvalue()).items[0].query == "Dune"

    def test_an_archive_of_something_else_entirely(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("notes.txt", "hello")
            archive.writestr("other.csv", "a,b\n1,2\n")
        with pytest.raises(UnreadableImportError):
            detect_and_parse(buffer.getvalue())

    def test_a_windows_spreadsheet_encoding(self) -> None:
        payload = 'Title,Date\n"Amélie",9/10/26\n'.encode("cp1252")
        assert detect_and_parse(payload).items[0].query == "Amélie"

    def test_a_byte_order_mark_is_not_part_of_the_first_column_name(self) -> None:
        assert detect_and_parse("﻿Title,Date\nDune,9/10/26\n".encode()).format == "netflix"


class TestRefusals:
    """Everything an upload is held to, before anything is read."""

    def test_a_file_that_is_not_an_export(self) -> None:
        with pytest.raises(UnreadableImportError):
            detect_and_parse(b"who,what\n1,2\n")

    def test_a_file_with_no_header(self) -> None:
        with pytest.raises(UnreadableImportError):
            detect_and_parse(b"")

    def test_a_file_that_is_not_a_zip_but_claims_to_be(self) -> None:
        with pytest.raises(UnreadableImportError):
            detect_and_parse(b"PK\x03\x04 not really")

    def test_an_export_with_a_header_and_no_rows(self) -> None:
        with pytest.raises(UnreadableImportError):
            detect_and_parse(b"Title,Date\n")

    def test_a_zip_bomb_is_refused_on_its_own_declared_sizes(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("watched.csv", "0" * (MAX_UNPACKED_BYTES + 1))
        with pytest.raises(UnreadableImportError):
            detect_and_parse(buffer.getvalue())

    def test_an_archive_of_too_many_members(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for index in range(300):
                archive.writestr(f"f{index}.csv", "a\n")
        with pytest.raises(UnreadableImportError):
            detect_and_parse(buffer.getvalue())

    def test_a_file_of_more_titles_than_an_import_may_cost(self) -> None:
        rows = "\n".join(f'"Film {index}","9/10/26"' for index in range(MAX_TITLES + 25))
        parsed = detect_and_parse(f"Title,Date\n{rows}\n".encode())
        assert parsed.rows_kept == MAX_TITLES
        assert parsed.skipped["over_limit"] == 25
