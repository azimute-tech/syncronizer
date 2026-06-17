<#
build_installer.ps1 — assemble the bundled runtimes and compile the Inno Setup installer.

Run on a Windows build box (PowerShell). Produces packaging\dist\syncronizer-setup.exe.
Nothing here touches the target machine; it only prepares the setup.exe.

Steps:
  1. Download a pinned, relocatable CPython (python-build-standalone, install_only) -> build\python
  2. Download MinGit (portable git)                                                 -> build\git
  3. Download NSSM and place nssm.exe                                               -> build\nssm
  4. Snapshot the repo for an offline-install fallback                              -> build\seed
  5. Compile installer.iss with ISCC                                                -> dist\syncronizer-setup.exe

Pin every URL to an exact release so all machines are byte-identical. Update the
versions below as you bump the runtime.
#>
[CmdletBinding()]
param(
    # Pin these to specific releases. Examples (verify the latest stable tags before use):
    [string]$PythonUrl = "https://github.com/astral-sh/python-build-standalone/releases/download/20240814/cpython-3.12.5+20240814-x86_64-pc-windows-msvc-install_only.tar.gz",
    [string]$MinGitUrl = "https://github.com/git-for-windows/git/releases/download/v2.46.0.windows.1/MinGit-2.46.0-64-bit.zip",
    [string]$NssmUrl   = "https://nssm.cc/release/nssm-2.24.zip",
    [string]$IsccPath  = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Definition
$repoRoot = Split-Path -Parent $here
$build = Join-Path $here "build"
$dist  = Join-Path $here "dist"

New-Item -ItemType Directory -Force -Path $build, $dist | Out-Null

function Get-File($url, $dest) {
    Write-Host "Downloading $url"
    Invoke-WebRequest -Uri $url -OutFile $dest -UseBasicParsing
}

# 1) Python (relocatable, install_only) — a .tar.gz containing a top-level python\ dir.
$pyTar = Join-Path $build "python.tar.gz"
Get-File $PythonUrl $pyTar
if (Test-Path (Join-Path $build "python")) { Remove-Item -Recurse -Force (Join-Path $build "python") }
# tar ships with Windows 10+. The archive extracts a "python" directory.
tar -xzf $pyTar -C $build
if (-not (Test-Path (Join-Path $build "python\python.exe"))) {
    throw "Expected $build\python\python.exe after extraction — check the PythonUrl archive layout."
}

# 2) MinGit (zip with cmd\git.exe).
$gitZip = Join-Path $build "mingit.zip"
Get-File $MinGitUrl $gitZip
if (Test-Path (Join-Path $build "git")) { Remove-Item -Recurse -Force (Join-Path $build "git") }
Expand-Archive -Path $gitZip -DestinationPath (Join-Path $build "git") -Force
if (-not (Test-Path (Join-Path $build "git\cmd\git.exe"))) {
    throw "Expected $build\git\cmd\git.exe after extraction."
}

# 3) NSSM (zip with win64\nssm.exe).
$nssmZip = Join-Path $build "nssm.zip"
Get-File $NssmUrl $nssmZip
$nssmTmp = Join-Path $build "nssm_tmp"
if (Test-Path $nssmTmp) { Remove-Item -Recurse -Force $nssmTmp }
Expand-Archive -Path $nssmZip -DestinationPath $nssmTmp -Force
$nssmExe = Get-ChildItem -Path $nssmTmp -Recurse -Filter nssm.exe |
    Where-Object { $_.FullName -match "win64" } | Select-Object -First 1
New-Item -ItemType Directory -Force -Path (Join-Path $build "nssm") | Out-Null
Copy-Item $nssmExe.FullName (Join-Path $build "nssm\nssm.exe") -Force

# 4) Offline seed snapshot of the repo (excludes git metadata and local artifacts).
$seed = Join-Path $build "seed"
if (Test-Path $seed) { Remove-Item -Recurse -Force $seed }
New-Item -ItemType Directory -Force -Path $seed | Out-Null
robocopy $repoRoot $seed /E /XD .git .venv data packaging\build packaging\dist __pycache__ /XF *.pyc | Out-Null

# 5) Compile the installer.
if (-not (Test-Path $IsccPath)) {
    throw "Inno Setup compiler not found at $IsccPath. Install Inno Setup 6 or pass -IsccPath."
}
& $IsccPath (Join-Path $here "installer.iss")
Write-Host "Done. Installer at $dist\syncronizer-setup.exe"
