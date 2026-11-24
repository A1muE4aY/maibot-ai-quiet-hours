# 工作原理与架构设计

本插件作为纯独立 SDK v2 插件运行，**不修改 MaiBot 主程序源码**。核心由四个模块协同工作：

```text
plugins/ai_quiet_hours/
├── plugin.py               # 生命周期管理、Hook 拦截链、命令、定时巡检与 WebUI API
├── settings.py             # Pydantic v2 配置模型与 WebUI Schema（含目标选择器）
├── schedule_engine.py      # 规则求值、跨夜继承、下次切换计算、模板渲染与豁免匹配
├── holiday_calendar.py     # 四级中国节假日/调休识别与 1900–2100 农历算法兜底
├── llm_plugin_auditor.py   # 大模型插件三层审计器与命令匹配器
├── _manifest.json          # 插件清单与所需宿主能力声明
└── webui.json              # WebUI 声明式控制台页面（v2 Schema）
```

---

## 1. 时段调度与边界通知机制

### 1.1 跨夜规则与日期归属
当某条规则 `start_time > end_time`（例如 `23:00` 至 `07:00`）时，调度引擎将其拆分为两个连续段：
- 当日 `start_time` → 次日 `00:00`：按**当日**的 `day_scope` 与节假日策略判定；
- 次日 `00:00` → `end_time`：继承**前一日**的日期类型判定（例如周五晚的夜间休眠在周六凌晨依然沿用周五的 `workday` 规则归属）。

### 1.2 巡检基线与 Hook 求值分离
- 后台定时任务每 `check_interval_seconds`（默认 15 秒）调用 `_reconcile_schedule_state()` 更新唯一的巡检基线 `_current_eval`，并对比 `was_quiet != is_quiet` 来触发组件同步、状态徽章刷新与开始/结束提示语广播。
- 消息链路上的各个 Hook 调用 `_get_live_evaluation()` 进行即时求值，**刻意不回写 `_current_eval`**，避免边界刚过去（如 07:00:05）有消息进入时提前覆盖基线而漏发结束提示语。

---

## 2. 精准关闭 AI 模型与保留非大模型功能

插件在 MaiBot 消息处理生命周期中注册了以下阻断与放行 Hook：

| Hook | 顺序 (`HookOrder`) | 作用 |
| --- | --- | --- |
| `chat.receive.before_process` | `EARLY` | 静默期为非豁免普通消息中尚未识别的 `image`/`emoji`/`voice` 填充占位文本（`[图片]`/`[表情包]`/`[语音消息]`），阻断预处理 VLM/ASR；命令消息保留原始附件。 |
| `chat.receive.after_process` | `LATE` | 放行所有内置命令与插件命令；对非豁免普通聊天中止后续进入 Maisaka Planner，同时因 `LATE` 顺序确保普通非大模型监听 Hook 仍已执行。 |
| `chat.command.before_execute` | `EARLY` | 在命令执行前根据大模型插件审计结果与豁免名单，精准拦截依赖大模型的插件命令，放行所有非大模型命令。 |
| `chat.command.after_execute` | `LATE` | 当一个非大模型命令声明 `continue_process=True`（不拦截消息）时，在静默期将其强制截断，防止命令落入 Maisaka AI 对话主链。 |
| `maisaka.replyer.before_request` / `before_model_request` | `EARLY` | 兜底阻断 Replyer 生成请求（覆盖主动发言、防抖在途请求与插件 `reply.generate`）。 |
| `expression.select.before_select` / `expression.learn.after_extract` | `EARLY` | 阻断非豁免会话的表达选择与表达学习模型批次。 |
| `jargon.extract.before_persist` | `EARLY` | 跳过非豁免会话的黑话提取持久化。 |
| `emoji.receive.before_cache` / `emoji.maintenance.before_register` | `EARLY` | 跳过非豁免会话的新表情包识图与后台表情包定时识图维护。 |

---

## 3. 大模型插件三层审计与阻断策略

[`LlmPluginAuditor`](../llm_plugin_auditor.py) 会周期性扫描已加载与已安装的插件，按以下三层逻辑判定是否依赖大模型：

