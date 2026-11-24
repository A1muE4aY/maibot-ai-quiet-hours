"""中国法定节假日、调休补班与自定义日历识别模块。

优先顺序（高到低）：
1. 用户配置 ``custom_workdays`` / ``custom_holidays``
2. 本地缓存的在线同步年度节假日数据（如国务院公告）
3. 内置 2025–2027 年中国法定节假日与调休补班表
4. 纯 Python 农历与清明节气算法兜底（1900–2100 年无需联网也能识别主要法定节日）
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Sequence, Tuple
from urllib.error import URLError
from urllib.request import Request, urlopen

import json
import os

from .settings import HolidaySectionConfig

DayType = Literal["holiday", "workday_makeup", "weekend", "workday"]

DAY_TYPE_LABELS: Dict[DayType, str] = {
    "holiday": "法定节假日",
    "workday_makeup": "调休补班",
    "weekend": "周末",
    "workday": "工作日",
}

DEFAULT_ONLINE_ENDPOINTS: Tuple[str, ...] = (
    "https://cdn.jsdelivr.net/gh/NateScarlet/holiday-cn@master/{year}.json",
    "https://timor.tech/api/holiday/year/{year}",
)

HTTP_USER_AGENT = "MaiBot-AIQuietHours/1.0"

# 1900-2100 农历压缩数据（经典 20 位编码：低 4 位闰月，高 16 位各月大小，位 16 闰月大小）
_LUNAR_INFO: Tuple[int, ...] = (
    0x04BD8, 0x04AE0, 0x0A570, 0x054D5, 0x0D260, 0x0D950, 0x16554, 0x056A0, 0x09AD0, 0x055D2,
    0x04AE0, 0x0A5B6, 0x0A4D0, 0x0D250, 0x1D255, 0x0B540, 0x0D6A0, 0x0ADA2, 0x095B0, 0x14977,
    0x04970, 0x0A4B0, 0x0B4B5, 0x06A50, 0x06D40, 0x1AB54, 0x02B60, 0x09570, 0x052F2, 0x04970,
    0x06566, 0x0D4A0, 0x0EA50, 0x06E95, 0x05AD0, 0x02B60, 0x186E3, 0x092E0, 0x1C8D7, 0x0C950,
    0x0D4A0, 0x1D8A6, 0x0B550, 0x056A0, 0x1A5B4, 0x025D0, 0x092D0, 0x0D2B2, 0x0A950, 0x0B557,
    0x06CA0, 0x0B550, 0x15355, 0x04DA0, 0x0A5B0, 0x14573, 0x052B0, 0x0A9A8, 0x0E950, 0x06AA0,
    0x0AEA6, 0x0AB50, 0x04B60, 0x0AAE4, 0x0A570, 0x05260, 0x0F263, 0x0D950, 0x05B57, 0x056A0,
    0x096D0, 0x04DD5, 0x04AD0, 0x0A4D0, 0x0D4D4, 0x0D250, 0x0D558, 0x0B540, 0x0B6A0, 0x195A6,
    0x095B0, 0x049B0, 0x0A974, 0x0A4B0, 0x0B27A, 0x06A50, 0x06D40, 0x0AF46, 0x0AB60, 0x09570,
    0x04AF5, 0x04970, 0x064B0, 0x074A3, 0x0EA50, 0x06B58, 0x05AC0, 0x0AB60, 0x096D5, 0x092E0,
    0x0C960, 0x0D954, 0x0D4A0, 0x0DA50, 0x07552, 0x056A0, 0x0ABB7, 0x025D0, 0x092D0, 0x0CAB5,
    0x0A950, 0x0B4A0, 0x0BAA4, 0x0AD50, 0x055D9, 0x04BA0, 0x0A5B0, 0x15176, 0x052B0, 0x0A930,
    0x07954, 0x06AA0, 0x0AD50, 0x05B52, 0x04B60, 0x0A6E6, 0x0A4E0, 0x0D260, 0x0EA65, 0x0D530,
    0x05AA0, 0x076A3, 0x096D0, 0x04AFB, 0x04AD0, 0x0A4D0, 0x1D0B6, 0x0D250, 0x0D520, 0x0DD45,
    0x0B5A0, 0x056D0, 0x055B2, 0x049B0, 0x0A577, 0x0A4B0, 0x0AA50, 0x1B255, 0x06D20, 0x0ADA0,
    0x14B63, 0x09370, 0x049F8, 0x04970, 0x064B0, 0x168A6, 0x0EA50, 0x06B20, 0x1A6C4, 0x0AAE0,
    0x092E0, 0x0D2E3, 0x0C960, 0x0D557, 0x0D4A0, 0x0DA50, 0x05D55, 0x056A0, 0x0A6D0, 0x055D4,
    0x052D0, 0x0A9B8, 0x0A950, 0x0B4A0, 0x0B6A6, 0x0AD50, 0x055A0, 0x0ABA4, 0x0A5B0, 0x052B0,
    0x0B273, 0x06930, 0x07337, 0x06AA0, 0x0AD50, 0x14B55, 0x04B60, 0x0A570, 0x054E4, 0x0D160,
    0x0E968, 0x0D520, 0x0DAA0, 0x16AA6, 0x056D0, 0x04AE0, 0x0A9D4, 0x0A2D0, 0x0D150, 0x0F252,
    0x0D520,
)
_LUNAR_BASE_DATE = date(1900, 1, 31)


def _expand_range(start_iso: str, end_iso: str, name: str, is_off_day: bool) -> Dict[str, Tuple[bool, str]]:
    """展开闭区间日期为映射表。"""
    current = date.fromisoformat(start_iso)
    end = date.fromisoformat(end_iso)
    entries: Dict[str, Tuple[bool, str]] = {}
    while current <= end:
        entries[current.isoformat()] = (is_off_day, name)
        current += timedelta(days=1)
    return entries


def _build_builtin_china_holidays() -> Dict[int, Dict[str, Tuple[bool, str]]]:
    """构建内置 2025–2027 年中国国务院放假与调休安排表。"""
    y2025: Dict[str, Tuple[bool, str]] = {}
    y2025.update(_expand_range("2025-01-01", "2025-01-01", "元旦", True))
    y2025.update(_expand_range("2025-01-26", "2025-01-26", "春节调休补班", False))
    y2025.update(_expand_range("2025-01-28", "2025-02-04", "春节", True))
    y2025.update(_expand_range("2025-02-08", "2025-02-08", "春节调休补班", False))
    y2025.update(_expand_range("2025-04-04", "2025-04-06", "清明节", True))
    y2025.update(_expand_range("2025-04-27", "2025-04-27", "劳动节调休补班", False))
    y2025.update(_expand_range("2025-05-01", "2025-05-05", "劳动节", True))
    y2025.update(_expand_range("2025-05-31", "2025-06-02", "端午节", True))
    y2025.update(_expand_range("2025-09-28", "2025-09-28", "国庆节调休补班", False))
    y2025.update(_expand_range("2025-10-01", "2025-10-08", "国庆节/中秋节", True))
    y2025.update(_expand_range("2025-10-11", "2025-10-11", "国庆节调休补班", False))

    # 2026 年依据《国务院办公厅关于 2026 年部分节假日安排的通知》
    y2026: Dict[str, Tuple[bool, str]] = {}
    y2026.update(_expand_range("2026-01-01", "2026-01-03", "元旦", True))
    y2026.update(_expand_range("2026-01-04", "2026-01-04", "元旦调休补班", False))
    y2026.update(_expand_range("2026-02-14", "2026-02-14", "春节调休补班", False))
    y2026.update(_expand_range("2026-02-15", "2026-02-23", "春节", True))
    y2026.update(_expand_range("2026-02-28", "2026-02-28", "春节调休补班", False))
    y2026.update(_expand_range("2026-04-04", "2026-04-06", "清明节", True))
    y2026.update(_expand_range("2026-05-01", "2026-05-05", "劳动节", True))
    y2026.update(_expand_range("2026-05-09", "2026-05-09", "劳动节调休补班", False))
    y2026.update(_expand_range("2026-06-19", "2026-06-21", "端午节", True))
    y2026.update(_expand_range("2026-09-20", "2026-09-20", "国庆节调休补班", False))
    y2026.update(_expand_range("2026-09-25", "2026-09-27", "中秋节", True))
    y2026.update(_expand_range("2026-10-01", "2026-10-07", "国庆节", True))
    y2026.update(_expand_range("2026-10-10", "2026-10-10", "国庆节调休补班", False))

    # 2027 年内置法定放假与调休安排（在线同步后将自动覆盖为最新官方数据）
    y2027: Dict[str, Tuple[bool, str]] = {}
    y2027.update(_expand_range("2027-01-01", "2027-01-03", "元旦", True))
    y2027.update(_expand_range("2027-01-31", "2027-01-31", "春节调休补班", False))
    y2027.update(_expand_range("2027-02-05", "2027-02-13", "春节", True))
    y2027.update(_expand_range("2027-02-14", "2027-02-14", "春节调休补班", False))
    y2027.update(_expand_range("2027-04-03", "2027-04-05", "清明节", True))
    y2027.update(_expand_range("2027-05-01", "2027-05-05", "劳动节", True))
    y2027.update(_expand_range("2027-05-08", "2027-05-08", "劳动节调休补班", False))
    y2027.update(_expand_range("2027-06-09", "2027-06-09", "端午节", True))
    y2027.update(_expand_range("2027-09-15", "2027-09-15", "中秋节", True))
    y2027.update(_expand_range("2027-09-26", "2027-09-26", "国庆节调休补班", False))
    y2027.update(_expand_range("2027-10-01", "2027-10-07", "国庆节", True))
    y2027.update(_expand_range("2027-10-09", "2027-10-09", "国庆节调休补班", False))

    return {2025: y2025, 2026: y2026, 2027: y2027}


BUILTIN_CHINA_HOLIDAYS: Dict[int, Dict[str, Tuple[bool, str]]] = _build_builtin_china_holidays()


def _lunar_leap_month(year: int) -> int:
    return _LUNAR_INFO[year - 1900] & 0xF


def _lunar_leap_days(year: int) -> int:
    if _lunar_leap_month(year):
        return 30 if (_LUNAR_INFO[year - 1900] & 0x10000) else 29
    return 0


def _lunar_month_days(year: int, month: int) -> int:
    return 30 if (_LUNAR_INFO[year - 1900] & (0x10000 >> month)) else 29


def _lunar_year_days(year: int) -> int:
    total = 348
    info = _LUNAR_INFO[year - 1900]
    bit = 0x8000
    while bit > 0x8:
        if info & bit:
            total += 1
        bit >>= 1
    return total + _lunar_leap_days(year)


def lunar_to_solar(lunar_year: int, lunar_month: int, lunar_day: int, is_leap: bool = False) -> Optional[date]:
    """将 1900–2100 年农历日期转换为公历日期。"""
    if not 1900 <= lunar_year <= 2100 or not 1 <= lunar_month <= 12 or not 1 <= lunar_day <= 30:
        return None
    offset = 0
    for year in range(1900, lunar_year):
        offset += _lunar_year_days(year)
    leap_month = _lunar_leap_month(lunar_year)
    for month in range(1, lunar_month):
        offset += _lunar_month_days(lunar_year, month)
        if leap_month and month == leap_month:
            offset += _lunar_leap_days(lunar_year)
    if is_leap:
        if leap_month != lunar_month:
            return None
        offset += _lunar_month_days(lunar_year, lunar_month)
    offset += lunar_day - 1
    return _LUNAR_BASE_DATE + timedelta(days=offset)


def qingming_date(year: int) -> date:
    """估算公历年份的清明节日期（适用于 1900–2099 年）。"""
    if 2000 <= year <= 2099:
        yy = year % 100
        day = int(yy * 0.2422 + 4.81) - (yy // 4)
        return date(year, 4, day)
    if 1900 <= year <= 1999:
        yy = year % 100
        day = int(yy * 0.2422 + 5.59) - (yy // 4)
        return date(year, 4, day)
    return date(year, 4, 5)


def compute_algorithmic_holidays(year: int) -> Dict[str, Tuple[bool, str]]:
    """当某年暂无官方调休表时，用公历与农历算法推算中国法定节假日正日。"""
    entries: Dict[str, Tuple[bool, str]] = {
        date(year, 1, 1).isoformat(): (True, "元旦"),
        qingming_date(year).isoformat(): (True, "清明节"),
        date(year, 5, 1).isoformat(): (True, "劳动节"),
        date(year, 5, 2).isoformat(): (True, "劳动节"),
        date(year, 10, 1).isoformat(): (True, "国庆节"),
        date(year, 10, 2).isoformat(): (True, "国庆节"),
        date(year, 10, 3).isoformat(): (True, "国庆节"),
    }
    if spring_day1 := lunar_to_solar(year, 1, 1):
        eve = spring_day1 - timedelta(days=1)
        if eve.year == year:
            entries[eve.isoformat()] = (True, "春节（除夕）")
        for delta in range(0, 4):
            current = spring_day1 + timedelta(days=delta)
            if current.year == year:
                entries[current.isoformat()] = (True, "春节")
    if next_spring := lunar_to_solar(year + 1, 1, 1):
        eve = next_spring - timedelta(days=1)
        if eve.year == year:
            entries[eve.isoformat()] = (True, "春节（除夕）")
    if dragon_boat := lunar_to_solar(year, 5, 5):
        if dragon_boat.year == year:
            entries[dragon_boat.isoformat()] = (True, "端午节")
    if mid_autumn := lunar_to_solar(year, 8, 15):
        if mid_autumn.year == year:
            entries[mid_autumn.isoformat()] = (True, "中秋节")
    return entries


@dataclass(frozen=True)
class DayClassification:
    """单个日期的节假日与工作日分类结果。"""

    day_date: date
    day_type: DayType
    holiday_name: str = ""
    source: str = "weekday"

    @property
    def label(self) -> str:
        return DAY_TYPE_LABELS[self.day_type]

    @property
    def is_holiday(self) -> bool:
        return self.day_type == "holiday"

    @property
    def is_makeup_workday(self) -> bool:
        return self.day_type == "workday_makeup"

    @property
    def is_weekend(self) -> bool:
        return self.day_type == "weekend"

    @property
    def is_workday(self) -> bool:
        return self.day_type in {"workday", "workday_makeup"}

    @property
    def is_offday(self) -> bool:
        return self.day_type in {"holiday", "weekend"}

    def format_summary(self) -> str:
        if self.holiday_name:
            return f"{self.label}（{self.holiday_name}）"
        return self.label

    def to_dict(self) -> Dict[str, Any]:
        return {
            "date": self.day_date.isoformat(),
            "weekday": self.day_date.isoweekday(),
            "day_type": self.day_type,
            "day_type_label": self.label,
            "holiday_name": self.holiday_name,
            "is_holiday": self.is_holiday,
            "is_workday": self.is_workday,
            "is_offday": self.is_offday,
            "source": self.source,
        }


def parse_online_holiday_payload(year: int, payload: Any) -> Dict[str, Tuple[bool, str]]:
    """解析常见公开节假日 JSON 数据（支持 holiday-cn 与 timor.tech 格式）。"""
    if not isinstance(payload, dict):
        raise ValueError("节假日接口返回不是 JSON 对象")

    entries: Dict[str, Tuple[bool, str]] = {}

    # 格式 1: NateScarlet/holiday-cn -> {"year": 2026, "days": [{"name": "...", "date": "YYYY-MM-DD", "isOffDay": true}]}
    days = payload.get("days")
    if isinstance(days, list):
        for item in days:
            if not isinstance(item, dict):
                continue
            raw_date = str(item.get("date") or "").strip()
            if not raw_date:
                continue
            parsed = date.fromisoformat(raw_date)
            if parsed.year != year:
                continue
            is_off = bool(item.get("isOffDay", True))
            name = str(item.get("name") or ("法定节假日" if is_off else "调休补班")).strip()
            entries[parsed.isoformat()] = (is_off, name)
        if entries:
            return entries

    # 格式 2: timor.tech -> {"code": 0, "holiday": {"01-01": {"holiday": true, "name": "元旦", "date": "2026-01-01"}}}
    holiday_map = payload.get("holiday")
    if isinstance(holiday_map, dict):
        for key, item in holiday_map.items():
            if not isinstance(item, dict):
                continue
            raw_date = str(item.get("date") or "").strip()
            if not raw_date and isinstance(key, str) and len(key) == 5:
                raw_date = f"{year}-{key}"
            if not raw_date:
                continue
            parsed = date.fromisoformat(raw_date)
            if parsed.year != year:
                continue
            is_off = bool(item.get("holiday", True))
            name = str(item.get("name") or ("法定节假日" if is_off else "调休补班")).strip()
            entries[parsed.isoformat()] = (is_off, name)
        if entries:
            return entries

    raise ValueError(f"未能从节假日响应中解析出 {year} 年有效数据")


class HolidayCalendar:
    """结合自定义规则、在线缓存、内置表与农历算法的节假日识别器。"""

    def __init__(self, cache_path: Optional[Path] = None) -> None:
        self._cache_path = cache_path
        # year -> {"YYYY-MM-DD": (is_off_day, name)}
        self._online_years: Dict[int, Dict[str, Tuple[bool, str]]] = {}
        self._synced_at: Dict[int, str] = {}
        self._sync_source: Dict[int, str] = {}
        self._algorithmic_cache: Dict[int, Dict[str, Tuple[bool, str]]] = {}

    def load_cache_sync(self) -> int:
        """从本地磁盘读取已同步的在线节假日缓存（供 asyncio.to_thread 调用）。"""
        if self._cache_path is None or not self._cache_path.is_file():
            return 0
        try:
            raw = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0
        if not isinstance(raw, dict):
            return 0

        loaded_years = 0
        years_data = raw.get("years", {})
        if isinstance(years_data, dict):
            for year_str, year_payload in years_data.items():
                if not isinstance(year_payload, dict):
                    continue
                try:
                    year = int(year_str)
                except ValueError:
                    continue
                entries_raw = year_payload.get("entries", {})
                if not isinstance(entries_raw, dict):
                    continue
                parsed_entries: Dict[str, Tuple[bool, str]] = {}
                for iso_day, item in entries_raw.items():
                    if isinstance(item, list) and len(item) >= 2:
                        parsed_entries[str(iso_day)] = (bool(item[0]), str(item[1]))
                if parsed_entries:
                    self._online_years[year] = parsed_entries
                    self._synced_at[year] = str(year_payload.get("synced_at") or "")
                    self._sync_source[year] = str(year_payload.get("source") or "cache")
                    loaded_years += 1
        return loaded_years

    def _save_cache_sync(self) -> None:
        if self._cache_path is None:
            return
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        serialized = {
            "version": 1,
            "years": {
                str(year): {
                    "synced_at": self._synced_at.get(year, ""),
                    "source": self._sync_source.get(year, "online"),
                    "entries": {day: [is_off, name] for day, (is_off, name) in entries.items()},
                }
                for year, entries in sorted(self._online_years.items())
            },
        }
        temp_path = self._cache_path.with_suffix(".json.tmp")
        temp_path.write_text(json.dumps(serialized, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp_path, self._cache_path)

    def sync_years_sync(
        self,
        years: Sequence[int],
        *,
        custom_api_url: str = "",
        timeout_seconds: float = 8.0,
    ) -> Dict[str, Any]:
        """同步指定年份的在线节假日数据（阻塞网络 I/O，须通过 asyncio.to_thread 调用）。"""
        endpoints: List[str] = []
        if custom_api_url.strip():
            endpoints.append(custom_api_url.strip())
        endpoints.extend(DEFAULT_ONLINE_ENDPOINTS)

        synced_years: List[int] = []
        errors: List[str] = []

        for year in sorted(set(int(item) for item in years)):
            year_success = False
            year_errors: List[str] = []
            for template in endpoints:
                url = template.format(year=year)
                try:
                    request = Request(url, headers={"Accept": "application/json", "User-Agent": HTTP_USER_AGENT})
                    with urlopen(request, timeout=timeout_seconds) as response:
                        payload = json.loads(response.read().decode("utf-8", errors="replace"))
                    entries = parse_online_holiday_payload(year, payload)
                    self._online_years[year] = entries
                    self._synced_at[year] = datetime.now().isoformat(timespec="seconds")
                    self._sync_source[year] = url
                    synced_years.append(year)
                    year_success = True
                    break
                except (OSError, URLError, ValueError) as exc:
                    year_errors.append(f"{url}: {exc}")
            if not year_success:
                errors.append(f"{year} 年同步失败: {'; '.join(year_errors)}")

        if synced_years:
            try:
                self._save_cache_sync()
            except OSError as exc:
                errors.append(f"保存本地缓存失败: {exc}")

        return {
            "synced_years": synced_years,
            "errors": errors,
            "cached_years": sorted(self._online_years.keys()),
        }

    def get_year_table(self, year: int) -> Tuple[Dict[str, Tuple[bool, str]], str]:
        """按优先级返回某年的节假日/调休表及数据来源。"""
        if year in self._online_years:
            return self._online_years[year], "online_cache"
        if year in BUILTIN_CHINA_HOLIDAYS:
            return BUILTIN_CHINA_HOLIDAYS[year], "builtin"
        if year not in self._algorithmic_cache:
            self._algorithmic_cache[year] = compute_algorithmic_holidays(year)
        return self._algorithmic_cache[year], "algorithmic"

    def classify(self, target_date: date, config: HolidaySectionConfig) -> DayClassification:
        """判定给定日期的节假日与工作日属性。"""
        iso = target_date.isoformat()
        mmdd = target_date.strftime("%m-%d")
        is_calendar_weekend = target_date.isoweekday() >= 6

        # 1. 自定义工作日优先覆盖一切
        if iso in config.custom_workdays:
            return DayClassification(
                day_date=target_date,
                day_type="workday_makeup" if is_calendar_weekend else "workday",
                holiday_name="自定义工作日" if is_calendar_weekend else "",
                source="custom_workday",
            )

        # 2. 自定义节假日（精确日期或每年循环 MM-DD）
        if iso in config.custom_holidays or mmdd in config.custom_holidays:
            return DayClassification(
                day_date=target_date,
                day_type="holiday",
                holiday_name="自定义节假日",
                source="custom_holiday",
            )

        # 3. 法定节假日与调休补班识别
        if config.enabled:
            year_table, source = self.get_year_table(target_date.year)
            if iso in year_table:
                is_off_day, name = year_table[iso]
                if is_off_day:
                    return DayClassification(
                        day_date=target_date,
                        day_type="holiday",
                        holiday_name=name,
                        source=source,
                    )
                if config.treat_makeup_as_workday:
                    return DayClassification(
                        day_date=target_date,
                        day_type="workday_makeup",
                        holiday_name=name,
                        source=source,
                    )

        # 4. 普通周末与普通工作日
        if is_calendar_weekend:
            return DayClassification(
                day_date=target_date,
                day_type="weekend",
                holiday_name="",
                source="calendar",
            )
        return DayClassification(
            day_date=target_date,
            day_type="workday",
            holiday_name="",
            source="calendar",
        )

    def list_upcoming_special_days(
        self,
        start_date: date,
        config: HolidaySectionConfig,
        *,
        horizon_days: int = 90,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """列出未来一段时间内即将到来的节假日与调休补班日。"""
        results: List[Dict[str, Any]] = []
        for offset in range(max(horizon_days, 1)):
            current = start_date + timedelta(days=offset)
            info = self.classify(current, config)
            if info.day_type in {"holiday", "workday_makeup"}:
                results.append(info.to_dict())
                if len(results) >= limit:
                    break
        return results

    def status_summary(self, year: int) -> Dict[str, Any]:
        """返回日历数据加载与同步摘要。"""
        _, active_source = self.get_year_table(year)
        return {
            "year": year,
            "active_source": active_source,
            "builtin_years": sorted(BUILTIN_CHINA_HOLIDAYS.keys()),
            "synced_years": sorted(self._online_years.keys()),
            "last_synced_at": self._synced_at.get(year, ""),
        }
