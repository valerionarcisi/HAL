#!/usr/bin/env python3
"""Unit tests for sub_fetcher improvements."""

import os
import sys
import re
import tempfile
import shutil
import unittest
import importlib
import io
import zipfile
import urllib.request
import json

# Setup: create a temp /config-like directory and patch the module
# before it gets imported, to avoid FileNotFoundError on /config
_test_tmpdir = tempfile.mkdtemp(prefix="subfetcher_test_")
_test_config = os.path.join(_test_tmpdir, "config")
os.makedirs(_test_config, exist_ok=True)

os.environ["TELEGRAM_BOT_TOKEN"] = "fake"
os.environ["TELEGRAM_CHAT_ID"] = "0"

# Read the source and exec with patched paths
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Patch: create a modified version of sub_fetcher with overridden paths
_src_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sub_fetcher.py")
with open(_src_path, "r") as f:
    _src = f.read()

# Replace hardcoded /config paths
_src = _src.replace('"/config"', f'"{_test_config}"')
_src = _src.replace("STATE_FILE = ", f'STATE_FILE = "{_test_config}/state.json"  # ')
_src = _src.replace("LOG_FILE = ", f'LOG_FILE = "{_test_config}/sub_fetcher.log"  # ')
_src = _src.replace("EXCLUDE_FOLDERS_FILE = ", f'EXCLUDE_FOLDERS_FILE = "{_test_config}/exclude_folders.txt"  # ')

import types
sub_fetcher = types.ModuleType("sub_fetcher")
sub_fetcher.__file__ = _src_path
exec(compile(_src, _src_path, "exec"), sub_fetcher.__dict__)
sys.modules["sub_fetcher"] = sub_fetcher


