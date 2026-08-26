from __future__ import annotations

import base64
import locale
import re
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class InstallerSafetyContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.install_path = ROOT / "install.ps1"
        cls.uninstall_path = ROOT / "uninstall.ps1"
        cls.install = cls.install_path.read_text(encoding="utf-8-sig")
        cls.uninstall = cls.uninstall_path.read_text(encoding="utf-8-sig")
        cls.powershell = shutil.which("powershell.exe") or shutil.which("powershell")

    def run_powershell_functions(
        self, source: Path, function_names: tuple[str, ...], body: str
    ) -> str:
        if not self.powershell:
            self.skipTest("Windows PowerShell is unavailable")

        quoted_source = str(source).replace("'", "''")
        wanted = ", ".join("'" + name.replace("'", "''") + "'" for name in function_names)
        script = f"""
$ErrorActionPreference = 'Stop'
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    '{quoted_source}', [ref]$tokens, [ref]$parseErrors
)
if ($parseErrors.Count -ne 0) {{
    throw (($parseErrors | ForEach-Object {{ $_.Message }}) -join '; ')
}}
$wanted = @({wanted})
$definitions = @(
    $ast.FindAll({{
        param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
            $wanted -contains $node.Name
    }}, $true) | ForEach-Object {{ $_.Extent.Text }}
)
if ($definitions.Count -ne $wanted.Count) {{
    throw "Requested $($wanted.Count) functions but found $($definitions.Count)."
}}
if ($definitions.Count -gt 0) {{
    Invoke-Expression ($definitions -join [Environment]::NewLine)
}}
{body}
"""
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        result = subprocess.run(
            [
                self.powershell,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-EncodedCommand",
                encoded,
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding=locale.getpreferredencoding(False),
            errors="replace",
            timeout=30,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            0,
            msg=f"PowerShell harness failed.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )
        return result.stdout

    def test_windows_powershell_scripts_keep_utf8_bom(self) -> None:
        self.assertTrue(self.install_path.read_bytes().startswith(b"\xef\xbb\xbf"))
        self.assertTrue(self.uninstall_path.read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_scripts_parse_in_windows_powershell(self) -> None:
        for source in (self.install_path, self.uninstall_path):
            output = self.run_powershell_functions(
                source, (), "Write-Output 'PARSE_OK'"
            )
            self.assertIn("PARSE_OK", output)

    def test_scripts_share_a_per_user_install_mutex(self) -> None:
        expected = '"Local\\DaShengFaTranslator.Install.$sid"'
        self.assertIn(expected, self.install)
        self.assertIn(expected, self.uninstall)
        self.assertIn("AbandonedMutexException", self.install)
        self.assertIn("AbandonedMutexException", self.uninstall)

    def test_upgrade_and_uninstall_reject_unknown_top_level_items(self) -> None:
        for script in (self.install, self.uninstall):
            self.assertIn("$managedTopLevelNames", script)
            self.assertRegex(script, r"\.Name\s+-notin\s+\$managedTopLevelNames")
            self.assertIn("不属于本软件管理的顶层项目", script)

    def test_staging_contract_covers_offline_runtime_and_binary_headers(self) -> None:
        required = (
            "DaShengFaTranslator.exe",
            "_internal\\resources\\ecdict.db",
            "_internal\\resources\\app_icon.png",
            "_internal\\resources\\app_icon.ico",
            "translate-en_zh-1_9\\model\\model.bin",
            "translate-en_zh-1_9\\sentencepiece.model",
            "translate-zh_en-1_9\\model\\model.bin",
            "translate-zh_en-1_9\\sentencepiece.model",
            "kokoro\\kokoro-v1.0.int8.onnx",
            "kokoro\\voices-v1.0.bin",
            "piper\\en_US-lessac-high.onnx",
            "piper\\en_US-lessac-high.onnx.json",
            "piper\\en_US-lessac-high.MODEL_CARD.md",
            "piper\\en_GB-cori-high.onnx",
            "piper\\en_GB-cori-high.onnx.json",
            "piper\\en_GB-cori-high.MODEL_CARD.md",
            "piper\\PIPER_GPL-3.0.txt",
            "piper\\README.md",
            "UIAutomationClient_VC140_X64.dll",
            "UIAutomationClient_VC140_X86.dll",
            "uninstall.ps1",
            "PNG 图标",
            "ICO 图标",
            "x64 UIAutomation DLL",
            "x86 UIAutomation DLL",
        )
        for name in required:
            self.assertIn(name, self.install)

    def test_staging_is_marked_before_any_package_copy(self) -> None:
        create = self.install.index("New-Item -ItemType Directory -Path $staging")
        mark = self.install.index(
            "Write-DeploymentMarker -Path $staging -Kind Staging -MarkerOperationId $operationId -SmokePassed $false"
        )
        copy = self.install.index(
            "foreach ($sourceItem in @(Get-ChildItem -LiteralPath $source -Force))"
        )
        self.assertLess(create, mark)
        self.assertLess(mark, copy)

    def test_smoke_test_precedes_target_replacement(self) -> None:
        smoke = self.install.index("Invoke-StagedSmokeTest -StagingPath $staging")
        stop = self.install.index("Stop-ExactCurrentUserExecutable -ExecutablePath $exe")
        replace = self.install.index("Move-Item -LiteralPath $target -Destination $backup")
        self.assertLess(smoke, stop)
        self.assertLess(stop, replace)
        self.assertIn("$env:LOCALAPPDATA = $smokeLocalAppData", self.install)
        self.assertIn("$process.WaitForExit(90000)", self.install)

    def test_smoke_test_validates_executable_and_report_identity(self) -> None:
        for token in (
            ".ProductName",
            ".CompanyName",
            ".ProductVersion",
            "$report.product -ne $productName",
            "$report.author -ne $productAuthor",
            "$report.version -ne $expectedVersion",
            "$report.resources -ne 'complete'",
            "$report.neural_speech -ne 'ok'",
            "$report.piper_speech -ne 'ok:us,uk'",
            "$report.kokoro_speech -ne 'ok:us,uk'",
            "-not ($report.ok -is [bool])",
        ):
            self.assertIn(token, self.install)

    def test_marker_updates_are_atomic_and_strictly_typed(self) -> None:
        for script in (self.install, self.uninstall):
            self.assertIn("$markerTempName = '.dashengfa-install.json.tmp'", script)
            self.assertIn("$markerBackupName = '.dashengfa-install.json.bak'", script)
            self.assertIn("$stream.Flush($true)", script)
            self.assertIn("[System.IO.File]::Replace", script)
            self.assertNotIn("$markerPath, $null", script)
            self.assertIn("[System.IO.File]::Move($pendingPath, $markerPath)", script)
            self.assertIn("-not ($Marker.smokePassed -is [bool])", script)
            self.assertIn("-cnotmatch '^[0-9a-f]{32}$'", script)
            self.assertIn("$Marker.kind -cnotin", script)
            self.assertIn("Assert-ValidDeploymentMarker", script)

    def test_marker_round_trip_recovery_and_type_rejection(self) -> None:
        output = self.run_powershell_functions(
            self.install_path,
            (
                "Assert-ValidDeploymentMarker",
                "Read-DeploymentMarkerFile",
                "Write-DeploymentMarker",
                "Read-DeploymentMarker",
            ),
            r"""
$productId = 'DaShengFaTranslator'
$markerName = '.dashengfa-install.json'
$markerTempName = '.dashengfa-install.json.tmp'
$markerBackupName = '.dashengfa-install.json.bak'
$operationId = 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
$testRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('dsf-marker-test-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $testRoot | Out-Null
try {
    Write-DeploymentMarker -Path $testRoot -Kind Target -MarkerOperationId $operationId -SmokePassed $true
    # A second write exercises File.Replace rather than only the new-file path.
    Write-DeploymentMarker -Path $testRoot -Kind Backup -MarkerOperationId $operationId -SmokePassed $true
    $marker = Read-DeploymentMarker -Path $testRoot
    if ($marker.kind -ne 'Backup' -or -not ($marker.smokePassed -is [bool])) {
        throw 'Valid marker did not round-trip.'
    }
    $markerPath = Join-Path $testRoot $markerName
    $pendingPath = Join-Path $testRoot $markerTempName
    [System.IO.File]::Move($markerPath, $pendingPath)
    $recovered = Read-DeploymentMarker -Path $testRoot
    if ($recovered.kind -ne 'Backup' -or $recovered.operationId -ne $operationId -or -not (Test-Path -LiteralPath $markerPath -PathType Leaf)) {
        throw 'Pending marker was not recovered.'
    }
    $backupPath = Join-Path $testRoot $markerBackupName
    [System.IO.File]::Move($markerPath, $backupPath)
    $backupRecovered = Read-DeploymentMarker -Path $testRoot
    if ($backupRecovered.kind -ne 'Backup' -or -not (Test-Path -LiteralPath $markerPath -PathType Leaf)) {
        throw 'Transaction backup marker was not recovered.'
    }
    $invalid = '{"schema":1,"productId":"DaShengFaTranslator","kind":"Target","operationId":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","smokePassed":"true"}'
    [System.IO.File]::WriteAllText($markerPath, $invalid, (New-Object System.Text.UTF8Encoding($false)))
    $rejected = $false
    try { [void](Read-DeploymentMarker -Path $testRoot) } catch { $rejected = $true }
    if (-not $rejected) { throw 'String smokePassed was accepted.' }
    Write-Output 'MARKER_BEHAVIOR_OK'
}
finally {
    if (Test-Path -LiteralPath $testRoot) { Remove-Item -LiteralPath $testRoot -Recurse -Force }
}
""",
        )
        self.assertIn("MARKER_BEHAVIOR_OK", output)

    def test_process_stop_is_owner_and_exact_path_scoped(self) -> None:
        for script in (self.install, self.uninstall):
            self.assertIn("ExecutablePath", script)
            self.assertIn("GetOwner", script)
            self.assertIn("WindowsIdentity", script)
            self.assertIn("OrdinalIgnoreCase", script)
            self.assertNotRegex(
                script,
                r"Get-Process\s+-Name\s+['\"]?DaShengFaTranslator['\"]?.*?\|\s*Stop-Process",
            )
        self.assertIn("System.Collections.Generic.HashSet[string]", self.install)
        self.assertIn("$otherInstancePaths", self.install)
        self.assertIn("便携/测试副本", self.install)

    def test_uninstaller_is_parsed_installed_and_has_start_menu_entry(self) -> None:
        self.assertIn("Assert-ValidPowerShellScript", self.install)
        self.assertIn(
            "Assert-ValidPowerShellScript -Path $packagedUninstaller", self.install
        )
        self.assertIn(
            "Copy-Item -LiteralPath $packagedUninstaller -Destination (Join-Path $staging 'uninstall.ps1')",
            self.install,
        )
        self.assertIn("大声发划词翻译 - 卸载.lnk", self.install)
        self.assertIn("大声发划词翻译 - 卸载.lnk", self.uninstall)

    def test_shortcut_failure_is_separate_from_main_install_result(self) -> None:
        committed = self.install.index("$committed = $true")
        shortcut_errors = self.install.index("$shortcutErrors = @()")
        self.assertLess(committed, shortcut_errors)
        self.assertIn("程序主体已安装成功", self.install)
        self.assertIn("$shortcutErrors +=", self.install)

    def test_uninstall_quarantines_before_delete_and_keeps_shortcuts_on_failure(self) -> None:
        move = self.uninstall.index(
            "Move-Item -LiteralPath $target -Destination $quarantine"
        )
        cleanup = self.uninstall.index(
            "Remove-UninstallQuarantine -Path $quarantine -ExpectedLeaf $quarantineLeaf"
        )
        first_shortcut = self.uninstall.index(
            "Remove-ShortcutIfOwned -ShortcutPath $desktopShortcut"
        )
        self.assertLess(move, cleanup)
        self.assertLess(cleanup, first_shortcut)
        self.assertIn("快捷方式仍保留。残留目录", self.uninstall)
        self.assertIn("ExpectedArgumentsContain", self.uninstall)

    def test_quarantine_refuses_unknown_content_then_deletes_managed_content(self) -> None:
        output = self.run_powershell_functions(
            self.uninstall_path,
            (
                "Assert-ExactProgramsSibling",
                "Assert-PlainDirectoryTree",
                "Assert-ManagedTopLevel",
                "Assert-ValidDeploymentMarker",
                "Read-DeploymentMarkerFile",
                "Read-DeploymentMarker",
                "Write-DeploymentMarker",
                "Remove-UninstallQuarantine",
            ),
            r"""
$productId = 'DaShengFaTranslator'
$targetLeaf = 'DaShengFaTranslator'
$markerName = '.dashengfa-install.json'
$markerTempName = '.dashengfa-install.json.tmp'
$markerBackupName = '.dashengfa-install.json.bak'
$managedTopLevelNames = @('DaShengFaTranslator.exe', '_internal', 'uninstall.ps1', $markerName, $markerTempName, $markerBackupName)
$testRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('dsf-quarantine-test-' + [guid]::NewGuid().ToString('N'))
$programsRoot = Join-Path $testRoot 'Programs'
$operationId = 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'
$leaf = ".$targetLeaf-uninstall-$operationId"
$quarantine = Join-Path $programsRoot $leaf
New-Item -ItemType Directory -Path (Join-Path $quarantine '_internal') -Force | Out-Null
try {
    Set-Content -LiteralPath (Join-Path $quarantine 'DaShengFaTranslator.exe') -Value 'placeholder'
    Set-Content -LiteralPath (Join-Path $quarantine 'uninstall.ps1') -Value 'placeholder'
    Write-DeploymentMarker -Path $quarantine -Kind Target -MarkerOperationId $operationId -SmokePassed $true
    Write-DeploymentMarker -Path $quarantine -Kind Uninstall -MarkerOperationId $operationId -SmokePassed $true
    Set-Content -LiteralPath (Join-Path $quarantine 'user-file.txt') -Value 'must survive refusal'
    $refused = $false
    try { Remove-UninstallQuarantine -Path $quarantine -ExpectedLeaf $leaf } catch { $refused = $true }
    if (-not $refused -or -not (Test-Path -LiteralPath (Join-Path $quarantine 'user-file.txt') -PathType Leaf)) {
        throw 'Unknown content was not safely refused.'
    }
    Remove-Item -LiteralPath (Join-Path $quarantine 'user-file.txt') -Force
    Remove-UninstallQuarantine -Path $quarantine -ExpectedLeaf $leaf
    if (Test-Path -LiteralPath $quarantine) { throw 'Managed quarantine was not removed.' }
    Write-Output 'QUARANTINE_BEHAVIOR_OK'
}
finally {
    if (Test-Path -LiteralPath $testRoot) { Remove-Item -LiteralPath $testRoot -Recurse -Force }
}
""",
        )
        self.assertIn("QUARANTINE_BEHAVIOR_OK", output)

    def test_orphan_cleanup_requires_an_exact_product_marker(self) -> None:
        self.assertIn("Test-RecognizedOrphan", self.install)
        self.assertIn("Assert-ValidDeploymentMarker", self.install)
        self.assertIn(
            ".Equals($productId, [System.StringComparison]::Ordinal)", self.install
        )
        self.assertIn("$marker.operationId -cne $suffix", self.install)
        self.assertIn("无法确认属于本软件的临时目录，已原样保留", self.install)
        self.assertIn(".$targetLeaf-uninstall-", self.install)

    def test_user_data_paths_are_all_preflighted_before_program_or_data_delete(self) -> None:
        preflight = self.uninstall.index(
            "$validatedDataPaths = if ($RemoveUserData)"
        )
        quarantine = self.uninstall.index(
            "Move-Item -LiteralPath $target -Destination $quarantine"
        )
        data_delete = self.uninstall.index(
            "foreach ($dataPath in $validatedDataPaths)"
        )
        self.assertLess(preflight, quarantine)
        self.assertLess(quarantine, data_delete)
        self.assertIn("Get-ValidatedUserDataPaths", self.uninstall)
        self.assertGreaterEqual(self.uninstall.count("@(Get-ValidatedUserDataPaths)"), 2)
        self.assertIn("DaShengFaTranslator', 'WordLocalTranslator", self.uninstall)

    def test_user_data_preflight_rejects_before_deleting_any_path(self) -> None:
        output = self.run_powershell_functions(
            self.uninstall_path,
            ("Get-ValidatedUserDataPaths",),
            r"""
$oldLocalAppData = $env:LOCALAPPDATA
$testRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('dsf-data-test-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $testRoot | Out-Null
try {
    $env:LOCALAPPDATA = $testRoot
    $newData = Join-Path $testRoot 'DaShengFaTranslator'
    $oldData = Join-Path $testRoot 'WordLocalTranslator'
    New-Item -ItemType Directory -Path $newData | Out-Null
    Set-Content -LiteralPath (Join-Path $newData 'keep.txt') -Value 'keep'
    Set-Content -LiteralPath $oldData -Value 'unsafe file'
    $refused = $false
    try { [void](Get-ValidatedUserDataPaths) } catch { $refused = $true }
    if (-not $refused -or
        -not (Test-Path -LiteralPath (Join-Path $newData 'keep.txt') -PathType Leaf) -or
        -not (Test-Path -LiteralPath $oldData -PathType Leaf)) {
        throw 'Preflight mutated data or accepted an unsafe path.'
    }
    Write-Output 'DATA_PREFLIGHT_OK'
}
finally {
    $env:LOCALAPPDATA = $oldLocalAppData
    if (Test-Path -LiteralPath $testRoot) { Remove-Item -LiteralPath $testRoot -Recurse -Force }
}
""",
        )
        self.assertIn("DATA_PREFLIGHT_OK", output)

    def test_disk_peak_preflight_is_present(self) -> None:
        self.assertIn("Assert-SufficientDiskSpace", self.install)
        self.assertIn("AvailableFreeSpace", self.install)
        self.assertIn("$sourceBytes + $reserveBytes", self.install)


if __name__ == "__main__":
    unittest.main()
