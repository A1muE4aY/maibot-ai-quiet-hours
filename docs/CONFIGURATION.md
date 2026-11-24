# 完整配置参考

本插件配置模型定义在 [`settings.py`](../settings.py)，共包含 6 个配置分区：`[plugin]`、`[schedule]`、`[holiday]`、`[notices]`、`[plugin_guard]`、`[exemptions]`。  
在 MaiBot WebUI「插件配置」页修改后支持热重载（`on_config_update`），无需重启即可生效。

---

## 1. `[plugin]` 基础设置

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `enabled` | `bool` | `true` | 插件总开关；关闭后不再拦截任何 AI 聊天或模型调用，并恢复被临时禁用的组件。 |
| `config_version` | `string` | `"1.0.0"` | 内部配置版本号（WebUI 中隐藏）。 |

---

## 2. `[schedule]` 时段调度与模型阻断

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `timezone` | `string` | `""` | 时区标识（如 `"Asia/Shanghai"`、`"UTC+8"`、`"UTC-5"`）；留空使用系统本地时区。 |
| `check_interval_seconds` | `int` | `15` | 后台巡检静默状态边界切换的周期（秒），范围 `5–300`。 |
| `block_media_ai` | `bool` | `true` | 静默期对非豁免普通消息中的未识别图片/表情/语音填充占位文本，跳过 VLM/ASR 预处理模型调用。 |
| `block_background_emoji_maintenance` | `bool` | `true` | 静默期暂停后台定时表情包维护的视觉模型注册任务。 |
| `rules` | `list[ScheduleRule]` | 见下方 | 静默时段规则列表；命中任意一条已启用规则即进入 AI 静默状态。 |

### `[[schedule.rules]]` 单条规则字段

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `name` | `string` | `"夜间休眠"` | 规则名称（1–64 字符），用于状态展示与提示语 `{rule_name}`。 |
| `enabled` | `bool` | `true` | 是否启用本条规则。 |
| `start_time` | `string` | `"23:00"` | 开始时间，格式 `HH:MM`（`00:00`–`23:59`；不支持 `24:00`）。当 `start_time > end_time` 时自动按跨夜处理。 |
| `end_time` | `string` | `"07:00"` | 结束时间，格式 `HH:MM`（支持 `24:00` 表示当日结束）。 |
| `day_scope` | `string` | `"all"` | 适用日期类型，可选值见下表。 |
| `weekdays` | `list[int]` | `[1, 2, 3, 4, 5]` | 仅在 `day_scope = "weekdays"` 时生效；`1` = 周一 … `7` = 周日。 |
| `respect_holiday_skip` | `bool` | `true` | 是否服从 `[holiday].skip_quiet_on_holidays`；当 `day_scope = "holiday"` 时自动忽略此项。 |
| `notify_on_start` | `bool` | `true` | 进入该规则对应的静默时段时是否发送开始提示语。 |
| `notify_on_end` | `bool` | `true` | 离开该规则对应的静默时段时是否发送结束提示语。 |
| `start_message` | `string` | `""` | 该规则专属开始提示语；留空使用 `[notices].default_start_message`。 |
| `end_message` | `string` | `""` | 该规则专属结束提示语；留空使用 `[notices].default_end_message`。 |

### `day_scope` 可选值说明

| 值 | 含义 | 与节假日/调休关系 |
| --- | --- | --- |
| `all` | 每天 | 每天匹配；若开启 `skip_quiet_on_holidays` 且规则 `respect_holiday_skip = true`，则在法定节假日跳过。 |
| `workday` | 工作日 | 包含普通周一至周五（排除法定节假日）及调休补班周末（受 `treat_makeup_as_workday` 控制）。 |
| `offday` | 休息日 | 包含普通周末（排除补班）及法定节假日。 |
| `holiday` | 仅法定节假日 | 仅匹配法定节假日（含自定义节假日）；不受 `skip_quiet_on_holidays` 跳过。 |
| `non_holiday` | 非法定节假日 | 除法定节假日外的所有日期。 |
| `weekend` | 普通周末 | 周六、周日，且在启用调休识别时排除补班日。 |
| `weekdays` | 自定义星期几 | 匹配 `weekdays` 列表指定的星期（`1`–`7`）。 |

