import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from lct_leaderboard.web import (
    _leaderboard_csv,
    _normalize_team_id,
    _submission_count,
    _submissions_closed,
    _init_db,
)


class WebHelpersTest(unittest.TestCase):
    def test_normalizes_team_id(self):
        self.assertEqual(_normalize_team_id("  Team   Alpha  "), "Team Alpha")

    def test_rejects_long_team_id(self):
        with self.assertRaises(ValueError):
            _normalize_team_id("x" * 65)

    def test_submission_deadline(self):
        old_value = os.environ.get("LCT_SUBMISSIONS_CLOSE_AT")
        os.environ["LCT_SUBMISSIONS_CLOSE_AT"] = "2026-09-25T23:59:00Z"
        try:
            self.assertFalse(
                _submissions_closed(datetime(2026, 9, 25, 23, 58, tzinfo=timezone.utc))
            )
            self.assertTrue(
                _submissions_closed(datetime(2026, 9, 26, 0, 0, tzinfo=timezone.utc))
            )
        finally:
            if old_value is None:
                os.environ.pop("LCT_SUBMISSIONS_CLOSE_AT", None)
            else:
                os.environ["LCT_SUBMISSIONS_CLOSE_AT"] = old_value

    def test_submission_count_starts_at_zero(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "leaderboard.sqlite"
            _init_db(db_path)
            self.assertEqual(_submission_count(db_path, "team", "dataset"), 0)

    def test_leaderboard_csv(self):
        text = _leaderboard_csv(
            [
                {
                    "rank": 1,
                    "team_id": "team",
                    "submission_id": 7,
                    "accepted": True,
                    "leaderboard_score": 1.0,
                    "calculated_cost": 100,
                    "new_network_length": 10,
                    "warning_count": 0,
                }
            ]
        )
        self.assertIn("team", text)
        self.assertIn("leaderboard_score", text)


if __name__ == "__main__":
    unittest.main()
