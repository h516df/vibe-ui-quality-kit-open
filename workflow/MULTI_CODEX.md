# Codex 与本地执行者协作

协作规则 0.5.3 / 简版 r8；协调器协议 1。适用于多个 Codex 云端任务、本地 Codex 与 DSH 参与同一项目，也支持先后接力。由代理操作，用户无需填写任务表、搬运聊天或记分支。保留既有文件名和协议以兼容已接入项目；此规则不授权自动创建子代理、定时任务或收费服务。

每个项目沿现有记录确认一个结果负责人，负责分工、跟踪、收回结果、整合验收和收尾。任务执行者与负责人可以是同一会话，普通任务连续完成即可；不能为了凑齐三个执行者强行并行。源码和重测试优先已有云端，依赖本机文件、运行软件或真实设备的部分才沿已有授权本地通道处理。owner/workspace 表示会话/工作区身份，不绑定模型或软件名；消息派发、旧写手移交与设备操作继续遵守对应授权和保护边界。缺少通道先完成独立部分，不另造桥接或让用户成为技术中转。

## 先做什么

1. 读取本项目 AGENTS、需求池、相关 PR 和对应分支交接；检查正在运行的协调登记。未登记的旧 PR 也不能视为无人修改；必要时保留现有成果，另建隔离草稿。
2. 每个活跃会话使用唯一 owner 和 workspace 标识，不把 GitHub 账号当成会话 ID。不同云机器上相同的 /workspace 路径并不表示同一工作区；标识应包含任务实例随机 ID。
3. 相关任务由一个 batch 关联，每项任务有独立 task、分支、基线和最小合理文件/目录 scopes。并行任务用不同工作区/分支；顺序接力确认上一写手停止后才沿用分支。
4. 原子领取成功并读回所有权后再编辑。先有隔离分支上的准备工作时，不把它当作已获共享范围或发布资格。扩大范围先执行 expand，冲突时调整分工或串行，不擅自接管。

代理负责根据需求拆分；同项目不同模块可并行，共享文件由一个任务处理。不同项目使用各自登记，无须统一排队。

## 唯一共享登记

每个采用项目使用独立分支 **codex-coordination-v1**，状态文件为 **.codex-coordination/state.json**。它不是源码开发分支，不把协调分支合入 main。初始化沿用基线源码树，只添加登记；后续只改状态文件，提交带 skip-ci，不触发业务构建。

- 核心字段：project、revision、tasks、batches；任务含 owner、workspace、role、branch、scopes、base/head、status、generation、lease_until。
- source 文件/目录按边界检测包含与重叠，ui 不会与 ui-extra 混淆。分支、工作区也不能由两个保留任务占用。
- active / paused / ready 都保留范围。租期到期会阻止继续写入，但不自动释放，避免失联会话恢复后覆盖成果。
- 公共 HANDOFF、需求池和发布入口由一个整合任务汇总。工作会话维护自己 PR 或 docs/tasks/<task-id>.md，不同时反复改一份总交接。

## 可执行工具与两种通道

通用源：[scripts/coordinate.py](scripts/coordinate.py)。新项目采用工具会复制为 cloud/coordinate.py；已接入项目审阅差异后补入，保留原自定义脚本/配置。

### 有已配置 Git 认证的云端

代理使用现有认证，不向用户索要 token，不自动登录：

```bash
python3 cloud/coordinate.py git-init --project owner/repository
python3 cloud/coordinate.py git-status
python3 cloud/coordinate.py git-apply --request /tmp/task-request.json
```

git-init 是显式、只创建操作，已存在则拒绝。工具以临时 Git index 构造状态提交，不 checkout 协调分支，不改源码工作区、当前分支或暂存内容。Git 更新使用限定在协调 ref 的 **--force-with-lease=<ref>:<确切旧SHA>**；初始化要求旧 ref 不存在。业务分支不使用此写入方式。

Git 通道对 ready 使用远端实际 head 和完整 git diff 核对范围；verify 核对每个就绪成员的远端 head 及其是整合源码的祖先。测试回执必须绑定确切整合提交；测试结果仍由实际检查产生，不能凭登记伪造。

### 当前环境仅有 GitHub / Make 连接器

工具的 plan 模式与 Git 通道共用同一状态转换逻辑：

```bash
python3 cloud/coordinate.py initial --project owner/repository
python3 cloud/coordinate.py plan --snapshot /tmp/snapshot.json --request /tmp/task-request.json
```

initial 只输出空状态，plan 只输出提案，不写 GitHub。snapshot 是 {"head":"协调提交完整SHA","state":{...登记...}}。代理通过当前已授权连接执行：

1. GET 协调 ref，得到 H；按 H 读取状态和 commit/tree，不能用两次变化中的分支读取拼出快照。
2. 运行 plan；检查 changed、expected_head、parent_sha、state。check 是只读结果，不能创建状态提交。
3. create_tree 以 H 的 tree 为基础，只替换状态文件；create_commit 的唯一父提交必须是 H。
4. update_ref 指向新提交，**force=false**，并传 expected_sha=H。两位写手从同一 H 产生不同子提交时，服务器只接受先到的一个，另一个因非快进被拒绝；禁止改成 force=true 强行通过。
5. 读回 ref 和状态，核对 owner/generation/租期。若被拒绝或回执不确定，先读取当前状态，确认是否已成功；重新运行转换再决定继续或报告冲突，不重放旧提案。

