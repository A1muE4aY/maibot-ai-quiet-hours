"""AI 时段静默守卫插件配置模型。"""

from __future__ import annotations

from datetime import date
from typing import Any, ClassVar, List, Literal

import re

from pydantic import field_validator, model_validator

from maibot_sdk import Field, PluginConfigBase

_HHMM_PATTERN = re.compile(r"^(?:(?:[01]\d|2[0-3]):[0-5]\d|24:00)$")
_DATE_FULL_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATE_ANNUAL_PATTERN = re.compile(r"^\d{2}-\d{2}$")

DayScope = Literal[
    "all",
    "workday",
    "offday",
    "holiday",
    "non_holiday",
    "weekend",
    "weekdays",
]

NoticeTargetMode = Literal[
    "all_non_exempt_groups",
    "all_non_exempt_streams",
    "configured_only",
]

PluginBlockMode = Literal[
    "smart",
    "hook_only",
    "disable_components",
]


def _normalize_target_input(value: Any, target_type: Literal["group", "user"]) -> Any:
    """兼容纯字符串或 ``platform:id`` 写法，转换为结构化目标字典。"""
    if not isinstance(value, str):
        return value
    raw = value.strip()
    if not raw:
        return {"platform": "", "type": target_type, "id": "", "name": ""}
    if ":" in raw:
        platform, target_id = raw.split(":", 1)
        return {
            "platform": platform.strip(),
            "type": target_type,
            "id": target_id.strip(),
            "name": "",
        }
    return {"platform": "", "type": target_type, "id": raw, "name": ""}


def _validate_hhmm(value: str) -> str:
    """校验 ``HH:MM`` 或 ``24:00`` 时间格式。"""
    normalized = value.strip()
    if not _HHMM_PATTERN.fullmatch(normalized):
        raise ValueError("时间格式必须为 HH:MM（00:00 至 23:59，结束时间支持 24:00）")
    return normalized


class GroupTarget(PluginConfigBase):
    """通用平台群聊目标；platform 为空表示不限平台。"""

    platform: str = Field(default="", description="平台标识；留空表示匹配任意平台")
    type: Literal["group"] = "group"
    id: str = Field(default="", min_length=1, description="群聊真实 ID")
    name: str = Field(default="", description="展示名称（仅用于展示，不参与匹配）")

    @model_validator(mode="before")
    @classmethod
    def coerce_legacy_string(cls, value: Any) -> Any:
        return _normalize_target_input(value, "group")

    @field_validator("platform", "id", "name")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()


class UserTarget(PluginConfigBase):
    """通用平台用户目标；platform 为空表示不限平台。"""

    platform: str = Field(default="", description="平台标识；留空表示匹配任意平台")
    type: Literal["user"] = "user"
    id: str = Field(default="", min_length=1, description="用户真实 ID")
    name: str = Field(default="", description="展示名称（仅用于展示，不参与匹配）")

    @model_validator(mode="before")
    @classmethod
    def coerce_legacy_string(cls, value: Any) -> Any:
        return _normalize_target_input(value, "user")

    @field_validator("platform", "id", "name")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()


