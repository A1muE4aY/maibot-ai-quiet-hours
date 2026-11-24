"""测试环境用轻量 maibot_sdk 兼容桩（仅在当前解释器未安装 maibot_sdk 时注入）。"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any, Callable, ClassVar, Dict, List, Optional

import sys
import types

from pydantic import BaseModel, Field


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
    return Path(__file__).resolve().parents[3]
