"""AI 时段静默守卫插件。

功能概览：
1. 按多条自定义时间规则与节假日/调休补班识别，在指定时段优先切换至 MaiBot 已注册回退模型继续服务；
   若未配置或无可用回退模型，则按静默规则自动关闭 AI 聊天与大模型能力；
2. 静默时段开始/结束时按配置发送自定义提示语（模型回退模式亦支持可选提示）；
3. 静默期保留不经过大模型的功能（内置命令、非大模型插件命令与非大模型 Hook）；
4. 通过 Manifest 能力声明 + 源码 AST 静态扫描自动识别所有同插件及大模型依赖，支持列表化勾选阻断/放行规则；
5. 支持豁免群聊、豁免用户与豁免平台（同时支持设置页目标选择器与插件控制台全量列表勾选）。
"""

from __future__ import annotations

from collections import deque
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar, Deque, Dict, List, Literal, Optional, Set, Tuple, cast

import asyncio
import contextlib
import json
import os
import re
import time
import tomllib

from maibot_sdk import API, Command, HomeCard, HookHandler, MaiBotPlugin
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

try:
    import tomlkit  # type: ignore
except ImportError:  # pragma: no cover - 精简测试环境可能未安装 tomlkit
    tomlkit = None  # type: ignore[assignment]

from .holiday_calendar import HolidayCalendar
from .llm_plugin_auditor import LlmPluginAuditor
from .model_fallback import (
    ALL_SWITCHABLE_TASKS,
    TASK_DISPLAY_LABELS,
    ModelCatalogSnapshot,
    ModelFallbackController,
    discover_maibot_instance_root,
)
from .schedule_engine import (
    ManualOverride,
    ScheduleEvaluation,
    evaluate_schedule,
    has_any_chat_exemptions,
    is_context_exempt,
    is_group_exempt,
    is_user_exempt,
    now_in_timezone,
    render_notice_template,
)
from .settings import ExemptionSectionConfig, GroupTarget, QuietHoursPluginConfig, UserTarget

PLUGIN_ID = "a1mue4ay.ai-quiet-hours"
_PLUGIN_DIR = Path(__file__).resolve().parent
_MAX_RECENT_EVENTS = 30
_MEDIA_PLACEHOLDERS: Dict[str, str] = {
    "image": "[图片]",
    "emoji": "[表情包]",
    "voice": "[语音消息]",
}


@dataclass(frozen=True)
class EffectiveRuntimeState:
    """结合调度求值与当前已注册模型状态得到的最终运行时模式。"""

    evaluation: ScheduleEvaluation
    mode: Literal["normal", "model_fallback", "quiet"]
    is_silencing: bool
    is_fallback: bool
    fallback_models: Tuple[str, ...] = ()
    invalid_fallback_models: Tuple[str, ...] = ()
    mode_note: str = ""


def _resolve_self_config_path(plugin_dir: Path) -> Path:
    """定位当前插件的 ``config.toml`` 路径。"""
    try:
        from src.common.paths import resolve_plugin_config_path  # type: ignore

        return Path(resolve_plugin_config_path(plugin_dir))
    except Exception:
        return plugin_dir / "config.toml"


def _format_toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        if all(not isinstance(item, dict) for item in value):
            inner = ", ".join(_format_toml_value(item) for item in value)
            return f"[{inner}]"
    return json.dumps(value, ensure_ascii=False)