class ScheduleRule(PluginConfigBase):
    """单条 AI 静默时段规则。"""

    name: str = Field(
        default="夜间休眠",
        min_length=1,
        max_length=64,
        description="规则名称，可用于提示语 {rule_name} 与状态展示。",
        json_schema_extra={"label": "规则名称"},
    )
    enabled: bool = Field(
        default=True,
        description="是否启用此规则。",
        json_schema_extra={"label": "启用规则", "x-widget": "switch"},
    )
    start_time: str = Field(
        default="23:00",
        description="静默开始时间，格式 HH:MM（例如 23:00）。当开始时间晚于结束时间时自动按跨天处理。",
        json_schema_extra={"label": "开始时间"},
    )
    end_time: str = Field(
        default="07:00",
        description="静默结束时间，格式 HH:MM（支持 24:00 表示当日结束）。",
        json_schema_extra={"label": "结束时间"},
    )
    day_scope: DayScope = Field(
        default="all",
        description=(
            "适用日期类型：all=每天，workday=工作日（含调休补班、排除法定节假日），"
            "offday=休息日（周末与法定节假日、排除补班），holiday=仅法定节假日，"
            "non_holiday=非法定节假日，weekend=普通周末（排除补班），weekdays=按下方星期几匹配。"
        ),
        json_schema_extra={"label": "日期范围", "x-widget": "select"},
    )
    weekdays: List[int] = Field(
        default_factory=lambda: [1, 2, 3, 4, 5],
        description="仅在 day_scope=weekdays 时生效：1=周一 ... 7=周日。",
        json_schema_extra={"label": "星期列表（1-7）"},
    )
    respect_holiday_skip: bool = Field(
        default=True,
        description="是否受全局“节假日跳过静默”策略约束；对于 day_scope=holiday 规则此项自动忽略。",
        json_schema_extra={"label": "服从节假日跳过策略", "x-widget": "switch"},
    )
    notify_on_start: bool = Field(
        default=True,
        description="进入该静默时段时是否发送开始提示语。",
        json_schema_extra={"label": "开始时发送提示", "x-widget": "switch"},
    )
    notify_on_end: bool = Field(
        default=True,
        description="结束该静默时段时是否发送恢复提示语。",
        json_schema_extra={"label": "结束时发送提示", "x-widget": "switch"},
    )
    start_message: str = Field(
        default="",
        description="该规则专属的开始提示语；留空使用全局默认开始提示语。支持 {rule_name}、{start_time}、{end_time}、{day_type}、{holiday_name}。",
        json_schema_extra={"label": "专属开始提示语", "x-widget": "textarea"},
    )
    end_message: str = Field(
        default="",
        description="该规则专属的结束提示语；留空使用全局默认结束提示语。",
        json_schema_extra={"label": "专属结束提示语", "x-widget": "textarea"},
    )

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("规则名称不能为空")
        return normalized

    @field_validator("start_time")
    @classmethod
    def validate_start_time(cls, value: str) -> str:
        normalized = _validate_hhmm(value)
        if normalized == "24:00":
            raise ValueError("开始时间不能为 24:00，请使用 00:00")
        return normalized

    @field_validator("end_time")
    @classmethod
    def validate_end_time(cls, value: str) -> str:
        return _validate_hhmm(value)

    @field_validator("weekdays")
    @classmethod
    def validate_weekdays(cls, value: List[int]) -> List[int]:
        deduped: List[int] = []
        for day in value:
            if type(day) is not int or not 1 <= day <= 7:
                raise ValueError("星期列表中的每一项必须是 1 到 7 的整数")
            if day not in deduped:
                deduped.append(day)
        return sorted(deduped)


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__: ClassVar[str] = "插件"
    __ui_icon__: ClassVar[str] = "package"
    __ui_order__: ClassVar[int] = 0

    enabled: bool = Field(
        default=True,
        description="是否启用 AI 时段静默守卫插件。",
        json_schema_extra={"label": "启用插件", "x-widget": "switch"},
    )
    config_version: str = Field(
        default="1.0.0",
        description="插件配置版本。",
        json_schema_extra={"hidden": True},
    )


class ScheduleSectionConfig(PluginConfigBase):
    """时段调度配置。"""

    __ui_label__: ClassVar[str] = "静默时段规则"
    __ui_icon__: ClassVar[str] = "clock"
    __ui_order__: ClassVar[int] = 1

    timezone: str = Field(
        default="",
        description="时区标识（如 Asia/Shanghai 或 UTC+8）；留空使用系统本地时间。",
        json_schema_extra={"label": "时区"},
    )
    check_interval_seconds: int = Field(
        default=15,
        ge=5,
        le=300,
        description="后台巡检静默状态切换的时间间隔（秒），建议 10–30 秒。",
        json_schema_extra={"label": "巡检间隔（秒）"},
    )
    block_media_ai: bool = Field(
        default=True,
        description="静默时段内跳过入站图片 VLM、表情包 VLM 与语音 ASR 转写，避免后台消耗模型额度。",
        json_schema_extra={"label": "静默期阻断图片/语音识别模型", "x-widget": "switch"},
    )
    block_background_emoji_maintenance: bool = Field(
        default=True,
        description="静默时段内暂停表情包后台维护的视觉模型注册任务。",
        json_schema_extra={"label": "静默期暂停表情包后台识图", "x-widget": "switch"},
    )
    rules: List[ScheduleRule] = Field(
        default_factory=lambda: [
            ScheduleRule(
                name="DeepSeek 早高峰",
                enabled=True,
                start_time="08:00",
                end_time="12:00",
                day_scope="workday",
                weekdays=[1, 2, 3, 4, 5],
                respect_holiday_skip=True,
                notify_on_start=True,
                notify_on_end=True,
                start_message="",
                end_message="",
            ),
            ScheduleRule(
                name="DeepSeek 午高峰",
                enabled=True,
                start_time="14:00",
                end_time="18:00",
                day_scope="workday",
                weekdays=[1, 2, 3, 4, 5],
                respect_holiday_skip=True,
                notify_on_start=True,
                notify_on_end=True,
                start_message="",
                end_message="",
            ),
        ],
        description="静默时段规则列表，命中任意一条已启用规则即关闭 AI 聊天与大模型能力。",
        json_schema_extra={"label": "静默时段列表", "x-widget": "list"},
    )


