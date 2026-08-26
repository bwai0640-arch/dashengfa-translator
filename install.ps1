param()

$ErrorActionPreference = 'Stop'

$productId = 'DaShengFaTranslator'
$productName = '大声发划词翻译'
$productAuthor = '眼泪斷了线'
$targetLeaf = 'DaShengFaTranslator'
$markerName = '.dashengfa-install.json'
$markerTempName = '.dashengfa-install.json.tmp'
$markerBackupName = '.dashengfa-install.json.bak'
$managedTopLevelNames = @(
    'DaShengFaTranslator.exe',
    '_internal',
    'uninstall.ps1',
    $markerName,
    $markerTempName,
    $markerBackupName
)
$packageRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$source = [System.IO.Path]::GetFullPath((Join-Path $packageRoot 'DaShengFaTranslator'))
$packagedUninstaller = [System.IO.Path]::GetFullPath((Join-Path $packageRoot 'uninstall.ps1'))

if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
    throw '安装失败：无法确定当前用户的 LOCALAPPDATA 目录。'
}

$programsRoot = [System.IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'Programs'))
$target = [System.IO.Path]::GetFullPath((Join-Path $programsRoot $targetLeaf))
$exe = Join-Path $target 'DaShengFaTranslator.exe'
$operationId = [System.Guid]::NewGuid().ToString('N')
$stagingLeaf = ".$targetLeaf-staging-$operationId"
$backupLeaf = ".$targetLeaf-backup-$operationId"
$staging = [System.IO.Path]::GetFullPath((Join-Path $programsRoot $stagingLeaf))
$backup = [System.IO.Path]::GetFullPath((Join-Path $programsRoot $backupLeaf))

function Assert-ExactProgramsSibling {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$ExpectedLeaf
    )

    $fullPath = [System.IO.Path]::GetFullPath($Path)
    $parent = [System.IO.Path]::GetDirectoryName($fullPath)
    $leaf = [System.IO.Path]::GetFileName($fullPath)
    if (-not $parent.Equals($programsRoot, [System.StringComparison]::OrdinalIgnoreCase) -or
        -not $leaf.Equals($ExpectedLeaf, [System.StringComparison]::Ordinal)) {
        throw "拒绝安装：部署路径校验失败：$fullPath"
    }
    return $fullPath
}

function Assert-PlainDirectoryTree {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }
    $rootItem = Get-Item -LiteralPath $Path -Force
    if (-not $rootItem.PSIsContainer) {
        throw "$Label 不是文件夹：$Path"
    }
    if (($rootItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "$Label 不能是符号链接或目录联接：$Path"
    }
    $reparsePoint = Get-ChildItem -LiteralPath $Path -Force -Recurse |
        Where-Object {
            ($_.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0
        } |
        Select-Object -First 1
    if ($null -ne $reparsePoint) {
        throw "$Label 内含符号链接或目录联接：$($reparsePoint.FullName)"
    }
}

function Assert-ManagedTopLevel {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }
    $unknown = @(
        Get-ChildItem -LiteralPath $Path -Force |
            Where-Object { $_.Name -notin $managedTopLevelNames } |
            ForEach-Object { $_.Name }
    )
    if ($unknown.Count -gt 0) {
        $listed = ($unknown | Sort-Object) -join '、'
        throw "$Label 含有不属于本软件管理的顶层项目，已拒绝覆盖或删除：$listed。请先将这些项目移出后重试。"
    }
}

function Assert-PackageTopLevel {
    param([Parameter(Mandatory = $true)][string]$Path)

    $allowedPackageNames = @('DaShengFaTranslator.exe', '_internal')
    $unexpected = @(
        Get-ChildItem -LiteralPath $Path -Force |
            Where-Object { $_.Name -notin $allowedPackageNames } |
            ForEach-Object { $_.Name }
    )
    if ($unexpected.Count -gt 0) {
        throw "安装包程序目录含有意外顶层项目：$(($unexpected | Sort-Object) -join '、')。请重新解压官方完整安装包。"
    }
}

function Assert-ValidPowerShellScript {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $tokens = $null
    $parseErrors = $null
    [System.Management.Automation.Language.Parser]::ParseFile(
        $Path,
        [ref]$tokens,
        [ref]$parseErrors
    ) | Out-Null
    if (@($parseErrors).Count -gt 0) {
        $messages = @($parseErrors | ForEach-Object { $_.Message }) -join '；'
        throw "$Label 语法损坏：$messages"
    }
}

