# 项目约定

- 产品名固定为“大声发划词翻译”，作者名固定为“眼泪斷了线”；公开文档、安装脚本、窗口标题、托盘和发布包必须一致。
- 当前桌面版入口是 `desktop_app.py`；`app.py` 提供共享离线翻译、发音和 Word 读取核心。不要因其中保留原型界面就删除该文件。
- `resources/` 是运行时契约：`ecdict.db`、`models/`、`app_icon.png` 与 `app_icon.ico` 必须随发布包保留。
- 取词顺序是 Word COM、Windows UI Automation、安全的文本剪贴板兼容读取。不得覆盖图片或文件剪贴板，不得读取密码框。
- 迷你窗高度必须按内容和 DPI 动态计算，底部的应用开关、“设置”和“展开”始终完整可见。
- 新数据目录是 `%LOCALAPPDATA%\DaShengFaTranslator`；仅在首次启动时从旧 `WordLocalTranslator` 目录复制设置和缓存，不删除旧目录。
- 打包使用 `DaShengFaTranslator.spec`；`build-desktop/`、`dist-desktop/`、`release-staging/` 和测试截图均为可再生产物，不提交 Git。
- 修改代码后至少运行 `python -m py_compile desktop_app.py app.py`；修改打包后还要启动成品 EXE 并检查本地模型、托盘与日志。
- 发布前必须运行 `github-upload-safety` 扫描；发现 BLOCK、REVIEW 或 INCOMPLETE 时不得上传。
