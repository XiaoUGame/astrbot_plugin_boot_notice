# astrbot_plugin_boot_notice

> AstrBot 启动 / 关闭时自动向指定会话发送通知，支持“开机补发”与多会话配置。

---

## 📖 简介

本插件用于在 AstrBot 的生命周期节点向指定会话推送消息：

- **启动通知**：可主动推送（等待平台就绪 + 可配置重试次数），也可等目标会话出现新消息时**补发**。
- **失败回退**：主动推送全部失败时，可自动转为“会话来消息后补发”。
- **关闭通知**：支持在**插件停用 / 卸载**时发送，也支持在 **AstrBot 进程正常退出**（如 `Ctrl+C`、`systemctl stop`）时发送。
- **多会话**：可同时向多个 UMO 目标发送，失败的目标会单独保留在待补发队列中。

---

## ✨ 功能特性

| 特性 | 说明 |
|---|---|
| 启动通知 | 支持“主动推送（带重试）”与“首条消息补发”两种模式，可组合使用 |
| 主动重试 | 主动推送默认尝试 3 次、间隔 5 秒，全部失败时记录 WARN 或转为补发 |
| 平台就绪等待 | 发送前等待目标平台就绪（QQ 官方以 WebSocket READY 为准），超时后继续 |
| 补发模式 | 目标会话出现任意新消息时，向所有待补发的会话补发启动消息 |
| 关闭通知 | 插件停用 / 卸载，或 AstrBot 进程正常退出时发送 |
| 多会话 | `sessions` 支持配置多个 UMO，逐个发送；失败的目标单独保留待补发 |
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
| `notify_on_boot` | bool | `false` | 启动后主动推送启动消息（先等待平台就绪，再按重试配置发送） |
| `notify_on_first_message` | bool | `true` | 主动推送失败或未开启时，目标会话来消息后补发 |
| `notify_on_shutdown` | bool | `true` | 插件停用 / 卸载或进程正常退出时发送关闭消息 |
| `boot_retry_count` | int | `3` | 主动推送最大尝试次数 |
| `boot_retry_interval` | int | `5` | 主动推送重试间隔（秒） |
| `platform_ready_timeout` | int | `60` | 等待目标平台就绪的最长秒数，`0` 表示不等待 |
| `sessions` | list | `[]` | 目标会话 UMO 列表，格式 `平台ID:消息类型:会话ID`；留空则不发送任何通知并记录 ERROR |
| `startup_msg` | string | `✅ AstrBot 已启动` | 启动消息内容 |
| `shutdown_msg` | string | `🛑 AstrBot 正在关闭` | 关闭消息内容 |

### 两种开关的组合语义

| `notify_on_boot` | `notify_on_first_message` | 行为 |
|---|---|---|
| ✅ | ❌ | 等待平台就绪后强制主动推送，最多尝试 `boot_retry_count` 次；全部失败记录 WARN |
| ❌ | ✅ | 不主动推送，目标会话任意消息到达时补发（按会话记录，失败的继续等待） |
| ✅ | ✅ | 先尝试主动推送 `boot_retry_count` 次，仍失败的会话转为等待补发 |

### 示例

```json
{
  "notify_on_boot": false,
  "notify_on_first_message": true,
  "notify_on_shutdown": true,
  "boot_retry_count": 3,
  "boot_retry_interval": 5,
  "platform_ready_timeout": 60,
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
AstrBot 加载完成（on_astrbot_loaded）
        │
        ▼
   挂载退出信号监听（notify_on_shutdown 开启时）
        │
        ├── 仅 notify_on_first_message ──► 立即进入待补发状态
        │
        └── notify_on_boot 开启
                    │
                    ▼
             等待 2 秒 + 等待目标平台就绪
             （QQ 官方以 WebSocket READY 为准，最多 platform_ready_timeout 秒）
                    │
                    ▼
             主动推送 startup_msg（默认 3 次，间隔 5 秒）
                    │
                    ├── 全部成功 ──► 结束
                    │
                    ├── 仍有失败 且 notify_on_first_message 开启
                    │             └──► 失败的会话进入待补发
                    │
                    └── 仍有失败 且仅开启 notify_on_boot
                                  └──► 记录 WARN

待补发状态下，任一目标会话出现新消息
        │
        ▼
   向所有待补发会话尝试补发 startup_msg
        │
        ├── 成功 ──► 从待补发队列移除
        └── 失败 ──► 保留，等下一次消息再试
```

### 关闭流程

