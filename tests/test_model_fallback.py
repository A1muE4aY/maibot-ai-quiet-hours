"""MaiBot 已注册模型发现、回退切换、保留注释与崩溃恢复单元测试。"""

from __future__ import annotations

from pathlib import Path

import os
import sys
import tempfile
import tomllib
import unittest

from ._sdk_stub import ensure_maibot_sdk_available, get_maibot_root

ensure_maibot_sdk_available()
sys.path.insert(0, str(get_maibot_root()))

from plugins.ai_quiet_hours.model_fallback import (  # noqa: E402
    ModelFallbackController,
    scan_registered_models,
)


_SAMPLE_MODEL_CONFIG = """# 顶层注释保留
[inner]
version = "1.7.0"

[[api_providers]]
name = "DeepSeek"
base_url = "https://api.deepseek.com"
api_key = "secret-do-not-expose"
client_type = "openai"

[[models]]
model_identifier = "deepseek-chat"
name = "deepseek-v4-pro"
model_type = "llm"
api_provider = "DeepSeek"
visual = false
price_in = 12.0
price_out = 24.0

[[models]]
model_identifier = "deepseek-flash"
name = "deepseek-v4-flash"
model_type = "llm"
api_provider = "DeepSeek"
visual = false
price_in = 1.0
price_out = 2.0

[[models]]
model_identifier = "bge-m3"
name = "bge-embed"
model_type = "embedding"
api_provider = "DeepSeek"

[model_task_config.replyer]
# 保留任务注释
model_list = [
    "deepseek-v4-pro",
]
temperature = 1.0

[model_task_config.planner]
model_list = ["deepseek-v4-pro"]

[model_task_config.utils]
model_list = ["deepseek-v4-pro"]

[model_task_config.fast_model]
model_list = []

[model_task_config.vlm]
model_list = []
"""


class TestModelFallback(unittest.TestCase):
    def test_scan_supports_shared_catalog_and_excludes_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir(parents=True)
            cfg_path = root / "config" / "model_config.toml"
            cfg_path.write_text(_SAMPLE_MODEL_CONFIG, encoding="utf-8")

            shared_path = root / "model_catalog.toml"
            shared_path.write_text(
                """
[inner]
version = "3.0.0"

[[api_providers]]
name = "SharedProvider"
base_url = "https://shared.example.com"
api_key = "shared-secret"

[[models]]
model_identifier = "qwen-turbo"
name = "qwen-flash"
model_type = "llm"
api_provider = "SharedProvider"
visual = true
""",
                encoding="utf-8",
            )

            prev = os.environ.get("MAIBOT_SHARED_MODEL_CATALOG")
            os.environ["MAIBOT_SHARED_MODEL_CATALOG"] = str(shared_path)
            try:
                snapshot = scan_registered_models(configured_root=str(root))
            finally:
                if prev is None:
                    os.environ.pop("MAIBOT_SHARED_MODEL_CATALOG", None)
                else:
                    os.environ["MAIBOT_SHARED_MODEL_CATALOG"] = prev

            self.assertEqual(snapshot.error, "")
            names = [item.name for item in snapshot.models]
            self.assertEqual(names, ["qwen-flash"])
            self.assertEqual(snapshot.provider_names, ["SharedProvider"])
            serialized = str(snapshot.to_dict())
            self.assertNotIn("secret", serialized)

    def test_activate_update_deactivate_and_crash_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir(parents=True)
            cfg_path = root / "config" / "model_config.toml"
            cfg_path.write_text(_SAMPLE_MODEL_CONFIG, encoding="utf-8")
            data_dir = root / "plugin_data"

            ctrl = ModelFallbackController(root, data_dir, instance_root_override=str(root))

            # 无效模型拒绝激活
            invalid_res = ctrl.activate(["non-existent-model"], ["replyer", "planner", "utils"])
            self.assertFalse(invalid_res.ok)
            self.assertFalse(invalid_res.active)

            # 激活为 deepseek-v4-flash
            res1 = ctrl.activate(
                ["deepseek-v4-flash"],
                ["replyer", "planner", "utils", "fast_model", "vlm"],
            )
            self.assertTrue(res1.ok)
            self.assertTrue(res1.active)
            self.assertIn("fast_model", res1.skipped_tasks)
            self.assertIn("vlm", res1.skipped_tasks)

            disk_after_1 = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
            self.assertEqual(disk_after_1["model_task_config"]["replyer"]["model_list"], ["deepseek-v4-flash"])
            self.assertEqual(disk_after_1["model_task_config"]["fast_model"]["model_list"], [])
            self.assertIn("# 顶层注释保留", cfg_path.read_text(encoding="utf-8"))

            # 在已激活状态下改为另一个模型，原备份基线必须保留为 deepseek-v4-pro
            res2 = ctrl.activate(["deepseek-v4-pro", "deepseek-v4-flash"], ["replyer", "planner", "utils"])
            self.assertTrue(res2.ok)
            summary = ctrl.get_active_summary()
            self.assertEqual(summary["original_tasks"]["replyer"], ["deepseek-v4-pro"])

            # 恢复时应还原到最初的 deepseek-v4-pro 并清理备份
            deact = ctrl.deactivate(reason="test_end")
            self.assertTrue(deact.ok)
            self.assertFalse(deact.active)
            self.assertFalse(ctrl.backup_path.exists())
            disk_restored = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
            self.assertEqual(disk_restored["model_task_config"]["replyer"]["model_list"], ["deepseek-v4-pro"])

            # 模拟异常退出残留备份，使用新控制器启动自动恢复
            ctrl.activate(["deepseek-v4-flash"], ["replyer", "planner", "utils"])
            ctrl_restart = ModelFallbackController(root, data_dir, instance_root_override=str(root))
            recovered = ctrl_restart.recover_on_startup()
            self.assertTrue(recovered.ok)
            self.assertTrue(recovered.changed)
            disk_after_recover = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
            self.assertEqual(disk_after_recover["model_task_config"]["replyer"]["model_list"], ["deepseek-v4-pro"])


if __name__ == "__main__":
    unittest.main()
