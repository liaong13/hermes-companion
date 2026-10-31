# 给 agent 的完整接入入口

本页适用于用户说“安装这个仓库”“接入 Hermes Companion”并提供仓库链接的场景。当前自动接入支持 **Linux 上已有的 Hermes 源码安装和 NapCat / OneBot 11 正向 WebSocket**，不负责代登录模型账号或 QQ，不自动部署 NapCat 容器。

当前四组件版本、宿主合同、三类安装问题与已执行验证见[安装修复与统一接入验收](docs/安装修复与统一接入验收.md)。组件默认配置和本安装方案的首次默认值分别说明，不能把安装成功当作可选功能或真实 QQ 行为已验收。

## 最短流程

先将 `COMPANION_REPOSITORY_URL` 设置为你选择的源码仓库 URL，再克隆到源码目录，避免克隆进目标 Hermes profile：

```sh
git clone "$COMPANION_REPOSITORY_URL"
cd hermes-companion
python3 scripts/install.py
```

已有 NapCat 配置和凭据会自动复用；缺少内容时，agent 集中向用户询问机器人 QQ、主人 QQ 和允许群。名称可选，默认“伙伴”。Token 请用户设为 `NAPCAT_ACCESS_TOKEN` 环境变量，或存入权限为 0600 的文件，通过 `--token-file` 传路径；**不接受明文 token 命令参数，也不要求用户在聊天中公开 token**。

首次安装示例中的号码为合成占位值，必须替换。`--groups` 为逗号分隔的明确群号，表示允许这些群的真实成员交流：

```sh
python3 scripts/install.py --self-id 100000001 --owner-id 100000002 \
  --groups 100000003 --name 伙伴 --token-file /path/to/napcat-token
```

计划没有阻挡后，用户已要求安装的情况下执行同样参数并加：

```sh
python3 scripts/install.py --apply --restart
```

首次显式提供的 ID、群号、名称或 token 文件参数在 apply 时同样带上；复用已有配置时只需上面这一条命令。agent 应检查 JSON 的 `status`，不能只看命令完成或文件存在。退出码 `0` 表示计划成功或激活后已连接，`2` 表示阻挡／失败，`3` 表示文件配置已安装但尚需激活。

安装后可用 `python3 scripts/install.py --check` 重新检查配套文件、worker 和当前网关；它不修改配置或发送消息。

## 自动完成什么

1. 从已安装 Hermes launcher 发现真实宿主，再读取 PM 选中的解释器，不猜旧 `venv`、不向系统 Python 安装依赖。
2. 检查 SDK、Python、bubblewrap、对话模型配置、账号／群、回环 WS、凭据存在性、专用 worker 目录和已有安装差异。
3. 在临时覆盖层组合、检查公共群权限、公开风格、安静接话等宿主补丁；需要本地交流上下文时追加相应增量。已有补丁不会重复应用；不能无冲突匹配的宿主明确阻挡，保留原文件。
4. 同版安装 `hermes-napcat`、`hermes-social`、`hermes-life-memory` 和完整 `hermes-scoped-memory` 数据层；安装 `napcat-qq` 与 `hermes-companion-setup`，social 的 namespaced Skill 随插件注册。
5. 将四个来源记录绑定到实际 Git origin 的对应子目录和当前 revision，剔除 URL 中的认证信息，保留固定版本策略。`companion-install.json` 另外记录实际文件哈希和源码是否含未提交修改。后续成套升级继续使用本入口，避免只更新某个插件造成合同漂移。
6. 备份本次精确目标，通过 Hermes CLI 合并配置、通过宿主凭据接口保存 token；使用 Hermes PM 准备 Python 依赖，不调用原始 pip 或 uv。
7. 检查真实隔离 worker，核对配置和安装文件读回；按 `--restart` 使用官方 gateway 命令启动／重载，并核对目标 home 的 NapCat 连接状态。全程不发送测试 QQ 消息。

配置只接入指定群、主人私聊和公共群工具路径。既有 SOUL、MEMORY、USER、聊天历史、Mem0 主体及其他插件配置不会被导出或替换；`memory.provider` 保留。scoped-memory 在这里是 life-memory 的数据层，不是第二个被激活的 memory provider。

默认关闭 NapCat 的宿主启动通知：它没有当前消息回复许可，与 strict 通道合同不兼容。正常回复、可选接话及经过独立授权的联系各自保留许可检查。发送拒绝或未知送达不进行纯文本重发。

## 记忆功能选项

首次基本接入提供自然接话和本人明确确认的人物候选，不自动采集普通对话。升级且未指定功能选项时保留已有选择。

