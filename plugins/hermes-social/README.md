# Hermes Internet Life（插件 ID：hermes-social）

定位为互联网生活运行层，MIT 许可。社交是它目前实现的一种活动能力；完整目标包括关注事项、计划、阅读探索、创作、交流、经历和复盘。当前基础版本 `0.2.0` 提供自然参与、近期文本、合并、节流、暂停、空闲机会和统一联系账本；通用活动调度及反思回用尚未完成。保留 `hermes-social` 插件 ID、模块名与数据路径，便于后续单独迁移。

框架不预设任何角色、性格、语气、亲密关系或兴趣。通道的呼唤名仅用于触发，不自动成为智能体身份。可用 `public_self` 提供实例愿意公开的初始自我描述；它不读取私人 SOUL，也不等于已经实现持续成长。后续自我模型的修订应由可追溯经历、反思、验证及回滚构成，见[开发方案](../../docs/互联网生活能力开发方案.md)。

与 NapCat／life-memory 成套接入优先使用 [INSTALL.md](../../INSTALL.md)；首次安装配置自然接话、无 idle 目标，升级保留既有停用和范围选择。本插件配置自身默认关闭，模型 idle 的字段默认值与安装方案显式选择的 `local` 模式分别说明。

`0.2.0` 增加 `initiative_mode=local`，只处理 life-memory 的明确约定，不调用 idle 模型；与模型 idle 共用一次性消费、额度和反馈。详见[共同经历与联系反馈](../../docs/共同经历约定与联系反馈.md)。此前 `0.1.2` 增加按精确群撤销近期记录的 `forget_messages`；重复通知不影响新交流。

## 交付边界

- **运行插件**：使用 `ctx.register_platform_handler('napcat', factory)` 接入通道，没有第二个 OneBot 连接。controller 只持有 ParticipationPort 和本插件的 profile/model binding，不导入连接器实现。
- **只读上下文**：idle 提供本目标最多 16 条、1 小时内真实近期记录；正常群聊临时快照使用十分钟、十二条窗口。内存最多保留 64 条；idle 可注入严格作用域的 `ContextProvider`，群聊快照只用通道真实观察，不借此读取长期私人记忆。
- **配套 Skill**：注册为 `hermes-social:social-participation`，提供交往和运维方法，不代替常驻任务。
- **原通道**：继续拥有真实事件、source ledger、worker、身份／工具权限、单次出站和准确回读。插件缺失、禁用、卸载或存储故障时，正常点名与主人私聊仍由通道处理。

本插件不包含自动加好友、非主人私聊、长期关系记忆服务或新的核心工具；安装器由仓库根目录单独提供。

模型 idle 主要依据近期上下文和时机规则；本地模式只提起已确认约定。持续阅读活动、关系推理及有来源的成果分享尚未接通。后续设计见[社交能力与分层本地记忆](../../docs/社交能力与分层本地记忆.md)，其中的未来能力不属于本版本。

可选 [hermes-life-memory](../hermes-life-memory/README.md) 提供本人自述、规则候选、同群交流及约定，由原通道独立接线，不更换公开群 `ContextProvider`。idle 不读取普通人物档案；约定联系只取得本人明确批准的该项内容、固定收件人与有效期限。

## 宿主与通道前提

需要现有 Hermes SDK 的 `register_platform_handler`、`register_skill`、`on_unload`、`spawn_task(coro, *, name=None)` 和 `ctx.llm.acomplete(messages=...)`。manifest 使用官方通用插件类型 `kind: standalone`，不声明未经核对的依赖字段。

通道须公开 `participation_port()` 和 `install_participation(controller)`。详见 [端口与控制器合同](ports.py)、[接线入口](__init__.py) 和项目的[拆分合同](../../docs/社交插件拆分.md)。实现还使用双方明确新增的 `set_registration_key(key)`、`detach()`、`last_receipt(chat)`。

controller 附着前要求 `public_context_contract=1`，并核对 `observe_activity`、`wait_visual_context(..., max_seconds=...)` 与 `invalidate_optional(..., keep_pure_image=...)` 签名，混用旧通道时在后台任务启动前拒绝，避免丢失临时公开上下文。后台失败只记录异常类型和代码位置，不记录异常正文、完整路径或含群号的任务名。

