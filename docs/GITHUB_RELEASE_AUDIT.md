# GitHub 发布审计：0.4.0

审计日期：2026-08-26

## 源代码上传面

对 Git 将上传的文件、暂存版本和完整本地历史运行 `github-upload-safety`，单文件扫描上限设为 100 MB：

- 上传文件：60 / 60 已扫描；二进制文件 9 个；缺失文件 0 个；
- Git 历史：完整扫描；
- `BLOCK`：0；`INCOMPLETE`：0；
- 自动扫描原始状态：`REVIEW`，共 93 条，全部属于隐私分类；
- Git 提交身份：未发现非 noreply 邮箱；
- GitGuardian：本机未安装命令行工具，`auto` 模式记录为未运行。

93 条 `REVIEW` 已逐项复核：代码里的 `name` 均为函数参数、语音名称、线程名称或应用记录字段，不是人员、地址、账号或真实用户数据。`resources/ecdict.db` 为公开 ECDICT 英汉词典，SQLite `PRAGMA quick_check` 返回 `ok`；数据库只包含 `entries` 表，字段为单词、音标、释义、翻译、词性和词形变化。其 MIT 许可与来源已写入第三方说明。

源代码上传面人工处置后结论：`PASS`。

## Release ZIP 上传面

将最终 ZIP 解压到仓库外的全新临时目录并初始化临时 Git 仓库，对 2,962 个发布文件逐一扫描：

- 未扫描文件：0；`INCOMPLETE`：0；
- 通用扫描器原始结果：`BLOCK` 2 条、`REVIEW` 187 条；
- `REVIEW` 来自 Python/Tcl/Tk/eSpeak/模型元数据中的公开作者信息、编码表数字和通用字段，经逐项复核不含项目密钥、账号或私人数据。

两条 `BLOCK` 均已按固定文件和固定摘要核验：

1. `_internal/resources/models/kokoro/kokoro-v1.0.int8.onnx` 的量化权重随机字节被文本正则误判为 URL 凭据。重新从 `thewh1teagle/kokoro-onnx` 官方 GitHub Release 下载同名资产，官方文件、本地源资源和 ZIP 内文件的 SHA-256 均为 `6E742170D309016E5891A994E1CE1559C702A2CCD0075E67EF7157974F6406CB`，字节完全一致。
2. `_internal/_tcl_data/http1.0/http-2.9.8.tm` 中的 `http://<dummy-user>:<dummy-pass>@www.bogus.net:8000/...` 是 Tcl 标准库展示 URL 解析的公开假账号示例。ZIP 内文件与本机 Python 3.12.3 所带 Tcl 文件的 SHA-256 均为 `7A30E7A49C1F6939537EB7A80CF2F5BC7A4969F2B2AD99BA4E26DB85BBC2FCC7`，字节完全一致。

Release ZIP 上传面人工处置后结论：`PASS`。上述处置只接受这两个固定路径、固定公开来源与固定 SHA-256，不对其他 ONNX、二进制文件或 URL 凭据模式建立通配豁免。

## 构建与烟测

- Python 3.12 隔离环境编译检查通过；
- 单元测试 371 项通过，16 项按平台条件跳过；
- 展开候选与重新解压 ZIP 均在临时 `LOCALAPPDATA` 下通过 `--smoke-test`；
- SQLite 全库快速检查和真实词条查询通过；
- OPUS 中英双向翻译、Piper 美音/英音、Kokoro 美音/英音真实推理通过；
- EXE `FileVersion`、`ProductVersion`、产品名、作者名与发布目录均已核对；
- ZIP 仅含一个中文顶层目录，无路径穿越条目，不含源码、测试、日志、缓存或 GPU 后端。

自动化结果不替代真实 Office、浏览器、PDF、QQ、系统托盘交互、真人听感、SmartScreen 与不同 Windows 机器上的人工验收；这些项目在发布后的实机验收中继续记录。

## 发布包

- 文件：`DaShengFaTranslator-0.4.0-Windows-x64.zip`
- 大小：594,062,715 字节
- SHA-256：`EF51F47B473B26609560B54E58B49CE1F061FB74036A532D3780465ABD8D8C31`

结论：源代码与当前 Release ZIP 均已完成自动扫描、固定摘要核验和人工处置，可作为“大声发划词翻译”0.4.0 的公开 GitHub 发布内容。
