function Assert-MivpnProbePackage {
    param(
        [Parameter(Mandatory = $true)][string]$PluginZip,
        [Parameter(Mandatory = $true)][string]$PublicKeyFile,
        [string]$Python = 'python',
        [string]$OpenSsl = ''
    )
    $pluginProject = if ($env:MIVPN_PLUGIN_ROOT) { $env:MIVPN_PLUGIN_ROOT } else {
        Join-Path (Split-Path $PSScriptRoot -Parent) 'mivpn-plugin'
    }
    $verifier = Join-Path $pluginProject 'tools\signing\verify_mivpn_package.py'
    if (-not (Test-Path -LiteralPath $verifier -PathType Leaf)) {
        throw 'MiVPN signing tools moved to the independent mivpn-plugin project. Prepare that project or set MIVPN_PLUGIN_ROOT.'
    }
    $publicKey = (Resolve-Path -LiteralPath $PublicKeyFile).Path
    $arguments = @($verifier, $PluginZip, '--public-key-file', $publicKey)
    if ($OpenSsl) { $arguments += @('--openssl', $OpenSsl) }
    $metadata = & $Python @arguments
    if ($LASTEXITCODE -ne 0) { throw 'MiVPN probe refuses an unsigned, untrusted or modified package.' }
    $verified = ($metadata -join "`n") | ConvertFrom-Json
    $authorizedIds = @('objojdkhlcekcnhaeoebojmffolmafll', 'hcjdpccaikglmijenhghaddadbmmhgea')
    if ($authorizedIds -notcontains $verified.extensionId -or
        $verified.packageDigest -notmatch '^[0-9a-f]{64}$' -or
        $verified.keyId -notmatch '^[0-9a-f]{32}$') {
        throw 'MiVPN package verifier did not return authenticated package metadata.'
    }
    # This preflight permits the expanded package shape. Actual loading still
    # requires the main-image kernel's compiled trust roots and immutable copy.
    return $verified
}

function Assert-MivpnProbePayload {
    param([Parameter(Mandatory = $true)][string]$PayloadRoot)

    $manifestPath = Join-Path $PayloadRoot 'build-manifest.json'
    $assemblyPath = Join-Path $PayloadRoot 'NomadBrowser.dll'
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf) -or
        -not (Test-Path -LiteralPath $assemblyPath -PathType Leaf)) {
        throw 'MiVPN probe requires a sealed Debug delivery with build-manifest.json.'
    }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    if ($manifest.identity.project -ne 'nomad_desktop' -or
        $manifest.identity.os -ne 'win' -or $manifest.identity.variant -ne 'debug') {
        throw 'MiVPN probe refuses non-Debug deliveries before launching the browser.'
    }
    $entry = $manifest.files.PSObject.Properties['NomadBrowser.dll']
    if (-not $entry -or $entry.Value -notmatch '^[0-9a-fA-F]{64}$' -or
        (Get-FileHash -LiteralPath $assemblyPath -Algorithm SHA256).Hash -ne $entry.Value) {
        throw 'MiVPN probe managed assembly does not match the sealed delivery.'
    }

    # Inspect metadata only. Loading the target assembly could execute code or
    # resolve dependencies before its isolated-profile guard has run. A Debug
    # label alone is insufficient: older Debug clients lack this safe harness.
    $stream = $null
    $pe = $null
    $found = $false
    try {
        try {
            Add-Type -AssemblyName System.Reflection.Metadata -ErrorAction Stop
        }
        catch {
            # Windows PowerShell 5 does not ship System.Reflection.Metadata.
            # Fall back to the Windows SDK metadata disassembler; it inspects
            # the sealed assembly without loading or executing target code.
            $windowsSdk = Join-Path ${env:ProgramFiles(x86)} 'Microsoft SDKs\Windows'
            $ildasm = Get-ChildItem -LiteralPath $windowsSdk -Filter ildasm.exe -File -Recurse -ErrorAction Stop |
                Sort-Object @{ Expression = { $_.FullName -match '[\\/]x64[\\/]' }; Descending = $true },
                    @{ Expression = 'FullName'; Descending = $true } |
                Select-Object -First 1
            if (-not $ildasm) { throw }
            $metadataText = @(& $ildasm.FullName /text /nobar $assemblyPath 2>$null)
            if ($LASTEXITCODE -ne 0) { throw }
            $found = [bool]($metadataText -match '^\.class .*NomadBrowser\.Avalonia\.NativeMivpnProbeProfile$') -and
                [bool]($metadataText -match '^\s+Prepare\(string\[\] args\) cil managed$')
        }
        if (-not $found) {
            $stream = [IO.File]::OpenRead($assemblyPath)
            $pe = [System.Reflection.PortableExecutable.PEReader]::new($stream)
            $metadata = [System.Reflection.Metadata.PEReaderExtensions]::GetMetadataReader($pe)
            foreach ($handle in $metadata.TypeDefinitions) {
                $type = $metadata.GetTypeDefinition($handle)
                if ($metadata.GetString($type.Namespace) -ne 'NomadBrowser.Avalonia' -or
                    $metadata.GetString($type.Name) -ne 'NativeMivpnProbeProfile') { continue }
                foreach ($methodHandle in $type.GetMethods()) {
                    if ($metadata.GetString($metadata.GetMethodDefinition($methodHandle).Name) -eq 'Prepare') {
                        $found = $true
                        break
                    }
                }
                break
            }
        }
    }
    catch {
        throw 'Cannot inspect the isolated MiVPN probe metadata; use PowerShell 7 and a matching Debug delivery.'
    }
    finally {
        if ($null -ne $pe) { $pe.Dispose() }
        if ($null -ne $stream) { $stream.Dispose() }
    }
    if (-not $found) { throw 'MiVPN probe refuses a client without the validated isolated-profile harness.' }
}

function Test-MivpnConnectionEvidence {
    param($Report)
    if ($null -eq $Report -or $Report.connectionsVerified -isnot [bool] -or -not $Report.connectionsVerified -or
        $Report.connectionResults -isnot [array] -or $Report.connectionResults.Count -lt 5) { return $false }
    foreach ($row in $Report.connectionResults) {
        if ($null -eq $row -or $row.passed -isnot [bool] -or -not $row.passed) { return $false }
    }
    $required = @(
        @('reality_tcp', 'connect', $false), @('reality_tcp', 'egress', $true),
        @('hysteria2', 'switch', $false), @('hysteria2', 'egress', $true),
        @('none', 'disconnect', $false)
    )
    foreach ($need in $required) {
        $matches = @($Report.connectionResults | Where-Object {
            $_.protocol -eq $need[0] -and $_.stage -eq $need[1] -and
            (-not $need[2] -or ($_.egressMatched -is [bool] -and $_.egressMatched))
        })
        if ($matches.Count -eq 0) { return $false }
    }
    return $true
}