def _dump_config_toml_fallback(data: Dict[str, Any]) -> str:
    """在缺少 tomlkit 时生成可被 ``tomllib`` 严格解析的配置 TOML。"""
    lines: List[str] = []
    for section_name, section_val in data.items():
        if not isinstance(section_val, dict):
            lines.append(f"{section_name} = {_format_toml_value(section_val)}")
            continue
        lines.append(f"[{section_name}]")
        list_of_tables: List[Tuple[str, List[Dict[str, Any]]]] = []
        for key, val in section_val.items():
            if isinstance(val, list) and any(isinstance(item, dict) for item in val):
                list_of_tables.append((key, [item for item in val if isinstance(item, dict)]))
            else:
                lines.append(f"{key} = {_format_toml_value(val)}")
        for table_key, items in list_of_tables:
            for item in items:
                lines.append("")
                lines.append(f"[[{section_name}.{table_key}]]")
                for sub_k, sub_v in item.items():
                    lines.append(f"{sub_k} = {_format_toml_value(sub_v)}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def _write_plugin_config_toml(config_path: Path, config_dict: Dict[str, Any]) -> None:
    """原子更新插件 ``config.toml``，优先使用 ``tomlkit`` 保留格式，无则使用干净序列化。"""
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if tomlkit is not None:
        text = tomlkit.dumps(config_dict)
    else:
        text = _dump_config_toml_fallback(config_dict)
    tmp_path = config_path.with_name(f".{config_path.name}.tmp.{os.getpid()}")
    tmp_path.write_text(text, encoding="utf-8")
    os.replace(tmp_path, config_path)


@dataclass
class _SessionMeta:
    """缓存的会话归属元数据，用于仅携带 session_id 的 Hook 判定豁免。"""

    session_id: str
    platform: str
    group_id: str = ""
    user_id: str = ""
    last_sender_id: str = ""
    is_group: bool = False
    display_name: str = ""


def _extract_message_identity(message: Any) -> Tuple[str, str, str, str]:
    """从序列化的 SessionMessage 字典中提取 ``(session_id, platform, group_id, user_id)``。"""
    if not isinstance(message, dict):
        return "", "", "", ""
    session_id = str(message.get("session_id") or "").strip()
    platform = str(message.get("platform") or "").strip()
    message_info = message.get("message_info")
    group_id = ""
    user_id = ""
    if isinstance(message_info, dict):
        group_info = message_info.get("group_info")
        if isinstance(group_info, dict):
            group_id = str(group_info.get("group_id") or "").strip()
        user_info = message_info.get("user_info")
        if isinstance(user_info, dict):
            user_id = str(user_info.get("user_id") or "").strip()
    return session_id, platform, group_id, user_id


def _sanitize_media_components_in_place(components: Any) -> bool:
    """为未识别的图片/表情/语音组件填充占位文本，阻止 SessionMessage.process() 触发 VLM/ASR。"""
    if not isinstance(components, list):
        return False
    changed = False
    for comp in components:
        if not isinstance(comp, dict):
            continue
        comp_type = str(comp.get("type") or "").strip().lower()
        if comp_type in _MEDIA_PLACEHOLDERS:
            if not str(comp.get("data") or "").strip():
                comp["data"] = _MEDIA_PLACEHOLDERS[comp_type]
                changed = True
        elif comp_type == "forward":
            nodes = comp.get("data")
            if isinstance(nodes, list):
                for node in nodes:
                    if isinstance(node, dict) and _sanitize_media_components_in_place(node.get("content")):
                        changed = True
    return changed


class AIQuietHoursPlugin(MaiBotPlugin):
    """AI 时段静默守卫插件。"""

    config_model: ClassVar[type[QuietHoursPluginConfig]] = QuietHoursPluginConfig

    def __init__(self) -> None:
        super().__init__()
        self._calendar: HolidayCalendar = HolidayCalendar()
        self._auditor: LlmPluginAuditor = LlmPluginAuditor(PLUGIN_ID, _PLUGIN_DIR)
        self._model_controller: ModelFallbackController = ModelFallbackController(
            _PLUGIN_DIR,
            _PLUGIN_DIR / "data",
        )
        self._model_catalog_snapshot: Optional[ModelCatalogSnapshot] = None
        self._model_catalog_mono: float = 0.0
        self._state_lock: asyncio.Lock = asyncio.Lock()
        self._scheduler_task: Optional[asyncio.Task[None]] = None
        self._manual_override: Optional[ManualOverride] = None
        self._current_eval: Optional[ScheduleEvaluation] = None
        self._current_state: Optional[EffectiveRuntimeState] = None
        self._session_cache: Dict[str, _SessionMeta] = {}
        self._disabled_components: Dict[Tuple[str, str], str] = {}  # (full_name, component_type) -> plugin_id
        self._blocked_reply_last_sent: Dict[str, float] = {}
        self._host_disabled_commands: List[str] = []
        self._host_require_self_mention: bool = False
        self._last_plugin_scan_mono: float = 0.0
        self._last_holiday_sync_day: str = ""
        self._recent_events: Deque[Dict[str, str]] = deque(maxlen=_MAX_RECENT_EVENTS)
        self._total_blocked_chats: int = 0
        self._total_blocked_plugins: int = 0
        self._total_blocked_models: int = 0
        self._total_fallback_switches: int = 0

    @property
    def _cfg(self) -> QuietHoursPluginConfig:
        return cast(QuietHoursPluginConfig, self.config)

    @property
    def _plugin_id(self) -> str:
        return getattr(self.ctx, "plugin_id", PLUGIN_ID) or PLUGIN_ID

    @property
    def _data_dir(self) -> Path:
        paths = getattr(self.ctx, "paths", None)
        if paths is not None and getattr(paths, "data_dir", None):
            return Path(paths.data_dir)
        return _PLUGIN_DIR / "data"

    @property
    def _state_file(self) -> Optional[Path]:
        paths = getattr(self.ctx, "paths", None)
        if paths is None:
            return None
        return Path(paths.data_dir) / "runtime_state.json"

    def get_webui_config_schema(
        self,
        *,
        plugin_id: str = "",
        plugin_name: str = "",
        plugin_version: str = "",
        plugin_description: str = "",
        plugin_author: str = "",
    ) -> Dict[str, Any]:
        """注入实时检测到的已注册模型、同插件列表与命令列表选项，减少手工填写 ID 的负担。"""
        base_schema: Dict[str, Any] = {}
        super_method = getattr(super(), "get_webui_config_schema", None)
        if callable(super_method):
            try:
                raw_schema = super_method(
                    plugin_id=plugin_id,
                    plugin_name=plugin_name,
                    plugin_version=plugin_version,
                    plugin_description=plugin_description,
                    plugin_author=plugin_author,
                )
                if isinstance(raw_schema, dict):
                    base_schema = deepcopy(raw_schema)
            except Exception:
                base_schema = {}

        if not isinstance(base_schema, dict) or not isinstance(base_schema.get("sections"), dict):
            return base_schema

        self._model_controller.set_instance_root_override(self._cfg.model_fallback.instance_root)
        catalog = self._model_controller.scan_catalog()
        if not self._auditor.records:
            # 确保未完成后台首轮扫描或仅通过清单即可查看同插件时也能提供可选项
            self._auditor.audit_sync({}, self._cfg.plugin_guard)

        sections = base_schema["sections"]
        self._inject_model_schema_choices(sections.get("model_fallback"), catalog)
        self._inject_plugin_guard_schema_choices(sections.get("plugin_guard"))
        return base_schema

    def _inject_model_schema_choices(
        self,
        section: Any,
        catalog: ModelCatalogSnapshot,
    ) -> None:
        if not isinstance(section, dict) or not isinstance(section.get("fields"), dict):
            return
        fields = section["fields"]

        fallback_field = fields.get("fallback_models")
        if isinstance(fallback_field, dict):
            choices: List[str] = []
            labels_zh: Dict[str, str] = {}
            seen: Set[str] = set()
            for model in catalog.models:
                if model.model_type not in {"llm", ""}:
                    continue
                choices.append(model.name)
                seen.add(model.name)
                labels_zh[f"choice:{model.name}"] = model.format_label()
            for current_val in self._cfg.model_fallback.fallback_models:
                if current_val not in seen:
                    choices.append(current_val)
                    seen.add(current_val)
                    labels_zh[f"choice:{current_val}"] = f"{current_val} [已保存/当前未在目录中找到]"

            fallback_field["type"] = "array"
            fallback_field["ui_type"] = "select"
            fallback_field["multiple"] = True
            fallback_field["choices"] = choices
            i18n = fallback_field.setdefault("i18n", {})
            if isinstance(i18n, dict):
                i18n.setdefault("zh-CN", {}).update(labels_zh)

        tasks_field = fields.get("target_tasks")
        if isinstance(tasks_field, dict):
            task_choices = list(ALL_SWITCHABLE_TASKS)
            for t_name in self._cfg.model_fallback.target_tasks:
                if t_name not in task_choices:
                    task_choices.append(t_name)
            tasks_field["type"] = "array"
            tasks_field["ui_type"] = "select"
            tasks_field["multiple"] = True
            tasks_field["choices"] = task_choices
            i18n = tasks_field.setdefault("i18n", {})
            if isinstance(i18n, dict):
                zh_map = i18n.setdefault("zh-CN", {})
                for t_name in task_choices:
                    current_list = catalog.task_models.get(t_name)
                    base_label = TASK_DISPLAY_LABELS.get(t_name, t_name)
                    zh_map[f"choice:{t_name}"] = (
                        f"{base_label} (当前: {', '.join(current_list)})" if current_list else base_label
                    )

    def _inject_plugin_guard_schema_choices(self, section: Any) -> None:
        if not isinstance(section, dict) or not isinstance(section.get("fields"), dict):
            return
        fields = section["fields"]
        peer_records = sorted(
            (rec for rec in self._auditor.records.values() if rec.plugin_id != self._plugin_id),
            key=lambda item: (not item.uses_llm, item.is_built_in, item.plugin_id),
        )
        plugin_choices = [rec.plugin_id for rec in peer_records]
        plugin_labels: Dict[str, str] = {}
        for rec in peer_records:
            llm_tag = "含LLM" if rec.uses_llm else "普通插件"
            runtime_tag = "已加载" if rec.loaded_in_runtime else "仅磁盘"
            origin_tag = "内置" if rec.is_built_in else ("适配器" if rec.is_adapter else "扩展")
            plugin_labels[f"choice:{rec.plugin_id}"] = (
                f"{rec.display_name} ({rec.plugin_id}) [{llm_tag} · {origin_tag} · {runtime_tag}]"
            )

        for field_key, current_list in (
            ("force_block_plugins", self._cfg.plugin_guard.force_block_plugins),
            ("force_allow_plugins", self._cfg.plugin_guard.force_allow_plugins),
        ):
            field_def = fields.get(field_key)
            if not isinstance(field_def, dict):
                continue
            merged_choices = list(plugin_choices)
            merged_labels = dict(plugin_labels)
            for val in current_list:
                if val not in merged_choices:
                    merged_choices.append(val)
                    merged_labels[f"choice:{val}"] = f"{val} [已保存/当前未检测到]"
            field_def["type"] = "array"
            field_def["ui_type"] = "select"
            field_def["multiple"] = True
            field_def["choices"] = merged_choices
            i18n = field_def.setdefault("i18n", {})
            if isinstance(i18n, dict):
                i18n.setdefault("zh-CN", {}).update(merged_labels)

        cmd_field = fields.get("force_allow_commands")
        if isinstance(cmd_field, dict):
            cmd_choices: List[str] = []
            cmd_labels: Dict[str, str] = {}
            seen_cmds: Set[str] = set()
            for rec in peer_records:
                for comp in rec.components:
                    if comp.component_type != "COMMAND":
                        continue
                    full_cmd = comp.full_name or f"{rec.plugin_id}.{comp.name}"
                    if full_cmd not in seen_cmds:
                        seen_cmds.add(full_cmd)
                        cmd_choices.append(full_cmd)
                        cmd_labels[f"choice:{full_cmd}"] = f"/{comp.name} ({full_cmd} · {rec.display_name})"
            for val in self._cfg.plugin_guard.force_allow_commands:
                if val not in seen_cmds:
                    seen_cmds.add(val)
                    cmd_choices.append(val)
                    cmd_labels[f"choice:{val}"] = f"{val} [已保存]"
            cmd_field["type"] = "array"
            cmd_field["ui_type"] = "select"
            cmd_field["multiple"] = True
            cmd_field["choices"] = cmd_choices
            i18n = cmd_field.setdefault("i18n", {})
            if isinstance(i18n, dict):
                i18n.setdefault("zh-CN", {}).update(cmd_labels)

    # ===== 生命周期 =====

    async def on_load(self) -> None:
        """初始化模型回退恢复、节假日日历、插件审计、会话缓存与后台调度循环。"""
        self._auditor = LlmPluginAuditor(self._plugin_id, _PLUGIN_DIR)
        self._model_controller = ModelFallbackController(
            _PLUGIN_DIR,
            self._data_dir,
            instance_root_override=self._cfg.model_fallback.instance_root,
        )
        # 若此前异常关闭遗留了模型回退快照，启动时先安全还原
        recovery = await asyncio.to_thread(self._model_controller.recover_on_startup)
        if recovery.changed:
            self.ctx.logger.warning("检测到未清理的模型回退备份，已自动恢复原任务模型: %s", ", ".join(recovery.updated_tasks))

        cache_path = self._data_dir / "holiday_cache.json"
        self._calendar = HolidayCalendar(cache_path=cache_path)

        await asyncio.to_thread(self._calendar.load_cache_sync)
        await asyncio.to_thread(self._load_persisted_state_sync)
        await self._refresh_host_command_settings()
        await self._refresh_session_cache()
        await self._scan_plugins()
        await self._refresh_model_catalog(force=True)

        async with self._state_lock:
            evaluation = self._evaluate_now()
            state = self._resolve_effective_state(evaluation, catalog=self._model_catalog_snapshot)
            state = await self._apply_runtime_state(state, trigger="startup")
            self._current_eval = evaluation
            self._current_state = state

            if state.is_silencing and self._cfg.notices.enabled and self._cfg.notices.notify_on_startup_if_quiet:
                await self._broadcast_transition_notice(transition="enter_quiet", current=state, previous=None)

        status_desc = (
            f"模型回退中({', '.join(state.fallback_models)})"
            if state.is_fallback
            else ("AI 静默中" if state.is_silencing else "AI 正常运行")
        )
        self._record_event(
            "startup",
            "system",
            f"插件已加载，当前状态：{status_desc}（{evaluation.today_info.format_summary()}）",
        )
        self._scheduler_task = asyncio.create_task(self._scheduler_loop(), name="ai_quiet_hours.scheduler")

        if self._cfg.holiday.enabled and self._cfg.holiday.enable_online_sync:
            asyncio.create_task(self._sync_online_holidays_safely(), name="ai_quiet_hours.holiday_sync")

        self.ctx.logger.info(
            "AI 时段静默守卫已加载: 状态=%s 今日=%s 已注册模型=%d 识别大模型插件=%d",
            status_desc,
            evaluation.today_info.format_summary(),
            len(self._model_catalog_snapshot.models) if self._model_catalog_snapshot else 0,
            sum(1 for rec in self._auditor.records.values() if rec.should_block),
        )

    async def on_unload(self, *, for_reload: bool = False) -> None:
        """停止后台循环、恢复回退前模型与临时禁用的插件组件，并清理状态标记。"""
        task = self._scheduler_task
        self._scheduler_task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        async with self._state_lock:
            await self._restore_disabled_components()
            await asyncio.to_thread(self._model_controller.deactivate, reason="unload")
            await self._clear_status_badge()
            await asyncio.to_thread(self._save_persisted_state_sync)

        self.ctx.logger.info("AI 时段静默守卫已卸载 (for_reload=%s)", for_reload)

    async def on_config_update(self, scope: str, config_data: Dict[str, object], version: str) -> None:
        """配置热重载后立即重新评估时段、回退模型与插件阻断策略。"""
        del config_data, version
        if scope == "bot":
            await self._refresh_host_command_settings()
            return
        if scope != "self":
            return

        self._model_controller.set_instance_root_override(self._cfg.model_fallback.instance_root)
        await self._refresh_host_command_settings()
        await self._scan_plugins()
        await self._refresh_model_catalog(force=True)
        await self._reconcile_schedule_state(trigger="config_update")

        if self._cfg.holiday.enabled and self._cfg.holiday.enable_online_sync:
            asyncio.create_task(self._sync_online_holidays_safely(), name="ai_quiet_hours.holiday_sync_reload")

    # ===== 持久化与后台调度 =====

    def _load_persisted_state_sync(self) -> None:
        state_file = self._state_file
        if state_file is None or not state_file.is_file():
            return
        try:
            raw = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        override_raw = raw.get("manual_override")
        if isinstance(override_raw, dict):
            mode = override_raw.get("mode")
            if mode in {"force_quiet", "force_active", "force_fallback"}:
                try:
                    created_at = datetime.fromisoformat(str(override_raw["created_at"]))
                    raw_exp = override_raw.get("expires_at")
                    expires_at = datetime.fromisoformat(str(raw_exp)) if raw_exp else None
                    raw_models = override_raw.get("fallback_models") or []
                    fb_models = tuple(str(m).strip() for m in raw_models if str(m).strip())
                    override = ManualOverride(
                        mode=mode,
                        reason=str(override_raw.get("reason") or ""),
                        created_at=created_at,
                        expires_at=expires_at,
                        fallback_models=fb_models,
                    )
                    if not override.is_expired(now_in_timezone(self._cfg.schedule.timezone)):
                        self._manual_override = override
                except (KeyError, ValueError):
                    pass

    def _save_persisted_state_sync(self) -> None:
        state_file = self._state_file
        if state_file is None:
            return
        try:
            state_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "manual_override": self._manual_override.to_dict() if self._manual_override else None,
                "saved_at": datetime.now().isoformat(timespec="seconds"),
            }
            temp_path = state_file.with_suffix(".json.tmp")
            temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp_path, state_file)
        except OSError:
            pass

    def _record_event(self, event_type: str, target: str, detail: str) -> None:
        now = now_in_timezone(self._cfg.schedule.timezone)
        self._recent_events.appendleft(
            {
                "time": now.strftime("%m-%d %H:%M:%S"),
                "event_type": event_type,
                "target": target,
                "detail": detail,
            }
        )

    def _evaluate_now(self, now: Optional[datetime] = None) -> ScheduleEvaluation:
        current_dt = now if now is not None else now_in_timezone(self._cfg.schedule.timezone)
        if self._manual_override is not None and self._manual_override.is_expired(current_dt):
            self._manual_override = None
        return evaluate_schedule(
            config=self._cfg,
            calendar=self._calendar,
            now=current_dt,
            manual_override=self._manual_override,
        )

    async def _scheduler_loop(self) -> None:
        """定期巡检时段切换、重扫插件并按日同步在线节假日。"""
        try:
            while True:
                interval = max(5, int(self._cfg.schedule.check_interval_seconds))
                await asyncio.sleep(interval)

                if not self._cfg.plugin.enabled:
                    continue

                if time.monotonic() - self._last_plugin_scan_mono >= max(30, int(self._cfg.plugin_guard.rescan_interval_seconds)):
                    await self._refresh_host_command_settings()
                    await self._refresh_session_cache()
                    await self._scan_plugins()

                today_iso = now_in_timezone(self._cfg.schedule.timezone).date().isoformat()
                if (
                    self._cfg.holiday.enabled
                    and self._cfg.holiday.enable_online_sync
                    and self._last_holiday_sync_day != today_iso
                ):
                    await self._sync_online_holidays_safely()

                await self._reconcile_schedule_state(trigger="scheduler")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.ctx.logger.error("静默时段调度循环异常退出: %s", exc, exc_info=True)

    async def _sync_online_holidays_safely(self) -> Dict[str, Any]:
        now = now_in_timezone(self._cfg.schedule.timezone)
        years = [now.year, now.year + 1]
        self._last_holiday_sync_day = now.date().isoformat()
        try:
            report = await asyncio.to_thread(
                self._calendar.sync_years_sync,
                years,
                custom_api_url=self._cfg.holiday.custom_api_url,
                timeout_seconds=self._cfg.holiday.sync_timeout_seconds,
            )
            if report["synced_years"]:
                self._record_event("holiday_sync", "calendar", f"已同步节假日年份：{report['synced_years']}")
                self.ctx.logger.info("节假日在线同步成功: years=%s", report["synced_years"])
            elif report["errors"]:
                self.ctx.logger.warning("节假日在线同步失败，继续使用内置/本地缓存日历: %s", "; ".join(report["errors"]))
            return report
        except Exception as exc:
            self.ctx.logger.warning("节假日在线同步异常，已回退本地日历: %s", exc)
            return {"synced_years": [], "errors": [str(exc)], "cached_years": []}

    async def _refresh_model_catalog(self, *, force: bool = False) -> ModelCatalogSnapshot:
        now_mono = time.monotonic()
        if not force and self._model_catalog_snapshot is not None and (now_mono - self._model_catalog_mono) < 10.0:
            return self._model_catalog_snapshot
        snapshot = await asyncio.to_thread(self._model_controller.scan_catalog)
        self._model_catalog_snapshot = snapshot
        self._model_catalog_mono = now_mono
        return snapshot

    def _resolve_effective_state(
        self,
        evaluation: ScheduleEvaluation,
        *,
        catalog: Optional[ModelCatalogSnapshot] = None,
    ) -> EffectiveRuntimeState:
        if not self._cfg.plugin.enabled:
            return EffectiveRuntimeState(
                evaluation=evaluation,
                mode="normal",
                is_silencing=False,
                is_fallback=False,
                mode_note="插件未启用",
            )

        if not evaluation.is_quiet:
            return EffectiveRuntimeState(
                evaluation=evaluation,
                mode="normal",
                is_silencing=False,
                is_fallback=False,
                mode_note=evaluation.reason,
            )

        if evaluation.preferred_mode == "model_fallback":
            valid, invalid, snap = self._model_controller.resolve_effective_models(
                evaluation.preferred_fallback_models,
                catalog=catalog or self._model_catalog_snapshot,
            )
            if valid:
                note = f"模型回退中：已切换至 {', '.join(valid)}（不进行静默）"
                if invalid:
                    note += f"；未注册已忽略: {', '.join(invalid)}"
                return EffectiveRuntimeState(
                    evaluation=evaluation,
                    mode="model_fallback",
                    is_silencing=False,
                    is_fallback=True,
                    fallback_models=tuple(valid),
                    invalid_fallback_models=tuple(invalid),
                    mode_note=note,
                )

            if not self._cfg.model_fallback.fallback_to_quiet_if_unavailable and evaluation.preferred_fallback_models:
                return EffectiveRuntimeState(
                    evaluation=evaluation,
                    mode="normal",
                    is_silencing=False,
                    is_fallback=False,
                    invalid_fallback_models=tuple(invalid),
                    mode_note=snap.error or f"回退模型不可用({', '.join(invalid)})且未开启失败转静默，保持正常运行",
                )

            reason_prefix = (
                f"回退模型不可用({', '.join(invalid)})"
                if invalid
                else (snap.error or "未配置可用回退模型")
            )
            return EffectiveRuntimeState(
                evaluation=evaluation,
                mode="quiet",
                is_silencing=True,
                is_fallback=False,
                invalid_fallback_models=tuple(invalid),
                mode_note=f"{reason_prefix}，已自动使用静默规则",
            )

        return EffectiveRuntimeState(
            evaluation=evaluation,
            mode="quiet",
            is_silencing=True,
            is_fallback=False,
            mode_note=f"静默规则生效中：{evaluation.rule_names}",
        )

    async def _apply_runtime_state(
        self,
        state: EffectiveRuntimeState,
        *,
        trigger: str,
    ) -> EffectiveRuntimeState:
        """同步模型回退切换、大模型组件拦截与状态徽章，返回最终核实后的状态。"""
        effective_state = state
        if effective_state.mode == "model_fallback":
            desired_models = list(effective_state.fallback_models)
            desired_tasks = list(self._cfg.model_fallback.target_tasks)
            active_summary = self._model_controller.get_active_summary()
            already_matching = (
                active_summary.get("active")
                and active_summary.get("applied_models") == desired_models
                and set((active_summary.get("applied_tasks") or {}).keys()) >= (set(desired_tasks) & {"replyer", "planner", "utils"})
            )
            if not already_matching or trigger in {"config_update", "manual_fallback", "startup"}:
                switch_res = await asyncio.to_thread(
                    self._model_controller.activate,
                    desired_models,
                    desired_tasks,
                    switch_mode=self._cfg.model_fallback.switch_mode,
                )
                if not switch_res.ok:
                    self.ctx.logger.warning("切换回退模型失败: %s", switch_res.error or switch_res.warning)
                    if self._cfg.model_fallback.fallback_to_quiet_if_unavailable:
                        effective_state = EffectiveRuntimeState(
                            evaluation=effective_state.evaluation,
                            mode="quiet",
                            is_silencing=True,
                            is_fallback=False,
                            invalid_fallback_models=effective_state.invalid_fallback_models,
                            mode_note=f"模型切换失败({switch_res.error})，已降级为静默拦截",
                        )
                elif switch_res.changed:
                    self._total_fallback_switches += 1
                    self._record_event(
                        "model_fallback",
                        trigger,
                        f"已切换任务模型至 {', '.join(switch_res.applied_models)} (涉及任务: {', '.join(switch_res.updated_tasks)})",
                    )
                    self.ctx.logger.info(
                        "时段回退模型已激活 [%s]: models=%s tasks=%s",
                        trigger,
                        switch_res.applied_models,
                        switch_res.updated_tasks,
                    )
                    await self._refresh_model_catalog(force=True)
        elif self._model_controller.is_active():
            restore_res = await asyncio.to_thread(self._model_controller.deactivate, reason=trigger)
            if restore_res.changed:
                self._record_event(
                    "model_restored",
                    trigger,
                    f"已恢复原任务模型配置 (涉及任务: {', '.join(restore_res.updated_tasks)})",
                )
                self.ctx.logger.info("已恢复原任务模型 [%s]: tasks=%s", trigger, restore_res.updated_tasks)
                await self._refresh_model_catalog(force=True)

        await self._sync_component_blocks(effective_state.evaluation, is_silencing=effective_state.is_silencing)
        await self._publish_status_badge(effective_state)
        return effective_state

    async def _reconcile_schedule_state(self, *, trigger: str) -> ScheduleEvaluation:
        """对比新旧状态（normal / model_fallback / quiet），在跨越边界时同步切换与通知。

        ``self._current_eval`` / ``self._current_state`` 是唯一的巡检基线，只在这里
        （以及 on_load 初始化时）更新，因此两条巡检之间无论涌入多少条消息，边界切换都不会漏判。
        """
        async with self._state_lock:
            previous_eval = self._current_eval
            previous_state = self._current_state
            if not self._cfg.plugin.enabled and previous_state is not None:
                pass
            catalog = await self._refresh_model_catalog(force=False)
            current_eval = self._evaluate_now()
            desired_state = self._resolve_effective_state(current_eval, catalog=catalog)
            current_state = await self._apply_runtime_state(desired_state, trigger=trigger)
            self._current_eval = current_eval
            self._current_state = current_state

            prev_mode = previous_state.mode if previous_state is not None else "normal"
            new_mode = current_state.mode

            if previous_state is not None and prev_mode != new_mode:
                if new_mode == "quiet":
                    self._record_event("transition", trigger, f"进入 AI 静默时段（{current_eval.rule_names}）")
                    self.ctx.logger.info("状态切换 [%s]: %s -> quiet (%s)", trigger, prev_mode, current_state.mode_note)
                    if self._cfg.notices.enabled:
                        await self._broadcast_transition_notice(
                            transition="enter_quiet",
                            current=current_state,
                            previous=previous_state,
                        )
                elif prev_mode == "quiet":
                    self._record_event(
                        "transition",
                        trigger,
                        f"结束 AI 静默时段（{previous_eval.rule_names if previous_eval else ''} -> {new_mode}）",
                    )
                    self.ctx.logger.info("状态切换 [%s]: quiet -> %s (%s)", trigger, new_mode, current_state.mode_note)
                    if self._cfg.notices.enabled:
                        await self._broadcast_transition_notice(
                            transition="exit_quiet",
                            current=current_state,
                            previous=previous_state,
                        )
                    if new_mode == "model_fallback" and self._cfg.notices.enabled:
                        await self._broadcast_transition_notice(
                            transition="enter_fallback",
                            current=current_state,
                            previous=previous_state,
                        )
                elif new_mode == "model_fallback":
                    self._record_event("transition", trigger, f"进入模型回退时段（{', '.join(current_state.fallback_models)}）")
                    self.ctx.logger.info("状态切换 [%s]: %s -> model_fallback (%s)", trigger, prev_mode, current_state.mode_note)
                    if self._cfg.notices.enabled:
                        await self._broadcast_transition_notice(
                            transition="enter_fallback",
                            current=current_state,
                            previous=previous_state,
                        )
                elif prev_mode == "model_fallback" and new_mode == "normal":
                    self._record_event("transition", trigger, "结束模型回退时段，已恢复原模型")
                    self.ctx.logger.info("状态切换 [%s]: model_fallback -> normal", trigger)
                    if self._cfg.notices.enabled:
                        await self._broadcast_transition_notice(
                            transition="exit_fallback",
                            current=current_state,
                            previous=previous_state,
                        )
            return current_eval

    # ===== 宿主信息缓存与插件审计 =====

    async def _refresh_host_command_settings(self) -> None:
        try:
            disabled = await self.ctx.config.get("plugin.disabled_commands", [])
            if isinstance(disabled, list):
                self._host_disabled_commands = [str(item) for item in disabled if str(item).strip()]
            require_mention = await self.ctx.config.get("plugin.command_require_self_mention", False)
            self._host_require_self_mention = bool(require_mention)
        except Exception as exc:
            self.ctx.logger.debug("读取宿主命令配置失败，沿用默认值: %s", exc)

    async def _refresh_session_cache(self) -> List[Dict[str, Any]]:
        try:
            raw_streams = await self.ctx.chat.get_all_streams(platform="all_platforms")
        except Exception as exc:
            self.ctx.logger.warning("查询聊天流列表失败: %s", exc)
            return []

        streams: List[Dict[str, Any]]
        if isinstance(raw_streams, list):
            streams = [item for item in raw_streams if isinstance(item, dict)]
        elif isinstance(raw_streams, dict) and isinstance(raw_streams.get("streams"), list):
            streams = [item for item in raw_streams["streams"] if isinstance(item, dict)]
        else:
            streams = []

        for stream in streams:
            session_id = str(stream.get("session_id") or stream.get("stream_id") or "").strip()
            if not session_id:
                continue
            existing = self._session_cache.get(session_id)
            platform = str(stream.get("platform") or "").strip()
            group_id = str(stream.get("group_id") or "").strip()
            user_id = str(stream.get("user_id") or "").strip()
            is_group = bool(stream.get("is_group_session") or group_id)
            display_name = str(stream.get("group_name") or stream.get("user_nickname") or group_id or user_id or session_id)
            self._session_cache[session_id] = _SessionMeta(
                session_id=session_id,
                platform=platform,
                group_id=group_id,
                user_id=user_id,
                last_sender_id=existing.last_sender_id if existing is not None else user_id,
                is_group=is_group,
                display_name=display_name,
            )
        return streams

    def _remember_session_from_message(self, message: Any) -> Tuple[str, str, str, str]:
        session_id, platform, group_id, user_id = _extract_message_identity(message)
        if session_id:
            existing = self._session_cache.get(session_id)
            is_group = bool(group_id)
            self._session_cache[session_id] = _SessionMeta(
                session_id=session_id,
                platform=platform or (existing.platform if existing else ""),
                group_id=group_id or (existing.group_id if existing else ""),
                user_id=(existing.user_id if existing and is_group else user_id),
                last_sender_id=user_id or (existing.last_sender_id if existing else ""),
                is_group=is_group,
                display_name=existing.display_name if existing else (group_id or user_id or session_id),
            )
        return session_id, platform, group_id, user_id

    async def _is_session_exempt(self, session_id: str) -> Tuple[bool, str]:
        normalized_id = session_id.strip()
        if not normalized_id:
            return False, ""
        meta = self._session_cache.get(normalized_id)
        if meta is None:
            await self._refresh_session_cache()
            meta = self._session_cache.get(normalized_id)
        if meta is None:
            return False, ""
        user_id_for_check = meta.last_sender_id if meta.is_group else (meta.user_id or meta.last_sender_id)
        return is_context_exempt(
            self._cfg.exemptions,
            platform=meta.platform,
            group_id=meta.group_id if meta.is_group else "",
            user_id=user_id_for_check,
        )

    async def _scan_plugins(self) -> Dict[str, Any]:
        """扫描已加载与已安装插件，更新大模型插件审计表与命令匹配索引。"""
        runtime_plugins: Dict[str, Any] = {}
        try:
            raw = await self.ctx.component.get_all_plugins()
            if isinstance(raw, dict):
                if isinstance(raw.get("plugins"), dict) and "success" in raw:
                    runtime_plugins = raw["plugins"]
                else:
                    runtime_plugins = raw
        except Exception as exc:
            self.ctx.logger.warning("获取已注册插件列表失败，将仅基于磁盘清单审计: %s", exc)

        scan_ts = now_in_timezone(self._cfg.schedule.timezone).strftime("%Y-%m-%d %H:%M:%S")
        records = await asyncio.to_thread(
            self._auditor.audit_sync,
            runtime_plugins,
            self._cfg.plugin_guard,
            scan_timestamp=scan_ts,
        )
        self._last_plugin_scan_mono = time.monotonic()
        return records

    async def _sync_component_blocks(
        self,
        evaluation: ScheduleEvaluation,
        *,
        is_silencing: Optional[bool] = None,
    ) -> None:
        """按配置策略同步大模型插件组件的临时禁用/恢复状态；模型回退期间不禁用组件。"""
        active_silencing = evaluation.is_quiet if is_silencing is None else is_silencing
        if not self._cfg.plugin.enabled or not self._cfg.plugin_guard.enabled or not active_silencing:
            await self._restore_disabled_components()
            return

        mode = self._cfg.plugin_guard.block_mode
        if mode == "hook_only":
            await self._restore_disabled_components()
            return

        # smart 模式下若配置了豁免群或豁免用户，则保留组件启用并依赖 Hook 按会话精细拦截，
        # 确保豁免对象在静默期仍可完整调用 AI 工具与大模型插件。
        if mode == "smart" and has_any_chat_exemptions(self._cfg.exemptions):
            await self._restore_disabled_components()
            return

        include_commands = mode == "disable_components"
        desired = {
            (full_name, comp_type): plugin_id
            for plugin_id, full_name, comp_type in self._auditor.get_disableable_components(
                include_commands=include_commands
            )
        }

        # 恢复不再需要禁用的组件
        for key in list(self._disabled_components.keys()):
            if key not in desired:
                full_name, comp_type = key
                await self._enable_component_safely(full_name, comp_type)
                self._disabled_components.pop(key, None)

        # 禁用新增目标组件
        for key, plugin_id in desired.items():
            if key in self._disabled_components:
                continue
            full_name, comp_type = key
            if await self._disable_component_safely(full_name, comp_type):
                self._disabled_components[key] = plugin_id

    async def _restore_disabled_components(self) -> None:
        if not self._disabled_components:
            return
        for full_name, comp_type in list(self._disabled_components.keys()):
            await self._enable_component_safely(full_name, comp_type)
            self._disabled_components.pop((full_name, comp_type), None)

    async def _disable_component_safely(self, full_name: str, component_type: str) -> bool:
        try:
            result = await self.ctx.component.disable_component(full_name, component_type)
            if isinstance(result, dict) and result.get("success") is False:
                self.ctx.logger.warning("临时禁用组件失败 %s (%s): %s", full_name, component_type, result.get("error"))
                return False
            self.ctx.logger.info("静默期已临时禁用大模型插件组件: %s (%s)", full_name, component_type)
            return True
        except Exception as exc:
            self.ctx.logger.warning("临时禁用组件异常 %s (%s): %s", full_name, component_type, exc)
            return False

    async def _enable_component_safely(self, full_name: str, component_type: str) -> bool:
        try:
            result = await self.ctx.component.enable_component(full_name, component_type)
            if isinstance(result, dict) and result.get("success") is False:
                self.ctx.logger.warning("恢复组件失败 %s (%s): %s", full_name, component_type, result.get("error"))
                return False
            self.ctx.logger.info("已恢复大模型插件组件: %s (%s)", full_name, component_type)
            return True
        except Exception as exc:
            self.ctx.logger.warning("恢复组件异常 %s (%s): %s", full_name, component_type, exc)
            return False

    # ===== WebUI 运行状态徽章 =====

    async def _publish_status_badge(self, state: EffectiveRuntimeState | ScheduleEvaluation) -> None:
        if isinstance(state, EffectiveRuntimeState):
            if state.is_fallback:
                models_text = ",".join(state.fallback_models) or "回退"
                label = f"模型回退 · {models_text}"[:60]
            elif state.is_silencing:
                label = f"AI 静默中 · {state.evaluation.rule_names}"[:60]
            else:
                label = f"AI 运行中 · {state.evaluation.today_info.label}"[:60]
        else:
            label = (
                f"AI 静默中 · {state.rule_names}"[:60]
                if state.is_quiet
                else f"AI 运行中 · {state.today_info.label}"[:60]
            )
        try:
            await self.ctx.call_capability(
                "runtime.set_status_badges",
                badges=[{"id": "quiet_hours_state", "label": label}],
            )
        except Exception as exc:
            self.ctx.logger.debug("更新运行状态徽章失败: %s", exc)

    async def _clear_status_badge(self) -> None:
        try:
            await self.ctx.call_capability("runtime.set_status_badges", badges=[])
        except Exception:
            pass

    # ===== 开始/结束提示语广播 =====

    def _resolve_transition_message(
        self,
        *,
        transition: Literal["enter_quiet", "exit_quiet", "enter_fallback", "exit_fallback"],
        current: EffectiveRuntimeState,
        previous: Optional[EffectiveRuntimeState],
    ) -> Optional[str]:
        cur_eval = current.evaluation
        prev_eval = previous.evaluation if previous is not None else None
        extra: Dict[str, Any] = {
            "fallback_models": ", ".join(
                current.fallback_models or (previous.fallback_models if previous else ())
            ) or "无",
        }
        if transition == "enter_quiet":
            rule = cur_eval.primary_rule
            if rule is not None and cur_eval.manual_override is None:
                if not rule.notify_on_start:
                    return None
                template = rule.start_message.strip() or self._cfg.notices.default_start_message.strip()
            else:
                template = self._cfg.notices.default_start_message.strip()
        elif transition == "exit_quiet":
            prev_rule = prev_eval.primary_rule if prev_eval is not None else None
            if prev_rule is not None and (prev_eval is None or prev_eval.manual_override is None):
                if not prev_rule.notify_on_end:
                    return None
                template = prev_rule.end_message.strip() or self._cfg.notices.default_end_message.strip()
            else:
                template = self._cfg.notices.default_end_message.strip()
        elif transition == "enter_fallback":
            rule = cur_eval.primary_rule
            if rule is not None and cur_eval.manual_override is None and not rule.notify_on_start:
                return None
            template = self._cfg.notices.fallback_enter_notice.strip()
        else:
            prev_rule = prev_eval.primary_rule if prev_eval is not None else None
            if prev_rule is not None and (prev_eval is None or prev_eval.manual_override is None) and not prev_rule.notify_on_end:
                return None
            template = self._cfg.notices.fallback_exit_notice.strip()

        if not template or template == "-":
            return None
        return render_notice_template(template, cur_eval, previous_evaluation=prev_eval, extra=extra)

    async def _resolve_notice_stream_ids(self) -> List[str]:
        streams = await self._refresh_session_cache()
        notices_cfg = self._cfg.notices
        exemptions = self._cfg.exemptions
        excluded_platforms = {p.strip().lower() for p in notices_cfg.exclude_platforms if p.strip()}

        selected: List[str] = []
        seen: Set[str] = set()

        def _add(stream_id: str) -> None:
            if stream_id and stream_id not in seen:
                seen.add(stream_id)
                selected.append(stream_id)

        # 显式配置的通知目标（按 platform/type/id 匹配已有聊天流）
        for target in notices_cfg.target_groups:
            for stream in streams:
                if not stream.get("is_group_session") and not stream.get("group_id"):
                    continue
                platform = str(stream.get("platform") or "").strip().lower()
                group_id = str(stream.get("group_id") or "").strip()
                if group_id != target.id:
                    continue
                if target.platform and target.platform.lower() != platform:
                    continue
                _add(str(stream.get("session_id") or stream.get("stream_id") or "").strip())

        for target in notices_cfg.target_users:
            for stream in streams:
                if stream.get("is_group_session") or stream.get("group_id"):
                    continue
                platform = str(stream.get("platform") or "").strip().lower()
                user_id = str(stream.get("user_id") or "").strip()
                if user_id != target.id:
                    continue
                if target.platform and target.platform.lower() != platform:
                    continue
                _add(str(stream.get("session_id") or stream.get("stream_id") or "").strip())

        if notices_cfg.target_mode == "configured_only":
            return selected

        include_private = notices_cfg.target_mode == "all_non_exempt_streams"
        for stream in streams:
            stream_id = str(stream.get("session_id") or stream.get("stream_id") or "").strip()
            if not stream_id:
                continue
            platform = str(stream.get("platform") or "").strip().lower()
            if platform in excluded_platforms:
                continue
            group_id = str(stream.get("group_id") or "").strip()
            user_id = str(stream.get("user_id") or "").strip()
            is_group = bool(stream.get("is_group_session") or group_id)

            if is_group:
                if is_group_exempt(exemptions, platform, group_id):
                    continue
                _add(stream_id)
            elif include_private:
                if is_user_exempt(exemptions, platform, user_id):
                    continue
                _add(stream_id)

        return selected

    async def _broadcast_transition_notice(
        self,
        *,
        transition: Literal["enter_quiet", "exit_quiet", "enter_fallback", "exit_fallback"],
        current: EffectiveRuntimeState,
        previous: Optional[EffectiveRuntimeState],
    ) -> int:
        text = self._resolve_transition_message(
            transition=transition,
            current=current,
            previous=previous,
        )
        if not text:
            return 0

        stream_ids = await self._resolve_notice_stream_ids()
        sent_count = 0
        for index, stream_id in enumerate(stream_ids):
            try:
                ok = await self.ctx.send.text(text, stream_id, storage_message=False)
                if ok:
                    sent_count += 1
            except Exception as exc:
                self.ctx.logger.warning("向聊天流 %s 发送时段提示语失败: %s", stream_id, exc)
            if index + 1 < len(stream_ids):
                await asyncio.sleep(0.2)

        label_map = {
            "enter_quiet": "静默开始",
            "exit_quiet": "静默结束",
            "enter_fallback": "模型回退开始",
            "exit_fallback": "模型回退结束",
        }
        self.ctx.logger.info(
            "已广播时段%s提示语: 目标会话=%d 成功=%d",
            label_map.get(transition, transition),
            len(stream_ids),
            sent_count,
        )
        return sent_count

    async def _maybe_send_blocked_chat_reply(
        self,
        message: Dict[str, Any],
        session_id: str,
        evaluation: ScheduleEvaluation,
    ) -> None:
        template = self._cfg.notices.blocked_chat_reply.strip()
        if not template or not session_id:
            return

        _, _, group_id, _ = _extract_message_identity(message)
        is_group = bool(group_id)
        if is_group and self._cfg.notices.only_reply_chat_when_mentioned_or_private:
            if not (message.get("is_at") or message.get("is_mentioned")):
                return

        cooldown = int(self._cfg.notices.blocked_reply_cooldown_seconds)
        now_mono = time.monotonic()
        if cooldown > 0:
            last_sent = self._blocked_reply_last_sent.get(session_id, 0.0)
            if now_mono - last_sent < cooldown:
                return
        self._blocked_reply_last_sent[session_id] = now_mono

        reply_text = render_notice_template(template, evaluation)
        if not reply_text:
            return
        try:
            await self.ctx.send.text(reply_text, session_id, storage_message=False)
        except Exception as exc:
            self.ctx.logger.debug("发送静默聊天提示失败 (%s): %s", session_id, exc)

    # ===== 核心拦截 Hook =====

    def _get_live_evaluation(self) -> ScheduleEvaluation:
        """按当前时间求值，供 Hook 即时判定使用。

        这里刻意不写回 ``self._current_eval``：它是 ``_reconcile_schedule_state`` 用来识别
        静默开始/结束的巡检基线，任何一条普通消息触发的 Hook 都不能改写它，否则时段边界
        的切换会被判定成“没有变化”，开始/结束提示语随之丢失。
        """
        return self._evaluate_now()

    def _get_live_state(self) -> EffectiveRuntimeState:
        """结合当前调度与已缓存的模型目录计算实时运行态，不改写巡检基线。"""
        evaluation = self._get_live_evaluation()
        if evaluation.preferred_mode == "model_fallback" and self._model_catalog_snapshot is None:
            self._model_catalog_snapshot = self._model_controller.scan_catalog()
            self._model_catalog_mono = time.monotonic()
        return self._resolve_effective_state(evaluation, catalog=self._model_catalog_snapshot)

    @HookHandler(
        "chat.receive.before_process",
        name="guard_media_ai_before_process",
        description="静默时段内为未识别的图片/表情/语音填充占位文本，阻断预处理阶段 VLM/ASR 模型调用，同时保留消息进入后续非大模型链路。",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        timeout_ms=3000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_before_process(self, message: Optional[Dict[str, Any]] = None, **kwargs: Any) -> Dict[str, Any]:
        del kwargs
        if not isinstance(message, dict):
            return {"action": "continue"}

        session_id, platform, group_id, user_id = self._remember_session_from_message(message)
        if not self._cfg.plugin.enabled or not self._cfg.schedule.block_media_ai:
            return {"action": "continue"}

        state = self._get_live_state()
        if not state.is_silencing:
            return {"action": "continue"}

        exempt, _ = is_context_exempt(
            self._cfg.exemptions,
            platform=platform,
            group_id=group_id,
            user_id=user_id,
        )
        if exempt:
            return {"action": "continue"}

        # 命令消息保留原始媒体组件，确保非大模型命令（如图片处理指令）仍可读取附件
        if self._auditor.is_command_message(
            message,
            disabled_commands=self._host_disabled_commands,
            command_require_self_mention=self._host_require_self_mention,
        ):
            return {"action": "continue"}

        raw_components = message.get("raw_message")
        if not isinstance(raw_components, list):
            return {"action": "continue"}

        cloned_message = json.loads(json.dumps(message, ensure_ascii=False))
        if _sanitize_media_components_in_place(cloned_message.get("raw_message")):
            self._total_blocked_models += 1
            self._record_event("media_ai_skipped", session_id or platform, "静默期跳过图片/语音/表情模型识别")
            return {"action": "continue", "modified_kwargs": {"message": cloned_message}}

        return {"action": "continue"}

    @HookHandler(
        "chat.receive.after_process",
        name="guard_ai_chat_after_process",
        description="静默时段内放行命令消息并阻断普通聊天进入 Maisaka AI 规划与回复链路；模型回退期间不拦截。",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        timeout_ms=3000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_after_process(self, message: Optional[Dict[str, Any]] = None, **kwargs: Any) -> Dict[str, Any]:
        del kwargs
        if not isinstance(message, dict):
            return {"action": "continue"}

        session_id, platform, group_id, user_id = self._remember_session_from_message(message)
        if not self._cfg.plugin.enabled:
            return {"action": "continue"}

        state = self._get_live_state()
        if not state.is_silencing:
            return {"action": "continue"}

        evaluation = state.evaluation
        exempt, _ = is_context_exempt(
            self._cfg.exemptions,
            platform=platform,
            group_id=group_id,
            user_id=user_id,
        )
        if exempt:
            return {"action": "continue"}

        # 放行所有内置与插件命令（大模型插件命令将在 chat.command.before_execute 被精细阻断）
        if self._auditor.is_command_message(
            message,
            disabled_commands=self._host_disabled_commands,
            command_require_self_mention=self._host_require_self_mention,
        ):
            return {"action": "continue"}

        self._total_blocked_chats += 1
        target_label = f"{platform}:{group_id or user_id or session_id}"
        self._record_event("chat_blocked", target_label, f"静默期拦截普通 AI 聊天（{evaluation.rule_names}）")
        await self._maybe_send_blocked_chat_reply(message, session_id, evaluation)

        return {
            "action": "abort",
            "custom_result": {
                "reason": f"当前处于 AI 静默时段（{evaluation.rule_names}，预计 {evaluation.end_time_text} 恢复），已暂停 AI 聊天。"
            },
        }

    @HookHandler(
        "chat.command.before_execute",
        name="guard_llm_plugin_commands",
        description="静默时段内检查命令所属插件是否依赖大模型，阻断大模型插件命令并保留非大模型命令；模型回退期间不拦截。",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        timeout_ms=3000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_command_before_execute(
        self,
        message: Optional[Dict[str, Any]] = None,
        command_name: str = "",
        plugin_id: str = "",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        del kwargs
        if not self._cfg.plugin.enabled or not self._cfg.plugin_guard.enabled:
            return {"action": "continue"}

        session_id, platform, group_id, user_id = self._remember_session_from_message(message)
        state = self._get_live_state()
        if not state.is_silencing:
            return {"action": "continue"}

        evaluation = state.evaluation
        exempt, _ = is_context_exempt(
            self._cfg.exemptions,
            platform=platform,
            group_id=group_id,
            user_id=user_id,
        )
        if exempt:
            return {"action": "continue"}

        blocked, record = self._auditor.should_block_command(plugin_id, command_name, self._cfg.plugin_guard)
        if not blocked:
            return {"action": "continue"}

        self._total_blocked_plugins += 1
        plugin_name = record.display_name if record is not None else plugin_id
        reasons_text = "；".join(record.reasons) if record is not None and record.reasons else "检测到依赖大模型"
        self._record_event("plugin_blocked", f"{plugin_id}.{command_name}", f"已阻断大模型插件命令：{reasons_text}")

        reply_template = self._cfg.notices.blocked_plugin_reply.strip()
        if reply_template and session_id:
            notice = render_notice_template(
                reply_template,
                evaluation,
                extra={
                    "plugin_id": plugin_id,
                    "plugin_name": plugin_name,
                    "command_name": command_name,
                    "reasons": reasons_text,
                },
            )
            if notice:
                try:
                    await self.ctx.send.text(notice, session_id, storage_message=False)
                except Exception as exc:
                    self.ctx.logger.debug("发送大模型插件阻断提示失败: %s", exc)

        return {
            "action": "abort",
            "custom_result": {
                "reason": f"静默时段已阻断依赖大模型的插件命令 {plugin_id}.{command_name}（{reasons_text}）"
            },
        }

    @HookHandler(
        "chat.command.after_execute",
        name="prevent_command_fallthrough_to_ai",
        description="静默时段内防止不拦截消息的非大模型命令落入 Maisaka AI 对话主链。",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        timeout_ms=3000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_command_after_execute(
        self,
        message: Optional[Dict[str, Any]] = None,
        continue_process: bool = False,
        intercept_message_level: int = 1,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        if not self._cfg.plugin.enabled or not continue_process:
            return {"action": "continue"}

        _, platform, group_id, user_id = self._remember_session_from_message(message)
        state = self._get_live_state()
        if not state.is_silencing:
            return {"action": "continue"}

        exempt, _ = is_context_exempt(
            self._cfg.exemptions,
            platform=platform,
            group_id=group_id,
            user_id=user_id,
        )
        if exempt:
            return {"action": "continue"}

        return {
            "action": "continue",
            "modified_kwargs": {
                **kwargs,
                "message": message,
                "continue_process": False,
                "intercept_message_level": max(int(intercept_message_level), 1),
            },
        }

    @HookHandler(
        "maisaka.replyer.before_request",
        name="guard_replyer_before_request",
        description="静默时段内阻断 Maisaka Replyer 生成；模型回退时段内即时注入回退模型名称。",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        timeout_ms=3000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_replyer_before_request(self, session_id: str = "", **kwargs: Any) -> Dict[str, Any]:
        if not self._cfg.plugin.enabled:
            return {"action": "continue"}

        state = self._get_live_state()
        if state.is_fallback and state.fallback_models:
            exempt, _ = await self._is_session_exempt(session_id)
            if exempt:
                active_summary = self._model_controller.get_active_summary()
                orig_replyer = (active_summary.get("original_tasks") or {}).get("replyer") or []
                if orig_replyer:
                    return {
                        "action": "continue",
                        "modified_kwargs": {**kwargs, "session_id": session_id, "model_name": orig_replyer[0]},
                    }
                return {"action": "continue"}
            return {
                "action": "continue",
                "modified_kwargs": {**kwargs, "session_id": session_id, "model_name": state.fallback_models[0]},
            }

        return await self._guard_model_hook_for_session(session_id, "maisaka.replyer.before_request")

    @HookHandler(
        "maisaka.replyer.before_model_request",
        name="guard_replyer_before_model_request",
        description="静默时段内阻断 Replyer 实际模型请求。",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        timeout_ms=3000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_replyer_before_model_request(self, session_id: str = "", **kwargs: Any) -> Dict[str, Any]:
        del kwargs
        return await self._guard_model_hook_for_session(session_id, "maisaka.replyer.before_model_request")

    @HookHandler(
        "expression.select.before_select",
        name="guard_expression_selector",
        description="静默时段内阻断表达方式选择模型调用。",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        timeout_ms=3000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_expression_before_select(self, chat_id: str = "", **kwargs: Any) -> Dict[str, Any]:
        del kwargs
        return await self._guard_model_hook_for_session(chat_id, "expression.select.before_select")

    @HookHandler(
        "expression.learn.after_extract",
        name="guard_expression_learning",
        description="静默时段内跳过非豁免会话的表达学习批次。",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        timeout_ms=3000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_expression_after_extract(self, session_id: str = "", **kwargs: Any) -> Dict[str, Any]:
        del kwargs
        return await self._guard_model_hook_for_session(session_id, "expression.learn.after_extract")

    @HookHandler(
        "jargon.extract.before_persist",
        name="guard_jargon_persist",
        description="静默时段内跳过非豁免会话的黑话持久化。",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        timeout_ms=3000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_jargon_before_persist(self, session_id: str = "", **kwargs: Any) -> Dict[str, Any]:
        del kwargs
        return await self._guard_model_hook_for_session(session_id, "jargon.extract.before_persist")

    @HookHandler(
        "emoji.receive.before_cache",
        name="guard_emoji_vlm_cache",
        description="静默时段内跳过非豁免会话的新表情包视觉模型识别与缓存。",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        timeout_ms=3000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_emoji_before_cache(self, session_id: str = "", **kwargs: Any) -> Dict[str, Any]:
        del kwargs
        if not self._cfg.schedule.block_media_ai:
            return {"action": "continue"}
        return await self._guard_model_hook_for_session(session_id, "emoji.receive.before_cache")

    @HookHandler(
        "emoji.maintenance.before_register",
        name="guard_emoji_maintenance_vlm",
        description="静默时段内暂停定时表情包维护的视觉模型注册。",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        timeout_ms=3000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_emoji_maintenance_before_register(self, **kwargs: Any) -> Dict[str, Any]:
        del kwargs
        if not self._cfg.plugin.enabled or not self._cfg.schedule.block_background_emoji_maintenance:
            return {"action": "continue"}
        state = self._get_live_state()
        if not state.is_silencing:
            return {"action": "continue"}
        self._total_blocked_models += 1
        return {
            "action": "abort",
            "custom_result": {"reason": f"当前处于 AI 静默时段（{state.evaluation.rule_names}），已暂停后台表情包识图注册。"},
        }

    async def _guard_model_hook_for_session(self, session_id: str, hook_name: str) -> Dict[str, Any]:
        if not self._cfg.plugin.enabled:
            return {"action": "continue"}
        state = self._get_live_state()
        if not state.is_silencing:
            return {"action": "continue"}
        evaluation = state.evaluation
        exempt, _ = await self._is_session_exempt(session_id)
        if exempt:
            return {"action": "continue"}
        self._total_blocked_models += 1
        self._record_event("model_blocked", hook_name, f"静默期拦截会话 {session_id or 'unknown'} 的模型请求")
        return {
            "action": "abort",
            "custom_result": {
                "reason": f"当前处于 AI 静默时段（{evaluation.rule_names}，预计 {evaluation.end_time_text} 恢复），已中止本次模型调用。"
            },
        }

    # ===== 聊天命令（非大模型，静默期始终可用） =====

    @Command(
        "quiet_status",
        description="查看当前 AI 时段静默/回退模型状态、今日节假日识别结果与豁免统计",
        pattern=r"^/(?:quiet(?:\s+status)?|静默状态)\s*$",
        aliases=["/静默状态"],
        permission="public",
    )
    async def cmd_status(self, stream_id: str = "", **kwargs: Any) -> Tuple[bool, str, bool]:
        del kwargs
        catalog = await self._refresh_model_catalog(force=False)
        state = self._resolve_effective_state(self._get_live_evaluation(), catalog=catalog)
        evaluation = state.evaluation
        blocked_plugins = [rec for rec in self._auditor.records.values() if rec.should_block]
        next_text = (
            evaluation.next_transition_at.strftime("%Y-%m-%d %H:%M")
            if evaluation.next_transition_at is not None
            else "无计划切换"
        )
        override_text = "无（自动调度）"
        if evaluation.manual_override is not None:
            mode_label_map = {
                "force_quiet": "强制静默",
                "force_fallback": "强制回退模型",
                "force_active": "强制恢复正常",
            }
            mode_label = mode_label_map.get(evaluation.manual_override.mode, evaluation.manual_override.mode)
            exp_label = (
                evaluation.manual_override.expires_at.strftime("%Y-%m-%d %H:%M")
                if evaluation.manual_override.expires_at is not None
                else "手动取消前"
            )
            override_text = f"{mode_label}（至 {exp_label}）"

        if state.is_fallback:
            status_line = f"🔄 模型回退中（已切至：{', '.join(state.fallback_models)}，不进行静默）"
        elif state.is_silencing:
            status_line = "🌙 AI 静默中（大模型已关闭）"
        else:
            status_line = "☀️ AI 正常运行"

        configured_fb = ", ".join(self._cfg.model_fallback.fallback_models) or "未设置（将使用静默规则）"
        lines = [
            "🛡️ AI 时段静默守卫状态",
            f"• 当前状态：{status_line}",
            f"• 当前时间：{evaluation.evaluated_at.strftime('%Y-%m-%d %H:%M')}",
            f"• 今日日期：{evaluation.today_info.format_summary()}",
            f"• 命中规则：{evaluation.rule_names}",
            f"• 回退模型配置：{configured_fb}（已识别 MaiBot 模型 {len(catalog.llm_models())} 个）",
            f"• 下次切换：{next_text}",
            f"• 手动覆盖：{override_text}",
            f"• 豁免配置：群聊 {len(self._cfg.exemptions.exempt_groups)} 个 / 用户 {len(self._cfg.exemptions.exempt_users)} 个",
            f"• 同插件审计：共 {len(self._auditor.records)} 个，静默期阻断 {len(blocked_plugins)} 个（拦截聊天 {self._total_blocked_chats} / 插件 {self._total_blocked_plugins}）",
            "• 查看指令帮助：/quiet help",
        ]
        reply = "\n".join(lines)
        if stream_id:
            await self.ctx.send.text(reply, stream_id, storage_message=False)
        return True, reply, True

    @Command(
        "quiet_holiday",
        description="查询今天或指定日期（YYYY-MM-DD）的节假日/调休识别结果及近期节假日",
        pattern=r"^/(?:quiet\s+holiday|节假日查询)(?:\s+(?P<target_date>\d{4}-\d{2}-\d{2}))?\s*$",
        aliases=["/节假日查询"],
        permission="public",
    )
    async def cmd_holiday(self, stream_id: str = "", **kwargs: Any) -> Tuple[bool, str, bool]:
        matched_groups = kwargs.get("matched_groups") if isinstance(kwargs.get("matched_groups"), dict) else {}
        raw_date = str(matched_groups.get("target_date") or "").strip()
        if not raw_date:
            raw_text = str(kwargs.get("text") or "").strip()
            match = re.search(r"(\d{4}-\d{2}-\d{2})", raw_text)
            if match is not None:
                raw_date = match.group(1)

        now = now_in_timezone(self._cfg.schedule.timezone)
        if raw_date:
            try:
                query_date = date.fromisoformat(raw_date)
            except ValueError:
                msg = f"日期无效：{raw_date}，请使用 YYYY-MM-DD 格式。"
                if stream_id:
                    await self.ctx.send.text(msg, stream_id, storage_message=False)
                return False, msg, True
        else:
            query_date = now.date()

        info = self._calendar.classify(query_date, self._cfg.holiday)
        upcoming = self._calendar.list_upcoming_special_days(query_date, self._cfg.holiday, horizon_days=60, limit=8)
        summary = self._calendar.status_summary(query_date.year)

        lines = [
            f"📅 节假日查询：{query_date.isoformat()}（周{'一二三四五六日'[query_date.isoweekday() - 1]}）",
            f"• 判定结果：{info.format_summary()}",
            f"• 工作日 / 休息日：{'工作日' if info.is_workday else '休息日'}（数据源：{info.source} / 年度表：{summary['active_source']}）",
        ]
        if upcoming:
            lines.append("• 未来 60 天节假日与调休：")
            for item in upcoming:
                lines.append(f"  - {item['date']}：{item['day_type_label']}（{item['holiday_name'] or '-'}）")
        reply = "\n".join(lines)
        if stream_id:
            await self.ctx.send.text(reply, stream_id, storage_message=False)
        return True, reply, True

    @Command(
        "quiet_plugins",
        description="查看大模型插件检查报告及静默期拦截清单",
        pattern=r"^/(?:quiet\s+plugins|模型插件检查)\s*$",
        aliases=["/模型插件检查"],
        permission="operator",
    )
    async def cmd_plugins(self, stream_id: str = "", **kwargs: Any) -> Tuple[bool, str, bool]:
        del kwargs
        await self._scan_plugins()
        records = sorted(self._auditor.records.values(), key=lambda item: (not item.should_block, item.plugin_id))
        blocked = [rec for rec in records if rec.should_block]
        allowed = [rec for rec in records if not rec.should_block]

        lines = [
            f"🔍 大模型插件检查报告（共 {len(records)} 个，静默期阻断 {len(blocked)} 个，放行 {len(allowed)} 个）",
            f"• 阻断策略：{self._cfg.plugin_guard.block_mode} | 最近扫描：{self._auditor.last_scan_at or '-'}",
        ]
        if blocked:
            lines.append("• 【静默期阻断的大模型插件】")
            for rec in blocked[:20]:
                reason = "；".join(rec.reasons[:2]) if rec.reasons else "强制阻断"
                lines.append(f"  - {rec.display_name} ({rec.plugin_id})：{reason}")
        else:
            lines.append("• 当前未检测到需要阻断的大模型插件。")

        if allowed:
            names = ", ".join(rec.plugin_id for rec in allowed[:15])
            suffix = f" 等 {len(allowed)} 个" if len(allowed) > 15 else ""
            lines.append(f"• 【静默期保留放行的非大模型插件】：{names}{suffix}")

        reply = "\n".join(lines)
        if stream_id:
            await self.ctx.send.text(reply, stream_id, storage_message=False)
        return True, reply, True

    @Command(
        "quiet_on",
        description="手动立即开启 AI 静默模式（可选分钟数与原因）",
        pattern=r"^/quiet\s+on(?:\s+(?P<minutes>\d+))?(?:\s+(?P<reason>.+))?\s*$",
        permission="operator",
    )
    async def cmd_force_quiet(self, stream_id: str = "", **kwargs: Any) -> Tuple[bool, str, bool]:
        matched_groups = kwargs.get("matched_groups") if isinstance(kwargs.get("matched_groups"), dict) else {}
        minutes_raw = matched_groups.get("minutes")
        duration = int(minutes_raw) if minutes_raw else 0
        reason = str(matched_groups.get("reason") or "管理员手动开启静默").strip()

        result = await self._set_manual_override("force_quiet", duration_minutes=duration, reason=reason, source="command")
        if stream_id:
            await self.ctx.send.text(result["message"], stream_id, storage_message=False)
        return True, result["message"], True

    @Command(
        "quiet_off",
        description="手动立即恢复 AI 聊天与大模型插件（可选临时恢复分钟数）",
        pattern=r"^/quiet\s+off(?:\s+(?P<minutes>\d+))?\s*$",
        permission="operator",
    )
    async def cmd_force_active(self, stream_id: str = "", **kwargs: Any) -> Tuple[bool, str, bool]:
        matched_groups = kwargs.get("matched_groups") if isinstance(kwargs.get("matched_groups"), dict) else {}
        minutes_raw = matched_groups.get("minutes")
        duration = int(minutes_raw) if minutes_raw else 0

        result = await self._set_manual_override("force_active", duration_minutes=duration, reason="管理员手动恢复 AI", source="command")
        if stream_id:
            await self.ctx.send.text(result["message"], stream_id, storage_message=False)
        return True, result["message"], True

    @Command(
        "quiet_auto",
        description="清除手动覆盖，恢复按时段与节假日规则自动调度",
        pattern=r"^/quiet\s+auto\s*$",
        permission="operator",
    )
    async def cmd_restore_auto(self, stream_id: str = "", **kwargs: Any) -> Tuple[bool, str, bool]:
        del kwargs
        result = await self._clear_manual_override(source="command")
        if stream_id:
            await self.ctx.send.text(result["message"], stream_id, storage_message=False)
        return True, result["message"], True

    @Command(
        "quiet_scan",
        description="立即重新扫描全部已安装/已加载插件的大模型依赖",
        pattern=r"^/quiet\s+scan\s*$",
        permission="operator",
    )
    async def cmd_scan(self, stream_id: str = "", **kwargs: Any) -> Tuple[bool, str, bool]:
        del kwargs
        await self._refresh_host_command_settings()
        records = await self._scan_plugins()
        await self._reconcile_schedule_state(trigger="manual_scan")
        blocked_count = sum(1 for rec in records.values() if rec.should_block)
        msg = f"✅ 插件大模型依赖重扫完成：共检查 {len(records)} 个插件，检出需阻断的大模型插件 {blocked_count} 个。"
        if stream_id:
            await self.ctx.send.text(msg, stream_id, storage_message=False)
        return True, msg, True

    @Command(
        "quiet_fallback",
        description="查看已注册模型或手动切换至指定回退模型（不静默）",
        pattern=r"^/(?:quiet\s+fallback|回退模型)(?:\s+(?P<arg>.+))?\s*$",
        aliases=["/回退模型"],
        permission="operator",
    )
    async def cmd_fallback(self, stream_id: str = "", **kwargs: Any) -> Tuple[bool, str, bool]:
        matched_groups = kwargs.get("matched_groups") if isinstance(kwargs.get("matched_groups"), dict) else {}
        arg_text = str(matched_groups.get("arg") or "").strip()
        catalog = await self._refresh_model_catalog(force=True)
        llm_models = catalog.llm_models()

        if not arg_text:
            active_summary = self._model_controller.get_active_summary()
            configured = ", ".join(self._cfg.model_fallback.fallback_models) or "未配置"
            model_lines = [f"  - {m.format_label()}" for m in llm_models[:20]]
            msg = "\n".join(
                [
                    "🧠 MaiBot 已注册对话模型与回退状态",
                    f"• 回退功能：{'开启' if self._cfg.model_fallback.enabled else '关闭'} | 已配回退模型：{configured}",
                    f"• 当前回退激活：{'是 -> ' + ', '.join(active_summary['applied_models']) if active_summary['active'] else '否'}",
                    f"• 可选 LLM 模型（共 {len(llm_models)} 个）：",
                    *(model_lines or ["  - （未检测到已注册模型）"]),
                    "• 临时切换命令：/quiet fallback <模型名[,模型名2]> 或 /quiet fallback off",
                ]
            )
            if stream_id:
                await self.ctx.send.text(msg, stream_id, storage_message=False)
            return True, msg, True

        if arg_text.lower() in {"off", "restore", "auto", "关闭"}:
            res = await self._clear_manual_override(source="command_fallback")
            if stream_id:
                await self.ctx.send.text(res["message"], stream_id, storage_message=False)
            return True, res["message"], True

        requested = [item.strip() for item in re.split(r"[\s,，]+", arg_text) if item.strip()]
        valid, invalid, _ = self._model_controller.resolve_effective_models(requested, catalog=catalog)
        if not valid:
            msg = f"❌ 未在 MaiBot 模型目录中找到已注册模型：{', '.join(invalid or requested)}。"
            if stream_id:
                await self.ctx.send.text(msg, stream_id, storage_message=False)
            return False, msg, True

        res = await self._set_manual_override(
            "force_fallback",
            duration_minutes=0,
            reason=f"手动切换回退模型（{', '.join(valid)}）",
            source="command",
            fallback_models=valid,
        )
        if stream_id:
            await self.ctx.send.text(res["message"], stream_id, storage_message=False)
        return True, res["message"], True

    @Command(
        "quiet_help",
        description="查看 AI 时段静默守卫命令帮助",
        pattern=r"^/quiet\s+help\s*$",
        permission="public",
    )
    async def cmd_help(self, stream_id: str = "", **kwargs: Any) -> Tuple[bool, str, bool]:
        del kwargs
        help_text = (
            "🛡️ AI 时段静默守卫指令帮助\n"
            "• /quiet 或 /quiet status（/静默状态）：查看当前运行/回退/静默状态与下次切换时间\n"
            "• /quiet holiday [YYYY-MM-DD]（/节假日查询）：查询指定日期节假日与调休识别\n"
            "• /quiet plugins（/模型插件检查）[管理员]：查看所有同插件与大模型依赖审计报告\n"
            "• /quiet fallback [模型名|off]（/回退模型）[管理员]：查看已注册模型或手动切换回退模型\n"
            "• /quiet scan [管理员]：立即重新扫描插件大模型依赖与模型目录\n"
            "• /quiet on [分钟数] [原因] [管理员]：立即强制进入 AI 静默\n"
            "• /quiet off [分钟数] [管理员]：立即强制恢复 AI 正常原模型运行\n"
            "• /quiet auto [管理员]：取消手动覆盖，恢复自动时段调度"
        )
        if stream_id:
            await self.ctx.send.text(help_text, stream_id, storage_message=False)
        return True, help_text, True

    # ===== 手动覆盖辅助 =====

    async def _set_manual_override(
        self,
        mode: str,
        *,
        duration_minutes: int,
        reason: str,
        source: str,
        fallback_models: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        if duration_minutes < 0 or duration_minutes > 1440:
            raise ValueError("duration_minutes 必须为 0 到 1440 之间的整数")

        now = now_in_timezone(self._cfg.schedule.timezone)
        expires_at = now + timedelta(minutes=duration_minutes) if duration_minutes > 0 else None
        normalized_mode: Literal["force_quiet", "force_active", "force_fallback"]
        if mode == "force_quiet":
            normalized_mode = "force_quiet"
        elif mode == "force_fallback":
            normalized_mode = "force_fallback"
        else:
            normalized_mode = "force_active"

        fb_tuple = tuple(str(m).strip() for m in (fallback_models or []) if str(m).strip())
        self._manual_override = ManualOverride(
            mode=normalized_mode,
            reason=reason.strip(),
            created_at=now,
            expires_at=expires_at,
            fallback_models=fb_tuple,
        )
        await asyncio.to_thread(self._save_persisted_state_sync)
        await self._reconcile_schedule_state(trigger=f"{source}:{normalized_mode}")
        state = self._current_state or self._get_live_state()
        exp_text = expires_at.strftime("%Y-%m-%d %H:%M") if expires_at else "手动执行 /quiet auto 前"
        if state.is_silencing and normalized_mode == "force_fallback":
            message = f"⚠️ 无可用已注册回退模型，已按静默规则进入 AI 静默模式（有效期至：{exp_text}）。"
        elif state.is_fallback:
            message = f"🔄 已切换至回退模型（{', '.join(state.fallback_models)}），期间不进行静默（有效期至：{exp_text}）。"
        elif normalized_mode == "force_quiet":
            message = f"🌙 已手动开启 AI 静默模式（有效期至：{exp_text}）。非大模型指令仍可正常使用。"
        else:
            message = f"☀️ 已手动恢复 AI 聊天与原模型能力（有效期至：{exp_text}）。"
        return {
            "success": True,
            "mode": state.mode,
            "is_quiet": state.is_silencing,
            "is_silencing": state.is_silencing,
            "is_fallback": state.is_fallback,
            "fallback_models": list(state.fallback_models),
            "message": message,
        }

    async def _clear_manual_override(self, *, source: str) -> Dict[str, Any]:
        self._manual_override = None
        await asyncio.to_thread(self._save_persisted_state_sync)
        await self._reconcile_schedule_state(trigger=f"{source}:auto")
        state = self._current_state or self._get_live_state()
        if state.is_fallback:
            state_text = f"模型回退中（{', '.join(state.fallback_models)}）"
        elif state.is_silencing:
            state_text = "AI 静默中"
        else:
            state_text = "AI 正常运行"
        return {
            "success": True,
            "mode": state.mode,
            "is_quiet": state.is_silencing,
            "is_silencing": state.is_silencing,
            "is_fallback": state.is_fallback,
            "fallback_models": list(state.fallback_models),
            "message": f"🔄 已恢复自动时段调度，当前状态：{state_text}（{state.mode_note}）。",
        }

    # ===== 配置持久化和列表收集辅助 =====

    async def _persist_config_patch(self, patch: Dict[str, Any], *, trigger: str) -> QuietHoursPluginConfig:
        """合并局部配置修改，校验后写入 ``config.toml`` 并立即热同步到内存。"""
        config_path = _resolve_self_config_path(_PLUGIN_DIR)
        base_data: Dict[str, Any] = {}
        if config_path.is_file():
            try:
                base_data = tomllib.loads(config_path.read_text(encoding="utf-8"))
            except Exception:
                base_data = {}
        if not base_data:
            base_data = self._cfg.model_dump(mode="python")

        merged = deepcopy(base_data)
        for section_key, section_val in patch.items():
            if isinstance(section_val, dict) and isinstance(merged.get(section_key), dict):
                merged[section_key] = {**merged[section_key], **section_val}
            else:
                merged[section_key] = deepcopy(section_val)
        merged.setdefault("plugin", {})
        if isinstance(merged["plugin"], dict):
            merged["plugin"].setdefault("enabled", self._cfg.plugin.enabled)
            merged["plugin"]["config_version"] = self._cfg.plugin.config_version

        validated = QuietHoursPluginConfig.model_validate(merged)
        dump_data = validated.model_dump(mode="python")
        await asyncio.to_thread(_write_plugin_config_toml, config_path, dump_data)

        self.set_plugin_config(dump_data)
        await self.on_config_update("self", dump_data, validated.plugin.config_version)
        self._record_event("config_saved", trigger, f"已通过控制台保存配置节: {', '.join(patch.keys())}")
        return validated

    async def _collect_selectable_targets(self) -> Dict[str, Any]:
        """聚合当前聊天流、历史会话、人物档案及已保存豁免名单，供前端多选列表使用。"""
        streams = await self._refresh_session_cache()
        groups_map: Dict[Tuple[str, str], Dict[str, Any]] = {}
        users_map: Dict[Tuple[str, str], Dict[str, Any]] = {}

        def _upsert_group(platform: str, group_id: str, name: str = "", source: str = "stream") -> None:
            p = str(platform or "").strip()
            gid = str(group_id or "").strip()
            if not gid:
                return
            key = (p.lower(), gid)
            prev = groups_map.get(key)
            cleaned_name = str(name or "").strip()
            if prev is None:
                groups_map[key] = {
                    "platform": p,
                    "type": "group",
                    "id": gid,
                    "name": cleaned_name or gid,
                    "source": source,
                }
            else:
                if not prev["platform"] and p:
                    prev["platform"] = p
                if cleaned_name and (prev["name"] == prev["id"] or not prev["name"]):
                    prev["name"] = cleaned_name

        def _upsert_user(platform: str, user_id: str, name: str = "", source: str = "stream") -> None:
            p = str(platform or "").strip()
            uid = str(user_id or "").strip()
            if not uid:
                return
            key = (p.lower(), uid)
            prev = users_map.get(key)
            cleaned_name = str(name or "").strip()
            if prev is None:
                users_map[key] = {
                    "platform": p,
                    "type": "user",
                    "id": uid,
                    "name": cleaned_name or uid,
                    "source": source,
                }
            else:
                if not prev["platform"] and p:
                    prev["platform"] = p
                if cleaned_name and (prev["name"] == prev["id"] or not prev["name"]):
                    prev["name"] = cleaned_name

        # 1. 活跃聊天流
        for s in streams:
            platform = str(s.get("platform") or "").strip()
            group_id = str(s.get("group_id") or "").strip()
            user_id = str(s.get("user_id") or "").strip()
            if s.get("is_group_session") or group_id:
                _upsert_group(platform, group_id, str(s.get("group_name") or ""), "stream")
            elif user_id:
                _upsert_user(platform, user_id, str(s.get("user_nickname") or s.get("user_cardname") or ""), "stream")

        # 2. 尝试补充群聊/私聊流及数据库档案（若宿主授权 database.query，可列出未建立私聊的历史群成员）
        for cap_name, args in (
            ("chat.get_group_streams", {"platform": "all_platforms"}),
            ("chat.get_private_streams", {"platform": "all_platforms"}),
            ("database.query", {"model_name": "ChatSession", "query_type": "get", "limit": 500}),
            ("database.query", {"model_name": "PersonInfo", "query_type": "get", "limit": 500}),
        ):
            try:
                res = await self.ctx.call_capability(cap_name, **args)
            except Exception:
                continue
            rows: List[Dict[str, Any]] = []
            if isinstance(res, list):
                rows = [item for item in res if isinstance(item, dict)]
            elif isinstance(res, dict):
                for key in ("streams", "data", "results", "items", "records"):
                    val = res.get(key)
                    if isinstance(val, list):
                        rows = [item for item in val if isinstance(item, dict)]
                        break
            for row in rows:
                p = str(row.get("platform") or "").strip()
                gid = str(row.get("group_id") or "").strip()
                uid = str(row.get("user_id") or "").strip()
                if gid:
                    _upsert_group(p, gid, str(row.get("group_name") or ""), "db" if "database" in cap_name else "stream")
                if uid and (cap_name != "chat.get_group_streams"):
                    name = str(
                        row.get("person_name")
                        or row.get("user_nickname")
                        or row.get("user_cardname")
                        or ""
                    )
                    _upsert_user(p, uid, name, "person_info" if args.get("model_name") == "PersonInfo" else "stream")

        # 3. 合并已保存的豁免配置（即使暂未在当前会话列表出现也保证可见与可取消勾选）
        for item in self._cfg.exemptions.exempt_groups:
            _upsert_group(item.platform, item.id, item.name, "configured")
        for item in self._cfg.exemptions.exempt_users:
            _upsert_user(item.platform, item.id, item.name, "configured")

        group_list = []
        for entry in groups_map.values():
            entry["exempt"] = is_group_exempt(
                ExemptionSectionConfig(exempt_groups=self._cfg.exemptions.exempt_groups, exempt_platforms=[]),
                entry["platform"],
                entry["id"],
            )
            group_list.append(entry)
        user_list = []
        for entry in users_map.values():
            entry["exempt"] = is_user_exempt(
                ExemptionSectionConfig(exempt_users=self._cfg.exemptions.exempt_users, exempt_platforms=[]),
                entry["platform"],
                entry["id"],
            )
            user_list.append(entry)

        group_list.sort(key=lambda item: (not item["exempt"], item["platform"].lower(), item["name"].lower(), item["id"]))
        user_list.sort(key=lambda item: (not item["exempt"], item["platform"].lower(), item["name"].lower(), item["id"]))
        platforms = sorted({item["platform"] for item in [*group_list, *user_list] if item["platform"]})
        return {"groups": group_list, "users": user_list, "platforms": platforms}

    # ===== WebUI HomeCard 与页面 API =====

    @HomeCard(
        "ai_quiet_hours_card",
        title="AI 时段静默守卫",
        description="支持按时段自动切换至 MaiBot 已注册回退模型（不静默），无可用回退模型时执行静默拦截，支持插件/群聊/用户列表勾选。",
        content=[
            {
                "type": "list",
                "items": [
                    "时段内优先切换至已注册回退模型继续服务；未配或无可用模型时启用静默规则",
                    "内置中国法定节假日与调休补班识别，静默期保留非大模型功能",
                    "WebUI 控制台支持可视化选择回退模型、同插件屏蔽规则、豁免群聊与用户",
                ],
            },
            {
                "type": "actions",
                "actions": [
                    {"label": "打开控制台与规则选择器", "url": "/extensions/a1mue4ay.ai-quiet-hours/dashboard"},
                    {"label": "打开插件表单设置", "url": "/plugin-config?plugin=a1mue4ay.ai-quiet-hours"},
                ],
            },
        ],
        link_url="/extensions/a1mue4ay.ai-quiet-hours/dashboard",
        link_label="进入静默控制台",
        width="medium",
        order=110,
    )
    async def home_card(self) -> None:
        return None

    @API("webui_status", description="获取 AI 时段静默守卫实时状态、节假日、模型回退与插件审计快照", version="1")
    async def api_webui_status(self) -> Dict[str, Any]:
        catalog = await self._refresh_model_catalog(force=False)
        state = self._resolve_effective_state(self._get_live_evaluation(), catalog=catalog)
        evaluation = state.evaluation
        matched_names = {item.rule.name for item in evaluation.matched_rules}
        audited = [
            rec.to_summary_dict()
            for rec in sorted(self._auditor.records.values(), key=lambda r: (not r.should_block, r.plugin_id))
        ]
        blocked_count = sum(1 for item in audited if item["should_block"])

        rules_view = [
            {
                "name": rule.name,
                "enabled": rule.enabled,
                "enabled_label": "启用" if rule.enabled else "停用",
                "time_range": f"{rule.start_time} - {rule.end_time}",
                "day_scope": rule.day_scope,
                "mode": rule.mode,
                "fallback_models": list(rule.fallback_models),
                "active_now": "命中" if rule.name in matched_names else "-",
            }
            for rule in self._cfg.schedule.rules
        ]

        holiday_summary = self._calendar.status_summary(evaluation.evaluated_at.year)
        upcoming = self._calendar.list_upcoming_special_days(
            evaluation.evaluated_at.date(),
            self._cfg.holiday,
            horizon_days=90,
            limit=20,
        )

        next_label = (
            evaluation.next_transition_at.strftime("%m-%d %H:%M")
            if evaluation.next_transition_at is not None
            else "无计划切换"
        )
        if state.is_fallback:
            state_label = f"模型回退中（{', '.join(state.fallback_models)}）"
        elif state.is_silencing:
            state_label = "AI 静默中"
        else:
            state_label = "AI 正常运行"

        detail_parts = [
            state.mode_note or evaluation.reason,
            f"豁免群聊 {len(self._cfg.exemptions.exempt_groups)} 个、豁免用户 {len(self._cfg.exemptions.exempt_users)} 个",
            f"累计拦截：聊天 {self._total_blocked_chats} / 大模型插件 {self._total_blocked_plugins} / 底层模型 {self._total_blocked_models}",
        ]

        return {
            "state_label": state_label,
            "mode": state.mode,
            "is_quiet": state.is_silencing,
            "is_silencing": state.is_silencing,
            "is_fallback": state.is_fallback,
            "in_schedule_window": evaluation.is_quiet,
            "fallback_models": list(state.fallback_models),
            "invalid_fallback_models": list(state.invalid_fallback_models),
            "mode_note": state.mode_note,
            "model_fallback_summary": self._model_controller.get_active_summary(),
            "registered_models_count": len(catalog.models),
            "today_summary": evaluation.today_info.format_summary(),
            "next_transition_label": next_label,
            "blocked_plugin_count": blocked_count,
            "status_detail": " | ".join(detail_parts),
            "last_scan_at": self._auditor.last_scan_at or "尚未扫描",
            "audited_plugins": audited,
            "rules": rules_view,
            "holiday_source_summary": (
                f"当前年度：{holiday_summary['year']}，数据源：{holiday_summary['active_source']}，"
                f"已缓存在线年份：{holiday_summary['synced_years'] or '无'}"
            ),
            "upcoming_holidays": upcoming,
            "recent_events": list(self._recent_events),
        }

    @API("webui_console_bootstrap", description="加载自定义 WebUI 控制台所需的全部实时状态、已注册模型、同插件、群聊与用户列表", version="1")
    async def api_webui_console_bootstrap(self) -> Dict[str, Any]:
        status = await self.api_webui_status()
        catalog = self._model_catalog_snapshot or await self._refresh_model_catalog(force=True)
        if not self._auditor.records:
            await self._scan_plugins()
        targets = await self._collect_selectable_targets()
        peer_plugins = [
            rec.to_detail_dict()
            for rec in sorted(
                self._auditor.records.values(),
                key=lambda item: (item.plugin_id == self._plugin_id, not item.uses_llm, item.is_built_in, item.plugin_id),
            )
            if rec.plugin_id != self._plugin_id
        ]
        return {
            "success": True,
            "status": status,
            "catalog": catalog.to_dict(),
            "plugins": peer_plugins,
            "targets": targets,
            "config": {
                "model_fallback": self._cfg.model_fallback.model_dump(mode="json"),
                "plugin_guard": self._cfg.plugin_guard.model_dump(mode="json"),
                "exemptions": self._cfg.exemptions.model_dump(mode="json"),
                "holiday": self._cfg.holiday.model_dump(mode="json"),
            },
        }

    @API("webui_force_quiet", description="从 WebUI 手动立即开启 AI 静默模式", version="1")
    async def api_webui_force_quiet(self, duration_minutes: int = 60, reason: str = "WebUI 手动开启") -> Dict[str, Any]:
        return await self._set_manual_override(
            "force_quiet",
            duration_minutes=int(duration_minutes),
            reason=str(reason or "WebUI 手动开启"),
            source="webui",
        )

    @API("webui_force_fallback", description="从 WebUI 手动切换至已配置/指定的回退模型", version="1")
    async def api_webui_force_fallback(
        self,
        duration_minutes: int = 60,
        fallback_models: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        return await self._set_manual_override(
            "force_fallback",
            duration_minutes=int(duration_minutes),
            reason="WebUI 手动切换回退模型",
            source="webui",
            fallback_models=list(fallback_models or []),
        )

    @API("webui_force_active", description="从 WebUI 手动立即恢复 AI 原模型正常运行", version="1")
    async def api_webui_force_active(self, duration_minutes: int = 60) -> Dict[str, Any]:
        return await self._set_manual_override(
            "force_active",
            duration_minutes=int(duration_minutes),
            reason="WebUI 手动恢复 AI",
            source="webui",
        )

    @API("webui_restore_auto", description="从 WebUI 取消手动覆盖，恢复自动时段调度", version="1")
    async def api_webui_restore_auto(self) -> Dict[str, Any]:
        return await self._clear_manual_override(source="webui")

    @API("webui_rescan_plugins", description="从 WebUI 立即重新扫描同插件与大模型插件依赖", version="1")
    async def api_webui_rescan_plugins(self) -> Dict[str, Any]:
        await self._refresh_host_command_settings()
        records = await self._scan_plugins()
        await self._reconcile_schedule_state(trigger="webui_scan")
        return {
            "success": True,
            "total_plugins": len(records),
            "blocked_plugins": sum(1 for rec in records.values() if rec.should_block),
        }

    @API("webui_rescan_models", description="从 WebUI 重新扫描 MaiBot 已注册模型与任务配置", version="1")
    async def api_webui_rescan_models(self) -> Dict[str, Any]:
        catalog = await self._refresh_model_catalog(force=True)
        await self._reconcile_schedule_state(trigger="webui_model_scan")
        return {
            "success": not bool(catalog.error),
            "total_models": len(catalog.models),
            "llm_models": len(catalog.llm_models()),
            "catalog": catalog.to_dict(),
        }

    @API("webui_save_model_fallback", description="从 WebUI 保存回退模型切换配置并立即生效", version="1")
    async def api_webui_save_model_fallback(
        self,
        enabled: bool = True,
        fallback_models: Optional[List[str]] = None,
        target_tasks: Optional[List[str]] = None,
        switch_mode: str = "replace",
        fallback_to_quiet_if_unavailable: bool = True,
    ) -> Dict[str, Any]:
        patch = {
            "model_fallback": {
                "enabled": bool(enabled),
                "fallback_models": list(fallback_models or []),
                "target_tasks": list(target_tasks or list(self._cfg.model_fallback.target_tasks)),
                "switch_mode": "prepend" if switch_mode == "prepend" else "replace",
                "fallback_to_quiet_if_unavailable": bool(fallback_to_quiet_if_unavailable),
            }
        }
        updated = await self._persist_config_patch(patch, trigger="webui_save_model_fallback")
        return {
            "success": True,
            "model_fallback": updated.model_fallback.model_dump(mode="json"),
            "status": await self.api_webui_status(),
        }

    @API("webui_save_plugin_guard", description="从 WebUI 保存同插件屏蔽/放行列表规则并立即生效", version="1")
    async def api_webui_save_plugin_guard(
        self,
        enabled: bool = True,
        block_mode: str = "smart",
        force_block_plugins: Optional[List[str]] = None,
        force_allow_plugins: Optional[List[str]] = None,
        force_allow_commands: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        normalized_mode = block_mode if block_mode in {"smart", "hook_only", "disable_components"} else "smart"
        patch = {
            "plugin_guard": {
                "enabled": bool(enabled),
                "block_mode": normalized_mode,
                "force_block_plugins": list(force_block_plugins or []),
                "force_allow_plugins": list(force_allow_plugins or []),
                "force_allow_commands": list(force_allow_commands or []),
            }
        }
        updated = await self._persist_config_patch(patch, trigger="webui_save_plugin_guard")
        return {
            "success": True,
            "plugin_guard": updated.plugin_guard.model_dump(mode="json"),
            "status": await self.api_webui_status(),
        }

    @API("webui_save_exemptions", description="从 WebUI 保存豁免群聊、豁免用户与豁免平台名单并立即生效", version="1")
    async def api_webui_save_exemptions(
        self,
        exempt_groups: Optional[List[Dict[str, Any]]] = None,
        exempt_users: Optional[List[Dict[str, Any]]] = None,
        user_exempt_in_group: bool = True,
        exempt_platforms: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        cleaned_groups = [
            GroupTarget.model_validate(item).model_dump(mode="json")
            for item in (exempt_groups or [])
        ]
        cleaned_users = [
            UserTarget.model_validate(item).model_dump(mode="json")
            for item in (exempt_users or [])
        ]
        patch = {
            "exemptions": {
                "exempt_groups": cleaned_groups,
                "exempt_users": cleaned_users,
                "user_exempt_in_group": bool(user_exempt_in_group),
                "exempt_platforms": (
                    list(exempt_platforms)
                    if exempt_platforms is not None
                    else list(self._cfg.exemptions.exempt_platforms)
                ),
            }
        }
        updated = await self._persist_config_patch(patch, trigger="webui_save_exemptions")
        return {
            "success": True,
            "exemptions": updated.exemptions.model_dump(mode="json"),
            "status": await self.api_webui_status(),
        }

    @API("webui_sync_holidays", description="从 WebUI 立即在线同步当年与次年节假日数据", version="1")
    async def api_webui_sync_holidays(self) -> Dict[str, Any]:
        report = await self._sync_online_holidays_safely()
        await self._reconcile_schedule_state(trigger="webui_holiday_sync")
        return {"success": bool(report["synced_years"]), **report}


def create_plugin() -> AIQuietHoursPlugin:
    """创建 AI 时段静默守卫插件实例。"""
    return AIQuietHoursPlugin()
