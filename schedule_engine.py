"""AI 静默时段调度与豁免判定引擎。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Any, Dict, List, Literal, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import re

from .holiday_calendar import DayClassification, HolidayCalendar
from .settings import (
    ExemptionSectionConfig,
    GroupTarget,
    QuietHoursPluginConfig,
    ScheduleRule,
    UserTarget,
)

_FIXED_OFFSET_PATTERN = re.compile(r"^(?:UTC|GMT)?\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?$", re.IGNORECASE)
_COMMON_TIMEZONE_OFFSETS: Dict[str, int] = {
    "asia/shanghai": 8 * 60,
    "asia/chongqing": 8 * 60,
    "asia/hong_kong": 8 * 60,
    "asia/taipei": 8 * 60,
    "asia/singapore": 8 * 60,
    "asia/tokyo": 9 * 60,
    "asia/seoul": 9 * 60,
    "utc": 0,
    "gmt": 0,
}


class _SafeTemplateDict(dict[str, str]):
    """模板格式化字典：遇到未知占位符时原样保留，避免配置误写触发异常。"""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def resolve_timezone(timezone_name: str) -> Optional[tzinfo]:
    """解析用户配置的时区，兼容 IANA 名称、UTC+8 偏移及缺少 tzdata 的 Windows 环境。"""
    normalized = timezone_name.strip()
    if not normalized:
        return None

    offset_match = _FIXED_OFFSET_PATTERN.fullmatch(normalized)
    if offset_match is not None:
        sign = 1 if offset_match.group(1) == "+" else -1
        hours = int(offset_match.group(2))
        minutes = int(offset_match.group(3) or "0")
        if hours <= 23 and minutes <= 59:
            return timezone(timedelta(minutes=sign * (hours * 60 + minutes)))

    try:
        return ZoneInfo(normalized)
    except ZoneInfoNotFoundError:
        fallback_minutes = _COMMON_TIMEZONE_OFFSETS.get(normalized.lower())
        if fallback_minutes is not None:
            return timezone(timedelta(minutes=fallback_minutes))
        return None


def now_in_timezone(timezone_name: str) -> datetime:
    """获取指定时区的当前时间；未指定时返回系统本地带时区当前时间。"""
    tz = resolve_timezone(timezone_name)
    if tz is not None:
        return datetime.now(tz)
    return datetime.now().astimezone()


def _parse_hhmm_minutes(text: str) -> int:
    hour_str, minute_str = text.strip().split(":", 1)
    return int(hour_str) * 60 + int(minute_str)


def _date_at_minutes(anchor_date: date, total_minutes: int, tz: Optional[tzinfo]) -> datetime:
    days_delta, rem_minutes = divmod(total_minutes, 1440)
    hour, minute = divmod(rem_minutes, 60)
    target_date = anchor_date + timedelta(days=days_delta)
    return datetime.combine(target_date, time(hour=hour, minute=minute), tzinfo=tz)


@dataclass(frozen=True)
class ManualOverride:
    """管理员通过指令或 WebUI 施加的手动覆盖状态。"""

    mode: Literal["force_quiet", "force_active", "force_fallback"]
    reason: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now().astimezone())
    expires_at: Optional[datetime] = None
    fallback_models: Tuple[str, ...] = ()

    def is_expired(self, now: datetime) -> bool:
        if self.expires_at is None:
            return False
        return now >= self.expires_at

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "reason": self.reason,
            "created_at": self.created_at.isoformat(timespec="seconds"),
            "expires_at": self.expires_at.isoformat(timespec="seconds") if self.expires_at else None,
            "fallback_models": list(self.fallback_models),
        }


@dataclass(frozen=True)
class ActiveRuleMatch:
    """单条命中当前时间的规则及其精确起止边界。"""

    rule: ScheduleRule
    anchor_date: date
    start_dt: datetime
    end_dt: datetime
    day_info: DayClassification


@dataclass(frozen=True)
class ScheduleEvaluation:
    """某一时刻的完整时段与模式评估结果。"""

    evaluated_at: datetime
    is_quiet: bool
    today_info: DayClassification
    matched_rules: Tuple[ActiveRuleMatch, ...] = ()
    effective_start_dt: Optional[datetime] = None
    effective_end_dt: Optional[datetime] = None
    next_transition_at: Optional[datetime] = None
    manual_override: Optional[ManualOverride] = None
    reason: str = ""
    preferred_mode: Literal["normal", "quiet", "model_fallback"] = "normal"
    preferred_fallback_models: Tuple[str, ...] = ()

    @property
    def primary_rule(self) -> Optional[ScheduleRule]:
        return self.matched_rules[0].rule if self.matched_rules else None

    @property
    def rule_names(self) -> str:
        if self.manual_override is not None:
            if self.manual_override.mode == "force_quiet":
                return self.manual_override.reason or "手动开启静默"
            if self.manual_override.mode == "force_fallback":
                return self.manual_override.reason or "手动切换回退模型"
            return self.manual_override.reason or "手动恢复运行"
        if not self.matched_rules:
            return "无"
        return "、".join(match.rule.name for match in self.matched_rules)

    @property
    def start_time_text(self) -> str:
        if self.effective_start_dt is not None:
            return self.effective_start_dt.strftime("%H:%M")
        return self.evaluated_at.strftime("%H:%M")

    @property
    def end_time_text(self) -> str:
        if self.effective_end_dt is not None:
            if self.effective_end_dt.date() != self.evaluated_at.date():
                return self.effective_end_dt.strftime("%m-%d %H:%M")
            return self.effective_end_dt.strftime("%H:%M")
        if self.manual_override is not None and self.manual_override.mode in {"force_quiet", "force_fallback"}:
            return "手动恢复前"
        return "未知"


def does_rule_match_date(
    rule: ScheduleRule,
    day_info: DayClassification,
    *,
    skip_quiet_on_holidays: bool,
) -> bool:
    """判断某条静默规则是否适用于指定日期。"""
    if not rule.enabled:
        return False

    # 全局“节假日跳过静默”仅作用于非 holiday 专用规则且勾选了 respect_holiday_skip 的规则
    if (
        skip_quiet_on_holidays
        and rule.respect_holiday_skip
        and rule.day_scope != "holiday"
        and day_info.is_holiday
    ):
        return False

    scope = rule.day_scope
    if scope == "all":
        return True
    if scope == "workday":
        return day_info.is_workday
    if scope == "offday":
        return day_info.is_offday
    if scope == "holiday":
        return day_info.is_holiday
    if scope == "non_holiday":
        return not day_info.is_holiday
    if scope == "weekend":
        return day_info.is_weekend
    if scope == "weekdays":
        return day_info.day_date.isoweekday() in rule.weekdays
    return False


def build_rule_window_for_anchor(
    rule: ScheduleRule,
    anchor_date: date,
    tz: Optional[tzinfo],
) -> Tuple[datetime, datetime]:
    """根据起始基准日构造单条规则的半开时间区间 ``[start_dt, end_dt)``。"""
    start_minutes = _parse_hhmm_minutes(rule.start_time)
    end_minutes = _parse_hhmm_minutes(rule.end_time)
    start_dt = _date_at_minutes(anchor_date, start_minutes, tz)
    if end_minutes == start_minutes:
        # 例如 00:00 ~ 00:00 视为整天 24 小时
        end_dt = start_dt + timedelta(days=1)
    elif end_minutes > start_minutes:
        end_dt = _date_at_minutes(anchor_date, end_minutes, tz)
    else:
        # 跨天窗口（例如 23:00 ~ 07:00）
        end_dt = _date_at_minutes(anchor_date + timedelta(days=1), end_minutes, tz)
    return start_dt, end_dt


def _collect_rule_windows(
    config: QuietHoursPluginConfig,
    calendar: HolidayCalendar,
    start_date: date,
    end_date: date,
    tz: Optional[tzinfo],
) -> List[ActiveRuleMatch]:
    """收集日期范围内所有生效的规则时间窗口。"""
    windows: List[ActiveRuleMatch] = []
    skip_holidays = config.holiday.skip_quiet_on_holidays
    current = start_date
    while current <= end_date:
        day_info = calendar.classify(current, config.holiday)
        for rule in config.schedule.rules:
            if not does_rule_match_date(rule, day_info, skip_quiet_on_holidays=skip_holidays):
                continue
            start_dt, end_dt = build_rule_window_for_anchor(rule, current, tz)
            windows.append(
                ActiveRuleMatch(
                    rule=rule,
                    anchor_date=current,
                    start_dt=start_dt,
                    end_dt=end_dt,
                    day_info=day_info,
                )
            )
        current += timedelta(days=1)
    return windows


def _merge_intervals(intervals: List[Tuple[datetime, datetime]]) -> List[Tuple[datetime, datetime]]:
    """合并重叠或首尾相接的时间区间。"""
    if not intervals:
        return []
    ordered = sorted(intervals, key=lambda pair: (pair[0], pair[1]))
    merged: List[Tuple[datetime, datetime]] = [ordered[0]]
    for start_dt, end_dt in ordered[1:]:
        prev_start, prev_end = merged[-1]
        if start_dt <= prev_end:
            merged[-1] = (prev_start, max(prev_end, end_dt))
        else:
            merged.append((start_dt, end_dt))
    return merged


def _resolve_window_fallback_preference(
    config: QuietHoursPluginConfig,
    matched: Tuple[ActiveRuleMatch, ...],
) -> Tuple[Literal["quiet", "model_fallback"], Tuple[str, ...]]:
    """根据命中规则与全局回退模型配置，解析当前时段期望模式（回退模型优先或强制静默）。"""
    # 若任一命中规则显式要求完全静默，则优先按最严格的静默规则处理
    if any(match.rule.mode == "quiet" for match in matched):
        return "quiet", ()

    rule_models: List[str] = []
    seen = set()
    for match in matched:
        for model_name in match.rule.fallback_models:
            cleaned = str(model_name or "").strip()
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                rule_models.append(cleaned)

    if rule_models:
        return "model_fallback", tuple(rule_models)

    if config.model_fallback.enabled and config.model_fallback.fallback_models:
        global_models = tuple(
            str(m).strip() for m in config.model_fallback.fallback_models if str(m).strip()
        )
        if global_models:
            return "model_fallback", global_models

    return "quiet", ()


def evaluate_schedule(
    config: QuietHoursPluginConfig,
    calendar: HolidayCalendar,
    now: Optional[datetime] = None,
    *,
    manual_override: Optional[ManualOverride] = None,
    lookahead_days: int = 14,
) -> ScheduleEvaluation:
    """评估指定时刻是否处于 AI 静默时段，并计算期望模式（模型回退 / 静默）、恢复时间与下次切换时刻。"""
    current = now if now is not None else now_in_timezone(config.schedule.timezone)
    tz = current.tzinfo
    today_info = calendar.classify(current.date(), config.holiday)

    active_override = manual_override
    if active_override is not None and active_override.is_expired(current):
        active_override = None

    windows = _collect_rule_windows(
        config=config,
        calendar=calendar,
        start_date=current.date() - timedelta(days=1),
        end_date=current.date() + timedelta(days=max(lookahead_days, 2)),
        tz=tz,
    )
    matched = tuple(item for item in windows if item.start_dt <= current < item.end_dt)
    merged = _merge_intervals([(item.start_dt, item.end_dt) for item in windows])

    scheduled_start: Optional[datetime] = None
    scheduled_end: Optional[datetime] = None
    next_scheduled_start: Optional[datetime] = None
    for interval_start, interval_end in merged:
        if interval_start <= current < interval_end:
            scheduled_start = interval_start
            scheduled_end = interval_end
            break
        if interval_start > current:
            next_scheduled_start = interval_start
            break

    if active_override is not None:
        if active_override.mode == "force_quiet":
            return ScheduleEvaluation(
                evaluated_at=current,
                is_quiet=True,
                today_info=today_info,
                matched_rules=matched,
                effective_start_dt=active_override.created_at,
                effective_end_dt=active_override.expires_at,
                next_transition_at=active_override.expires_at,
                manual_override=active_override,
                reason=active_override.reason or "管理员手动开启 AI 静默",
                preferred_mode="quiet",
                preferred_fallback_models=(),
            )
        if active_override.mode == "force_fallback":
            override_models = active_override.fallback_models or tuple(
                str(m).strip() for m in config.model_fallback.fallback_models if str(m).strip()
            )
            return ScheduleEvaluation(
                evaluated_at=current,
                is_quiet=True,
                today_info=today_info,
                matched_rules=matched,
                effective_start_dt=active_override.created_at,
                effective_end_dt=active_override.expires_at,
                next_transition_at=active_override.expires_at,
                manual_override=active_override,
                reason=active_override.reason or "管理员手动切换至回退模型",
                preferred_mode="model_fallback" if override_models else "quiet",
                preferred_fallback_models=override_models,
            )
        return ScheduleEvaluation(
            evaluated_at=current,
            is_quiet=False,
            today_info=today_info,
            matched_rules=matched,
            effective_start_dt=None,
            effective_end_dt=None,
            next_transition_at=active_override.expires_at or next_scheduled_start,
            manual_override=active_override,
            reason=active_override.reason or "管理员手动恢复 AI 运行",
            preferred_mode="normal",
            preferred_fallback_models=(),
        )

    if scheduled_start is not None and scheduled_end is not None:
        pref_mode, pref_models = _resolve_window_fallback_preference(config, matched)
        return ScheduleEvaluation(
            evaluated_at=current,
            is_quiet=True,
            today_info=today_info,
            matched_rules=matched,
            effective_start_dt=scheduled_start,
            effective_end_dt=scheduled_end,
            next_transition_at=scheduled_end,
            manual_override=None,
            reason=f"命中静默规则：{', '.join(match.rule.name for match in matched)}",
            preferred_mode=pref_mode,
            preferred_fallback_models=pref_models,
        )

    return ScheduleEvaluation(
        evaluated_at=current,
        is_quiet=False,
        today_info=today_info,
        matched_rules=(),
        effective_start_dt=None,
        effective_end_dt=None,
        next_transition_at=next_scheduled_start,
        manual_override=None,
        reason="当前处于正常 AI 运行时段",
        preferred_mode="normal",
        preferred_fallback_models=(),
    )


def render_notice_template(
    template: str,
    evaluation: ScheduleEvaluation,
    *,
    previous_evaluation: Optional[ScheduleEvaluation] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> str:
    """安全渲染提示语模板，支持规则名、时间、日期类型、节假日与回退模型变量。"""
    source_eval = previous_evaluation if (not evaluation.is_quiet and previous_evaluation is not None) else evaluation
    values: Dict[str, str] = {
        "rule_name": source_eval.rule_names,
        "start_time": source_eval.start_time_text,
        "end_time": source_eval.end_time_text,
        "now": evaluation.evaluated_at.strftime("%Y-%m-%d %H:%M"),
        "time": evaluation.evaluated_at.strftime("%H:%M"),
        "date": evaluation.evaluated_at.strftime("%Y-%m-%d"),
        "day_type": evaluation.today_info.label,
        "holiday_name": evaluation.today_info.holiday_name,
        "reason": evaluation.reason,
        "fallback_models": ", ".join(source_eval.preferred_fallback_models) or "无",
    }
    if extra:
        for key, val in extra.items():
            values[str(key)] = "" if val is None else str(val)
    return template.format_map(_SafeTemplateDict(values)).strip()


def _matches_group_target(target: GroupTarget, platform: str, group_id: str) -> bool:
    if not group_id or target.id != group_id:
        return False
    return not target.platform or target.platform.lower() == platform.lower()


def _matches_user_target(target: UserTarget, platform: str, user_id: str) -> bool:
    if not user_id or target.id != user_id:
        return False
    return not target.platform or target.platform.lower() == platform.lower()


def is_group_exempt(exemptions: ExemptionSectionConfig, platform: str, group_id: str) -> bool:
    """判断指定群聊是否在豁免名单中。"""
    normalized_platform = platform.strip().lower()
    normalized_group = group_id.strip()
    if normalized_platform and normalized_platform in {p.strip().lower() for p in exemptions.exempt_platforms}:
        return True
    if not normalized_group:
        return False
    return any(_matches_group_target(target, normalized_platform, normalized_group) for target in exemptions.exempt_groups)


def is_user_exempt(exemptions: ExemptionSectionConfig, platform: str, user_id: str) -> bool:
    """判断指定用户是否在豁免名单中。"""
    normalized_platform = platform.strip().lower()
    normalized_user = user_id.strip()
    if normalized_platform and normalized_platform in {p.strip().lower() for p in exemptions.exempt_platforms}:
        return True
    if not normalized_user:
        return False
    return any(_matches_user_target(target, normalized_platform, normalized_user) for target in exemptions.exempt_users)


def is_context_exempt(
    exemptions: ExemptionSectionConfig,
    *,
    platform: str,
    group_id: str = "",
    user_id: str = "",
) -> Tuple[bool, str]:
    """综合判断某条消息或会话是否豁免静默限制，并返回豁免原因。"""
    normalized_platform = platform.strip().lower()
    if normalized_platform and normalized_platform in {p.strip().lower() for p in exemptions.exempt_platforms}:
        return True, f"exempt_platform:{normalized_platform}"

    normalized_group = group_id.strip()
    normalized_user = user_id.strip()

    if normalized_group and is_group_exempt(exemptions, normalized_platform, normalized_group):
        return True, f"exempt_group:{normalized_group}"

    if normalized_user and is_user_exempt(exemptions, normalized_platform, normalized_user):
        if not normalized_group or exemptions.user_exempt_in_group:
            return True, f"exempt_user:{normalized_user}"

    return False, ""


def has_any_chat_exemptions(exemptions: ExemptionSectionConfig) -> bool:
    """判断用户是否配置了需要按群聊/用户动态放行的豁免项。"""
    return bool(exemptions.exempt_groups or exemptions.exempt_users)
