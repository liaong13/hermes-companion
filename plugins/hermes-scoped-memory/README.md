# Hermes Companion · 分域记忆组件（实验性）

[Hermes Companion](../../README.md) 的可选记忆提供者，发现名称为 **`scoped-memory`**，源码目录为 `hermes-scoped-memory`，清单版本为 `0.3.2`。它与人格无关，不要求每位用户一个 profile，也不会自动迁移既有云记忆。

`0.3.2` 补齐明确语义记录及普通好友 DM 的精确主体过滤，公共 API 仍为 `1`；本人确认、来源删除与过期检查由 life-memory 调用原存储合同完成。

QQ／social／life-memory 成套接入使用 [INSTALL.md](../../INSTALL.md)，完整分发本目录作为数据层并保留已有私人 provider；下面启用本 provider 的实验步骤不属于基本安装。provider 与个人命令使用宿主 `hermes_yaml`，不依赖 PM 环境另有 PyYAML；当前修复与配套验证见[安装验收记录](../../docs/安装修复与统一接入验收.md)。

`0.3.1` 在公共 Store 增加调用方事务内的来源、证据与候选核对入口，使[共同经历及约定](../../docs/共同经历约定与联系反馈.md)可以复用原有存储，并让回复原文和撤回依赖原子提交。公共 API 版本保持 `1`，无新增 API 调用或 provider 切换。

`0.3.0` 提供版本化公共入口 [api.py](api.py)，以及严格的 `api_free=true` 方案：必须使用 `local-only`，关闭自动提取、embedding，并移除 `embedding.local_http`；矛盾配置会拒绝初始化，状态可读回 `api_free`。存储、归档、证据、关键词检索与删除无需任何 API 或本机推理服务，配置见[本阶段说明](../../docs/纯本地记忆与交流续接.md)。该策略不控制 Hermes 的对话模型和通道网络。

`0.2.2` 增加可复用 `people.py`，由独立 [hermes-life-memory](../hermes-life-memory/README.md) 组合 Store 实现本人自述证据、修订和当前群视图；该服务不要求注册或启用本 provider。既有 provider 提取与通用候选入口不自动转换为人物资料。

> **不是即装即用的多人长期记忆方案。** 路由、数据层、隔离 worker 与宿主补丁已有实现和离线证据，但完整社交长期记忆链路仍有宿主应用、真实接线与生产验收阻挡。插件无法独自阻止内置 MEMORY／USER、历史、文件或工具泄露；配置审核标志不是安全认证。不要据离线测试放宽真实白名单。

## 两种模式，不要混用部署说明

| 模式 | 用途 | 主人后端 | 归档与作业 |
| --- | --- | --- | --- |
| `composite`（schema 1／`legacy` 域） | 兼容既有主人 Mem0，其他来源本地存储 | 仅严格主人 CLI／私聊延迟加载既有 Mem0 | 旧容量 256 内存队列，崩溃可能丢未刷盘任务 |
| `local-only`（schema 2／独立域） | 独立社交数据层 | 不要求主人映射，不读取 Mem0；home 含 `mem0.json` 即拒绝 | 来源与 outbox 同事务；成功返回 `durably_accepted`，失败返回 `failed` |

local-only 提供持久作业、证据校验、提取候选、关键词／关联与向量混合检索、个人候选核对命令、公共提案和本地忘记流程。embedding、自动提取和公共库默认关闭；开启配置不等于宿主能力已接通。`0.2.1` 增加显式本地 HTTP embedding 的普通注册接线，不自动选择模型或启动服务。

[NapCat `host_llm` 合同 4](../hermes-napcat/README.md)虽复用本目录 `isolated_worker.py` 做 prepare，**该公共问答分支不写长期记忆，也不加载本提供者**。它的部分 QQ 场景验收不能算本组件的生产验收。带本地记忆的 `ProcessIsolationBridge` 路径是另外一条接线。

## 身份、归属与可见范围

composite 路由规则：

