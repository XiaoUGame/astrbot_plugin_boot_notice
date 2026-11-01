# astrbot_plugin_boot_notice

> AstrBot 启动 / 关闭时自动向指定会话发送通知，支持“开机补发”与多会话配置。

---

## 📖 简介

本插件用于在 AstrBot 的生命周期节点向指定会话推送消息：

- **启动通知**：可立即主动发送，也可等目标会话出现新消息时**补发**（推荐，规避部分平台主动消息限制）。
- **关闭通知**：插件停用 / 卸载时发送（依赖 AstrBot 官方 `terminate()` 生命周期钩子）。
- **多会话**：可同时向多个 UMO 目标发送。

---

## ✨ 功能特性

| 特性 | 说明 |
|---|---|
| 启动通知 | 支持“直接主动发送”与“首条消息补发”两种模式 |
| 补发模式 | 目标会话出现任意新消息时，向**所有**配置的目标会话补发启动消息 |
| 关闭通知 | 插件被停用 / 卸载时发送（官方生命周期钩子，非信号拦截） |
| 多会话 | `sessions` 支持配置多个 UMO，逐个发送 |
| 并发安全 | 补发使用 `asyncio.Lock` 保护，避免并发消息导致重复发送 |
| 配置健壮 | `sessions` 为空时记录 ERROR 并跳过发送，不会误发到未知会话 |

---

## 📦 安装

1. 将插件目录放置到 AstrBot 的插件目录下：

   ```
   data/plugins/astrbot_plugin_boot_notice/
   ├── main.py
   ├── metadata.yaml
   └── _conf_schema.json
   ```

2. 重启 AstrBot，或通过 WebUI 重载插件。
3. 进入 **WebUI → 插件管理 → 开关机通知 → 配置**，填写 `sessions` 后保存。

---

## ⚙️ 配置项

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `enable` | bool | `true` | 总开关，关闭后不发送任何通知 |
| `notify_on_boot` | bool | `false` | 启动后立即主动发送启动消息（部分平台发不出去） |
| `notify_on_first_message` | bool | `true` | 目标会话出现新消息时补发；与 `notify_on_boot` 同时开启时**不生效**（`notify_on_boot` 优先） |
| `notify_on_shutdown` | bool | `true` | 插件停用 / 卸载时发送关闭消息（依赖 `terminate()`） |
| `sessions` | list | `[]` | 目标会话 UMO 列表，格式 `平台ID:消息类型:会话ID`；留空则不发送任何通知并记录 ERROR |
| `startup_msg` | string | `✅ AstrBot 已启动` | 启动消息内容 |
| `shutdown_msg` | string | `🛑 AstrBot 正在关闭` | 关闭消息内容 |

### 示例

```json
{
  "enable": true,
  "notify_on_boot": false,
  "notify_on_first_message": true,
  "notify_on_shutdown": true,
  "sessions": [
    "default:GroupMessage:群号或会话哈希",
    "default:FriendMessage:QQ号或会话哈希"
  ],
  "startup_msg": "✅ AstrBot 已启动",
  "shutdown_msg": "🛑 AstrBot 正在关闭"
}
```

---

## 🔑 如何获取 UMO

UMO（Unified Message Origin）是 AstrBot 用于唯一定位会话的字符串，格式为：

```
平台ID:消息类型:会话ID
```

常见获取方式：

1. **在目标会话中发送 `/sid` 命令**：Bot 会直接回复当前会话的 UMO，复制即可。
2. **查看 AstrBot 运行日志**：收到消息时日志中会打印 `unified_msg_origin`。
3. **从已配置的其他插件配置中复制**：若已有插件使用 UMO，可直接复用。
4. 确保目标会话**已经与 Bot 产生过交互**（发过消息），否则部分平台无法主动推送。

> ⚠️ UMO 必须完整准确，包含平台 ID 与消息类型前缀，缺失或错误会导致发送失败。

---

## 🧠 行为说明

### 启动流程

```
AstrBot 加载完成
        │
        ▼
   等待 2 秒（等待平台就绪）
        │
        ├── notify_on_boot = true ─────► 立即向所有 sessions 发送 startup_msg
        │
        └── notify_on_first_message = true
                    │
                    ▼
            进入待补发状态（_pending）
                    │
                    ▼
        任意目标会话出现新消息 ─────► 向所有 sessions 补发 startup_msg
```

### 关闭流程

```
插件被停用 / 卸载
        │
        ▼
   AstrBot 调用 terminate()
        │
        ▼
  向所有 sessions 发送 shutdown_msg（最多等待 5 秒）
```

---

## ⚠️ 限制与注意事项

- **关闭通知不等于进程退出通知**：`terminate()` 只在**插件停用 / 卸载**时触发。若是进程被 `SIGKILL`、容器强制终止、宿主机断电等情况，`terminate()` 没有机会执行，关闭消息**不会发出**。这是 AstrBot 插件 API 的固有边界，插件层面无法绕过。
- **主动消息存在平台限制**：部分平台（如 QQ 官方接口）不允许 Bot 在没有上下文的情况下主动推送，此时 `notify_on_boot` 可能无效，推荐使用 `notify_on_first_message` 补发模式。
- **补发是“全部会话”**：一旦 `_pending` 被触发，会向 `sessions` 中的**每一个**目标发送启动消息，而不是只回复触发的那一个会话。
- **关闭消息可能部分送达**：`terminate()` 中发送超时为 5 秒，超时后已发出的消息不会回滚，超时点之后的目标可能收不到。
- **热重载**：插件被重载时会走一次 `terminate()`，若 `notify_on_shutdown` 为 `true`，重载时也会发送关闭消息，属预期行为。

---

## ❓ 常见问题

**Q：日志提示 `sessions 未配置或没有有效 UMO`，但通知没发出去？**
A：说明 `sessions` 为空或只包含无效值。请在 WebUI 配置中填入真实 UMO 后保存。

**Q：启动后没收到消息？**
A：检查以下几点：
1. `enable` 是否为 `true`；
2. `sessions` 是否填写了有效 UMO；
3. 若使用 `notify_on_first_message`，需要**目标会话先发一条消息**才会触发补发；
4. 查看 AstrBot 日志中是否有 `[boot_notice] 发送失败` 记录。

**Q：`notify_on_boot` 和 `notify_on_first_message` 能同时开吗？**
A：可以同时勾选，但**只有 `notify_on_boot` 生效**，`notify_on_first_message` 本次运行会被忽略。启动日志中会打印一条 warning 提示。

**Q：插件重载时会发关闭消息，正常吗？**
A：正常。重载等价于“停用 + 启用”，`terminate()` 会被调用。若不需要，可将 `notify_on_shutdown` 设为 `false`。

---

## 📁 目录结构

```
astrbot_plugin_boot_notice/
├── main.py            # 插件主逻辑
├── metadata.yaml      # 插件元信息
├── _conf_schema.json  # 配置项 Schema（WebUI 据此生成配置界面）
└── README.md          # 本文件
```

---

## 👤 作者

- Deepseek, XiaoU

---

## 📄 许可

本项目采用 AGPL-3.0 许可证开源，详见仓库根目录 `LICENSE` 文件。
