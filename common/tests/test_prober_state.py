import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common import prober_state


class CountryCodeTests(unittest.TestCase):
    def test_reads_flag_prefix(self) -> None:
        self.assertEqual(prober_state.country_code("🇩🇪 Германия 1"), "DE")
        self.assertEqual(prober_state.country_code("🇷🇺 YT & DS 4 [Trojan]"), "RU")

    def test_returns_none_without_a_flag(self) -> None:
        self.assertIsNone(prober_state.country_code("Вот бы снова лето..."))
        self.assertIsNone(prober_state.country_code("🏴‍☠️ Пиратский сервер"))

    def test_first_flag_wins(self) -> None:
        self.assertEqual(prober_state.country_code("🇷🇺 🇩🇪"), "RU")


class ParseCountryCodesTests(unittest.TestCase):
    def test_normalizes_and_deduplicates(self) -> None:
        self.assertEqual(
            prober_state.parse_country_codes(["ru", "CN", "ru"], "filter"),
            ("RU", "CN"),
        )

    def test_empty_list_is_allowed(self) -> None:
        self.assertEqual(prober_state.parse_country_codes([], "filter"), ())

    def test_rejects_bad_shapes(self) -> None:
        for value in (["RUS"], ["ß"], [""], [5], "RU", None):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "country code"):
                    prober_state.parse_country_codes(value, "filter")


class FilterCandidatesTests(unittest.TestCase):
    def test_denylist_drops_only_flagged_nodes(self) -> None:
        candidates = ["🇷🇺 YT & DS 4", "🇩🇪 Германия 1", "no flag"]
        self.assertEqual(
            prober_state.filter_candidates(candidates, ("RU",)),
            ["🇩🇪 Германия 1", "no flag"],
        )

    def test_empty_denylist_keeps_everything(self) -> None:
        candidates = ["🇷🇺 YT & DS 4", "🇩🇪 Германия 1"]
        self.assertEqual(prober_state.filter_candidates(candidates, ()), candidates)


class LoadExcludedCountriesTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "post-prober.toml"

    def test_reads_the_filter_table(self) -> None:
        self.path.write_text(
            '[probe]\nurl = "https://example.com"\n'
            '\n[filter]\nexclude_countries = ["ru", "CN"]\n',
            encoding="utf-8",
        )
        self.assertEqual(prober_state.load_excluded_countries(self.path), ("RU", "CN"))

    def test_absent_filter_is_empty(self) -> None:
        self.path.write_text('[probe]\nurl = "https://example.com"\n', encoding="utf-8")
        self.assertEqual(prober_state.load_excluded_countries(self.path), ())

    def test_missing_file_fails_loudly(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot read prober config"):
            prober_state.load_excluded_countries(self.path)

    def test_bad_toml_fails_loudly(self) -> None:
        self.path.write_text("not = [", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "invalid TOML"):
            prober_state.load_excluded_countries(self.path)

    def test_non_table_filter_fails_loudly(self) -> None:
        self.path.write_text("filter = 5\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "filter must be a table"):
            prober_state.load_excluded_countries(self.path)

    def test_unknown_filter_key_fails_loudly(self) -> None:
        self.path.write_text('[filter]\nexclude = ["RU"]\n', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "unknown keys: exclude"):
            prober_state.load_excluded_countries(self.path)


class VerdictCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.path = self.root / "cache" / "verdicts.json"

    def test_round_trip(self) -> None:
        cache = prober_state.VerdictCache(path=self.path, day="2026-10-19")
        cache.record("🇩🇪 good", True)
        cache.record("🇷🇺 bad", False)
        with mock.patch.object(prober_state, "_utc_day", return_value="2026-10-19"):
            loaded = prober_state.VerdictCache.load(self.path)
        self.assertFalse(loaded.is_bad("🇩🇪 good"))
        self.assertTrue(loaded.is_bad("🇷🇺 bad"))
        self.assertFalse(loaded.is_bad("unknown"))

    def test_rollover_clears_the_day(self) -> None:
        cache = prober_state.VerdictCache(path=self.path, day="2026-10-19")
        cache.record("🇷🇺 bad", False)
        with mock.patch.object(prober_state, "_utc_day", return_value="2026-10-20"):
            cache.rollover()
        self.assertEqual(cache.day, "2026-10-20")
        self.assertEqual(cache.verdicts, {})
        document = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(document["day"], "2026-10-20")

    def test_stale_day_is_discarded(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text(
            json.dumps({"day": "2026-10-18", "nodes": {"🇷🇺 bad": False}}),
            encoding="utf-8",
        )
        with mock.patch.object(prober_state, "_utc_day", return_value="2026-10-19"):
            cache = prober_state.VerdictCache.load(self.path)
        self.assertEqual(cache.verdicts, {})

    def test_corrupt_file_is_ignored(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text("not json", encoding="utf-8")
        with mock.patch.object(prober_state, "_utc_day", return_value="2026-10-19"):
            cache = prober_state.VerdictCache.load(self.path)
        self.assertEqual(cache.verdicts, {})


class EligibleCandidatesTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cache = prober_state.VerdictCache(
            path=Path(tmp.name) / "cache.json", day="2026-10-19"
        )

    def test_drops_denied_and_skips_bad(self) -> None:
        self.cache.record("🇩🇪 b", False)
        candidates = ["🇷🇺 a", "🇩🇪 b", "🇩🇪 c", "no flag"]
        selection = prober_state.eligible_candidates(candidates, ("RU",), self.cache)
        self.assertEqual(selection.to_probe, ("🇩🇪 c", "no flag"))
        self.assertEqual(selection.skipped_bad, 1)
        self.assertFalse(selection.all_bad_retry)

    def test_all_bad_retries_them(self) -> None:
        self.cache.record("a", False)
        self.cache.record("b", False)
        selection = prober_state.eligible_candidates(["a", "b"], (), self.cache)
        self.assertEqual(selection.to_probe, ("a", "b"))
        self.assertEqual(selection.skipped_bad, 0)
        self.assertTrue(selection.all_bad_retry)

    def test_all_denied_is_an_empty_selection(self) -> None:
        selection = prober_state.eligible_candidates(
            ["🇷🇺 a", "🇨🇳 b"], ("RU", "CN"), self.cache
        )
        self.assertEqual(selection.to_probe, ())
        self.assertFalse(selection.all_bad_retry)


if __name__ == "__main__":
    unittest.main()
