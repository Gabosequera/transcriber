# Python oficial firmado, privado de esta app. /a extrae los MSI sin registrar
# una instalacion de Python ni cambiar PATH o las protecciones de Windows.
param([string]$Root = (Split-Path $PSScriptRoot -Parent))
$ErrorActionPreference = 'Stop'
$version = '3.13.16'
$pythonDir = Join-Path $Root 'shared\python'
$python = Join-Path $pythonDir 'python.exe'
$marker = Join-Path $pythonDir 'transcriptor-python-version.txt'

function Assert-PythonSignature([string]$Path) {
    $signature = Get-AuthenticodeSignature -LiteralPath $Path
    if ($signature.Status -ne 'Valid' -or
        $signature.SignerCertificate.Subject -notmatch 'CN=Python Software Foundation,') {
        throw "Firma de Python Software Foundation invalida: $Path"
    }
}

if (-not (Test-Path $marker)) {
    $layout = Join-Path $Root "shared\tools\python-$version"
    $logs = Join-Path $Root 'shared\logs'
    foreach ($directory in @($layout, $logs, $pythonDir)) {
        New-Item -ItemType Directory -Force -Path $directory | Out-Null
    }
    foreach ($package in @('core', 'exe', 'lib', 'tcltk')) {
        $msi = Join-Path $layout "$package.msi"
        if (-not (Test-Path $msi)) {
            Invoke-WebRequest "https://www.python.org/ftp/python/$version/amd64/$package.msi" `
                -UseBasicParsing -OutFile $msi
        }
        Assert-PythonSignature $msi
        $log = Join-Path $logs "python-extract-$package.log"
        $process = Start-Process -FilePath (Join-Path $env:SystemRoot 'System32\msiexec.exe') `
            -ArgumentList "/a `"$msi`" /qn TARGETDIR=`"$pythonDir`" /L*v `"$log`"" `
            -WindowStyle Hidden -Wait -PassThru
        if ($process.ExitCode -ne 0) {
            throw "No se pudo extraer Python ($package, codigo $($process.ExitCode)). Ver $log"
        }
    }
}
Assert-PythonSignature $python
Assert-PythonSignature (Join-Path $pythonDir 'pythonw.exe')
& $python -c "import sys, tkinter, venv; assert sys.version_info[:2] == (3, 13)"
if ($LASTEXITCODE -ne 0) { throw 'Python oficial incompleto o incompatible' }
Set-Content -LiteralPath $marker -Value $version -Encoding ASCII
Write-Output $python
