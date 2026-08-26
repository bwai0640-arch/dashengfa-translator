# 大声发划词翻译

> 划一下就翻译，点一下就发音。

**大声发划词翻译**是一款面向 Windows 的本地划词翻译与英语发音工具。它可以在 Word、PowerPoint、Excel、浏览器、PDF 阅读器等常用应用中直接读取当前选区，在不离开原页面的情况下显示音标、直译，并播放美式或英式发音。单词、短语、长句和中译英均可处理，同时提供迷你浮窗、完整大窗口、全局快捷键、按应用启用以及系统托盘管理。

软件安装后无需注册账号、登录手机号或配置 API Key；词典、翻译模型与语音模型均在本机运行，查询速度不受网络状态影响，选中的文档内容也不会被发送给在线翻译服务。

**作者：眼泪斷了线**

最新版本：`0.4.0` · Windows 10/11 · 本地运行 · 无需 API Key

![大声发划词翻译大窗口](docs/images/panel.png)

## 为什么做这款软件

这款软件最初来自一个很具体的需求：准备雅思口语时，背稿和阅读材料中经常会遇到不会读、读不准或不确定重音的单词。传统流程通常要先复制文本，再切换到词典或翻译软件，完成查询后还要回到原文。对于长句，有时还需要把电脑里的文档转到手机上处理。频繁切换窗口不仅增加操作成本，也很容易打断背诵和阅读节奏。

大声发划词翻译把这一过程缩短为一次选中：

1. 在当前文档或网页中双击单词，或拖动选择短语和句子。
2. 迷你浮窗在选区附近显示音标、原文和直译，无需跳转到其他软件。
3. 默认“优先速度”设置下，单击 `US` / `UK` 可立即听取微软美式或英式发音，双击则调用本地 AI 语音模型；选择“优先自然音色”后，两种手势的映射完全反转。
4. 需要查看长句、编辑原文或复制译文时，可随时展开为完整大窗口。

核心目标不是增加一个新的学习平台，而是减少查询动作对原有学习流程的干扰：让用户继续停留在正在阅读或背诵的页面里，把注意力留给内容本身。

## 为什么选择它

| 常见问题 | 大声发划词翻译的处理方式 |
| --- | --- |
| 查询发音需要复制、切换、粘贴 | 在原应用中选中文字，直接显示结果并发音。 |
| 单词能查，短语或长句处理不方便 | 单词走本地词典，短语和句子走本地中英双向翻译模型。 |
| 系统发音响应快，但自然度有限 | 同时提供 Windows SAPI 与本地 AI 发音，并允许选择“优先速度”或“优先自然音色”。 |
| 在线翻译受网速、账号或服务状态影响 | 运行过程完全本地化，无需账号、API 或持续联网。 |
| 浮窗遮挡内容，或按钮被长文本挤出屏幕 | 迷你窗按内容、DPI 和当前屏幕工作区动态调整，底部操作始终保留。 |
| 担心取词破坏原剪贴板或读取敏感内容 | 仅在能够完整、安全还原剪贴板时使用兼容读取；密码框和未知状态会安全失败。 |

## 核心功能

| 功能 | 说明 |
| --- | --- |
| 自动发音偏好 | 默认“优先速度”：US/UK 单击、双击 Alt 自动朗读和短语/句子使用微软 Zira/Hazel，US/UK 双击使用 AI；切到“优先自然音色”后映射完全反转。 |
| 快捷操作 | 默认双击左 `Alt` 重新获取选区并自动播放美音；默认 `Alt+C` 切换窗口模式，两项均可在设置中修改。 |
| 迷你浮窗 | 普通划词后靠近选区出现；重新获取时保留原来的选词锚点，不跟随后来移动的鼠标；按内容调整并保留底部按钮，点窗口外任意位置立即收起。 |
| 完整大窗口 | 发音、原文、直译、复制和手动编辑集中展示，按钮不会被内容挤掉。 |
| 按应用启用 | 常用应用优先；其他应用可批量开关、单独设置或提升为常用。 |
| 系统托盘 | 右键图标即可切换窗口模式、暂停取词、打开设置或退出。 |
| 完全离线 | ECDICT、OPUS/CTranslate2、Piper 与 Kokoro 均随 Windows 发布包安装并在本机运行；不联网下载模型，也不依赖 GPU。 |
| 多应用取词 | Word、PowerPoint、Excel 和经典 Outlook 优先使用专用选区读取；Office、WPS、浏览器与 PDF 可继续尝试有界深层无障碍读取。 |
| 剪贴板保护 | 兼容读取只处理受支持且可完整重建的剪贴板格式；无法完整、安全还原时不会发送复制快捷键。 |

<!-- 旧 mini.png 仍含“设置”按钮，待测试候选实机验收后重拍再恢复引用。 -->

## 下载与安装

