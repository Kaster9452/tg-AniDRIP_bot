import unittest
from datetime import datetime, timezone, timedelta

from bot.timeparse import TimeParseError, parse_when


class ParseWhenTests(unittest.TestCase):
    def setUp(self) -> None:
        self.timezone = timezone(timedelta(hours=3))
        self.now = datetime(2026, 8, 24, 12, 0, tzinfo=self.timezone)

    def test_parses_time_today(self) -> None:
        result = parse_when("18:30", self.timezone, now=self.now)

        self.assertEqual(result, datetime(2026, 8, 24, 18, 30, tzinfo=self.timezone))

    def test_parses_tomorrow(self) -> None:
        result = parse_when("завтра 09:00", self.timezone, now=self.now)

        self.assertEqual(result, datetime(2026, 8, 25, 9, 0, tzinfo=self.timezone))

    def test_parses_relative_interval(self) -> None:
        result = parse_when("+1ч30м", self.timezone, now=self.now)

        self.assertEqual(result, datetime(2026, 8, 24, 13, 30, tzinfo=self.timezone))

    def test_time_that_already_passed_moves_to_tomorrow(self) -> None:
        result = parse_when("09:00", self.timezone, now=self.now)

        self.assertEqual(result, datetime(2026, 8, 25, 9, 0, tzinfo=self.timezone))

    def test_rejects_invalid_time(self) -> None:
        with self.assertRaises(TimeParseError):
            parse_when("25:90", self.timezone, now=self.now)


if __name__ == "__main__":
    unittest.main()