**途径一：插件被停用 / 卸载**
```
WebUI 停用插件 / 卸载 / 热重载
        │
        ▼
   AstrBot 调用 terminate()
        │
        ▼
  向所有 sessions 发送 shutdown_msg（最多等待 5 秒）
```

**途径二：AstrBot 进程正常退出**
```
用户按下 Ctrl+C 或 systemctl stop
        │
        ▼
   插件拦截 SIGINT / SIGTERM 信号
        │
        ▼
   向事件循环投递发送任务
        │
        ▼
   延迟 2 秒放行框架原有的退出逻辑（给网络发送留出时间）
        │
        ▼
   进程安全退出
```

---

## ⚠️ 限制与注意事项

- **进程退出时会被刻意延迟约 2 秒**：为了确保关闭消息能在事件循环被销毁前成功发出，拦截到退出信号后，插件会强行让进程“续命”2 秒。这是正常现象，请勿视为卡死。
- **无法拦截强制终止**：插件只能拦截 `SIGTERM` / `SIGINT`（正常退出）。若进程被 `SIGKILL`（`kill -9`）、Docker 强制 `docker kill`、宿主机断电等情况，进程直接被杀灭，关闭消息**不会发出**。
- **QQ 官方群主动推送需要“补状态”**：AstrBot 的 `qq_official` 适配器要求本次运行内该会话已有 `scene` / `msg_id`，否则会在发送前直接跳过（日志：`No cached msg_id for session ... skip send_by_session`）。主动推送（启动与关闭通知）时，插件会在发送瞬间临时把群会话标记为 `group`，发送后立即还原，从而走主动推送接口。该实现依赖 AstrBot 内部字段，AstrBot 升级后可能失效；失效时不会崩溃，会自动转为补发或记录 WARN。
- **QQ 群与频道子频道无法从 UMO 区分**：两者都是 `GroupMessage`。强制主动推送时按“QQ 群”处理；若目标实际是频道子频道，发送会失败，随后按补发逻辑等待该会话的消息。
- **主动消息存在平台频率限制**：即使接口允许主动推送，QQ 官方等平台仍可能限制频率，重试全部失败时会记录 WARN。
- **补发按“待补发会话”逐个重试**：一旦有待补发会话，任一目标会话来消息都会触发一轮补发；发送成功的目标会移出队列，失败的继续保留。
- **关闭消息可能部分送达**：发送超时为 5 秒，超时后已发出的消息不会回滚，超时点之后的目标可能收不到。
- **热重载**：插件被重载时会走一次 `terminate()`，若 `notify_on_shutdown` 为 `true`，重载时也会发送关闭消息，属预期行为。

---

## ❓ 常见问题

**Q：日志提示 `sessions 未配置或没有有效 UMO`，但通知没发出去？**
A：说明 `sessions` 为空或只包含无效值。请在 WebUI 配置中填入真实 UMO 后保存。

**Q：启动后没收到消息？**
A：检查以下几点：
1. `sessions` 是否填写了有效 UMO；
2. 若只开启 `notify_on_first_message`，需要**目标会话先发一条消息**才会触发补发；
3. 查看 AstrBot 日志中是否有 `[boot_notice] 等待平台就绪`、`[boot_notice] 发送失败` 或 `启动通知发送失败`；
4. QQ 官方群若主动推送被平台拒绝，插件重试后会把会话放进待补发队列，等该群来消息时再发。

**Q：日志出现 `No cached msg_id for session ... skip send_by_session` 是什么意思？**
A：这是 AstrBot 的 QQ 官方适配器在“没有本次运行内的消息上下文”时跳过主动推送。插件开启 `notify_on_boot` 时会在发送瞬间临时补上群场景来绕过该检查；若仍然出现，说明目标实际不是 QQ 群（例如频道子频道），或 AstrBot 内部结构已变更。

**Q：`notify_on_boot` 和 `notify_on_first_message` 能同时开吗？**
A：可以。同时开启时会先尝试主动推送 `boot_retry_count` 次，仍失败的会话转为等待补发；只开 `notify_on_boot` 时全部失败则记录 WARN。

**Q：重试次数和等待平台就绪的时间能改吗？**
A：可以。`boot_retry_count`（默认 3）、`boot_retry_interval`（默认 5 秒）、`platform_ready_timeout`（默认 60 秒，`0` 表示不等待）。

**Q：关闭 AstrBot 的时候，为什么终端卡了 2 秒才退出？**
A：这是插件在工作。拦截到退出信号后，插件会延迟 2 秒放行退出，利用这 2 秒把关闭消息发送出去，属于正常设计。

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
