# 安全边界

当前真实 QQ 基本通道已使用；这不代表新多人路由或记忆隔离已验收。下文是现有安全约束，当前证据与待做边界见 [开发状态](docs/开发状态.md)，下一版方案见 [接入与隔离设计](docs/接入与隔离设计.md)。自有文档采用 MIT。

## Fail closed

默认 `group_admission=allowed_users`，精确用户白名单语义保留。显式 `all_members` 只开放 `allowed_groups` 内真实成员的公共输入，要求 strict、公共 bridge 合同1及匹配 host／worker 合同4；旧含记忆 bridge 不满足此合同，拒绝连接及执行。普通成员DM、非白名单群、自发机器人及身份不一致仍拒绝。默认真实@，显式 name_or_mention 可接受原始正文中的“示例呼唤名”；附件、引用正文、昵称／名片和伪CQ不触发。owner 仅由已校验数字 ID 与配置 owner_id 相等确定，群管理员／同昵称／允许DM都不能成为主人；群内主人也无管理能力。

`self_id` 同时用于连接后 get_login_info 校验和每个事件的身份检查；拒绝自身 user_id 与 message_sent/request 等事件。普通聊天只接收 message；可选媒体服务另行检查 group_upload/group_recall notice，上传通知不会自动聊天。未经验证的连接事件不进入队列。数组格式才能表达真正的 at/reply；用户字面 CQ 文本永远不是控制码。拒绝非好友私聊、错误/缺失 ID。

密钥只从 `NAPCAT_ACCESS_TOKEN` 获取，配置中的 token/access_token 字段直接报错。禁止 URL 内密钥、query、fragment、非 loopback IP 与重定向；trust_env=False，禁止环境代理泄露。代码不打印 token/远端响应/原始异常字符串。不要把 NapCat 13011 或管理端口暴露公网。容器宿主映射也必须绑定 127.0.0.1。

`extra.isolation.mode: strict` 要求受信任 `IsolationBridge`；普通 factory 仅在显式 runtime host_llm／contract4 时绑定正式实现，其他 strict 配置缺 bridge 连 OneBot 也不连接。connect 先验证实际宿主 facade／生命周期和真实 sandbox probe，失败不回落私人 handler。社交 worker 只读挂载专用配置文件、无网络／凭据，prepare 合同不能指定任意 messages／model／target；宿主构造固定 system 和本轮公共身份，通过正式 ctx.llm trust gate 单次调用，不加载私人 agent／记忆。昵称来自当前事件，JSON数据经worker校验和digest绑定，不插入system；恶意或缺失昵称回退公开QQ昵称或“群友”。配置自述不能代替实际验证。本阶段只做离线验证，父任务负责现场部署，详见 [群权限与昵称](docs/群权限与昵称.md)。

严格路由仅主人精确私聊进入私人域，主人群聊也走社交域；访客私聊另有显式清单。回复能力只限本轮来源和原消息，dispatch 退出或取消时撤销；后台复制的 ContextVar 不延长能力。向已有允许群发送并不代表内容安全，bridge 仍须防止私人上下文进入社交执行域。legacy 行为保留既有权限边界。

## 已知限制

- legacy 或主人 native DM 的授权用户可能使用宿主工具，须只授权信任的身份。strict 公共 factory 不构造工具循环或私人 agent，所有群成员只有本轮公共问答能力；新群开放不能使用 legacy 或旧含记忆执行域。
- 群名片／昵称和资料仍是不可信输入，不能保证模型内容永远遵守称呼要求；代码保证其不能改变system模板、角色、目标、工具与路由，也不加载私人资料。真实输出质量仍需QQ验收。
- 若 NapCat 或其 token 被攻陷，攻击者可以伪造协议用户 ID；插件不能加密认证单个 QQ 成员。宿主、NapCat 和 QQ 账号均在信任域。
- 默认入站媒体不取 URL、不读路径、不附原始消息。可选 strict v2 媒体服务新增了下载面：默认 `allowlist` 精确 HTTPS host，显式 `public_https` 取消域名过滤；两者均保留 HTTPS／443、TLS、DNS 地址绑定、公网地址校验、禁代理／重定向／压缩、累计字节限额；专用共享出口使用 dirfd／O_NOFOLLOW。取消域名过滤不等于没有网络／解析风险。解析无网络和私人 home，失败不降级宿主解析。新下载策略的真实 CDN 与 QQ 客户端、容器出口或 QQ 语音解码本阶段未验证；[完整边界](docs/媒体实现与验收.md)。普通聊天 URL 不成为媒体抓取入口，其宿主工具网络策略另行管理。
- 媒体默认关闭，需要收窄的群／类型清单、strict bridge v2 及独立出站许可；原文、图片识别和转写不能调用管理命令、修改 ACL 或自动写记忆。媒体轮不持久化进记忆／历史；最终回答沿用原对话模型。取消只停止未完成任务，不能撤回已传云的数据。
- 媒体模式的普通文本、取消控制与媒体处理采用分别有界的分发；普通模型等待不能阻塞同一OneBot消费者处理撤回／取消，本地准入probe也不占模型semaphore。控制仍须精确来源和域准入，不是第二个未认证入口。队列过载可丢新项，不承诺可靠投递。
- 媒体候选在请求入队时冻结来源、scope与TTL，缺失／歧义结果也保持；执行时重新验证，失效不换成后来的附件。撤回依赖在排队时登记；同条附件与明确引用同样保留执行时检查。
- parser／worker使用共享ProcessOwner；recall、disconnect和后续重复取消不能丢弃启动句柄或打断唯一回收任务。回收在原调用／槽位内join，完成后传播取消，正常清理5秒、后备关闭核验2秒；确认进程退出、stdin关闭和stdout EOF后才正常返回。无法确认则报固定错误并拒绝继续启动，不把断开记为成功。R1／R2／R3历史基线已独立验收，新 factory 下也保留真实重复取消回归。
- 辅助云模型主读取 `OPENROUTER_API_KEY`；仅在标准变量不存在时兼容 `NAPCAT_MEDIA_OPENROUTER_API_KEY`，标准变量空／非法不切换计费 key。解析器无凭据。默认 local 模式仍要求完整持久化预算；只有显式 budget_mode=provider 才跳过本地费用闸门，供应商额度需父任务核实。未知计费和 provider 错误不自动重试；并发／字节／超时／权限限制保持。原对话模型的费用由宿主部署管理。
- 有界内存队列与短期去重，不提供持久事件/出站队列。发送拒绝、断线及部分／未知投递不走宿主纯文本 fallback，也不自动重发；未知结果须由人核对。strict 启动通知缺少当前回复许可，统一安装明确关闭该通知，不扩大许可。
- v0.1 不做跨进程 token 锁。不要多个网关/profile 同时连接同一机器人，以免重复响应。重连阶段宿主状态显示可能滞后。
- 插件和暂存脚本不自行安装到 active profile 或启动／重启。仓库[统一安装器](../../INSTALL.md)默认只读计划；用户要求安装后用 `--apply` 写入、用 `--restart` 请求正式激活，不绕过自重启保护，不接管官方 qqbot。真实账号的消息验收仍须在授权目标内另行完成。

## 报告

请私下向部署维护者报告漏洞。提供版本、脱敏配置和最小复现；不要公开 access token、QQ 登录缓存、二维码、群成员身份、宿主密钥或完整私人聊天日志。