| 可信来源 | 关键记忆后端 | 本轮原文 |
| --- | --- | --- |
| 明确本地 `platform=cli`，无网关／聊天／用户身份 | 既有主人 Mem0 主体 | 本地主人／CLI 来源 |
| NapCat 主人且精确为 `dm:<同一用户ID>` | 同一既有 Mem0 主体 | 本地主人／私聊来源 |
| 其他 NapCat 私聊 | 本地 SQLite | 对应用户／私聊来源 |
| 所有 NapCat 群聊，包括主人 | 本地 SQLite | 对应用户／群／线程来源 |
| 身份缺失、畸形、未知平台、子任务或定时任务 | 拒绝，不回退主人 | 不归档 |

local-only 不开放 CLI 主人回退，不委托 Mem0。范围以域、用户 `napcat:<发送者ID>` 和精确来源 `dm:<发送者ID>` 或 `group:<群ID>[:thread:<线程ID>]` 绑定。默认不跨群、不从私聊到群、不读同群其他人的个人记忆／原文；主人也没有本地超级用户权限。模型工具不能传身份／租户参数改变范围，会话重置不改变持久归属。

身份依赖宿主初始化参数和逐请求可信 ContextVar（platform/user/chat/type/thread/session/message ID），可选 `turn_author.id` 必须匹配。插件不信任正文、昵称、会话名、`channel_context` JSON、进程环境回退或工具参数。未知内部接口、网关缺失实时上下文均拒绝；API、Desktop、local 别名不自动成为主人。

composite 中其他用户与群聊不会经本插件的召回、轮次预取、同步、镜像或工具写入主人 Mem0。整段 messages、会话结束历史和压缩前历史不发送到 Mem0。主人逐轮提取与关键记忆 CRUD 委托既有插件，保留原 `agent_id`、语料和配置；`user_id` 必须与显式 `owner_mem0_user_id` 一致。更新／删除额外验证 `backend.get(id).user_id` 完全匹配，缺失也拒绝；这是已检查的 Mem0 内部接口，升级必须复验。外部部署应显式指定自己的主体，不依赖源码的历史默认值。

## 宿主版本与补丁依赖

核对基线为 `5894e750c3dba980d43b25aa29e3958149ba2c61`。当前代码依赖 `agent/agent_init.py` 的 provider 参数、网关身份初始化参数、`gateway.session_context` 和 MemoryManager 的上下文传播；这些内部契约升级需检查。

普通 `PluginContext.llm` 存在，**不代表 memory-provider collector／host 子进程已经能使用**。自动提取的正式注册接线依赖 [llm-facade.patch](../../integration/hermes-host/llm-facade.patch)提供的 `ctx.bound_llm`、`ctx.bound_session`、collector 转发和 RPC 白名单：注册时取得实例能力，初始化核对 home／plugin／辅助任务归属，每次请求读取实时身份，调用前后检查绑定和卸载状态。不从 collector 私有字段取服务，也不缓存跨 profile singleton。

