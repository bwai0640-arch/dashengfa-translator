param(
    [switch]$RemoveUserData
)

$ErrorActionPreference = 'Stop'

$productId = 'DaShengFaTranslator'
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

if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
    throw '卸载失败：无法确定当前用户的 LOCALAPPDATA 目录。'
}

$programsRoot = [System.IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'Programs'))
$target = [System.IO.Path]::GetFullPath((Join-Path $programsRoot $targetLeaf))
$exe = Join-Path $target 'DaShengFaTranslator.exe'
$operationId = [System.Guid]::NewGuid().ToString('N')
$quarantineLeaf = ".$targetLeaf-uninstall-$operationId"
$quarantine = [System.IO.Path]::GetFullPath((Join-Path $programsRoot $quarantineLeaf))

function Assert-ExactTargetPath {
    $parent = [System.IO.Path]::GetDirectoryName($target)
    $leaf = [System.IO.Path]::GetFileName($target)
    if (-not $parent.Equals($programsRoot, [System.StringComparison]::OrdinalIgnoreCase) -or
        -not $leaf.Equals($targetLeaf, [System.StringComparison]::Ordinal)) {
        throw "拒绝卸载：目标路径校验失败：$target"
    }
}

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
        throw "拒绝卸载：隔离路径校验失败：$fullPath"
    }
    return $fullPath
}

