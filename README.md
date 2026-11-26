# AI 时段静默守卫 (`a1mue4ay.ai-quiet-hours`)

> 适用于 **MaiBot v1.4.x**（`maibot-plugin-sdk >= 2.20.0`）的定时 AI 时段管控插件。  
> 支持在指定时段**自动切换至 MaiBot 已注册的回退模型继续服务（不进行静默）**；若未配置或无可用回退模型，则按静默规则关闭 AI 聊天与依赖大模型的插件（**完整保留不经过大模型的指令与监听功能**）。

---

## 文档导航

| 文档 | 说明 |
| --- | --- |
| [README.md](README.md) | 功能概览、安装部署、快速上手与命令速查 |
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | 完整配置项参考（6 大分区字段表、日期范围、提示语变量与豁免格式） |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 工作原理（Hook 拦截链、大模型插件三层审计、节假日识别优先级与测试） |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | 常见问题与部署排查（含 `config.toml` 权限报错、提示语未触发等） |
| [CHANGELOG.md](CHANGELOG.md) | 版本更新记录 |

---

## 核心特性

1. **多规则时段控制（支持跨夜与重叠合并）**
   - 支持配置多条静默时段规则（如工作日白天、每晚 `23:00–07:00` 跨夜休眠、仅法定节假日等）。
   - 跨夜时段在次日凌晨自动继承前一日的日期类型判定；多条命中规则自动合并并精确计算下次切换时间。

2. **中国法定节假日与调休补班识别**
   - 四级优先级判定：**自定义节假日/工作日** > **在线同步本地缓存** > **内置 2025–2027 年国务院放假与调休表** > **1900–2100 年纯 Python 农历与清明节气算法兜底**。
   - 规则支持 7 种日期范围：`all`、`workday`、`offday`、`holiday`、`non_holiday`、`weekend`、`weekdays`。

3. **精准关闭大模型消耗，保留非大模型功能**
   - **阻断模型链路**：跳过非豁免入站图片/表情 VLM 与语音 ASR 预处理；在 `LATE` 阶段拦截普通消息进入 Maisaka Planner；兜底拦截 Replyer、表达选择/学习、黑话提取及表情包定时识图维护。
   - **保留非大模型能力**：内置管理命令（`/pm`、`/clear`、`/offline`、`/online`、`/list chat`）、本插件命令、非大模型插件 `@Command` 与非大模型消息监听 Hook 照常运行，并阻止非大模型命令透传触发 Maisaka AI。

4. **已注册回退模型切换（v1.1.0 新增）**
   - 可在 `[model_fallback]` 或单条时段规则中勾选 MaiBot `model_config.toml`（含共享模型目录）已注册的回退模型（如峰时从 Pro 切至 Flash）。
   - 存在可用回退模型时**不静默**：自动备份并切换目标任务模型，同时支持 Replyer 请求零延迟注入；离开时段或卸载时安全恢复原始模型配置，并具备异常重启恢复保护。
   - 未设置回退模型或所选模型均不可用时，自动回退执行静默拦截规则。

5. **同插件/群聊/用户列表可视化选择（v1.1.0 增强）**
   - 自动检测当前同插件及注册命令，在 WebUI「插件配置」表单中以下拉多选展现插件与模型名称，同时在「AI 时段静默守卫控制台」中支持搜索与一键勾选同插件规则、豁免群聊与豁免用户。
   - 仍支持 `smart`（默认）、`hook_only`、`disable_components` 三种阻断策略与单条命令放行，且永不禁用 `MESSAGE_GATEWAY` 网关组件。

6. **时段开始/结束提示语与 WebUI 控制台**
   - 跨越静默边界时主动广播开始/结束提示语（模型回退模式亦支持可选提示语，默认留空不打扰），提供首页卡片（`HomeCard`）、运行状态徽章及完整自定义交互控制台（`/extensions/a1mue4ay.ai-quiet-hours/dashboard`）。

---

## 安装与部署

### 方式一：目录安装（推荐）

将本插件目录完整放置到 MaiBot 的 `plugins/ai_quiet_hours/` 下：

```text
MaiBot/
└── plugins/
    └── ai_quiet_hours/
        ├── _manifest.json
        ├── webui.json
        ├── plugin.py
        └── ...
```

重启 MaiBot 或在 WebUI 插件管理页重载插件后，即可在 WebUI「插件配置」与「AI 时段静默控制台」中进行可视化配置。

### 方式二：ZIP 安装

将插件目录打包为 `.zip`（压缩包根目录直接包含 `_manifest.json` 或单层 `ai_quiet_hours/` 目录均可），通过 MaiBot WebUI 插件管理页上传安装。

