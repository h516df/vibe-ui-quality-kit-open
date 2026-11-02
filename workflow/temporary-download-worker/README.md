# APK 一次性下载：Cloudflare Worker + 私有 R2 + D1

**当前状态：仅源代码与离线验证，未上线。** 不存在已经实测可用的 60 分钟直链，也没有修改任一 Android App 的构建、签名或正式发布入口。

这是现有 `workflow/TEMP_DOWNLOAD.md` 交付契约的一个**可选执行组件**，不是独立任务队列、第二套发布系统或 Super-agent。先在一个真实 Release 上手动受控试运行，通过后才考虑与原发布流程按需串接。

## 能力与边界

- **独立托管**：仅从私有 R2 存储流式读取 APK，成功下载直接返回 `200` 和 APK 字节，无 GitHub 代理、跳转或前端下载页；保留原 APK 字节和正式 GitHub Release。
- **固定 60 分钟**：D1 对每个令牌存毫秒 `issued_at` 和 `expires_at=issued_at+3600000`，服务端时钟大于或等于到期时间时拒绝；无需定时清理才能过期。
- **原子核销**：D1 使用单条 `UPDATE ... WHERE consumed_at IS NULL AND expires_at > ? RETURNING` 领取。两个并发 Worker 最多一个取得完整 `200` 响应；第三次返回 `410`。只存令牌 SHA256，不存明文。
- **兼容性**：下载前 `HEAD` 返回只读元数据，不消耗令牌。忽略 `Range` 请求并只提供完整 `200`（不支持断点续传）；Android 下载管理器和浏览器仍须用真实设备验证。
- **内容保护**：注册前操作端比对正式 APK 与从 R2 回读的文件 SHA256。D1 绑定对象大小、SHA256、R2 ETag；对象缺失或 ETag 变化时故障关闭，不消耗令牌。
- **权限**：管理端点仅接受强随机 Bearer secret；签发请求必须提供稳定 `requestId`，重试不重复签发。公开端点仅 `/d/<token>` 与健康检查，所有响应禁止缓存。不要将令牌放入任何 GitHub 日志、公开工单或命令行参数。

真实网络中无法可靠知道手机是否**成功将全部字节保存到磁盘**。保证是“最多一次可获得完整 HTTP 200 响应机会”：连接在流式传输中断时也可能已核销，需运营端另签新链接。聊天软件的 GET 链接预览/安全扫描也可能抢先消耗，交付时尽可能使用不触发预取的方式；不把此风险描述为已解决。

## 不涉及付费的准备

1. 需要实际可用的 Cloudflare 账号以及 Worker、R2、D1 权限。R2 激活可能涉及本人付款资料验证；未获许可不得添加付款方式、选择付费计划或购买域名。可先在免费额度内评估，但不能承诺永远免费。
2. 在云端受信任的临时工作目录执行下列命令，**不要在性能较弱的本机执行 APK 下载、校验和云端部署**。账号登录/令牌配置只走 Cloudflare 官方登录与 GitHub Secret，不要把令牌发到聊天。
3. 只允许在新建的 `apk-once-*` 资源上操作，避免修改现有账号内其他服务。密钥必须是 32 字节以上的随机值；`wrangler.jsonc` 中的 `PUBLIC_BASE_URL`、D1 `database_id` 均为占位符，部署前替换为真实值。

