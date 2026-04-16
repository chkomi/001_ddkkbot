[CmdletBinding()]
param(
    [ValidateSet("", "nat", "mirrored", "virtioproxy")]
    [string]$NetworkingMode = ""
)

$ErrorActionPreference = "Stop"

function Find-SectionStart {
    param(
        [System.Collections.Generic.List[string]]$Lines,
        [string]$SectionName
    )
    $target = "^\s*\[" + [Regex]::Escape($SectionName) + "\]\s*$"
    for ($i = 0; $i -lt $Lines.Count; $i++) {
        if ($Lines[$i] -imatch $target) {
            return $i
        }
    }
    return -1
}

function Find-SectionEnd {
    param(
        [System.Collections.Generic.List[string]]$Lines,
        [int]$SectionStart
    )
    for ($i = $SectionStart + 1; $i -lt $Lines.Count; $i++) {
        if ($Lines[$i] -match "^\s*\[[^\]]+\]\s*$") {
            return $i
        }
    }
    return $Lines.Count
}

function Upsert-Key {
    param(
        [System.Collections.Generic.List[string]]$Lines,
        [int]$SectionStart,
        [int]$SectionEnd,
        [string]$Key,
        [string]$Value,
        [ref]$Changed
    )

    $target = "$Key=$Value"
    $keyPattern = "^\s*" + [Regex]::Escape($Key) + "\s*="
    for ($i = $SectionStart + 1; $i -lt $SectionEnd; $i++) {
        if ($Lines[$i] -match "^\s*[#;]") {
            continue
        }
        if ($Lines[$i] -imatch $keyPattern) {
            if ($Lines[$i].Trim() -ne $target) {
                $Lines[$i] = $target
                $Changed.Value = $true
            }
            return $SectionEnd
        }
    }

    $Lines.Insert($SectionEnd, $target)
    $Changed.Value = $true
    return ($SectionEnd + 1)
}

$wslConfigPath = Join-Path $env:USERPROFILE ".wslconfig"
$lines = New-Object "System.Collections.Generic.List[string]"
if (Test-Path -LiteralPath $wslConfigPath) {
    foreach ($line in (Get-Content -LiteralPath $wslConfigPath)) {
        [void]$lines.Add($line)
    }
}

$changed = $false
$sectionStart = Find-SectionStart -Lines $lines -SectionName "wsl2"
if ($sectionStart -lt 0) {
    if ($lines.Count -gt 0 -and $lines[$lines.Count - 1].Trim() -ne "") {
        [void]$lines.Add("")
    }
    [void]$lines.Add("[wsl2]")
    $sectionStart = $lines.Count - 1
    $changed = $true
}

$sectionEnd = Find-SectionEnd -Lines $lines -SectionStart $sectionStart

# NOTE: dnsTunneling and autoProxy are disabled due to compatibility issues with WSL
# Only configure networkingMode if explicitly requested
if ($NetworkingMode) {
    $sectionEnd = Upsert-Key -Lines $lines -SectionStart $sectionStart -SectionEnd $sectionEnd -Key "networkingMode" -Value $NetworkingMode -Changed ([ref]$changed)
    $sectionEnd = Upsert-Key -Lines $lines -SectionStart $sectionStart -SectionEnd $sectionEnd -Key "localhostForwarding" -Value "true" -Changed ([ref]$changed)
}

if ($changed) {
    if (Test-Path -LiteralPath $wslConfigPath) {
        $backupPath = "$wslConfigPath.bak-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
        Copy-Item -LiteralPath $wslConfigPath -Destination $backupPath -Force
        Write-Output "BACKUP_PATH=$backupPath"
    }
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllLines($wslConfigPath, $lines, $encoding)
}

Write-Output "WSLCONFIG_PATH=$wslConfigPath"
Write-Output ("CHANGED=" + $(if ($changed) { "1" } else { "0" }))
if ($NetworkingMode) {
    Write-Output "NETWORKING_MODE=$NetworkingMode"
    Write-Output "LOCALHOST_FORWARDING=true"
}
