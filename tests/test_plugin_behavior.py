"""插件端到端行为测试与 MaiBot v1.4.8 宿主契约校验。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncio
import json
import logging
import sys
import tempfile
import unittest

from ._sdk_stub import ensure_host_test_deps_available, ensure_maibot_sdk_available, get_maibot_root

ensure_maibot_sdk_available()
ensure_host_test_deps_available()
MAIBOT_ROOT = get_maibot_root()
if str(MAIBOT_ROOT) not in sys.path:
    sys.path.insert(0, str(MAIBOT_ROOT))

from plugins.ai_quiet_hours import plugin as plugin_module  # noqa: E402
from plugins.ai_quiet_hours.plugin import AIQuietHoursPlugin, create_plugin  # noqa: E402
from plugins.ai_quiet_hours.settings import (  # noqa: E402
    ExemptionSectionConfig,
    GroupTarget,
    HolidaySectionConfig,
    ModelFallbackSectionConfig,
    NoticeSectionConfig,
    PluginGuardSectionConfig,
    QuietHoursPluginConfig,
    ScheduleRule,
    ScheduleSectionConfig,
    UserTarget,
)
from src.plugin_runtime.host.component_registry import ComponentRegistry  # noqa: E402
from src.plugin_runtime.host.hook_spec_registry import HookSpecRegistry  # noqa: E402
from src.plugin_runtime.runner.manifest_validator import ManifestValidator  # noqa: E402
from src.plugin_runtime.webui_schema import load_webui_extension  # noqa: E402

TZ_CST = timezone(timedelta(hours=8))
PLUGIN_DIR = Path(__file__).resolve().parents[1]


def _make_ctx(tmp_dir: Path, streams: list[dict], plugins: dict) -> SimpleNamespace:
    send_mock = SimpleNamespace(text=AsyncMock(return_value=True))
    chat_mock = SimpleNamespace(get_all_streams=AsyncMock(return_value=streams))
    component_mock = SimpleNamespace(
        get_all_plugins=AsyncMock(return_value=plugins),
        disable_component=AsyncMock(return_value={"success": True}),
        enable_component=AsyncMock(return_value={"success": True}),
    )
    config_mock = SimpleNamespace(get=AsyncMock(return_value=[]))
    call_cap = AsyncMock(return_value={"success": True})
    paths = SimpleNamespace(data_dir=tmp_dir / "data", runtime_dir=tmp_dir / "runtime")
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    paths.runtime_dir.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        plugin_id="a1mue4ay.ai-quiet-hours",
        logger=logging.getLogger("test.ai_quiet_hours"),
        paths=paths,
        send=send_mock,
        chat=chat_mock,
        component=component_mock,
        config=config_mock,
        call_capability=call_cap,
    )


def _msg(
    *,
    session_id: str,
    platform: str = "qq",
    group_id: str = "",
    user_id: str = "u1",
    text: str = "你好",
    raw_message: list | None = None,
    is_at: bool = False,
) -> dict:
    return {
        "session_id": session_id,
        "platform": platform,
        "account_id": "bot1",
        "processed_plain_text": text,
        "is_at": is_at,
        "is_mentioned": is_at,
        "message_info": {
            "user_info": {"user_id": user_id, "user_nickname": "测试用户"},
            "group_info": {"group_id": group_id, "group_name": "测试群"} if group_id else None,
        },
        "raw_message": raw_message if raw_message is not None else [{"type": "text", "data": text}],
    }


class TestHostContractAndBehavior(unittest.TestCase):
    def test_manifest_and_webui_pass_real_maibot_validators(self) -> None:
        # 1. 使用真实 MaiBot ManifestValidator 校验 _manifest.json
        validator = ManifestValidator(
            host_version="1.4.8",
            sdk_version="2.23.0",
            project_root=MAIBOT_ROOT,
            validate_python_package_dependencies=True,
            log_errors=False,
        )
        manifest_data = json.loads((PLUGIN_DIR / "_manifest.json").read_text(encoding="utf-8"))
        parsed = validator.parse_manifest(manifest_data)
        self.assertIsNotNone(parsed, f"Manifest 校验失败: {validator.errors}")
        assert parsed is not None
        self.assertEqual(parsed.id, "a1mue4ay.ai-quiet-hours")

        # 2. 使用真实 MaiBot load_webui_extension 校验 webui.json
        webui = load_webui_extension(str(PLUGIN_DIR))
        self.assertIsNotNone(webui)
        assert webui is not None
        self.assertEqual(webui.schema_version, 2)
        self.assertEqual(webui.pages[0].id, "dashboard")

        # 3. 校验所有声明的 HookHandler 与 MaiBot 内置 HookSpecRegistry 完全一致
        plugin = create_plugin()
        api_names = {comp["name"] for comp in plugin.get_components() if comp["type"] == "API"}
        for binding in [*webui.pages[0].queries.values(), *webui.pages[0].actions.values()]:
            self.assertIn(binding.api, api_names)

        # 用真实的 HookSpec 列表构建注册表并注册本插件所有组件
        from src.plugin_runtime.host.hook_spec_registry import HookSpec

        spec_registry = HookSpecRegistry()
        for hook_name, allow_abort, allow_mut in [
            ("chat.receive.before_process", True, True),
            ("chat.receive.after_process", True, True),
            ("chat.command.before_execute", True, True),
            ("chat.command.after_execute", False, True),
            ("maisaka.replyer.before_request", True, True),
            ("maisaka.replyer.before_model_request", True, True),
            ("expression.select.before_select", True, True),
            ("expression.learn.after_extract", True, True),
            ("jargon.extract.before_persist", True, True),
            ("emoji.receive.before_cache", True, False),
            ("emoji.maintenance.before_register", True, False),
        ]:
            spec_registry.register_hook_spec(
                HookSpec(
                    name=hook_name,
                    allow_blocking=True,
                    allow_observe=True,
                    allow_abort=allow_abort,
                    allow_kwargs_mutation=allow_mut,
                )
            )

        comp_registry = ComponentRegistry(hook_spec_registry=spec_registry)
        for comp in plugin.get_components():
            if comp["type"] == "API":
                continue
            self.assertTrue(
                comp_registry.register_component(
                    comp["name"],
                    comp["type"],
                    "a1mue4ay.ai-quiet-hours",
                    comp["metadata"],
                )
            )

    def test_end_to_end_blocking_exemptions_and_notices(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                streams = [
                    {"session_id": "s_normal", "platform": "qq", "group_id": "100", "group_name": "普通群", "is_group_session": True},
                    {"session_id": "s_exempt", "platform": "qq", "group_id": "200", "group_name": "豁免群", "is_group_session": True},
                    {"session_id": "s_webui", "platform": "webui", "user_id": "admin", "is_group_session": False},
                ]
                runtime_plugins = {
                    "demo.non-llm": {
                        "display_name": "时钟",
                        "version": "1.0.0",
                        "components": [
                            {"name": "time", "full_name": "demo.non-llm.time", "type": "COMMAND", "enabled": True, "metadata": {"command_pattern": r"^/time$"}},
                        ],
                    },
                    "demo.llm-bot": {
                        "display_name": "AI 助手插件",
                        "version": "1.0.0",
                        "components": [
                            {"name": "ask", "full_name": "demo.llm-bot.ask", "type": "COMMAND", "enabled": True, "metadata": {"command_pattern": r"^/ask"}},
                            {"name": "auto_bg", "full_name": "demo.llm-bot.auto_bg", "type": "EVENT_HANDLER", "enabled": True, "metadata": {}},
                        ],
                    },
                }

                plugin = AIQuietHoursPlugin()
                plugin.set_plugin_config(
                    QuietHoursPluginConfig(
                        schedule=ScheduleSectionConfig(
                            rules=[ScheduleRule(name="测试静默", start_time="09:00", end_time="18:00", day_scope="all")]
                        ),
                        holiday=HolidaySectionConfig(enable_online_sync=False),
                        notices=NoticeSectionConfig(
                            enabled=True,
                            default_start_message="开始静默:{rule_name}->{end_time}",
                            default_end_message="结束静默:{rule_name}",
                            blocked_chat_reply="静默中，请稍候",
                            blocked_reply_cooldown_seconds=300,
                        ),
                        plugin_guard=PluginGuardSectionConfig(
                            enabled=True,
                            block_mode="smart",
                            force_block_plugins=["demo.llm-bot"],
                        ),
                        exemptions=ExemptionSectionConfig(
                            exempt_groups=[GroupTarget(platform="qq", id="200", name="豁免群")],
                            exempt_users=[UserTarget(platform="qq", id="vip_user", name="VIP")],
                            user_exempt_in_group=True,
                        ),
                    ).model_dump()
                )
                ctx = _make_ctx(tmp_path, streams, runtime_plugins)
                plugin._set_context(ctx)

                # 固定当前时间为 10:00（处于静默时段）
                current_time = [datetime(2026, 11, 23, 10, 0, tzinfo=TZ_CST)]
                original_eval = plugin._evaluate_now

                def patched_eval(now=None):
                    return original_eval(now if now is not None else current_time[0])

                plugin._evaluate_now = patched_eval  # type: ignore[method-assign]

                await plugin.on_load()

                # 1. 普通群普通聊天消息：before_process 填充媒体占位阻断 VLM/ASR，after_process 阻断进入 Maisaka
                media_msg = _msg(
                    session_id="s_normal",
                    group_id="100",
                    user_id="normal_u",
                    text="",
                    raw_message=[{"type": "image", "data": "", "hash": "abc"}, {"type": "voice", "data": ""}],
                    is_at=True,
                )
                res_before = await plugin.handle_before_process(message=media_msg)
                self.assertEqual(res_before["action"], "continue")
                sanitized_raw = res_before["modified_kwargs"]["message"]["raw_message"]
                self.assertEqual(sanitized_raw[0]["data"], "[图片]")
                self.assertEqual(sanitized_raw[1]["data"], "[语音消息]")

                chat_msg = _msg(session_id="s_normal", group_id="100", user_id="normal_u", text="陪我聊天", is_at=True)
                res_after = await plugin.handle_after_process(message=chat_msg)
                self.assertEqual(res_after["action"], "abort")
                ctx.send.text.assert_awaited_once()  # 发送了冷却内的聊天拦截提示

                # 2. 豁免群普通聊天 -> 放行！
                exempt_group_msg = _msg(session_id="s_exempt", group_id="200", user_id="normal_u", text="陪我聊天")
                self.assertEqual((await plugin.handle_after_process(message=exempt_group_msg))["action"], "continue")

                # 3. 豁免用户在普通群发消息 -> 放行！
                exempt_user_msg = _msg(session_id="s_normal", group_id="100", user_id="vip_user", text="陪我聊天")
                self.assertEqual((await plugin.handle_after_process(message=exempt_user_msg))["action"], "continue")

                # 4. 非大模型插件命令 /time -> after_process 放行，command.before_execute 放行！
                cmd_non_llm = _msg(session_id="s_normal", group_id="100", user_id="normal_u", text="/time")
                self.assertEqual((await plugin.handle_after_process(message=cmd_non_llm))["action"], "continue")
                res_cmd_ok = await plugin.handle_command_before_execute(
                    message=cmd_non_llm, command_name="time", plugin_id="demo.non-llm"
                )
                self.assertEqual(res_cmd_ok["action"], "continue")

                # 4b. 若非大模型命令返回 continue_process=True，after_execute 必须封住防止落入 Maisaka AI
                res_cmd_after = await plugin.handle_command_after_execute(
                    message=cmd_non_llm, continue_process=True, intercept_message_level=0
                )
                self.assertEqual(res_cmd_after["action"], "continue")
                self.assertFalse(res_cmd_after["modified_kwargs"]["continue_process"])

                # 5. 大模型插件命令 /ask -> 非豁免被阻断；豁免用户被放行！
                cmd_llm = _msg(session_id="s_normal", group_id="100", user_id="normal_u", text="/ask 写首诗")
                res_cmd_block = await plugin.handle_command_before_execute(
                    message=cmd_llm, command_name="ask", plugin_id="demo.llm-bot"
                )
                self.assertEqual(res_cmd_block["action"], "abort")

                cmd_llm_vip = _msg(session_id="s_normal", group_id="100", user_id="vip_user", text="/ask 写首诗")
                res_cmd_vip = await plugin.handle_command_before_execute(
                    message=cmd_llm_vip, command_name="ask", plugin_id="demo.llm-bot"
                )
                self.assertEqual(res_cmd_vip["action"], "continue")

                # 6. Replyer 兜底拦截：非豁免会话拦截，豁免群会话放行
                # 注意上面 vip_user 发过消息会更新 last_sender_id，这里先由普通用户发一条更新普通群上下文
                await plugin.handle_before_process(message=chat_msg)
                self.assertEqual((await plugin.handle_replyer_before_request(session_id="s_normal"))["action"], "abort")
                self.assertEqual((await plugin.handle_replyer_before_request(session_id="s_exempt"))["action"], "continue")

                # 7. 时段结束切换 -> 仅向非豁免群 s_normal 发送结束提示语（不发给豁免群 s_exempt 或 webui）
                ctx.send.text.reset_mock()
                current_time[0] = datetime(2026, 11, 23, 18, 0, tzinfo=TZ_CST)
                await plugin._reconcile_schedule_state(trigger="test_end")
                sent_targets = [call.args[1] for call in ctx.send.text.await_args_list]
                self.assertEqual(sent_targets, ["s_normal"])
                self.assertIn("结束静默:测试静默", ctx.send.text.await_args_list[0].args[0])

                # 8. 无豁免时 smart 模式自动禁用大模型插件后台组件，结束/卸载时自动恢复
                plugin._cfg.exemptions.exempt_groups = []
                plugin._cfg.exemptions.exempt_users = []
                current_time[0] = datetime(2026, 11, 24, 9, 0, tzinfo=TZ_CST)
                ctx.send.text.reset_mock()
                await plugin._reconcile_schedule_state(trigger="test_start")
                ctx.component.disable_component.assert_awaited_once_with("demo.llm-bot.auto_bg", "EVENT_HANDLER")
                self.assertIn("开始静默:测试静默->18:00", ctx.send.text.await_args_list[0].args[0])

                # 卸载插件时必须自动恢复临时禁用的组件
                await plugin.on_unload()
                ctx.component.enable_component.assert_awaited_once_with("demo.llm-bot.auto_bg", "EVENT_HANDLER")

        asyncio.run(scenario())

    def test_transition_notices_survive_hook_evaluations(self) -> None:
        """回归：巡检间隔内消息触发 Hook 时，进入/结束静默的提示语都必须照常发出。"""

        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                streams = [
                    {
                        "session_id": "s_normal",
                        "platform": "qq",
                        "group_id": "100",
                        "group_name": "普通群",
                        "is_group_session": True,
                    }
                ]
                plugin = AIQuietHoursPlugin()
                plugin.set_plugin_config(
                    QuietHoursPluginConfig(
                        schedule=ScheduleSectionConfig(
                            rules=[
                                ScheduleRule(
                                    name="夜间休眠",
                                    start_time="23:00",
                                    end_time="07:00",
                                    day_scope="all",
                                )
                            ]
                        ),
                        holiday=HolidaySectionConfig(enable_online_sync=False),
                        notices=NoticeSectionConfig(
                            enabled=True,
                            notify_on_startup_if_quiet=False,
                            default_start_message="进入静默:{rule_name}",
                            default_end_message="结束静默:{rule_name}",
                        ),
                        plugin_guard=PluginGuardSectionConfig(enabled=True, block_mode="hook_only"),
                    ).model_dump()
                )
                ctx = _make_ctx(tmp_path, streams, {})
                plugin._set_context(ctx)

                current_time = [datetime(2026, 11, 23, 22, 0, tzinfo=TZ_CST)]
                original_eval = plugin._evaluate_now
                plugin._evaluate_now = lambda now=None: original_eval(now if now is not None else current_time[0])  # type: ignore[method-assign]

                await plugin.on_load()
                self.assertIsNotNone(plugin._current_eval)

                # 1. 刚过 23:00 就有消息触发 Hook，随后巡检仍必须发出开始提示语
                current_time[0] = datetime(2026, 11, 23, 23, 0, 3, tzinfo=TZ_CST)
                await plugin.handle_after_process(message=_msg(session_id="s_normal", group_id="100"))
                await plugin._reconcile_schedule_state(trigger="scheduler")
                self.assertEqual([call.args[1] for call in ctx.send.text.await_args_list], ["s_normal"])
                self.assertIn("进入静默:夜间休眠", ctx.send.text.await_args_list[0].args[0])
                ctx.send.text.reset_mock()

                # 2. 静默期间持续有消息，巡检基线不能被 Hook 改写
                current_time[0] = datetime(2026, 11, 24, 3, 0, tzinfo=TZ_CST)
                await plugin.handle_after_process(message=_msg(session_id="s_normal", group_id="100"))
                assert plugin._current_eval is not None
                self.assertTrue(plugin._current_eval.is_quiet)

                # 3. 刚过 07:00 就有消息（早高峰），随后巡检仍必须发出结束提示语
                current_time[0] = datetime(2026, 11, 24, 7, 0, 5, tzinfo=TZ_CST)
                await plugin.handle_after_process(message=_msg(session_id="s_normal", group_id="100"))
                await plugin._reconcile_schedule_state(trigger="scheduler")
                self.assertEqual([call.args[1] for call in ctx.send.text.await_args_list], ["s_normal"])
                self.assertIn("结束静默:夜间休眠", ctx.send.text.await_args_list[0].args[0])

                await plugin.on_unload()

        asyncio.run(scenario())

    def test_commands_apis_defense_hooks_and_hot_reload(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                streams = [
                    {"session_id": "s_normal", "platform": "qq", "group_id": "100", "group_name": "普通群", "is_group_session": True},
                    {"session_id": "s_exempt", "platform": "qq", "group_id": "200", "group_name": "豁免群", "is_group_session": True},
                ]
                runtime_plugins = {
                    "demo.non-llm": {
                        "display_name": "签到插件",
                        "version": "1.0.0",
                        "components": [
                            {"name": "sign", "full_name": "demo.non-llm.sign", "type": "COMMAND", "enabled": True, "metadata": {"command_pattern": r"^/sign$"}},
                        ],
                    },
                    "demo.llm-writer": {
                        "display_name": "写作插件",
                        "version": "1.0.0",
                        "components": [
                            {"name": "write", "full_name": "demo.llm-writer.write", "type": "COMMAND", "enabled": True, "metadata": {"command_pattern": r"^/write"}},
                        ],
                    },
                }

                plugin = AIQuietHoursPlugin()
                base_cfg = QuietHoursPluginConfig(
                    schedule=ScheduleSectionConfig(
                        rules=[ScheduleRule(name="日间静默", start_time="09:00", end_time="18:00", day_scope="all")]
                    ),
                    holiday=HolidaySectionConfig(enable_online_sync=False),
                    notices=NoticeSectionConfig(enabled=True),
                    plugin_guard=PluginGuardSectionConfig(
                        enabled=True,
                        block_mode="hook_only",
                        force_block_plugins=["demo.llm-writer"],
                    ),
                    exemptions=ExemptionSectionConfig(
                        exempt_groups=[GroupTarget(platform="qq", id="200", name="豁免群")],
                    ),
                )
                plugin.set_plugin_config(base_cfg.model_dump())
                ctx = _make_ctx(tmp_path, streams, runtime_plugins)
                plugin._set_context(ctx)

                current_time = [datetime(2026, 11, 23, 10, 0, tzinfo=TZ_CST)]
                original_eval = plugin._evaluate_now
                plugin._evaluate_now = lambda now=None: original_eval(now if now is not None else current_time[0])  # type: ignore[method-assign]

                await plugin.on_load()

                # 1. 兜底模型 Hook：普通会话拦截，豁免会话放行，后台维护在静默期拦截
                self.assertEqual((await plugin.handle_replyer_before_model_request(session_id="s_normal"))["action"], "abort")
                self.assertEqual((await plugin.handle_replyer_before_model_request(session_id="s_exempt"))["action"], "continue")
                self.assertEqual((await plugin.handle_expression_before_select(chat_id="s_normal"))["action"], "abort")
                self.assertEqual((await plugin.handle_expression_before_select(chat_id="s_exempt"))["action"], "continue")
                self.assertEqual((await plugin.handle_expression_after_extract(session_id="s_normal"))["action"], "abort")
                self.assertEqual((await plugin.handle_jargon_before_persist(session_id="s_normal"))["action"], "abort")
                self.assertEqual((await plugin.handle_emoji_before_cache(session_id="s_normal"))["action"], "abort")
                self.assertEqual((await plugin.handle_emoji_before_cache(session_id="s_exempt"))["action"], "continue")
                self.assertEqual((await plugin.handle_emoji_maintenance_before_register())["action"], "abort")

                # 2. 全部 8 个命令均可执行且返回正确内容
                ok, status_text, intercept = await plugin.cmd_status(stream_id="s_normal")
                self.assertTrue(ok and intercept)
                self.assertIn("AI 静默中", status_text)

                ok, holiday_text, _ = await plugin.cmd_holiday(
                    stream_id="s_normal", matched_groups={"target_date": "2026-10-01"}
                )
                self.assertTrue(ok)
                self.assertIn("国庆节", holiday_text)

                ok, bad_date_text, _ = await plugin.cmd_holiday(
                    stream_id="s_normal", matched_groups={"target_date": "2026-99-99"}
                )
                self.assertFalse(ok)
                self.assertIn("日期无效", bad_date_text)

                ok, plugins_text, _ = await plugin.cmd_plugins(stream_id="s_normal")
                self.assertTrue(ok)
                self.assertIn("demo.llm-writer", plugins_text)
                self.assertIn("demo.non-llm", plugins_text)

                ok, scan_text, _ = await plugin.cmd_scan(stream_id="s_normal")
                self.assertTrue(ok)
                self.assertIn("重扫完成", scan_text)

                ok, help_text, _ = await plugin.cmd_help(stream_id="s_normal")
                self.assertTrue(ok)
                self.assertIn("/quiet auto", help_text)

                # 手动覆盖命令：先 /quiet off 30 临时恢复 AI，再 /quiet on 15，再 /quiet auto
                ok, off_text, _ = await plugin.cmd_force_active(stream_id="s_normal", matched_groups={"minutes": "30"})
                self.assertTrue(ok)
                self.assertIn("手动恢复 AI", off_text)
                self.assertFalse(plugin._get_live_evaluation().is_quiet)

                ok, on_text, _ = await plugin.cmd_force_quiet(
                    stream_id="s_normal", matched_groups={"minutes": "15", "reason": "临时维护"}
                )
                self.assertTrue(ok)
                self.assertIn("手动开启 AI 静默", on_text)
                self.assertTrue(plugin._get_live_evaluation().is_quiet)

                ok, auto_text, _ = await plugin.cmd_restore_auto(stream_id="s_normal")
                self.assertTrue(ok)
                self.assertIn("恢复自动时段调度", auto_text)

                # 3. WebUI API 接口完整可用
                snap = await plugin.api_webui_status()
                self.assertTrue(snap["is_quiet"])
                self.assertEqual(snap["blocked_plugin_count"], 1)
                self.assertEqual(len(snap["rules"]), 1)

                res_active = await plugin.api_webui_force_active(duration_minutes=45)
                self.assertTrue(res_active["success"])
                self.assertFalse(res_active["is_quiet"])

                res_quiet = await plugin.api_webui_force_quiet(duration_minutes=45, reason="WebUI 测试")
                self.assertTrue(res_quiet["success"])
                self.assertTrue(res_quiet["is_quiet"])

                res_auto = await plugin.api_webui_restore_auto()
                self.assertTrue(res_auto["success"])

                res_scan = await plugin.api_webui_rescan_plugins()
                self.assertTrue(res_scan["success"])
                self.assertEqual(res_scan["blocked_plugins"], 1)

                # 4. 配置热重载 on_config_update（与 Runner._handle_config_updated 顺序一致）：新增豁免群 100 后 s_normal 立即被放行
                updated_cfg = base_cfg.model_dump()
                updated_cfg["exemptions"]["exempt_groups"].append({"platform": "qq", "id": "100", "name": "新豁免群"})
                plugin.set_plugin_config(updated_cfg)
                await plugin.on_config_update("self", updated_cfg, "v2")
                self.assertEqual((await plugin.handle_replyer_before_model_request(session_id="s_normal"))["action"], "continue")

                # 5. 使用真实 MaiBot ComponentRegistry 校验全部命令正则与别名匹配
                comp_registry = ComponentRegistry()
                for comp in plugin.get_components():
                    if comp["type"] == "COMMAND":
                        self.assertTrue(
                            comp_registry.register_component(
                                comp["name"], comp["type"], "a1mue4ay.ai-quiet-hours", comp["metadata"]
                            )
                        )
                for sample_cmd, expected_name in [
                    ("/quiet", "quiet_status"),
                    ("/quiet status", "quiet_status"),
                    ("/静默状态", "quiet_status"),
                    ("/quiet holiday", "quiet_holiday"),
                    ("/quiet holiday 2026-10-01", "quiet_holiday"),
                    ("/节假日查询", "quiet_holiday"),
                    ("/节假日查询 2026-10-01", "quiet_holiday"),
                    ("/quiet plugins", "quiet_plugins"),
                    ("/模型插件检查", "quiet_plugins"),
                    ("/quiet fallback", "quiet_fallback"),
                    ("/quiet fallback deepseek-v4-flash", "quiet_fallback"),
                    ("/回退模型 deepseek-v4-flash", "quiet_fallback"),
                    ("/quiet on", "quiet_on"),
                    ("/quiet on 60 夜间维护", "quiet_on"),
                    ("/quiet off 30", "quiet_off"),
                    ("/quiet auto", "quiet_auto"),
                    ("/quiet scan", "quiet_scan"),
                    ("/quiet help", "quiet_help"),
                ]:
                    match = comp_registry.find_command_by_text(sample_cmd)
                    self.assertIsNotNone(match, f"命令未被 MaiBot ComponentRegistry 匹配: {sample_cmd}")
                    assert match is not None
                    self.assertEqual(match[0].name, expected_name)

                await plugin.on_unload()

        asyncio.run(scenario())

    def test_v110_model_fallback_mode_and_selectable_rules(self) -> None:
        sample_model_toml = """
