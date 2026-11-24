# 常见问题与排查指南

## 1. 启动失败：`[Errno 13] Permission denied: '.../config.toml'`

### 现象
在 WebUI 插件加载失败详情中看到类似报错：
```text
[Errno 13] Permission denied: '/home/admin/MaiBot/plugins/ai_quiet_hours/config.toml'
```

### 原因
MaiBot 插件运行时（`PluginRunner._save_plugin_config`）在启动插件时会自动向插件目录写入或更新 `config.toml`；同时本插件也会在插件目录的 `data/` 子目录下持久化 `holiday_cache.json` 与 `runtime_state.json`。

在 Linux 服务器上，如果使用 `root`、`sudo` 或高权限 SFTP 账户上传/解压插件目录，而 MaiBot 进程是以普通用户（例如 `admin`）启动的，进程将没有权限创建或修改该目录下的 `config.toml` 与 `data/`。

### 解决方法
在服务器上将插件目录的所有权和读写执行权限授予运行 MaiBot 的用户（以 `admin` 为例）：

```bash
sudo chown -R admin:admin /home/admin/MaiBot/plugins/ai_quiet_hours
chmod -R u+rwX /home/admin/MaiBot/plugins/ai_quiet_hours
```

> [!TIP]
> 如果你手动重命名过目录（例如拼写成了 `ai_quite_hours`），建议顺便统一改为 `ai_quiet_hours`，随后重启或重载插件即可。

---

## 2. 静默开始或结束时没有收到群提示语

请按以下顺序逐项检查：

1. **确认通知开关已开启**：`notices.enabled = true`，且对应规则的 `notify_on_start` / `notify_on_end` 为 `true`。
2. **检查 `notices.target_mode` 与平台过滤**：
   - 默认 `all_non_exempt_groups` 仅广播给已知的非豁免群聊，不会发给私聊，也不会发给已在 `exemptions.exempt_groups` 中豁免的群聊；
   - 默认 `notices.exclude_platforms` 排除了 `["local", "webui", "internal"]`；
   - 若群聊历史尚未被适配器加载到当前会话缓存，可先在群内触发一次消息或将该群显式加入 `notices.target_groups`，或使用 `target_mode = "configured_only"`。
3. **重启时是否处于静默中**：`notices.notify_on_startup_if_quiet` 默认为 `false`（防止重启刷屏），若希望重启时在静默期也补发通知，请手动开启该选项。

---

## 3. 为什么我在 WebUI 聊天室 / 本地控制台还能正常对话？

因为 `[exemptions].exempt_platforms` 默认包含 `["local", "webui", "internal"]`，确保管理员在本地控制台与 WebUI 调试对话不受静默时段锁定。如需连同这些平台一并限制，可在配置中清空 `exempt_platforms`。

---

## 4. 某个第三方大模型插件在静默期没有被拦截（或被误判）

1. 先执行管理员命令 `/quiet plugins`（或 `/模型插件检查`）或在 WebUI「大模型插件审计」页签查看该插件的判定结果与检测依据。
2. 若新安装了插件，执行 `/quiet scan` 或在 WebUI 点击「重新扫描大模型插件」。
3. 若插件使用了非标准方式调用外部模型导致未被静态检出，将其插件 ID 加入 `[plugin_guard].force_block_plugins`。
4. 若某纯工具插件被误判（例如仅读取配置或包含特定注释/模块导入），将其插件 ID 加入 `[plugin_guard].force_allow_plugins`，或将特定命令加入 `force_allow_commands`。

---

## 5. 节假日在线同步失败会影响使用吗？

不会。当离线或公开接口不可达时，插件会按顺序自动回退到：
1. 本地已缓存的 `data/holiday_cache.json`；
2. 内置 **2025–2027 年** 国务院法定节假日与调休补班表；
3. 1900–2100 年农历与清明节气算法。

如需临时补充/覆盖特定日期，可直接在 `[holiday].custom_holidays`（支持 `YYYY-MM-DD` 与 `MM-DD`）或 `custom_workdays`（`YYYY-MM-DD`）中添加。
