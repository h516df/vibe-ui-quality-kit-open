# 许愿式开发工具库

把需求讨论、实施、验证、GitHub 交付和接续连起来的轻量资料与 Python 标准库工具。

**[从通用工作流开始](workflow/README.md)**。由代理理解目标、选择成熟方案、实现和自检；用户提供需求与使用反馈。检查按改动风险执行，复用有效结果，相关结果通过后交付。

- [工作流与完成条件](workflow/WORKFLOW.md)
- [界面设计方向](workflow/STYLE.md)
- [只读接续、项目接入与检查工具](workflow/scripts/workflow.py)
- [发布核验](workflow/DELIVERY.md)
- [隐私检查](workflow/PRIVACY.md)
- [云端开发环境](CLOUD_DEVELOPMENT.md)

本开源版从当前通用源码建立干净历史，未包含本机恢复快照、旧任务记录或私人配置。原私有恢复历史继续保留，后续通用功能在本库维护。使用标准 GitHub-hosted 执行机；仓库免费执行不代表模型、Codespaces 或第三方服务免费。

## 许可与上游

本库原创工具、模板和说明采用 [MIT](LICENSE) 许可。第三方软件遵循其原许可证。

`Install-Impeccable.ps1` 从 [Impeccable 官方版本](https://github.com/pbakaus/impeccable/tree/5bf0a069dc857e1b5f4b090e004d9d5b231416ca) 取得固定版本并核对 SHA256；本库未分发其二进制或代码快照。DSH 注册仍要求原有私有检查点及明确参数，不在 CI 或开源初始化时执行安装。

这是开发辅助工具库，不能把脚本检查、工作流同步或发行元数据当作业务 App 的功能和设备验收。
