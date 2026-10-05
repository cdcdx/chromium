cd d:/workplace/arupa_kernel; 
$t=(Resolve-Path .\src\out).Path; Write-Output "target = $t"; 
if (Test-Path .\src_out) { 
    Write-Output "existed: SymbolicLink:"; 
    Get-Item .\src_out | Format-List Name,LinkType,Target 
} else { 
    try { 
        New-Item -ItemType SymbolicLink -Path .\src_out -Target $t -ErrorAction Stop | Out-Null; 
        Write-Output "created: SymbolicLink" 
    } catch { 
        Write-Output ("symlink failed: " + $_.Exception.Message); 
        New-Item -ItemType Junction -Path .\src_out -Target $t | Out-Null; Write-Output "created: Junction (兜底)" 
    } 
    Get-Item .\src_out | Format-List Name,LinkType,Target 
}; 