1. 前往 [Releases](https://github.com/bwai0640-arch/dashengfa-translator/releases/latest) 下载 `DaShengFaTranslator-0.4.0-Windows-x64.zip`。
2. 完整解压 ZIP，不要只单独复制 EXE。
3. 双击 `安装.cmd`。
4. 安装完成后，软件会安静进入 Windows 系统托盘。

软件无需安装 Python、Docker 或翻译插件。

安装程序会先检查运行文件、磁盘空间，并在独立临时数据目录中实际加载两套离线模型；全部通过后才替换旧版本。开始菜单同时提供“大声发划词翻译 - 卸载”。升级或卸载时，如果安装目录顶层存在用户自行放入的未知文件，程序会列出文件名并停止，避免误删。

## 使用方法

1. 右键托盘中的“声”图标，打开“应用设置”。
2. 打开需要使用划词翻译的应用开关。
3. 双击英文单词，或拖动选择短语与句子。
4. 在设置中选择“自动发音偏好”，再单击或双击迷你窗的 `US` / `UK`；默认速度模式单击微软语音、双击 AI，自然音色模式则相反。
5. 需要更多空间时点击“展开”，进入大窗口。

迷你窗中的“当前应用”开关可直接停用当前软件；“复制译文”无需展开即可复制结果；单击迷你窗外任意区域即可关闭本次浮窗。设置仍可从大窗口或托盘进入。

“自动发音偏好”统一控制 US/UK 单击与双击、双击 Alt 重新获取后的自动朗读、短语和句子，以及中文选区译成英文后的朗读。默认“优先速度”让单击与自动朗读直接走 Windows SAPI，双击才走 AI；“优先自然音色”让单击与自动朗读走 AI，双击走 SAPI。双击会取消同一轮待定单击，不会先响一种音色再补响另一种。AI 内部仍按文本路由：独立英文单词使用 CPU-only Piper（US Lessac / UK Cori），短语、句子和中文译出的英文使用 Kokoro（US `af_bella` / UK `bf_emma`）；SAPI 使用 Microsoft Zira / Hazel。

设置中的“慢速 / 标准 / 快速”只调整 Kokoro 短语和句子的模型原生速度，分别为 `0.75 / 0.85 / 1.00`。Piper 单词固定保持模型原版 `1.00`，不做播放加速或后处理拉伸；微软 SAPI 不受该设置影响。Piper 与 Kokoro 会在启动后并行做真实小样本预热；按钮按下可生成当前双击窗口专用、可取消的 AI 候选，真实点击会让预热和旧候选让路。任一路 AI 模型缺失、损坏或加载失败时，本次 AI 发音安全回退到 SAPI，不联网修复。普通短语和短句完整生成后播放；较长句子只按自然标点分段，并且只有至少两段、约 5 秒真实音频已缓冲且耗时预测安全时才提前开播，否则等待整句完成。不存在按固定字符数切换 SAPI 的规则。

本项目及随附语音模型按开源、免费、非商用方式提供。如认为项目中的任何内容侵犯了您的权利，请联系作者“眼泪斷了线”，将在核实后删除或处理。Piper 组件的 GPL 权利以随包许可证及其上游项目为准，本项目说明不改变该许可。

快捷键在任何已启用应用中都可使用：

- 连续轻按两次左 `Alt`：只用于重新获取当前选区并自动播放美音；默认速度模式走 Microsoft Zira，自然音色模式走 AI。中文会等待本地英译完成后按同一偏好朗读。已有迷你窗继续锚定在原选词位置，不跟随后来移动的鼠标。单按、长按、`Alt+Tab`、`Alt+C` 与右 Alt/AltGr 不会误触。
- `Alt+C`：在迷你浮窗和大窗口之间切换；热键打开大窗口时不会主动抢走原应用焦点。
- 两个快捷操作都能在“设置 → 全局快捷键”中重新录入；若组合已被其他程序占用，软件会保留原设置。
- 关闭浮窗、关闭大窗口或点击窗外会停止当前长句朗读；“展开”和窗口模式切换不会误停。

## 应用范围管理

默认常用应用完整名单如下（括号内为识别的进程名）：

- Microsoft Office：Word（`winword.exe`）、Excel（`excel.exe`）、PowerPoint（`powerpnt.exe`）、经典 Outlook（`outlook.exe`）、新版 Outlook（`olk.exe`）、OneNote（`onenote.exe`）。
- WPS：WPS 文字（`wps.exe`）、WPS 表格（`et.exe`）、WPS 演示（`wpp.exe`）、WPS PDF（`pdf.exe`）。
- 浏览器与 PDF：Microsoft Edge（`msedge.exe`）、Google Chrome（`chrome.exe`）、Mozilla Firefox（`firefox.exe`）、Adobe Acrobat Reader（`acrord32.exe`）、Adobe Acrobat（`acrobat.exe`）、Foxit PDF Reader（`foxitpdfreader.exe`）。
- 通讯与工具：QQ（`qq.exe`）、记事本（`notepad.exe`）。微信（`wechat.exe` / `weixin.exe`）已取消取词支持，不会出现在应用列表中，也不能手动开启。

- 调整过的应用会优先排列。
- “其他应用”支持一键全部开启或关闭，也可以展开后逐项设置。
- 任意其他应用都可以提升到常用区。
- 未运行的程序可通过“添加应用…”手动选择 EXE。

## 隐私与安全边界

- 文档选区不会上传到在线翻译服务。
- 程序不会读取密码输入框。
- 兼容读取只会短暂保存并还原白名单内受支持、可完整重建的原剪贴板格式；遇到未知 Office/OLE 格式、虚拟附件或超限内容时不会发送复制快捷键。
- 密码输入框或受保护选区会安全失败；微信已明确排除，不会尝试取词。
- 自动出现的迷你窗和大窗口不会抢走源应用焦点，划词后仍可在原应用使用 `Ctrl+C`。
- 取词等待期间如果焦点或剪贴板被其他操作改变，软件会取消本次读取，不覆盖新内容。
- 设置、日志和翻译缓存位于 `%LOCALAPPDATA%\DaShengFaTranslator`。
- 从 `0.2.x` 升级时，软件会复制旧目录中的设置与翻译缓存，不删除旧数据。

## 技术路线

```text
鼠标双击 / 拖选 / 用户快捷操作
        ↓
应用白名单检查
        ↓
Word / PowerPoint / Excel / 经典 Outlook 专用读取 → Windows UI Automation（含有界深层文档选区）→ 安全剪贴板兼容读取
        ↓
ECDICT 单词查询 / OPUS 本地句子翻译
        ↓
迷你浮窗或大窗口
        ↓
自动发音偏好 → 单击 / 双击 / 双击 Alt 的唯一后端
AI：单词 → Piper CPU；短语、句子、中文英译 → Kokoro
SAPI：US → Microsoft Zira；UK → Microsoft Hazel
```

详细设计见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)，刁难用户场景见 [docs/ADVERSARIAL_REVIEW.md](docs/ADVERSARIAL_REVIEW.md)，构建与发布步骤见 [docs/RELEASE.md](docs/RELEASE.md)。

