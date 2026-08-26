# 项目约定

- 产品名固定为“大声发划词翻译”，作者名固定为“眼泪斷了线”；公开文档、安装脚本、窗口标题、托盘和发布包必须一致。
- 当前桌面版入口是 `desktop_app.py`；`app.py` 提供共享离线翻译、发音和 Word 读取核心。不要因其中保留原型界面就删除该文件。
- `resources/` 是运行时契约：`ecdict.db`、`models/`、`app_icon.png` 与 `app_icon.ico` 必须随发布包保留。
- 取词顺序是 Word / PowerPoint / Excel / 经典 Outlook 专用读取、Windows UI Automation、安全剪贴板兼容读取。复杂文档应用可从前台窗口根进行有界深层 UIA 选区探测；只有 UIA 明确确认选区安全时才能发送兼容复制，密码或未知状态必须安全失败；不得读取整篇文档、覆盖图片、文件或并发更新的剪贴板。
- 迷你窗尺寸必须按内容、DPI 和鼠标所在显示器的工作区 `rcWork` 动态计算，支持负坐标和多方向任务栏；底部的应用开关、“复制译文”和“展开”始终完整可见。
- 默认连续轻按两次左 `Alt` 会重新获取并自动播放美音，默认 `Alt+C` 切换迷你/大窗口；两项均可由用户在设置中修改，双击 Alt 只允许用于重新获取。“自动发音偏好”默认“优先速度”：单击 US/UK、双击 Alt 与句子走 Windows SAPI，双击 US/UK 才走 AI；“优先自然音色”则完全反转为单击 US/UK、双击 Alt 与句子走 AI，双击 US/UK 走 SAPI。不得擅自另设时长阈值或改变这份映射。
- 兼容读取只允许可完整、安全重建的托管剪贴板格式；精确白名单包含 Chromium 的 source URL 与 RFH token HGLOBAL 元数据，但不得接受相似或任意注册格式，不得用“部分还原”换取兼容性，也不得保留裸 OLE/GDI 指针。
- 新数据目录是 `%LOCALAPPDATA%\DaShengFaTranslator`；迁移标记只在设置和缓存复制成功后完成，临时失败可在下次启动重试，且不删除旧目录。
- 打包使用 `DaShengFaTranslator.spec` 和隔离的 Python 3.12 虚拟环境；`build-desktop/`、`dist-desktop/`、`release-staging/` 和测试截图不提交 Git。ZIP 内容与烟测通过后应清理可再生的 `build-desktop/`、`dist-desktop/` 和旧候选，只保留当前候选、ZIP 与烟测证据；不得直接裁剪成品 `_internal`。
- 修改代码后至少在项目 Python 3.12 环境运行 `python -m py_compile desktop_app.py selection_capture.py capture_runtime.py hotkey_service.py app.py` 和 `python -m unittest discover -s tests -v`；还要按 `docs/ADVERSARIAL_REVIEW.md` 模拟刁难用户的操作序列。修改打包后必须用临时 `LOCALAPPDATA` 运行成品 `--smoke-test`，其中词典要通过 SQLite `PRAGMA quick_check` 和真实词条查询，再检查模型、托盘与日志。
- 发布前必须运行 `github-upload-safety` 扫描；发现 BLOCK、REVIEW 或 INCOMPLETE 时不得上传。
