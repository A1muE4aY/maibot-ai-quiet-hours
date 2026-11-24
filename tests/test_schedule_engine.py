"""静默时段调度、跨天窗口、模板渲染与豁免名单单元测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import sys
import unittest

from ._sdk_stub import ensure_maibot_sdk_available, get_maibot_root

ensure_maibot_sdk_available()
sys.path.insert(0, str(get_maibot_root()))

from plugins.ai_quiet_hours.holiday_calendar import HolidayCalendar  # noqa: E402
from plugins.ai_quiet_hours.schedule_engine import (  # noqa: E402
    ManualOverride,
    evaluate_schedule,
    is_context_exempt,
    render_notice_template,
)
from plugins.ai_quiet_hours.settings import (  # noqa: E402
    ExemptionSectionConfig,
    HolidaySectionConfig,
    QuietHoursPluginConfig,
    ScheduleRule,
    ScheduleSectionConfig,
)

TZ_CST = timezone(timedelta(hours=8))


class TestScheduleEngine(unittest.TestCase):
    def setUp(self) -> None:
        self.calendar = HolidayCalendar()

    def test_overnight_workday_window_inherits_previous_day(self) -> None:
        config = QuietHoursPluginConfig(
            schedule=ScheduleSectionConfig(
                rules=[
                    ScheduleRule(
                        name="工作日夜间",
                        enabled=True,
                        start_time="23:00",
                        end_time="07:00",
                        day_scope="workday",
                    )
                ]
            )
        )
        # 2026-11-20 是周五（工作日），23:30 应处于静默
        fri_night = datetime(2026, 11, 20, 23, 30, tzinfo=TZ_CST)
        ev_fri = evaluate_schedule(config, self.calendar, fri_night)
        self.assertTrue(ev_fri.is_quiet)
        self.assertEqual(ev_fri.effective_end_dt, datetime(2026, 11, 21, 7, 0, tzinfo=TZ_CST))

        # 2026-11-21 是周六 02:00，属于周五跨天窗口，仍应静默！
        sat_early = datetime(2026, 11, 21, 2, 0, tzinfo=TZ_CST)
        ev_sat_early = evaluate_schedule(config, self.calendar, sat_early)
        self.assertTrue(ev_sat_early.is_quiet)
        self.assertEqual(ev_sat_early.effective_end_dt, datetime(2026, 11, 21, 7, 0, tzinfo=TZ_CST))

        # 2026-11-21 周六 23:30 不属于工作日，不应静默！
        sat_night = datetime(2026, 11, 21, 23, 30, tzinfo=TZ_CST)
        ev_sat_night = evaluate_schedule(config, self.calendar, sat_night)
        self.assertFalse(ev_sat_night.is_quiet)

    def test_workday_rule_respects_holidays_and_makeup_workdays(self) -> None:
        config = QuietHoursPluginConfig(
            schedule=ScheduleSectionConfig(
                rules=[
                    ScheduleRule(
                        name="上班静默",
                        enabled=True,
                        start_time="09:00",
                        end_time="18:00",
                        day_scope="workday",
                    )
                ]
            )
        )
        # 2026-10-01 周四是国庆节 -> 放假不静默
        ev_holiday = evaluate_schedule(config, self.calendar, datetime(2026, 10, 1, 10, 0, tzinfo=TZ_CST))
        self.assertFalse(ev_holiday.is_quiet)
        self.assertEqual(ev_holiday.today_info.holiday_name, "国庆节")

        # 2026-10-10 周六是国庆调休补班 -> 视作工作日照常静默
        ev_makeup = evaluate_schedule(config, self.calendar, datetime(2026, 10, 10, 10, 0, tzinfo=TZ_CST))
        self.assertTrue(ev_makeup.is_quiet)
        self.assertEqual(ev_makeup.today_info.day_type, "workday_makeup")

    def test_holiday_skip_toggle_and_holiday_only_rule(self) -> None:
        config = QuietHoursPluginConfig(
            holiday=HolidaySectionConfig(skip_quiet_on_holidays=True),
            schedule=ScheduleSectionConfig(
                rules=[
                    ScheduleRule(
                        name="日常早静默",
                        enabled=True,
                        start_time="08:00",
                        end_time="12:00",
                        day_scope="all",
                        respect_holiday_skip=True,
                    ),
                    ScheduleRule(
                        name="节假日午休",
                        enabled=True,
                        start_time="13:00",
                        end_time="15:00",
                        day_scope="holiday",
                        respect_holiday_skip=True,
                    ),
                ]
            ),
        )
        # 2026-10-01 10:00：日常早静默在节假日自动跳过
        self.assertFalse(evaluate_schedule(config, self.calendar, datetime(2026, 10, 1, 10, 0, tzinfo=TZ_CST)).is_quiet)
        # 2026-10-01 14:00：holiday 专用规则照常生效
        self.assertTrue(evaluate_schedule(config, self.calendar, datetime(2026, 10, 1, 14, 0, tzinfo=TZ_CST)).is_quiet)
        # 2026-11-23 10:00：普通工作日照常生效
        self.assertTrue(evaluate_schedule(config, self.calendar, datetime(2026, 11, 23, 10, 0, tzinfo=TZ_CST)).is_quiet)

    def test_overlapping_rules_merge_end_time(self) -> None:
        config = QuietHoursPluginConfig(
            schedule=ScheduleSectionConfig(
                rules=[
                    ScheduleRule(name="时段A", start_time="10:00", end_time="12:00", day_scope="all"),
                    ScheduleRule(name="时段B", start_time="11:30", end_time="14:00", day_scope="all"),
                ]
            )
        )
        ev = evaluate_schedule(config, self.calendar, datetime(2026, 11, 23, 11, 0, tzinfo=TZ_CST))
        self.assertTrue(ev.is_quiet)
        self.assertEqual(ev.effective_end_dt, datetime(2026, 11, 23, 14, 0, tzinfo=TZ_CST))

    def test_manual_override_and_expiry(self) -> None:
        config = QuietHoursPluginConfig(
            schedule=ScheduleSectionConfig(
                rules=[ScheduleRule(name="夜间", start_time="23:00", end_time="07:00", day_scope="all")]
            )
        )
        now = datetime(2026, 11, 23, 15, 0, tzinfo=TZ_CST)
        override = ManualOverride(
            mode="force_quiet",
            reason="临时维护",
            created_at=now,
            expires_at=now + timedelta(minutes=30),
        )
        ev_active = evaluate_schedule(config, self.calendar, now + timedelta(minutes=10), manual_override=override)
        self.assertTrue(ev_active.is_quiet)
        self.assertEqual(ev_active.rule_names, "临时维护")

        ev_expired = evaluate_schedule(config, self.calendar, now + timedelta(minutes=31), manual_override=override)
        self.assertFalse(ev_expired.is_quiet)

    def test_safe_template_rendering(self) -> None:
        config = QuietHoursPluginConfig(
            schedule=ScheduleSectionConfig(
                rules=[ScheduleRule(name="国庆休眠", start_time="09:00", end_time="18:00", day_scope="holiday")]
            )
        )
        ev = evaluate_schedule(config, self.calendar, datetime(2026, 10, 1, 10, 0, tzinfo=TZ_CST))
        rendered = render_notice_template(
            "{rule_name}|{end_time}|{day_type}|{holiday_name}|{unknown_var}",
            ev,
        )
        self.assertEqual(rendered, "国庆休眠|18:00|法定节假日|国庆节|{unknown_var}")

    def test_exemptions_and_legacy_string_coercion(self) -> None:
        exemptions = ExemptionSectionConfig.model_validate(
            {
                "exempt_groups": ["10001", "telegram:20002", {"platform": "qq", "type": "group", "id": "30003", "name": "核心群"}],
                "exempt_users": ["qq:8888", "9999"],
                "user_exempt_in_group": True,
                "exempt_platforms": ["webui"],
            }
        )
        self.assertTrue(is_context_exempt(exemptions, platform="qq", group_id="10001", user_id="1")[0])
        self.assertTrue(is_context_exempt(exemptions, platform="telegram", group_id="20002", user_id="1")[0])
        self.assertFalse(is_context_exempt(exemptions, platform="qq", group_id="20002", user_id="1")[0])
        self.assertTrue(is_context_exempt(exemptions, platform="qq", group_id="99999", user_id="8888")[0])
        self.assertTrue(is_context_exempt(exemptions, platform="telegram", group_id="", user_id="9999")[0])
        self.assertTrue(is_context_exempt(exemptions, platform="webui", group_id="", user_id="any")[0])

        exemptions.user_exempt_in_group = False
        self.assertFalse(is_context_exempt(exemptions, platform="qq", group_id="99999", user_id="8888")[0])
        self.assertTrue(is_context_exempt(exemptions, platform="qq", group_id="", user_id="8888")[0])


if __name__ == "__main__":
    unittest.main()
