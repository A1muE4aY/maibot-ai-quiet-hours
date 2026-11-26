"""MaiBot 已注册模型发现与时段模型回退切换模块。

功能：
1. 自动定位当前 MaiBot 实例根目录与 ``config/model_config.toml``（兼容 ``MAIBOT_SHARED_MODEL_CATALOG`` 共享模型目录）；
2. 扫描并返回 MaiBot 中所有已注册模型及各任务当前绑定的模型列表；
3. 在定时静默时段配置了有效回退模型时，原子切换 ``model_config.toml`` 中的任务 ``model_list``，
   并在离开时段、手动关闭、配置热更新、插件卸载或异常重启后恢复原始配置，避免配置污染。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import json
import os
import re
import tomllib

try:
    import tomlkit  # type: ignore
except ImportError:  # pragma: no cover - 精简测试环境可能未安装 tomlkit
    tomlkit = None  # type: ignore[assignment]


DEFAULT_SWITCH_TARGET_TASKS: Tuple[str, ...] = (
    "replyer",
    "planner",
    "utils",
    "memory",
    "learner",
    "fast_model",
)

ALL_SWITCHABLE_TASKS: Tuple[str, ...] = (
    "replyer",
    "planner",
    "utils",
    "memory",
    "learner",
    "fast_model",
    "vlm",
)

TASK_DISPLAY_LABELS: Dict[str, str] = {
    "replyer": "回复生成 (replyer)",
    "planner": "决策规划 (planner)",
    "utils": "通用工具 (utils)",
    "memory": "记忆整理 (memory)",
    "learner": "表达学习 (learner)",
    "fast_model": "快速小模型 (fast_model)",
    "vlm": "视觉识图 (vlm)",
}

# fast_model / learner / memory 等为空时，MaiBot 默认继承 utils，除非显式非空。
_PRIMARY_ALWAYS_PATCH_TASKS = frozenset({"replyer", "planner", "utils"})

_BACKUP_FILENAME = "model_fallback_backup.json"


@dataclass(slots=True)
class RegisteredModelInfo:
    """MaiBot 中已注册的单个模型摘要。"""

    name: str
    model_identifier: str = ""
    model_type: str = "llm"
    api_provider: str = ""
    visual: bool = False
    price_in: float = 0.0
    price_out: float = 0.0

    def format_label(self) -> str:
        """生成适合下拉框与控制台展示的中文标注。"""
        tags: List[str] = [self.model_type or "llm"]
        if self.api_provider:
            tags.append(self.api_provider)
        if self.model_identifier and self.model_identifier != self.name:
            tags.append(self.model_identifier)
        if self.visual:
            tags.append("视觉")
        if self.price_in > 0 or self.price_out > 0:
            tags.append(f"¥{self.price_in:g}/{self.price_out:g}")
        return f"{self.name} [{' · '.join(tags)}]"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "model_identifier": self.model_identifier,
            "model_type": self.model_type,
            "api_provider": self.api_provider,
            "visual": self.visual,
            "price_in": self.price_in,
            "price_out": self.price_out,
            "label": self.format_label(),
        }


@dataclass(slots=True)
class ModelCatalogSnapshot:
    """一次读取 ``model_config.toml``（与共享模型目录）后得到的目录快照。"""

    instance_root: Optional[Path] = None
    model_config_path: Optional[Path] = None
    catalog_path: Optional[Path] = None
    models: List[RegisteredModelInfo] = field(default_factory=list)
    task_models: Dict[str, List[str]] = field(default_factory=dict)
    provider_names: List[str] = field(default_factory=list)
    error: str = ""

    def get_model(self, name: str) -> Optional[RegisteredModelInfo]:
        normalized = str(name or "").strip()
        for item in self.models:
            if item.name == normalized:
                return item
        return None

    def has_model(self, name: str) -> bool:
        return self.get_model(name) is not None

    def llm_models(self, include_non_llm: bool = False) -> List[RegisteredModelInfo]:
        if include_non_llm:
            return list(self.models)
        return [item for item in self.models if item.model_type in {"llm", ""}]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "instance_root": str(self.instance_root) if self.instance_root else "",
            "model_config_path": str(self.model_config_path) if self.model_config_path else "",
            "catalog_path": str(self.catalog_path) if self.catalog_path else "",
            "models": [item.to_dict() for item in self.models],
            "task_models": {k: list(v) for k, v in self.task_models.items()},
            "provider_names": list(self.provider_names),
            "error": self.error,
        }


@dataclass(slots=True)
class ModelSwitchResult:
    """模型回退切换或恢复的执行结果。"""

    ok: bool = True
    active: bool = False
    changed: bool = False
    applied_models: List[str] = field(default_factory=list)
    updated_tasks: List[str] = field(default_factory=list)
    skipped_tasks: List[str] = field(default_factory=list)
    warning: str = ""
    error: str = ""


def _normalize_str_list(values: Iterable[Any]) -> List[str]:
    result: List[str] = []
    seen = set()
    for raw in values:
        text = str(raw or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def discover_maibot_instance_root(
    plugin_dir: Optional[Path] = None,
    configured_root: str = "",
) -> Optional[Path]:
    """尽力定位当前运行中的 MaiBot 实例根目录。"""
    candidates: List[Path] = []
    override = str(configured_root or "").strip()
    if override:
        override_path = Path(os.path.expandvars(os.path.expanduser(override))).resolve()
        if (override_path / "config" / "model_config.toml").is_file():
            return override_path
        if override_path.name == "model_config.toml" and override_path.is_file():
            return override_path.parent.parent
        candidates.append(override_path)

    env_dir = str(os.environ.get("MAIBOT_INSTANCE_DIR", "") or "").strip()
    if env_dir:
        candidates.append(Path(os.path.expandvars(os.path.expanduser(env_dir))).resolve())

    try:
        from src.common.paths import get_instance_root  # type: ignore

        candidates.append(Path(get_instance_root()).resolve())
    except Exception:
        pass

    anchors: List[Path] = [Path.cwd().resolve()]
    if plugin_dir is not None:
        anchors.append(Path(plugin_dir).resolve())
    anchors.append(Path(__file__).resolve().parent)

    for anchor in anchors:
        for base in (anchor, *anchor.parents):
            candidates.append(base)
            source_dir = base / "source"
            if source_dir.is_dir():
                try:
                    subdirs = sorted(
                        (p / "MaiBot-main" for p in source_dir.iterdir() if p.is_dir()),
                        reverse=True,
                    )
                    candidates.extend(subdirs)
                except OSError:
                    pass

    seen = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        key = str(resolved).casefold()
        if key in seen:
            continue
        seen.add(key)
        if (resolved / "config" / "model_config.toml").is_file():
            return resolved

    # 二次兜底：若尚无 config/model_config.toml，但能确认实例根（含 config/ 与 bot.py / src/）
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if (resolved / "config").is_dir() and ((resolved / "bot.py").is_file() or (resolved / "src").is_dir()):
            return resolved
    return None


def _resolve_shared_catalog_path() -> Optional[Path]:
    raw = str(os.environ.get("MAIBOT_SHARED_MODEL_CATALOG", "") or "").strip()
    if not raw:
        return None
    path = Path(os.path.expandvars(os.path.expanduser(raw))).resolve()
    return path if path.is_file() else None


def scan_registered_models(
    plugin_dir: Optional[Path] = None,
    configured_root: str = "",
) -> ModelCatalogSnapshot:
    """读取 MaiBot 已注册模型目录及各任务当前分配的模型列表。"""
    instance_root = discover_maibot_instance_root(plugin_dir=plugin_dir, configured_root=configured_root)
    catalog_path = _resolve_shared_catalog_path()
    model_config_path = (instance_root / "config" / "model_config.toml") if instance_root is not None else None

    raw_model_data: Dict[str, Any] = {}
    if model_config_path is not None and model_config_path.is_file():
        try:
            with model_config_path.open("rb") as fh:
                raw_model_data = tomllib.load(fh)
        except Exception as exc:
            return ModelCatalogSnapshot(
                instance_root=instance_root,
                model_config_path=model_config_path,
                catalog_path=catalog_path,
                error=f"读取 model_config.toml 失败: {exc}",
            )
    elif catalog_path is None:
        return ModelCatalogSnapshot(
            instance_root=instance_root,
            model_config_path=model_config_path,
            catalog_path=None,
            error="未找到 MaiBot config/model_config.toml（请在设置中检查或填写实例目录）",
        )

    effective_data = dict(raw_model_data)
    if catalog_path is not None:
        try:
            with catalog_path.open("rb") as fh:
                shared_data = tomllib.load(fh)
            for key in ("api_providers", "models"):
                if key in shared_data:
                    effective_data[key] = shared_data[key]
        except Exception as exc:
            return ModelCatalogSnapshot(
                instance_root=instance_root,
                model_config_path=model_config_path,
                catalog_path=catalog_path,
                error=f"读取共享模型目录失败: {exc}",
            )

    models: List[RegisteredModelInfo] = []
    seen_names = set()
    for entry in effective_data.get("models", []) or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        if not name or name in seen_names:
            continue
        seen_names.add(name)
        models.append(
            RegisteredModelInfo(
                name=name,
                model_identifier=str(entry.get("model_identifier") or "").strip(),
                model_type=str(entry.get("model_type") or "llm").strip().lower() or "llm",
                api_provider=str(entry.get("api_provider") or "").strip(),
                visual=bool(entry.get("visual", False)),
                price_in=float(entry.get("price_in") or 0.0),
                price_out=float(entry.get("price_out") or 0.0),
            )
        )

    # 排序：对话大模型优先，其次按名称
    models.sort(key=lambda item: (0 if item.model_type in {"llm", ""} else 1, item.name.lower()))

    provider_names = _normalize_str_list(
        entry.get("name")
        for entry in (effective_data.get("api_providers", []) or [])
        if isinstance(entry, dict)
    )

    task_models: Dict[str, List[str]] = {}
    raw_tasks = effective_data.get("model_task_config", {}) or {}
    if isinstance(raw_tasks, dict):
        for task_name, task_cfg in raw_tasks.items():
            if isinstance(task_cfg, dict) and isinstance(task_cfg.get("model_list"), list):
                task_models[str(task_name)] = _normalize_str_list(task_cfg["model_list"])

    return ModelCatalogSnapshot(
        instance_root=instance_root,
        model_config_path=model_config_path,
        catalog_path=catalog_path,
        models=models,
        task_models=task_models,
        provider_names=provider_names,
    )


def _format_toml_str_list(values: Sequence[str]) -> str:
    encoded = ", ".join(json.dumps(str(v), ensure_ascii=False) for v in values)
    return f"[{encoded}]"


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    tmp_path.write_text(text, encoding="utf-8")
    os.replace(tmp_path, path)


def _patch_toml_text_pure_python(original_text: str, updates: Dict[str, Optional[List[str]]]) -> str:
    """在缺少 tomlkit 时的纯 Python 手术式 TOML 更新器（保留其它节与注释）。

    ``updates`` 中值为 ``list[str]`` 表示写入 ``model_list = [...]``，
    值为 ``None`` 表示删除对应 ``[model_task_config.<task>]`` 中的 ``model_list`` 键或空自建节。
    """
    newline = "\r\n" if "\r\n" in original_text else "\n"
    lines = original_text.splitlines(keepends=False)
    section_re = re.compile(r"^\s*\[([^\[\]]+)\]\s*(?:#.*)?$")
    key_re = re.compile(r"^\s*model_list\s*=\s*(.*)$")

    # 定位各 section 范围 [start_idx, end_idx)
    section_ranges: Dict[str, Tuple[int, int]] = {}
    current_name: Optional[str] = None
    current_start = 0
    for idx, line in enumerate(lines):
        m = section_re.match(line)
        if m:
            if current_name is not None:
                section_ranges[current_name] = (current_start, idx)
            current_name = m.group(1).strip()
            current_start = idx
    if current_name is not None:
        section_ranges[current_name] = (current_start, len(lines))

    pending_append: List[str] = []
    out_lines = list(lines)

    # 先按后往前修补已有 section，避免索引偏移
    existing_ops: List[Tuple[int, int, str, Optional[List[str]]]] = []
    for task_name, new_value in updates.items():
        sec_key = f"model_task_config.{task_name}"
        if sec_key in section_ranges:
            start, end = section_ranges[sec_key]
            existing_ops.append((start, end, task_name, new_value))
        elif new_value is not None:
            pending_append.append(f"[model_task_config.{task_name}]")
            pending_append.append(f"model_list = {_format_toml_str_list(new_value)}")

    existing_ops.sort(key=lambda item: item[0], reverse=True)
    for start, end, _task_name, new_value in existing_ops:
        sec_body = out_lines[start + 1 : end]
        new_sec_body: List[str] = []
        i = 0
        replaced = False
        while i < len(sec_body):
            line = sec_body[i]
            if not replaced and key_re.match(line):
                replaced = True
                # 计算多行数组平衡括号
                bracket_balance = line.count("[") - line.count("]")
                while bracket_balance > 0 and i + 1 < len(sec_body):
                    i += 1
                    bracket_balance += sec_body[i].count("[") - sec_body[i].count("]")
                if new_value is not None:
                    new_sec_body.append(f"model_list = {_format_toml_str_list(new_value)}")
            else:
                new_sec_body.append(line)
            i += 1
        if not replaced and new_value is not None:
            new_sec_body.insert(0, f"model_list = {_format_toml_str_list(new_value)}")
        out_lines[start + 1 : end] = new_sec_body

    result_text = newline.join(out_lines)
    if pending_append:
        if result_text and not result_text.endswith(newline):
            result_text += newline
        result_text += newline + newline.join(pending_append) + newline
    elif original_text.endswith(("\n", "\r\n")) and not result_text.endswith(newline):
        result_text += newline
    return result_text


def patch_model_config_tasks(model_config_path: Path, updates: Dict[str, Optional[List[str]]]) -> None:
    """将 ``model_task_config.<task>.model_list`` 原子写入 ``model_config.toml``，保留注释与格式。"""
    original_text = model_config_path.read_text(encoding="utf-8")
    if tomlkit is not None:
        doc = tomlkit.parse(original_text)
        task_root = doc.get("model_task_config")
        if task_root is None:
            task_root = tomlkit.table()
            doc["model_task_config"] = task_root
        for task_name, new_value in updates.items():
            if new_value is None:
                if task_name in task_root and isinstance(task_root[task_name], dict):
                    task_root[task_name].pop("model_list", None)
            else:
                if task_name not in task_root or not isinstance(task_root[task_name], dict):
                    task_root[task_name] = tomlkit.table()
                arr = tomlkit.array()
                arr.extend([str(item) for item in new_value])
                task_root[task_name]["model_list"] = arr
        new_text = tomlkit.dumps(doc)
    else:
        new_text = _patch_toml_text_pure_python(original_text, updates)
    _atomic_write_text(model_config_path, new_text)


class ModelFallbackController:
    """负责验证回退模型、执行可逆的任务模型切换及崩溃恢复。"""

    def __init__(
        self,
        plugin_dir: Path,
        data_dir: Path,
        instance_root_override: str = "",
    ) -> None:
        self._plugin_dir = Path(plugin_dir)
        self._data_dir = Path(data_dir)
        self._instance_root_override = instance_root_override
        self._active_applied: Dict[str, List[str]] = {}
        self._last_models: List[str] = []

    def set_instance_root_override(self, instance_root: str) -> None:
        self._instance_root_override = str(instance_root or "").strip()

    @property
    def backup_path(self) -> Path:
        return self._data_dir / _BACKUP_FILENAME

    def scan_catalog(self) -> ModelCatalogSnapshot:
        return scan_registered_models(
            plugin_dir=self._plugin_dir,
            configured_root=self._instance_root_override,
        )

    def resolve_effective_models(
        self,
        requested_models: Sequence[str],
        catalog: Optional[ModelCatalogSnapshot] = None,
    ) -> Tuple[List[str], List[str], ModelCatalogSnapshot]:
        """过滤并校验请求的回退模型是否在 MaiBot 中已注册。

        Returns:
            Tuple[List[str], List[str], ModelCatalogSnapshot]:
                ``(valid_models, invalid_models, catalog)``
        """
        snapshot = catalog or self.scan_catalog()
        normalized = _normalize_str_list(requested_models)
        if snapshot.error or not snapshot.models:
            return [], normalized, snapshot
        registered_names = {item.name for item in snapshot.models}
        valid = [name for name in normalized if name in registered_names]
        invalid = [name for name in normalized if name not in registered_names]
        return valid, invalid, snapshot

    def read_backup(self) -> Optional[Dict[str, Any]]:
        if not self.backup_path.is_file():
            return None
        try:
            payload = json.loads(self.backup_path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else None
        except Exception:
            return None

    def is_active(self) -> bool:
        return bool(self._active_applied or self.read_backup())

    def get_active_summary(self) -> Dict[str, Any]:
        backup = self.read_backup()
        if not backup:
            return {
                "active": False,
                "applied_models": [],
                "applied_tasks": {},
                "original_tasks": {},
                "applied_at": "",
            }
        return {
            "active": True,
            "applied_models": list(backup.get("applied_models", [])),
            "applied_tasks": dict(backup.get("applied_tasks", {})),
            "original_tasks": {
                k: (v.get("original_model_list") if isinstance(v, dict) else None)
                for k, v in (backup.get("original_tasks") or {}).items()
            },
            "applied_at": str(backup.get("applied_at") or ""),
        }

    def recover_on_startup(self) -> ModelSwitchResult:
        """启动时检测是否存在异常退出残留的模型切换备份，若有则安全还原。"""
        backup = self.read_backup()
        if not backup:
            return ModelSwitchResult(ok=True, active=False, changed=False)
        return self.deactivate(reason="startup_recovery")

    def activate(
        self,
        fallback_models: Sequence[str],
        target_tasks: Sequence[str],
        *,
        switch_mode: str = "replace",
    ) -> ModelSwitchResult:
        """激活或更新时段回退模型切换，并保存原始配置快照。"""
        valid_models, invalid_models, snapshot = self.resolve_effective_models(fallback_models)
        warnings: List[str] = []
        if invalid_models:
            warnings.append(f"以下回退模型未在 MaiBot 中注册，已跳过: {', '.join(invalid_models)}")
        if not valid_models:
            # 没有任何可用回退模型时退回静默逻辑，调用方据此切换静默。
            if self.is_active():
                self.deactivate(reason="no_valid_fallback_models")
            return ModelSwitchResult(
                ok=False,
                active=False,
                changed=False,
                warning="；".join(warnings),
                error=snapshot.error or "无可用的已注册回退模型",
            )

        if snapshot.model_config_path is None or not snapshot.model_config_path.is_file():
            return ModelSwitchResult(
                ok=False,
                active=False,
                changed=False,
                error=snapshot.error or "未找到 model_config.toml，无法切换任务模型",
            )

        try:
            raw_disk = tomllib.loads(snapshot.model_config_path.read_text(encoding="utf-8"))
        except Exception as exc:
            return ModelSwitchResult(ok=False, active=False, changed=False, error=f"解析 model_config.toml 失败: {exc}")

        disk_tasks = raw_disk.get("model_task_config") or {}
        existing_backup = self.read_backup() or {}
        original_tasks: Dict[str, Dict[str, Any]] = dict(existing_backup.get("original_tasks") or {})
        prev_applied_tasks: Dict[str, List[str]] = dict(existing_backup.get("applied_tasks") or {})

        normalized_targets = _normalize_str_list(target_tasks) or list(DEFAULT_SWITCH_TARGET_TASKS)
        desired_target_set = set(normalized_targets)
        updates: Dict[str, Optional[List[str]]] = {}
        new_applied_tasks: Dict[str, List[str]] = {}
        updated_tasks: List[str] = []
        skipped_tasks: List[str] = []

        # 1. 先恢复此前改写过但已移出 target_tasks 的任务
        for prev_task, prev_applied_list in list(prev_applied_tasks.items()):
            if prev_task in desired_target_set:
                continue
            orig_meta = original_tasks.pop(prev_task, None)
            if not isinstance(orig_meta, dict):
                continue
            current_disk_list = _normalize_str_list(
                (disk_tasks.get(prev_task) or {}).get("model_list", [])
                if isinstance(disk_tasks.get(prev_task), dict)
                else []
            )
            if current_disk_list == prev_applied_list:
                updates[prev_task] = orig_meta.get("original_model_list") if orig_meta.get("had_model_list") else None

        # 2. 计算需要切换的目标任务新列表
        for task in normalized_targets:
            task_table = disk_tasks.get(task)
            table_exists = isinstance(task_table, dict)
            had_model_list = table_exists and isinstance(task_table.get("model_list"), list)
            current_list = _normalize_str_list(task_table.get("model_list", [])) if had_model_list else []

            # 若尚未记录该任务原始配置，先锁定真实原始值
            if task not in original_tasks:
                # 若文件已经留有插件的旧值且与内存/备份一致不会进入此处；首次记录即真实原始值
                original_tasks[task] = {
                    "table_exists": table_exists,
                    "had_model_list": had_model_list,
                    "original_model_list": list(current_list) if had_model_list else None,
                }

            orig_entry = original_tasks[task]
            orig_list = list(orig_entry.get("original_model_list") or [])

            # 对默认可能留空继承 utils 的辅助任务（如 fast_model / learner / memory / vlm）：
            # 若原列表本来就是空（[] 或未定义），且 utils 已在目标列表中，则不强行填充，让其继续继承或保持关闭。
            if (
                task not in _PRIMARY_ALWAYS_PATCH_TASKS
                and not orig_list
                and (task != "vlm")
                and "utils" in desired_target_set
            ):
                skipped_tasks.append(task)
                continue
            if task == "vlm" and not orig_list:
                skipped_tasks.append(task)
                continue
            if task == "vlm":
                has_visual = any((m := snapshot.get_model(name)) is not None and m.visual for name in valid_models)
                if not has_visual:
                    warnings.append("回退模型均未标记视觉支持(visual=true)，已跳过视觉识图任务 vlm")
                    skipped_tasks.append(task)
                    continue

            if switch_mode == "prepend":
                target_list = valid_models + [m for m in orig_list if m not in valid_models]
            else:
                target_list = list(valid_models)

            new_applied_tasks[task] = target_list
            if current_list != target_list:
                updates[task] = target_list
                updated_tasks.append(task)

        if updates:
            try:
                patch_model_config_tasks(snapshot.model_config_path, updates)
            except Exception as exc:
                return ModelSwitchResult(ok=False, active=False, changed=False, error=f"写入 model_config.toml 失败: {exc}")

        backup_payload = {
            "version": 1,
            "model_config_path": str(snapshot.model_config_path),
            "applied_models": list(valid_models),
            "applied_at": existing_backup.get("applied_at") or datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "original_tasks": original_tasks,
            "applied_tasks": new_applied_tasks,
        }
        self._data_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(self.backup_path, json.dumps(backup_payload, ensure_ascii=False, indent=2) + "\n")
        self._active_applied = {k: list(v) for k, v in new_applied_tasks.items()}
        self._last_models = list(valid_models)

        return ModelSwitchResult(
            ok=True,
            active=True,
            changed=bool(updates),
            applied_models=list(valid_models),
            updated_tasks=updated_tasks or list(new_applied_tasks.keys()),
            skipped_tasks=skipped_tasks,
            warning="；".join(warnings),
        )

    def deactivate(self, reason: str = "") -> ModelSwitchResult:
        """从备份安全还原 ``model_config.toml`` 的原始任务模型配置。"""
        del reason
        backup = self.read_backup()
        self._active_applied = {}
        self._last_models = []
        if not backup:
            return ModelSwitchResult(ok=True, active=False, changed=False)

        raw_path = str(backup.get("model_config_path") or "").strip()
        model_config_path = Path(raw_path) if raw_path else None
        if model_config_path is None or not model_config_path.is_file():
            snapshot = self.scan_catalog()
            model_config_path = snapshot.model_config_path

        warnings: List[str] = []
        restored_tasks: List[str] = []
        if model_config_path is not None and model_config_path.is_file():
            try:
                raw_disk = tomllib.loads(model_config_path.read_text(encoding="utf-8"))
                disk_tasks = raw_disk.get("model_task_config") or {}
                applied_tasks: Dict[str, List[str]] = dict(backup.get("applied_tasks") or {})
                original_tasks: Dict[str, Dict[str, Any]] = dict(backup.get("original_tasks") or {})
                updates: Dict[str, Optional[List[str]]] = {}

                for task, orig_meta in original_tasks.items():
                    if task not in applied_tasks:
                        continue
                    expected_applied = _normalize_str_list(applied_tasks.get(task) or [])
                    task_table = disk_tasks.get(task)
                    current_disk = (
                        _normalize_str_list(task_table.get("model_list", []))
                        if isinstance(task_table, dict) and isinstance(task_table.get("model_list"), list)
                        else []
                    )
                    # 若用户在回退期间手动修改过该任务，则保留用户改动不强行覆盖
                    if current_disk != expected_applied:
                        warnings.append(f"任务 {task} 已被外部修改，还原时保留当前用户配置")
                        continue
                    if isinstance(orig_meta, dict) and orig_meta.get("had_model_list"):
                        updates[task] = list(orig_meta.get("original_model_list") or [])
                    else:
                        updates[task] = None
                    restored_tasks.append(task)

                if updates:
                    patch_model_config_tasks(model_config_path, updates)
            except Exception as exc:
                return ModelSwitchResult(
                    ok=False,
                    active=True,
                    changed=False,
                    error=f"恢复 model_config.toml 失败: {exc}",
                )

        try:
            self.backup_path.unlink(missing_ok=True)
        except OSError:
            pass

        return ModelSwitchResult(
            ok=True,
            active=False,
            changed=bool(restored_tasks),
            updated_tasks=restored_tasks,
            warning="；".join(warnings),
        )
