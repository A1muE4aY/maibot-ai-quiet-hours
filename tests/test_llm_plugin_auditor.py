"""大模型插件检测、AST 静态分析与命令识别器单元测试。"""

from __future__ import annotations

from pathlib import Path

import json
import sys
import tempfile
import unittest

from ._sdk_stub import ensure_maibot_sdk_available, get_maibot_root

ensure_maibot_sdk_available()
sys.path.insert(0, str(get_maibot_root()))

from plugins.ai_quiet_hours.llm_plugin_auditor import (  # noqa: E402
    LlmPluginAuditor,
    scan_plugin_source_for_llm_sync,
)
from plugins.ai_quiet_hours.settings import PluginGuardSectionConfig  # noqa: E402


class TestLlmPluginAuditor(unittest.TestCase):
    def test_detects_manifest_and_ast_llm_plugins_without_false_positives(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            # 插件 1：Manifest 声明 llm.generate
            p1 = root / "ai_writer"
            p1.mkdir()
            (p1 / "_manifest.json").write_text(
                json.dumps({"id": "demo.ai-writer", "name": "AI 写作", "version": "1.0.0", "capabilities": ["llm.generate", "send.text"]}),
                encoding="utf-8",
            )
            (p1 / "plugin.py").write_text("async def run(ctx):\n    return await ctx.llm.generate('hi')\n", encoding="utf-8")

            # 插件 2：未声明 capabilities，但源码直连 OpenAI SDK
            p2 = root / "direct_openai"
            p2.mkdir()
            (p2 / "_manifest.json").write_text(
                json.dumps({"id": "demo.direct-openai", "name": "外部模型插件", "version": "1.0.0", "capabilities": ["send.text"]}),
                encoding="utf-8",
            )
            (p2 / "plugin.py").write_text("from openai import AsyncOpenAI\nclient = AsyncOpenAI()\n", encoding="utf-8")

            # 插件 3：纯非大模型工具插件（含普通字符串但不使用 HTTP 客户端）
            p3 = root / "clock_tool"
            p3.mkdir()
            (p3 / "_manifest.json").write_text(
                json.dumps({"id": "demo.clock-tool", "name": "时钟工具", "version": "1.0.0", "capabilities": ["send.text"]}),
                encoding="utf-8",
            )
            (p3 / "plugin.py").write_text("DOC = 'note about /chat/completions'\ndef now():\n    return '12:00'\n", encoding="utf-8")

            # 插件 4：通过 httpx 请求 /chat/completions
            p4 = root / "raw_http_llm"
            p4.mkdir()
            (p4 / "_manifest.json").write_text(
                json.dumps({"id": "demo.raw-http-llm", "name": "HTTP 模型插件", "version": "1.0.0", "capabilities": ["send.text"]}),
                encoding="utf-8",
            )
            (p4 / "plugin.py").write_text(
                "import httpx\nURL = 'https://api.example.com/v1/chat/completions'\n",
                encoding="utf-8",
            )

            # 插件 5：适配器插件（包含 MESSAGE_GATEWAY 与一个 LLM 命令）
            p5 = root / "my_adapter"
            p5.mkdir()
            (p5 / "_manifest.json").write_text(
                json.dumps(
                    {
                        "id": "demo.my-adapter",
                        "name": "测试适配器",
                        "version": "1.0.0",
                        "plugin_type": "adapter",
                        "capabilities": ["llm.generate"],
                    }
                ),
                encoding="utf-8",
            )
            (p5 / "plugin.py").write_text("pass\n", encoding="utf-8")

            runtime_plugins = {
                "demo.ai-writer": {
                    "display_name": "AI 写作",
                    "version": "1.0.0",
                    "components": [
                        {"name": "write", "full_name": "demo.ai-writer.write", "type": "COMMAND", "enabled": True, "metadata": {"command_pattern": r"^/write"}},
                        {"name": "bg_hook", "full_name": "demo.ai-writer.bg_hook", "type": "HOOK_HANDLER", "enabled": True, "metadata": {}},
                    ],
                },
                "demo.clock-tool": {
                    "display_name": "时钟工具",
                    "version": "1.0.0",
                    "components": [
                        {"name": "time", "full_name": "demo.clock-tool.time", "type": "COMMAND", "enabled": True, "metadata": {"command_pattern": r"^/time$"}},
                    ],
                },
                "demo.my-adapter": {
                    "display_name": "测试适配器",
                    "version": "1.0.0",
                    "components": [
                        {"name": "gw", "full_name": "demo.my-adapter.gw", "type": "MESSAGE_GATEWAY", "enabled": True, "metadata": {}},
                        {"name": "ai_tool", "full_name": "demo.my-adapter.ai_tool", "type": "TOOL", "enabled": True, "metadata": {}},
                    ],
                },
            }

            guard_cfg = PluginGuardSectionConfig(
                force_allow_commands=["demo.ai-writer.safe_cmd"],
            )
            auditor = LlmPluginAuditor("a1mue4ay.ai-quiet-hours", root)
            records = auditor.audit_sync(runtime_plugins, guard_cfg, plugin_roots=[root])

            self.assertTrue(records["demo.ai-writer"].should_block)
            self.assertTrue(records["demo.direct-openai"].should_block)
            self.assertTrue(records["demo.raw-http-llm"].should_block)
            self.assertFalse(records["demo.clock-tool"].should_block)

            # 命令阻断判定
            self.assertTrue(auditor.should_block_command("demo.ai-writer", "write", guard_cfg)[0])
            self.assertFalse(auditor.should_block_command("demo.ai-writer", "safe_cmd", guard_cfg)[0])
            self.assertFalse(auditor.should_block_command("demo.clock-tool", "time", guard_cfg)[0])
            self.assertFalse(auditor.should_block_command("a1mue4ay.ai-quiet-hours", "quiet_status", guard_cfg)[0])
            self.assertFalse(auditor.should_block_command("core", "pm", guard_cfg)[0])

            # 可禁用组件永不包含 MESSAGE_GATEWAY
            disableable = auditor.get_disableable_components(include_commands=False)
            full_names = {item[1] for item in disableable}
            self.assertIn("demo.ai-writer.bg_hook", full_names)
            self.assertIn("demo.my-adapter.ai_tool", full_names)
            self.assertNotIn("demo.my-adapter.gw", full_names)
            self.assertNotIn("demo.ai-writer.write", full_names)

            # 命令识别器测试
            self.assertTrue(auditor.is_command_message({"processed_plain_text": "/time"}))
            self.assertTrue(auditor.is_command_message({"processed_plain_text": "/clear"}))
            self.assertTrue(auditor.is_command_message({"processed_plain_text": "/pm plugin list"}))
            self.assertTrue(auditor.is_command_message({"processed_plain_text": "/offline group"}))
            self.assertFalse(auditor.is_command_message({"processed_plain_text": "今天天气怎么样"}))

            # 支持前导 @bot 命令提取
            at_msg = {
                "account_id": "bot123",
                "processed_plain_text": "@麦麦 /time",
                "raw_message": [
                    {"type": "at", "data": {"target_user_id": "bot123", "target_user_nickname": "麦麦"}},
                    {"type": "text", "data": " /time"},
                ],
            }
            self.assertTrue(auditor.is_command_message(at_msg, command_require_self_mention=True))

    def test_real_repo_non_llm_plugins_are_not_flagged(self) -> None:
        maibot_root = get_maibot_root()
        hello_findings = scan_plugin_source_for_llm_sync(maibot_root / "plugins" / "hello_world_plugin")
        typo_findings = scan_plugin_source_for_llm_sync(maibot_root / "src" / "plugins" / "built_in" / "chinese_typo")
        self.assertEqual(hello_findings, [])
        self.assertEqual(typo_findings, [])


if __name__ == "__main__":
    unittest.main()