function Get-ContractErrors {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [switch]$RequireUninstaller
    )

    $requirements = @(
        @{ Relative = 'DaShengFaTranslator.exe'; Minimum = 1024 },
        @{ Relative = '_internal\resources\ecdict.db'; Minimum = 1048576 },
        @{ Relative = '_internal\resources\app_icon.png'; Minimum = 128 },
        @{ Relative = '_internal\resources\app_icon.ico'; Minimum = 128 },
        @{ Relative = '_internal\resources\models\translate-en_zh-1_9\model\model.bin'; Minimum = 1048576 },
        @{ Relative = '_internal\resources\models\translate-en_zh-1_9\model\config.json'; Minimum = 16 },
        @{ Relative = '_internal\resources\models\translate-en_zh-1_9\sentencepiece.model'; Minimum = 65536 },
        @{ Relative = '_internal\resources\models\translate-zh_en-1_9\model\model.bin'; Minimum = 1048576 },
        @{ Relative = '_internal\resources\models\translate-zh_en-1_9\model\config.json'; Minimum = 16 },
        @{ Relative = '_internal\resources\models\translate-zh_en-1_9\sentencepiece.model'; Minimum = 65536 },
        @{ Relative = '_internal\resources\models\kokoro\kokoro-v1.0.int8.onnx'; Minimum = 83886080 },
        @{ Relative = '_internal\resources\models\kokoro\voices-v1.0.bin'; Minimum = 1048576 },
        @{ Relative = '_internal\resources\models\piper\en_US-lessac-high.onnx'; Minimum = 104857600 },
        @{ Relative = '_internal\resources\models\piper\en_US-lessac-high.onnx.json'; Minimum = 1024 },
        @{ Relative = '_internal\resources\models\piper\en_US-lessac-high.MODEL_CARD.md'; Minimum = 128 },
        @{ Relative = '_internal\resources\models\piper\en_GB-cori-high.onnx'; Minimum = 104857600 },
        @{ Relative = '_internal\resources\models\piper\en_GB-cori-high.onnx.json'; Minimum = 1024 },
        @{ Relative = '_internal\resources\models\piper\en_GB-cori-high.MODEL_CARD.md'; Minimum = 128 },
        @{ Relative = '_internal\resources\models\piper\PIPER_GPL-3.0.txt'; Minimum = 32768 },
        @{ Relative = '_internal\resources\models\piper\README.md'; Minimum = 512 },
        @{ Relative = '_internal\uiautomation\bin\UIAutomationClient_VC140_X64.dll'; Minimum = 1024 },
        @{ Relative = '_internal\uiautomation\bin\UIAutomationClient_VC140_X86.dll'; Minimum = 1024 }
    )
    if ($RequireUninstaller) {
        $requirements += @{ Relative = 'uninstall.ps1'; Minimum = 128 }
    }

    $errors = @()
    foreach ($requirement in $requirements) {
        $candidate = Join-Path $Path $requirement.Relative
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            $errors += "缺少 $($requirement.Relative)"
            continue
        }
        $item = Get-Item -LiteralPath $candidate -Force
        if ($item.Length -lt $requirement.Minimum) {
            $errors += "$($requirement.Relative) 体积异常（$($item.Length) 字节）"
        }
    }
    $prefixRequirements = @(
        @{ Relative = '_internal\resources\app_icon.png'; Prefix = [byte[]](0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A); Label = 'PNG 图标' },
        @{ Relative = '_internal\resources\app_icon.ico'; Prefix = [byte[]](0x00, 0x00, 0x01, 0x00); Label = 'ICO 图标' },
        @{ Relative = '_internal\uiautomation\bin\UIAutomationClient_VC140_X64.dll'; Prefix = [byte[]](0x4D, 0x5A); Label = 'x64 UIAutomation DLL' },
        @{ Relative = '_internal\uiautomation\bin\UIAutomationClient_VC140_X86.dll'; Prefix = [byte[]](0x4D, 0x5A); Label = 'x86 UIAutomation DLL' }
    )
    foreach ($requirement in $prefixRequirements) {
        $candidate = Join-Path $Path $requirement.Relative
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            continue
        }
        try {
            $stream = [System.IO.File]::Open($candidate, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::Read)
            try {
                $actual = New-Object byte[] $requirement.Prefix.Length
                $read = $stream.Read($actual, 0, $actual.Length)
            }
            finally {
                $stream.Dispose()
            }
            if ($read -ne $requirement.Prefix.Length -or
                -not [System.Linq.Enumerable]::SequenceEqual([byte[]]$actual, [byte[]]$requirement.Prefix)) {
                $errors += "$($requirement.Label) 文件头无效"
            }
        }
        catch {
            $errors += "$($requirement.Label) 无法读取：$($_.Exception.Message)"
        }
    }
    return @($errors)
}

function Assert-DeploymentContract {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label,
        [switch]$RequireUninstaller
    )

    $errors = @(Get-ContractErrors -Path $Path -RequireUninstaller:$RequireUninstaller)
    if ($errors.Count -gt 0) {
        throw "$Label 运行文件不完整：$($errors -join '；')。"
    }
}

function Assert-ValidDeploymentMarker {
    param(
        [Parameter(Mandatory = $true)][object]$Marker,
        [Parameter(Mandatory = $true)][string]$MarkerPath
    )

    $validSchemaType = $Marker.schema -is [int] -or $Marker.schema -is [long]
    if (-not $validSchemaType -or [int64]$Marker.schema -ne 1 -or
        -not ($Marker.productId -is [string]) -or
        -not ([string]$Marker.productId).Equals($productId, [System.StringComparison]::Ordinal) -or
        -not ($Marker.kind -is [string]) -or
        [string]$Marker.kind -cnotin @('Target', 'Staging', 'Backup', 'Uninstall') -or
        -not ($Marker.operationId -is [string]) -or
        [string]$Marker.operationId -cnotmatch '^[0-9a-f]{32}$' -or
        -not ($Marker.smokePassed -is [bool])) {
        throw "部署标记字段或类型无效，拒绝自动处理：$MarkerPath"
    }
}

