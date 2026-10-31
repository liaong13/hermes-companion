# Mobile QQ Fallback Skill

这是 Hermes Companion 当前提供的第一个可分发 Skill，用于在 NapCat 不可用、不适合或使用者明确指定手机路径时，通过 [Mobile MCP](https://github.com/mobile-next/mobile-mcp) 操作已经登录 QQ 的移动设备发送私聊或群消息。

Skill 只提供操作流程和验证边界，不包含 Mobile MCP、QQ、设备驱动、账号、联系人或登录状态。Mobile MCP 是独立的 Apache-2.0 第三方项目，不因本目录采用 MIT 而改变其许可证。

## 前提

- 已安装 Hermes Agent，并具备 MCP 支持。
- 已安装 Node.js 20 或更高版本以及 `npx`（以当前固定的 Mobile MCP `1.0.1` 包元数据为准）。
- Android 设备已开启所需调试授权，或已准备 Mobile MCP 支持的模拟器、iOS 设备或云设备。
- QQ 已由使用者在设备上完成登录；本 Skill 不处理密码、验证码、支付或二次验证。
- 当前任务已经明确授权消息目标和内容。

## 安装 Skill

复制整个目录到当前 Hermes profile 的 Skills 目录；`HERMES_HOME` 未设置时通常为 `~/.hermes`：

```sh
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
mkdir -p "$HERMES_HOME/skills/mobile-qq-fallback"
cp SKILL.md LICENSE "$HERMES_HOME/skills/mobile-qq-fallback/"
```

重新开始会话，让 Hermes 发现新 Skill。源码目录与安装目录应分开；修改仓库文件不会自动更新已安装副本。

## 接入 Mobile MCP

先查看当前 Hermes 版本支持的参数，再通过 CLI 添加，避免手工改写 `config.yaml`：

```sh
hermes mcp add --help
hermes mcp add mobile-mcp --command npx --args -y @mobilenext/mobile-mcp@1.0.1
hermes mcp list
hermes mcp test mobile-mcp
```

这里固定了编写本文时核对的 `1.0.1`，避免 `@latest` 在无人复核时改变行为。升级前检查 [Mobile MCP 发布记录](https://github.com/mobile-next/mobile-mcp/releases)、许可证和工具变化，然后重新执行设备及 QQ 发送验收。

Hermes 添加 MCP 时会展示发现的工具，应只启用实际需要的设备读取、启动应用、元素读取、截图、点击、滑动和输入等工具。配置完成后新开会话，或在支持的会话中运行 `/reload-mcp`；发现工具不等于设备已连接。

## 验证层级

1. `hermes mcp list` 中存在并启用服务。
2. `hermes mcp test mobile-mcp` 能建立协议连接并发现工具。
3. 设备列表返回目标设备且状态在线。
4. QQ 能启动，前台应用和元素可读取。
5. 在明确授权的测试联系人或测试群中，发送文字后能从正确会话回读完整气泡。

前四层不能替代第五层。一次真实发送也只证明该设备、账号、QQ 版本和当时连接下的具体场景成功，不证明持续收信或永久可用。

## 与 NapCat 的关系

NapCat 是持续连接的 QQ 消息通道，适合接收事件、自动回复和独立通知；本 Skill 是交互式备用出站路径。两者不共享登录状态、回执语义或在线保证，也不应在不确定发送结果时自动相互重试，以免产生重复消息。
