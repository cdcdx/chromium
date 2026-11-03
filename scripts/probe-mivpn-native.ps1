param(
    [Parameter(Mandatory = $true)][string]$PayloadRoot,
    [Parameter(Mandatory = $true)][string]$ProbeRoot,
    [Parameter(Mandatory = $true)][string]$PluginZip,
    [int]$TimeoutSeconds = 180,
    [switch]$VerifyDefaultSandbox,
    [switch]$VerifyConnections,
    [string]$EgressUrl = ''
)

$ErrorActionPreference = 'Stop'
$egressEndpoint = $null
if ($VerifyConnections) {
    if (-not [Uri]::TryCreate($EgressUrl, [UriKind]::Absolute, [ref]$egressEndpoint) -or
        $egressEndpoint.Scheme -ne 'https' -or $egressEndpoint.UserInfo -or $egressEndpoint.Fragment -or
        $egressEndpoint.IsLoopback) {
        throw 'Connection verification requires an explicit HTTPS JSON {ip} endpoint without credentials, fragments or loopback.'
    }
    if (-not $PSBoundParameters.ContainsKey('TimeoutSeconds')) { $TimeoutSeconds = 420 }
}
$payload = (Resolve-Path -LiteralPath $PayloadRoot).Path
$archive = (Resolve-Path -LiteralPath $PluginZip).Path
$probe = [IO.Path]::GetFullPath($ProbeRoot)
$executable = Join-Path $payload 'NomadBrowser.exe'
if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) { throw 'Native NomadBrowser.exe is required.' }
. (Join-Path $PSScriptRoot 'mivpn_probe_guard.ps1')
Assert-MivpnProbePayload -PayloadRoot $payload
if (Test-Path -LiteralPath $probe) { throw 'ProbeRoot must be a new disposable directory.' }
$normalProfile = Join-Path $env:LOCALAPPDATA 'NomadBrowser.Avalonia'
if ($probe.StartsWith($normalProfile, [StringComparison]::OrdinalIgnoreCase)) { throw 'The normal user profile cannot be used.' }
New-Item -ItemType Directory -Path $probe | Out-Null
Set-Content -LiteralPath (Join-Path $probe '.nomad-mivpn-probe') -Value 'Disposable MiVPN native launch probe.'
$extensionRoot = Join-Path $probe 'extensions'
$installed = Join-Path $extensionRoot 'objojdkhlcekcnhaeoebojmffolmafll'
Expand-Archive -LiteralPath $archive -DestinationPath $installed
$output = Join-Path $probe 'checks.json'
$environmentValues = @{
    NOMAD_MIVPN_TEST = '1'
    NOMAD_PLUGIN_STORAGE_TEST = 'mivpn'
    NOMAD_PROFILE_DIR = $probe
    NOMAD_EXTENSION_ROOT = $extensionRoot
    NOMAD_PLUGIN_ROOT = (Join-Path $probe 'plugins')
    NOMAD_MIVPN_CONNECT_TEST = $(if ($VerifyConnections) { '1' } else { $null })
    NOMAD_MIVPN_EGRESS_URL = $(if ($VerifyConnections) { $egressEndpoint.AbsoluteUri } else { $null })
    ARUPA_DESKTOP_DIR = (Join-Path $payload 'arupa-desktop')
    ARUPA_UNSAFE_DISABLE_SANDBOX = $(if ($VerifyDefaultSandbox) { $null } else { '0' })
}
$previous = @{}
$process = $null
try {
    foreach ($key in $environmentValues.Keys) {
        $previous[$key] = [Environment]::GetEnvironmentVariable($key, 'Process')
        [Environment]::SetEnvironmentVariable($key, $environmentValues[$key], 'Process')
    }
    $process = Start-Process -FilePath $executable -WorkingDirectory $payload -WindowStyle Hidden -PassThru `
        -ArgumentList @('--nativeextensiontest', ('"' + $output + '"')) `
        -RedirectStandardOutput (Join-Path $probe 'stdout.log') -RedirectStandardError (Join-Path $probe 'stderr.log')
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while (-not $process.WaitForExit(1000)) {
        if ([DateTime]::UtcNow -gt $deadline) { throw 'MiVPN native launch probe timed out.' }
    }
    # PowerShell 5 needs the parameterless wait to refresh redirected streams
    # and ExitCode after the timed WaitForExit overload reports completion.
    $process.WaitForExit()
    if (-not (Test-Path -LiteralPath $output -PathType Leaf)) { throw ('Probe did not produce checks.json; process exit=' + $process.ExitCode) }
    # Windows PowerShell 5 wraps a top-level JSON array as one object inside @().
    # Send it through the pipeline once so every check is counted independently.
    $parsedChecks = Get-Content -LiteralPath $output -Raw | ConvertFrom-Json
    $checks = @($parsedChecks | ForEach-Object { $_ })
    $checks | Format-Table name, pass, detail -Wrap
    $apiChecksPassed = $checks.Count -ge 5 -and @($checks | Where-Object { -not $_.pass }).Count -eq 0
    $productReport = $null
    $productReportPath = $output + '.mivpn.json'
    if (Test-Path -LiteralPath $productReportPath -PathType Leaf) {
        try { $productReport = Get-Content -LiteralPath $productReportPath -Raw | ConvertFrom-Json }
        catch { $productReport = $null }
    }
    $connectionsVerified = $false
    if ($VerifyConnections) { $connectionsVerified = Test-MivpnConnectionEvidence -Report $productReport }
    $nativeLogs = @(Get-ChildItem -LiteralPath (Join-Path $probe 'userdata/diagnostics') -Filter 'native-r6-*.log' -ErrorAction SilentlyContinue)
    $logText = ($nativeLogs | ForEach-Object { Get-Content -LiteralPath $_.FullName -Raw }) -join "`n"
    $sandbox = $logText -match 'stage=admission.protection phase=end reason=app_container_win32k'
    $mappedProcess = $logText -match 'stage=browser.process_mapping phase=end reason=browser_generation_to_child_pid'
    $apiV2 = $logText -match 'stage=utility.get_version phase=end reason=adapter_available [^\r\n]* code=2(?: |$)'
    $evidence = [ordered]@{
        exitCode = $process.ExitCode
        apiChecksPassed = $apiChecksPassed
        appContainerAndWin32kConfirmed = $sandbox
        childProcessMapped = $mappedProcess
        sdkApiVersion = $(if ($apiV2) { 2 } else { $null })
        sandboxConfiguration = $(if ($VerifyDefaultSandbox) { 'main_image_default' } else { 'explicit_enabled' })
        profileMode = 'marked_regular_profile'
        connectionsRequested = [bool]$VerifyConnections
        connectionsVerified = $(if ($VerifyConnections) { $connectionsVerified } else { $null })
    }
    if ($VerifyConnections -and $null -ne $productReport) {
        $evidence.connectionResults = @($productReport.connectionResults | Select-Object protocol, stage, passed, egressMatched, code)
    }
    $evidence | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $probe 'sandbox-evidence.json')
    if (-not $apiChecksPassed) { throw 'MiVPN API or requested connection checks failed; inspect the probe evidence.' }
    if (-not $sandbox -or -not $mappedProcess -or -not $apiV2) { throw 'MiVPN product API passed but native sandbox/process/API v2 evidence is missing.' }
    if ($VerifyConnections -and -not $connectionsVerified) { throw 'MiVPN connection mode requires both protocol/egress checks and confirmed disconnect.' }
    Write-Output ('MiVPN native SDK/sandbox probe passed: ' + $probe)
}
finally {
    if ($null -ne $process) {
        # Kill only this owned process object. Its sandbox jobs close with the
        # broker; avoid PID searches and the .NET-only Kill(entireProcessTree).
        if (-not $process.HasExited) { $process.Kill(); $process.WaitForExit(5000) | Out-Null }
        $process.Dispose()
    }
    foreach ($key in $previous.Keys) { [Environment]::SetEnvironmentVariable($key, $previous[$key], 'Process') }
}