function Read-DeploymentMarkerFile {
    param([Parameter(Mandatory = $true)][string]$MarkerPath)

    try {
        $marker = Get-Content -LiteralPath $MarkerPath -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    catch {
        throw "部署标记损坏，拒绝自动处理：$MarkerPath"
    }
    Assert-ValidDeploymentMarker -Marker $marker -MarkerPath $MarkerPath
    return $marker
}

function Write-DeploymentMarker {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][ValidateSet('Target', 'Staging', 'Backup', 'Uninstall')][string]$Kind,
        [Parameter(Mandatory = $true)][string]$MarkerOperationId,
        [Parameter(Mandatory = $true)][bool]$SmokePassed
    )

    if ($MarkerOperationId -cnotmatch '^[0-9a-f]{32}$') {
        throw "拒绝写入无效的部署操作编号：$MarkerOperationId"
    }
    $markerPath = Join-Path $Path $markerName
    $pendingPath = Join-Path $Path $markerTempName
    $backupPath = Join-Path $Path $markerBackupName
    $payload = [ordered]@{
        schema = 1
        productId = $productId
        kind = $Kind
        operationId = $MarkerOperationId
        smokePassed = $SmokePassed
        updatedUtc = [DateTime]::UtcNow.ToString('o')
    } | ConvertTo-Json
    foreach ($artifactPath in @($pendingPath, $backupPath)) {
        if (Test-Path -LiteralPath $artifactPath) {
            $artifactItem = Get-Item -LiteralPath $artifactPath -Force
            if ($artifactItem.PSIsContainer -or
                ($artifactItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "部署标记事务路径不是普通文件，拒绝覆盖：$artifactPath"
            }
            Remove-Item -LiteralPath $artifactPath -Force
        }
    }
    $replaceExisting = Test-Path -LiteralPath $markerPath
    if ($replaceExisting) {
        $markerItem = Get-Item -LiteralPath $markerPath -Force
        if ($markerItem.PSIsContainer -or
            ($markerItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "部署标记路径不是普通文件，拒绝覆盖：$markerPath"
        }
    }
    $bytes = (New-Object System.Text.UTF8Encoding($false)).GetBytes($payload)
    $stream = New-Object System.IO.FileStream(
        $pendingPath,
        [System.IO.FileMode]::CreateNew,
        [System.IO.FileAccess]::Write,
        [System.IO.FileShare]::None
    )
    try {
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush($true)
    }
    finally {
        $stream.Dispose()
    }
    if ($replaceExisting) {
        # Windows PowerShell 5.1 不能把空备份路径传给 File.Replace；使用同目录受管备份保持原子替换。
        [System.IO.File]::Replace($pendingPath, $markerPath, $backupPath, $true)
        Remove-Item -LiteralPath $backupPath -Force -ErrorAction SilentlyContinue
    }
    else {
        [System.IO.File]::Move($pendingPath, $markerPath)
    }
}

function Read-DeploymentMarker {
    param([Parameter(Mandatory = $true)][string]$Path)

    $markerPath = Join-Path $Path $markerName
    $pendingPath = Join-Path $Path $markerTempName
    $backupPath = Join-Path $Path $markerBackupName
    if (Test-Path -LiteralPath $markerPath) {
        $markerItem = Get-Item -LiteralPath $markerPath -Force
        if ($markerItem.PSIsContainer -or
            ($markerItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "部署标记路径不是普通文件，拒绝自动处理：$markerPath"
        }
        $marker = Read-DeploymentMarkerFile -MarkerPath $markerPath
        foreach ($artifactPath in @($pendingPath, $backupPath)) {
            if (Test-Path -LiteralPath $artifactPath) {
                $artifactItem = Get-Item -LiteralPath $artifactPath -Force
                if ($artifactItem.PSIsContainer -or
                    ($artifactItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                    throw "部署标记事务路径不是普通文件，拒绝自动处理：$artifactPath"
                }
                Remove-Item -LiteralPath $artifactPath -Force
            }
        }
        return $marker
    }

    $recoveryPaths = @(
        @($pendingPath, $backupPath) | Where-Object { Test-Path -LiteralPath $_ }
    )
    if ($recoveryPaths.Count -gt 1) {
        throw "同时发现多个部署标记事务文件，拒绝猜测恢复顺序：$Path"
    }
    if ($recoveryPaths.Count -eq 1) {
        $recoveryPath = [string](@($recoveryPaths)[0])
        $recoveryItem = Get-Item -LiteralPath $recoveryPath -Force
        if ($recoveryItem.PSIsContainer -or
            ($recoveryItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "部署标记事务路径不是普通文件，拒绝恢复：$recoveryPath"
        }
        $recoveredMarker = Read-DeploymentMarkerFile -MarkerPath $recoveryPath
        [System.IO.File]::Move($recoveryPath, $markerPath)
        return $recoveredMarker
    }
    return $null
}

function Assert-RecognizedInstalledTarget {
    param([Parameter(Mandatory = $true)][string]$Path)

    $marker = Read-DeploymentMarker -Path $Path
    if ($null -ne $marker) {
        if ([string]$marker.kind -cnotin @('Target', 'Staging', 'Backup', 'Uninstall')) {
            throw "现有安装目录的部署状态无效，拒绝自动覆盖：$($marker.kind)"
        }
        return
    }
    # 0.3.1 及更早版本没有部署标记；只有完整的旧版运行契约才视为可接管。
    $legacyErrors = @(Get-ContractErrors -Path $Path)
    if ($legacyErrors.Count -gt 0) {
        throw "现有目录没有本软件部署标记，也不符合旧版完整运行契约，拒绝自动覆盖：$($legacyErrors -join '；')。"
    }
}

function Test-RecognizedOrphan {
    param(
        [Parameter(Mandatory = $true)][System.IO.DirectoryInfo]$Directory,
        [Parameter(Mandatory = $true)][ValidateSet('Staging', 'Backup', 'Uninstall')][string]$ExpectedKind
    )

    $prefix = if ($ExpectedKind -eq 'Staging') {
        ".$targetLeaf-staging-"
    }
    elseif ($ExpectedKind -eq 'Backup') {
        ".$targetLeaf-backup-"
    }
    else {
        ".$targetLeaf-uninstall-"
    }
    if (-not $Directory.Name.StartsWith($prefix, [StringComparison]::Ordinal)) {
        return $null
    }
    $suffix = $Directory.Name.Substring($prefix.Length)
    if ($suffix -cnotmatch '^[0-9a-f]{32}$') {
        return $null
    }
    $marker = Read-DeploymentMarker -Path $Directory.FullName
    if ($null -eq $marker -or [string]$marker.kind -cne $ExpectedKind -or
        [string]$marker.operationId -cne $suffix) {
        return $null
    }
    Assert-PlainDirectoryTree -Path $Directory.FullName -Label '遗留部署目录'
    Assert-ManagedTopLevel -Path $Directory.FullName -Label '遗留部署目录'
    return $marker
}

function Remove-ValidatedDeploymentDirectory {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$ExpectedLeaf,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $validated = Assert-ExactProgramsSibling -Path $Path -ExpectedLeaf $ExpectedLeaf
    if (Test-Path -LiteralPath $validated) {
        Assert-PlainDirectoryTree -Path $validated -Label $Label
        Assert-ManagedTopLevel -Path $validated -Label $Label
        Remove-Item -LiteralPath $validated -Recurse -Force
    }
}

function Recover-OrphanedDeployments {
    $recognizedBackups = @()
    $recognizedStaging = @()
    $recognizedUninstalls = @()
    $unknownOrphans = @()
    foreach ($directory in @(Get-ChildItem -LiteralPath $programsRoot -Directory -Force -ErrorAction SilentlyContinue)) {
        $kind = $null
        if ($directory.Name -like ".$targetLeaf-backup-*") {
            $kind = 'Backup'
        }
        elseif ($directory.Name -like ".$targetLeaf-staging-*") {
            $kind = 'Staging'
        }
        elseif ($directory.Name -like ".$targetLeaf-uninstall-*") {
            $kind = 'Uninstall'
        }
        if ($null -eq $kind) {
            continue
        }
        try {
            $marker = Test-RecognizedOrphan -Directory $directory -ExpectedKind $kind
            if ($null -eq $marker) {
                $unknownOrphans += $directory.FullName
            }
            elseif ($kind -eq 'Backup') {
                Assert-DeploymentContract -Path $directory.FullName -Label '遗留旧版本备份'
                $recognizedBackups += $directory
            }
            elseif ($kind -eq 'Uninstall') {
                $recognizedUninstalls += $directory
            }
            elseif ($marker.smokePassed -is [bool] -and $marker.smokePassed -eq $true) {
                Assert-DeploymentContract -Path $directory.FullName -Label '遗留新版本暂存目录' -RequireUninstaller
                $recognizedStaging += $directory
            }
            else {
                Remove-ValidatedDeploymentDirectory -Path $directory.FullName -ExpectedLeaf $directory.Name -Label '未通过冒烟检查的遗留暂存目录'
            }
        }
        catch {
            $unknownOrphans += "$($directory.FullName)（$($_.Exception.Message)）"
        }
    }
    if ($unknownOrphans.Count -gt 0) {
        Write-Warning "发现名称相似但无法确认属于本软件的临时目录，已原样保留：$($unknownOrphans -join '；')"
    }
    foreach ($directory in $recognizedUninstalls) {
        try {
            Remove-ValidatedDeploymentDirectory -Path $directory.FullName -ExpectedLeaf $directory.Name -Label '上次卸载遗留的隔离目录'
        }
        catch {
            Write-Warning "上次卸载的隔离残留仍无法安全清理，已保留：$($directory.FullName)；$($_.Exception.Message)"
        }
    }

    $targetExists = Test-Path -LiteralPath $target
    if ($targetExists) {
        Assert-PlainDirectoryTree -Path $target -Label '现有安装目录'
        Assert-ManagedTopLevel -Path $target -Label '现有安装目录'
        Assert-RecognizedInstalledTarget -Path $target
        $targetErrors = @(Get-ContractErrors -Path $target)
        if ($targetErrors.Count -gt 0) {
            if ($recognizedBackups.Count -ne 1) {
                throw "现有安装不完整（$($targetErrors -join '；')），且没有唯一可安全恢复的旧版本备份。已停止安装，未删除任何目录。"
            }
            Remove-ValidatedDeploymentDirectory -Path $target -ExpectedLeaf $targetLeaf -Label '损坏的现有安装目录'
            $restored = $recognizedBackups[0]
            Move-Item -LiteralPath $restored.FullName -Destination $target
            Write-DeploymentMarker -Path $target -Kind Target -MarkerOperationId $restored.Name.Substring((".$targetLeaf-backup-").Length) -SmokePassed $true
            $recognizedBackups = @()
        }
        else {
            $targetMarker = Read-DeploymentMarker -Path $target
            if ($null -ne $targetMarker -and $targetMarker.kind -ne 'Target') {
                Write-DeploymentMarker -Path $target -Kind Target -MarkerOperationId ([string]$targetMarker.operationId) -SmokePassed $true
            }
        }
    }
    elseif ($recognizedBackups.Count -gt 0) {
        if ($recognizedBackups.Count -ne 1) {
            throw "发现多个可恢复的旧版本备份，无法确定正确版本，已停止安装：$($recognizedBackups.FullName -join '；')"
        }
        $restored = $recognizedBackups[0]
        Move-Item -LiteralPath $restored.FullName -Destination $target
        Write-DeploymentMarker -Path $target -Kind Target -MarkerOperationId $restored.Name.Substring((".$targetLeaf-backup-").Length) -SmokePassed $true
        $recognizedBackups = @()
        $targetExists = $true
    }
    elseif ($recognizedStaging.Count -gt 0) {
        if ($recognizedStaging.Count -ne 1) {
            throw "发现多个已通过检查的新版本暂存目录，无法确定正确版本，已停止安装：$($recognizedStaging.FullName -join '；')"
        }
        $restored = $recognizedStaging[0]
        Move-Item -LiteralPath $restored.FullName -Destination $target
        Write-DeploymentMarker -Path $target -Kind Target -MarkerOperationId $restored.Name.Substring((".$targetLeaf-staging-").Length) -SmokePassed $true
        $recognizedStaging = @()
        $targetExists = $true
    }

    if ($targetExists -and (Test-Path -LiteralPath $target)) {
        Assert-DeploymentContract -Path $target -Label '恢复后的安装目录'
        foreach ($directory in @($recognizedBackups + $recognizedStaging)) {
            if (Test-Path -LiteralPath $directory.FullName) {
                Remove-ValidatedDeploymentDirectory -Path $directory.FullName -ExpectedLeaf $directory.Name -Label '已完成恢复后的遗留部署目录'
            }
        }
    }
}

function Get-DirectorySize {
    param([Parameter(Mandatory = $true)][string]$Path)

    $sum = (Get-ChildItem -LiteralPath $Path -File -Force -Recurse |
        Measure-Object -Property Length -Sum).Sum
    if ($null -eq $sum) {
        return [int64]0
    }
    return [int64]$sum
}

function Assert-SufficientDiskSpace {
    param([Parameter(Mandatory = $true)][string]$SourcePath)

    $sourceBytes = Get-DirectorySize -Path $SourcePath
    $reserveBytes = [Math]::Max([int64](64MB), [int64]($sourceBytes * 0.05))
    $requiredBytes = $sourceBytes + $reserveBytes
    try {
        $drive = [System.IO.DriveInfo]::new([System.IO.Path]::GetPathRoot($programsRoot))
        $availableBytes = [int64]$drive.AvailableFreeSpace
    }
    catch {
        Write-Warning "无法读取安装盘剩余空间，仍将继续；复制阶段如空间不足会自动恢复：$($_.Exception.Message)"
        return
    }
    if ($availableBytes -lt $requiredBytes) {
        $requiredMiB = [Math]::Ceiling($requiredBytes / 1MB)
        $availableMiB = [Math]::Floor($availableBytes / 1MB)
        throw "安装盘空间不足：升级峰值至少需要约 $requiredMiB MiB，当前可用约 $availableMiB MiB。未改动现有安装。"
    }
}

function Stop-ExactCurrentUserExecutable {
    param([Parameter(Mandatory = $true)][string]$ExecutablePath)

    $expected = [System.IO.Path]::GetFullPath($ExecutablePath)
    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $stoppedIds = @()
    $otherPaths = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)
    $processes = @(Get-CimInstance -ClassName Win32_Process -Filter "Name = 'DaShengFaTranslator.exe'" -ErrorAction SilentlyContinue)
    foreach ($process in $processes) {
        if ([string]::IsNullOrWhiteSpace([string]$process.ExecutablePath)) {
            continue
        }
        $actual = [System.IO.Path]::GetFullPath([string]$process.ExecutablePath)
        try {
            $owner = Invoke-CimMethod -InputObject $process -MethodName GetOwner -ErrorAction Stop
            $ownerName = if ([string]::IsNullOrWhiteSpace([string]$owner.Domain)) {
                [string]$owner.User
            }
            else {
                "$($owner.Domain)\$($owner.User)"
            }
        }
        catch {
            if ($actual.Equals($expected, [System.StringComparison]::OrdinalIgnoreCase)) {
                throw "无法确认目标安装目录进程的所有者（PID $($process.ProcessId)）：$($_.Exception.Message)"
            }
            continue
        }
        if (-not $ownerName.Equals($identity, [System.StringComparison]::OrdinalIgnoreCase)) {
            continue
        }
        if (-not $actual.Equals($expected, [System.StringComparison]::OrdinalIgnoreCase)) {
            [void]$otherPaths.Add($actual)
            continue
        }
        try {
            Stop-Process -Id $process.ProcessId -Force -ErrorAction Stop
            $stoppedIds += [int]$process.ProcessId
        }
        catch {
            throw "无法停止当前用户从目标安装目录运行的程序（PID $($process.ProcessId)）：$($_.Exception.Message)"
        }
    }
    if ($stoppedIds.Count -gt 0) {
        Start-Sleep -Milliseconds 350
    }
    return @($otherPaths)
}

function Invoke-StagedSmokeTest {
    param([Parameter(Mandatory = $true)][string]$StagingPath)

    $stagedExe = Join-Path $StagingPath 'DaShengFaTranslator.exe'
    $versionInfo = (Get-Item -LiteralPath $stagedExe).VersionInfo
    $expectedVersion = [string]$versionInfo.ProductVersion
    if ([string]::IsNullOrWhiteSpace($expectedVersion) -or
        [string]$versionInfo.ProductName -ne $productName -or
        [string]$versionInfo.CompanyName -ne $productAuthor) {
        throw '新版本 EXE 的产品名、作者或版本元数据不完整，拒绝安装。'
    }
    $smokeRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("DaShengFaTranslator-install-smoke-" + [System.Guid]::NewGuid().ToString('N'))
    $smokeLocalAppData = Join-Path $smokeRoot 'LocalAppData'
    $reportPath = Join-Path $smokeRoot 'smoke-report.json'
    New-Item -ItemType Directory -Path $smokeLocalAppData -Force | Out-Null
    $oldLocalAppData = $env:LOCALAPPDATA
    try {
        $env:LOCALAPPDATA = $smokeLocalAppData
        $quotedReport = '"' + $reportPath.Replace('"', '\"') + '"'
        $process = Start-Process -FilePath $stagedExe -ArgumentList @('--smoke-test', $quotedReport) -WorkingDirectory $StagingPath -PassThru
        if (-not $process.WaitForExit(90000)) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
            throw '新版本离线引擎冒烟检查等待超过 90 秒。'
        }
        $process.Refresh()
        if ($process.ExitCode -ne 0) {
            throw "新版本离线引擎冒烟检查失败，退出代码：$($process.ExitCode)。"
        }
        if (-not (Test-Path -LiteralPath $reportPath -PathType Leaf)) {
            throw '新版本离线引擎冒烟检查未生成结果文件。'
        }
        $report = Get-Content -LiteralPath $reportPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if (-not ($report.ok -is [bool]) -or $report.ok -ne $true -or
            $report.product -ne $productName -or
            $report.author -ne $productAuthor -or
            $report.version -ne $expectedVersion -or
            $report.resources -ne 'complete' -or
            $report.neural_speech -ne 'ok' -or
            $report.piper_speech -ne 'ok:us,uk' -or
            $report.kokoro_speech -ne 'ok:us,uk' -or
            [string]::IsNullOrWhiteSpace([string]$report.en_to_zh) -or
            [string]::IsNullOrWhiteSpace([string]$report.zh_to_en)) {
            throw "新版本离线引擎冒烟检查结果不完整：$((Get-Content -LiteralPath $reportPath -Raw -Encoding UTF8))"
        }
    }
    finally {
        $env:LOCALAPPDATA = $oldLocalAppData
        if (Test-Path -LiteralPath $smokeRoot) {
            Remove-Item -LiteralPath $smokeRoot -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

function New-ProductShortcut {
    param(
        [Parameter(Mandatory = $true)][object]$Shell,
        [Parameter(Mandatory = $true)][string]$ShortcutPath,
        [Parameter(Mandatory = $true)][string]$TargetPath,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [string]$Arguments = '',
        [string]$Description = ''
    )

    $shortcut = $Shell.CreateShortcut($ShortcutPath)
    $shortcut.TargetPath = $TargetPath
    $shortcut.WorkingDirectory = $WorkingDirectory
    $shortcut.Arguments = $Arguments
    $shortcut.IconLocation = "$exe,0"
    $shortcut.Description = $Description
    $shortcut.Save()
}

$target = Assert-ExactProgramsSibling -Path $target -ExpectedLeaf $targetLeaf
$staging = Assert-ExactProgramsSibling -Path $staging -ExpectedLeaf $stagingLeaf
$backup = Assert-ExactProgramsSibling -Path $backup -ExpectedLeaf $backupLeaf

$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$mutexName = "Local\DaShengFaTranslator.Install.$sid"
$mutex = New-Object System.Threading.Mutex($false, $mutexName)
$mutexAcquired = $false
try {
    try {
        $mutexAcquired = $mutex.WaitOne([TimeSpan]::FromSeconds(30))
    }
    catch [System.Threading.AbandonedMutexException] {
        $mutexAcquired = $true
    }
    if (-not $mutexAcquired) {
        throw '安装失败：另一个安装或卸载操作仍在进行，请稍后重试。'
    }

    New-Item -ItemType Directory -Force -Path $programsRoot | Out-Null
    $programsRootItem = Get-Item -LiteralPath $programsRoot -Force
    if (-not $programsRootItem.PSIsContainer -or
        ($programsRootItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "拒绝安装：Programs 目录不是普通文件夹：$programsRoot"
    }

    Recover-OrphanedDeployments

    Assert-PlainDirectoryTree -Path $source -Label '安装包程序目录'
    Assert-PackageTopLevel -Path $source
    Assert-DeploymentContract -Path $source -Label '安装包'
    if (-not (Test-Path -LiteralPath $packagedUninstaller -PathType Leaf) -or
        (Get-Item -LiteralPath $packagedUninstaller).Length -lt 128) {
        throw '安装包不完整：找不到有效的 uninstall.ps1。请重新解压完整安装包。'
    }
    Assert-ValidPowerShellScript -Path $packagedUninstaller -Label '安装包卸载脚本'
    Assert-SufficientDiskSpace -SourcePath $source

    if ((Test-Path -LiteralPath $staging) -or (Test-Path -LiteralPath $backup)) {
        throw '安装失败：本次部署的临时目录已经存在。'
    }

    $hadOriginalTarget = Test-Path -LiteralPath $target
    $oldMoved = $false
    $newPromoted = $false
    $committed = $false
    $otherInstancePaths = @()

    try {
        New-Item -ItemType Directory -Path $staging | Out-Null
        Write-DeploymentMarker -Path $staging -Kind Staging -MarkerOperationId $operationId -SmokePassed $false
        foreach ($sourceItem in @(Get-ChildItem -LiteralPath $source -Force)) {
            Copy-Item -LiteralPath $sourceItem.FullName -Destination $staging -Recurse -Force
        }
        Copy-Item -LiteralPath $packagedUninstaller -Destination (Join-Path $staging 'uninstall.ps1') -Force
        Assert-PlainDirectoryTree -Path $staging -Label '新版本暂存目录'
        Assert-ManagedTopLevel -Path $staging -Label '新版本暂存目录'
        Assert-DeploymentContract -Path $staging -Label '新版本暂存目录' -RequireUninstaller
        Assert-ValidPowerShellScript -Path (Join-Path $staging 'uninstall.ps1') -Label '暂存目录卸载脚本'
        Invoke-StagedSmokeTest -StagingPath $staging
        Write-DeploymentMarker -Path $staging -Kind Staging -MarkerOperationId $operationId -SmokePassed $true

        $otherInstancePaths = @(Stop-ExactCurrentUserExecutable -ExecutablePath $exe)

        if ((Test-Path -LiteralPath $target) -ne $hadOriginalTarget) {
            throw '安装已取消：安装目录在准备期间被其他程序改变。'
        }

        if ($hadOriginalTarget) {
            Assert-PlainDirectoryTree -Path $target -Label '现有安装目录'
            Assert-ManagedTopLevel -Path $target -Label '现有安装目录'
            Assert-RecognizedInstalledTarget -Path $target
            Assert-DeploymentContract -Path $target -Label '现有安装目录'
            Write-DeploymentMarker -Path $target -Kind Backup -MarkerOperationId $operationId -SmokePassed $true
            Move-Item -LiteralPath $target -Destination $backup
            $oldMoved = $true
        }

        Move-Item -LiteralPath $staging -Destination $target
        $newPromoted = $true
        Write-DeploymentMarker -Path $target -Kind Target -MarkerOperationId $operationId -SmokePassed $true
        Assert-DeploymentContract -Path $target -Label '接管后的新版本' -RequireUninstaller

        $committed = $true
        if ($oldMoved) {
            try {
                Remove-ValidatedDeploymentDirectory -Path $backup -ExpectedLeaf $backupLeaf -Label '旧版本备份目录'
            }
            catch {
                # 新版本已经完整接管；不冒险回滚到可能已被部分清理或新增用户文件的备份。
                Write-Warning "新版本安装成功，但旧版本备份未能安全清理，已原样保留：$backup；$($_.Exception.Message)"
            }
            $oldMoved = $false
        }
    }
    catch {
        $installError = $_
        $rollbackError = $null
        try {
            if ($oldMoved -and (Test-Path -LiteralPath $backup)) {
                if (Test-Path -LiteralPath $target) {
                    Remove-ValidatedDeploymentDirectory -Path $target -ExpectedLeaf $targetLeaf -Label '未完成的新版本目录'
                }
                Move-Item -LiteralPath $backup -Destination $target
                Write-DeploymentMarker -Path $target -Kind Target -MarkerOperationId $operationId -SmokePassed $true
                $oldMoved = $false
            }
            elseif ($newPromoted -and -not $hadOriginalTarget -and (Test-Path -LiteralPath $target)) {
                Remove-ValidatedDeploymentDirectory -Path $target -ExpectedLeaf $targetLeaf -Label '未完成的新版本目录'
            }
            elseif (-not $oldMoved -and $hadOriginalTarget -and (Test-Path -LiteralPath $target)) {
                $currentMarker = Read-DeploymentMarker -Path $target
                if ($null -ne $currentMarker -and
                    $currentMarker.kind -eq 'Backup' -and
                    $currentMarker.operationId -eq $operationId) {
                    Write-DeploymentMarker -Path $target -Kind Target -MarkerOperationId $operationId -SmokePassed $true
                }
            }
        }
        catch {
            $rollbackError = $_
        }

        if ($null -ne $rollbackError) {
            throw "安装失败且自动恢复未完成。原错误：$($installError.Exception.Message)；恢复错误：$($rollbackError.Exception.Message)；旧版本备份（如存在）：$backup"
        }
        throw $installError
    }
    finally {
        if (-not $committed -and (Test-Path -LiteralPath $staging)) {
            try {
                Remove-ValidatedDeploymentDirectory -Path $staging -ExpectedLeaf $stagingLeaf -Label '新版本暂存目录'
            }
            catch {
                Write-Warning "未能清理安装暂存目录：$staging；$($_.Exception.Message)"
            }
        }
    }

    $desktopShortcutPath = Join-Path ([Environment]::GetFolderPath('Desktop')) '大声发划词翻译.lnk'
    $startMenuDir = Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs'
    $startShortcutPath = Join-Path $startMenuDir '大声发划词翻译.lnk'
    $uninstallShortcutPath = Join-Path $startMenuDir '大声发划词翻译 - 卸载.lnk'
    $uninstallArguments = '-NoProfile -ExecutionPolicy Bypass -File "' + (Join-Path $target 'uninstall.ps1') + '"'
    $shortcutErrors = @()
    try {
        $shell = New-Object -ComObject WScript.Shell
        $shortcutSpecs = @(
            @{
                Path = $desktopShortcutPath
                Target = $exe
                WorkingDirectory = $target
                Arguments = ''
                Description = '大声发划词翻译：桌面划词翻译与英美发音'
            },
            @{
                Path = $startShortcutPath
                Target = $exe
                WorkingDirectory = $target
                Arguments = ''
                Description = '大声发划词翻译：桌面划词翻译与英美发音'
            },
            @{
                Path = $uninstallShortcutPath
                Target = (Join-Path $PSHOME 'powershell.exe')
                WorkingDirectory = $programsRoot
                Arguments = $uninstallArguments
                Description = '卸载大声发划词翻译（默认保留设置和翻译缓存）'
            }
        )
        foreach ($spec in $shortcutSpecs) {
            try {
                New-ProductShortcut `
                    -Shell $shell `
                    -ShortcutPath $spec.Path `
                    -TargetPath $spec.Target `
                    -WorkingDirectory $spec.WorkingDirectory `
                    -Arguments $spec.Arguments `
                    -Description $spec.Description
            }
            catch {
                $shortcutErrors += "$($spec.Path)：$($_.Exception.Message)"
            }
        }
    }
    catch {
        $shortcutErrors += "无法使用 Windows 快捷方式服务：$($_.Exception.Message)"
    }
    if ($shortcutErrors.Count -gt 0) {
        Write-Warning "程序主体已安装成功，但以下快捷方式未能创建：$($shortcutErrors -join '；')"
    }

    if ($otherInstancePaths.Count -gt 0) {
        Write-Warning "检测到当前用户仍在运行其他位置的便携/测试副本，已保留且未强制结束：$($otherInstancePaths -join '；')。请关闭它们后，从新快捷方式启动正式安装版。"
        Write-Host '程序主体安装完成；为避免与便携副本争用单实例，本次没有自动启动正式安装版。' -ForegroundColor Green
    }
    else {
        try {
            $startedProcess = Start-Process -FilePath $exe -WorkingDirectory $target -PassThru
            Start-Sleep -Milliseconds 1200
            if ($startedProcess.HasExited) {
                Write-Warning "程序主体安装成功，但正式安装版启动后立即退出（退出代码 $($startedProcess.ExitCode)）。请从开始菜单重试并查看日志。"
            }
            else {
                Write-Host '安装完成。新版本已通过离线引擎检查，正式安装版已经启动。' -ForegroundColor Green
            }
        }
        catch {
            Write-Warning "程序主体与可创建的快捷方式已经安装完成，但程序未能自动启动：$($_.Exception.Message)"
        }
    }
}
finally {
    if ($mutexAcquired) {
        $mutex.ReleaseMutex()
    }
    $mutex.Dispose()
}
