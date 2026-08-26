# 构建与发布

## 前置条件

- Windows 10/11 x64；
- Python 3.12；
- 项目专用的干净 Python 3.12 虚拟环境，不复用装有 Gradio、Notebook、训练或数据分析工具的全局环境；
- `requirements-runtime.txt` 中的依赖；
- `resources/ecdict.db`、两套 OPUS 翻译模型和 Kokoro 的 `kokoro-v1.0.int8.onnx`、`voices-v1.0.bin` 完整存在；
- `resources/models/piper/` 中两份高质量 ONNX 声音模型、配置、模型卡、`PIPER_GPL-3.0.txt` 与 `README.md` 完整存在并符合 README 的 SHA256 清单。Piper 固定为 CPU-only；构建机和目标机都不需要 Vulkan、CUDA 或独立显卡。

## 构建

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-runtime.txt
.\.venv\Scripts\python.exe -m pip install PyInstaller
.\.venv\Scripts\python.exe make_icon.py
.\.venv\Scripts\python.exe -m py_compile desktop_app.py selection_capture.py capture_runtime.py hotkey_service.py app.py
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean `
  --distpath dist-desktop `
  --workpath build-desktop `
  DaShengFaTranslator.spec
```

开始 PyInstaller 前应按 `resources/models/piper/README.md` 逐项重算 Piper 资源 SHA256；任一文件缺失或摘要不一致都停止构建。

成品入口应为 `dist-desktop/DaShengFaTranslator/DaShengFaTranslator.exe`。

不得从现有 `_internal` 手工删除看似无关的模块。依赖瘦身只能通过干净环境重建，并在完整自动测试与成品烟测通过后比较替换。

## 对抗式门禁

构建前必须按 [ADVERSARIAL_REVIEW.md](ADVERSARIAL_REVIEW.md) 复核刁难用户场景。自动测试至少覆盖快速 A→B→C 换词、加载中操作浮窗、关闭后迟到结果、快捷键中断、物理修饰键、损坏设置、多显示器工作区、单击/双击发音仲裁、并行预热、AI 故障回退、词典损坏和安装状态恢复。任何失败都不能用“实机可能正常”跳过。

需要真实 Office、PDF、QQ、SAPI、SmartScreen 或强杀安装的项目不得在当前桌面偷偷执行；放入下方实机清单，由用户测试或在一次性 Windows 用户/虚拟机中完成。微信已不在取词支持范围内。

## 冒烟检查