class HolidaySectionConfig(PluginConfigBase):
    """节假日与调休补班识别配置。"""

    __ui_label__: ClassVar[str] = "节假日识别"
    __ui_icon__: ClassVar[str] = "calendar"
    __ui_order__: ClassVar[int] = 2

    enabled: bool = Field(
        default=True,
        description="启用中国法定节假日与调休补班识别（内置 2025–2027 年表与农历算法兜底）。",
        json_schema_extra={"label": "启用节假日识别", "x-widget": "switch"},
    )
    skip_quiet_on_holidays: bool = Field(
        default=False,
        description="开启后，在法定节假日（或自定义节假日）自动跳过标记了“服从节假日跳过策略”的静默规则。",
        json_schema_extra={"label": "节假日自动跳过静默", "x-widget": "switch"},
    )
    treat_makeup_as_workday: bool = Field(
        default=True,
        description="将国务院公布的调休补班周末识别为工作日（workday），不再视为普通周末。",
        json_schema_extra={"label": "调休补班按工作日处理", "x-widget": "switch"},
    )
    enable_online_sync: bool = Field(
        default=True,
        description="自动从公开节假日数据源同步当年及次年法定节假日与调休安排并缓存到本地，断网时回退至内置日历。",
        json_schema_extra={"label": "自动在线同步年度节假日", "x-widget": "switch"},
    )
    sync_timeout_seconds: float = Field(
        default=8.0,
        ge=2.0,
        le=30.0,
        description="在线节假日同步超时时间（秒）。",
        json_schema_extra={"label": "同步超时（秒）", "advanced": True},
    )
    custom_api_url: str = Field(
        default="",
        description="可选的自定义节假日 JSON 接口模板，使用 {year} 占位符；留空使用内置公开数据源。",
        json_schema_extra={"label": "自定义节假日接口 URL", "advanced": True},
    )
    custom_holidays: List[str] = Field(
        default_factory=list,
        description="额外自定义节假日，支持 YYYY-MM-DD（单日）或 MM-DD（每年循环），优先级高于内置与在线数据。",
        json_schema_extra={"label": "自定义节假日列表"},
    )
    custom_workdays: List[str] = Field(
        default_factory=list,
        description="强制视为工作日的日期（YYYY-MM-DD），可覆盖周末或法定节假日。",
        json_schema_extra={"label": "自定义工作日列表"},
    )

    @field_validator("custom_holidays")
    @classmethod
    def validate_custom_holidays(cls, value: List[str]) -> List[str]:
        normalized: List[str] = []
        for raw_item in value:
            item = raw_item.strip()
            if not item:
                continue
            if _DATE_FULL_PATTERN.fullmatch(item):
                date.fromisoformat(item)
            elif _DATE_ANNUAL_PATTERN.fullmatch(item):
                month, day = (int(part) for part in item.split("-"))
                date(2024, month, day)
            else:
                raise ValueError(f"自定义节假日格式无效: {item}（应为 YYYY-MM-DD 或 MM-DD）")
            if item not in normalized:
                normalized.append(item)
        return normalized

    @field_validator("custom_workdays")
    @classmethod
    def validate_custom_workdays(cls, value: List[str]) -> List[str]:
        normalized: List[str] = []
        for raw_item in value:
            item = raw_item.strip()
            if not item:
                continue
            if not _DATE_FULL_PATTERN.fullmatch(item):
                raise ValueError(f"自定义工作日格式无效: {item}（应为 YYYY-MM-DD）")
            date.fromisoformat(item)
            if item not in normalized:
                normalized.append(item)
        return normalized