1. **宿主运行时组件注册表**（`component.get_all_plugins`）：获取各插件注册的 `COMMAND`、`EVENT_HANDLER`、`HOOK_HANDLER`、`TOOL` 等组件及启用状态。
2. **`_manifest.json` 声明检查**：识别 `capabilities` / `llm_providers` 中包含 `llm.generate`、`llm.generate_with_tools`、`llm.embed`、`llm.transcribe_audio`、`maisaka.reply.generate`、`maisaka.proactive.trigger`、`emoji.register` 的插件（`llm.get_available_models` 默认不计入，可通过配置开启）。
3. **Python 源码 AST 静态分析**：解析插件目录 `*.py` 的抽象语法树（跳过 `tests/`、`data/`、`assets/`、`webui/` 等目录），检测：
   - 宿主内部模型模块（`src.services.llm_service`、`src.services.embedding_service`、`src.llm_models`、`src.plugin_system.apis.llm_api`）；
   - SDK LLM 调用方法（`generate_with_model`、`generate_with_model_with_tools`、`LLMServiceClient`、`LLMOrchestrator` 等）；
   - 常见外部 AI SDK（`openai`、`anthropic`、`google.genai`、`litellm`、`dashscope`、`zhipuai`、`ollama`、`deepseek`、`langchain` 等）；
   - 通用 HTTP 客户端直接请求 `/chat/completions`、`/v1/responses`、`/v1/messages`、`/v1/embeddings`、`/api/generate`、`/api/chat` 等接口。

### 三种阻断模式对比

| 模式 (`plugin_guard.block_mode`) | 行为 | 适用场景 |
| --- | --- | --- |
| `smart`（默认） | 命令通过 `chat.command.before_execute` 按会话/用户豁免精准拦截；当**未配置任何群聊/用户/平台豁免**时，额外在组件注册表临时禁用大模型插件的非网关组件（如后台 `EVENT_HANDLER`、`HOOK_HANDLER`、`TOOL`）。 | 兼顾豁免精度与后台事件防护 |
| `hook_only` | 仅通过 Hook 拦截大模型插件命令与回复生成，不修改组件启用状态。 | 有精细群聊/用户豁免时最安全 |
| `disable_components` | 静默期直接在组件注册表禁用大模型插件的非网关组件（永不禁用 `MESSAGE_GATEWAY`、`HOME_CARD`、`API`），并在静默结束或插件卸载时自动恢复。 | 无豁免、需要最彻底禁用后台组件的场景 |

---

## 4. 节假日识别优先级

[`HolidayCalendar`](../holiday_calendar.py) 按以下优先级判定某一天属于 `holiday`（法定节假日）、`workday_makeup`（调休补班）、`weekend`（周末）或 `workday`（工作日）：

1. **用户自定义配置**：`custom_workdays`（最高）> `custom_holidays`（支持 `YYYY-MM-DD` 与 `MM-DD`）。
2. **在线同步本地缓存**（`data/holiday_cache.json`）：支持 `holiday-cn`、`timor.tech` 及自定义 JSON 接口。
3. **内置官方年表**：内置 **2025–2027 年** 中国国务院放假与调休补班完整日期表。
4. **纯 Python 算法兜底**：基于 1900–2100 年农历压缩数据与清明节气公式，离线推算元旦、春节、清明、劳动节、端午、中秋、国庆基础节日。

---

## 5. 运行时数据与开发测试

### 运行时持久化文件
插件会在自身数据目录（由宿主 `ctx.paths.data_dir` 提供，通常为 `<plugin_dir>/data/`）下读写：
- `holiday_cache.json`：在线同步的年度节假日缓存；
- `runtime_state.json`：管理员通过 `/quiet on` / `/quiet off` 设置的手动覆盖状态（重启后若未过期自动恢复）。

### 本地测试套件
测试位于 [`tests/`](../tests/)，通过 [`tests/_sdk_stub.py`](../tests/_sdk_stub.py) 在无需完整 MaiBot 运行时依赖的环境中运行单元测试，并直接校验 MaiBot v1.4.8 真实 `ManifestValidator`、`load_webui_extension`、`HookSpecRegistry` 与 `ComponentRegistry` 契约：

```bash
python -m compileall -q plugins/ai_quiet_hours
python -m unittest discover -s plugins/ai_quiet_hours/tests -t . -v
```
