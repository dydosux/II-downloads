param([Parameter(Mandatory=$true)][string]$Manifest, [string]$Output = 'dist\II-Setup.exe')
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
$destination = [IO.Path]::GetFullPath($Output)
New-Item -ItemType Directory -Force -Path (Split-Path $destination -Parent) | Out-Null
& $compiler /nologo /target:winexe /platform:x64 /optimize+ "/out:$destination" "/resource:$([IO.Path]::GetFullPath($Manifest)),payload.json" "/resource:$root\docs\SD15-LICENSE.txt,SD15-LICENSE.txt" "/win32manifest:$PSScriptRoot\installer.manifest" /reference:System.Windows.Forms.dll /reference:System.Drawing.dll /reference:System.Web.Extensions.dll /reference:System.IO.Compression.dll "$PSScriptRoot\Installer.cs"
if ($LASTEXITCODE -ne 0) { throw 'Installer compilation failed' }
Write-Output $destination
