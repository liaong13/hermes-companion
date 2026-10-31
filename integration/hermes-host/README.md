# Hermes Companion · Hermes 宿主集成补丁

本目录属于 [Hermes Companion](../../README.md)，交付 NapCat 公共群、social、life-memory 及实验性 [scoped-memory](../../plugins/hermes-scoped-memory/README.md) 的宿主补丁和副本验证工具。**补丁文件本身不安装或激活组件，也不是生产长期记忆已可用的声明。**

QQ／social／life-memory 成套接入现在可使用仓库根目录的 [统一安装入口](../../INSTALL.md)。它会在临时覆盖层组合检查本目录的匹配增量，再备份并应用必要文件；本目录保留独立补丁与实验性 provider 的手动复验说明，不代表任意版本都能直接应用。

默认组合为 `napcat-public-turn`、`napcat-social-participation`、`napcat-ambient-quiet`、`napcat-shared-identity`；启用本地交流上下文或显式共享人格时另加 `napcat-local-memory-context`，不自动应用 provider 的 `llm-facade.patch`。共享增量仅声明可信通道可提供 SOUL 与经筛选审核的非敏感日常摘要，默认不启用文件读取；边界见[共享人格与日常记忆](../../plugins/hermes-napcat/docs/共享人格与日常记忆.md)。当前配套、实际补丁组合测试及运行／客户端验收区别见[安装验收记录](../../docs/安装修复与统一接入验收.md)。

补丁基线：`5894e750c3dba980d43b25aa29e3958149ba2c61`。自有补丁采用 MIT；修改目标仍遵循 Hermes 的许可证。其他宿主版本必须重新检查与测试，不能仅凭补丁文件存在或历史测试通过认定兼容。

`--enhance-social` 同样要求 `napcat-local-memory-context`，用于每次重新筛选跨天语义资料。独立的 `verify_napcat_social_live.py --enhanced-social`、`verify_crossday_memory_live.py`、`verify_semantic_methods_live.py` 分别覆盖多人编排、明确记忆和语义方法；均需显式 `--live` 才调用真实模型，使用合成身份及捕获通道，不连接 QQ。运行命令应提供明确的 host、owner-home 和目标插件路径；报告区分初始合成 ACK、实际模型结果与客户端验收。

另有独立的 [NapCat 公共群回合补丁](napcat-public-turn.md)及[源码补丁文件](napcat-public-turn.patch)，用于 `social_agent / contract: 5` 的完整 gateway 回合、工具执行边界与持久化 source 凭证接续。它不是下文记忆 facade 补丁的同义名称，两者不能互相替代；群文本已完成特定部署的用户确认，其他能力的证据边界见对应说明。

[正式主动聊天](../../plugins/hermes-napcat/docs/主动聊天.md)还需要[未点名候选的安静交付](napcat-ambient-quiet.md)与[公开交流风格和参与判断增量](napcat-social-participation.md)。后者提供不读取私人身份文件的固定公开风格、出站类型标记，以及完整 gateway + 真实模型、但不连接 QQ 的行为验证工具；不能以其通过代替实际客户端验收。

可选的[纯本地上下文增量](napcat-local-memory-context.md)支持 life-memory `0.2.0` 的对话记忆与依赖撤回：显式开启后，公共回合使用新 Agent 和空宿主历史，由当前有效本地事件重建上下文。其[离线副本验证工具](verify_local_memory_offline.py)不调用模型、不改写真实宿主；不需要把 memory-provider 的 LLM facade 作为本地规则记忆的依赖。

## 为什么需要这份补丁

普通平台插件可以使用正式 `ctx.llm`，但 memory-provider 的 collector／host 子进程不能据此自动得到完整的 LLM 与可信来源接线。[llm-facade.patch](llm-facade.patch)增加：

- `ctx.bound_llm`：实例级绑定实际 profile home、插件与辅助任务归属；调用前后检查归属，卸载撤权并拒绝迟到结果。collector 同时公开普通 `ctx.llm`。
- `ctx.bound_session`：每请求读取父进程实时 ContextVars，缺失字段返回空字符串，不信任进程环境回退，不缓存跨轮身份；跨 profile 和卸载后拒绝。
- plugin host RPC 的明确白名单与对应绑定转发，覆盖真实并发、无 origin 后台、发现顺序、重载和身份伪造行为。
- 宿主 memory-provider 接口文档及行为测试。

`scoped-memory` 在 `register` 获取实例能力、初始化检查 home／plugin／任务归属，每次请求读取当前身份，并在提取前后复验绑定；不序列化服务、不访问 collector 私有入口、不缓存跨 profile ctx。缺少此合同的普通宿主不能算完成自动提取接线。

