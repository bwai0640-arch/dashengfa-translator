# 构建与发布

## 前置条件

- Windows 10/11 x64；
- Python 3.12；
- `requirements-runtime.txt` 中的依赖；
- `resources/ecdict.db` 与两套模型完整存在。

## 构建

```powershell
python -m pip install -r requirements-runtime.txt
python make_icon.py
python -m py_compile desktop_app.py app.py
python -m PyInstaller --noconfirm --clean `
  --distpath dist-desktop `
  --workpath build-desktop `
  DaShengFaTranslator.spec
```

成品入口应为 `dist-desktop/DaShengFaTranslator/DaShengFaTranslator.exe`。

## 冒烟检查

1. 从 `dist-desktop` 启动成品 EXE，确认进程持续运行且日志没有模型或托盘错误。
2. 在记事本或浏览器双击英文单词，确认迷你窗出现。
3. 使用长释义检查当前应用开关、“设置”和“展开”完整可见。
4. 点击迷你窗外区域，确认窗口立即收起。
5. 切换 US/UK 发音。
6. 打开设置，验证应用开关、批量开关与设为常用。
7. 检查中英双向模型、`ecdict.db` 与 UIAutomation DLL 均在成品目录。

## 发布包

发布目录结构：

```text
大声发划词翻译-0.3.0-Windows-x64/
├─ DaShengFaTranslator/
├─ 安装.cmd
├─ 卸载.cmd
├─ install.ps1
├─ uninstall.ps1
├─ 使用说明.txt
└─ THIRD_PARTY_NOTICES.md
```

压缩为 `大声发划词翻译-0.3.0-Windows-x64.zip`，计算 SHA256，并把 ZIP 作为 GitHub Release 资产上传。构建目录与 ZIP 不进入 Git 历史。

## GitHub 发布门禁

发布前运行仓库内检查以及 `github-upload-safety`。只有状态为 PASS 时才能创建远端、推送或上传 Release；REVIEW、BLOCK 与 INCOMPLETE 都必须先处理。