补丁仅在临时宿主副本与真实插件两种加载路径验证，不能据此声称目标安装已应用或其他版本兼容。详细复验、临时目录限制和应用边界见 [宿主集成 README](../../integration/hermes-host/README.md)。只有一个外部 memory provider 可激活，因此 composite 内部委托 Mem0，而不是同时选择两个 provider，参阅 [官方契约](https://hermes-agent.nousresearch.com/docs/developer-guide/memory-provider-plugin)。

## 安装、配置与只读预检

以下是**获准隔离测试环境**的准备说明，不是要求修改私人 default 或线上环境。先确定实际宿主解释器、源码和目标 home，安装代码与运行数据分开：

```sh
export COMPANION_SOURCE="$HOME/tools/hermes-companion"
export HERMES_SOURCE="/path/to/hermes-agent"
export HERMES_PYTHON="/path/to/actual/hermes/python"
export HERMES_HOME="/path/to/approved/staging-home"
```

1. 审核文件／历史／工具／共享上下文与模型出站边界，在**目标 home** 通过 Hermes 配置命令显式设置布尔 `memory.memory_enabled=false` 和 `memory.user_profile_enabled=false`。缺省并不关闭；宿主先加载内置记忆再初始化插件，插件拒绝不能撤回已注入的内容。旧 agent 需重建，原文件保留备份。不要为了测试直接关闭私人 default 的全部记忆。
2. 在目标 home 明确创建 `scoped-memory.json`。local-only 最小关闭状态如下：

   ```json
   {
     "schema_version": 2,
     "mode": "local-only",
     "domain_id": "social-example",
     "policy_revision": 1,
     "isolation_reviewed": true,
     "max_injection_bytes": 768,
     "source_retention_days": null,
     "extraction": {"enabled": false},
     "embedding": {"enabled": false},
     "public_group_memory": {"enabled": false},
     "jobs": {"concurrency": 1, "max_attempts": 5}
   }
   ```

   `isolation_reviewed` 仅记录操作者确认；local-only 不配置 `owner_mem0_user_id`，home 不得含 Mem0 配置。composite 则需从 `platforms.napcat.extra.owner_id` 读取真实主人映射，并使用自己的既有主体，例如 `{"mode":"composite","schema_version":1,"domain_id":"legacy","isolation_reviewed":true,"owner_mem0_user_id":"existing-owner-id","max_injection_bytes":768,"source_retention_days":null}`；须与目标 `mem0.json` 的 `user_id` 完全一致。`owner_mem0_user_id` 现在必须显式填写非空字符串，省略时阻挡初始化，不再提供维护者身份默认值。升级既有 composite 配置时，先在本地填写原有主体，不创建新主体或迁移云语料，再执行预检并重建 agent。
3. 执行只读配置预检：

   ```sh
   PYTHONPATH="$HERMES_SOURCE" "$HERMES_PYTHON" \
     "$COMPANION_SOURCE/plugins/hermes-scoped-memory/preflight.py" \
     --home "$HERMES_HOME"
   ```

   退出 2 是阻挡；退出 0 也只代表配置门槛通过，仍输出 `NOT_CERTIFIED`。预检不写配置、不访问 Mem0，不认证隔离、提取或云服务。
4. 另经部署审阅后，完整插件源码放入目标 `$HERMES_HOME/plugins/scoped-memory/`，依赖使用实际 Hermes Python 环境；composite 保留既有 `plugins/mem0`。再通过 `hermes config set memory.provider scoped-memory` 选择提供者。重启由独立管理终端执行，并确认实际加载与失败处理。复制文件或预检通过不构成上线批准。

域、模式、主人映射或 policy revision 变更会使旧 provider 拒绝继续操作，须更新策略并重建 agent。`max_injection_tokens` 只作为旧 UTF-8 **字节**别名兼容并告警，与 `max_injection_bytes` 冲突时拒绝。

## 存储、归档与恢复

运行数据不放源码目录：

| 路径（相对目标 home） | 用途 |
| --- | --- |
| `scoped-memory.json` | 明确的模式／域／审核与功能设置 |
| `scoped-memory/memory.sqlite3` | 内容、证据、索引、向量与 outbox |
| `scoped-memory-deletions/ledger.sqlite3` | 独立删除清单与恢复水位 |
| `scoped-memory-billing/` | 独立 embedding 费用账；另需显式 profile 外 checkpoint |

内容目录 `0700`、数据库 `0600`，SQLite WAL、逐次连接、15 秒超时与事务。这是逻辑隔离，不是加密或 OS 沙箱。备份使用 SQLite backup API 或停库复制，活跃 WAL 时只复制主文件可能漏数据。

原文完整保留宿主交给 provider 的本轮 user／assistant 字符串，附所有者、精确来源、session/message ID、时间和 JSON `[user_text, assistant_text]` 的 SHA-256；不收附件、工具轨迹或任意整段历史。宿主可能已脱敏并去技能支架，它不是原始协议包逐字节副本，引用文本也不是认证事实。

- composite 异步队列满或磁盘失败会更新 `last_error` 并写不含正文的警告；`drain()`／关闭等待已接收任务，但无持久重试与恰好一次保证。
- local-only 来源和 outbox 同事务，带消息幂等、修订、租约、fencing、删除 epoch、总尝试预算、重试和死信。durable receipt 不证明宿主已向用户报告失败，也不是网关可靠消息投递保证。
- 恢复内容库必须保留当前独立删除清单；UUID／水位异常或缺失时拒绝。同时回滚整个 home／文件系统快照，无法用同一快照自证最新删除状态，缺少独立可信水位时不得恢复上线。
- 费用恢复检查 UUID／单调序号／哈希链及 profile 外 checkpoint。回滚内容库不退款，丢失 checkpoint 拒绝；未知 usage／超时预留不释放，不把未知费用记为零。连同 checkpoint 回滚仍不能本机自证最新。

## 检索、工具与删除语义

主对话按需调用 `scoped_memory`，工具不能指定其他用户或群：

```jsonl
{"action":"sources","limit":8}
{"action":"source","source_id":"<当前范围来源ID>","offset":0,"limit":4000}
{"action":"add","content":"示例项目周五发布。","source_id":"<来源ID>","associations":{"topic":["示例项目"],"person":["小明"],"event":["发布会"]},"ttl_days":365}
{"action":"search","query":"示例项目发布安排","limit":8}
{"action":"get","memory_id":"<关键记忆ID>"}
{"action":"update","memory_id":"<旧ID>","content":"发布延期。","source_id":"<新来源ID>","associations":{"topic":["示例项目"]}}
{"action":"archive","memory_id":"<关键记忆ID>"}
{"action":"delete","memory_id":"<关键记忆ID>"}
{"action":"pending","limit":8}
{"action":"forget","memory_id":"<明确要忘记的本地记忆ID>"}
```

- composite 仍需显式关键记忆写入。关键词索引 ASCII 词、中文相邻双字和独立单字，按词重合及精确 topic/person/event 标签扩展一跳；不是中文分词、语义模糊匹配或多跳推理。默认 8 条、最多 20 条，每条最多 128 个词、查询 64 个词；无关／标点查询不注入，单字不保证命中双字内部。
- 自动召回只注入有效未过期关键记忆，不注入原文；默认预算 768 UTF-8 字节，包含分隔符，不是精确 token，宿主包装另计。截断不影响存档。
- 显式读取最多 20 条、每条最多 16000 字节；原文单次最多 4000 字节，offset／limit 按字节，边界可能省略不完整字符。来源列表只返回元数据。显式工具输出可能超过自动注入预算，仍需宿主约束。
- `update` 事务中新建记录、旧记录标 `superseded`，标签／TTL／来源不自动继承，并发仅一次替换成功。TTL 过期立即退出检索，维护时归档；可选原文保留期清正文而保留哈希／元数据，默认 null 不自动删除。维护由归档写入后或操作者 `store.retain(...)` 执行，没有生产 cron。
- `archive`／`delete` 是软状态，当前范围仍能按 ID 读旧文。`forget` 清本地来源及依赖事实／证据／索引／作业，不等于擦除磁盘残留、宿主旧提示词／摘要、备份、已发 QQ 消息或云端副本。
- 主人 Mem0 仅支持 search/add/update/delete；本地 get/archive、标签、TTL 和来源绑定不适用并拒绝，主人本地原文仍可读。

local-only 的向量服务在 SQL 范围过滤后做精确 cosine＋RRF，校验完整模型指纹并使用影子代次切换，无需 sqlite-vec；异常／查询超时降级关键词，不混代。真实中文 embedding 质量、延迟、合规与成本未验收。自动提取只调用绑定的 `HostExtractor.complete_structured`，校验 schema、原文坐标和证据，候选进入 pending，不自动确权。

## 显式本地向量服务

在已审核的 local-only home 中，`scoped-memory.json` 的 `embedding` 可配置如下。端口、模型名、实际维度与版本均须按自己已经运行的服务填写；示例不安装模型或启动服务：

```json
{
  "enabled": true,
  "local_http": {
    "endpoint": "http://127.0.0.1:8765/v1/embeddings",
    "model": "your-local-embedding-model",
    "model_revision": "your-reviewed-model-revision",
    "dimensions": 1024,
    "query_prefix": "",
    "document_prefix": ""
  }
}
```

- 只接受带显式端口的字面量 `127.0.0.1` 或 `[::1]` HTTP 地址及精确路径 `/v1/embeddings`；不接受主机名、凭据、query、fragment 或远端地址。不使用环境代理、不跟随重定向、不读取 API key。
- 服务需接收 OpenAI 格式的 `model`、字符串数组 `input`、`encoding_format=float`，返回相同模型名和带唯一 `index` 的向量。维度、有限数值、模型版本与预处理前缀均纳入校验或指纹；修改 `local_http` 后重建 provider。
- 普通 `register()` 会连接该运行时。后台每批最多 8 条，仅为已经 active 且未过期的本地事实补向量；不把原文、pending 候选或 Mem0 内容送给服务。查询截止约 0.8 秒，索引请求约 5 秒，失败退避；服务忙、超时或异常可降级关键词，删除和策略变化使旧结果失效。
- loopback 只限定本插件的请求目的地，不能证明这个服务自身没有向外转发；服务的部署、模型版本、数据处理与真实效果仍需审核。本次只使用合成 HTTP 向量验证接线。
- 该接线属于普通 memory provider。独立带记忆 worker 仍使用其原有关键词路径；合同 4／5 的无记忆 prepare 分支不会初始化 provider 或开启向量检索。

调用工具 `{"action":"status"}` 查看能力：`embedding` 报告 `disabled`、`indexing`、`ready`、`degraded_keywords`、`injected_runtime` 或 `blocked_missing_runtime`；`last_retrieval` 单独记录最近检索路径。只有 `enabled=true` 而没有本地配置或受信注入服务时，明确报告 `blocked_missing_runtime`，仍可使用关键词，不把它报告成向量成功。只读预检的 `configured_local_runtime_unprobed` 仅表示配置有效，没有探测服务。

## 个人候选核对与确认

自动提取产生 pending 候选，默认不能被自动召回。本人可在原来源发送：

```text
/scoped-memory pending
/scoped-memory review <候选ID>
/scoped-memory confirm <候选ID> <核对码>
/scoped-memory reject <候选ID> <核对码>
```

`review` 返回完整候选与确认／拒绝命令。核对码绑定候选内容、来源证据、修订、有效期、删除 epoch 和策略版本；证据修订／清除、候选过期、忘记或策略变化后必须重新核对，已处理命令不能重放。只能操作本人当前来源／线程的候选；确认个人记忆不将其发布为群公共事实。

入口有两种，不能混用部署结论：

1. 普通插件注册 SDK 命令，从实时 `bound_session`／宿主原生任务上下文取身份，并复核当前 home、已选择的 `memory.provider=scoped-memory` 和消息来源。未验证身份、环境变量伪造或切换 provider 均拒绝。该入口可以在 agent 初始化前核对已有候选。
2. 带记忆的 `SandboxedWorker` 从已认证 route 解析原始文字命令，直接核对数据库而不调用模型。当前核对的宿主会阻挡 NapCat 群通用 slash 命令，本次没有放宽该管理门槛；群内使用须接通这条独立 worker 路径。现有无记忆合同或媒体分支不解析这些命令。

模型工具没有 `confirm`／`reject` 权限，模型回答中出现同样文字不会执行。生成候选仍需既有提取 facade 或受信写入流程；新增命令本身不自动开启提取。用于完整人物资料与跨场所社交的后续设计见[社交能力与分层本地记忆](../../docs/社交能力与分层本地记忆.md)。

## 公共事实与隔离 worker

公共库必须显式开启、绑定本 Store 的可信服务，并配置精确群／线程的 canonical principal 确认／决议名单。模型只获得 `public_propose`、`public_pending`、`public_lookup`、`public_withdraw`，不能签发一次性确认 grant；昵称、群 role 和“主人”自述不确权。

独立记忆 worker 可解析**原始用户命令** `/memory-propose`、`/memory-confirm`、`/memory-resolution`、`/memory-resolve`、`/memory-withdraw`、`/forget`、`/forget-all`；不执行模型输出中的管理命令。`/forget-all` 只清本人当前来源。撤回贡献后，仅剩已批准且包含全文的独立来源可重建公共事实，空白“确认”不能替代原证据。命令格式、冲突流程与权限表见 [后续需求处理](docs/后续需求处理.md#公共事实如何确认)。

带记忆的 `SandboxedWorker` 使用 bubblewrap namespace，只挂社交 home 与源码，清空环境，无文件／终端／历史／技能／cron／delegate／任意发送工具；主模型使用社交 home 自己的 `chat.json` 和 `0600` 的 `model.key`，不是主人凭据。bridge 仍需可信启动代码注入完整私人 turn 回调；普通工厂不会自动完成这条有记忆路径。它提供归档、关键词和公共记忆，不暗建另一套云提取 fallback。详细入口见 [实现与验收](docs/实现与验收.md)和[后续需求处理](docs/后续需求处理.md)。

## 仍需宿主与部署负责

1. 禁止私人 MEMORY／USER／SOUL、项目上下文及历史在社交初始化前进入上下文；旧 agent、压缩摘要和缓存必须独立治理。
2. 限制文件、终端、技能、历史、子任务及定时能力，防止绕过数据库归属；profile 本身不是 OS 沙箱。
3. provider 失败时宿主可能继续聊天，必须在社交 agent 构建前强制准入，不能把 provider 禁用当成整机拒绝。
4. 本地存储不意味着主模型、日志、中继或辅助云不处理文本；provider／ZDR／出站授权、真实费用与保留策略需逐项确认。
5. 应用匹配宿主补丁、真实私人回调、单一 OneBot 所有权、公共确认名单、独立 checkpoint 保留介质，以及多账号 QQ、重启、压缩／重置和故障演练，仍不能用合成结果代替。

## 可复现测试与证据

测试前设置获准、存在且可写的 `TMPDIR`；夹具直接读取该变量创建合成 home。Mem0 离线集成另需 `MEM0_PLUGIN_SOURCE` 指向已审阅的扁平 Python 插件源码目录；只复制顶层 Python 源码、清单和 LICENSE，不复制配置或数据。未配置时该项明确 skip，配置无效时失败，不查找维护者的安装目录：

```sh
cd "$COMPANION_SOURCE"
TMPDIR=/path/to/reviewed/writable/scratch PYTHONPATH="$HERMES_SOURCE" \
  "$HERMES_PYTHON" -m pytest -q plugins/hermes-scoped-memory/tests
"$HERMES_PYTHON" plugins/hermes-scoped-memory/benchmark.py --split acceptance --k 3
```

[测试报告](docs/测试报告.md)最新记录为 2026-10-31 安装修复后的 **225 passed、2 skipped**；两个 skip 是未应用 `llm-facade.patch` 的宿主绑定测试，保留 809 条既有弃用告警。历史临时补丁副本执行过 2 passed、宿主 canonical runner 45 passed，本次未重跑该补丁流程。Mem0 集成使用真实发现／MemoryManager 与离线后端，禁止该集成访问 socket，不访问生产云记忆；向量接线使用单独的本地合成 HTTP 服务。

中文固定集使用人工概念向量，只验证排序／权限流程；不能当真实 embedding 效果或付费成本证明。更完整的当前事实与历史分别见：

- [开发状态](docs/开发状态.md) · [实现与验收](docs/实现与验收.md) · [后续需求处理](docs/后续需求处理.md)
- [开发设计](docs/开发设计.md) · [开发日志](docs/开发日志.md) · [OpenRouter 嵌入模型选型](docs/OpenRouter嵌入模型选型.md)

自有代码、测试与文档采用 [MIT](LICENSE)，未复制或改编 LivingMemory 的 AGPL 源码；Hermes、Mem0 与其他依赖保留各自许可证。
