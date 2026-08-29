import unittest
from datetime import datetime, timedelta, timezone

from bot.slots import build_days, next_free_slot, resolve_slot, slot_of

TZ = timezone(timedelta(hours=3))
NOW = datetime(2026, 8, 24, 12, 0, tzinfo=TZ)  # понедельник, 12:00


def make_post(post_id: int, when: datetime, own: bool = False) -> dict:
    return {"id": post_id, "is_own": own, "scheduled_at": when}


class SlotOfTests(unittest.TestCase):
    def test_morning_post_falls_into_previous_slot(self) -> None:
        moment = datetime(2026, 8, 24, 9, 40, tzinfo=TZ)
        self.assertEqual(slot_of(moment), (datetime(2026, 8, 24).date(), 8))

    def test_before_first_slot_belongs_to_yesterday(self) -> None:
        moment = datetime(2026, 8, 24, 0, 30, tzinfo=TZ)
        self.assertEqual(slot_of(moment), (datetime(2026, 8, 23).date(), 23))

    def test_exactly_at_slot_start_is_that_slot(self) -> None:
        moment = datetime(2026, 8, 24, 2, 0, tzinfo=TZ)
        self.assertEqual(slot_of(moment), (datetime(2026, 8, 24).date(), 2))

    def test_late_evening_is_last_slot_of_same_day(self) -> None:
        moment = datetime(2026, 8, 24, 23, 30, tzinfo=TZ)
        self.assertEqual(slot_of(moment), (datetime(2026, 8, 24).date(), 23))

    def test_one_minute_before_first_slot_is_yesterday(self) -> None:
        moment = datetime(2026, 8, 24, 1, 59, tzinfo=TZ)
        self.assertEqual(slot_of(moment), (datetime(2026, 8, 23).date(), 23))


class ResolveSlotTests(unittest.TestCase):
    def test_day_offset_and_hour(self) -> None:
        result = resolve_slot(TZ, NOW, 1, 20)
        self.assertEqual(result, datetime(2026, 8, 25, 20, 0, tzinfo=TZ))

    def test_day_zero_is_today(self) -> None:
        result = resolve_slot(TZ, NOW, 0, 14)
        self.assertEqual(result, datetime(2026, 8, 24, 14, 0, tzinfo=TZ))


class BuildDaysTests(unittest.TestCase):
    def test_three_days_with_names_and_dates(self) -> None:
        days = build_days(TZ, NOW, [])
        self.assertEqual([d["name"] for d in days], ["сегодня", "завтра", "послезавтра"])
        self.assertEqual([d["date"] for d in days], ["24.08", "25.08", "26.08"])
        self.assertEqual([d["index"] for d in days], [0, 1, 2])

    def test_today_has_eight_slots_ordered_latest_first(self) -> None:
        days = build_days(TZ, NOW, [])
        hours = [s["hour"] for s in days[0]["slots"]]
        self.assertEqual(hours, [23, 20, 17, 14, 11, 8, 5, 2])

    def test_past_slots_marked_past(self) -> None:
        days = build_days(TZ, NOW, [])
        states = {s["hour"]: s["state"] for s in days[0]["slots"]}
        self.assertEqual(states[11], "past")
        self.assertEqual(states[8], "past")
        self.assertEqual(states[2], "past")

    def test_upcoming_slot_is_free(self) -> None:
        days = build_days(TZ, NOW, [])
        states = {s["hour"]: s["state"] for s in days[0]["slots"]}
        self.assertEqual(states[14], "free")
        self.assertEqual(states[23], "free")

    def test_taken_slot_shows_post(self) -> None:
        when = datetime(2026, 8, 25, 20, 30, tzinfo=TZ)
        days = build_days(TZ, NOW, [make_post(5, when, own=True)])
        slot = next(s for s in days[1]["slots"] if s["hour"] == 20)
        self.assertEqual(slot["state"], "taken")
        self.assertEqual(slot["postId"], 5)
        self.assertTrue(slot["postOwn"])

    def test_two_posts_in_one_slot_earliest_wins(self) -> None:
        when = datetime(2026, 8, 25, 14, 10, tzinfo=TZ)
        days = build_days(TZ, NOW, [make_post(5, when), make_post(9, when)])
        slot = next(s for s in days[1]["slots"] if s["hour"] == 14)
        self.assertEqual(slot["postId"], 5)

    def test_post_without_time_is_ignored(self) -> None:
        days = build_days(TZ, NOW, [{"id": 3, "is_own": False, "scheduled_at": None}])
        taken = [s for d in days for s in d["slots"] if s["state"] == "taken"]
        self.assertEqual(taken, [])


class NextFreeSlotTests(unittest.TestCase):
    def test_nearest_free_is_today_next_upcoming_slot(self) -> None:
        result = next_free_slot(build_days(TZ, NOW, []))
        self.assertEqual(result, {"time": "14:00", "day": 0, "dayName": "сегодня"})

    def test_taken_slot_is_skipped(self) -> None:
        taken_today = datetime(2026, 8, 24, 14, 30, tzinfo=TZ)
        result = next_free_slot(build_days(TZ, NOW, [make_post(5, taken_today)]))
        self.assertEqual(result, {"time": "17:00", "day": 0, "dayName": "сегодня"})

    def test_returns_none_when_nothing_free(self) -> None:
        # сегодня заняты все будущие слоты, завтра и послезавтра — вообще все
        scheduled = [
            make_post(10 + d * 10 + h, datetime(2026, 8, 24, h, 0, tzinfo=TZ) + timedelta(days=d))
            for d in range(3)
            for h in (14, 17, 20, 23)
        ]
        scheduled += [
            make_post(50 + d * 10 + h, datetime(2026, 8, 25, h, 0, tzinfo=TZ) + timedelta(days=d))
            for d in range(2)
            for h in (2, 5, 8, 11)
        ]
        result = next_free_slot(build_days(TZ, NOW, scheduled))
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
