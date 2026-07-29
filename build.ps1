# Builds Speedster.exe with the in-box .NET Framework C# compiler (no SDK required).
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

$csc = Get-ChildItem 'C:\Windows\Microsoft.NET\Framework64\v4.0.*\csc.exe' -ErrorAction SilentlyContinue |
       Sort-Object FullName -Descending | Select-Object -First 1 -ExpandProperty FullName
if (-not $csc) {
    $csc = Get-ChildItem 'C:\Windows\Microsoft.NET\Framework\v4.0.*\csc.exe' -ErrorAction SilentlyContinue |
           Sort-Object FullName -Descending | Select-Object -First 1 -ExpandProperty FullName
}
if (-not $csc) { throw 'csc.exe (.NET Framework 4.x) not found.' }

$src = Join-Path $root 'Speedster.cs'
$tpl = Join-Path $root 'portal.html'
$out = Join-Path $root 'Speedster.exe'

Write-Host "csc: $csc"
& $csc /nologo /target:winexe /optimize+ /out:"$out" `
    /reference:System.Windows.Forms.dll `
    /reference:System.Drawing.dll `
    /resource:"$tpl",portal.html `
    "$src"

if ($LASTEXITCODE -ne 0) { throw "Build failed (exit $LASTEXITCODE)." }
Write-Host "Built: $out"