参考官方文档：[R2 Worker 绑定](https://developers.cloudflare.com/r2/get-started/workers-api/)、[D1 创建与远端执行](https://developers.cloudflare.com/d1/get-started/)、[Wrangler Secret](https://developers.cloudflare.com/workers/configuration/secrets/)、[Workers Free 限制](https://developers.cloudflare.com/workers/platform/limits/)。

## 首次部署（须先有账号授权）

### 推荐：手动触发的 GitHub Actions 首次部署

已有 `.github/workflows/apk-once-bootstrap.yml`，**仅 `workflow_dispatch` 可启动**，不会跟随普通开发 PR、push 或 Android Release 运行；没有授权密钥时不会调用 Cloudflare。由用户在 `h516df/vibe-ui-quality-kit-open` 的 Settings → Secrets and variables → Actions 中一次性录入：

- `CLOUDFLARE_ACCOUNT_ID`：从 Cloudflare 控制台读取的 32 位账号 ID。
- `CLOUDFLARE_API_TOKEN`：Cloudflare API Token，临时限定此账号，权限仅为新建/部署 Worker 的 Workers Admin、创建 D1 的 D1 Write、创建/读写 R2 的 Workers R2 Storage Write。设置短期有效期；不要使用 Global API Key，不要给整个 Cloudflare 组织的其他账号权限。
- `APK_ONCE_ADMIN_TOKEN`：独立、安全随机的至少 32 字节管理密钥，**不同于** Cloudflare API Token，稍后用于已部署 Worker 的 APK 令牌签发。只能保存在 GitHub Secret 和 Cloudflare Worker Secret，不能放入聊天、Issue 或 CI 日志。

由代理在 GitHub 的 Actions 页面选择 “APK one-time host bootstrap (manual, free-only)”，输入精确 `FREE_ONLY` 后手动触发。此流程先检查 Node 服务和 Python 幂等保护，再查询账号中现有资源，缺失时只新建 `apk-once-*` 私有 R2、D1 和 Worker；已存在的同名资源只接续，不删除，不开启公开 R2，最后只验证公开健康检查。Cloudflare 账号必须先启用默认 `workers.dev` 子域；若未启用则在任何写操作前停止，不擅自改账号级子域配置。部署返回的 `origin` 不包含下载令牌，可存档到任务回执。

**初次上线后及时撤销/到期临时创建权限**，后续日常部署使用仅限该 Worker 的 Editor，后续对象上传按需创建单桶最小权限。持有 Cloudflare Token 本身不等于已验证 APK 交付。涉及付费启用、绑定银行卡或超出免费额度时停止，由本人决定。

### 备用：有权限的受信任云端命令行

从本目录执行：

```bash
node tests/worker.test.js
npx wrangler r2 bucket create apk-once-private
npx wrangler d1 create apk-once-tokens
# 把命令返回的 D1 database_id 写入 wrangler.jsonc
# 同时把 PUBLIC_BASE_URL 换成实际 https://<worker>.<subdomain>.workers.dev
npx wrangler d1 execute apk-once-tokens --remote --file=./migrations/0001_init.sql
# 通过安全标准输入设置 ADMIN_TOKEN；不要打印明文。此命令会部署 Worker 版本。
npx wrangler secret put ADMIN_TOKEN
npx wrangler deploy
```

部署后先只读验证 `GET /healthz`、非 GitHub 域名和 TLS；上线环境 `PUBLIC_BASE_URL` 必须与实际请求源相同。`wrangler secret put` 可能触发立即部署，建议先设置 R2/D1 绑定与正确变量，再操作 Secret。R2 保持默认私有，不开启 `r2.dev` 公网读，也不要在对象存储公开桶权限。

## 单版本受控验收与交付

在原项目完成正式 GitHub Release 验包、签名与覆盖升级检查后，由具备最小 Cloudflare 权限的云端操作端完成以下流程。**所有 `issued*.json` 只保存于可信临时目录，权限 0600，不上传 GitHub 或 Actions artifact。** 使用带审计的实际发布 SHA、asset 名称与摘要；示意命令中的版本和路径不能冒充实测。

```bash
# 在云端拿到经过现有发布门禁验证的 APK，例如 /tmp/original.apk
# 只上传到新的版本专用 R2 对象键，不覆盖已有版本对象
npx wrangler r2 object put apk-once-private/jiyixia/v0.23/app.apk \
  --file=/tmp/original.apk --remote
npx wrangler r2 object get apk-once-private/jiyixia/v0.23/app.apk \
  --file=/tmp/r2-readback.apk --remote

# 将以下 env secret 仅注入可信进程；不要使用 shell 的 echo 将值写进日志。
# export APK_ONCE_ADMIN_TOKEN=<从安全凭据渠道注入，不写进仓库>
python3 apk_operator.py --endpoint https://YOUR-WORKER.workers.dev register \
  --apk /tmp/original.apk --r2-readback /tmp/r2-readback.apk \
  --object-key jiyixia/v0.23/app.apk --filename jiyixia-v0.23.apk

python3 apk_operator.py --endpoint https://YOUR-WORKER.workers.dev issue \
  --object-key jiyixia/v0.23/app.apk --kind canary \
  --request-id RELEASE_CANARY_UNIQUE_STABLE_ID --output /tmp/issued-canary.secret.json
python3 apk_operator.py --endpoint https://YOUR-WORKER.workers.dev canary \
  --issued /tmp/issued-canary.secret.json

# 单独签发过期测试令牌：真实经过 60 分钟后才能运行 expiry 检查。
python3 apk_operator.py --endpoint https://YOUR-WORKER.workers.dev issue \
  --object-key jiyixia/v0.23/app.apk --kind expiry \
  --request-id RELEASE_EXPIRY_UNIQUE_STABLE_ID --output /tmp/issued-expiry.secret.json
# 等真实 60 分钟以上后，单次执行；不长时间占用 CI Runner：
python3 apk_operator.py --endpoint https://YOUR-WORKER.workers.dev expiry \
  --issued /tmp/issued-expiry.secret.json

# 上面测试都通过后，才为用户单独签发未消耗的 B 链接。
python3 apk_operator.py --endpoint https://YOUR-WORKER.workers.dev issue \
  --object-key jiyixia/v0.23/app.apk --kind recipient \
  --request-id RELEASE_RECIPIENT_UNIQUE_STABLE_ID --output /tmp/issued-recipient.secret.json
python3 apk_operator.py --endpoint https://YOUR-WORKER.workers.dev inspect \
  --issued /tmp/issued-recipient.secret.json
```

`requestId` 至少 16 字符，仅允许字母、数字、`_`、`-`；同一操作重试必须沿用原 ID，并在 `409` 时**不重新生成 ID 盲目签发**。用户链接 B 不用 GET 验证（否则会被消耗）；只用管理端 `inspect` 读取未消费状态、到期时间和摘要。操作端会在新文件中返回真实 B 链接，此文件不能进入可被别人读到的工作流日志。实际交付时通过用户授权的私密渠道传给用户；不能放入公共 GitHub Release 说明。

## 生产接入前的剩余门禁

1. Cloudflare 免费账户/资源权限实际可用，Worker 已部署，R2 私有，D1 远端 schema 已建立。
2. 一个真实正式 APK 的上传回读 SHA256 与原版本一致，实际流式下载对单次令牌并发试验通过（现有 Python 验收器）。
3. 真实时间经过 60 分钟后的独立令牌返回 `410`。本地伪时钟测试和 D1 SQL 测试**不能**取代这一步。
4. 在实际 Android 下载环境验证浏览器/下载管理器可下载和安装；旧数据升级验收沿原 App 流程处理，不能由 Worker 证明。
5. 以上通过后，按已有仓库发布步骤增量接入；只保留一个实际发布负责人，禁止自动工作流重复签发与重复发布。

当前所有这些**线上**门禁尚未执行。若 Cloudflare 要求开通收费或新的密钥权限，先停下，保留本实现，不绕过授权。停用服务时先停止新令牌签发并撤销现存链接，再审慎迁移/清理资源；不直接删除已有发行包。
