"""测试环境用轻量 maibot_sdk 兼容桩（仅在当前解释器未安装 maibot_sdk 时注入）。"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any, Callable, ClassVar, Dict, List, Optional

import importlib.util
import os
import sys
import types

from pydantic import BaseModel, Field


def ensure_standalone_package_alias() -> Path:
    """在独立项目目录（如 ``ai-quiet-hours``）中运行测试时，确保 ``plugins.ai_quiet_hours`` 指向当前项目目录。"""
    project_root = Path(__file__).resolve().parents[1]
    if "plugins" not in sys.modules:
        plugins_pkg = types.ModuleType("plugins")
        plugins_pkg.__path__ = [str(project_root.parent)]  # type: ignore[attr-defined]
        sys.modules["plugins"] = plugins_pkg
    if "plugins.ai_quiet_hours" not in sys.modules:
        init_file = project_root / "__init__.py"
        spec = importlib.util.spec_from_file_location(
            "plugins.ai_quiet_hours",
            init_file,
            submodule_search_locations=[str(project_root)],
        )
        if spec is not None and spec.loader is not None:
            mod = importlib.util.module_from_spec(spec)
            sys.modules["plugins.ai_quiet_hours"] = mod
            spec.loader.exec_module(mod)
    return project_root


def ensure_maibot_sdk_available() -> None:
    """若环境未安装 maibot_sdk，注册与 v2.23+ 一致的最小测试桩。"""
    if "maibot_sdk" in sys.modules:
        return
    try:
        import maibot_sdk  # noqa: F401
        return
    except ImportError:
        pass

    sdk_mod = types.ModuleType("maibot_sdk")
    types_mod = types.ModuleType("maibot_sdk.types")

    class HookMode(str, Enum):
        BLOCKING = "blocking"
        OBSERVE = "observe"

    class HookOrder(str, Enum):
        EARLY = "early"
        NORMAL = "normal"
        LATE = "late"

    class ErrorPolicy(str, Enum):
        ABORT = "abort"
        SKIP = "skip"
        LOG = "log"

    class PluginConfigBase(BaseModel):
        pass

    def _attach(comp_type: str, name: str, metadata: Dict[str, Any]) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            setattr(
                func,
                "_maibot_component_info",
                {
                    "name": name or func.__name__,
                    "type": comp_type,
                    "metadata": {**metadata, "handler_name": func.__name__},
                },
            )
            return func

        return decorator

    def HookHandler(
        hook: str,
        *,
        name: str = "",
        description: str = "",
        mode: HookMode = HookMode.BLOCKING,
        order: HookOrder = HookOrder.NORMAL,
        timeout_ms: int = 0,
        error_policy: ErrorPolicy = ErrorPolicy.SKIP,
        **metadata: Any,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return _attach(
            "HOOK_HANDLER",
            name,
            {
                "hook": hook,
                "description": description,
                "mode": getattr(mode, "value", mode),
                "order": getattr(order, "value", order),
                "timeout_ms": timeout_ms,
                "error_policy": getattr(error_policy, "value", error_policy),
                **metadata,
            },
        )

    def Command(
        name: str,
        description: str = "",
        pattern: str = "",
        aliases: Optional[List[str]] = None,
        **metadata: Any,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return _attach(
            "COMMAND",
            name,
            {
                "description": description,
                "command_pattern": pattern,
                "aliases": aliases or [],
                **metadata,
            },
        )

    def API(
        name: str,
        description: str = "",
        version: str = "1",
        public: bool = False,
        **metadata: Any,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return _attach(
            "API",
            name,
            {
                "description": description,
                "version": version,
                "public": public,
                **metadata,
            },
        )

    def HomeCard(
        name: str,
        *,
        title: str = "",
        description: str = "",
        content: Optional[List[Dict[str, Any]]] = None,
        link_url: str = "",
        link_label: str = "",
        width: str = "medium",
        order: int = 1000,
        **metadata: Any,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return _attach(
            "HOME_CARD",
            name,
            {
                "title": title,
                "description": description,
                "content": content or [],
                "link_url": link_url,
                "link_label": link_label,
                "width": width,
                "order": order,
                **metadata,
            },
        )

    class MaiBotPlugin:
        config_model: ClassVar[Optional[type[PluginConfigBase]]] = None

        def __init__(self) -> None:
            self.ctx: Any = None
            self._config: Any = self.config_model() if self.config_model is not None else None

        @property
        def config(self) -> Any:
            return self._config

        def _set_context(self, context: Any) -> None:
            self.ctx = context

        def set_plugin_config(self, config_data: Dict[str, Any]) -> None:
            if self.config_model is not None:
                self._config = self.config_model.model_validate(config_data)

        def get_components(self) -> List[Dict[str, Any]]:
            components: List[Dict[str, Any]] = []
            for attr_name in dir(self):
                attr = getattr(type(self), attr_name, None)
                info = getattr(attr, "_maibot_component_info", None)
                if isinstance(info, dict):
                    components.append(info)
            return components

        async def on_load(self) -> None:
            pass

        async def on_unload(self) -> None:
            pass

        async def on_config_update(self, scope: str, config_data: Dict[str, object], version: str) -> None:
            pass

        def get_webui_config_schema(
            self,
            *,
            plugin_id: str = "",
            plugin_name: str = "",
            plugin_version: str = "",
            plugin_description: str = "",
            plugin_author: str = "",
        ) -> Dict[str, Any]:
            """构建与真实 maibot_sdk / dashboard plugin-config.tsx 兼容的测试配置 Schema。"""
            del plugin_id, plugin_name, plugin_version, plugin_description, plugin_author
            if self.config_model is None:
                return {}
            sections: Dict[str, Any] = {}
            for section_name, field_info in self.config_model.model_fields.items():
                annotation = field_info.annotation
                if isinstance(annotation, type) and issubclass(annotation, PluginConfigBase):
                    fields_schema: Dict[str, Any] = {}
                    for sub_name, sub_field in annotation.model_fields.items():
                        extra = dict(sub_field.json_schema_extra or {}) if isinstance(sub_field.json_schema_extra, dict) else {}
                        sub_ann = sub_field.annotation
                        field_type = "string"
                        if sub_ann is bool:
                            field_type = "boolean"
                        elif sub_ann in (int, float):
                            field_type = "number"
                        elif getattr(sub_ann, "__origin__", None) in (list, List):
                            field_type = "array"
                        ui_type = extra.get("x-widget") or (
                            "switch" if field_type == "boolean" else ("list" if field_type == "array" else "text")
                        )
                        fields_schema[sub_name] = {
                            "name": sub_name,
                            "type": field_type,
                            "label": extra.get("label", sub_name),
                            "description": sub_field.description or "",
                            "hint": extra.get("hint", ""),
                            "hidden": bool(extra.get("hidden", False)),
                            "ui_type": ui_type,
                            "choices": extra.get("choices"),
                        }
                    sections[section_name] = {
                        "name": section_name,
                        "title": getattr(annotation, "__ui_label__", section_name),
                        "fields": fields_schema,
                    }
            return {"schema_version": 2, "sections": sections}

    types_mod.HookMode = HookMode
    types_mod.HookOrder = HookOrder
    types_mod.ErrorPolicy = ErrorPolicy

    sdk_mod.API = API
    sdk_mod.Command = Command
    sdk_mod.Field = Field
    sdk_mod.HomeCard = HomeCard
    sdk_mod.HookHandler = HookHandler
    sdk_mod.MaiBotPlugin = MaiBotPlugin
    sdk_mod.PluginConfigBase = PluginConfigBase
    sdk_mod.types = types_mod

    sys.modules["maibot_sdk"] = sdk_mod
    sys.modules["maibot_sdk.types"] = types_mod


def ensure_host_test_deps_available() -> None:
    """当测试解释器缺少宿主第三方日志依赖（如 structlog）时，提供轻量 logger 兼容桩。

    同时避免仅用于校验 Manifest/WebUI/ComponentRegistry 的单元测试在工作区创建 logs 目录。
    """
    if "src.common.logger" in sys.modules:
        return
    try:
        import structlog  # noqa: F401
        return
    except ImportError:
        pass

    import logging

    logger_mod = types.ModuleType("src.common.logger")
    logger_mod.get_logger = lambda name="maibot": logging.getLogger(str(name))
    sys.modules["src.common.logger"] = logger_mod


def get_maibot_root() -> Path:
    env_root = str(os.environ.get("MAIBOT_ROOT", "") or "").strip()
    if env_root:
        candidate = Path(env_root).expanduser().resolve()
        if (candidate / "src" / "plugin_runtime").is_dir():
            return candidate
    here = Path(__file__).resolve()
    for base in here.parents:
        if (base / "src" / "plugin_runtime").is_dir():
            return base
        source_dir = base / "source"
        if source_dir.is_dir():
            for child in sorted(source_dir.iterdir(), reverse=True):
                candidate = child / "MaiBot-main"
                if (candidate / "src" / "plugin_runtime").is_dir():
                    return candidate
    return here.parents[3]


ensure_standalone_package_alias()
