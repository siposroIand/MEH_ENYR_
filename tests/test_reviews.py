import unittest
from datetime import date

from utils.reviews import (
    compute_review_date, next_review_date, parse_reviews, review_dates, review_entries,
    review_occurrences,
)


class ReviewTests(unittest.TestCase):
    def test_multiple_relative_reviews_and_zero_days(self):
        entity = {
            "lejarat": "2030-03-01", "hataly_tipus": "fixed",
            "felulvizsgalatok": [
                {"type": "before_expiry", "days": 0},
                {"type": "before_expiry", "days": 30},
                {"type": "date", "date": "2029-01-01"},
            ],
        }
        self.assertEqual(review_dates(entity), [
            date(2029, 1, 1), date(2030, 1, 30), date(2030, 3, 1),
        ])
        self.assertEqual(next_review_date(entity, date(2030, 1, 1)), date(2030, 1, 30))
        self.assertEqual(next_review_date(entity, date(2030, 3, 1)), date(2030, 3, 1))

    def test_mixed_legacy_and_typed_list(self):
        parsed = parse_reviews({"felulvizsgalatok":
            '[{"date":"2030-01-01","note":"old"},'
            '{"type":"before_expiry","days":"0","note":"new"}]'})
        self.assertTrue(parsed.valid)
        self.assertEqual(parsed.entries[0]["type"], "date")
        self.assertEqual(parsed.entries[1]["days"], 0)

    def test_explicit_empty_is_authoritative(self):
        for raw in ([], "[]"):
            entity = {"felulvizsgalatok": raw, "felulvizsgalat_date": "2030-01-01",
                      "felulvizsgalat_nap": 30, "lejarat": "2031-01-01"}
            self.assertEqual(review_dates(entity), [])
            self.assertFalse(parse_reviews(entity).uses_legacy)

    def test_legacy_fields_preserve_both_rules(self):
        entity = {"felulvizsgalat_date": "2030-01-01", "felulvizsgalat_nap": "30",
                  "lejarat": "2030-03-01"}
        self.assertEqual(len(review_dates(entity)), 2)
        self.assertEqual(compute_review_date(entity), date(2030, 1, 1))

    def test_malformed_data_never_falls_back(self):
        for raw in ("broken", "{}", '[{"type":"date","date":"bad"}]',
                    [{"type": "before_expiry", "days": -1}],
                    [{"type": "before_expiry", "days": 10**100}],
                    [{"type": "before_expiry", "days": True}],
                    [{"type": "date", "date": "2030-01-01", "note": []}],
                    [{"type": "before_expiry", "days": 1.5}]):
            parsed = parse_reviews({"felulvizsgalatok": raw,
                                    "felulvizsgalat_date": "2030-01-01"})
            self.assertFalse(parsed.valid)
            self.assertEqual(parsed.raw, raw)
            self.assertFalse(parsed.uses_legacy)

    def test_no_relative_calculation_for_indefinite_or_modified(self):
        for fields in ({"hataly_tipus": "indefinite"}, {"hataly_tipus": "until_modified"},
                       {"hatarozatlan": 1}, {"lejarat": "9999-12-31"}):
            entity = {"lejarat": "2030-01-01", "felulvizsgalatok": [
                {"type": "before_expiry", "days": 0},
                {"date": "2029-01-01"}], **fields}
            self.assertEqual(review_dates(entity), [date(2029, 1, 1)])

    def test_invalid_entry_does_not_hide_valid_entries(self):
        parsed = parse_reviews({"felulvizsgalatok": [
            {"date": "2030-01-01"}, {"type": "unknown"}]})
        self.assertFalse(parsed.valid)
        self.assertEqual(len(parsed.entries), 1)

    def test_resolved_occurrences_retain_relative_notes_and_stable_ties(self):
        entity = {"lejarat": "2030-01-01", "felulvizsgalatok": [
            {"type": "before_expiry", "days": 0, "note": "relative"},
            {"date": "2030-01-01", "note": "legacy"}]}
        occurrences = review_occurrences(entity)
        self.assertEqual([due for due, _ in occurrences], [date(2030, 1, 1)] * 2)
        self.assertEqual([entry["note"] for _, entry in occurrences], ["relative", "legacy"])
        self.assertEqual(review_entries(entity), [
            (date(2030, 1, 1), "relative"), (date(2030, 1, 1), "legacy")])

    def test_legacy_date_formats_normalize_without_losing_notes(self):
        for value in ("2030.01.02", "2030-01-02T10:20:30", "2030-01-02T10:20:30+02:00"):
            parsed = parse_reviews({"felulvizsgalatok": [{"date": value, "note": "old"}]})
            self.assertTrue(parsed.valid)
            self.assertEqual(parsed.entries[0]["date"], "2030-01-02")
            self.assertEqual(review_entries({"felulvizsgalat_date": value}), [
                (date(2030, 1, 2), "")])


if __name__ == "__main__":
    unittest.main()