已有记忆群范围、life-memory/social 停用状态、群成员准入及显式参与范围同样保留。扩大聊天白名单不会自动扩大原文采集；用 `--memory-groups` 明确调整记忆群，恢复已停用 social 用 `--enable-social`，改变成员准入用 `--group-admission`。需要把旧的参与测试范围迁移为跟随通道白名单时，显式使用 `--follow-allowed-groups`。`--features` 是显式选择记忆功能并启用 life-memory；它不会代替这些独立的范围和社交选择。

若旧安装通过 `plugins.disabled` 停用 life-memory 或 social，成套登记会把该停用选择保留到各自功能开关；显式恢复仍使用上述选项，不因插件重新登记而恢复活动。

```sh
# 显式启用本群对话记录、规则候选、共同经历与本人核对的约定
python3 scripts/install.py --features local-memory --apply --restart

# 仅在另行授权原群主动联系时，允许本人确认的到期约定提起一次
python3 scripts/install.py --features local-memory --idle-groups 100000003 \
  --proactive-agreements --apply --restart
```

本地记忆使用 SQLite 与规则，不增加记忆 API。正常聊天仍使用用户已配置的 Hermes 模型。原始对话记录默认保留 7 天，资料及约定仍按本人确认、当前来源和依赖撤销规则处理；不是完整语义理解或删除外部模型记录的承诺。`--idle-groups` 只授予所列群的空闲联系范围，不复制日常参与白名单。

图片、音频、文件解析、Mobile MCP、MoviePilot 和实验性 scoped-memory provider 各有额外前提。已有媒体配置会保留，本入口不会为了“完整”自动开放云端媒体解析、迁移私人记忆、安装所有外部服务或启用另一个 provider。

## 迁移、失败与恢复

- 旧版本来源记录不对、但安装文件与源码一致时可以直接接管为正确来源；不同于已记录哈希的本地改动默认阻挡。审阅计划后可用 `--adopt-existing` 备份并接管明确目标；不会删除额外文件。
- 已有 `plugins/scoped-memory` 与本项目兄弟目录布局可能冲突，需要先审阅旧 provider 归属。不能静默并存两个 worker 源或替换一个正在使用的 provider。
- 文件、配置或 worker 验证失败时只恢复本轮精确目标；检测到并发修改时保留新修改并报告冲突。备份在目标 home 的 `backups/hermes-companion/`，权限 0700，配置和凭据原件为 0600。PM 自己管理依赖事务，本工具不直接覆盖 PM 内部状态。
- 激活失败不撤销已经验证的文件安装，也不报告“已连接”。活动会话不强行中断；在 Hermes 自身工具进程内无法重启时，返回 `restart_required` 及独立终端命令，不使用后台／定时绕过。
- 此工具不是任意 Hermes 版本的无条件补丁器。当前补丁基线为 `5894e750c3dba980d43b25aa29e3958149ba2c61`；其他版本须通过实际补丁组合和 SDK 检查。封装不可写的宿主、缺少 bubblewrap 或不匹配的源码会明确阻挡。

`--home` 选择目标 profile，`--host-source` 指定自定义宿主源码，`--hermes` 指定实际 launcher，`--python` 仅用于明确指定已准备的实际宿主解释器，`--runtime-home` 指定专用 worker 目录。默认 worker 位于当前用户本地数据目录中，按目标 home 的哈希隔离，不使用维护者路径。

`--check` 还会比较实际配置，并使用安装记录中的网关 PID 判断是否已重载；旧进程仍显示 connected 不代表新文件已经生效。未通过本入口登记的旧安装，应先检查并接管，再报告完整激活。

完成后 agent 应分别报告：安装文件与哈希、宿主增量、配置／记忆功能选择、worker 检查、网关连接，以及尚未完成的 QQ 客户端验收。真实消息只在用户授权的目标内验收，不用捕获端测试冒充实际送达。

## 仓库内的安装验证

`python3 -m unittest discover -s tests -v` 执行合成事务、来源绑定、兼容增量、范围保留、失败回滚和激活状态测试。提供 `HERMES_SOURCE` 时验证实际干净宿主基线的补丁组合；再提供 `HERMES_PYTHON`、`HERMES_LAUNCHER` 时，在一次性 home 中执行真实配置 CLI、解析器与隔离 worker 的完整安装和重复安装。

该集成夹具借用已准备依赖，明确替代 PM 下载与 gateway 激活，不修改真实宿主、账号或运行 profile，不连接 QQ。真实环境仍由安装器调用 PM 并通过正式 gateway 命令激活，不能把夹具结果写成任意发行版或 QQ 客户端已验收。