## 本地开发

要求：Windows 10/11、Python 3.12。开发与打包使用项目专用虚拟环境，避免把全局 Python 中与本项目无关的依赖带入成品。

> GitHub Release 的 Windows ZIP 已包含完整 Piper/Kokoro 语音模型。由于部分语音模型超过 GitHub 普通 Git 的单文件限制，源码仓库不跟踪这些 ONNX/BIN 二进制；从源码自行构建发布包时，需要按 [docs/RELEASE.md](docs/RELEASE.md) 准备对应资源。许可证、配置和模型卡仍保留在仓库中。

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-runtime.txt
.\.venv\Scripts\python.exe desktop_app.py
```

构建无需 Python 的 Windows 文件夹版：

```powershell
.\.venv\Scripts\python.exe -m pip install PyInstaller
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean `
  --distpath dist-desktop `
  --workpath build-desktop `
  DaShengFaTranslator.spec
```

发布前至少运行：

```powershell
.\.venv\Scripts\python.exe -m py_compile desktop_app.py selection_capture.py capture_runtime.py hotkey_service.py app.py
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## 项目结构

| 路径 | 用途 |
| --- | --- |
| `desktop_app.py` | 桌面版入口、托盘、全局取词、界面和应用设置。 |
| `selection_capture.py` | Word、PowerPoint、Excel、经典 Outlook、UI Automation 与安全剪贴板兼容读取。 |
| `capture_runtime.py` | 按应用自适应等待策略。 |
| `hotkey_service.py` | 原生 Windows 全局快捷键注册与安全启停。 |
| `app.py` | 本地词典、OPUS/CTranslate2 翻译、Piper/Kokoro/SAPI 发音和 Word 读取核心。 |
| `resources/` | ECDICT 数据库、双向翻译模型、Piper/Kokoro 离线发音资源和程序图标。 |
| `DaShengFaTranslator.spec` | PyInstaller 打包配置。 |
| `docs/` | 需求总账、架构、对抗式审查、发布说明和产品截图。 |
| `tests/` | 不操作真实桌面和剪贴板的取词回归测试。 |

## 作者与许可

大声发划词翻译由 **眼泪斷了线** 制作。

仓库暂未声明统一的开源许可。除第三方组件按各自许可证使用外，项目代码与品牌权利由作者保留。第三方组件与数据来源见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

安全问题请通过 GitHub 的 Private vulnerability reporting 提交，参见 [SECURITY.md](SECURITY.md)。