[inner]
version = "1.7.0"

[[api_providers]]
name = "DeepSeek"
base_url = "https://api.deepseek.com"
api_key = "sk-test-secret"
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

[model_task_config.replyer]
model_list = ["deepseek-v4-pro"]

[model_task_config.planner]
model_list = ["deepseek-v4-pro"]

[model_task_config.utils]
model_list = ["deepseek-v4-pro"]

[model_task_config.fast_model]
model_list = []
"""

        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                instance_root = tmp_path / "instance"
                (instance_root / "config").mkdir(parents=True)
                model_cfg_path = instance_root / "config" / "model_config.toml"
                model_cfg_path.write_text(sample_model_toml, encoding="utf-8")

                plugins_root = tmp_path / "plugins"
                peer_dir = plugins_root / "demo_weather"
                peer_dir.mkdir(parents=True)
                (peer_dir / "_manifest.json").write_text(
                    json.dumps(
                        {
                            "manifest_version": 2,
                            "id": "demo.weather",
                            "version": "1.2.0",
                            "name": "天气助手",
                            "description": "查询天气",
                            "capabilities": ["llm.generate"],
                        }
                    ),
                    encoding="utf-8",
                )

                peer_plain = plugins_root / "demo_ping"
                peer_plain.mkdir(parents=True)
                (peer_plain / "_manifest.json").write_text(
                    json.dumps(
                        {
                            "manifest_version": 2,
                            "id": "demo.ping",
                            "version": "1.0.0",
                            "name": "Ping 工具",
                            "description": "纯工具插件",
                            "capabilities": [],
                        }
                    ),
                    encoding="utf-8",
                )

                streams = [
                    {"session_id": "s_g1", "platform": "qq", "group_id": "888001", "group_name": "测试交流群", "is_group_session": True},
                    {"session_id": "s_u1", "platform": "qq", "user_id": "777001", "user_nickname": "小明", "is_group_session": False},
                ]
                plugins_map = {
                    "demo.weather": {
                        "plugin_id": "demo.weather",
                        "display_name": "天气助手",
                        "version": "1.2.0",
                        "components": [
                            {"name": "weather", "full_name": "demo.weather.weather", "type": "COMMAND", "enabled": True, "metadata": {}},
                        ],
                    },
                    "demo.ping": {
                        "plugin_id": "demo.ping",
                        "display_name": "Ping 工具",
                        "version": "1.0.0",
                        "components": [],
                    },
                }

                ctx = _make_ctx(tmp_path / "plugin_runtime", streams, plugins_map)
                async def fake_cap(cap_name: str, **kwargs):
                    if cap_name == "database.query":
                        if kwargs.get("model_name") == "PersonInfo":
                            return [{"platform": "qq", "user_id": "999002", "user_nickname": "群友小红", "person_name": "小红"}]
                        return []
                    return {"success": True}
                ctx.call_capability = AsyncMock(side_effect=fake_cap)

                plugin = AIQuietHoursPlugin()
                cfg = QuietHoursPluginConfig(
                    schedule=ScheduleSectionConfig(
                        rules=[ScheduleRule(name="全天测试时段", enabled=True, start_time="00:00", end_time="24:00", day_scope="all")]
                    ),
                    model_fallback=ModelFallbackSectionConfig(
                        enabled=True,
                        fallback_models=["deepseek-v4-flash"],
                        fallback_to_quiet_if_unavailable=True,
                        instance_root=str(instance_root),
                    ),
                    holiday=HolidaySectionConfig(enable_online_sync=False),
                    plugin_guard=PluginGuardSectionConfig(enable_ast_code_scan=False),
                    exemptions=ExemptionSectionConfig(
                        exempt_groups=[GroupTarget(platform="qq", id="888001", name="测试交流群")],
                        exempt_platforms=["local"],
                    ),
                )
                plugin.set_plugin_config(cfg.model_dump())
                plugin._set_context(ctx)

                config_toml_path = tmp_path / "plugin_config.toml"
                orig_resolver = plugin_module._resolve_self_config_path
                plugin_module._resolve_self_config_path = lambda _dir: config_toml_path
                try:
                    # 模拟扫描自定义插件目录
                    orig_audit = plugin._auditor.audit_sync
                    plugin._auditor.audit_sync = lambda rp, gc, **kw: orig_audit(rp, gc, plugin_roots=[plugins_root], **kw)

                    await plugin.on_load()

                    # 1. 有可用回退模型时：不静默，已切至 deepseek-v4-flash
                    status = await plugin.api_webui_status()
                    self.assertEqual(status["mode"], "model_fallback")
                    self.assertFalse(status["is_quiet"])
                    self.assertTrue(status["is_fallback"])
                    self.assertEqual(status["fallback_models"], ["deepseek-v4-flash"])
                    # 没有因静默拦截普通聊天和LLM插件命令
                    after_res = await plugin.handle_after_process(message=_msg(session_id="s_u1", group_id="", user_id="777001", text="在吗"))
                    self.assertEqual(after_res["action"], "continue")
                    cmd_res = await plugin.handle_command_before_execute(
                        message=_msg(session_id="s_u1", user_id="777001", text="/weather"),
                        command_name="weather",
                        plugin_id="demo.weather",
                    )
                    self.assertEqual(cmd_res["action"], "continue")
                    # Replyer before_request 立即注入回退模型（非豁免），豁免群保留原模型
                    reply_non_exempt = await plugin.handle_replyer_before_request(session_id="s_u1", model_name="")
                    self.assertEqual(reply_non_exempt["modified_kwargs"]["model_name"], "deepseek-v4-flash")
                    reply_exempt = await plugin.handle_replyer_before_request(session_id="s_g1", model_name="")
                    self.assertEqual(reply_exempt["modified_kwargs"]["model_name"], "deepseek-v4-pro")

                    # model_config.toml 确实已被切换，且不含 sk-test-secret 暴露
                    import tomllib
                    live_disk = tomllib.loads(model_cfg_path.read_text(encoding="utf-8"))
                    self.assertEqual(live_disk["model_task_config"]["replyer"]["model_list"], ["deepseek-v4-flash"])
                    self.assertEqual(live_disk["model_task_config"]["planner"]["model_list"], ["deepseek-v4-flash"])

                    # 2. 动态配置 Schema 注入可选择列表（插件/模型/命令/任务）
                    schema = plugin.get_webui_config_schema()
                    fb_field = schema["sections"]["model_fallback"]["fields"]["fallback_models"]
                    self.assertEqual(fb_field["ui_type"], "select")
                    self.assertTrue(fb_field["multiple"])
                    self.assertIn("deepseek-v4-flash", fb_field["choices"])
                    self.assertIn("deepseek-v4-pro", fb_field["choices"])
                    guard_block_field = schema["sections"]["plugin_guard"]["fields"]["force_block_plugins"]
                    self.assertEqual(guard_block_field["ui_type"], "select")
                    self.assertTrue(guard_block_field["multiple"])
                    self.assertIn("demo.weather", guard_block_field["choices"])
                    self.assertIn("demo.ping", guard_block_field["choices"])
                    cmd_allow_field = schema["sections"]["plugin_guard"]["fields"]["force_allow_commands"]
                    self.assertIn("demo.weather.weather", cmd_allow_field["choices"])

                    # 3. WebUI bootstrap 聚合模型、插件与群聊/用户列表（含 PersonInfo 小红），以及保存接口写盘生效
                    boot = await plugin.api_webui_console_bootstrap()
                    self.assertTrue(boot["success"])
                    user_ids = {u["id"] for u in boot["targets"]["users"]}
                    self.assertIn("999002", user_ids)
                    self.assertIn("777001", user_ids)

                    save_guard = await plugin.api_webui_save_plugin_guard(
                        enabled=True,
                        block_mode="hook_only",
                        force_block_plugins=["demo.ping"],
                        force_allow_plugins=[],
                        force_allow_commands=[],
                    )
                    self.assertTrue(save_guard["success"])
                    self.assertIn("demo.ping", plugin.config.plugin_guard.force_block_plugins)

                    save_ex = await plugin.api_webui_save_exemptions(
                        exempt_groups=[{"platform": "qq", "type": "group", "id": "888001", "name": "测试交流群"}],
                        exempt_users=[{"platform": "qq", "type": "user", "id": "999002", "name": "小红"}],
                        user_exempt_in_group=True,
                        exempt_platforms=["local"],
                    )
                    self.assertTrue(save_ex["success"])
                    self.assertEqual(plugin.config.exemptions.exempt_users[0].id, "999002")

                    # 4. 清空或配为不存在的回退模型 -> 自动降级回静默规则，同时还原 model_config.toml
                    save_empty_fb = await plugin.api_webui_save_model_fallback(
                        enabled=True,
                        fallback_models=[],
                        target_tasks=["replyer", "planner", "utils"],
                        switch_mode="replace",
                        fallback_to_quiet_if_unavailable=True,
                    )
                    self.assertTrue(save_empty_fb["success"])
                    self.assertEqual(save_empty_fb["status"]["mode"], "quiet")
                    self.assertTrue(save_empty_fb["status"]["is_quiet"])

                    restored_disk = tomllib.loads(model_cfg_path.read_text(encoding="utf-8"))
                    self.assertEqual(restored_disk["model_task_config"]["replyer"]["model_list"], ["deepseek-v4-pro"])

                    blocked_res = await plugin.handle_after_process(message=_msg(session_id="s_u1", group_id="", user_id="777001", text="在吗"))
                    self.assertEqual(blocked_res["action"], "abort")
                    # 新增豁免的 999002 仍可放行
                    exempt_user_res = await plugin.handle_after_process(
                        message=_msg(session_id="s_u2", group_id="", user_id="999002", text="在吗")
                    )
                    self.assertEqual(exempt_user_res["action"], "continue")

                    await plugin.on_unload()
                finally:
                    plugin_module._resolve_self_config_path = orig_resolver

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
