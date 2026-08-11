# GitHub 发布审计：0.3.2

审计日期：2026-08-11

## 自动扫描

执行了仓库文件与 Git 历史的上传安全扫描，并将单文件扫描上限提高到 100 MB，以覆盖随发布包提供的离线词典和翻译模型。

- `BLOCK`：0
- 未扫描文件：0
- Git 历史：已完整扫描
- Git 提交邮箱：未发现非 noreply 邮箱
- GitGuardian：未运行（本机没有该命令行工具）

## 人工复核

自动扫描留下的 `REVIEW` 已逐项复核：

- 代码中出现的 `name` 都是 PyInstaller、线程或应用记录的程序字段，例如 `SpeechPlayer`、`TrayIcon`；不是人员、地址或账号信息。
- `resources/ecdict.db` 是公开的 ECDICT 英汉词典，数据库结构只包含词条、音标和释义。项目的第三方说明已记录其 MIT 许可证和来源。

结论：上述项目可作为“大声发划词翻译”0.3.2 的公开 GitHub 发布内容。

## 发布包

- 文件：`DaShengFaTranslator-0.3.2-Windows-x64.zip`
- SHA-256：`F97927164E87EB044F6022D8B860B4306EA915278E119D2135BA98962C87E43F`
