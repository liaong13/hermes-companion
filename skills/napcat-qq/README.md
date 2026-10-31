# NapCat QQ Skill

这是 Hermes Companion 为 `hermes-napcat` 提供的可分发操作 Skill，集中保存通用的接入、权限、触发、媒体、部署、排障和验收流程。它取代此前混在官方 `qq-bot` Skill 中的 NapCat 专项经验，但不包含插件代码、NapCat 服务、QQ 登录状态或运行配置。

## 适用范围

- 安装、升级或排查 `hermes-napcat`。
- 设计 QQ 群准入、成员权限、昵称、触发及公共上下文隔离。
- 排查图片、文件、引用、撤回、慢任务和主动通知。
- 比较源码、Hermes 安装副本、运行配置与网关实际加载版本。
- 组织自动化、运行加载和真实 QQ 客户端分层验收。

腾讯官方 QQ Bot API 使用另一套适配器、身份和平台规则，不属于本 Skill。

## 安装

完整接入或成套升级优先使用 [INSTALL.md](../../INSTALL.md)，安装器会同时分发本 Skill 与 `hermes-companion-setup`，检查并备份既有差异。它们分别负责通道维护和成套安装；已有自定义 Skill 不应直接覆盖。

仅单独安装本 Skill 且目标尚无本地版本时，可从仓库根目录复制到当前 profile：

```sh
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
mkdir -p "$HERMES_HOME/skills/napcat-qq"
cp skills/napcat-qq/SKILL.md skills/napcat-qq/LICENSE "$HERMES_HOME/skills/napcat-qq/"
```

重新开始会话，让 Hermes 发现新 Skill。仓库源码与已安装副本彼此独立；后续更新仓库文件不会自动更新 profile 中的 Skill。

## 配套文档

Skill 只保留执行时需要自动加载的判断和流程，具体配置及实现事实继续以组件文档为准：

- [NapCat 插件说明](../../plugins/hermes-napcat/README.md)
- [开发状态](../../plugins/hermes-napcat/docs/开发状态.md)
- [群权限与昵称](../../plugins/hermes-napcat/docs/群权限与昵称.md)
- [接入与隔离设计](../../plugins/hermes-napcat/docs/接入与隔离设计.md)
- [媒体实现与验收](../../plugins/hermes-napcat/docs/媒体实现与验收.md)
- [测试部署接线](../../plugins/hermes-napcat/docs/测试部署接线.md)
- [安全说明](../../plugins/hermes-napcat/SECURITY.md)

文档中的历史记录不能覆盖当前代码和现场验证；账号、群、路径、凭据、社交 home 与数据库均留在仓库之外。

## 与其他 Skill 的关系

- `napcat-qq`：主要 QQ 通道的开发、部署和运行维护。
- `mobile-qq-fallback`：NapCat 不可用或不适合时，通过 Mobile MCP 操作手机 QQ 备用发送。
- `qq-group-members`：在明确为 QQ 群聊的平台上下文中维护群成员昵称认识记录。

三者分别处理通道维护、备用出站和成员称呼，不互相授予权限，也不代表持续在线。