---

## 3. `[holiday]` 节假日与调休识别

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `enabled` | `bool` | `true` | 是否启用节假日与调休补班识别；关闭后仅按星期几区分工作日/周末。 |
| `skip_quiet_on_holidays` | `bool` | `false` | 开启后，在法定节假日自动跳过标记了 `respect_holiday_skip = true` 的静默规则。 |
| `treat_makeup_as_workday` | `bool` | `true` | 是否将国务院调休补班周末识别为工作日（`workday`）。 |
| `enable_online_sync` | `bool` | `true` | 是否自动在线拉取当年与次年节假日 JSON 并缓存到 `data/holiday_cache.json`。 |
| `sync_timeout_seconds` | `float` | `8.0` | 在线同步 HTTP 请求超时时间（秒），范围 `2.0–30.0`。 |
| `custom_api_url` | `string` | `""` | 自定义节假日 JSON 接口模板（需含 `{year}` 占位符）；留空依次尝试 `holiday-cn` 与 `timor.tech`。 |
| `custom_holidays` | `list[string]` | `[]` | 自定义节假日，支持 `YYYY-MM-DD`（单日）或 `MM-DD`（每年循环），优先级最高。 |
| `custom_workdays` | `list[string]` | `[]` | 强制视为工作日的日期（`YYYY-MM-DD`），优先级最高，可覆盖周末或法定节假日。 |

---

## 4. `[notices]` 提示语与通知

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `enabled` | `bool` | `true` | 是否在跨越静默边界时主动发送开始/结束提示语。 |
| `notify_on_startup_if_quiet` | `bool` | `false` | 机器人启动时若已处于静默期，是否补发一次开始提示语（默认关闭以防重启刷屏）。 |
| `default_start_message` | `string` | 见源码默认值 | 默认开始提示语模板。 |
| `default_end_message` | `string` | 见源码默认值 | 默认结束提示语模板。 |
| `target_mode` | `string` | `"all_non_exempt_groups"` | 通知范围：`all_non_exempt_groups`（非豁免群聊）、`all_non_exempt_streams`（非豁免群聊+私聊）、`configured_only`（仅下方指定目标）。 |
| `target_groups` | `list[GroupTarget]` | `[]` | 显式指定接收通知的群聊；在 `configured_only` 下仅发给这些群，在其它模式下与非豁免会话合并去重。 |
| `target_users` | `list[UserTarget]` | `[]` | 显式指定接收通知的用户私聊。 |
| `exclude_platforms` | `list[string]` | `["local", "webui", "internal"]` | 自动广播通知时跳过的平台标识列表。 |
| `blocked_chat_reply` | `string` | `""` | 静默期普通聊天被拦截时的可选回复文案；**留空则静默不回复**。 |
| `only_reply_chat_when_mentioned_or_private` | `bool` | `true` | 配置了 `blocked_chat_reply` 后，群聊中仅在被 @ 或提及时才回复拦截提示。 |
| `blocked_reply_cooldown_seconds` | `int` | `300` | 同一会话发送 `blocked_chat_reply` 的冷却时间（秒），范围 `0–86400`；`0` 表示不冷却。 |
| `blocked_plugin_reply` | `string` | 见源码默认值 | 静默期阻断大模型插件命令时回复的提示语；留空则静默阻断。 |

### 提示语模板变量

模板渲染采用安全字典替换（未知占位符将原样保留，不会报错）：

