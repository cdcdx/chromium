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
        Add-Type -AssemblyName System.Reflection.Metadata -ErrorAction Stop
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