SDK 工厂缓存键的 owner 是 `ctx.manifest.name`，不是一定相同的 `ctx.plugin_id`。本插件设置 `(ctx.manifest.name, factory.__qualname__)`；卸载先同步 `active=False`、撤销可选许可并 `port.detach()`，再在 attach 捕获的 loop 上取消与 join。通道的 detach 必须清除该 SDK 缓存键，才能在同一个 native client 上重新启用。

工厂用弱引用键记录已附着的 adapter，以兼容通道每次返回新 port；controller 本身不保存 adapter。重复 factory、新 native 或重连不会新增第二个 controller。旧 factory 在卸载后不再附着。

## 配置

全部社交策略放在：

```text
plugins.entries.hermes-social.settings.channels.napcat
```

没有第二份 QQ 白名单：

| 字段 | 默认与语义 |
|---|---|
| `enabled` | `false`，可选社交总开关 |
| `public_self` | `{}`；可选初始公开资料：`name`、`description`、`communication_preferences`，不提供系统提示词覆盖 |
| `participation_groups` | 省略时跟随 port.groups；仅保留显式测试／迁移覆盖，不能扩大通道授权 |
| `targets` | `[]`；仅为空闲发言的精确目标，绝不从参与群自动推导 |
| `initiative_mode` | `model`；`local` 仅处理已确认约定，不请求 idle 模型 |
| `timezone` | `Asia/Shanghai` |
| `quiet_hours` | `[23, 9]`，idle 安静时段；相等表示无安静窗口 |
| `idle_after_seconds` | `1800`，范围 300–86400 |
| `min_idle_interval_seconds` | `7200`，范围 300–604800 |
| `daily_limit` | `3`，范围 1–12；无回应时退让 |
| `ambient_cooldown_seconds` | `20`，范围 5–300；真实回复后有短续聊窗口 |
| `ambient_debounce_seconds` | `3`，范围 1–30；合并未点名候选 |

使用宿主配置 CLI，不手改 config.yaml。先检查 `hermes config set --help`；以下仅启用已由通道授权群的自然接话，不开启 idle：

```sh
hermes config set plugins.entries.hermes-social.settings.channels.napcat '{"enabled":true,"targets":[]}'
```

安装、启用插件与活动网关重载是不同步骤；本目录未提供安装／重启脚本。部署时须先检查原通道与宿主合同，不复制凭据、私人历史或另一 profile 的运行数据。

### 聊天内控制

兼容 `/napcat-social pause|resume|status`，也接受 `/social pause|resume|status`。命令只影响已验证的当前聊天；成员可以暂停，只有已验证主人可恢复。暂停后仍观察该聊天以接收恢复命令，正常点名与主人私聊不受可选策略影响。

## 数据与一次性升级

固定状态路径由官方 helper 取得：

```python
from plugins.plugin_storage import plugin_data_dir
path = plugin_data_dir('hermes-social') / 'napcat' / 'social-state.json'
```

即 `<profile-home>/plugin-data/hermes-social/napcat/social-state.json`。只写时间／次数／暂停／无人回应状态，原子持久化消费后才允许发送；消息、昵称、凭据和模型结果不落入此文件。发送回执未知时不重试、不重放。

`0.2.0` 在同目录新增私有 `contacts.sqlite3`，原子保存模型 idle 与约定的一次性消费、统一额度、精确收件人、回执及反馈元数据，不保存正文。载入时保守合并旧 JSON 计数；回滚 JSON 不能恢复已消费额度。定向联系只有收件人的活动才解除无人回应退让；引用实际回执才算回应。恢复时须保留账本，不能只恢复旧内容库并丢弃消费记录。

默认 controller **不读取** `<profile-home>/napcat/social-state.json`。显式升级开关是：

```text
plugins.entries.hermes-social.settings.migrations.napcat_state_v1 = true
```

开启后只在 factory 取得 port、创建 controller **之前**尝试迁移；register 时不复制。迁移源必须是当前 owner 的普通 `0600` 文件、最多 128 KiB、schema 1 且仅含原计数字段；私有消息、额外字段、符号链接和畸形值均拒绝。新目录／文件以 `0700`／`0600` 创建，通过原子 link 发布，目标已存在或竞争创建时绝不覆盖，源保留，无源则跳过且不创建迁移目录。