class NoticeSectionConfig(PluginConfigBase):
    """时段开始/结束与阻断提示语配置。"""

    __ui_label__: ClassVar[str] = "提示语与通知"
    __ui_icon__: ClassVar[str] = "bell"
    __ui_order__: ClassVar[int] = 3

    enabled: bool = Field(
        default=True,
        description="是否在静默时段开始/结束时主动发送提示语。",
        json_schema_extra={"label": "启用开始/结束提示语", "x-widget": "switch"},
    )
    notify_on_startup_if_quiet: bool = Field(
        default=False,
        description="机器人启动时若已处于静默时段，是否补发一次开始提示语（默认关闭，避免重启刷屏）。",
        json_schema_extra={"label": "启动时处于静默期补发通知", "x-widget": "switch"},
    )
    default_start_message: str = Field(
        default=(
            "💤 现已进入 AI 静默时段（原因：{rule_name}，预计 {end_time} 恢复）。\n"
            "期间 AI 聊天与依赖大模型的插件已暂停，不经过大模型的指令功能仍可正常使用。"
        ),
        description="默认开始提示语，支持变量：{rule_name}、{start_time}、{end_time}、{now}、{date}、{day_type}、{holiday_name}。",
        json_schema_extra={"label": "默认开始提示语", "x-widget": "textarea"},
    )
    default_end_message: str = Field(
        default="💡 AI 静默时段（{rule_name}）已结束，AI 聊天与大模型能力已恢复正常！",
        description="默认结束提示语，支持变量：{rule_name}、{start_time}、{end_time}、{now}、{date}、{day_type}、{holiday_name}。",
        json_schema_extra={"label": "默认结束提示语", "x-widget": "textarea"},
    )
    target_mode: NoticeTargetMode = Field(
        default="all_non_exempt_groups",
        description=(
            "提示语发送范围：all_non_exempt_groups=所有非豁免群聊，"
            "all_non_exempt_streams=所有非豁免群聊与私聊，"
            "configured_only=仅发送给下方指定的通知群聊与用户。"
        ),
        json_schema_extra={"label": "通知发送范围", "x-widget": "select"},
    )
    target_groups: List[GroupTarget] = Field(
        default_factory=list,
        description="指定接收开始/结束提示语的群聊；在 configured_only 模式下仅发给这些群，在其它模式下会与非豁免会话合并去重。",
        json_schema_extra={"x-widget": "target", "label": "指定通知群聊", "hint": "搜索并选择群聊"},
    )
    target_users: List[UserTarget] = Field(
        default_factory=list,
        description="指定接收开始/结束提示语的用户私聊。",
        json_schema_extra={"x-widget": "target", "label": "指定通知用户", "hint": "搜索并选择用户"},
    )
    exclude_platforms: List[str] = Field(
        default_factory=lambda: ["local", "webui", "internal"],
        description="自动广播开始/结束提示语时跳过的平台列表。",
        json_schema_extra={"label": "广播排除平台", "advanced": True},
    )
    blocked_chat_reply: str = Field(
        default="",
        description="静默时段内普通聊天消息被拦截时的可选提示语；留空则完全静默不回复。",
        json_schema_extra={"label": "聊天拦截提示语（留空静默）", "x-widget": "textarea"},
    )
    only_reply_chat_when_mentioned_or_private: bool = Field(
        default=True,
        description="填写“聊天拦截提示语”后，群聊中仅在被 @ 或提及时回复该提示语，避免打扰普通群聊。",
        json_schema_extra={"label": "群聊仅被 @/提及时回复拦截提示", "x-widget": "switch"},
    )
    blocked_reply_cooldown_seconds: int = Field(
        default=300,
        ge=0,
        le=86400,
        description="同一聊天流发送“聊天拦截提示语”的冷却时间（秒），0 表示不限。",
        json_schema_extra={"label": "拦截提示冷却时间（秒）"},
    )
    blocked_plugin_reply: str = Field(
        default=(
            "⏸️ 当前处于 AI 静默时段（{rule_name}，预计 {end_time} 恢复），"
            "命令 /{command_name} 所属插件（{plugin_name}）依赖大模型能力，已暂时阻断。"
        ),
        description="静默时段内阻断依赖大模型的插件命令时回复的提示语；留空则静默阻断。",
        json_schema_extra={"label": "大模型插件阻断提示语", "x-widget": "textarea"},
    )

    @field_validator("target_groups", "target_users", mode="before")
    @classmethod
    def drop_blank_strings(cls, value: Any) -> Any:
        if isinstance(value, list):
            return [item for item in value if not isinstance(item, str) or item.strip()]
        return value