function Assert-PlainDirectoryTree {
    param([Parameter(Mandatory = $true)][string]$Path)

    $rootItem = Get-Item -LiteralPath $Path -Force
    if (-not $rootItem.PSIsContainer -or
        ($rootItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "拒绝卸载：安装目录不是普通文件夹：$Path"
    }
    $reparsePoint = Get-ChildItem -LiteralPath $Path -Force -Recurse |
        Where-Object {
            ($_.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0
        } |
        Select-Object -First 1
    if ($null -ne $reparsePoint) {
        throw "拒绝卸载：安装目录内含符号链接或目录联接：$($reparsePoint.FullName)"
    }
}

function Assert-ManagedTopLevel {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $unknown = @(
        Get-ChildItem -LiteralPath $Path -Force |
            Where-Object { $_.Name -notin $managedTopLevelNames } |
            ForEach-Object { $_.Name }
    )
    if ($unknown.Count -gt 0) {
        $listed = ($unknown | Sort-Object) -join '、'
        throw "$Label 含有不属于本软件管理的顶层项目，已拒绝删除：$listed。请先将这些项目移出后重试。"
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
        throw "安装标记字段或类型无效，拒绝卸载：$MarkerPath"
    }
}

function Read-DeploymentMarkerFile {
    param([Parameter(Mandatory = $true)][string]$MarkerPath)

    try {
        $marker = Get-Content -LiteralPath $MarkerPath -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    catch {
        throw "安装标记损坏，拒绝卸载：$MarkerPath"
    }
    Assert-ValidDeploymentMarker -Marker $marker -MarkerPath $MarkerPath
    return $marker
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
            throw "安装标记路径不是普通文件，拒绝卸载：$markerPath"
        }
        $marker = Read-DeploymentMarkerFile -MarkerPath $markerPath
        foreach ($artifactPath in @($pendingPath, $backupPath)) {
            if (Test-Path -LiteralPath $artifactPath) {
                $artifactItem = Get-Item -LiteralPath $artifactPath -Force
                if ($artifactItem.PSIsContainer -or
                    ($artifactItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                    throw "安装标记事务路径不是普通文件，拒绝卸载：$artifactPath"
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
        throw "同时发现多个安装标记事务文件，拒绝猜测恢复顺序：$Path"
    }
    if ($recoveryPaths.Count -eq 1) {
        $recoveryPath = [string](@($recoveryPaths)[0])
        $recoveryItem = Get-Item -LiteralPath $recoveryPath -Force
        if ($recoveryItem.PSIsContainer -or
            ($recoveryItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "安装标记事务路径不是普通文件，拒绝恢复：$recoveryPath"
        }
        $recoveredMarker = Read-DeploymentMarkerFile -MarkerPath $recoveryPath
        [System.IO.File]::Move($recoveryPath, $markerPath)
        return $recoveredMarker
    }
    return $null
}

function Write-DeploymentMarker {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][ValidateSet('Target', 'Uninstall')][string]$Kind,
        [Parameter(Mandatory = $true)][string]$MarkerOperationId,
        [Parameter(Mandatory = $true)][bool]$SmokePassed
    )

    if ($MarkerOperationId -cnotmatch '^[0-9a-f]{32}$') {
        throw "拒绝写入无效的卸载操作编号：$MarkerOperationId"
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
                throw "安装标记事务路径不是普通文件，拒绝覆盖：$artifactPath"
            }
            Remove-Item -LiteralPath $artifactPath -Force
        }
    }
    $replaceExisting = Test-Path -LiteralPath $markerPath
    if ($replaceExisting) {
        $markerItem = Get-Item -LiteralPath $markerPath -Force
        if ($markerItem.PSIsContainer -or
            ($markerItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "安装标记路径不是普通文件，拒绝覆盖：$markerPath"
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
        # Windows PowerShell 5.1 不能把空备份路径传给 File.Replace。
        [System.IO.File]::Replace($pendingPath, $markerPath, $backupPath, $true)
        Remove-Item -LiteralPath $backupPath -Force -ErrorAction SilentlyContinue
    }
    else {
        [System.IO.File]::Move($pendingPath, $markerPath)
    }
}

function Assert-ManagedInstallation {
    Assert-ManagedTopLevel -Path $target -Label '安装目录'

    $marker = Read-DeploymentMarker -Path $target
    if ($null -ne $marker) {
        if ([string]$marker.kind -cnotin @('Target', 'Uninstall')) {
            throw "安装标记不属于本软件的可卸载状态，拒绝卸载：$($marker.kind)"
        }
        return
    }

    if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {
        throw '现有目录没有本软件安装标记，也找不到主程序；为避免误删，已拒绝卸载。'
    }
    $legacyRequirements = @(
        '_internal\resources\ecdict.db',
        '_internal\resources\app_icon.png',
        '_internal\resources\app_icon.ico',
        '_internal\resources\models\translate-en_zh-1_9\model\model.bin',
        '_internal\resources\models\translate-zh_en-1_9\model\model.bin'
    )
    $missingLegacyFiles = @(
        $legacyRequirements |
            Where-Object { -not (Test-Path -LiteralPath (Join-Path $target $_) -PathType Leaf) }
    )
    if ($missingLegacyFiles.Count -gt 0) {
        throw "现有目录没有本软件安装标记，也不符合旧版完整运行契约；为避免误删，已拒绝卸载：$($missingLegacyFiles -join '、')。"
    }
}

function Stop-ExactCurrentUserExecutable {
    $expected = [System.IO.Path]::GetFullPath($exe)
    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $stoppedIds = @()
    $processes = @(Get-CimInstance -ClassName Win32_Process -Filter "Name = 'DaShengFaTranslator.exe'" -ErrorAction SilentlyContinue)
    foreach ($process in $processes) {
        if ([string]::IsNullOrWhiteSpace([string]$process.ExecutablePath)) {
            continue
        }
        $actual = [System.IO.Path]::GetFullPath([string]$process.ExecutablePath)
        if (-not $actual.Equals($expected, [System.StringComparison]::OrdinalIgnoreCase)) {
            continue
        }
        try {
            $owner = Invoke-CimMethod -InputObject $process -MethodName GetOwner -ErrorAction Stop
            $ownerName = if ([string]::IsNullOrWhiteSpace([string]$owner.Domain)) {
                [string]$owner.User
            }
            else {
                "$($owner.Domain)\$($owner.User)"
            }
            if (-not $ownerName.Equals($identity, [System.StringComparison]::OrdinalIgnoreCase)) {
                continue
            }
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
}

function Remove-UninstallQuarantine {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$ExpectedLeaf
    )

    $validated = Assert-ExactProgramsSibling -Path $Path -ExpectedLeaf $ExpectedLeaf
    Assert-PlainDirectoryTree -Path $validated
    Assert-ManagedTopLevel -Path $validated -Label '卸载隔离目录'
    $marker = Read-DeploymentMarker -Path $validated
    $prefix = ".$targetLeaf-uninstall-"
    $expectedOperationId = if ($ExpectedLeaf.StartsWith($prefix, [System.StringComparison]::Ordinal)) {
        $ExpectedLeaf.Substring($prefix.Length)
    }
    else {
        ''
    }
    if ($null -eq $marker -or $marker.kind -ne 'Uninstall' -or
        $marker.operationId -cne $expectedOperationId) {
        throw "隔离目录缺少匹配的本软件卸载标记，拒绝删除：$validated"
    }

    # 先删受管理内容，最后才删标记和空目录。中途失败时标记仍在，下一次可继续清理。
    foreach ($name in @('DaShengFaTranslator.exe', '_internal', 'uninstall.ps1', $markerTempName, $markerBackupName)) {
        $itemPath = Join-Path $validated $name
        if (Test-Path -LiteralPath $itemPath) {
            Remove-Item -LiteralPath $itemPath -Recurse -Force
        }
    }
    Assert-ManagedTopLevel -Path $validated -Label '卸载隔离目录'
    $markerPath = Join-Path $validated $markerName
    if (Test-Path -LiteralPath $markerPath -PathType Leaf) {
        Remove-Item -LiteralPath $markerPath -Force
    }
    Remove-Item -LiteralPath $validated -Force
}

function Remove-RecognizedUninstallQuarantines {
    foreach ($directory in @(Get-ChildItem -LiteralPath $programsRoot -Directory -Force -ErrorAction SilentlyContinue)) {
        $prefix = ".$targetLeaf-uninstall-"
        if (-not $directory.Name.StartsWith($prefix, [System.StringComparison]::Ordinal)) {
            continue
        }
        $suffix = $directory.Name.Substring($prefix.Length)
        if ($suffix -cnotmatch '^[0-9a-f]{32}$') {
            Write-Warning "发现名称相似但无法确认属于本软件的卸载残留，已保留：$($directory.FullName)"
            continue
        }
        try {
            Remove-UninstallQuarantine -Path $directory.FullName -ExpectedLeaf $directory.Name
        }
        catch {
            throw "上次卸载的隔离残留仍无法安全清理：$($directory.FullName)；$($_.Exception.Message)"
        }
    }
}

function Get-ValidatedUserDataPaths {
    $dataPaths = @(
        [System.IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'DaShengFaTranslator')),
        [System.IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'WordLocalTranslator'))
    )
    $existingPaths = @()
    foreach ($dataPath in $dataPaths) {
        $leaf = [System.IO.Path]::GetFileName($dataPath)
        $parent = [System.IO.Path]::GetDirectoryName($dataPath)
        if ($leaf -notin @('DaShengFaTranslator', 'WordLocalTranslator') -or
            -not $parent.Equals([System.IO.Path]::GetFullPath($env:LOCALAPPDATA), [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "拒绝删除用户数据：路径校验失败：$dataPath"
        }
        if (-not (Test-Path -LiteralPath $dataPath)) {
            continue
        }
        $item = Get-Item -LiteralPath $dataPath -Force
        if (-not $item.PSIsContainer -or
            ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "拒绝删除用户数据：不是普通文件夹：$dataPath"
        }
        $dataReparsePoint = Get-ChildItem -LiteralPath $dataPath -Force -Recurse |
            Where-Object {
                ($_.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0
            } |
            Select-Object -First 1
        if ($null -ne $dataReparsePoint) {
            throw "拒绝删除用户数据：目录内含符号链接或目录联接：$($dataReparsePoint.FullName)"
        }
        $existingPaths += $dataPath
    }
    return @($existingPaths)
}

function Remove-ShortcutIfOwned {
    param(
        [Parameter(Mandatory = $true)][string]$ShortcutPath,
        [Parameter(Mandatory = $true)][string[]]$ExpectedTargets,
        [string]$ExpectedArgumentsContain = ''
    )

    if (-not (Test-Path -LiteralPath $ShortcutPath -PathType Leaf)) {
        return
    }
    try {
        $shell = New-Object -ComObject WScript.Shell
        $shortcut = $shell.CreateShortcut($ShortcutPath)
        $shortcutTarget = [System.IO.Path]::GetFullPath([string]$shortcut.TargetPath)
        $owned = $false
        foreach ($expectedTarget in $ExpectedTargets) {
            if ($shortcutTarget.Equals([System.IO.Path]::GetFullPath($expectedTarget), [System.StringComparison]::OrdinalIgnoreCase)) {
                $owned = $true
                break
            }
        }
        if ($owned -and -not [string]::IsNullOrWhiteSpace($ExpectedArgumentsContain)) {
            $owned = ([string]$shortcut.Arguments).IndexOf($ExpectedArgumentsContain, [System.StringComparison]::OrdinalIgnoreCase) -ge 0
        }
        if ($owned) {
            Remove-Item -LiteralPath $ShortcutPath -Force
        }
        else {
            Write-Warning "同名快捷方式并不指向本次安装，已保留：$ShortcutPath"
        }
    }
    catch {
        Write-Warning "无法确认或删除快捷方式，已保留：$ShortcutPath；$($_.Exception.Message)"
    }
}

Assert-ExactTargetPath
$quarantine = Assert-ExactProgramsSibling -Path $quarantine -ExpectedLeaf $quarantineLeaf

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
        throw '卸载失败：另一个安装或卸载操作仍在进行，请稍后重试。'
    }

    # 显式删除用户数据时，先把两个目录全部预检完；任一不安全则主体程序也保持不变。
    $validatedDataPaths = if ($RemoveUserData) {
        @(Get-ValidatedUserDataPaths)
    }
    else {
        @()
    }

    Remove-RecognizedUninstallQuarantines

    if (Test-Path -LiteralPath $target) {
        Assert-PlainDirectoryTree -Path $target
        Assert-ManagedInstallation
        Stop-ExactCurrentUserExecutable

        # 先同卷隔离正式路径；快捷方式必须等隔离目录完整删除成功后再处理。
        Assert-PlainDirectoryTree -Path $target
        Assert-ManagedInstallation
        try {
            Write-DeploymentMarker -Path $target -Kind Uninstall -MarkerOperationId $operationId -SmokePassed $true
            Move-Item -LiteralPath $target -Destination $quarantine
        }
        catch {
            if (Test-Path -LiteralPath $target) {
                try {
                    Write-DeploymentMarker -Path $target -Kind Target -MarkerOperationId $operationId -SmokePassed $true
                }
                catch {
                    throw "卸载隔离失败，且安装状态标记未能恢复。安装目录仍在：$target；$($_.Exception.Message)"
                }
            }
            throw "卸载隔离失败，安装目录与快捷方式均未删除：$($_.Exception.Message)"
        }
        try {
            Remove-UninstallQuarantine -Path $quarantine -ExpectedLeaf $quarantineLeaf
        }
        catch {
            throw "程序已从正式安装位置隔离，但未能完全删除；快捷方式仍保留。残留目录：$quarantine；$($_.Exception.Message)"
        }
    }

    $desktopShortcut = Join-Path ([Environment]::GetFolderPath('Desktop')) '大声发划词翻译.lnk'
    $startMenuDir = Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs'
    $startShortcut = Join-Path $startMenuDir '大声发划词翻译.lnk'
    $uninstallShortcut = Join-Path $startMenuDir '大声发划词翻译 - 卸载.lnk'
    Remove-ShortcutIfOwned -ShortcutPath $desktopShortcut -ExpectedTargets @($exe)
    Remove-ShortcutIfOwned -ShortcutPath $startShortcut -ExpectedTargets @($exe)
    Remove-ShortcutIfOwned -ShortcutPath $uninstallShortcut -ExpectedTargets @((Join-Path $PSHOME 'powershell.exe')) -ExpectedArgumentsContain (Join-Path $target 'uninstall.ps1')

    if ($RemoveUserData) {
        try {
            # 主体卸载期间目录可能被同步软件或其他进程改变，删除前重新验证当前状态。
            $validatedDataPaths = @(Get-ValidatedUserDataPaths)
            foreach ($dataPath in $validatedDataPaths) {
                Remove-Item -LiteralPath $dataPath -Recurse -Force
            }
        }
        catch {
            throw "程序主体已经卸载，但用户数据未能全部删除：$($_.Exception.Message)"
        }
    }

    Write-Host '卸载完成。翻译缓存默认保留。' -ForegroundColor Green
}
finally {
    if ($mutexAcquired) {
        $mutex.ReleaseMutex()
    }
    $mutex.Dispose()
}