| 变量 | 说明 | 适用场景 |
| --- | --- | --- |
| `{rule_name}` | 命中的静默规则名称（结束通知中自动取上一静默期规则名） | 开始/结束提示、聊天/插件拦截提示 |
| `{start_time}` | 规则开始时间（或手动覆盖起始时间） | 同上 |
| `{end_time}` | 规则结束时间（或预计恢复时间） | 同上 |
| `{now}` | 当前完整时间（`YYYY-MM-DD HH:MM`） | 同上 |
| `{time}` | 当前时刻（`HH:MM`） | 同上 |
| `{date}` | 当前日期（`YYYY-MM-DD`） | 同上 |
| `{day_type}` | 今日日期类型标签（如 `工作日`、`周末`、`法定节假日`、`调休补班`） | 同上 |
| `{holiday_name}` | 节假日或调休说明名称（非节假日为空字符串） | 同上 |
| `{reason}` | 当前状态判定原因说明 | 同上 |
| `{plugin_id}` | 被阻断插件的 ID | 仅 `blocked_plugin_reply` |
| `{plugin_name}` | 被阻断插件的显示名称 | 仅 `blocked_plugin_reply` |
| `{command_name}` | 被阻断的命令名称 | 仅 `blocked_plugin_reply` |
| `{reasons}` | 判定为大模型插件的依据摘要 | 仅 `blocked_plugin_reply` |

---

## 5. `[plugin_guard]` 大模型插件守卫

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `enabled` | `bool` | `true` | 是否在静默期自动检测并阻断依赖大模型的插件。 |
| `block_mode` | `string` | `"smart"` | 阻断策略：`smart` / `hook_only` / `disable_components`，详见 [ARCHITECTURE.md](ARCHITECTURE.md)。 |
| `enable_ast_code_scan` | `bool` | `true` | 除扫描 `_manifest.json` 外，是否对插件源码执行 Python AST 静态分析。 |
| `treat_model_list_capability_as_llm` | `bool` | `false` | 是否将仅声明 `llm.get_available_models`（只读模型列表）的插件也判定为依赖大模型。 |
| `rescan_interval_seconds` | `int` | `300` | 自动重扫已加载与已安装插件的周期（秒），范围 `30–3600`。 |
| `force_block_plugins` | `list[string]` | `[]` | 强制判定为大模型插件并在静默期阻断的插件 ID 列表。 |
| `force_allow_plugins` | `list[string]` | `[]` | 强制豁免、静默期始终放行的插件 ID 列表。 |
| `force_allow_commands` | `list[string]` | `[]` | 静默期始终放行的单个命令，支持 `plugin_id.command_name` 或 `command_name`。 |

---

## 6. `[exemptions]` 豁免群聊与用户

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `exempt_groups` | `list[GroupTarget]` | `[]` | 豁免群聊列表；静默期内可正常 AI 聊天与使用大模型插件，且默认不接收静默广播。 |
| `exempt_users` | `list[UserTarget]` | `[]` | 豁免用户列表；静默期内不受 AI 关闭与大模型插件阻断限制。 |
| `user_exempt_in_group` | `bool` | `true` | 豁免用户在**非豁免群聊**中发言或执行大模型命令时，是否同样放行。 |
| `exempt_platforms` | `list[string]` | `["local", "webui", "internal"]` | 始终豁免的平台标识列表（留空则所有平台一并受时段限制）。 |

### 目标配置写法（WebUI 与 `config.toml`）

在 WebUI 中，目标字段（`target_groups`、`target_users`、`exempt_groups`、`exempt_users`）直接渲染为 **目标选择器**（`x-widget: target`），支持按名称搜索多选。

若手动编辑 `config.toml`，支持以下三种等价写法：

```toml
[exemptions]
# 写法 1：纯 ID（匹配任意平台）
exempt_groups = ["123456789"]

# 写法 2：platform:id
exempt_users = ["qq:987654321"]

# 写法 3：完整结构化对象（WebUI 保存后生成的格式）
[[exemptions.exempt_groups]]
platform = "qq"
type = "group"
id = "123456789"
name = "测试管理群"
```
