# Instalador limpio para Windows. Codigo, runtime y datos pesados quedan separados.
# No requiere permisos de administrador y puede ejecutarse de nuevo de forma segura.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$ProgressPreference = "SilentlyContinue"

function Assert-Ok($Message) {
    if ($LASTEXITCODE -ne 0) { throw "$Message (codigo $LASTEXITCODE)" }
}

function Assert-GitHubDigest($Path, $Asset) {
    $digest = [string]$Asset.digest
    if (-not $digest.StartsWith("sha256:")) { throw "GitHub no publico SHA-256 para $($Asset.name)" }
    $actual = (Get-FileHash -Algorithm SHA256 -Path $Path).Hash.ToLowerInvariant()
    if ($actual -ne $digest.Substring(7).ToLowerInvariant()) {
        throw "SHA-256 incorrecto para $($Asset.name)"
    }
}

function Fetch-Repo($Owner, $Name, $Commit, $Sha256, $Marker) {
    if (Test-Path $Marker) {
        Write-Host "   - $Name ya esta." -ForegroundColor Green
        return
    }
    $components = Join-Path $PSScriptRoot "shared\components"
    $zip = Join-Path $env:TEMP "transcriptor-$Name-$Commit.zip"
    $expanded = Join-Path $env:TEMP "transcriptor-$Name-$Commit"
    try {
        if (Test-Path $expanded) { Remove-Item $expanded -Recurse -Force }
        Invoke-WebRequest "https://github.com/$Owner/$Name/archive/$Commit.zip" -OutFile $zip
        $actual = (Get-FileHash -Algorithm SHA256 -Path $zip).Hash.ToLowerInvariant()
        if ($actual -ne $Sha256.ToLowerInvariant()) { throw "SHA-256 incorrecto para $Name" }
        Expand-Archive -Path $zip -DestinationPath $expanded -Force
        $source = Get-ChildItem $expanded -Directory | Select-Object -First 1
        $target = Join-Path $components $Name
        if (Test-Path $target) { Remove-Item $target -Recurse -Force }
        Move-Item $source.FullName $target
        Write-Host "   - $Name descargado y verificado." -ForegroundColor Green
    } catch {
        Write-Host "   [aviso] No se pudo preparar $Name: $($_.Exception.Message)" -ForegroundColor DarkYellow
    } finally {
        if (Test-Path $zip) { Remove-Item $zip -Force }
        if (Test-Path $expanded) { Remove-Item $expanded -Recurse -Force }
    }
}

Write-Host ""
Write-Host "=== Instalador Transcriptor para Windows ===" -ForegroundColor Cyan
Write-Host "    Releases, runtimes y modelos persistentes" -ForegroundColor Gray
Write-Host ""

$shared = Join-Path $PSScriptRoot "shared"
$tools = Join-Path $shared "tools"
$components = Join-Path $shared "components"
foreach ($directory in @($shared, $tools, $components, (Join-Path $shared "models"),
                          (Join-Path $shared "cache"), (Join-Path $shared "config"),
                          (Join-Path $shared "logs"), (Join-Path $shared "llama"),
                          (Join-Path $shared "marcas_store"))) {
    New-Item -ItemType Directory -Force -Path $directory | Out-Null
}