初始化必须采用只创建 ref 的 POST /repos/{owner}/{repo}/git/refs（已存在返回冲突）或 Git 通道的空 expected lease；不要用会覆盖已有分支的 upsert 辅助函数。

连接器模式下代理必须补齐 Git 通道的外部证据：ready 的实际远端分支 head 和完整变更路径；verify 的每个就绪成员远端 head、成员提交确实进入整合提交的祖先关系；check publish / complete 时再次核对分支 head 未变。不得使用截断的文件清单做范围验证，也不把成员分别通过当作整合后通过。

## 操作与请求例子

领取一个任务（其他操作带返回的 generation 和原 owner）：

```json
{
  "action": "claim",
  "task": "menu-motion",
  "owner": "codex-session-unique-id",
  "workspace": "cloud-instance-worktree-unique-id",
  "batch": "interaction-01",
  "branch": "codex/menu-motion-unique-id",
  "base": "完整40位基线SHA",
  "scopes": ["ui/menu.js", "ui/menu.css"],
  "ttl": 3600
}
```

| action | 使用要求 |
| --- | --- |
| check | 编辑/推送/交付前核对 owner、generation、有效租期及 paths；publish=true 另需整合者与匹配回执 |
| renew | 有效租期内在长任务关键节点续租；不逐行请求、不后台轮询 |
| expand | 编辑新范围前扩展 scopes；仍需无冲突 |
| pause | 先推送可保留成果、保存 head/handoff；保留占用 |
| resume | 原 owner 重新核对远端 head 后恢复暂停/就绪/过期任务；已封批成员不能自行改 head |
| handoff | 原 owner 明确写手已停止，保存 head/handoff，新 owner/workspace 和代次；同分支只用于确认已停的顺序接力 |
| recover | 仅暂停或过期任务，核实旧写手已停并说明原因；新 owner、分支、工作区，保留旧分支与历史，不因超时自动抢占 |
| ready | 工作已保存，实际变更在范围内；冻结 head，保留占用等待整合 |
| verify | 整合者核对所有就绪成员 head、祖先关系和绑定整合 SHA 的通过检查 |
| complete | 同一已验证整合 SHA 完成后，成员标 integrated 并释放占用；若发布，另附匹配源码的实际 publication 回执 |
| abandon | 明确停止且保留原成果/理由；取消整合会重新打开批次，成员仍 ready，不删除分支 |

owner/generation 是协作检查信息，不是身份认证密钥。尚未确认旧写手停止时保持保留状态，继续独立任务；确需人工确认只说明这个具体缺口。

## 一个整合者，集中交付

工作成员都 ready 后，单独的 role=integrator 任务领取 @integration 和 @publish。工具在一个项目内串行整合/发布，并封闭该批，防止边整合边加入新工作。它使用自己的独立分支/工作区，合入本批成员的确切提交；可在就绪成员的范围内解决整合问题，额外范围先 expand 并检查冲突，不顺带修改无关源码。Git 通道 verify/complete 也检查整合后的实际路径；无关 PR 不自动合并。

verify 的 members 必须与当前 ready 任务的 head 完全一致，checks 必须 passed 且 source_sha 等于整合提交。源码变化会使旧检查失效；重新检查后才能获得发布检查通过。check publish 只给协作层资格，不执行发布、不新增公开发布授权。实际平台发布成功后才记 publication；没有发布则仅记 integrated。

优先保留成员提交的 merge 关系，使祖先核对可执行；已 squash 的结果不能凭相同标题当作成员已进入整合。既有保护要求和项目检查继续适用。

## 已验证范围与限制

- 有隔离真实 bare Git 远端、两个独立工作区的竞争更新测试；覆盖重复任务、路径/分支/工作区冲突、过期不抢占、移交代次、源工作区保护、合入两方成果以及变更后旧检查失效。
- 本账户本轮在通用库和 TimeWorth 初始化专用协调分支，并用 plan + GitHub 连接器登记本次真实工作任务，后续结果回读见相关 PR/交接。
- 这是合作式协调，不是 GitHub 权限隔离或跨会话进程管理器。所有写手必须读取并遵守入口；无法阻止持有仓库写权限的人绕过工具直接推送，也不能强制停止用户另开的 Codex。
- 新环境缺少登记访问时可继续独立草稿/检查，明确尚未获共享写入或发布资格；不声称所有新会话天然继承连接，亦不把本方案当作账户级全局设置。
- 计费/Actions/安卓更新等外部能力仍单独处理；本方案不引入服务端、付费数据库、自动子代理或定时任务。

依据：[Git 精确 expected lease](https://git-scm.com/docs/git-push)、[GitHub refs](https://docs.github.com/en/rest/git/refs)。状态提交采用原子比较更新，普通 issue 领取备注不能替代这一机制。

## 同一交付目标的纠正

同一目标未交付完整时，优先保留当前活跃批次、任务分支和 PR，按既有角色/租期/范围规则继续；不要把中间文件、暂时绕路或只修正说明就结批为完成。整合后需要修正时，在整合者已有或已 expand 的范围处理并重验，不能回写冻结成员。已完成任务按协议新建关联接续，不抢占旧分支；不因此默认创建子代理。交付判定使用 [DELIVERY](DELIVERY.md) 的用户目标与实际回读。
