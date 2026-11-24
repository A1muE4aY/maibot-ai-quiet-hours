"""大模型插件检测与命令匹配模块。

通过三层机制识别依赖大模型的插件：
1. 读取宿主运行时组件注册表（``component.get_all_plugins``），汇总插件与组件信息；
2. 扫描插件目录 ``_manifest.json`` 中的 ``capabilities`` / ``llm_providers`` 声明；
3. 对插件 ``*.py`` 源码执行 AST 静态分析，检测直接调用 MaiBot LLM/Maisaka 能力或外部模型 SDK。

同时提供与 MaiBot 主程序 ``bot.py`` 对齐的命令识别器，确保静默期精准放行非大模型命令。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import ast
import json
import os
import re
import tomllib

from .settings import PluginGuardSectionConfig

CORE_LLM_CAPABILITIES: frozenset[str] = frozenset(
    {
        "llm.generate",
        "llm.generate_with_tools",
        "llm.embed",
        "llm.transcribe_audio",
        "maisaka.reply.generate",
        "maisaka.proactive.trigger",
        "emoji.register",
    }
)

ADVISORY_LLM_CAPABILITIES: frozenset[str] = frozenset({"llm.get_available_models"})

EXTERNAL_LLM_MODULES: frozenset[str] = frozenset(
    {
        "openai",
        "anthropic",
        "google.genai",
        "google.generativeai",
        "litellm",
        "dashscope",
        "zhipuai",
        "ollama",
        "deepseek",
        "cohere",
        "mistralai",
        "groq",
        "volcenginesdkarkruntime",
        "langchain",
        "langchain_openai",
        "langchain_anthropic",
        "langchain_google_genai",
        "llama_index",
    }
)

HOST_LLM_MODULES: frozenset[str] = frozenset(
    {
        "src.services.llm_service",
        "src.services.embedding_service",
        "src.llm_models",
        "src.plugin_system.apis.llm_api",
    }
)

LLM_CALL_NAMES: frozenset[str] = frozenset(
    {
        "generate_with_model",
        "generate_with_model_with_tools",
        "LLMServiceClient",
        "LLMOrchestrator",
        "EmbeddingServiceClient",
        "LLMRequest",
    }
)

HTTP_CLIENT_MODULES: frozenset[str] = frozenset({"httpx", "aiohttp", "requests", "urllib.request"})

LLM_ENDPOINT_MARKERS: Tuple[str, ...] = (
    "/chat/completions",
    "/v1/responses",
    "/v1/messages",
    "/v1/embeddings",
    "/api/generate",
    "/api/chat",
)

# 永不被禁用或自动阻断的系统/自身标识
PROTECTED_PLUGIN_IDS: frozenset[str] = frozenset({"core"})
PROTECTED_COMPONENT_TYPES: frozenset[str] = frozenset({"MESSAGE_GATEWAY", "HOME_CARD", "API"})

_SKIP_SCAN_DIR_NAMES: frozenset[str] = frozenset(
    {"__pycache__", ".git", ".venv", "venv", "node_modules", "tests", "pytests", "data", "assets", "web", "webui"}
)
_MAX_SCAN_FILE_BYTES = 512 * 1024
_PM_PATTERN = re.compile(r"^/pm(?:\s+.+)?\s*$")


@dataclass(frozen=True)
class AuditedComponent:
    """插件注册的单个组件信息。"""

    name: str
    full_name: str
    component_type: str
    enabled: bool
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PluginAuditRecord:
    """单个插件的大模型依赖审计结果。"""

    plugin_id: str
    display_name: str
    version: str
    plugin_dir: str = ""
    is_adapter: bool = False
    uses_llm: bool = False
    decision: str = "allow"  # "block" | "allow" | "self" | "force_block" | "force_allow"
    reasons: Tuple[str, ...] = ()
    capabilities: Tuple[str, ...] = ()
    components: Tuple[AuditedComponent, ...] = ()

    @property
    def should_block(self) -> bool:
        return self.decision in {"block", "force_block"}

    def to_summary_dict(self) -> Dict[str, Any]:
        command_names = [comp.name for comp in self.components if comp.component_type == "COMMAND"]
        return {
            "plugin_id": self.plugin_id,
            "display_name": self.display_name or self.plugin_id,
            "version": self.version,
            "uses_llm": self.uses_llm,
            "should_block": self.should_block,
            "decision": self.decision,
            "is_adapter": self.is_adapter,
            "command_count": len(command_names),
            "commands": ", ".join(command_names) if command_names else "-",
            "reasons": "；".join(self.reasons) if self.reasons else "未发现大模型调用",
        }


@dataclass(frozen=True)
class CompiledCommandRule:
    """已编译的插件命令匹配规则。"""

    plugin_id: str
    command_name: str
    full_name: str
    pattern: Optional[re.Pattern[str]]
    aliases: Tuple[str, ...]


def discover_plugin_roots(current_plugin_dir: Path) -> List[Path]:
    """发现当前运行环境下所有可能存放插件的根目录。"""
    roots: List[Path] = []
    seen: Set[Path] = set()

    def _add(candidate: Path) -> None:
        try:
            resolved = candidate.resolve()
        except OSError:
            return
        if resolved.is_dir() and resolved not in seen:
            seen.add(resolved)
            roots.append(resolved)

    env_dirs = os.environ.get("MAIBOT_PLUGIN_DIRS", "")
    for raw in env_dirs.split(os.pathsep):
        if raw.strip():
            _add(Path(raw.strip()))

    # 当前插件所在的第三方 plugins/ 目录
    plugins_parent = current_plugin_dir.resolve().parent
    _add(plugins_parent)

    # 推断 MaiBot 实例根目录下的 plugins 与 src/plugins/built_in
    repo_root = plugins_parent.parent
    _add(repo_root / "plugins")
    _add(repo_root / "src" / "plugins" / "built_in")
    _add(Path.cwd() / "plugins")
    _add(Path.cwd() / "src" / "plugins" / "built_in")

    return roots


def scan_plugin_manifests_sync(plugin_roots: Sequence[Path]) -> Dict[str, Tuple[Path, Dict[str, Any]]]:
    """同步扫描所有插件根目录下的 ``_manifest.json``，返回 ``plugin_id -> (plugin_dir, manifest)``。"""
    manifests: Dict[str, Tuple[Path, Dict[str, Any]]] = {}
    for root in plugin_roots:
        if not root.is_dir():
            continue
        try:
            entries = sorted(root.iterdir())
        except OSError:
            continue
        for entry in entries:
            if not entry.is_dir() or entry.name.startswith(".") or entry.name.casefold() in {"data", "__pycache__"}:
                continue
            manifest_path = entry / "_manifest.json"
            if not manifest_path.is_file():
                continue
            try:
                data = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            plugin_id = str(data.get("id") or "").strip()
            if plugin_id and plugin_id not in manifests:
                manifests[plugin_id] = (entry, data)
    return manifests


def _resolve_dotted_name(node: ast.AST) -> str:
    parts: List[str] = []
    current: Optional[ast.AST] = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    parts.reverse()
    return ".".join(parts)


def scan_plugin_source_for_llm_sync(
    plugin_dir: Path,
    *,
    llm_capabilities: frozenset[str] = CORE_LLM_CAPABILITIES,
) -> List[str]:
    """使用 AST 静态扫描插件目录中的 Python 源码，识别直接或间接的大模型调用。"""
    if not plugin_dir.is_dir():
        return []

    findings: List[str] = []
    seen_findings: Set[str] = set()

    def _record(message: str) -> None:
        if message not in seen_findings:
            seen_findings.add(message)
            findings.append(message)

    for py_path in sorted(plugin_dir.rglob("*.py")):
        try:
            relative = py_path.relative_to(plugin_dir)
        except ValueError:
            continue
        if any(part in _SKIP_SCAN_DIR_NAMES or part.startswith(".") for part in relative.parts[:-1]):
            continue
        try:
            if py_path.stat().st_size > _MAX_SCAN_FILE_BYTES:
                continue
            source = py_path.read_text(encoding="utf-8", errors="ignore")
            tree = ast.parse(source, filename=str(relative))
        except (OSError, SyntaxError):
            continue

        rel_str = relative.as_posix()
        uses_http_client = False
        string_endpoints: List[Tuple[int, str]] = []

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    mod = alias.name
                    top = mod.split(".", 1)[0]
                    if mod in EXTERNAL_LLM_MODULES or top in EXTERNAL_LLM_MODULES:
                        _record(f"源码导入大模型 SDK: {rel_str}:{node.lineno} ({mod})")
                    elif mod in HOST_LLM_MODULES or any(mod.startswith(f"{h}.") for h in HOST_LLM_MODULES):
                        _record(f"源码导入宿主模型模块: {rel_str}:{node.lineno} ({mod})")
                    if mod in HTTP_CLIENT_MODULES or top in HTTP_CLIENT_MODULES:
                        uses_http_client = True

            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                top = mod.split(".", 1)[0] if mod else ""
                if mod in EXTERNAL_LLM_MODULES or top in EXTERNAL_LLM_MODULES:
                    _record(f"源码导入大模型 SDK: {rel_str}:{node.lineno} ({mod})")
                elif mod in HOST_LLM_MODULES or any(mod.startswith(f"{h}.") for h in HOST_LLM_MODULES):
                    _record(f"源码导入宿主模型模块: {rel_str}:{node.lineno} ({mod})")
                if mod in HTTP_CLIENT_MODULES or top in HTTP_CLIENT_MODULES:
                    uses_http_client = True
                for alias in node.names:
                    if alias.name in LLM_CALL_NAMES:
                        _record(f"源码导入大模型接口: {rel_str}:{node.lineno} ({alias.name})")

            elif isinstance(node, ast.Call):
                dotted = _resolve_dotted_name(node.func)
                tail = dotted.rsplit(".", 1)[-1] if dotted else ""
                if tail in LLM_CALL_NAMES:
                    _record(f"源码调用大模型接口: {rel_str}:{node.lineno} ({dotted})")

                # SDK 代理调用：ctx.llm.generate / ctx.llm.embed / ctx.maisaka.reply.generate 等
                if ".llm." in f".{dotted}":
                    if any(
                        dotted.endswith(suffix)
                        for suffix in (".llm.generate", ".llm.generate_with_tools", ".llm.embed", ".llm.transcribe_audio")
                    ):
                        _record(f"源码调用 SDK 模型能力: {rel_str}:{node.lineno} ({dotted})")
                if any(
                    dotted.endswith(suffix)
                    for suffix in (
                        ".maisaka.reply.generate",
                        ".maisaka.proactive.trigger",
                        ".emoji.register",
                    )
                ):
                    _record(f"源码调用宿主 AI 能力: {rel_str}:{node.lineno} ({dotted})")

                # 通用 call_capability("llm.generate", ...)
                if tail == "call_capability" and node.args:
                    first_arg = node.args[0]
                    if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
                        cap_name = first_arg.value.strip()
                        if cap_name in llm_capabilities:
                            _record(f"源码显式调用能力: {rel_str}:{node.lineno} ({cap_name})")

            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                text = node.value
                for marker in LLM_ENDPOINT_MARKERS:
                    if marker in text:
                        string_endpoints.append((getattr(node, "lineno", 0), marker))
                        break

        if uses_http_client:
            for lineno, marker in string_endpoints:
                _record(f"源码疑似直连模型 HTTP 接口: {rel_str}:{lineno} ({marker})")

    return findings


def read_pm_aliases_sync(instance_root: Path) -> Dict[str, str]:
    """读取宿主 ``config/commands/pm.toml`` 中的插件管理命令快捷别名。"""
    pm_path = instance_root / "config" / "commands" / "pm.toml"
    mapping: Dict[str, str] = {"/插件管理": "/pm"}
    if not pm_path.is_file():
        return mapping
    try:
        with pm_path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return mapping

    aliases_cfg = data.get("aliases")
    if not isinstance(aliases_cfg, dict):
        return mapping

    prefixes = aliases_cfg.get("management_prefixes")
    if isinstance(prefixes, list):
        for prefix in prefixes:
            normalized = str(prefix or "").strip()
            if normalized:
                mapping[normalized] = "/pm"

    shortcuts = aliases_cfg.get("shortcuts")
    if isinstance(shortcuts, dict):
        for alias, target in shortcuts.items():
            normalized_alias = str(alias or "").strip()
            normalized_target = str(target or "").strip()
            if normalized_alias and normalized_target.startswith("/pm"):
                mapping[normalized_alias] = normalized_target

    return mapping


class LlmPluginAuditor:
    """聚合运行时插件、Manifest 能力声明与源码 AST 扫描的审计器。"""

    def __init__(self, self_plugin_id: str, current_plugin_dir: Path) -> None:
        self._self_plugin_id = self_plugin_id
        self._current_plugin_dir = current_plugin_dir
        self._records: Dict[str, PluginAuditRecord] = {}
        self._command_rules: List[CompiledCommandRule] = []
        self._pm_aliases: Dict[str, str] = {"/插件管理": "/pm"}
        self._last_scan_at: str = ""

    @property
    def records(self) -> Dict[str, PluginAuditRecord]:
        return dict(self._records)

    @property
    def last_scan_at(self) -> str:
        return self._last_scan_at

    def audit_sync(
        self,
        runtime_plugins: Dict[str, Any],
        guard_config: PluginGuardSectionConfig,
        *,
        plugin_roots: Optional[Sequence[Path]] = None,
        scan_timestamp: str = "",
    ) -> Dict[str, PluginAuditRecord]:
        """执行完整审计并更新内存索引（在工作线程中运行，不阻塞事件循环）。"""
        roots = list(plugin_roots) if plugin_roots is not None else discover_plugin_roots(self._current_plugin_dir)
        manifests = scan_plugin_manifests_sync(roots)

        # 读取 pm 别名以便命令放行匹配
        instance_root = self._current_plugin_dir.resolve().parent.parent
        self._pm_aliases = read_pm_aliases_sync(instance_root)

        llm_caps = set(CORE_LLM_CAPABILITIES)
        if guard_config.treat_model_list_capability_as_llm:
            llm_caps.update(ADVISORY_LLM_CAPABILITIES)
        llm_caps_frozen = frozenset(llm_caps)

        force_allow = {item.strip() for item in guard_config.force_allow_plugins if item.strip()}
        force_block = {item.strip() for item in guard_config.force_block_plugins if item.strip()}

        all_plugin_ids = sorted(set(runtime_plugins.keys()) | set(manifests.keys()))
        records: Dict[str, PluginAuditRecord] = {}
        compiled_commands: List[CompiledCommandRule] = []

        for plugin_id in all_plugin_ids:
            runtime_info = runtime_plugins.get(plugin_id)
            manifest_entry = manifests.get(plugin_id)
            plugin_dir = manifest_entry[0] if manifest_entry is not None else None
            manifest_data = manifest_entry[1] if manifest_entry is not None else {}

            components: List[AuditedComponent] = []
            if isinstance(runtime_info, dict):
                raw_components = runtime_info.get("components", [])
                if isinstance(raw_components, list):
                    for item in raw_components:
                        if not isinstance(item, dict):
                            continue
                        comp = AuditedComponent(
                            name=str(item.get("name") or ""),
                            full_name=str(item.get("full_name") or f"{plugin_id}.{item.get('name', '')}"),
                            component_type=str(item.get("type") or "").upper(),
                            enabled=bool(item.get("enabled", True)),
                            metadata=dict(item.get("metadata") or {}) if isinstance(item.get("metadata"), dict) else {},
                        )
                        components.append(comp)
                        if comp.component_type == "COMMAND" and comp.enabled:
                            pattern_text = str(comp.metadata.get("command_pattern") or "").strip()
                            compiled_pattern: Optional[re.Pattern[str]] = None
                            if pattern_text:
                                try:
                                    compiled_pattern = re.compile(pattern_text)
                                except re.error:
                                    compiled_pattern = None
                            raw_aliases = comp.metadata.get("aliases", [])
                            aliases = (
                                tuple(str(a).strip() for a in raw_aliases if str(a).strip())
                                if isinstance(raw_aliases, list)
                                else ()
                            )
                            compiled_commands.append(
                                CompiledCommandRule(
                                    plugin_id=plugin_id,
                                    command_name=comp.name,
                                    full_name=comp.full_name,
                                    pattern=compiled_pattern,
                                    aliases=aliases,
                                )
                            )

            display_name = ""
            if isinstance(runtime_info, dict):
                display_name = str(runtime_info.get("display_name") or "").strip()
            if not display_name:
                display_name = str(manifest_data.get("name") or plugin_id).strip()

            version = ""
            if isinstance(runtime_info, dict):
                version = str(runtime_info.get("version") or "").strip()
            if not version:
                version = str(manifest_data.get("version") or "").strip()

            raw_caps = manifest_data.get("capabilities", [])
            declared_caps = tuple(
                str(cap).strip() for cap in raw_caps if isinstance(raw_caps, list) and str(cap).strip()
            )
            plugin_type = str(manifest_data.get("plugin_type", manifest_data.get("type", "extension")) or "").lower()
            is_adapter = plugin_type == "adapter" or any(
                comp.component_type == "MESSAGE_GATEWAY" for comp in components
            )

            reasons: List[str] = []
            for cap in declared_caps:
                if cap in llm_caps_frozen:
                    reasons.append(f"manifest 声明能力: {cap}")

            if isinstance(manifest_data.get("llm_providers"), list) and manifest_data["llm_providers"]:
                reasons.append("manifest 声明 LLM Provider")

            if guard_config.enable_ast_code_scan and plugin_dir is not None and plugin_id != self._self_plugin_id:
                reasons.extend(scan_plugin_source_for_llm_sync(plugin_dir, llm_capabilities=llm_caps_frozen))

            uses_llm = bool(reasons)

            if plugin_id == self._self_plugin_id or plugin_id in PROTECTED_PLUGIN_IDS:
                decision = "self"
            elif plugin_id in force_allow:
                decision = "force_allow"
                reasons.append("位于强制放行名单")
            elif plugin_id in force_block:
                decision = "force_block"
                uses_llm = True
                reasons.append("位于强制阻断名单")
            elif uses_llm:
                decision = "block"
            else:
                decision = "allow"

            records[plugin_id] = PluginAuditRecord(
                plugin_id=plugin_id,
                display_name=display_name,
                version=version,
                plugin_dir=str(plugin_dir) if plugin_dir else "",
                is_adapter=is_adapter,
                uses_llm=uses_llm,
                decision=decision,
                reasons=tuple(reasons),
                capabilities=declared_caps,
                components=tuple(components),
            )

        self._records = records
        self._command_rules = compiled_commands
        if scan_timestamp:
            self._last_scan_at = scan_timestamp
        return records

    def should_block_command(
        self,
        plugin_id: str,
        command_name: str,
        guard_config: PluginGuardSectionConfig,
    ) -> Tuple[bool, Optional[PluginAuditRecord]]:
        """判断指定插件的命令在静默期是否应当阻断。"""
        if not guard_config.enabled:
            return False, None
        normalized_plugin = plugin_id.strip()
        normalized_command = command_name.strip()
        if not normalized_plugin or normalized_plugin == self._self_plugin_id or normalized_plugin in PROTECTED_PLUGIN_IDS:
            return False, None

        full_command_id = f"{normalized_plugin}.{normalized_command}"
        allowed_commands = {item.strip() for item in guard_config.force_allow_commands if item.strip()}
        if normalized_command in allowed_commands or full_command_id in allowed_commands:
            return False, self._records.get(normalized_plugin)

        record = self._records.get(normalized_plugin)
        if record is not None:
            return record.should_block, record

        # 未扫描到的未知插件按手动强制阻断名单判断
        force_block = {item.strip() for item in guard_config.force_block_plugins if item.strip()}
        if normalized_plugin in force_block:
            synthetic = PluginAuditRecord(
                plugin_id=normalized_plugin,
                display_name=normalized_plugin,
                version="",
                uses_llm=True,
                decision="force_block",
                reasons=("位于强制阻断名单",),
            )
            return True, synthetic
        return False, None

    def get_disableable_components(self, *, include_commands: bool = False) -> List[Tuple[str, str, str]]:
        """收集静默期可在组件注册表临时禁用的大模型插件组件 ``(plugin_id, full_name, component_type)``。

        永不禁用 ``MESSAGE_GATEWAY``、``HOME_CARD``、``API`` 及自身插件组件。
        """
        target_types = {"EVENT_HANDLER", "HOOK_HANDLER", "TOOL", "REPLY_EXTENSION"}
        if include_commands:
            target_types.add("COMMAND")

        targets: List[Tuple[str, str, str]] = []
        for record in self._records.values():
            if not record.should_block:
                continue
            for comp in record.components:
                if not comp.enabled:
                    continue
                if comp.component_type in PROTECTED_COMPONENT_TYPES:
                    continue
                if comp.component_type in target_types:
                    targets.append((record.plugin_id, comp.full_name, comp.component_type))
        return targets

    def is_command_message(
        self,
        message: Dict[str, Any],
        *,
        disabled_commands: Sequence[str] = (),
        command_require_self_mention: bool = False,
    ) -> bool:
        """与 MaiBot ``bot.py`` 对齐，判断入站消息是否命中内置或已注册插件命令。"""
        if command_require_self_mention and not self._is_command_target_allowed(message):
            return False

        command_text = self.extract_command_text(message)
        if not command_text:
            return False

        disabled_set = set(disabled_commands)
        parts = command_text.split()
        if parts[:1] in [["/offline"], ["/online"]] or parts == ["/list", "chat"]:
            return True

        if command_text == "/clear" or command_text.startswith("/clear "):
            if "core.clear" not in disabled_set:
                return True

        if "core.pm" not in disabled_set and self._matches_pm_command(command_text):
            return True

        for rule in self._command_rules:
            if rule.full_name in disabled_set:
                continue
            if rule.pattern is not None and rule.pattern.search(command_text):
                return True
            for alias in rule.aliases:
                if command_text == alias or command_text.startswith(f"{alias} "):
                    return True

        return False

    def _matches_pm_command(self, text: str) -> bool:
        if not text.startswith("/"):
            return False
        if _PM_PATTERN.fullmatch(text):
            return True
        for alias in sorted(self._pm_aliases.keys(), key=len, reverse=True):
            if text == alias or text.startswith(f"{alias} ") or text.startswith(f"{alias}\t"):
                return True
        return False

    @staticmethod
    def _is_command_target_allowed(message: Dict[str, Any]) -> bool:
        raw_components = message.get("raw_message")
        if not isinstance(raw_components, list):
            return True
        account_id = str(message.get("account_id") or "")
        mentions = [comp for comp in raw_components if isinstance(comp, dict) and comp.get("type") == "at"]
        if not mentions:
            return True
        for comp in mentions:
            data = comp.get("data")
            if isinstance(data, dict) and str(data.get("target_user_id") or "") == account_id:
                return True
        return False

    @staticmethod
    def extract_command_text(message: Dict[str, Any]) -> str:
        """提取命令匹配文本，剥离指向当前机器人的前导结构化 @ 提及。"""
        text = str(message.get("processed_plain_text") or "").strip()
        raw_components = message.get("raw_message")
        account_id = str(message.get("account_id") or "")

        if not text and isinstance(raw_components, list):
            parts: List[str] = []
            for comp in raw_components:
                if not isinstance(comp, dict):
                    continue
                comp_type = comp.get("type")
                if comp_type == "text":
                    parts.append(str(comp.get("data") or ""))
                elif comp_type == "at" and isinstance(comp.get("data"), dict):
                    at_data = comp["data"]
                    if str(at_data.get("target_user_id") or "") != account_id:
                        name = at_data.get("target_user_cardname") or at_data.get("target_user_nickname") or at_data.get("target_user_id")
                        parts.append(f"@{name}")
            text = " ".join(part.strip() for part in parts if part.strip()).strip()

        if not text.startswith("@") or not isinstance(raw_components, list):
            return text

        for comp in raw_components:
            if not isinstance(comp, dict):
                break
            comp_type = comp.get("type")
            if comp_type == "text" and not str(comp.get("data") or "").strip():
                continue
            if comp_type != "at":
                break
            at_data = comp.get("data")
            if not isinstance(at_data, dict):
                break
            if str(at_data.get("target_user_id") or "") != account_id:
                break
            name = str(
                at_data.get("target_user_cardname")
                or at_data.get("target_user_nickname")
                or at_data.get("target_user_id")
                or ""
            )
            prefix = f"@{name}"
            if not text.startswith(prefix):
                break
            remainder = text[len(prefix) :]
            if remainder and not remainder[0].isspace():
                break
            text = remainder.lstrip()
        return text
