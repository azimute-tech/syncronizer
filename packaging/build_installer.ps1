<#
build_installer.ps1 — assemble the bundled runtimes and compile the Inno Setup installer.

Runs on a Windows machine (locally OR on a GitHub Actions windows-latest runner — see
.github/workflows/build-installer.yml). Produces packaging\dist\syncronizer-setup.exe.

It resolves the LATEST relocatable Python (python-build-standalone) and MinGit assets
from the GitHub API so the pinned versions never rot; pass -PythonUrl / -MinGitUrl to
override with an exact pin. NSSM is pinned (its release URL is stable).

Steps:
  1. Resolve + download a relocatable CPython 3.12 (install_only) -> build\python
  2. Resolve + download MinGit (portable git)                     -> build\git
  3. Download NSSM and place nssm.exe                             -> build\nssm
  4. Snapshot the repo for an offline-install fallback           -> build\seed
  5. Compile installer.iss with ISCC                             -> dist\syncronizer-setup.exe
#>
[CmdletBinding()]
param(
    [string]$PythonUrl = "",   # override to pin; otherwise latest cpython-3.12 install_only
    [string]$MinGitUrl = "",   # override to pin; otherwise latest MinGit 64-bit
    [string]$NssmUrl   = "https://nssm.cc/release/nssm-2.24.zip",
    [string]$IsccPath  = ""
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Definition
$repoRoot = Split-Path -Parent $here
$build = Join-Path $here "build"
$dist  = Join-Path $here "dist"
New-Item -ItemType Directory -Force -Path $build, $dist | Out-Null

function Get-LatestAsset([string]$repo, [string]$pattern) {
    $headers = @{ "User-Agent" = "syncronizer-build"; "Accept" = "application/vnd.github+json" }
    if ($env:GITHUB_TOKEN) { $headers["Authorization"] = "Bearer $($env:GITHUB_TOKEN)" }
    $rel = Invoke-RestMethod -Uri "https://api.github.com/repos/$repo/releases/latest" -Headers $headers
    $asset = $rel.assets | Where-Object { $_.name -match $pattern } | Select-Object -First 1
    if (-not $asset) { throw "no asset matching /$pattern/ in $repo latest release ($($rel.tag_name))" }
    Write-Host "Resolved $repo -> $($asset.name)"
    return $asset.browser_download_url
}

function Get-File([string]$url, [string]$dest) {
    for ($i = 1; $i -le 4; $i++) {
        try {
            Write-Host "Downloading $url (tentativa $i)"
            Invoke-WebRequest -Uri $url -OutFile $dest -UseBasicParsing -TimeoutSec 180
            return
        } catch {
            Write-Warning "download falhou ($($_.Exception.Message)); retry em 5s"
            Start-Sleep -Seconds 5
        }
    }
    throw "download falhou apos varias tentativas: $url"
}

if (-not $PythonUrl) {
    $PythonUrl = Get-LatestAsset "astral-sh/python-build-standalone" `
        "cpython-3\.12\.\d+\+\d+-x86_64-pc-windows-msvc-install_only\.tar\.gz$"
}
if (-not $MinGitUrl) {
    $MinGitUrl = Get-LatestAsset "git-for-windows/git" "^MinGit-[\d.]+-64-bit\.zip$"
}

# 1) Python (relocatable, install_only) — extracts a top-level "python" directory.
$pyTar = Join-Path $build "python.tar.gz"
Get-File $PythonUrl $pyTar
if (Test-Path (Join-Path $build "python")) { Remove-Item -Recurse -Force (Join-Path $build "python") }
tar -xzf $pyTar -C $build      # bsdtar ships with Windows 10+ and the CI runner
if (-not (Test-Path (Join-Path $build "python\python.exe"))) {
    throw "Expected $build\python\python.exe after extraction — check the Python archive layout."
}

# 2) MinGit (zip with cmd\git.exe at the root).
$gitZip = Join-Path $build "mingit.zip"
Get-File $MinGitUrl $gitZip
if (Test-Path (Join-Path $build "git")) { Remove-Item -Recurse -Force (Join-Path $build "git") }
Expand-Archive -Path $gitZip -DestinationPath (Join-Path $build "git") -Force
if (-not (Test-Path (Join-Path $build "git\cmd\git.exe"))) {
    throw "Expected $build\git\cmd\git.exe after extraction."
}

# 3) NSSM — use the VENDORED binary (nssm.cc is flaky / returns 503). Fall back to
#    downloading only if the vendored copy is missing.
New-Item -ItemType Directory -Force -Path (Join-Path $build "nssm") | Out-Null
$vendorNssm = Join-Path $here "vendor\nssm.exe"
if (Test-Path $vendorNssm) {
    Write-Host "Using vendored nssm.exe ($vendorNssm)"
    Copy-Item $vendorNssm (Join-Path $build "nssm\nssm.exe") -Force
} else {
    $nssmZip = Join-Path $build "nssm.zip"
    Get-File $NssmUrl $nssmZip
    $nssmTmp = Join-Path $build "nssm_tmp"
    if (Test-Path $nssmTmp) { Remove-Item -Recurse -Force $nssmTmp }
    Expand-Archive -Path $nssmZip -DestinationPath $nssmTmp -Force
    $nssmExe = Get-ChildItem -Path $nssmTmp -Recurse -Filter nssm.exe |
        Where-Object { $_.FullName -match "win64" } | Select-Object -First 1
    Copy-Item $nssmExe.FullName (Join-Path $build "nssm\nssm.exe") -Force
}

# 4) Offline seed snapshot of the repo (excludes git metadata + local artifacts).
$seed = Join-Path $build "seed"
if (Test-Path $seed) { Remove-Item -Recurse -Force $seed }
New-Item -ItemType Directory -Force -Path $seed | Out-Null
robocopy $repoRoot $seed /E /XD .git .venv data build dist __pycache__ /XF *.pyc | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy failed ($LASTEXITCODE)" } else { $global:LASTEXITCODE = 0 }

# 5) Compile the installer.
if (-not $IsccPath) {
    $IsccPath = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
    if (-not (Test-Path $IsccPath)) {
        $cmd = Get-Command ISCC.exe -ErrorAction SilentlyContinue
        if ($cmd) { $IsccPath = $cmd.Source }
    }
}
if (-not (Test-Path $IsccPath)) {
    throw "Inno Setup compiler not found. Install Inno Setup 6 or pass -IsccPath."
}
& $IsccPath (Join-Path $here "installer.iss")
if ($LASTEXITCODE -ne 0) { throw "ISCC failed ($LASTEXITCODE)" }
Write-Host "Done. Installer at $dist\syncronizer-setup.exe"