1. 把成品复制到隔离目录，并用临时 `LOCALAPPDATA` 运行 `DaShengFaTranslator.exe --smoke-test <结果 JSON>`。确认产品名、作者、版本与候选一致，SQLite `PRAGMA quick_check`、真实词条查询和两套 OPUS 模型的中英双向离线翻译均成功。
2. 检查 smoke 报告逐项核实两份 Piper ONNX 模型、配置、模型卡、GPL 文本、资源 README，以及 Kokoro 模型与 voices 文件的固定 SHA256；Piper 的 eSpeak 数据必须位于成品目录，不能因构建机安装的 Python 包而假通过。
3. 让成品 smoke 实际生成短音频：Piper 分别合成一个 US/UK 英文单词，Kokoro 合成一个英文句子；输出必须是可解析、非空、采样参数有效的 WAV，且 Piper 必须精确为 22.05 kHz。该检查在没有 Vulkan/CUDA、没有独立显卡的 CPU-only 环境也必须通过。
4. 断网冷启动图形界面，确认进程持续运行、界面立即可操作、日志没有托盘错误；Piper 与 Kokoro 在后台并行预热且各只启动一次，不能串行卡住界面。一行状态应按真实事件显示并行预热、Piper/Kokoro 分别就绪，不能出现伪造百分比或迟到状态倒退。
5. 分别选择“优先速度”和“优先自然音色”，覆盖 US/UK 单击、双击、双击 Alt、短语/句子及中文英译朗读；确认单击和双击严格反转、待定单击被抑制且没有另一后端补播。AI 单词应分别走 Piper `en_US-lessac-high` / `en_GB-cori-high`，AI 短语/句子应走 Kokoro `af_bella` / `bf_emma`，SAPI 应走 Microsoft Zira / Hazel。再用长句确认只在安全缓冲后提前播放，文本长度本身不会切换后端。
6. 确认三档设置只对 Kokoro 短语/句子使用模型原生 `0.75 / 0.85 / 1.00`；Piper 单词在三档下都保持原版 `1.00`、采样数不变且不进入任何后处理变速，切换三档也不得改变 SAPI 双击发音的系统语速。
7. 把实际成品生成的 Piper US/UK、Kokoro 三档 US/UK 与 Microsoft Zira/Hazel 样本随机编号，隐藏后端和档位后由真人试听；记录评价者、机器/耳机环境、自然度、清晰度、音高、节奏和口音。技术检查通过但没有真人听感结论时，发音功能仍不得标记为通过或可发布。
8. 只在成品的可丢弃副本中，分别模拟 Piper 模型、配置、eSpeak 数据、Kokoro 模型缺失或损坏以及初始化异常。需要 AI 的手势应及时回退 SAPI，不崩溃、不挂起、不弹下载提示；明确映射到 SAPI 的手势不等待 AI；一行状态应显示实际生成后端、缓冲、取消或已回退微软语音。
9. 保持网络断开，重复 AI 发音与故障回退，确认没有下载、HTTP 请求、固定端口或 GPU 探测依赖；成品不得包含 `ggml-vulkan`、CUDA、HIP 等 GPU 后端。
10. 在记事本或浏览器双击英文单词，确认迷你窗出现；选择长句时确认 US、UK、关闭、当前应用开关、“复制译文”和“展开”始终完整可见。
11. 点击迷你窗外区域，确认窗口立即收起。
12. 选中英文并显示迷你窗后，把鼠标移到别处再连续轻按两次左 Alt，确认重新获取、自动播放美音且迷你窗仍锚定在原选词位置；再验证首次无旧锚点和切换来源窗口时会安全采用新位置，单按、长按、`Alt+Tab`、`Alt+C`、Ctrl+Alt 与右 Alt/AltGr 均不触发。
13. 分别从迷你浮窗和大窗口按默认 `Alt+C`，确认双向切换且原应用焦点不被抢走；在设置中把两项改成未占用组合并复测，再尝试一个已占用组合，确认原设置不变。
14. 分别在 Word、PowerPoint、Excel 与经典 Outlook 验证专用选区读取；Excel 覆盖单格、多格、多区域、单元格编辑、公式栏和图形对象，Outlook 覆盖独立邮件窗口、内嵌回复和普通阅读窗，确认只读取真实文字选区。
15. 在 WPS 文字/表格/演示、Chrome/Edge/Firefox 的普通网页和文本型 PDF，以及 Adobe/Foxit/WPS PDF 中验证普通及有界深层 UIA 选区；扫描件或受保护 PDF 应明确失败且不改变剪贴板。
16. 在 QQ 中验证选区读取；连续快速换词时确认鼠标不卡顿、程序不退出、原剪贴板仍可正常粘贴。确认微信（`wechat.exe` / `weixin.exe`）不可启用，且不会尝试读取选区。
17. 划词后立即在原应用按 `Ctrl+C`，确认复制仍生效且浮窗没有抢走焦点；再用纯文本、网页富文本、截图，以及含未知 Office/OLE 格式的剪贴板分别复测：受支持且可完整重建的格式应还原，无法完整重建的状态应在发送复制前安全取消。
18. 手动切换 US/UK 发音，确认长句也能看到并点击两个发音按钮；点击迷你窗“复制译文”确认复制当前结果。
19. 打开设置，确认默认常用区完整包含 Word、Excel、PowerPoint、新旧 Outlook、OneNote，WPS 文字/表格/演示/PDF，Edge、Chrome、Firefox，Adobe Reader/Acrobat、Foxit、QQ 与记事本；微信不显示且不可手动开启。再验证应用开关、批量开关、最近调整排序与设为常用。
20. 检查中英双向模型、`ecdict.db`、Piper/Kokoro 完整资源与 UIAutomation DLL 均在成品目录。
21. 在隔离的临时 `LOCALAPPDATA\Programs` 树复测安装升级：未知顶层文件必须阻止覆盖并列出名称；模型、发音资源或词典缺失、冒烟失败、空间不足时旧版本必须保持原样；同名便携副本不得被停止。
22. 确认安装目录内含 `uninstall.ps1`，开始菜单含独立卸载入口；模拟卸载失败时快捷方式不得提前删除。
23. 在一次性 Windows 用户或虚拟机中强杀复制、标记写入、接管和卸载阶段；下一次运行只能恢复严格带本产品标记的目录，未知目录与用户文件必须原样保留。

