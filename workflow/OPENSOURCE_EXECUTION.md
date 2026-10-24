# 开源实施能力：Superpowers 内联执行

用于已有多步骤计划、反复接续或容易漏做的实施任务；清楚的小改仍直接沿项目原流程完成。用户不用选择技能、记命令或在任务之间说“继续”。

## 已核对的来源

- [obra/superpowers](https://github.com/obra/superpowers/tree/2ce8aa15d05bd57213e6f746f953aafb7ded7a8b)，固定提交 `2ce8aa15d05bd57213e6f746f953aafb7ded7a8b`，MIT；这是经过隔离试用的提交快照，不宣称为其最新正式发行。
- [executing-plans 原版](https://github.com/obra/superpowers/blob/2ce8aa15d05bd57213e6f746f953aafb7ded7a8b/skills/executing-plans/SKILL.md)：同一个实现者连续执行计划，用任务原文与进度记录减少接续遗失，末尾复核。
- [task-start / task-done](https://github.com/obra/superpowers/tree/2ce8aa15d05bd57213e6f746f953aafb7ded7a8b/skills/executing-plans/scripts)：提取任务和基线，运行项目命令，失败不记 complete。
- [共享辅助脚本](https://github.com/obra/superpowers/tree/2ce8aa15d05bd57213e6f746f953aafb7ded7a8b/skills/subagent-driven-development/scripts)：sdd-workspace、task-brief、review-package；保持相对目录，不只复制一个孤立脚本。
- 按需要读取同提交的 test-driven-development、systematic-debugging、verification-before-completion、requesting-code-review、using-git-worktrees、finishing-a-development-branch 及其引用。取得/复制源码时保留 [LICENSE](https://github.com/obra/superpowers/blob/2ce8aa15d05bd57213e6f746f953aafb7ded7a8b/LICENSE)，不修改上游原文件来冒充原版。

## 由代理接入，不增加用户步骤

1. 先恢复项目 AGENTS、用户最近纠正、原计划、真实入口及写手；需要规划时复用已有规划能力，原计划可用则继续，不建立第二份需求池。
2. 用现有授权工具在隔离的工具目录取得该固定提交，核对 Git 提交/文件身份与引用。不安装全局 bootstrap、hooks、新后台或修改模型配置；已安装且身份一致则直接复用。云端不能访问本机路径，不把本机安装当成云端已加载。
3. 默认沿用户的单写手方式运行内联路线。代理按技能读取任务原文并执行真实项目测试，失败定位后修复，不在每个任务后要求用户批准继续。关键目标不明、授权缺口和必须本人操作仍集中询问。
4. 使用 upstream 的 task-start / task-done 保存当前计划的辅助进度。接续核对 plan-path、progress.md 和其中实际 Git 提交，再继续首个未完成项；这些脚本不会自动替代理选任务、派发模型或核验写手。
5. 末尾按风险采用一次独立只读复核，检查整个任务范围和真实使用入口。遵守项目原有协调、发布、签名、数据与授权边界；既有 Git 授权继续有效，不因上游泛化提示追加技术审批。

## 适配边界

用户当前规则优先：不自动更换模型/推理档位，不机械对小改启用计划、TDD、全量重复测试或每任务一套代理。长期决定和结果仍保留在现有 HANDOFF/Issue/Git；辅助目录不是新的长期任务库，不自动清理未知成果。

task-done 只根据所运行命令的退出码记录进度。它不能证明断言质量、真实 UI、手机、发布包或用户满意；提交后变化也不能继续引用不适用的旧检查。命令、输出、进度与审阅包不得含凭据或私人数据，公开提交前排除机器路径和本地试验日志。

## 本轮证据与未验

2026-10-23，在 Windows / Git Bash 的独立合成项目中实际使用 5 个原版脚本完成两项任务：主题保存并跨进程读回、无效输入拒绝且原数据不变。两项均先失败后通过；失败时不写完成项，成功后各记一次；生成了包含两次提交的整体审阅包。19 份上游原文件逐个核对 Git blob SHA1，MIT 原样保留。

此证据不等于真实业务项目、云端新会话、UI/手机或无人值守服务已验收。本机 Codex/DSH 的既有 vibe-delivery 路线接入情况以本轮交付回执为准。

## 其他候选

读过 [GSD 自主推进](https://github.com/open-gsd/gsd-core/blob/v1.18.0/docs/how-to/run-phases-autonomously.md)、[OpenSpec](https://github.com/Fission-AI/OpenSpec)、[Symphony](https://github.com/openai/symphony) 与 [BMAD Loop](https://github.com/bmad-code-org/bmad-loop)。GSD 的 npm 取得超时、Git 取得出现 TLS 中断，未安装成功；没有把它加入生产入口。其他框架的规划/运行设施不在本次安装范围，避免替换已有系统。