**不要混淆两条路径：** [NapCat `host_llm` 合同 4](../../plugins/hermes-napcat/README.md)使用正式平台 `ctx.llm.acomplete` 进行无工具、无长期记忆的公共单次问答，该模式不依赖本补丁；本目录解决的是记忆提供者的绑定能力，不会让该问答模式自动拥有记忆。

## 只读检查目标版本与差异

先指定真实路径；源码仓库与运行数据分开，不把 profile 配置、认证、数据库放进本仓库：

```sh
export COMPANION_SOURCE="$HOME/tools/hermes-companion"
export HERMES_SOURCE="/path/to/hermes-agent"
export HERMES_PYTHON="/path/to/actual/hermes/python"
git -C "$HERMES_SOURCE" rev-parse HEAD
git -C "$HERMES_SOURCE" status --short
git -C "$HERMES_SOURCE" apply --check \
  "$COMPANION_SOURCE/integration/hermes-host/llm-facade.patch"
```

上述 `apply --check` 不应用补丁，但也不能证明运行进程已经加载接口。历史目标曾只读检查通过且保留原 qqbot 两处本地差异；这是既有记录，不代表本次或外部目标已检查。应用前仍需审阅触及文件、目标基线、本地差异与回退方案，不能覆盖未审阅修改。

## 在一次性副本中复验

需要本地已有基线 Git 对象、可用的 `git`／`bash`，以及完整宿主测试依赖和 `pytest` 的实际解释器。工具不会安装依赖；不要因解释器不合适而向生产环境补装依赖。

```sh
cd "$COMPANION_SOURCE"
"$HERMES_PYTHON" integration/hermes-host/test_llm_patch.py \
  --host-source "$HERMES_SOURCE" \
  --python "$HERMES_PYTHON"
# 可选：先证明新增行为测试在未打补丁基线失败，再执行补丁后测试
"$HERMES_PYTHON" integration/hermes-host/test_llm_patch.py \
  --host-source "$HERMES_SOURCE" \
  --python "$HERMES_PYTHON" --prove-red
```

工具使用 `git archive` 展开固定基线的跟踪源码，应用补丁并运行宿主 canonical runner，随后复制本仓库真实插件源码，执行 in_process／host 两种加载路径的集成测试。它不复制目标工作树差异、真实配置或认证，不连接模型服务；`--base` 可指定另一 Git revision，但更换基线后必须重新审阅接口和结果。

**当前临时目录限制：** `test_llm_patch.py` 显式使用系统 `/tmp`，且只在副本中把 runner 的 `/var/tmp` scratch 根改为 `/tmp`；该适配不进入产品补丁。工具不遵循外部 `TMPDIR` 来改变这两个位置。如果部署政策禁止系统临时目录，不能靠设置 `TMPDIR` 绕过，应先单独审阅／修改验证工具，或在允许该行为的隔离环境复验。本文保留真实限制，不把命令写成可在任意 scratch 策略下直接运行；本次文档任务未执行该工具。

## 已有证据及其边界

[测试报告](../../plugins/hermes-scoped-memory/docs/测试报告.md)记录：临时副本中的宿主测试 **45 passed／0 failed**，实际插件两路集成 **2 passed**。普通未打补丁宿主上的这两项会明确 skip，不能把 skip 算接线成功。

已有集成覆盖辅助任务归属、profile／卸载拒绝、8 并发实时身份、环境伪造、无 origin 后台身份为空，以及真实 provider 经 RPC 的归档／合成候选提取。它证明临时副本里的合同，不证明生产网关加载、真实模型效果、云端数据政策或 QQ 多账号隐私。本文改写没有重跑这些测试，也没有修改真实宿主。

## 应用仍需独立部署审阅

本目录不会自动：

- 安装或启用任何插件、应用到真实宿主、重启网关；
- 注入带记忆隔离 worker 所需的完整私人 turn 回调；
- 设置公共事实确认／决议名单、外部费用 checkpoint 或删除恢复保留策略；
- 扩大 QQ 白名单、改变旧 `qqbot`、复制私人 SOUL／MEMORY／USER 或授权云出站。

真实部署须另行确认补丁版本、受限社交执行域与单一 OneBot 连接所有权，验证 provider 失败不会回落私人 agent，并完成真实模型／QQ、压缩与重置、重载、取消、故障及恢复验收。普通宿主已有上下文缓存／摘要没有被本补丁全局改造，不能据本地删除声称完整“忘记”。

完整剩余项见 [实现与验收](../../plugins/hermes-scoped-memory/docs/实现与验收.md)、[后续需求处理](../../plugins/hermes-scoped-memory/docs/后续需求处理.md)和[开发状态](../../plugins/hermes-scoped-memory/docs/开发状态.md)。