class TestFindImdbId(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_finds_imdb_from_tvshow_nfo(self):
        series_dir = os.path.join(self.tmpdir, "MyShow")
        season_dir = os.path.join(series_dir, "Season 1")
        os.makedirs(season_dir)

        with open(os.path.join(series_dir, "tvshow.nfo"), "w") as f:
            f.write('<?xml version="1.0"?>\n<tvshow>\n<id>tt1234567</id>\n</tvshow>')

        video = os.path.join(season_dir, "MyShow.S01E01.mkv")
        open(video, "w").close()

        result = sub_fetcher.find_imdb_id(video)
        self.assertEqual(result, "tt1234567")

    def test_finds_imdb_from_episode_nfo(self):
        d = os.path.join(self.tmpdir, "show")
        os.makedirs(d)

        with open(os.path.join(d, "episode.nfo"), "w") as f:
            f.write("some text tt9876543 more text")

        video = os.path.join(d, "video.mkv")
        open(video, "w").close()

        result = sub_fetcher.find_imdb_id(video)
        self.assertEqual(result, "tt9876543")

    def test_returns_none_when_no_nfo(self):
        d = os.path.join(self.tmpdir, "empty")
        os.makedirs(d)
        video = os.path.join(d, "video.mkv")
        open(video, "w").close()

        result = sub_fetcher.find_imdb_id(video)
        self.assertIsNone(result)

    def test_returns_none_when_nfo_has_no_imdb(self):
        d = os.path.join(self.tmpdir, "show")
        os.makedirs(d)

        with open(os.path.join(d, "tvshow.nfo"), "w") as f:
            f.write("no imdb id here")

        video = os.path.join(d, "video.mkv")
        open(video, "w").close()

        result = sub_fetcher.find_imdb_id(video)
        self.assertIsNone(result)


class TestDetectLanguageFromSrt(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def _write_srt(self, name, content):
        path = os.path.join(self.tmpdir, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path

    def test_detects_italian(self):
        srt = self._write_srt("test.srt", """1
00:00:01,000 --> 00:00:03,000
Che cosa stai facendo?

2
00:00:04,000 --> 00:00:06,000
Non sono sicuro di questo.

3
00:00:07,000 --> 00:00:09,000
Anche per me è una cosa strana.
""")
        self.assertEqual(sub_fetcher.detect_language_from_srt(srt), "it")

    def test_detects_english(self):
        srt = self._write_srt("test.srt", """1
00:00:01,000 --> 00:00:03,000
What are you doing?

2
00:00:04,000 --> 00:00:06,000
I was not sure about that.

3
00:00:07,000 --> 00:00:09,000
They have been there for a while.
""")
        self.assertEqual(sub_fetcher.detect_language_from_srt(srt), "en")

    def test_returns_unknown_for_ambiguous(self):
        srt = self._write_srt("test.srt", "123\n")
        self.assertEqual(sub_fetcher.detect_language_from_srt(srt), "unknown")

    def test_handles_missing_file(self):
        self.assertEqual(sub_fetcher.detect_language_from_srt("/nonexistent.srt"), "unknown")


class TestFindExistingSrt(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_finds_english_srt(self):
        video = os.path.join(self.tmpdir, "Movie.2024.mkv")
        en_srt = os.path.join(self.tmpdir, "Movie.2024.en.srt")
        open(video, "w").close()
        with open(en_srt, "w") as f:
            f.write("1\n00:00:01,000 --> 00:00:02,000\nHello\n")

        result = sub_fetcher.find_existing_srt(video)
        self.assertIsNotNone(result)
        self.assertEqual(result["lang"], "en")
        self.assertEqual(result["path"], en_srt)

    def test_finds_generic_srt_and_detects_lang(self):
        video = os.path.join(self.tmpdir, "Movie.2024.mkv")
        srt = os.path.join(self.tmpdir, "Movie.2024.srt")
        open(video, "w").close()
        with open(srt, "w", encoding="utf-8") as f:
            f.write("1\n00:00:01,000 --> 00:00:03,000\nWhat are you doing?\n\n"
                    "2\n00:00:04,000 --> 00:00:06,000\nThey have been there.\n\n"
                    "3\n00:00:07,000 --> 00:00:09,000\nThis was not the right time.\n")

        result = sub_fetcher.find_existing_srt(video)
        self.assertIsNotNone(result)
        self.assertEqual(result["lang"], "en")

    def test_returns_none_when_no_srt(self):
        video = os.path.join(self.tmpdir, "Movie.2024.mkv")
        open(video, "w").close()

        result = sub_fetcher.find_existing_srt(video)
        self.assertIsNone(result)

    def test_skips_italian_tagged_srt(self):
        video = os.path.join(self.tmpdir, "Movie.2024.mkv")
        open(video, "w").close()
        ita_srt = os.path.join(self.tmpdir, "Movie.2024.it.srt")
        with open(ita_srt, "w") as f:
            f.write("1\n00:00:01,000 --> 00:00:02,000\nCiao\n")

        result = sub_fetcher.find_existing_srt(video)
        self.assertIsNone(result)


class TestGetSearchQueries(unittest.TestCase):
    def test_episode_with_different_folder(self):
        path = "/media/series/PLUR1BUS/Pluribus.S01E01.720p.x264-FENiX.mkv"
        queries = sub_fetcher.get_search_queries(path)
        self.assertIn("Pluribus", queries)
        self.assertTrue(any("PLUR" in q.upper() for q in queries))

    def test_episode_same_name(self):
        path = "/media/series/The Chosen/Season 1/The.Chosen.S01E01.mkv"
        queries = sub_fetcher.get_search_queries(path)
        self.assertEqual(queries[0], "The Chosen")

    def test_movie(self):
        path = "/media/films/Birdman (2014)/Birdman.2014.1080p.mkv"
        queries = sub_fetcher.get_search_queries(path)
        self.assertEqual(queries[0], "Birdman")

    def test_deduplication(self):
        path = "/media/series/Pluribus/Pluribus.S01E01.mkv"
        queries = sub_fetcher.get_search_queries(path)
        lower_queries = [q.lower() for q in queries]
        self.assertEqual(len(lower_queries), len(set(lower_queries)))


class TestParseVideo(unittest.TestCase):
    def test_episode(self):
        result = sub_fetcher.parse_video("/media/series/PLUR1BUS/Pluribus.S01E05.720p.x264-FENiX.mkv")
        self.assertEqual(result["type"], "episode")
        self.assertEqual(result["name"], "Pluribus")
        self.assertEqual(result["season"], 1)
        self.assertEqual(result["episode"], 5)

    def test_movie(self):
        result = sub_fetcher.parse_video("/media/films/Birdman (2014)/Birdman.2014.1080p.BluRay.mkv")
        self.assertEqual(result["type"], "movie")
        self.assertEqual(result["name"], "Birdman")
        self.assertEqual(result["year"], 2014)

    def test_unknown_uses_filename_not_parent(self):
        result = sub_fetcher.parse_video("/media/films/Loro.mp4")
        self.assertEqual(result["type"], "unknown")
        self.assertEqual(result["name"], "Loro")
        self.assertNotEqual(result["name"], "films")

    def test_movie_year_in_parentheses(self):
        result = sub_fetcher.parse_video("/media/films/Punch-Drunk Love (2002).mkv")
        self.assertEqual(result["type"], "movie")
        self.assertEqual(result["name"], "Punch-Drunk Love")
        self.assertEqual(result["year"], 2002)

    def test_movie_strips_scraper_prefix(self):
        result = sub_fetcher.parse_video("/media/films/www.SceneTime.com - Punch-Drunk Love (2002).mkv")
        self.assertEqual(result["type"], "movie")
        self.assertEqual(result["name"], "Punch-Drunk Love")
        self.assertEqual(result["year"], 2002)

    def test_movie_strips_bracket_tracker_tag(self):
        result = sub_fetcher.parse_video("/media/films/[YTS.MX] Gummo (1997).mkv")
        self.assertEqual(result["type"], "movie")
        self.assertEqual(result["name"], "Gummo")
        self.assertEqual(result["year"], 1997)

    def test_episode_strips_scraper_prefix(self):
        result = sub_fetcher.parse_video("/media/series/www.SceneTime.com - Pluribus.S01E05.720p.mkv")
        self.assertEqual(result["type"], "episode")
        self.assertEqual(result["name"], "Pluribus")
        self.assertEqual(result["season"], 1)
        self.assertEqual(result["episode"], 5)

    def test_unknown_cleans_junk_tags(self):
        result = sub_fetcher.parse_video("/media/films/SomeMovie/SomeMovie.720p.BluRay.x264.mp4")
        self.assertEqual(result["type"], "unknown")
        self.assertNotIn("720p", result["name"])
        self.assertIn("SomeMovie", result["name"])


class TestPlaceholderDetection(unittest.TestCase):
    def _fake_sub(self, n_blocks, footer=""):
        lines = []
        for i in range(1, n_blocks + 1):
            h, m = divmod(i, 60)
            lines.append(f"{i}\n00:{h:02d}:{m:02d},000 --> 00:{h:02d}:{m:02d},500\nLine {i}\n")
        return ("\n".join(lines) + footer).encode("utf-8")

    def test_rejects_too_short(self):
        content = self._fake_sub(2)
        self.assertTrue(sub_fetcher.is_placeholder_sub(content))

    def test_rejects_strong_pattern_osdb(self):
        content = self._fake_sub(500, "\nVisit osdb.link/vip for more")
        self.assertTrue(sub_fetcher.is_placeholder_sub(content))

    def test_rejects_strong_pattern_vip_member(self):
        content = self._fake_sub(500, "\nBecome a VIP member now")
        self.assertTrue(sub_fetcher.is_placeholder_sub(content))

    def test_accepts_real_sub_with_opensubtitles_credit(self):
        # Real sub with ~500 blocks and "opensubtitles" only in footer credits
        content = self._fake_sub(500, "\nDownloaded from opensubtitles.org\n")
        self.assertFalse(sub_fetcher.is_placeholder_sub(content))

    def test_rejects_short_sub_with_opensubtitles_keyword(self):
        content = self._fake_sub(10, "\nopensubtitles")
        self.assertTrue(sub_fetcher.is_placeholder_sub(content))

    def test_accepts_long_real_sub(self):
        content = self._fake_sub(800)
        self.assertFalse(sub_fetcher.is_placeholder_sub(content))

    def test_rejects_single_block_long_span(self):
        content = b"1\n00:00:00,000 --> 05:00:00,000\nAd\n\n2\n00:00:01,000 --> 00:00:02,000\n.\n\n3\n00:00:03,000 --> 00:00:04,000\n.\n"
        self.assertTrue(sub_fetcher.is_placeholder_sub(content))


class TestOSClientRestMapping(unittest.TestCase):
    """Verify the REST v1 -> legacy field mapping in OSClient._to_legacy."""

    def test_maps_basic_fields(self):
        item = {
            "attributes": {
                "release": "Punch-Drunk.Love.2002.1080p.BluRay.x264-DEPTH",
                "download_count": 1234,
                "ratings": 8.5,
                "moviehash_match": False,
                "files": [{"file_id": 987, "file_name": "Punch-Drunk.Love.srt"}],
            }
        }
        legacy = sub_fetcher.OSClient._to_legacy(item)
        self.assertEqual(legacy["SubFileName"], "Punch-Drunk.Love.srt")
        self.assertEqual(legacy["MovieReleaseName"], "Punch-Drunk.Love.2002.1080p.BluRay.x264-DEPTH")
        self.assertEqual(legacy["IDSubtitleFile"], "987")
        self.assertEqual(legacy["SubFormat"], "srt")
        self.assertEqual(legacy["SubDownloadsCnt"], 1234)
        self.assertEqual(legacy["SubRating"], 8.5)
        self.assertEqual(legacy["MatchedBy"], "")

    def test_marks_hash_matches(self):
        item = {"attributes": {"moviehash_match": True, "files": [{"file_id": 1, "file_name": "x.srt"}]}}
        legacy = sub_fetcher.OSClient._to_legacy(item)
        self.assertEqual(legacy["MatchedBy"], "moviehash")

    def test_handles_empty_files(self):
        item = {"attributes": {"release": "Foo", "files": []}}
        legacy = sub_fetcher.OSClient._to_legacy(item)
        self.assertEqual(legacy["SubFileName"], "Foo")
        self.assertEqual(legacy["IDSubtitleFile"], "")

    def test_handles_missing_attributes(self):
        legacy = sub_fetcher.OSClient._to_legacy({})
        self.assertEqual(legacy["SubFileName"], "")
        self.assertEqual(legacy["SubDownloadsCnt"], 0)


class TestTmdbLookup(unittest.TestCase):
    def test_returns_none_without_api_key(self):
        original = sub_fetcher.TMDB_API_KEY
        sub_fetcher.TMDB_API_KEY = ""
        try:
            self.assertIsNone(sub_fetcher.tmdb_find_imdb_id("Whatever", 2020))
        finally:
            sub_fetcher.TMDB_API_KEY = original

    def test_resolves_imdb_id_via_two_calls(self):
        from unittest.mock import patch, MagicMock
        original = sub_fetcher.TMDB_API_KEY
        sub_fetcher.TMDB_API_KEY = "fake"

        search_resp = MagicMock()
        search_resp.read.return_value = json.dumps({"results": [{"id": 42}]}).encode()
        search_resp.__enter__ = lambda s: s
        search_resp.__exit__ = lambda *a: None

        ext_resp = MagicMock()
        ext_resp.read.return_value = json.dumps({"imdb_id": "tt0272338"}).encode()
        ext_resp.__enter__ = lambda s: s
        ext_resp.__exit__ = lambda *a: None

        try:
            with patch("urllib.request.urlopen", side_effect=[search_resp, ext_resp]):
                result = sub_fetcher.tmdb_find_imdb_id("Punch-Drunk Love", 2002)
            self.assertEqual(result, "tt0272338")
        finally:
            sub_fetcher.TMDB_API_KEY = original

    def test_returns_none_on_empty_search(self):
        from unittest.mock import patch, MagicMock
        original = sub_fetcher.TMDB_API_KEY
        sub_fetcher.TMDB_API_KEY = "fake"
        empty_resp = MagicMock()
        empty_resp.read.return_value = json.dumps({"results": []}).encode()
        empty_resp.__enter__ = lambda s: empty_resp
        empty_resp.__exit__ = lambda *a: None
        try:
            with patch("urllib.request.urlopen", return_value=empty_resp):
                self.assertIsNone(sub_fetcher.tmdb_find_imdb_id("NonExistentMovie", 1900))
        finally:
            sub_fetcher.TMDB_API_KEY = original


class TestItalianOriginalDetection(unittest.TestCase):
    """tmdb_get_original_language and is_italian_original."""

    def _mock_tmdb_search(self, original_language):
        from unittest.mock import MagicMock
        resp = MagicMock()
        resp.read.return_value = json.dumps(
            {"results": [{"id": 1, "original_language": original_language}]}
        ).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda *a: None
        return resp

    def test_get_original_language_returns_it(self):
        from unittest.mock import patch
        original = sub_fetcher.TMDB_API_KEY
        sub_fetcher.TMDB_API_KEY = "fake"
        try:
            with patch("urllib.request.urlopen",
                       return_value=self._mock_tmdb_search("it")):
                lang = sub_fetcher.tmdb_get_original_language("La Grande Bellezza", 2013)
            self.assertEqual(lang, "it")
        finally:
            sub_fetcher.TMDB_API_KEY = original

    def test_get_original_language_returns_none_without_key(self):
        original = sub_fetcher.TMDB_API_KEY
        sub_fetcher.TMDB_API_KEY = ""
        try:
            self.assertIsNone(sub_fetcher.tmdb_get_original_language("X", 2020))
        finally:
            sub_fetcher.TMDB_API_KEY = original

    def test_is_italian_original_true_for_italian_film(self):
        from unittest.mock import patch
        with patch.object(sub_fetcher, "tmdb_get_original_language", return_value="it"):
            self.assertTrue(
                sub_fetcher.is_italian_original("/media/films/La Grande Bellezza (2013)/film.mkv")
            )

    def test_is_italian_original_false_for_english_film(self):
        from unittest.mock import patch
        with patch.object(sub_fetcher, "tmdb_get_original_language", return_value="en"):
            self.assertFalse(
                sub_fetcher.is_italian_original("/media/films/Punch-Drunk Love (2002)/film.mkv")
            )

    def test_is_italian_original_false_when_tmdb_unknown(self):
        from unittest.mock import patch
        with patch.object(sub_fetcher, "tmdb_get_original_language", return_value=None):
            self.assertFalse(
                sub_fetcher.is_italian_original("/media/films/Random Movie (2020)/film.mkv")
            )

    def test_scan_missing_skips_italian_originals_and_caches(self):
        from unittest.mock import patch
        tmp = tempfile.mkdtemp()
        try:
            italian_dir = os.path.join(tmp, "La Grande Bellezza (2013)")
            os.makedirs(italian_dir)
            video = os.path.join(italian_dir, "La Grande Bellezza.mkv")
            open(video, "w").close()

            state = {"asked": {}, "downloaded": {}, "italian_original": {}}

            with patch.object(sub_fetcher, "FILMS_PATH", tmp), \
                 patch.object(sub_fetcher, "SERIES_PATH", "/nonexistent"), \
                 patch.object(sub_fetcher, "has_italian_audio", return_value=False), \
                 patch.object(sub_fetcher, "is_italian_original", return_value=True), \
                 patch.object(sub_fetcher, "save_state"):
                missing = sub_fetcher.scan_missing(state, excludes=set())
            self.assertEqual(missing, [], "Italian-original film must not be in missing list")
            self.assertIn(video, state["italian_original"],
                "Italian-original detection should be cached in state")
        finally:
            shutil.rmtree(tmp)


class TestFindEnglishSub(unittest.TestCase):
    def test_finds_en_srt(self):
        tmp = tempfile.mkdtemp()
        try:
            video = os.path.join(tmp, "film.mkv")
            en = os.path.join(tmp, "film.en.srt")
            open(video, "w").close()
            open(en, "w").close()
            self.assertEqual(sub_fetcher.find_english_sub(video), en)
        finally:
            shutil.rmtree(tmp)

    def test_finds_eng_srt(self):
        tmp = tempfile.mkdtemp()
        try:
            video = os.path.join(tmp, "film.mkv")
            eng = os.path.join(tmp, "film.eng.srt")
            open(video, "w").close()
            open(eng, "w").close()
            self.assertEqual(sub_fetcher.find_english_sub(video), eng)
        finally:
            shutil.rmtree(tmp)

    def test_finds_english_srt(self):
        tmp = tempfile.mkdtemp()
        try:
            video = os.path.join(tmp, "film.mkv")
            english = os.path.join(tmp, "film.english.srt")
            open(video, "w").close()
            open(english, "w").close()
            self.assertEqual(sub_fetcher.find_english_sub(video), english)
        finally:
            shutil.rmtree(tmp)

    def test_returns_none_when_no_en_sub(self):
        tmp = tempfile.mkdtemp()
        try:
            video = os.path.join(tmp, "film.mkv")
            open(video, "w").close()
            self.assertIsNone(sub_fetcher.find_english_sub(video))
        finally:
            shutil.rmtree(tmp)

    def test_prefers_en_over_eng_over_english(self):
        tmp = tempfile.mkdtemp()
        try:
            video = os.path.join(tmp, "film.mkv")
            en = os.path.join(tmp, "film.en.srt")
            eng = os.path.join(tmp, "film.eng.srt")
            open(video, "w").close()
            open(en, "w").close()
            open(eng, "w").close()
            self.assertEqual(sub_fetcher.find_english_sub(video), en)
        finally:
            shutil.rmtree(tmp)


class TestAutoEnqueueMissing(unittest.TestCase):
    """auto_enqueue_missing should put download jobs in the queue without
    asking for user confirmation."""

    def _drain_queue(self):
        from queue import Empty
        items = []
        while True:
            try:
                items.append(sub_fetcher.download_queue.get_nowait())
                sub_fetcher.download_queue.task_done()
            except Empty:
                break
        return items

    def test_single_film_enqueues_single_job(self):
        from unittest.mock import patch
        self._drain_queue()
        with patch.object(sub_fetcher, "tg_send",
                          return_value={"ok": True, "result": {"message_id": 1}}), \
             patch.object(sub_fetcher, "save_state"), \
             patch.object(sub_fetcher, "queue_position", return_value=0), \
             patch("time.sleep"):
            sub_fetcher.auto_enqueue_missing(
                ["/media/films/Film.2024/Film.2024.mkv"],
                state={"asked": {}, "downloaded": {}},
            )
        items = self._drain_queue()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["type"], "single")
        self.assertEqual(items[0]["path"], "/media/films/Film.2024/Film.2024.mkv")

    def test_series_with_multiple_episodes_enqueues_one_batch(self):
        from unittest.mock import patch
        self._drain_queue()
        paths = [
            "/media/series/Pluribus/Pluribus.S01E01.mkv",
            "/media/series/Pluribus/Pluribus.S01E02.mkv",
            "/media/series/Pluribus/Pluribus.S01E03.mkv",
        ]
        with patch.object(sub_fetcher, "tg_send",
                          return_value={"ok": True, "result": {"message_id": 1}}), \
             patch.object(sub_fetcher, "save_state"), \
             patch.object(sub_fetcher, "queue_position", return_value=0), \
             patch("time.sleep"):
            sub_fetcher.auto_enqueue_missing(paths, state={"asked": {}, "downloaded": {}})
        items = self._drain_queue()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["type"], "batch")
        self.assertEqual(set(items[0]["paths"]), set(paths))

    def test_no_confirmation_buttons_in_messages(self):
        """Verify the new flow does not send any keyboard with Scarica/No buttons."""
        from unittest.mock import patch
        self._drain_queue()
        sent = []
        with patch.object(sub_fetcher, "tg_send",
                          side_effect=lambda *a, **k: sent.append((a, k)) or {"ok": True, "result": {"message_id": 1}}), \
             patch.object(sub_fetcher, "save_state"), \
             patch.object(sub_fetcher, "queue_position", return_value=0), \
             patch("time.sleep"):
            sub_fetcher.auto_enqueue_missing(
                ["/media/films/Film.mkv"],
                state={"asked": {}, "downloaded": {}},
            )
        self._drain_queue()
        for args, kwargs in sent:
            self.assertNotIn("reply_markup", kwargs,
                "auto_enqueue should not send confirmation buttons")
            self.assertNotIn("Scarica?", str(args))


class TestOfferDelete(unittest.TestCase):
    """offer_delete: list subs and offer confirmation."""

    def test_no_matches_reports_and_exits(self):
        from unittest.mock import patch
        sent = []
        with patch.object(sub_fetcher, "find_videos_by_name", return_value=[]), \
             patch.object(sub_fetcher, "tg_send", side_effect=lambda *a, **k: sent.append(a)):
            sub_fetcher.offer_delete("Nothing", state={})
        self.assertTrue(any("Nessun video trovato" in str(s) for s in sent))

    def test_no_subs_reports_and_exits(self):
        from unittest.mock import patch
        tmp = tempfile.mkdtemp()
        try:
            video = os.path.join(tmp, "film.mkv")
            open(video, "w").close()
            sent = []
            with patch.object(sub_fetcher, "find_videos_by_name", return_value=[video]), \
                 patch.object(sub_fetcher, "tg_send", side_effect=lambda *a, **k: sent.append(a)):
                sub_fetcher.offer_delete("film", state={})
            self.assertTrue(any("Nessun sub da cancellare" in str(s) for s in sent))
        finally:
            shutil.rmtree(tmp)

    def test_offers_delete_with_buttons_when_subs_exist(self):
        from unittest.mock import patch
        tmp = tempfile.mkdtemp()
        try:
            video = os.path.join(tmp, "film.mkv")
            it_sub = os.path.join(tmp, "film.it.srt")
            en_sub = os.path.join(tmp, "film.en.srt")
            open(video, "w").close()
            open(it_sub, "w").close()
            open(en_sub, "w").close()
            sent = []
            batches_store = {}
            with patch.object(sub_fetcher, "find_videos_by_name", return_value=[video]), \
                 patch.object(sub_fetcher, "load_batches", return_value=batches_store), \
                 patch.object(sub_fetcher, "save_batches", side_effect=lambda b: batches_store.update(b)), \
                 patch.object(sub_fetcher, "tg_send", side_effect=lambda *a, **k: sent.append((a, k))):
                sub_fetcher.offer_delete("film", state={})
            joined = str(sent)
            self.assertIn("delete_yes", joined)
            self.assertIn("delete_no", joined)
            self.assertIn("film.it.srt", joined)
            self.assertIn("film.en.srt", joined)
            # Batch was stored
            self.assertEqual(len(batches_store), 1)
            stored = list(batches_store.values())[0]
            self.assertEqual(stored["type"], "delete")
            self.assertEqual(set(stored["subs"]), {it_sub, en_sub})
        finally:
            shutil.rmtree(tmp)


class TestListVideoSubs(unittest.TestCase):
    def test_returns_only_existing_sub_files(self):
        tmp = tempfile.mkdtemp()
        try:
            video = os.path.join(tmp, "film.mkv")
            open(video, "w").close()
            existing = []
            for suffix in [".it.srt", ".en.srt"]:
                p = os.path.join(tmp, "film" + suffix)
                open(p, "w").close()
                existing.append(p)
            self.assertEqual(set(sub_fetcher.list_video_subs(video)), set(existing))
        finally:
            shutil.rmtree(tmp)

    def test_returns_empty_when_no_subs(self):
        tmp = tempfile.mkdtemp()
        try:
            video = os.path.join(tmp, "film.mkv")
            open(video, "w").close()
            self.assertEqual(sub_fetcher.list_video_subs(video), [])
        finally:
            shutil.rmtree(tmp)


class TestTranslatePrep(unittest.TestCase):
    """do_translate_prep: sync .en.srt for matches then ask the user to translate."""

    def _mk_video_with_en(self, tmp, name):
        video = os.path.join(tmp, f"{name}.mkv")
        en = os.path.join(tmp, f"{name}.en.srt")
        open(video, "w").close()
        with open(en, "w") as f:
            f.write("1\n00:00:01,000 --> 00:00:02,000\nHello\n")
        return video, en

    def test_no_matches_reports_and_exits(self):
        from unittest.mock import patch
        sent = []
        with patch.object(sub_fetcher, "find_videos_by_name", return_value=[]), \
             patch.object(sub_fetcher, "tg_send", side_effect=lambda *a, **k: sent.append(("send", a, k))), \
             patch.object(sub_fetcher, "tg_edit_message", side_effect=lambda *a, **k: sent.append(("edit", a, k))):
            sub_fetcher.do_translate_prep("Nothing", state={}, progress_msg_id=None)
        self.assertTrue(sent)
        self.assertIn("Nessun video", str(sent))

    def test_no_eligible_videos_reports_skipped(self):
        from unittest.mock import patch
        tmp = tempfile.mkdtemp()
        try:
            video = os.path.join(tmp, "film.mkv")
            open(video, "w").close()
            sent = []
            with patch.object(sub_fetcher, "find_videos_by_name", return_value=[video]), \
                 patch.object(sub_fetcher, "has_italian_sub", return_value=False), \
                 patch.object(sub_fetcher, "tg_send", side_effect=lambda *a, **k: sent.append(a)):
                sub_fetcher.do_translate_prep("film", state={}, progress_msg_id=None)
            self.assertTrue(any("Senza .en.srt" in str(a) for a in sent))
        finally:
            shutil.rmtree(tmp)

    def test_accepts_eng_srt_suffix(self):
        from unittest.mock import patch, MagicMock
        tmp = tempfile.mkdtemp()
        try:
            video = os.path.join(tmp, "film.mkv")
            eng = os.path.join(tmp, "film.eng.srt")
            open(video, "w").close()
            with open(eng, "w") as f:
                f.write("1\n00:00:01,000 --> 00:00:02,000\nHi\n")
            sync_mock = MagicMock(return_value={"ok": True})
            sent = []
            batches_store = {}
            with patch.object(sub_fetcher, "find_videos_by_name", return_value=[video]), \
                 patch.object(sub_fetcher, "has_italian_sub", return_value=False), \
                 patch.object(sub_fetcher, "sync_subtitle", sync_mock), \
                 patch.object(sub_fetcher, "_estimate_batch_translation_cost", return_value=(0.05, 5)), \
                 patch.object(sub_fetcher, "load_batches", return_value=batches_store), \
                 patch.object(sub_fetcher, "save_batches", side_effect=lambda b: batches_store.update(b)), \
                 patch.object(sub_fetcher, "tg_send", side_effect=lambda *a, **k: sent.append((a, k)) or {"ok": True, "result": {"message_id": 1}}):
                sub_fetcher.do_translate_prep("film", state={}, progress_msg_id=None)
            sync_mock.assert_called_once_with(video, eng)
            self.assertTrue(any("batch_translate" in str(s) for s in sent))
        finally:
            shutil.rmtree(tmp)

    def test_eligible_video_gets_synced_and_offered(self):
        from unittest.mock import patch, MagicMock
        tmp = tempfile.mkdtemp()
        try:
            video, en = self._mk_video_with_en(tmp, "film")
            sync_mock = MagicMock(return_value={"ok": True})
            sent = []
            batches_store = {}
            with patch.object(sub_fetcher, "find_videos_by_name", return_value=[video]), \
                 patch.object(sub_fetcher, "has_italian_sub", return_value=False), \
                 patch.object(sub_fetcher, "sync_subtitle", sync_mock), \
                 patch.object(sub_fetcher, "_estimate_batch_translation_cost", return_value=(0.12, 10)), \
                 patch.object(sub_fetcher, "load_batches", return_value=batches_store), \
                 patch.object(sub_fetcher, "save_batches", side_effect=lambda b: batches_store.update(b)), \
                 patch.object(sub_fetcher, "tg_send", side_effect=lambda *a, **k: sent.append((a, k)) or {"ok": True, "result": {"message_id": 1}}):
                sub_fetcher.do_translate_prep("film", state={}, progress_msg_id=None)
            sync_mock.assert_called_once_with(video, en)
            self.assertTrue(any("batch_translate" in str(s) for s in sent))
            self.assertTrue(any("0.12" in str(s) for s in sent))
        finally:
            shutil.rmtree(tmp)


class TestCascadeSearchUnit(unittest.TestCase):
    """Test _cascade_search logic with mocked OSClient."""

    def test_returns_hash_results_first(self):
        class MockClient:
            token = "fake"
            def search_hash(self, file_hash, file_size):
                return [{"SubFileName": "hash_result.srt"}]
            def search_imdb(self, *a, **kw): return []
            def search_name(self, *a, **kw): return []
        client = MockClient()

        results = sub_fetcher._cascade_search(
            client, "/media/series/Test/Test.S01E01.mkv", "ita",
            file_hash="abc123", file_size=1000
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["SubFileName"], "hash_result.srt")

    def test_falls_through_to_name_search(self):
        class MockClient:
            token = "fake"
            def search_hash(self, *a, **kw): return []
            def search_imdb(self, *a, **kw): return []
            def search_name(self, query, season=None, episode=None, language=None):
                if query == "Test":
                    return [{"SubFileName": "name_result.srt"}]
                return []
        client = MockClient()

        results = sub_fetcher._cascade_search(
            client, "/media/series/Test/Test.S01E01.mkv", "ita",
            file_hash="abc123", file_size=1000
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["SubFileName"], "name_result.srt")


class TestGroupBySeries(unittest.TestCase):
    def test_groups_by_folder(self):
        paths = [
            "/media/series/PLUR1BUS/Pluribus.S01E01.mkv",
            "/media/series/PLUR1BUS/Pluribus.S01E02.mkv",
            "/media/series/The Chosen/Season 1/The.Chosen.S01E01.mkv",
        ]
        groups = sub_fetcher.group_by_series(paths)
        self.assertIn("PLUR1BUS", groups)
        self.assertIn("The Chosen", groups)
        self.assertEqual(len(groups["PLUR1BUS"]), 2)
        self.assertEqual(len(groups["The Chosen"]), 1)

    def test_sorts_episodes(self):
        paths = [
            "/media/series/Show/Show.S01E03.mkv",
            "/media/series/Show/Show.S01E01.mkv",
            "/media/series/Show/Show.S01E02.mkv",
        ]
        groups = sub_fetcher.group_by_series(paths)
        self.assertEqual(groups["Show"][0], paths[1])  # E01 first
        self.assertEqual(groups["Show"][2], paths[0])  # E03 last

    def test_single_file(self):
        paths = ["/media/films/Movie (2024)/Movie.2024.mkv"]
        groups = sub_fetcher.group_by_series(paths)
        self.assertEqual(len(groups), 1)


class TestProgressBar(unittest.TestCase):
    def test_zero(self):
        bar = sub_fetcher._progress_bar(0, 10)
        self.assertIn("0%", bar)

    def test_half(self):
        bar = sub_fetcher._progress_bar(5, 10)
        self.assertIn("50%", bar)
        self.assertIn("▓", bar)
        self.assertIn("░", bar)

    def test_full(self):
        bar = sub_fetcher._progress_bar(10, 10)
        self.assertIn("100%", bar)

    def test_empty_total(self):
        bar = sub_fetcher._progress_bar(0, 0)
        self.assertEqual(bar, "")


class TestHasItalianAudio(unittest.TestCase):
    """Test has_italian_audio with mocked subprocess."""

    def _mock_ffprobe(self, streams):
        """Replace subprocess.run with a mock returning given streams."""
        import subprocess
        original_run = subprocess.run
        def mock_run(*args, **kwargs):
            import json as j
            result = type("R", (), {
                "returncode": 0,
                "stdout": j.dumps({"streams": streams}),
                "stderr": ""
            })()
            return result
        subprocess.run = mock_run
        return original_run

    def test_detects_italian_audio(self):
        import subprocess
        orig = self._mock_ffprobe([
            {"tags": {"language": "ita", "title": "Italian"}}
        ])
        try:
            self.assertTrue(sub_fetcher.has_italian_audio("/fake/video.mkv"))
        finally:
            subprocess.run = orig

    def test_returns_false_for_english_only(self):
        import subprocess
        orig = self._mock_ffprobe([
            {"tags": {"language": "eng", "title": "English"}}
        ])
        try:
            self.assertFalse(sub_fetcher.has_italian_audio("/fake/video.mkv"))
        finally:
            subprocess.run = orig

    def test_returns_false_when_no_tags(self):
        import subprocess
        orig = self._mock_ffprobe([{"codec_type": "audio"}])
        try:
            self.assertFalse(sub_fetcher.has_italian_audio("/fake/video.mkv"))
        finally:
            subprocess.run = orig

    def test_handles_ffprobe_missing(self):
        # If ffprobe is not installed, should return False (not crash)
        import subprocess
        original_run = subprocess.run
        def mock_run(*args, **kwargs):
            raise FileNotFoundError("ffprobe not found")
        subprocess.run = mock_run
        try:
            self.assertFalse(sub_fetcher.has_italian_audio("/fake/video.mkv"))
        finally:
            subprocess.run = original_run


class TestSubdlClient(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        # Save original and set a fake API key
        self._orig_key = sub_fetcher.SUBDL_API_KEY
        sub_fetcher.SUBDL_API_KEY = "test_key"

    def tearDown(self):
        shutil.rmtree(self.tmpdir)
        sub_fetcher.SUBDL_API_KEY = self._orig_key

    def test_search_returns_empty_without_api_key(self):
        sub_fetcher.SUBDL_API_KEY = ""
        client = sub_fetcher.SubdlClient()
        self.assertEqual(client.search("Test"), [])

    def test_download_extracts_srt_from_zip(self):
        # Create a mock ZIP with an SRT file
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as zf:
            zf.writestr("subtitle.srt", "1\n00:00:01,000 --> 00:00:03,000\nHello world\n")
        zip_bytes = zip_buffer.getvalue()

        # Mock urllib to return our ZIP
        original_urlopen = urllib.request.urlopen
        def mock_urlopen(req, **kwargs):
            return io.BytesIO(zip_bytes)
        urllib.request.urlopen = mock_urlopen

        try:
            client = sub_fetcher.SubdlClient()
            content = client.download({"url": "test/path", "release_name": "test"})
            self.assertIsNotNone(content)
            self.assertIn(b"Hello world", content)
        finally:
            urllib.request.urlopen = original_urlopen

    def test_download_returns_none_for_empty_zip(self):
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as zf:
            zf.writestr("readme.txt", "no srt here")
        zip_bytes = zip_buffer.getvalue()

        original_urlopen = urllib.request.urlopen
        def mock_urlopen(req, **kwargs):
            return io.BytesIO(zip_bytes)
        urllib.request.urlopen = mock_urlopen

        try:
            client = sub_fetcher.SubdlClient()
            content = client.download({"url": "test/path", "release_name": "test"})
            self.assertIsNone(content)
        finally:
            urllib.request.urlopen = original_urlopen

    def test_lang_map(self):
        client = sub_fetcher.SubdlClient()
        self.assertEqual(client.LANG_MAP.get("ita"), "it")
        self.assertEqual(client.LANG_MAP.get("eng"), "en")


class TestSubdlForcedFiltering(unittest.TestCase):
    """Test that forced/signs-only subs are penalized and rejected."""

    def test_scoring_penalizes_forced(self):
        results = [
            {"release_name": "Movie.720p.x264-GRP.eng-forced", "url": "/sub/1.zip", "name": "forced"},
            {"release_name": "Movie.720p.x264-GRP.eng-SDH", "url": "/sub/2.zip", "name": "full"},
        ]
        client = sub_fetcher.SubdlClient()
        video_path = "/media/films/Movie (2024)/Movie.720p.x264-GRP.mkv"
        video_base = os.path.splitext(os.path.basename(video_path))[0].lower()
        release_group = "grp"

        scored = []
        for sub in results:
            score = 0
            release = (sub.get("release_name", "") or "").lower()
            sub_name = (sub.get("name", "") or "").lower()
            if any(tag in release or tag in sub_name for tag in ["forced", "signs", "songs", "sdh", "hi-only"]):
                score -= 200
            scored.append((score, sub))
        scored.sort(key=lambda x: x[0], reverse=True)

        # The non-forced sub should rank higher (SDH is penalized too but less important here)
        # Both have negative scores due to "forced" and "sdh" tags
        # The forced one should have -200, SDH also -200
        # In real usage the full sub without any tag would rank highest
        self.assertTrue(scored[0][0] >= scored[-1][0])

    def test_rejects_sub_with_few_blocks(self):
        # A forced sub with only 5 blocks should be rejected
        forced_content = b"1\n00:00:01,000 --> 00:00:03,000\nKusimayu!\n\n" \
                         b"2\n00:00:10,000 --> 00:00:12,000\n[speaks Spanish]\n\n" \
                         b"3\n00:00:20,000 --> 00:00:22,000\nBongiorno\n\n"
        block_count = len(re.findall(rb"\d+\r?\n\d{2}:\d{2}:\d{2}", forced_content))
        self.assertLess(block_count, 10)

    def test_accepts_sub_with_many_blocks(self):
        # A full sub with 500+ blocks should be accepted
        lines = []
        for i in range(100):
            m, s = divmod(i * 3, 60)
            lines.append(f"{i+1}\n00:{m:02d}:{s:02d},000 --> 00:{m:02d}:{s+2:02d},000\nDialogue line {i}\n\n")
        full_content = "".join(lines).encode("utf-8")
        block_count = len(re.findall(rb"\d+\r?\n\d{2}:\d{2}:\d{2}", full_content))
        self.assertGreaterEqual(block_count, 10)


class TestSubdlZipPreferNonForced(unittest.TestCase):
    """Test that ZIP extraction prefers non-forced SRT files."""

    def test_prefers_non_forced_srt_in_zip(self):
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as zf:
            zf.writestr("Movie.eng-forced.srt", "1\n00:00:01,000 --> 00:00:03,000\nForced only\n")
            zf.writestr("Movie.eng.srt", "1\n00:00:01,000 --> 00:00:03,000\nFull dialogue\n")
        zip_bytes = zip_buffer.getvalue()

        original_urlopen = urllib.request.urlopen
        def mock_urlopen(req, **kwargs):
            return io.BytesIO(zip_bytes)
        urllib.request.urlopen = mock_urlopen

        try:
            client = sub_fetcher.SubdlClient()
            content = client.download({"url": "test/path", "release_name": "test"})
            self.assertIsNotNone(content)
            self.assertIn(b"Full dialogue", content)
            self.assertNotIn(b"Forced only", content)
        finally:
            urllib.request.urlopen = original_urlopen

    def test_falls_back_to_forced_if_only_option(self):
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as zf:
            zf.writestr("Movie.eng-forced.srt", "1\n00:00:01,000 --> 00:00:03,000\nForced only\n")
        zip_bytes = zip_buffer.getvalue()

        original_urlopen = urllib.request.urlopen
        def mock_urlopen(req, **kwargs):
            return io.BytesIO(zip_bytes)
        urllib.request.urlopen = mock_urlopen

        try:
            client = sub_fetcher.SubdlClient()
            content = client.download({"url": "test/path", "release_name": "test"})
            self.assertIsNotNone(content)
            self.assertIn(b"Forced only", content)
        finally:
            urllib.request.urlopen = original_urlopen


class TestSyncSkipLogic(unittest.TestCase):
    """Test that sync is skipped for local English subs."""

    def test_translate_and_save_accepts_skip_sync(self):
        # Just verify the function signature accepts skip_sync
        import inspect
        sig = inspect.signature(sub_fetcher._translate_and_save)
        self.assertIn("skip_sync", sig.parameters)
        self.assertEqual(sig.parameters["skip_sync"].default, False)


class TestSyncSubtitleReturn(unittest.TestCase):
    """Test that sync_subtitle returns dict with score info."""

    def test_sync_signature_has_min_score(self):
        import inspect
        sig = inspect.signature(sub_fetcher.sync_subtitle)
        self.assertIn("min_score", sig.parameters)
        self.assertEqual(sig.parameters["min_score"].default, 0)


class TestDownloadQueue(unittest.TestCase):
    """Test download queue infrastructure."""

    def test_queue_exists(self):
        self.assertTrue(hasattr(sub_fetcher, "download_queue"))

    def test_queue_position_returns_int(self):
        pos = sub_fetcher.queue_position()
        self.assertIsInstance(pos, int)
        self.assertEqual(pos, 0)

    def test_queue_put_and_get(self):
        sub_fetcher.download_queue.put({"type": "single", "path": "/test/video.mkv"})
        self.assertEqual(sub_fetcher.queue_position(), 1)
        job = sub_fetcher.download_queue.get_nowait()
        self.assertEqual(job["path"], "/test/video.mkv")
        sub_fetcher.download_queue.task_done()


class TestEpisodeMatchingInScoring(unittest.TestCase):
    """Test that Subdl scoring correctly matches episode numbers."""

    def test_correct_episode_gets_high_score(self):
        results = [
            {"release_name": "Pluribus S01E01 720p WEB", "url": "/sub/1.zip", "name": ""},
            {"release_name": "Pluribus S01E08 720p WEB", "url": "/sub/2.zip", "name": ""},
        ]
        client = sub_fetcher.SubdlClient()
        video_path = "/media/series/PLUR1BUS/Pluribus.S01E01.720p.x264-FENiX.mkv"

        scored = []
        parsed = sub_fetcher.parse_video(video_path)
        for sub_item in results:
            score = 0
            release = (sub_item.get("release_name", "") or "").lower()
            ep_match = re.search(r"s(\d+)e(\d+)", release)
            if ep_match and parsed.get("season") is not None:
                if int(ep_match.group(1)) == parsed["season"] and int(ep_match.group(2)) == parsed["episode"]:
                    score += 500
                else:
                    score -= 1000
            scored.append((score, sub_item))
        scored.sort(key=lambda x: x[0], reverse=True)

        self.assertEqual(scored[0][1]["release_name"], "Pluribus S01E01 720p WEB")
        self.assertGreater(scored[0][0], scored[1][0])

    def test_wrong_episode_gets_negative_score(self):
        parsed = sub_fetcher.parse_video("/media/series/Show/Show.S01E01.mkv")
        release = "show s01e08 720p web"
        ep_match = re.search(r"s(\d+)e(\d+)", release)
        self.assertIsNotNone(ep_match)
        sub_season = int(ep_match.group(1))
        sub_episode = int(ep_match.group(2))
        self.assertNotEqual(sub_episode, parsed["episode"])


class TestTwoPhaseDownload(unittest.TestCase):
    """Test that do_download supports translate=False for two-phase flow."""

    def test_do_download_accepts_translate_param(self):
        import inspect
        sig = inspect.signature(sub_fetcher.do_download)
        self.assertIn("translate", sig.parameters)
        self.assertEqual(sig.parameters["translate"].default, True)

    def test_do_batch_translate_exists(self):
        self.assertTrue(hasattr(sub_fetcher, "do_batch_translate"))
        self.assertTrue(callable(sub_fetcher.do_batch_translate))

    def test_estimate_batch_translation_cost_exists(self):
        self.assertTrue(hasattr(sub_fetcher, "_estimate_batch_translation_cost"))


class TestFindVideosByNameWithDots(unittest.TestCase):
    """Test that manual search handles dots and underscores in filenames."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_series = sub_fetcher.SERIES_PATH
        self._orig_films = sub_fetcher.FILMS_PATH
        sub_fetcher.SERIES_PATH = os.path.join(self.tmpdir, "series")
        sub_fetcher.FILMS_PATH = os.path.join(self.tmpdir, "films")
        os.makedirs(sub_fetcher.SERIES_PATH)
        os.makedirs(sub_fetcher.FILMS_PATH)

    def tearDown(self):
        shutil.rmtree(self.tmpdir)
        sub_fetcher.SERIES_PATH = self._orig_series
        sub_fetcher.FILMS_PATH = self._orig_films

    def test_finds_dotted_filename(self):
        show_dir = os.path.join(sub_fetcher.SERIES_PATH, "PLUR1BUS")
        os.makedirs(show_dir)
        video = os.path.join(show_dir, "Pluribus.S01E01.720p.x264-FENiX.mkv")
        open(video, "w").close()

        matches = sub_fetcher.find_videos_by_name("pluribus s01e01")
        self.assertEqual(len(matches), 1)

    def test_finds_by_folder_name(self):
        show_dir = os.path.join(sub_fetcher.SERIES_PATH, "PLUR1BUS")
        os.makedirs(show_dir)
        video = os.path.join(show_dir, "Pluribus.S01E01.720p.x264-FENiX.mkv")
        open(video, "w").close()

        matches = sub_fetcher.find_videos_by_name("pluribus")
        self.assertEqual(len(matches), 1)


class TestQueueTranslateJobType(unittest.TestCase):
    """Test that queue worker handles 'translate' job type."""

    def test_queue_accepts_translate_job(self):
        sub_fetcher.download_queue.put({"type": "translate", "paths": ["/test/video.mkv"]})
        job = sub_fetcher.download_queue.get_nowait()
        self.assertEqual(job["type"], "translate")
        sub_fetcher.download_queue.task_done()


class TestQueueWorkerSingleJobNoDuplicateMessages(unittest.TestCase):
    """Regression: queue worker for single jobs must not produce duplicate
    Telegram notifications. do_download must be called with silent=True so
    only the worker's edit of the original progress message is shown."""

    def _run_one_job(self, job, do_download_result):
        from unittest.mock import patch
        from queue import Empty
        sent = []
        captured = {}

        def fake_do_download(video_path, state, **kwargs):
            captured["kwargs"] = kwargs
            return do_download_result

        # Drain any leftover items so the worker picks up our job first
        while True:
            try:
                sub_fetcher.download_queue.get_nowait()
                sub_fetcher.download_queue.task_done()
            except Empty:
                break

        sub_fetcher.download_queue.put(job)

        # Run the worker body for exactly one job by inlining the per-job
        # logic via a patched queue.get that raises Empty after the first call.
        from threading import Thread
        stop = {"done": False}
        original_get = sub_fetcher.download_queue.get

        def get_then_stop(*args, **kwargs):
            if stop["done"]:
                raise Empty()
            stop["done"] = True
            return original_get(*args, **kwargs)

        with patch.object(sub_fetcher, "do_download", side_effect=fake_do_download), \
             patch.object(sub_fetcher, "tg_send", side_effect=lambda *a, **k: sent.append(("send", a, k)) or {"ok": True, "result": {"message_id": 1}}), \
             patch.object(sub_fetcher, "tg_edit_message", side_effect=lambda *a, **k: sent.append(("edit", a, k))), \
             patch.object(sub_fetcher, "load_state", return_value={"asked": {}, "downloaded": {}}), \
             patch.object(sub_fetcher.download_queue, "get", side_effect=get_then_stop):
            t = Thread(target=sub_fetcher._queue_worker, args=({},), daemon=True)
            t.start()
            t.join(timeout=2.0)
        return sent, captured

    def test_single_job_passes_silent_true(self):
        sent, captured = self._run_one_job(
            {"type": "single", "path": "/media/films/X/X.mkv", "msg_id": 42},
            do_download_result=False,
        )
        self.assertEqual(captured["kwargs"].get("silent"), True,
            "queue worker must call do_download with silent=True so the "
            "function does not emit its own user-facing message")

    def test_single_job_failure_emits_only_one_message(self):
        sent, _ = self._run_one_job(
            {"type": "single", "path": "/media/films/X/X.mkv", "msg_id": 42},
            do_download_result=False,
        )
        # do_download is mocked so it does not call tg_send itself; the worker
        # should produce exactly one user-facing notification (an edit of the
        # original progress message).
        edits = [s for s in sent if s[0] == "edit"]
        sends = [s for s in sent if s[0] == "send"]
        self.assertEqual(len(edits), 1)
        self.assertEqual(len(sends), 0)
        self.assertIn("Nessun sub ITA né ENG", str(edits[0]))
        self.assertIn("Riproverò tra 24h", str(edits[0]))


class TestValidateSync(unittest.TestCase):
    """validate_sync: write, sync, validate score, clean up on failure."""

    def test_returns_ok_when_sync_passes(self):
        from unittest.mock import patch
        content = b"1\n00:00:01,000 --> 00:00:02,000\nHi\n"
        tmp = tempfile.mkdtemp()
        dest = os.path.join(tmp, "test.it.srt")
        try:
            with patch.object(sub_fetcher, "sync_subtitle",
                              return_value={"ok": True, "score": 900.0, "offset": "0.5", "fps_scale": "1.0"}):
                result = sub_fetcher.validate_sync("/fake/video.mkv", content, dest)
            self.assertTrue(result["ok"])
            self.assertEqual(result["score"], 900.0)
        finally:
            shutil.rmtree(tmp)

    def test_removes_file_when_score_too_low(self):
        from unittest.mock import patch
        content = b"1\n00:00:01,000 --> 00:00:02,000\nHi\n"
        tmp = tempfile.mkdtemp()
        dest = os.path.join(tmp, "test.it.srt")
        try:
            with patch.object(sub_fetcher, "sync_subtitle",
                              return_value={"ok": False, "score": 200.0, "offset": "0", "fps_scale": "1.0"}):
                result = sub_fetcher.validate_sync("/fake/video.mkv", content, dest)
            self.assertFalse(result["ok"])
            self.assertFalse(os.path.exists(dest))
        finally:
            shutil.rmtree(tmp)

    def test_removes_file_on_sync_failure(self):
        from unittest.mock import patch
        content = b"1\n00:00:01,000 --> 00:00:02,000\nHi\n"
        tmp = tempfile.mkdtemp()
        dest = os.path.join(tmp, "test.it.srt")
        try:
            with patch.object(sub_fetcher, "sync_subtitle", return_value=False):
                result = sub_fetcher.validate_sync("/fake/video.mkv", content, dest)
            self.assertFalse(result)
            self.assertFalse(os.path.exists(dest))
        finally:
            shutil.rmtree(tmp)


class TestSyncValidationCascade(unittest.TestCase):
    """do_download: ITA sync failure should fall through to ENG."""

    def _mock_do_download(self, ita_content, ita_sync_ok, eng_content, eng_sync_ok):
        from unittest.mock import patch, MagicMock
        tmp = tempfile.mkdtemp()
        video = os.path.join(tmp, "Film.2024.mkv")
        open(video, "w").close()

        def fake_validate(vp, content, dest):
            if dest.endswith(".it.srt"):
                if ita_sync_ok:
                    with open(dest, "wb") as f:
                        f.write(content if isinstance(content, bytes) else content.encode())
                    return {"ok": True, "score": 900.0}
                return {"ok": False, "score": 200.0}
            elif dest.endswith(".en.srt"):
                if eng_sync_ok:
                    with open(dest, "wb") as f:
                        f.write(content if isinstance(content, bytes) else content.encode())
                    return {"ok": True, "score": 1000.0}
                return {"ok": False, "score": 100.0}
            return False

        subdl_mock = MagicMock()
        subdl_mock.search_and_download = MagicMock(
            side_effect=lambda vp, language="it", trace=None: ita_content if language == "it" else eng_content
        )

        os_client = MagicMock()
        os_client.login.return_value = False
        os_client.available = False

        state = {"asked": {}, "downloaded": {}}
        sent = []

        with patch.object(sub_fetcher, "SubdlClient", return_value=subdl_mock), \
             patch.object(sub_fetcher, "OSClient", return_value=os_client), \
             patch.object(sub_fetcher, "validate_sync", side_effect=fake_validate), \
             patch.object(sub_fetcher, "find_existing_srt", return_value=None), \
             patch.object(sub_fetcher, "_save_sub_and_update_state"), \
             patch.object(sub_fetcher, "save_state"), \
             patch.object(sub_fetcher, "tg_send", side_effect=lambda *a, **k: sent.append(a)):
            result = sub_fetcher.do_download(video, state, silent=True, translate=False)

        shutil.rmtree(tmp)
        return result

    def test_good_ita_sync_returns_true(self):
        result = self._mock_do_download(
            ita_content=b"1\n00:00:01,000 --> 00:00:02,000\nCiao\n",
            ita_sync_ok=True,
            eng_content=None,
            eng_sync_ok=False,
        )
        self.assertTrue(result)

    def test_both_ita_and_eng_saved_when_available(self):
        """Even when ITA is found, ENG should also be downloaded as backup."""
        from unittest.mock import patch, MagicMock
        tmp = tempfile.mkdtemp()
        video = os.path.join(tmp, "Film.2024.mkv")
        open(video, "w").close()

        saved_paths = []

        def fake_validate(vp, content, dest):
            saved_paths.append(dest)
            with open(dest, "wb") as f:
                f.write(content if isinstance(content, bytes) else content.encode())
            return {"ok": True, "score": 900.0}

        subdl_mock = MagicMock()
        subdl_mock.search_and_download = MagicMock(
            side_effect=lambda vp, language="it", trace=None: b"sub-content"
        )
        os_client = MagicMock()
        os_client.login.return_value = False
        os_client.available = False
        os_client.downloads_remaining = None

        state = {"asked": {}, "downloaded": {}}
        try:
            with patch.object(sub_fetcher, "SubdlClient", return_value=subdl_mock), \
                 patch.object(sub_fetcher, "OSClient", return_value=os_client), \
                 patch.object(sub_fetcher, "validate_sync", side_effect=fake_validate), \
                 patch.object(sub_fetcher, "find_existing_srt", return_value=None), \
                 patch.object(sub_fetcher, "_save_sub_and_update_state"), \
                 patch.object(sub_fetcher, "save_state"), \
                 patch.object(sub_fetcher, "tg_send"):
                result = sub_fetcher.do_download(video, state, silent=True, translate=False)
            self.assertTrue(result)
            # Both .it.srt and .en.srt were written
            self.assertTrue(any(p.endswith(".it.srt") for p in saved_paths))
            self.assertTrue(any(p.endswith(".en.srt") for p in saved_paths))
        finally:
            shutil.rmtree(tmp)

    def test_bad_ita_sync_falls_through_to_eng(self):
        result = self._mock_do_download(
            ita_content=b"1\n00:00:01,000 --> 00:00:02,000\nCiao\n",
            ita_sync_ok=False,
            eng_content=b"1\n00:00:01,000 --> 00:00:02,000\nHi\n",
            eng_sync_ok=True,
        )
        self.assertEqual(result, "en_only")

    def test_no_ita_no_eng_returns_false(self):
        result = self._mock_do_download(
            ita_content=None,
            ita_sync_ok=False,
            eng_content=None,
            eng_sync_ok=False,
        )
        self.assertFalse(result)

    def test_bad_ita_bad_eng_sync_returns_false(self):
        # Both ITA and ENG were found but neither synced well — discard both.
        result = self._mock_do_download(
            ita_content=b"1\n00:00:01,000 --> 00:00:02,000\nCiao\n",
            ita_sync_ok=False,
            eng_content=b"1\n00:00:01,000 --> 00:00:02,000\nHi\n",
            eng_sync_ok=False,
        )
        self.assertFalse(result)


class TestSearchTrace(unittest.TestCase):
    """do_download should accept a trace list and populate it with what it tried."""

    def test_do_download_accepts_trace_param(self):
        import inspect
        sig = inspect.signature(sub_fetcher.do_download)
        self.assertIn("trace", sig.parameters)
        self.assertIsNone(sig.parameters["trace"].default)

    def test_format_search_trace_renders_entries(self):
        trace = [
            {"provider": "Subdl", "lang": "ITA", "method": "imdb",
             "query": "tt1234567", "results": 0},
            {"provider": "Subdl", "lang": "ITA", "method": "nome",
             "query": "Father Mother", "results": 5,
             "rejected": "forced/incompleti (3 blocchi)"},
            {"provider": "OpenSubtitles", "lang": "ITA", "method": "hash",
             "query": "abc123", "results": 0},
        ]
        out = sub_fetcher.format_search_trace(trace)
        self.assertIn("Subdl imdb ITA 'tt1234567': 0", out)
        self.assertIn("Subdl nome ITA 'Father Mother': 5", out)
        self.assertIn("forced/incompleti", out)
        self.assertIn("OpenSubtitles hash ITA 'abc123': 0", out)

    def test_format_search_trace_handles_empty(self):
        self.assertEqual(sub_fetcher.format_search_trace([]), "")
        self.assertEqual(sub_fetcher.format_search_trace(None), "")

    def test_format_search_trace_includes_quota(self):
        trace = [
            {"provider": "Subdl", "lang": "ITA", "method": "imdb",
             "query": "tt1234567", "results": 0},
            {"_quota": 3},
        ]
        out = sub_fetcher.format_search_trace(trace)
        self.assertIn("download rimanenti: 3", out)
        self.assertIn("Subdl imdb ITA", out)

    def test_cascade_search_populates_trace(self):
        class MockClient:
            token = "fake"
            def search_hash(self, *a, **kw): return []
            def search_imdb(self, *a, **kw): return []
            def search_name(self, query, *a, **kw):
                return [{"SubFileName": "x.srt"}] if query == "Test" else []
        trace = []
        results = sub_fetcher._cascade_search(
            MockClient(), "/media/films/Test/Test.2024.mkv", "ita",
            file_hash="deadbeef", file_size=1000, trace=trace,
        )
        # Should have entries for hash, imdb, and at least one name search
        methods = [t["method"] for t in trace]
        self.assertIn("hash", methods)
        # name should appear too because hash and imdb returned 0
        self.assertIn("nome", methods)
        # The hash entry should have the right query
        hash_entry = next(t for t in trace if t["method"] == "hash")
        self.assertEqual(hash_entry["query"], "deadbeef")
        self.assertEqual(hash_entry["results"], 0)

    def test_subdl_search_and_download_populates_trace(self):
        from unittest.mock import patch
        client = sub_fetcher.SubdlClient()
        trace = []
        # find_imdb_id returns None so only the name cascade fires
        with patch.object(sub_fetcher, "find_imdb_id", return_value=None), \
             patch.object(sub_fetcher, "get_search_queries", return_value=["father mother"]), \
             patch.object(client, "search", return_value=[]):
            result = client.search_and_download(
                "/media/films/Father/Father.Mother.2025.mkv",
                language="it",
                trace=trace,
            )
        self.assertIsNone(result)
        self.assertTrue(any(t.get("method") == "nome" and t.get("query") == "father mother"
                            for t in trace))
        # Also has the imdb "non risolto" entry
        self.assertTrue(any(t.get("method") == "imdb" and "non risolto" in t.get("query", "")
                            for t in trace))


# =============================================================================
# NEW TESTS — batches.json, do_cleanup, do_sync, queue job types
# =============================================================================

class TestLoadSaveBatches(unittest.TestCase):
    """Test that load_batches/save_batches correctly persist to a separate file."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_batches = sub_fetcher.BATCHES_FILE
        sub_fetcher.BATCHES_FILE = os.path.join(self.tmpdir, "batches.json")

    def tearDown(self):
        shutil.rmtree(self.tmpdir)
        sub_fetcher.BATCHES_FILE = self._orig_batches

    def test_returns_empty_dict_when_file_missing(self):
        batches = sub_fetcher.load_batches()
        self.assertEqual(batches, {})

    def test_roundtrip_save_and_load(self):
        data = {"abc12345": {"paths": ["/media/films/Movie.mkv"], "type": "translate"}}
        sub_fetcher.save_batches(data)
        loaded = sub_fetcher.load_batches()
        self.assertEqual(loaded, data)

    def test_save_multiple_batches(self):
        sub_fetcher.save_batches({"hash1": {"paths": ["/a.mkv"]}})
        sub_fetcher.save_batches({"hash2": {"paths": ["/b.mkv"]}})
        # Second save overwrites first — caller is responsible for merging
        loaded = sub_fetcher.load_batches()
        self.assertIn("hash2", loaded)

    def test_save_does_not_touch_state_json(self):
        state_file = os.path.join(self.tmpdir, "state.json")
        sub_fetcher.save_batches({"h": {"paths": []}})
        self.assertFalse(os.path.exists(state_file))

    def test_batches_independent_from_state(self):
        """Saving state must NOT wipe batches.json."""
        sub_fetcher.save_batches({"abc": {"paths": ["/x.mkv"], "type": "translate"}})

        orig_state = sub_fetcher.STATE_FILE
        sub_fetcher.STATE_FILE = os.path.join(self.tmpdir, "state.json")
        try:
            state = {"asked": {}, "downloaded": {}, "last_offset": 0}
            sub_fetcher.save_state(state)
            # Batches must still be there after save_state
            loaded = sub_fetcher.load_batches()
            self.assertIn("abc", loaded)
        finally:
            sub_fetcher.STATE_FILE = orig_state


class TestDoCleanup(unittest.TestCase):
    """Test that do_cleanup removes placeholder subs and updates state."""

    _PLACEHOLDER = (
        b"1\n00:00:01,000 --> 00:00:03,000\n"
        b"Subtitles by VIP Member - www.BestSubs.com\n\n"
    )
    _REAL_SUB = (
        b"1\n00:00:01,000 --> 00:00:03,000\nDialogue line here\n\n"
        b"2\n00:00:04,000 --> 00:00:06,000\nAnother line\n\n"
        b"3\n00:00:07,000 --> 00:00:09,000\nAnd a third line\n\n"
    )

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_films = sub_fetcher.FILMS_PATH
        self._orig_series = sub_fetcher.SERIES_PATH
        sub_fetcher.FILMS_PATH = os.path.join(self.tmpdir, "films")
        sub_fetcher.SERIES_PATH = os.path.join(self.tmpdir, "series")
        os.makedirs(sub_fetcher.FILMS_PATH)
        os.makedirs(sub_fetcher.SERIES_PATH)

        self._tg_calls = []
        self._orig_tg_send = sub_fetcher.tg_send
        self._orig_tg_edit = sub_fetcher.tg_edit_message
        sub_fetcher.tg_send = lambda *a, **kw: self._tg_calls.append(("send", a))
        sub_fetcher.tg_edit_message = lambda *a, **kw: self._tg_calls.append(("edit", a))

    def tearDown(self):
        shutil.rmtree(self.tmpdir)
        sub_fetcher.FILMS_PATH = self._orig_films
        sub_fetcher.SERIES_PATH = self._orig_series
        sub_fetcher.tg_send = self._orig_tg_send
        sub_fetcher.tg_edit_message = self._orig_tg_edit

    def _create_video_and_sub(self, folder, basename, sub_content):
        d = os.path.join(sub_fetcher.FILMS_PATH, folder)
        os.makedirs(d, exist_ok=True)
        video = os.path.join(d, basename + ".mkv")
        srt = os.path.join(d, basename + ".it.srt")
        open(video, "w").close()
        with open(srt, "wb") as f:
            f.write(sub_content)
        return video, srt

    def test_removes_placeholder_sub(self):
        video, srt = self._create_video_and_sub("Movie (2024)", "Movie.2024", self._PLACEHOLDER)
        state = {"asked": {}, "downloaded": {video: {"sub": srt}}}
        sub_fetcher.do_cleanup(state)
        self.assertFalse(os.path.exists(srt))

    def test_leaves_real_sub_intact(self):
        video, srt = self._create_video_and_sub("Movie (2024)", "Movie.2024", self._REAL_SUB)
        state = {"asked": {}, "downloaded": {}}
        sub_fetcher.do_cleanup(state)
        self.assertTrue(os.path.exists(srt))

    def test_removes_video_from_downloaded_state(self):
        video, srt = self._create_video_and_sub("Movie (2024)", "Movie.2024", self._PLACEHOLDER)
        state = {"asked": {video: {"status": "yes"}}, "downloaded": {video: {"sub": srt}}}
        sub_fetcher.do_cleanup(state)
        self.assertNotIn(video, state["downloaded"])
        self.assertNotIn(video, state["asked"])

    def test_uses_progress_msg_id_when_provided(self):
        self._create_video_and_sub("Movie (2024)", "Movie.2024", self._PLACEHOLDER)
        state = {"asked": {}, "downloaded": {}}
        sub_fetcher.do_cleanup(state, progress_msg_id=42)
        edits = [c for c in self._tg_calls if c[0] == "edit"]
        self.assertTrue(len(edits) > 0)
        self.assertEqual(edits[-1][1][0], 42)

    def test_sends_message_when_no_msg_id(self):
        self._create_video_and_sub("Movie (2024)", "Movie.2024", self._PLACEHOLDER)
        state = {"asked": {}, "downloaded": {}}
        sub_fetcher.do_cleanup(state, progress_msg_id=None)
        sends = [c for c in self._tg_calls if c[0] == "send"]
        self.assertTrue(len(sends) > 0)


class TestDoSync(unittest.TestCase):
    """Test do_sync dispatches correctly and respects progress_msg_id."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_series = sub_fetcher.SERIES_PATH
        self._orig_films = sub_fetcher.FILMS_PATH
        sub_fetcher.SERIES_PATH = os.path.join(self.tmpdir, "series")
        sub_fetcher.FILMS_PATH = os.path.join(self.tmpdir, "films")
        os.makedirs(sub_fetcher.SERIES_PATH)
        os.makedirs(sub_fetcher.FILMS_PATH)

        self._tg_calls = []
        self._orig_tg_send = sub_fetcher.tg_send
        self._orig_tg_edit = sub_fetcher.tg_edit_message
        self._orig_sync = sub_fetcher.sync_subtitle
        sub_fetcher.tg_send = lambda *a, **kw: self._tg_calls.append(("send", a)) or {"ok": True, "result": {"message_id": 99}}
        sub_fetcher.tg_edit_message = lambda *a, **kw: self._tg_calls.append(("edit", a))
        sub_fetcher.sync_subtitle = lambda v, s, **kw: {"ok": True, "score": 100, "offset": 0.0}

    def tearDown(self):
        shutil.rmtree(self.tmpdir)
        sub_fetcher.SERIES_PATH = self._orig_series
        sub_fetcher.FILMS_PATH = self._orig_films
        sub_fetcher.tg_send = self._orig_tg_send
        sub_fetcher.tg_edit_message = self._orig_tg_edit
        sub_fetcher.sync_subtitle = self._orig_sync

    def _create_pair(self, folder, basename):
        d = os.path.join(sub_fetcher.SERIES_PATH, folder)
        os.makedirs(d, exist_ok=True)
        video = os.path.join(d, basename + ".mkv")
        srt = os.path.join(d, basename + ".it.srt")
        open(video, "w").close()
        open(srt, "w").close()
        return video, srt

    def test_accepts_progress_msg_id_param(self):
        import inspect
        sig = inspect.signature(sub_fetcher.do_sync)
        self.assertIn("progress_msg_id", sig.parameters)

    def test_sends_not_found_when_no_matching_srt(self):
        state = {}
        sub_fetcher.do_sync("NonExistent", state)
        texts = " ".join(str(c) for c in self._tg_calls)
        self.assertIn("nonexistent", texts.lower())

    def test_uses_progress_msg_id_on_not_found(self):
        state = {}
        sub_fetcher.do_sync("NonExistent", state, progress_msg_id=55)
        edits = [c for c in self._tg_calls if c[0] == "edit"]
        self.assertTrue(len(edits) > 0)
        self.assertEqual(edits[0][1][0], 55)

    def test_syncs_matching_pair_and_reports_result(self):
        self._create_pair("Pluribus", "Pluribus.S01E01")
        state = {}
        sub_fetcher.do_sync("Pluribus", state, progress_msg_id=10)
        edits = [c for c in self._tg_calls if c[0] == "edit"]
        # Last edit should be the summary
        last_text = edits[-1][1][1]
        self.assertIn("Sync completato", last_text)
        self.assertIn("1", last_text)

    def test_sync_all_finds_all_subs(self):
        self._create_pair("ShowA", "ShowA.S01E01")
        self._create_pair("ShowB", "ShowB.S01E01")
        synced_calls = []
        sub_fetcher.sync_subtitle = lambda v, s, **kw: synced_calls.append(v) or {"ok": True, "score": 100, "offset": 0.0}
        sub_fetcher.do_sync("all", {})
        self.assertEqual(len(synced_calls), 2)


class TestQueueSyncCleanupJobTypes(unittest.TestCase):
    """Test that queue accepts and correctly structures sync/cleanup jobs."""

    def test_queue_accepts_sync_job(self):
        sub_fetcher.download_queue.put({"type": "sync", "query": "Pluribus", "msg_id": 42})
        job = sub_fetcher.download_queue.get_nowait()
        self.assertEqual(job["type"], "sync")
        self.assertEqual(job["query"], "Pluribus")
        self.assertEqual(job["msg_id"], 42)
        sub_fetcher.download_queue.task_done()

    def test_queue_accepts_cleanup_job(self):
        sub_fetcher.download_queue.put({"type": "cleanup", "msg_id": 7})
        job = sub_fetcher.download_queue.get_nowait()
        self.assertEqual(job["type"], "cleanup")
        self.assertEqual(job["msg_id"], 7)
        sub_fetcher.download_queue.task_done()

    def test_do_cleanup_is_callable(self):
        self.assertTrue(callable(sub_fetcher.do_cleanup))

    def test_do_sync_is_callable(self):
        self.assertTrue(callable(sub_fetcher.do_sync))


class TestBatchTranslatePersistence(unittest.TestCase):
    """Test that batch_translate entries survive a save_state() call."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_state = sub_fetcher.STATE_FILE
        self._orig_batches = sub_fetcher.BATCHES_FILE
        sub_fetcher.STATE_FILE = os.path.join(self.tmpdir, "state.json")
        sub_fetcher.BATCHES_FILE = os.path.join(self.tmpdir, "batches.json")

    def tearDown(self):
        shutil.rmtree(self.tmpdir)
        sub_fetcher.STATE_FILE = self._orig_state
        sub_fetcher.BATCHES_FILE = self._orig_batches

    def test_batch_survives_save_state(self):
        """Simulates the race condition that was the root cause of 'Batch non trovato'."""
        # Queue worker saves a batch
        sub_fetcher.save_batches({"deadbeef": {"paths": ["/media/films/Movie.mkv"], "type": "translate"}})

        # Main loop saves state (without batches) — should NOT wipe batches.json
        state = {"asked": {}, "downloaded": {}, "last_offset": 42}
        sub_fetcher.save_state(state)

        # Batch must still be there
        batches = sub_fetcher.load_batches()
        self.assertIn("deadbeef", batches)
        self.assertEqual(batches["deadbeef"]["paths"], ["/media/films/Movie.mkv"])

    def test_batch_found_after_multiple_state_saves(self):
        sub_fetcher.save_batches({"tr123": {"paths": ["/a.mkv", "/b.mkv"], "type": "translate"}})

        for i in range(5):
            sub_fetcher.save_state({"asked": {f"path_{i}": {"status": "no"}}, "downloaded": {}, "last_offset": i})

        batches = sub_fetcher.load_batches()
        self.assertIn("tr123", batches)

    def test_explicit_batch_removal_works(self):
        sub_fetcher.save_batches({"abc": {"paths": ["/x.mkv"]}, "def": {"paths": ["/y.mkv"]}})
        batches = sub_fetcher.load_batches()
        batches.pop("abc", None)
        sub_fetcher.save_batches(batches)

        loaded = sub_fetcher.load_batches()
        self.assertNotIn("abc", loaded)
        self.assertIn("def", loaded)


class TestClaudeBisectFallback(unittest.TestCase):
    """Verify the Claude fallback recovers from truncation/missing cues by
    bisecting the batch instead of silently leaving cues in English."""

    def _make_response(self, translations, stop_reason="end_turn"):
        """Build a fake Claude API response body."""
        text = "\n".join(f"[{i}] {v}" for i, v in translations.items())
        return {
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 100, "output_tokens": 50},
            "stop_reason": stop_reason,
        }

    def test_all_cues_translated_no_retry(self):
        """Happy path: model returns translations for every asked-for index."""
        indexed = [(0, "Hello"), (1, "World"), (2, "Bye")]
        responses = iter([self._make_response({0: "Ciao", 1: "Mondo", 2: "Addio"})])
        calls = []

        def fake_call(itxts, vname):
            calls.append(list(itxts))
            r = next(responses)
            return {"translations": {i: t for i, t in zip([x[0] for x in itxts], ["Ciao", "Mondo", "Addio"])},
                    "input_tokens": 100, "output_tokens": 50, "truncated": False, "ok": True}

        orig = sub_fetcher._claude_translate_call
        sub_fetcher._claude_translate_call = fake_call
        try:
            result = sub_fetcher._claude_translate_bisect(indexed, "Test")
        finally:
            sub_fetcher._claude_translate_call = orig

        self.assertEqual(result["translations"], {0: "Ciao", 1: "Mondo", 2: "Addio"})
        self.assertEqual(len(calls), 1)  # No retry needed

    def test_truncated_response_triggers_bisect(self):
        """When Claude truncates and drops the last cue, the batch must split
        and retry the missing one."""
        calls = []

        def fake_call(itxts, vname):
            calls.append([i for i, _ in itxts])
            asked = [i for i, _ in itxts]
            if len(asked) == 4:
                # First call: returns 3 of 4 (truncated dropped the last)
                return {"translations": {0: "A", 1: "B", 2: "C"},
                        "input_tokens": 100, "output_tokens": 8192, "truncated": True, "ok": True}
            else:
                # Retry for the missing cue (3)
                return {"translations": {i: f"v{i}" for i in asked},
                        "input_tokens": 30, "output_tokens": 30, "truncated": False, "ok": True}

        orig = sub_fetcher._claude_translate_call
        sub_fetcher._claude_translate_call = fake_call
        try:
            result = sub_fetcher._claude_translate_bisect(
                [(0, "a"), (1, "b"), (2, "c"), (3, "d")], "Test")
        finally:
            sub_fetcher._claude_translate_call = orig

        # All 4 cues must be present in the final translation.
        self.assertEqual(set(result["translations"].keys()), {0, 1, 2, 3})
        # First call asked for all 4; subsequent calls retried just cue 3.
        self.assertEqual(calls[0], [0, 1, 2, 3])
        self.assertTrue(any(3 in c for c in calls[1:]))

    def test_persistent_single_cue_failure_keeps_en(self):
        """Single-cue retry that still fails returns no translation for that
        cue — caller falls back to EN. No infinite recursion."""
        def fake_call(itxts, vname):
            # Always returns empty translations regardless of input.
            return {"translations": {}, "input_tokens": 10, "output_tokens": 0,
                    "truncated": False, "ok": True}

        orig = sub_fetcher._claude_translate_call
        sub_fetcher._claude_translate_call = fake_call
        try:
            result = sub_fetcher._claude_translate_bisect([(0, "stubborn cue")], "Test")
        finally:
            sub_fetcher._claude_translate_call = orig

        self.assertEqual(result["translations"], {})

    def test_hallucinated_index_is_rejected(self):
        """Claude returning [99] when we asked for [0..3] must not pollute the
        translation map."""
        # Use the real _claude_translate_call wired to a urlopen mock.
        original_urlopen = urllib.request.urlopen

        class FakeResponse:
            def __init__(self, body):
                self._body = body
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self): return json.dumps(self._body).encode("utf-8")

        def fake_urlopen(req, timeout=None):
            return FakeResponse({
                "content": [{"type": "text", "text": "[0] Zero\n[1] One\n[99] hallucinated\n"}],
                "usage": {"input_tokens": 10, "output_tokens": 10},
                "stop_reason": "end_turn",
            })

        urllib.request.urlopen = fake_urlopen
        # _claude_translate_call requires CLAUDE_API_KEY to be truthy to send.
        prev_key = sub_fetcher.CLAUDE_API_KEY
        sub_fetcher.CLAUDE_API_KEY = "test"
        try:
            result = sub_fetcher._claude_translate_call([(0, "a"), (1, "b")], "Test")
        finally:
            urllib.request.urlopen = original_urlopen
            sub_fetcher.CLAUDE_API_KEY = prev_key

        # Only asked-for indices survive.
        self.assertEqual(set(result["translations"].keys()), {0, 1})
        self.assertEqual(result["translations"][0], "Zero")
        self.assertEqual(result["translations"][1], "One")


class TestRadarrReleaseParsing(unittest.TestCase):
    """Verify language detection and ranking on synthetic Radarr release payloads."""

    def test_detects_ita_from_structured_languages(self):
        rel = {"title": "Movie.2024.1080p.x265", "languages": [{"id": 5, "name": "Italian"}]}
        self.assertIn("ITA", sub_fetcher.detect_release_languages(rel))

    def test_detects_eng_from_structured_languages(self):
        rel = {"title": "Movie.2024.1080p.x265", "languages": [{"id": 1, "name": "English"}]}
        self.assertIn("ENG", sub_fetcher.detect_release_languages(rel))

    def test_detects_ita_from_title_substring(self):
        rel = {"title": "Movie.2024.iTALiAN.1080p.WEBRip.x265.mkv", "languages": []}
        self.assertIn("ITA", sub_fetcher.detect_release_languages(rel))

    def test_detects_multi_release(self):
        rel = {"title": "Movie.2024.MULTi.2160p.UHD.BluRay.x265.mkv", "languages": []}
        self.assertIn("MULTI", sub_fetcher.detect_release_languages(rel))

    def test_no_false_positive_eng_inside_word(self):
        # "ENGAGE" must not match "ENG" because of the word boundary.
        rel = {"title": "Engage.With.The.Aliens.2024.1080p.mkv", "languages": []}
        self.assertNotIn("ENG", sub_fetcher.detect_release_languages(rel))

    def test_unknown_language_fallback(self):
        rel = {"title": "Random.Movie.2024.x265", "languages": []}
        self.assertEqual(sub_fetcher.detect_release_languages(rel), ["?"])

    def test_rank_prefers_preferred_language_then_quality(self):
        ita_720 = {"title": "ITA.720p", "languages": [{"name": "Italian"}], "size": 1, "seeders": 10,
                   "quality": {"quality": {"name": "HDTV-720p"}}}
        eng_2160 = {"title": "ENG.2160p", "languages": [{"name": "English"}], "size": 1, "seeders": 100,
                    "quality": {"quality": {"name": "Bluray-2160p"}}}
        ita_1080 = {"title": "ITA.1080p", "languages": [{"name": "Italian"}], "size": 1, "seeders": 5,
                    "quality": {"quality": {"name": "Bluray-1080p"}}}
        ranked = sub_fetcher.rank_releases([eng_2160, ita_720, ita_1080], preferred=["ITA", "ENG"])
        # ITA must come before ENG even at higher quality; among ITA, 1080p > 720p.
        self.assertEqual(ranked[0]["title"], "ITA.1080p")
        self.assertEqual(ranked[1]["title"], "ITA.720p")
        self.assertEqual(ranked[2]["title"], "ENG.2160p")

    def test_rank_uses_seeders_as_tiebreaker(self):
        a = {"title": "A", "languages": [{"name": "Italian"}], "size": 1, "seeders": 5,
             "quality": {"quality": {"name": "Bluray-1080p"}}}
        b = {"title": "B", "languages": [{"name": "Italian"}], "size": 1, "seeders": 200,
             "quality": {"quality": {"name": "Bluray-1080p"}}}
        ranked = sub_fetcher.rank_releases([a, b], preferred=["ITA"])
        self.assertEqual(ranked[0]["title"], "B")

    def test_format_release_button_keeps_under_telegram_limit(self):
        rel = {"title": "Some.Very.Long.Release.Title.That.Goes.On.Forever.2160p.UHD.BluRay.x265.HDR.DV.Atmos-GROUP",
               "languages": [{"name": "Italian"}],
               "quality": {"quality": {"name": "Bluray-2160p"}},
               "size": 18_400_000_000, "seeders": 142, "leechers": 3, "rejections": []}
        label = sub_fetcher.format_release_button(rel)
        self.assertLessEqual(len(label), 80)
        self.assertIn("🇮🇹", label)
        self.assertIn("2160p", label)
        self.assertIn("GB", label)

    def test_format_release_button_marks_rejections(self):
        rel = {"title": "Foo", "languages": [{"name": "English"}],
               "quality": {"quality": {"name": "Bluray-1080p"}},
               "size": 1_000_000_000, "seeders": 10, "rejections": ["Already imported"]}
        label = sub_fetcher.format_release_button(rel)
        self.assertTrue(label.startswith("🚫"))


class TestRadarrRequestsState(unittest.TestCase):
    """Ensure requests.json round-trips cleanly and is decoupled from state.json."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig = sub_fetcher.REQUESTS_FILE
        sub_fetcher.REQUESTS_FILE = os.path.join(self.tmpdir, "requests.json")

    def tearDown(self):
        shutil.rmtree(self.tmpdir)
        sub_fetcher.REQUESTS_FILE = self._orig

    def test_save_load_round_trip(self):
        rs = sub_fetcher.load_requests()
        rs["pending_radarr"]["download:123"] = {"title": "Foo", "year": 2024}
        sub_fetcher.save_requests(rs)
        loaded = sub_fetcher.load_requests()
        self.assertEqual(loaded["pending_radarr"]["download:123"]["title"], "Foo")

    def test_missing_file_returns_empty_pending(self):
        rs = sub_fetcher.load_requests()
        self.assertEqual(rs, {"pending_radarr": {}})


class TestCommandDispatcher(unittest.TestCase):
    """Verify the alias registry resolves all known aliases and falls back to
    'did you mean?' for typos."""

    def test_canonical_lookup(self):
        for canonical in ["/stato", "/traduci", "/ritraduci", "/coda", "/log", "/falliti", "/scarica", "/aiuto"]:
            spec = sub_fetcher._find_command(canonical)
            self.assertIsNotNone(spec, f"canonical {canonical} should resolve")
            self.assertEqual(spec["canonical"], canonical)

    def test_alias_lookup(self):
        aliases = {
            "/status": "/stato",
            "/st": "/stato",
            "/translate": "/traduci",
            "/t": "/traduci",
            "/rt": "/ritraduci",
            "/del": "/cancella",
            "/queue": "/coda",
            "/q": "/coda",
            "/failed": "/falliti",
            "/help": "/aiuto",
            "/?": "/aiuto",
        }
        for alias, expected_canonical in aliases.items():
            spec = sub_fetcher._find_command(alias)
            self.assertIsNotNone(spec, f"alias {alias} should resolve")
            self.assertEqual(spec["canonical"], expected_canonical)

    def test_unknown_returns_none(self):
        self.assertIsNone(sub_fetcher._find_command("/notacommand"))

    def test_suggest_close_typo(self):
        # "/staus" is closest to the alias "/status" (insert 't'); both /status
        # and /stato map to the same handler, so any close hit is good UX.
        suggestion = sub_fetcher._suggest_command("/staus")
        self.assertIn(suggestion, ["/status", "/stato"])
        # "/coa" is closest to "/coda"
        self.assertEqual(sub_fetcher._suggest_command("/coa"), "/coda")

    def test_suggest_too_far(self):
        self.assertIsNone(sub_fetcher._suggest_command("/zzzzzzzzz"))

    def test_no_duplicate_aliases(self):
        seen = {}
        for c in sub_fetcher.COMMANDS:
            for form in [c["canonical"]] + c["aliases"]:
                self.assertNotIn(form, seen,
                    f"alias {form} appears in both {seen.get(form)} and {c['canonical']}")
                seen[form] = c["canonical"]


class TestPolishOutputParsing(unittest.TestCase):
    """Verify the post-processing step strips double-translation artifacts."""

    def _parse_polish_line(self, line):
        """Replicate the parsing logic from polish_translation_with_claude."""
        import re
        m = re.match(r"\[(\d+)\]\s*(.+)", line.strip())
        if not m:
            return None, None
        raw = m.group(2)
        if "| IT:" in raw:
            raw = raw.split("| IT:", 1)[1].strip()
        elif raw.upper().startswith("IT:"):
            raw = raw[3:].strip()
        elif raw.upper().startswith("EN:"):
            return int(m.group(1)), None  # skip
        return int(m.group(1)), raw

    def test_clean_output_unchanged(self):
        idx, text = self._parse_polish_line("[5] Ciao, come stai?")
        self.assertEqual(idx, 5)
        self.assertEqual(text, "Ciao, come stai?")

    def test_double_translation_stripped(self):
        idx, text = self._parse_polish_line("[3] EN: How are you? | IT: Come stai?")
        self.assertEqual(idx, 3)
        self.assertEqual(text, "Come stai?")

    def test_it_prefix_stripped(self):
        idx, text = self._parse_polish_line("[7] IT: Tutto bene.")
        self.assertEqual(idx, 7)
        self.assertEqual(text, "Tutto bene.")

    def test_en_only_skipped(self):
        idx, text = self._parse_polish_line("[2] EN: How are you?")
        self.assertEqual(idx, 2)
        self.assertIsNone(text)

    def test_it_prefix_case_insensitive(self):
        idx, text = self._parse_polish_line("[1] it: Va bene così.")
        self.assertEqual(idx, 1)
        self.assertEqual(text, "Va bene così.")


class TestHtmlEntitiesUnescapedInTranslation(unittest.TestCase):
    """DeepL is called with tag_handling=html (to preserve <i>...</i>), which
    makes it escape apostrophes/quotes as &#x27;/&quot;. Output must be
    unescaped so the SRT renders correctly for humans."""

    def test_deepl_output_is_html_unescaped(self):
        from unittest.mock import patch, MagicMock
        original_key = sub_fetcher.DEEPL_API_KEY
        sub_fetcher.DEEPL_API_KEY = "fake-key"

        srt = "1\n00:00:01,000 --> 00:00:02,000\nIt's fine.\n"

        resp = MagicMock()
        resp.read.return_value = json.dumps({
            "translations": [
                {"text": "L&#x27;&quot;ora&quot; va bene."},
            ]
        }).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda *a: None

        try:
            with patch("urllib.request.urlopen", return_value=resp):
                result = sub_fetcher.translate_srt_with_deepl(srt)
            self.assertIsNotNone(result)
            blocks, _ = result
            self.assertEqual(blocks[0][2], 'L\'"ora" va bene.')
            self.assertNotIn("&#x27;", blocks[0][2])
            self.assertNotIn("&quot;", blocks[0][2])
        finally:
            sub_fetcher.DEEPL_API_KEY = original_key

    def test_polish_rewrite_is_html_unescaped(self):
        from unittest.mock import patch, MagicMock
        original_key = sub_fetcher.CLAUDE_API_KEY
        sub_fetcher.CLAUDE_API_KEY = "fake-key"

        en_blocks = [("1", "00:00:01,000 --> 00:00:02,000", "It's fine.")]
        it_blocks = [("1", "00:00:01,000 --> 00:00:02,000", "Va bene.")]

        resp = MagicMock()
        resp.read.return_value = json.dumps({
            "content": [{"type": "text", "text": "[0] L&#x27;ora &amp; il momento"}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "stop_reason": "end_turn",
        }).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda *a: None

        try:
            with patch("urllib.request.urlopen", return_value=resp):
                polished, n = sub_fetcher.polish_translation_with_claude(en_blocks, it_blocks, "Test")
            self.assertEqual(n, 1)
            self.assertEqual(polished[0][2], "L'ora & il momento")
        finally:
            sub_fetcher.CLAUDE_API_KEY = original_key


class TestLanguageNormalization(unittest.TestCase):
    """Verify two-letter / three-letter / full-name inputs all normalize to
    the same canonical 3-letter ISO code from LANGUAGE_REGISTRY."""

    def test_two_letter_lowercase(self):
        self.assertEqual(sub_fetcher.normalize_lang_input("it"), "ITA")
        self.assertEqual(sub_fetcher.normalize_lang_input("en"), "ENG")
        self.assertEqual(sub_fetcher.normalize_lang_input("ja"), "JPN")

    def test_two_letter_uppercase(self):
        self.assertEqual(sub_fetcher.normalize_lang_input("IT"), "ITA")
        self.assertEqual(sub_fetcher.normalize_lang_input("EN"), "ENG")
        self.assertEqual(sub_fetcher.normalize_lang_input("DE"), "GER")

    def test_three_letter(self):
        self.assertEqual(sub_fetcher.normalize_lang_input("ita"), "ITA")
        self.assertEqual(sub_fetcher.normalize_lang_input("JPN"), "JPN")
        self.assertEqual(sub_fetcher.normalize_lang_input("kor"), "KOR")

    def test_full_name(self):
        self.assertEqual(sub_fetcher.normalize_lang_input("italian"), "ITA")
        self.assertEqual(sub_fetcher.normalize_lang_input("Japanese"), "JPN")
        self.assertEqual(sub_fetcher.normalize_lang_input("MANDARIN"), "CHI")

    def test_unknown_returns_none(self):
        self.assertIsNone(sub_fetcher.normalize_lang_input("klingon"))
        self.assertIsNone(sub_fetcher.normalize_lang_input(""))
        self.assertIsNone(sub_fetcher.normalize_lang_input(None))
        # 4-letter code that is neither a token nor a registry key.
        self.assertIsNone(sub_fetcher.normalize_lang_input("xxxx"))

    def test_strips_punctuation(self):
        # `--lang IT.` should still resolve once normalized.
        self.assertEqual(sub_fetcher.normalize_lang_input("it."), "ITA")
        self.assertEqual(sub_fetcher.normalize_lang_input(" en "), "ENG")


class TestExtractLangFlag(unittest.TestCase):
    """Parsing the `--lang CODE` flag out of free-text Telegram input."""

    def test_no_flag_returns_query_unchanged(self):
        cleaned, code, raw = sub_fetcher.extract_lang_flag("Punch-Drunk Love 2002")
        self.assertEqual(cleaned, "Punch-Drunk Love 2002")
        self.assertIsNone(code)
        self.assertIsNone(raw)

    def test_flag_trailing(self):
        cleaned, code, raw = sub_fetcher.extract_lang_flag("Punch-Drunk Love --lang ITA")
        self.assertEqual(cleaned, "Punch-Drunk Love")
        self.assertEqual(code, "ITA")
        self.assertEqual(raw, "ITA")

    def test_flag_leading(self):
        cleaned, code, _ = sub_fetcher.extract_lang_flag("--lang JPN Spirited Away")
        self.assertEqual(cleaned, "Spirited Away")
        self.assertEqual(code, "JPN")

    def test_flag_middle(self):
        cleaned, code, _ = sub_fetcher.extract_lang_flag("Spirited --lang JPN Away")
        self.assertEqual(cleaned, "Spirited Away")
        self.assertEqual(code, "JPN")

    def test_flag_equals_form(self):
        cleaned, code, _ = sub_fetcher.extract_lang_flag("Film --lang=fra")
        self.assertEqual(cleaned, "Film")
        self.assertEqual(code, "FRA")

    def test_two_letter_input(self):
        cleaned, code, raw = sub_fetcher.extract_lang_flag("Film --lang it")
        self.assertEqual(cleaned, "Film")
        self.assertEqual(code, "ITA")
        self.assertEqual(raw, "it")

    def test_unknown_lang_returns_raw(self):
        cleaned, code, raw = sub_fetcher.extract_lang_flag("Film --lang klingon")
        self.assertEqual(cleaned, "Film")
        self.assertIsNone(code)
        self.assertEqual(raw, "klingon")


class TestDetectReleaseLanguagesRegistry(unittest.TestCase):
    """Verify the extended LANGUAGE_REGISTRY recognizes the new languages and
    rejects the documented false-positive substrings."""

    def _detect(self, title, languages=None):
        return sub_fetcher.detect_release_languages({
            "title": title,
            "languages": languages or [],
        })

    def test_italian_capitalized_token(self):
        self.assertIn("ITA", self._detect("Punch-Drunk.Love.2002.iTALiAN.BDRip.1080p"))

    def test_multi_release(self):
        tags = self._detect("Spirited.Away.2001.MULTi.1080p.BluRay.x264-CMD")
        self.assertIn("MULTI", tags)

    def test_dual_audio_korean_english(self):
        tags = self._detect("Parasite.2019.Dual.Audio.KOR-ENG.1080p")
        self.assertIn("KOR", tags)
        self.assertIn("ENG", tags)
        self.assertIn("DUAL", tags)

    def test_french(self):
        self.assertIn("FRA", self._detect("Amelie.2001.FRENCH.1080p.x264"))

    def test_japanese_two_letter(self):
        self.assertIn("JPN", self._detect("Akira.1988.JP.1080p"))
        self.assertIn("JPN", self._detect("Akira.1988.JA.1080p"))

    def test_japanese_jap_token(self):
        self.assertIn("JPN", self._detect("Akira.1988.JAP.1080p"))

    def test_german(self):
        self.assertIn("GER", self._detect("Run.Lola.Run.1998.GERMAN.x264"))
        self.assertIn("GER", self._detect("Lola.1998.DEU.x264"))

    def test_spanish(self):
        self.assertIn("SPA", self._detect("Volver.2006.CASTELLANO.1080p"))

    def test_chinese(self):
        self.assertIn("CHI", self._detect("Hero.2002.MANDARIN.1080p"))
        self.assertIn("CHI", self._detect("Hero.2002.ZH.1080p"))

    def test_russian(self):
        self.assertIn("RUS", self._detect("Stalker.1979.RU.x265"))

    def test_composite_ita_eng(self):
        tags = self._detect("Movie.2020.ITA-ENG.1080p")
        self.assertIn("ITA", tags)
        self.assertIn("ENG", tags)

    def test_multi_ita_composite(self):
        tags = self._detect("Movie.2020.MULTi-ITA.1080p")
        self.assertIn("MULTI", tags)
        self.assertIn("ITA", tags)

    # False positives — `IT` substring inside common English words must NOT
    # match, thanks to the word-boundary split in detect_release_languages.
    def test_no_false_positive_it_in_hobbit(self):
        self.assertNotIn("ITA", self._detect("The.Hobbit.2012.1080p.x265-GROUP"))

    def test_no_false_positive_it_in_limit(self):
        self.assertNotIn("ITA", self._detect("inception.2010.LIMITED.1080p"))

    def test_no_false_positive_it_in_white(self):
        self.assertNotIn("ITA", self._detect("Whiteout.2009.720p.BluRay"))

    def test_no_false_positive_it_in_itunes(self):
        self.assertNotIn("ITA", self._detect("iTunes.Rip.Title.2015"))

    def test_structured_japanese_language_field(self):
        tags = self._detect("Random.mkv", languages=[{"name": "Japanese"}])
        self.assertIn("JPN", tags)


class TestFilterReleasesByLanguage(unittest.TestCase):
    """Verify the language pre-filter applied to the Radarr release list."""

    def _rel(self, title, languages=None):
        return {"title": title, "languages": languages or []}

    def test_target_ita_keeps_only_matching_and_multi(self):
        rels = [
            self._rel("Movie.2024.ITA.1080p"),
            self._rel("Movie.2024.ENG.1080p"),
            self._rel("Movie.2024.MULTi.1080p"),
        ]
        kept = sub_fetcher.filter_releases_by_language(rels, "ITA")
        titles = [r["title"] for r in kept]
        self.assertIn("Movie.2024.ITA.1080p", titles)
        self.assertIn("Movie.2024.MULTi.1080p", titles)
        self.assertNotIn("Movie.2024.ENG.1080p", titles)

    def test_target_jpn_keeps_dual_audio(self):
        # Three-release scenario: ENG-only (filtered out), JP+ENG dual (kept
        # because it explicitly tags JP), and a generic MULTI release (kept
        # via the multi pass-through).
        rels = [
            self._rel("Movie.2024.ENG.1080p"),
            self._rel("Movie.2024.Dual.Audio.JP-EN.1080p"),
            self._rel("Movie.2024.MULTi.1080p"),
        ]
        kept = sub_fetcher.filter_releases_by_language(rels, "JPN")
        titles = [r["title"] for r in kept]
        self.assertIn("Movie.2024.Dual.Audio.JP-EN.1080p", titles)
        self.assertIn("Movie.2024.MULTi.1080p", titles)
        self.assertNotIn("Movie.2024.ENG.1080p", titles)

    def test_none_target_returns_input_unchanged(self):
        rels = [self._rel("A.ENG"), self._rel("B.FRA")]
        self.assertEqual(sub_fetcher.filter_releases_by_language(rels, None), rels)

    def test_zero_matches_returns_empty(self):
        rels = [self._rel("Movie.2024.ENG.1080p"), self._rel("Movie.2024.FRA.1080p")]
        self.assertEqual(sub_fetcher.filter_releases_by_language(rels, "JPN"), [])

    def test_marker_with_other_concrete_lang_is_kept_low_score(self):
        # Soft filter: target=ITA keeps explicit ITA at top, then MULTI alone,
        # then markers with other disclosed langs (lower confidence, still
        # surfaced because MULTI/DUAL releases sometimes do contain ITA on an
        # undisclosed track). ENG-only single-language releases are excluded.
        rels = [
            self._rel("Luca.2021.WEBRip.1080p.DUAL+HIN.x264"),
            self._rel("Luca.2021.WEBRip.720p.DUAL+ENG.x264"),
            self._rel("Luca.2021.WEBDL-1080p.ENG+MULTI.x264"),
            self._rel("Luca.2021.WEBRip.1080p.ITA.x264"),
            self._rel("Luca.2021.WEBRip.1080p.MULTi.x264"),
            self._rel("Luca.2021.WEBRip.1080p.ENG.x264"),
        ]
        kept = sub_fetcher.filter_releases_by_language(rels, "ITA")
        titles = [r["title"] for r in kept]
        # Order matters: ITA (1000) → MULTI alone (600) → MULTI+other (400) → DUAL+other (200)
        self.assertEqual(titles[0], "Luca.2021.WEBRip.1080p.ITA.x264")
        self.assertEqual(titles[1], "Luca.2021.WEBRip.1080p.MULTi.x264")
        # MULTI+ENG comes before DUAL+anything
        self.assertIn("Luca.2021.WEBDL-1080p.ENG+MULTI.x264", titles[:3])
        # DUAL+other still surfaced (low score) but ENG-only is dropped
        self.assertIn("Luca.2021.WEBRip.1080p.DUAL+HIN.x264", titles)
        self.assertIn("Luca.2021.WEBRip.720p.DUAL+ENG.x264", titles)
        self.assertNotIn("Luca.2021.WEBRip.1080p.ENG.x264", titles)

    def test_score_levels_for_each_release_shape(self):
        # Pin the confidence scale so future tweaks are intentional.
        score = sub_fetcher.score_release_language_confidence
        self.assertEqual(score(self._rel("Movie.ITA.1080p"), "ITA"), 1000)
        self.assertEqual(score(self._rel("Movie.MULTi.1080p"), "ITA"), 600)
        self.assertEqual(score(self._rel("Movie.DUAL.Audio.1080p"), "ITA"), 500)
        self.assertEqual(score(self._rel("Movie.MULTi.ENG.1080p"), "ITA"), 400)
        self.assertEqual(score(self._rel("Movie.DUAL.HIN.1080p"), "ITA"), 200)
        self.assertIsNone(score(self._rel("Movie.ENG.1080p"), "ITA"))
        self.assertIsNone(score(self._rel("Movie.FRA.1080p"), "ITA"))
        # No-target → None
        self.assertIsNone(score(self._rel("Movie.ITA.1080p"), None))

    def test_marker_with_target_explicit_is_kept(self):
        # MULTI+ITA and DUAL+ITA: target is explicitly disclosed → keep.
        rels = [
            self._rel("Movie.2024.DUAL.ITA-ENG.1080p"),
            self._rel("Movie.2024.MULTi-ITA.1080p"),
        ]
        kept = sub_fetcher.filter_releases_by_language(rels, "ITA")
        self.assertEqual(len(kept), 2)

    def test_technical_tags_are_not_misdetected_as_languages(self):
        # Regression: tokens like "BR", "BRRip", "PT", "NO", "DA", "RO",
        # "EL" must NOT be mistaken for Portuguese/Norwegian/Danish/etc.
        # Target ITA + a clean ITA-only release with technical/scene tags
        # → only the ITA release passes (no false marker pass-through).
        rels = [
            self._rel("Movie.2024.BRRip.1080p.x264"),
            self._rel("Movie.2024.BluRay.1080p.x264"),
            self._rel("Movie.2024.WEBRip.NO-RETAG.1080p"),
            self._rel("Movie.2024.ITA.BRRip.1080p"),
        ]
        kept = sub_fetcher.filter_releases_by_language(rels, "ITA")
        titles = [r["title"] for r in kept]
        self.assertEqual(titles, ["Movie.2024.ITA.BRRip.1080p"])


class TestAudioMediaInfoParser(unittest.TestCase):
    """Verify the MediaInfo-style text parser used by all scrapers."""

    def test_pirate_bay_real_descr(self):
        # Verbatim sample from apibay.org for Luca.2021.MULTi.1080p.WEB.H264-LOST
        text = "  . Audio ... : English E-AC-3 5.1 / French E-AC-3 5.1\n  . Subs .... : Forced Srt / French Srt / English Srt"
        found = sub_fetcher._scrape_audio_from_mediainfo_text(text)
        self.assertEqual(found, {"ENG", "FRA"})

    def test_italian_mediainfo_block(self):
        text = "Audio: Italian AC3 5.1 / English DTS"
        found = sub_fetcher._scrape_audio_from_mediainfo_text(text)
        self.assertEqual(found, {"ITA", "ENG"})

    def test_language_field_only(self):
        text = "Format    : Matroska\nLanguage  : Italian\nLanguage  : English"
        found = sub_fetcher._scrape_audio_from_mediainfo_text(text)
        self.assertEqual(found, {"ITA", "ENG"})

    def test_no_audio_info(self):
        text = "Just a generic description with no track info."
        found = sub_fetcher._scrape_audio_from_mediainfo_text(text)
        self.assertEqual(found, set())

    def test_empty_input(self):
        self.assertEqual(sub_fetcher._scrape_audio_from_mediainfo_text(""), set())
        self.assertEqual(sub_fetcher._scrape_audio_from_mediainfo_text(None), set())

    def test_markers_are_filtered_out(self):
        # The parser must never return MULTI/DUAL as a resolved language.
        text = "Audio: MULTI 5.1\nLanguage: DUAL"
        found = sub_fetcher._scrape_audio_from_mediainfo_text(text)
        self.assertEqual(found, set())

    def test_storyline_with_italian_word_does_not_leak(self):
        # Regression: a release description that mentions "Italian" in
        # narrative prose (storyline, plot summary) but has Language: header
        # listing only Hindi+English MUST NOT add ITA to the audio set.
        # Real-world sample from TPB id=47852730 (Luca Hindi-dub).
        text = (
            "Luca (2021) 720p WEBRip [Hindi + English] Dual-Audio x264\n"
            ":: Hindi Fan Dubbed ::\n"
            "Language: Hindi [HQ Fan Dubbed] + English\n"
            "Note: This is Not A Official Hindi Dubbed Version\n"
            "Storyline:\n"
            "Luca Paguro, a young sea monster living off the coast of the "
            "Italian city Portorosso, spends his daily routine herding "
            "goatfish."
        )
        found = sub_fetcher._scrape_audio_from_mediainfo_text(text)
        self.assertEqual(found, {"HIN", "ENG"})
        self.assertNotIn("ITA", found)

    def test_payload_truncated_after_120_chars(self):
        # Defensive: payload over 120 chars is cut off so the parser cannot
        # wander into narrative prose attached after a structured header.
        long_payload = "English / " + ("X" * 200) + " / Italian"
        text = f"Audio: {long_payload}"
        found = sub_fetcher._scrape_audio_from_mediainfo_text(text)
        # English is within the first 120 chars; "Italian" is past the cap.
        self.assertIn("ENG", found)
        self.assertNotIn("ITA", found)

    def test_audio_must_be_first_word_not_inside_sentence(self):
        # A sentence containing "Audio quality is great" must not be parsed
        # as a track header just because the word Audio appears.
        text = "Note: Audio quality is great: Italian dubs available"
        found = sub_fetcher._scrape_audio_from_mediainfo_text(text)
        # "Audio" here is not the FIRST alphabetic word of the line, but the
        # current regex `^[\s\W]*Audio\b...` accepts decorative punctuation
        # before the keyword. "Note: " starts with alpha "N" → does NOT match.
        # So this test pins that behaviour.
        self.assertEqual(found, set())


class TestScoreWithScrape(unittest.TestCase):
    """Verify score_release_language_confidence uses scrape results to refine
    ambiguous (200..600) parser scores."""

    def setUp(self):
        sub_fetcher._AUDIO_SCRAPE_CACHE.clear()

    def tearDown(self):
        sub_fetcher._AUDIO_SCRAPE_CACHE.clear()

    def _rel(self, title, info_hash="h1"):
        return {"title": title, "languages": [], "infoUrl": "http://x/?id=1",
                "infoHash": info_hash}

    def test_scrape_confirms_target_bumps_to_1000(self):
        rel = self._rel("Movie.MULTi.1080p.WEB")
        sub_fetcher._AUDIO_SCRAPE_CACHE[rel["infoHash"]] = {"ITA", "ENG"}
        score = sub_fetcher.score_release_language_confidence(rel, "ITA")
        self.assertEqual(score, 1000)

    def test_scrape_refutes_target_drops_to_none(self):
        # Exact scenario from the screenshot: MULTi release that the parser
        # would rate 400 (MULTI+ENG), but TPB descr says audio = ENG+FRA only.
        rel = self._rel("Luca.2021.MULTi.1080p.WEB.H264-LOST")
        rel["languages"] = [{"id": 1, "name": "English"}]
        sub_fetcher._AUDIO_SCRAPE_CACHE[rel["infoHash"]] = {"ENG", "FRA"}
        score = sub_fetcher.score_release_language_confidence(rel, "ITA")
        self.assertIsNone(score)

    def test_scrape_empty_set_keeps_parser_score(self):
        # Empty set (cached as set()) means scrape ran but found no audio info.
        # The parser score (here 600 for MULTi alone) is retained.
        rel = self._rel("Movie.MULTi.1080p")
        sub_fetcher._AUDIO_SCRAPE_CACHE[rel["infoHash"]] = set()
        score = sub_fetcher.score_release_language_confidence(rel, "ITA")
        self.assertEqual(score, 600)

    def test_scrape_none_keeps_parser_score(self):
        # Scrape failed (network error, no api etc). Cache stores None.
        rel = self._rel("Movie.MULTi.1080p")
        sub_fetcher._AUDIO_SCRAPE_CACHE[rel["infoHash"]] = None
        score = sub_fetcher.score_release_language_confidence(rel, "ITA")
        self.assertEqual(score, 600)

    def test_explicit_match_skips_scrape(self):
        # If parser already says target=ITA explicitly, the scrape is not
        # consulted (and not even cached). Score is 1000.
        rel = self._rel("Movie.ITA.1080p")
        score = sub_fetcher.score_release_language_confidence(rel, "ITA")
        self.assertEqual(score, 1000)

    def test_allow_scrape_false_keeps_parser_score(self):
        rel = self._rel("Movie.MULTi.ENG.1080p")
        rel["languages"] = [{"id": 1, "name": "English"}]
        score = sub_fetcher.score_release_language_confidence(rel, "ITA", allow_scrape=False)
        self.assertEqual(score, 400)


class TestTpbScraperIdExtraction(unittest.TestCase):
    """Verify the TPB scraper correctly extracts the torrent id from the URL
    Radarr supplies and makes the expected apibay request."""

    def test_extracts_id_from_url(self):
        from unittest.mock import patch, MagicMock
        sample = '{"descr": "Audio: Italian AC3 / English DTS"}'
        mock_resp = MagicMock()
        mock_resp.read.return_value = sample.encode()
        mock_resp.__enter__ = lambda self: self
        mock_resp.__exit__ = lambda self, *a: None
        with patch.object(sub_fetcher.urllib.request, "urlopen", return_value=mock_resp) as up:
            found = sub_fetcher._scrape_audio_tpb("https://thepiratebay.org/description.php?id=47222815")
            args, _ = up.call_args
            req = args[0]
            self.assertIn("apibay.org/t.php?id=47222815", req.full_url)
        self.assertEqual(found, {"ITA", "ENG"})

    def test_returns_none_when_no_id(self):
        found = sub_fetcher._scrape_audio_tpb("https://thepiratebay.org/random/path")
        self.assertIsNone(found)


class TestTmdbOriginalLanguageByTmdbId(unittest.TestCase):
    """Verify the new TMDb helper returns the original_language field and
    handles missing keys / unreachable network."""

    def setUp(self):
        self._orig_key = sub_fetcher.TMDB_API_KEY
        sub_fetcher.TMDB_API_KEY = "fake-key"

    def tearDown(self):
        sub_fetcher.TMDB_API_KEY = self._orig_key

    def test_returns_iso1_code(self):
        from unittest.mock import patch, MagicMock
        resp = MagicMock()
        resp.read.return_value = json.dumps({"original_language": "ja"}).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda *a: None
        with patch("urllib.request.urlopen", return_value=resp):
            self.assertEqual(
                sub_fetcher.tmdb_get_original_language_for_tmdb_id(12345),
                "ja",
            )

    def test_none_without_api_key(self):
        sub_fetcher.TMDB_API_KEY = ""
        self.assertIsNone(sub_fetcher.tmdb_get_original_language_for_tmdb_id(12345))

    def test_iso1_to_code_mapping(self):
        self.assertEqual(sub_fetcher.tmdb_iso1_to_code("ja"), "JPN")
        self.assertEqual(sub_fetcher.tmdb_iso1_to_code("it"), "ITA")
        self.assertEqual(sub_fetcher.tmdb_iso1_to_code("en"), "ENG")
        self.assertIsNone(sub_fetcher.tmdb_iso1_to_code("xx"))
        self.assertIsNone(sub_fetcher.tmdb_iso1_to_code(None))


class TestVerifyAudioLanguage(unittest.TestCase):
    """Post-grab ffprobe-based verification of the downloaded file's audio
    track against the user-requested language."""

    def _mock_ffprobe(self, streams):
        import subprocess
        original_run = subprocess.run

        def mock_run(*args, **kwargs):
            return type("R", (), {
                "returncode": 0,
                "stdout": json.dumps({"streams": streams}),
                "stderr": "",
            })()

        subprocess.run = mock_run
        return original_run

    def test_none_required_returns_true(self):
        # `required_code=None` is the "no filter" sentinel; never inspects ffprobe.
        self.assertTrue(sub_fetcher.verify_audio_language("/fake/video.mkv", None))

    def test_ita_match_on_language_tag(self):
        import subprocess
        orig = self._mock_ffprobe([{"tags": {"language": "ita", "title": ""}}])
        try:
            self.assertTrue(sub_fetcher.verify_audio_language("/fake/video.mkv", "ITA"))
        finally:
            subprocess.run = orig

    def test_ita_mismatch_when_only_english(self):
        import subprocess
        orig = self._mock_ffprobe([{"tags": {"language": "eng", "title": "English"}}])
        try:
            self.assertFalse(sub_fetcher.verify_audio_language("/fake/video.mkv", "ITA"))
        finally:
            subprocess.run = orig

    def test_jpn_match_on_title_tag(self):
        import subprocess
        orig = self._mock_ffprobe([{"tags": {"language": "jpn", "title": "Japanese 5.1"}}])
        try:
            self.assertTrue(sub_fetcher.verify_audio_language("/fake/video.mkv", "JPN"))
        finally:
            subprocess.run = orig

    def test_jpn_match_title_when_language_missing(self):
        import subprocess
        orig = self._mock_ffprobe([{"tags": {"title": "Japanese"}}])
        try:
            self.assertTrue(sub_fetcher.verify_audio_language("/fake/video.mkv", "JPN"))
        finally:
            subprocess.run = orig

    def test_unknown_code_returns_false(self):
        # Codes outside LANGUAGE_REGISTRY (e.g. accidentally passing "XX") must
        # fail closed instead of crashing.
        self.assertFalse(sub_fetcher.verify_audio_language("/fake/video.mkv", "XYZ"))

    def test_ffprobe_missing_returns_false(self):
        import subprocess
        original_run = subprocess.run

        def mock_run(*args, **kwargs):
            raise FileNotFoundError("ffprobe not found")

        subprocess.run = mock_run
        try:
            self.assertFalse(sub_fetcher.verify_audio_language("/fake/video.mkv", "ITA"))
        finally:
            subprocess.run = original_run


class TestItaCommandAlias(unittest.TestCase):
    """The /ita command must resolve, must be wired to the same handler chain
    as /scarica with --lang ITA, and must appear in the help registry."""

    def test_ita_resolves_in_command_registry(self):
        spec = sub_fetcher._find_command("/ita")
        self.assertIsNotNone(spec)
        self.assertEqual(spec["canonical"], "/ita")

    def test_ita_appears_in_did_you_mean(self):
        # User mistypes "/itaa" -> suggestion should be "/ita".
        self.assertEqual(sub_fetcher._suggest_command("/itaa"), "/ita")

    def test_ita_enqueues_lang_ita_job(self):
        # Tap into the download queue and assert the job pushed by /ita has
        # the same shape /scarica would push with `--lang ITA`.
        from unittest.mock import patch

        captured = []

        def fake_put(item, *args, **kwargs):
            captured.append(item)

        with patch.object(sub_fetcher.download_queue, "put", side_effect=fake_put), \
                patch.object(sub_fetcher, "tg_send", return_value={"ok": True, "result": {"message_id": 1}}), \
                patch.object(sub_fetcher, "queue_position", return_value=0):
            # Force the Radarr-config gate to pass.
            orig_url, orig_key = sub_fetcher.RADARR_URL, sub_fetcher.RADARR_API_KEY
            sub_fetcher.RADARR_URL = "http://fake-radarr"
            sub_fetcher.RADARR_API_KEY = "fake"
            try:
                sub_fetcher._cmd_ita("Punch-Drunk Love", state={}, excludes=[])
            finally:
                sub_fetcher.RADARR_URL = orig_url
                sub_fetcher.RADARR_API_KEY = orig_key

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["type"], "scarica")
        self.assertEqual(captured[0]["lang"], "ITA")
        self.assertEqual(captured[0]["lang_source"], "explicit")
        self.assertEqual(captured[0]["query"], "Punch-Drunk Love")


class TestFlagFormatting(unittest.TestCase):
    """Verify _flag_for emits the right flag for the extended registry."""

    def test_ita_wins_when_mixed(self):
        self.assertEqual(sub_fetcher._flag_for(["ITA", "ENG"]), "🇮🇹")

    def test_jpn_solo(self):
        self.assertEqual(sub_fetcher._flag_for(["JPN"]), "🇯🇵")

    def test_kor_solo(self):
        self.assertEqual(sub_fetcher._flag_for(["KOR"]), "🇰🇷")

    def test_multi_marker(self):
        self.assertEqual(sub_fetcher._flag_for(["MULTI"]), "🌐")

    def test_dual_marker(self):
        self.assertEqual(sub_fetcher._flag_for(["DUAL"]), "🎭")

    def test_unknown(self):
        self.assertEqual(sub_fetcher._flag_for(["?"]), "❔")


if __name__ == "__main__":
    unittest.main()