class PluginGuardSectionConfig(PluginConfigBase):
    """大模型插件检查与阻断配置。"""

    __ui_label__: ClassVar[str] = "大模型插件守卫"
    __ui_icon__: ClassVar[str] = "shield"
    __ui_order__: ClassVar[int] = 4

    enabled: bool = Field(
        default=True,
        description="静默时段内自动检测并阻断需要调用大模型的第三方/内置插件。",
        json_schema_extra={"label": "启用大模型插件检测与阻断", "x-widget": "switch"},
    )
    block_mode: PluginBlockMode = Field(
        default="smart",
        description=(
            "阻断策略：smart=命令通过 Hook 按豁免精细拦截，无豁免时额外临时禁用大模型插件的后台事件/Hook/工具组件；"
            "hook_only=仅通过 Hook 拦截（最适合配合豁免名单）；"
            "disable_components=静默期在组件注册表直接禁用大模型插件的非网关组件并在结束时恢复。"
        ),
        json_schema_extra={"label": "插件阻断策略", "x-widget": "select"},
    )
    enable_ast_code_scan: bool = Field(
        default=True,
        description="除检查插件 _manifest.json 的 capabilities 声明外，同时通过 AST 静态分析插件源码中对 LLM SDK/API 的直接调用。",
        json_schema_extra={"label": "启用源码 AST 静态检测", "x-widget": "switch"},
    )
    treat_model_list_capability_as_llm: bool = Field(
        default=False,
        description="是否将仅声明 llm.get_available_models（只读模型名列表）的插件视为依赖大模型（默认关闭以减少误伤）。",
        json_schema_extra={"label": "将模型列表查询能力视为大模型依赖", "x-widget": "switch", "advanced": True},
    )
    rescan_interval_seconds: int = Field(
        default=300,
        ge=30,
        le=3600,
        description="自动重新扫描已加载插件与命令列表的周期（秒）。",
        json_schema_extra={"label": "插件重扫间隔（秒）", "advanced": True},
    )
    force_block_plugins: List[str] = Field(
        default_factory=list,
        description="强制判定为“需要大模型”并在静默期阻断的插件 ID 列表（如 author.my-ai-plugin）。",
        json_schema_extra={"label": "强制阻断插件 ID 列表"},
    )
    force_allow_plugins: List[str] = Field(
        default_factory=list,
        description="强制豁免检查、静默期始终放行的插件 ID 列表。",
        json_schema_extra={"label": "强制放行插件 ID 列表"},
    )
    force_allow_commands: List[str] = Field(
        default_factory=list,
        description="静默期始终放行的单个命令，支持 plugin_id.command_name 或 command_name。",
        json_schema_extra={"label": "强制放行命令列表"},
    )


class ExemptionSectionConfig(PluginConfigBase):
    """豁免群聊与豁免用户配置。"""

    __ui_label__: ClassVar[str] = "豁免群聊与用户"
    __ui_icon__: ClassVar[str] = "users"
    __ui_order__: ClassVar[int] = 5

    exempt_groups: List[GroupTarget] = Field(
        default_factory=list,
        description="豁免群聊列表：静默时段内这些群聊仍可正常使用 AI 聊天与全部插件，且默认不接收静默提示。",
        json_schema_extra={"x-widget": "target", "label": "豁免群聊", "hint": "搜索并选择不受静默限制的群聊"},
    )
    exempt_users: List[UserTarget] = Field(
        default_factory=list,
        description="豁免用户列表：静默时段内这些用户不受 AI 关闭与大模型插件阻断限制。",
        json_schema_extra={"x-widget": "target", "label": "豁免用户", "hint": "搜索并选择不受静默限制的用户"},
    )
    user_exempt_in_group: bool = Field(
        default=True,
        description="豁免用户在非豁免群聊中发言或执行大模型命令时，是否同样放行。",
        json_schema_extra={"label": "豁免用户在群聊中同样生效", "x-widget": "switch"},
    )
    exempt_platforms: List[str] = Field(
        default_factory=lambda: ["local", "webui", "internal"],
        description="始终豁免的平台列表（默认保留本地控制台与 WebUI 聊天室可用；留空则一并受时段限制）。",
        json_schema_extra={"label": "豁免平台列表"},
    )

    @field_validator("exempt_groups", "exempt_users", mode="before")
    @classmethod
    def drop_blank_strings(cls, value: Any) -> Any:
        if isinstance(value, list):
            return [item for item in value if not isinstance(item, str) or item.strip()]
        return value


class QuietHoursPluginConfig(PluginConfigBase):
    """AI 时段静默守卫完整配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    schedule: ScheduleSectionConfig = Field(default_factory=ScheduleSectionConfig)
    holiday: HolidaySectionConfig = Field(default_factory=HolidaySectionConfig)
    notices: NoticeSectionConfig = Field(default_factory=NoticeSectionConfig)
    plugin_guard: PluginGuardSectionConfig = Field(default_factory=PluginGuardSectionConfig)
    exemptions: ExemptionSectionConfig = Field(default_factory=ExemptionSectionConfig)
