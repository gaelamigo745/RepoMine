param([string]$Python = "python")
$ErrorActionPreference = "Stop"
& (Join-Path $PSScriptRoot "launcher/build.ps1") -Python $Python