## 发布包

发布目录结构：

```text
大声发划词翻译-<版本号>-Windows-x64/
├─ DaShengFaTranslator/
├─ 安装.cmd
├─ 卸载.cmd
├─ install.ps1
├─ uninstall.ps1
├─ 使用说明.txt
└─ THIRD_PARTY_NOTICES.md
```

压缩为 `DaShengFaTranslator-<版本号>-Windows-x64.zip`，计算 SHA256，并把 ZIP 作为 GitHub Release 资产上传。压缩包内部的顶层目录仍使用中文产品名；构建目录与 ZIP 不进入 Git 历史。

发布包还必须满足：

- 源码 `APP_VERSION`、EXE `FileVersion`/`ProductVersion`、使用说明、文件夹名、ZIP 名和冒烟报告版本完全一致；
- ZIP 只有一个中文产品顶层目录，不含源码、测试、缓存、日志、构建目录或路径穿越条目；
- 仓库根与压缩包内的 `install.ps1`、`uninstall.ps1` 哈希相同；
- `_internal` 内的词典、翻译模型、Kokoro 资源、图标和两种 UIAutomation DLL 通过运行契约检查；
- `_internal/resources/models/piper/` 含两份 ONNX 模型、配置、模型卡、GPL 文本与 README；`_internal/piper/espeak-ng-data/` 存在且可用于实际合成。各文件 SHA256 与资源 README 一致，且不含 Vulkan、CUDA、HIP 后端。

## 本地制品保留策略

- `build-desktop/` 是 PyInstaller 中间目录；`dist-desktop/` 是进入 staging 前的展开副本。当前候选完成 ZIP 内容核对、SHA256 和临时 `LOCALAPPDATA` 烟测后，两者都应删除。
- `release-staging/` 只保留当前候选展开目录、对应 ZIP 和烟测报告。旧候选在确认不再用于回退后删除。
- 当前候选仍从 staging 直接运行时不得删除展开目录；安装并验收后可只保留 ZIP 与烟测报告，按需重新解压。
- 任何清理都不得删除 `resources/` 源资源、用户设置/缓存、Piper/Kokoro 授权材料或 Git 中尚未纳入可恢复历史的源码。

## GitHub 发布门禁

发布前运行仓库内检查以及 `github-upload-safety`。只有状态为 PASS 时才能创建远端、推送或上传 Release；REVIEW、BLOCK 与 INCOMPLETE 都必须先处理。

当前 Kokoro ONNX 的 UINT8 `TensorProto.raw_data` 量化权重字节可能触发通用文本正则误报。处理时必须逐字节核对官方发布资产，并让扫描器仅跳过 ONNX 权重字段、仅接受固定路径与固定 SHA256 的公开材料；不得忽略全部 `.onnx` 或大文件。只写说明不能代替门禁通过，在扫描器仍返回 BLOCK 时不得上传。