> [!IMPORTANT]
> **Linux 部署权限要求（避免 `Permission denied: '.../config.toml'`）**  
> MaiBot 启动加载插件时，会自动向插件目录写入/更新 `config.toml`，本插件也会在插件目录的 `data/` 下写入 `holiday_cache.json` 与 `runtime_state.json`。  
> 如果使用 `root` 上传或解压插件而 MaiBot 以 `admin` 等普通用户运行，会触发 `[Errno 13] Permission denied`。请在宿主机执行：
> ```bash
> sudo chown -R admin:admin /home/admin/MaiBot/plugins/ai_quiet_hours
> chmod -R u+rwX /home/admin/MaiBot/plugins/ai_quiet_hours
> ```
> （请将 `admin:admin` 和路径替换为你的实际运行用户与 MaiBot 安装路径。完整排查见 [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)。）

---

## 常用配置示例

完整默认配置示例见 [`config.example.toml`](config.example.toml)，默认内置规则针对 **DeepSeek 峰时价格避峰**（工作日 `09:00–12:00` 与 `14:00–18:00` 自动静默，法定节假日开放、调休补班照常静默）。以下片段可直接参考修改 `config.toml`（或在 WebUI「插件配置」页面可视化编辑）：

### 1. 避开 DeepSeek 峰时价格（默认配置）

```toml
[[schedule.rules]]
name = "DeepSeek 早高峰"
enabled = true
start_time = "09:00"
end_time = "12:00"
day_scope = "workday"
respect_holiday_skip = true

[[schedule.rules]]
name = "DeepSeek 午高峰"
enabled = true
start_time = "14:00"
end_time = "18:00"
day_scope = "workday"
respect_holiday_skip = true
```

### 2. 每晚跨夜休眠，但法定节假日跳过静默

```toml
[holiday]
enabled = true
skip_quiet_on_holidays = true
treat_makeup_as_workday = true

[[schedule.rules]]
name = "夜间休眠"
enabled = true
start_time = "23:00"
end_time = "07:00"
day_scope = "all"
respect_holiday_skip = true
```

### 3. 仅在法定节假日全天关闭 AI

```toml
[[schedule.rules]]
name = "法定节假日休假"
enabled = true
start_time = "00:00"
end_time = "24:00"
day_scope = "holiday"
```

更多字段与进阶配置请参阅 [docs/CONFIGURATION.md](docs/CONFIGURATION.md)。

---

## 聊天命令（静默期始终可用）

| 命令 | 别名 | 权限 | 说明 |
| --- | --- | --- | --- |
| `/quiet` 或 `/quiet status` | `/静默状态` | 所有人 | 查看当前 AI 状态、今日节假日判定、规则命中情况与下次切换时间 |
| `/quiet holiday [YYYY-MM-DD]` | `/节假日查询` | 所有人 | 查询今天或指定日期的节假日/调休判定及未来 60 天安排 |
| `/quiet help` | - | 所有人 | 查看插件指令帮助 |
| `/quiet plugins` | `/模型插件检查` | 管理员 | 查看所有同插件、大模型依赖审计与当前拦截/放行规则 |
| `/quiet fallback [模型\|off]` | `/回退模型` | 管理员 | 查看 MaiBot 已注册模型列表或手动切换至指定回退模型（不静默） |
| `/quiet scan` | - | 管理员 | 立即重新扫描所有插件的大模型依赖 |
| `/quiet on [分钟数] [原因]` | - | 管理员 | 手动立即开启 AI 静默模式（不填分钟数则持续至 `/quiet auto`） |
| `/quiet off [分钟数]` | - | 管理员 | 手动立即恢复 AI 原模型正常运行 |
| `/quiet auto` | - | 管理员 | 清除手动覆盖，恢复按时段与节假日规则自动调度 |

---

## WebUI 控制台

插件在 WebUI 中注册了以下入口：

- **首页卡片**：展示核心能力摘要，并提供「打开控制台与规则选择器」与「打开插件表单设置」快捷按钮。
- **自定义控制台页面**（`/extensions/a1mue4ay.ai-quiet-hours/dashboard`）：
  - **概览与时段规则**：查看当前模式（正常 / 模型回退 / 静默）、下次切换与快速覆盖操作；
  - **回退模型切换**：可视化勾选已注册模型、目标任务节点与切换策略并直接热保存；
  - **同插件屏蔽选择**：搜索/筛选所有同插件与命令，一键点选「自动策略 / 强制屏蔽 / 豁免放行」；
  - **豁免群聊与用户**：结合活跃聊天流与数据库人物/历史会话档案，直接搜索和批量勾选豁免对象；
  - **节假日与事件日志**：查看未来节假日、调休安排及近期切换/拦截事件。

---

## 本地测试

在独立项目根目录（或部署到 `plugins/ai_quiet_hours` 后在 MaiBot 根目录）执行：

```bash
python -m compileall -q .
python -m unittest discover -s tests -t . -v
```

---

## 许可证

遵循 [GPL-3.0-or-later](_manifest.json) 许可证开源。
