param()

$ErrorActionPreference = 'Stop'
$packageRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$source = Join-Path $packageRoot 'DaShengFaTranslator'
$target = [System.IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'Programs\DaShengFaTranslator'))
$exe = Join-Path $target 'DaShengFaTranslator.exe'

if (-not (Test-Path -LiteralPath (Join-Path $source 'DaShengFaTranslator.exe'))) {
    throw '安装包不完整：找不到 DaShengFaTranslator.exe。请重新解压完整安装包。'
}

Get-Process -Name 'DaShengFaTranslator' -ErrorAction SilentlyContinue | Stop-Process -Force
New-Item -ItemType Directory -Force -Path $target | Out-Null
Copy-Item -Path (Join-Path $source '*') -Destination $target -Recurse -Force

$shell = New-Object -ComObject WScript.Shell
$desktopShortcut = $shell.CreateShortcut((Join-Path ([Environment]::GetFolderPath('Desktop')) '大声发划词翻译.lnk'))
$desktopShortcut.TargetPath = $exe
$desktopShortcut.WorkingDirectory = $target
$desktopShortcut.IconLocation = "$exe,0"
$desktopShortcut.Description = '大声发划词翻译：桌面划词翻译与英美发音'
$desktopShortcut.Save()

$startMenuDir = Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs'
$startShortcut = $shell.CreateShortcut((Join-Path $startMenuDir '大声发划词翻译.lnk'))
$startShortcut.TargetPath = $exe
$startShortcut.WorkingDirectory = $target
$startShortcut.IconLocation = "$exe,0"
$startShortcut.Description = '大声发划词翻译：桌面划词翻译与英美发音'
$startShortcut.Save()

Start-Process -FilePath $exe -WorkingDirectory $target
Write-Host '安装完成。程序已经启动，并已创建桌面和开始菜单快捷方式。' -ForegroundColor Green
