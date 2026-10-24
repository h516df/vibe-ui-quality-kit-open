# 新云端任务的能力核对与建仓

规则版本 0.4 · 2026-10-21。此流程由代理执行，不要求用户运行本机命令、发送 token 或重复测试工具。优先复用当前可用的官方连接，不为验证创建空测试仓库或修改计费设置。

## 先按目标做最小只读核对

1. 检查当前任务有哪些 GitHub / Make / 执行环境工具，确认账号与目标仓库。连接、运行环境、GitHub Actions、发布平台各自独立，不沿用旧会话的登录或费用状态假设。
2. 已有项目先找仓库、相关 PR 和交接，再评估读写与构建能力。只有新项目需要建仓；仓库存在时复用，避免同名或重复仓库。
3. 直接 GitHub 工具没有建仓入口时，可检查已有的 Make GitHub API shell；核对场景、连接归属、账号、输入映射和当前 scope。权限符合官方要求只说明可发起，不等于建仓成功。
4. 构建能力以实际项目工具链和一次有意义的构建结果为准。Actions 因付款/预算拒绝启动时单列托管通道阻塞，不能推断仓库/Release 必须付费或所有构建都不可用；账单无权限时不推断欠费/额度耗尽。先核对已授权云端独立构建或一次性自托管替代路线，规则见 [DELIVERY](DELIVERY.md)。不得因此调整支出上限、购买资源、把私有源码改公开或反复重试。

## 已获准创建真正项目时

先确定仓库用途与名称；用户委托代理命名时自行选择并检查是否存在。一般默认私有。解释具体目标与调用，沿会话现有授权及工具规则执行；工作流本身不新增二次开工确认，也不授权无关账号操作。

GitHub 个人建仓端点为 `POST /user/repos`，显式设置 `private: true`，需要时初始化 README。成功后读回 `full_name`、`private`、默认分支和仓库链接，才能标“已创建”。超时或不确定失败先查实际是否存在，避免盲目重建。随后写入 AGENTS 的通用入口与独立简版、项目 README/CLOUD_DEVELOPMENT/HANDOFF 和需求池链接。

新仓库能创建，不代表原 GitHub App 已获该仓库权限，或该仓库已经能被 Codex 云端选择；分开核对。若后续编辑仍可通过已有 Make 连接完成，可在授权内复用；若缺少不可替代的仓库选择/连接能力，说明具体缺口，只请用户完成平台必须本人进行的最少操作。不得绕过服务的权限边界。

## 本账户已有通道：核验快照，不是永久保证

以下 ID 仅用于定位用户已有连接，不是密钥；迁移其他账号时重新发现，不能照抄当作已授权。

- 2026-10-21：Make 区域 `us2.make.com`，私有空间 `2895912`。
- 已有按需场景 `6715378`，GitHub OAuth 连接 `11842349`；无定时任务，复用前读回配置与连接状态。
- 接口输入 `path/method/header/qs/body`，输出 `data/grantedScopes`；GET 不发送 body，查询参数用 qs。
- 已实际执行 `GET /user`，返回账号 `h516df`、scope `repo`；Make execution `03c4969bbcb343999828b9135bd175bd`。
- 官方文档确认 OAuth `repo` 支持创建个人私有仓库。**尚未实际执行建仓，不能标为成功验证；本轮也不为测试创建仓库。**
- 此连接的存在不等于所有新 Codex 任务都可访问 Make；任务启动时检查实际工具。云端 VM 不存凭据；不得把 token 写入代码、参数日志、issue 或附件。

官方依据：[创建个人仓库](https://docs.github.com/en/rest/repos/repos#create-a-repository-for-the-authenticated-user)、[Codex AGENTS](https://developers.openai.com/codex/agent-configuration/agents-md)、[Actions 计费](https://docs.github.com/en/billing/concepts/product-billing/github-actions)。接口、权限和账户限制以当前文档及实际响应为准。
