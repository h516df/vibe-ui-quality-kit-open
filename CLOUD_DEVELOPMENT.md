# 云端开发 · vibe-ui-quality-kit

自 2026-10-21 起，后续源码开发、测试与构建默认在云端执行。

[在浏览器创建开发工作区](https://codespaces.new/h516df/vibe-ui-quality-kit-open)。选择本仓库的开发容器；首次创建会安装固定主版本工具与项目依赖。用完停止工作区，避免持续计费。也可以在 Codex 云端拉取本仓库，运行 `bash .devcontainer/setup.sh` 后使用同一检查入口。

```bash
bash cloud/check.sh
```

GitHub Actions 的 **Cloud development** 在 PR、main 推送及手动运行时检查。通过结果绑定当前提交；构建和模拟器通过不能替代实体手机验收。依赖与缓存可重建，代码必须提交到分支并推送，临时工作区不是长期资料备份。

## 当前边界

云端只维护恢复资料与脚本；本次不执行 Recover 或安装器，不接触正在运行的 DSH。

原 README、AGENTS 与交接文件中的 Windows 路径作为历史恢复线索保留；云端开发不依赖这些路径。生产运行与发布步骤按本节边界处理，不自动运行旧的本机派发、安装、隧道或签名脚本。凭据通过平台 Secrets 注入，不能进入源码、日志或构建附件；私有项目继续保持私有。

## 配置依据

- [Dev Container 规范](https://containers.dev/implementors/json_reference/)
- [GitHub Codespaces](https://docs.github.com/en/codespaces/overview)

## 会话接续

### 新会话从哪里开始

本仓库内的约定和已推送的文件是接续依据。新会话不会自动继承旧聊天、未推送改动、上一台云机器的临时工具或登录态。

1. 读取根目录 [AGENTS.md](AGENTS.md) 与 [HANDOFF.md](HANDOFF.md)，再读已有项目规则和任务相关文档。
2. 核对远端 main、当前分支、工作区状态与相关 PR。HANDOFF 里的历史分支/测试结果需和 GitHub 当前状态逐项核对，不能直接当成最新状态。
3. 如果上次工作尚未合并，打开交接指定分支或 PR；不要只取 main 后重新实现一遍。一个分支对应一个进行中的任务，共享文件避免并发覆盖。
4. 工具链缺失时按本文现有初始化方式恢复；业务验证继续使用现有检查入口。不要自动运行历史恢复、生产部署、发消息、签名或发布命令。

### 结束前必须保存什么

- **长期约定**：更新 AGENTS 或已有项目规则，区分用户明确要求与代理建议。
- **当前进度**：维护 HANDOFF 顶部“当前接续记录”，替换过时状态，保留下面原有历史和证据链接。不同任务的未合并进度留在各自分支/PR，合并时整合当前记录。
- **交接最少字段**：当前目标；已完成和待完成；仓库与分支/PR；基线或验证提交；运行/测试命令及 passed / failed / not run；所需配置的名称与来源；阻塞；下一步。
- **远端副本**：提交本任务源码与交接，推送独立分支，再核对 GitHub 上的提交 SHA。PR 尚未合并也可继续，但必须告知准确分支/PR；不要仅保存到旧会话的磁盘。
- **交付回执**：给用户仓库、分支/PR、远端提交 SHA 与简短接续摘要（不要求用户复制）。远端未确认前不能说“可以删除旧会话”。

只写配置的键名与申请位置，不写值、个人资料或私人日志。未推送的文件、任务 VM 和 Actions 临时附件不作为唯一长期备份。

### 环境与凭据

本仓库的开发容器和初始化脚本用于恢复工具链；它们不会自动把当前任务变成所有会话共用的机器。若需新 Codex 任务自带已安装工具，必须在 Codex Cloud 环境的编辑流程中准备、验证并 Publish / Republish；仓库提交不等于环境已发布。

密钥通过所选环境的变量/Secret 配置提供；GitHub Actions Secrets、Codex Cloud 凭据和旧电脑登录态不是同一处配置。本轮只建立仓库接续方式，没有读取或迁移凭据，也不声称已修改 Codex Cloud 的已发布环境。

### 代理负责接续

用户无需粘贴启动提示、记分支或重复授权已有连接。代理按本次意图读取 AGENTS 的统一入口、需求池与当前 HANDOFF，查询相关 PR 和分支状态；缺少会改变关键含义的信息才集中询问。没有固定开工口令；明确实施请求直接推进，讨论与零散反馈先收集。

参考：[Codex Cloud 的环境复用和任务保存机制](https://learn.chatgpt.com/docs/environments/cloud-environments#reuse-and-update-saved-state)。

## 可复用工作流

用户已授权维护 `workflow/`。该目录可在云端通过 `python3 -m unittest discover -s workflow/tests -v` 独立检查；不运行 Recover、安装器或 DSH。CI 的 Reusable workflow checks 检查这一目录，原有恢复脚本检查保留。接入说明见 [workflow/README.md](workflow/README.md)。

## 通用工作流入口

通用约定与模板统一维护在 [vibe-ui-quality-kit/workflow](https://github.com/h516df/vibe-ui-quality-kit-open/blob/main/workflow/README.md)。开始任务由代理主动读取；本仓库 AGENTS 内保留可独立执行的简版，跨仓库读取暂不可用时先沿用，不要求用户重复说明工作流或添加仓库。具体工具链、数据、签名、生产与设备保护仍以本项目约束为准。

新任务从默认分支开始时，代理应按用户描述核对相关未合并 PR、依赖和对应分支交接，不能直接把 main 当作所有最新成果；不根据旧快照自动合并业务 PR。这里的仓库配置不代表 Codex 账户级环境已发布。

## 多 Codex 领取与整合

[多会话规则](workflow/MULTI_CODEX.md) 与 [协调器](workflow/scripts/coordinate.py) 由代理使用；共享状态在本仓库 codex-coordination-v1 分支的 .codex-coordination/state.json。该分支不合入 main。新会话先看状态与相关 PR，不把 GitHub 账号当会话 ID；独立分支/工作区，先领取范围，移交确认旧写手已停。工作任务保存各自记录，登记的整合者汇总公共 HANDOFF 并集中验证/交付。

现有 `python3 -m unittest discover -s workflow/tests -v` 同时检查接入器与协调器，测试只使用隔离 fixture/bare Git，不写用户仓库；真实仓库的登记读写单独回读。源码 push 的必要检查结果与协调状态提交（skip-ci）分开，不借状态提交伪造功能通过。
