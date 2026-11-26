# 更新日志

## 1.1.0

### 新增与优化
- **支持切换至 MaiBot 已注册回退模型（不静默）**：新增 `[model_fallback]` 配置分区及按规则 `mode` / `fallback_models` 覆盖；时段命中时若已配置可用回退模型，自动切换任务模型并保持对话与插件正常响应（`is_silencing = false`），离开时段、卸载或异常重启自动还原原 `model_config.toml`；若未配置或无可用回退模型，则自动按静默规则执行拦截。
- **插件屏蔽/放行列表可视化选择**：覆盖 `get_webui_config_schema`，向 `force_block_plugins`、`force_allow_plugins`、`force_allow_commands`、`fallback_models`、`target_tasks` 动态注入当前已检测同插件、命令及 MaiBot 已注册模型多选下拉选项。
- **全新 WebUI 自定义交互控制台**：将 [`webui.json`](webui.json) 升级为自定义页面（[`webui/console.html`](webui/console.html)），集中提供“回退模型勾选”“同插件一键三态切换（自动/强制屏蔽/豁免放行）”“已发现群聊与已知用户全量勾选”并可直接热更新写入 `config.toml`。
- **新增命令与工程规范**：新增 `/quiet fallback [模型名|off]`（别名 `/回退模型`）命令；完善独立项目工程结构与 `scripts/archive_release.py` 版本归档工具。

### 错误修正
- 修正默认规则“DeepSeek 早高峰”的开始时间配置错误，将默认 `start_time` 由 `08:00` 更正为 `09:00`（`09:00–12:00`，与[`settings.py`](settings.py)、[`config.example.toml`](config.example.toml) 及文档保持一致）。

## 1.0.0

### 主要功能
- 新增多规则 AI 静默时段调度，支持跨夜时段、重叠合并与下次状态切换计算。
- 新增中国法定节假日与调休补班识别引擎（内置 2025–2027 年表、1900–2100 农历算法兜底、自动在线同步缓存及自定义节假日/工作日覆盖）。
- 新增时段开始/结束提示语广播与模板变量渲染，支持全局与单规则自定义文案。
- 新增基于 Manifest 能力声明与 Python AST 源码静态分析的大模型插件检查与阻断机制，同时完整保留非大模型指令与基础功能。
- 新增豁免群聊、豁免用户与豁免平台配置，支持 WebUI 原生目标选择器与声明式控制台页面。