迁移使用公共 `gateway.status.live_gateway_pid_for_home(home)`；若另一个活动网关 PID 仍拥有该 home，则拒绝迁移。应在旧服务停机后由取得 profile 锁的新网关 attach 读取最新状态，避免部署时提前复制后丢失最后的消费计数。配置开关、文件安装完成不等于状态迁移已经发生；必须在实际重载后检查目标。迁移不是长期记忆启用。

## 上下文与模型

[ContextScope / ContextProvider](context.py) 将 profile home、platform、chat 与 subject 绑定。群公开作用域的 subject 为空；单人私有作用域必须显式匹配，DM 作用域必须等于精确收件人。读取前拒绝跨作用域请求，返回记录再核对相同 scope；注入的 provider 自身也必须在 I/O 前核验授权，返回检查不能撤销已经发生的越权读取。昵称不参与身份合并。

默认数据只来自真实 `observe` 和已发送的真实 `record_self`；撤回会使依赖资料的输出失效，断线与卸载撤销可选参与。卸载生活插件不会撤销通道原本已准入的点名回复。通知不应由通道传给 `record_self`。只有真实回复会开启续聊窗口，`NO_REPLY` 不记为发言。

未点名的自然文本经合并与节流获得模型判断机会，不再以疑问词、短答词表或主题字面重合判断意图。真实 @／核验引用给其他成员的消息不触发可选回复，但可作为标注过的同群背景。模型自行选择回应或 `NO_REPLY`。真实出站的 `reply_to_message_id` 与原发言人构成近期交流证据，其他成员插话不会抹掉这对记录，也不会因此断言新消息一定在回答机器人。

点名与可选群聊均通过宿主的临时上下文钩子取得本群十分钟内最多十二条公开记录，预算 5000 字节，避免只看到各自的个人会话；原始用户文本不附加提示词。撤回及忘记会撤销依赖这些资料的输出。普通新消息在首次可选回复发起后最多二十秒、八条内不会自动撤销在途文本回复；不延长原许可，不改变收件人。明确新请求、控制命令、撤回、暂停、断线仍撤销；媒体候选保持更严格的新活动失效规则。

图片识别完成后在完整路由窗口内更新原消息；超过展示窗口仍可更新，超过 64 条、过期或已撤回的消息不会因迟到结果重新出现。自动化验证通过不等于已安装或真实 QQ 验收。

idle 使用 `ctx.llm` 复用宿主模型与登录，没有 provider/model/profile override、工具、私人 `side_question` 或历史加载。新任务在空 Context 中建立，仅绑定自己的 profile 和当前目标 session；群只传固定公开 system 与本群近期 user 资料。真实未点名消息仍经 port 进入原通道完整 gateway/tool 路径，不冒充自动化用户。

## 验证

在本目录执行：

```sh
PYTHONDONTWRITEBYTECODE=1 hermes --run-module unittest discover -s tests -p 'test_*.py' -v
```

测试使用 `$TMPDIR`，没有真实 QQ 连接：

- 独立策略／controller／上下文／状态／迁移测试，真实 SDK `PluginContext`、`PluginManager`、任务监督与 `PluginLlm` facade，模型响应为离线 fixture。
- 仓库中存在相邻插件源码时，额外验证真实 `HostedNapCatAdapter`、ParticipationPort、source ledger、worker 的 `prepare_proactive` 校验函数、SDK late-load／unload／重新启用；仅 OneBot 换为发送／回读捕获端，强制禁止真实 connect。
- 独立分发没有相邻源码时，通道集成用例明确 skip，不把它们记作已通过。

本测试不等于实际模型的自然交流验收，也不覆盖完整 durable FIFO/source codec/AIAgent 真实模型链路；后者由项目集成验收负责。安装副本、活动网关加载、长期记忆和 QQ 客户端可见性均须分别报告。

本轮源码与已安装副本各完成 107 项离线测试；安装器及跨组件回合另列，见[安装验收记录](../../docs/安装修复与统一接入验收.md)。这不是新 checkout 自动重跑的结果。

## 许可证

[MIT](LICENSE)。配套 Skill 见 [Skill 说明](skills/social-participation/README.md)。
