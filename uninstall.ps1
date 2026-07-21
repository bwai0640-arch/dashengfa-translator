param(
    [switch]$RemoveUserData
)

$ErrorActionPreference = 'Stop'
$programsRoot = [System.IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'Programs'))
$target = [System.IO.Path]::GetFullPath((Join-Path $programsRoot 'DaShengFaTranslator'))
$safePrefix = $programsRoot.TrimEnd('\') + '\'

if (-not $target.StartsWith($safePrefix, [System.StringComparison]::OrdinalIgnoreCase) -or
    [System.IO.Path]::GetFileName($target) -ne 'DaShengFaTranslator') {
    throw "拒绝卸载：目标路径校验失败：$target"
}

Get-Process -Name 'DaShengFaTranslator' -ErrorAction SilentlyContinue | Stop-Process -Force

$shortcutPaths = @(
    (Join-Path ([Environment]::GetFolderPath('Desktop')) '大声发划词翻译.lnk'),
    (Join-Path (Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs') '大声发划词翻译.lnk')
)
foreach ($shortcut in $shortcutPaths) {
    if (Test-Path -LiteralPath $shortcut) {
        Remove-Item -LiteralPath $shortcut -Force
    }
}

if (Test-Path -LiteralPath $target) {
    Remove-Item -LiteralPath $target -Recurse -Force
}

if ($RemoveUserData) {
    $dataPaths = @(
        [System.IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'DaShengFaTranslator')),
        [System.IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'WordLocalTranslator'))
    )
    foreach ($dataPath in $dataPaths) {
        $leaf = [System.IO.Path]::GetFileName($dataPath)
        if ($leaf -in @('DaShengFaTranslator', 'WordLocalTranslator') -and
            (Test-Path -LiteralPath $dataPath)) {
            Remove-Item -LiteralPath $dataPath -Recurse -Force
        }
    }
}

Write-Host '卸载完成。翻译缓存默认保留。' -ForegroundColor Green