# 1. uv estable, fuera de todas las releases.
$uv = Join-Path $tools "uv.exe"
if (-not (Test-Path $uv)) {
    Write-Host "[1/7] Descargando uv..." -ForegroundColor Yellow
    $uvZip = Join-Path $env:TEMP "transcriptor-uv.zip"
    $uvTmp = Join-Path $env:TEMP "transcriptor-uv"
    if (Test-Path $uvTmp) { Remove-Item $uvTmp -Recurse -Force }
    $uvRelease = Invoke-RestMethod "https://api.github.com/repos/astral-sh/uv/releases/latest" `
        -Headers @{ "User-Agent" = "transcriptor-setup" }
    $uvAsset = $uvRelease.assets | Where-Object { $_.name -eq "uv-x86_64-pc-windows-msvc.zip" } | Select-Object -First 1
    if (-not $uvAsset) { throw "La release de uv no contiene el asset de Windows esperado" }
    Invoke-WebRequest $uvAsset.browser_download_url -OutFile $uvZip
    Assert-GitHubDigest $uvZip $uvAsset
    Expand-Archive -Path $uvZip -DestinationPath $uvTmp -Force
    $uvFound = Get-ChildItem $uvTmp -Recurse -Filter "uv.exe" | Select-Object -First 1
    if (-not $uvFound) { throw "El paquete de uv no contiene uv.exe" }
    Copy-Item $uvFound.FullName $uv -Force
    Remove-Item $uvZip -Force
    Remove-Item $uvTmp -Recurse -Force
} else {
    Write-Host "[1/7] uv ya esta." -ForegroundColor Green
}

# 2. Python administrado por uv y entorno bootstrap sin dependencias pesadas.
Write-Host "[2/7] Preparando Python 3.13 y bootstrap..." -ForegroundColor Yellow
& $uv python install 3.13
Assert-Ok "No se pudo instalar Python 3.13"
$bootstrap = Join-Path $PSScriptRoot ".bootstrap"
$bootstrapPython = Join-Path $bootstrap "Scripts\python.exe"
if (-not (Test-Path $bootstrapPython)) {
    & $uv venv --python 3.13 $bootstrap
    Assert-Ok "No se pudo crear el entorno bootstrap"
}
& $bootstrapPython -c "import tkinter; tkinter.Tk().destroy(); print('tkinter OK')"
Assert-Ok "El Python bootstrap no incluye Tkinter"

# 3. Construir el bundle local con la misma herramienta usada por GitHub Actions.
Write-Host "[3/7] Construyendo la release instalada..." -ForegroundColor Yellow
$version = (Get-Content (Join-Path $PSScriptRoot "VERSION") -Raw).Trim()
$bundleDir = Join-Path $PSScriptRoot "updates\bootstrap"
New-Item -ItemType Directory -Force -Path $bundleDir | Out-Null
& $bootstrapPython (Join-Path $PSScriptRoot "tools\build_release.py") --version $version --output $bundleDir
Assert-Ok "No se pudo construir la release local"
$archive = Join-Path $bundleDir "transcriptor-windows-v$version.zip"
$manifest = Join-Path $bundleDir "transcriptor-update-v$version.json"

# 4. Instalar la release y su runtime por hash. Si ya existe, se reutiliza completo.
Write-Host "[4/7] Instalando release y runtime verificado..." -ForegroundColor Yellow
& $bootstrapPython (Join-Path $PSScriptRoot "updater.py") install-bundle `
    --root $PSScriptRoot --manifest $manifest --archive $archive
Assert-Ok "No se pudo instalar la release"

# 5. Componentes auxiliares persistentes.
Write-Host "[5/7] Preparando modelos auxiliares..." -ForegroundColor Yellow
Fetch-Repo "ydqmkkx" "Respiro-en" "70e01c60c2f582c41092730680f2894ab24d6467" `
    "6dccbdb32feb8b8fa2b07d71904eedbe7267dbec951c958ef79df066a2658116" `
    (Join-Path $components "Respiro-en\modules.py")
Fetch-Repo "omine-me" "LaughterSegmentation" "a525292d26f744e14624e3a2f1fb5e3c7858d7b3" `
    "7edd1485499968e16d0603763309a7f63d2b6d2e4734c44a8cd4c6ce2be3f248" `
    (Join-Path $components "LaughterSegmentation\train\model.py")
$respiro = Join-Path $components "Respiro-en\respiro-en.pt"
if ((Test-Path $respiro) -and ((Get-Item $respiro).Length -lt 1MB)) {
    Write-Host "   [aviso] respiro-en.pt parece un puntero Git LFS; respiraciones quedaran deshabilitadas." -ForegroundColor DarkYellow
}

# 6. ffmpeg persistente.
Write-Host "[6/7] Preparando ffmpeg..." -ForegroundColor Yellow
$ffdir = Join-Path $tools "ffmpeg"
if (-not (Test-Path (Join-Path $ffdir "ffmpeg.exe"))) {
    $ffz = Join-Path $env:TEMP "transcriptor-ffmpeg.zip"
    $fftmp = Join-Path $env:TEMP "transcriptor-ffmpeg"
    if (Test-Path $fftmp) { Remove-Item $fftmp -Recurse -Force }
    $ffRelease = Invoke-RestMethod "https://api.github.com/repos/BtbN/FFmpeg-Builds/releases/latest" `
        -Headers @{ "User-Agent" = "transcriptor-setup" }
    $ffAsset = $ffRelease.assets | Where-Object { $_.name -eq "ffmpeg-master-latest-win64-gpl.zip" } | Select-Object -First 1
    if (-not $ffAsset) { throw "La release de ffmpeg no contiene el asset esperado" }
    Invoke-WebRequest $ffAsset.browser_download_url -OutFile $ffz
    Assert-GitHubDigest $ffz $ffAsset
    Expand-Archive -Path $ffz -DestinationPath $fftmp -Force
    $ffexe = Get-ChildItem $fftmp -Recurse -Filter "ffmpeg.exe" | Select-Object -First 1
    if (-not $ffexe) { throw "El paquete de ffmpeg no contiene ffmpeg.exe" }
    New-Item -ItemType Directory -Force -Path $ffdir | Out-Null
    Get-ChildItem $ffexe.Directory.FullName | Copy-Item -Destination $ffdir -Force
    Remove-Item $ffz -Force
    Remove-Item $fftmp -Recurse -Force
}

# 7. llama.cpp Vulkan persistente (best effort: no bloquea el resto de la app).
Write-Host "[7/7] Preparando llama.cpp Vulkan..." -ForegroundColor Yellow
$llamaDir = Join-Path $shared "llama"
if (-not (Test-Path (Join-Path $llamaDir "llama-server.exe"))) {
    try {
        $release = Invoke-RestMethod "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest" `
            -Headers @{ "User-Agent" = "transcriptor-setup" }
        $asset = $release.assets | Where-Object { $_.name -match "win.*vulkan.*x64" } | Select-Object -First 1
        if ($asset) {
            $llamaZip = Join-Path $env:TEMP "transcriptor-llama.zip"
            Invoke-WebRequest $asset.browser_download_url -OutFile $llamaZip
            Assert-GitHubDigest $llamaZip $asset
            Expand-Archive -Path $llamaZip -DestinationPath $llamaDir -Force
            Remove-Item $llamaZip -Force
            if (-not (Test-Path (Join-Path $llamaDir "llama-server.exe"))) {
                $found = Get-ChildItem $llamaDir -Recurse -Filter "llama-server.exe" | Select-Object -First 1
                if ($found) { Get-ChildItem $found.Directory.FullName | Move-Item -Destination $llamaDir -Force }
            }
        }
    } catch {
        Write-Host "   [aviso] llama.cpp no se pudo descargar; el resto de la app funciona." -ForegroundColor DarkYellow
    }
}

Write-Host ""
Write-Host "=== Instalacion completa: Transcriptor $version ===" -ForegroundColor Cyan
Write-Host "Doble clic en run.bat. Los modelos futuros quedan en shared y sobreviven actualizaciones." -ForegroundColor Green
Write-Host ""
Read-Host "Enter para cerrar"
