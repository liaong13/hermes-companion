---
name: hermes-companion-setup
description: "Install or upgrade the matched Hermes Companion QQ, social and local-memory bundle from its repository; configure and verify the target Hermes profile."
license: MIT
metadata:
  version: 0.1.0
---

# Hermes Companion 接入

用户要求安装或升级 Hermes Companion 时，获取其指定源码，先读仓库根目录 `INSTALL.md`，执行 `python3 scripts/install.py` 的只读计划。源码地址由用户或当前 checkout 提供，不将某个个人账号的仓库地址写成默认值。

- 自动复用目标 profile 现有配置；缺失时集中询问机器人 QQ、主人 QQ、允许群。Token 通过环境变量或 0600 文件提供，不回显、不让用户把密钥放进聊天。
- 用户已要求实际安装后，用相同参数加 `--apply --restart`，让仓库安装器处理配套插件、宿主补丁、Skills、依赖、来源记录、配置、备份与验证。不要另写只复制某个插件的安装流程。
- 普通对话采集需明确选择 `--features local-memory`；仅需本人确认的偏好、经历、约定及多人请求／语义方法时，使用 `--enhance-social`，保留原始采集关闭。原群主动约定联系另需 `--proactive-agreements --idle-groups`。不因“完整安装”自动开放新的数据采集、媒体云调用、其他用户或群。
- 升级保留已停用服务和已有记忆群范围；聊天 ACL 不等于采集范围。扩大采集用明确的 `--memory-groups`，恢复 social 用 `--enable-social`；不要为消除阻挡把旧白名单、停用状态或用户选择全部改成默认。
- 保留原 memory provider。scoped-memory 的完整源码作为本地数据层分发，不代表已启用 provider 或多人长期记忆。
- 看 JSON `status` 和退出码：`connected` 才是运行连接通过；`restart_required` 是已安装但未激活，`blocked` 是未完成。缺 SDK、补丁冲突、未知本地差异或双 worker 布局时查明确阻挡，不绕过。
- 安装后用 `python3 scripts/install.py --check` 复核文件、实际配置、worker 与激活记录；旧 PID connected 不能当作新版已加载。
- 旧安装差异只有在已审阅并有授权时用 `--adopt-existing`；有本轮备份也不授权覆盖其他修改。重启受官方自重启保护，不能用分离进程或定时器绕过。

安装、worker 检查、网关连接和 QQ 客户端实际送达分别报告。不发送未经明确授权的测试 QQ 消息，不导出私人 profile，不自动提交或推送源码。
