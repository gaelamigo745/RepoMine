param(
    [string]$Python = "python"
)
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
$RepoRoot = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = "$PSScriptRoot;$RepoRoot"

function Invoke-Checked {
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Program terminó con código $LASTEXITCODE" }
}

Invoke-Checked $Python @("-c", "import sys; assert sys.version_info >= (3, 11), 'Se necesita Python 3.11 o posterior'")
if (-not (Test-Path -LiteralPath ".venv-build/Scripts/python.exe")) {
    Invoke-Checked $Python @("-m", "venv", ".venv-build")
}
$BuildPython = Join-Path $PSScriptRoot ".venv-build/Scripts/python.exe"
Invoke-Checked $BuildPython @("-m", "pip", "install", "-r", "requirements-build.txt")
Invoke-Checked $BuildPython @("-m", "unittest", "discover", "-s", "tests", "-v")
Invoke-Checked $BuildPython @("-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", (Join-Path $RepoRoot "dist"), "--workpath", (Join-Path $RepoRoot "build"), "InstaladorModsMinecraft.spec")
$Executable = Join-Path $RepoRoot "dist/InstaladorModsMinecraft.exe"
$Digest = (Get-FileHash -LiteralPath $Executable -Algorithm SHA256).Hash.ToLowerInvariant()
[System.IO.File]::WriteAllText((Join-Path $RepoRoot "dist/SHA256SUMS.txt"), "$Digest  InstaladorModsMinecraft.exe`n", [System.Text.UTF8Encoding]::new($false))
Write-Host "Listo: dist/InstaladorModsMinecraft.exe y dist/SHA256SUMS.txt"
