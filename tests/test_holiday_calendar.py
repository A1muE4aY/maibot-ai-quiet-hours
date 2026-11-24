"""节假日与调休补班识别单元测试。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import sys
import tempfile
import unittest

from ._sdk_stub import ensure_maibot_sdk_available, get_maibot_root

ensure_maibot_sdk_available()
sys.path.insert(0, str(get_maibot_root()))

from plugins.ai_quiet_hours.holiday_calendar import (  # noqa: E402
    HolidayCalendar,
    compute_algorithmic_holidays,
    lunar_to_solar,
    parse_online_holiday_payload,
)
from plugins.ai_quiet_hours.settings import HolidaySectionConfig  # noqa: E402


class TestHolidayCalendar(unittest.TestCase):
    def setUp(self) -> None:
        self.calendar = HolidayCalendar()
        self.config = HolidaySectionConfig()

    def test_2026_official_holidays_and_makeups(self) -> None:
        # 2026 元旦及调休补班
        jan1 = self.calendar.classify(date(2026, 1, 1), self.config)
        self.assertEqual(jan1.day_type, "holiday")
        self.assertEqual(jan1.holiday_name, "元旦")
        self.assertTrue(jan1.is_holiday)
        self.assertTrue(jan1.is_offday)
        self.assertFalse(jan1.is_workday)

        jan4 = self.calendar.classify(date(2026, 1, 4), self.config)  # 周日补班
        self.assertEqual(jan4.day_type, "workday_makeup")
        self.assertTrue(jan4.is_workday)
        self.assertFalse(jan4.is_offday)

        # 2026 春节与调休补班
        feb14 = self.calendar.classify(date(2026, 2, 14), self.config)
        self.assertEqual(feb14.day_type, "workday_makeup")
        feb17 = self.calendar.classify(date(2026, 2, 17), self.config)  # 2026 春节初一
        self.assertEqual(feb17.day_type, "holiday")
        self.assertEqual(feb17.holiday_name, "春节")

        # 2026 国庆节与调休补班
        oct1 = self.calendar.classify(date(2026, 10, 1), self.config)
        self.assertEqual(oct1.day_type, "holiday")
        oct10 = self.calendar.classify(date(2026, 10, 10), self.config)
        self.assertEqual(oct10.day_type, "workday_makeup")

        # 普通工作日与普通周末
        nov23 = self.calendar.classify(date(2026, 11, 23), self.config)  # 周一
        self.assertEqual(nov23.day_type, "workday")
        nov22 = self.calendar.classify(date(2026, 11, 22), self.config)  # 周日
        self.assertEqual(nov22.day_type, "weekend")

    def test_makeup_disabled_falls_back_to_weekend(self) -> None:
        cfg = HolidaySectionConfig(treat_makeup_as_workday=False)
        jan4 = self.calendar.classify(date(2026, 1, 4), cfg)
        self.assertEqual(jan4.day_type, "weekend")
        self.assertFalse(jan4.is_workday)

    def test_custom_overrides_have_highest_priority(self) -> None:
        cfg = HolidaySectionConfig(
            custom_holidays=["2026-11-23", "12-25"],
            custom_workdays=["2026-10-01", "2026-11-22"],
        )
        # 自定义节假日（单日与每年循环）
        self.assertEqual(self.calendar.classify(date(2026, 11, 23), cfg).day_type, "holiday")
        self.assertEqual(self.calendar.classify(date(2026, 12, 25), cfg).day_type, "holiday")
        self.assertEqual(self.calendar.classify(date(2029, 12, 25), cfg).day_type, "holiday")

        # 自定义工作日覆盖法定节假日与普通周末
        oct1 = self.calendar.classify(date(2026, 10, 1), cfg)
        self.assertTrue(oct1.is_workday)
        self.assertEqual(oct1.source, "custom_workday")
        nov22 = self.calendar.classify(date(2026, 11, 22), cfg)
        self.assertEqual(nov22.day_type, "workday_makeup")

    def test_lunar_algorithm_fallback_for_future_years(self) -> None:
        # 2026 农历正月初一 = 2026-02-17；2028 农历正月初一 = 2028-01-26；2030 农历正月初一 = 2030-02-03
        self.assertEqual(lunar_to_solar(2026, 1, 1), date(2026, 2, 17))
        self.assertEqual(lunar_to_solar(2028, 1, 1), date(2028, 1, 26))
        self.assertEqual(lunar_to_solar(2030, 1, 1), date(2030, 2, 3))

        table_2030 = compute_algorithmic_holidays(2030)
        self.assertIn("2030-01-01", table_2030)
        self.assertIn("2030-02-03", table_2030)
        self.assertIn("2030-10-01", table_2030)

        feb3 = self.calendar.classify(date(2030, 2, 3), self.config)
        self.assertEqual(feb3.day_type, "holiday")
        self.assertEqual(feb3.source, "algorithmic")

    def test_online_payload_parsers_and_cache_roundtrip(self) -> None:
        holiday_cn_payload = {
            "year": 2028,
            "days": [
                {"name": "元旦", "date": "2028-01-01", "isOffDay": True},
                {"name": "春节补班", "date": "2028-01-22", "isOffDay": False},
            ],
        }
        parsed_cn = parse_online_holiday_payload(2028, holiday_cn_payload)
        self.assertEqual(parsed_cn["2028-01-01"], (True, "元旦"))
        self.assertEqual(parsed_cn["2028-01-22"], (False, "春节补班"))

        timor_payload = {
            "code": 0,
            "holiday": {
                "01-01": {"holiday": True, "name": "元旦", "date": "2028-01-01"},
                "01-22": {"holiday": False, "name": "春节后补班", "date": "2028-01-22"},
            },
        }
        parsed_timor = parse_online_holiday_payload(2028, timor_payload)
        self.assertEqual(parsed_timor["2028-01-01"], (True, "元旦"))
        self.assertEqual(parsed_timor["2028-01-22"], (False, "春节后补班"))

        with tempfile.TemporaryDirectory() as tmp:
            cache_file = Path(tmp) / "holiday_cache.json"
            cal_writer = HolidayCalendar(cache_path=cache_file)
            cal_writer._online_years[2028] = parsed_cn
            cal_writer._save_cache_sync()

            cal_reader = HolidayCalendar(cache_path=cache_file)
            self.assertEqual(cal_reader.load_cache_sync(), 1)
            jan22 = cal_reader.classify(date(2028, 1, 22), self.config)
            self.assertEqual(jan22.day_type, "workday_makeup")
            self.assertEqual(jan22.source, "online_cache")


if __name__ == "__main__":
    unittest.main()
