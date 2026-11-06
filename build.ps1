# Build a standalone Windows version in dist\MorningstarReader (+ a zip for releases).
# Usage:  powershell -ExecutionPolicy Bypass -File build.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path .venv)) { python -m venv .venv }
$py = ".\.venv\Scripts\python.exe"
& $py -m pip install --quiet --upgrade pip
& $py -m pip install --quiet -r requirements-dev.txt

if (-not (Test-Path assets\icon.ico)) { & $py tools\make_icons.py }

$version = (& $py -c "import morningstar; print(morningstar.__version__)").Trim()

& $py -m PyInstaller --noconfirm --clean --windowed `
    --name MorningstarReader `
    --icon assets\icon.ico `
    --add-data "assets\icon.ico;assets" `
    --add-data "assets\icon.png;assets" `
    --exclude-module tkinter `
    --exclude-module numpy `
    --exclude-module PIL `
    --exclude-module PySide6.QtQml `
    --exclude-module PySide6.QtQuick `
    --exclude-module PySide6.QtPdf `
    main.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

# Drop Qt parts a widgets-only app never loads (keeps the download small).
$internal = "dist\MorningstarReader\_internal\PySide6"
$junk = @("opengl32sw.dll", "Qt6Quick.dll", "Qt6Qml.dll", "Qt6QmlModels.dll", "Qt6QmlMeta.dll",
          "Qt6QmlWorkerScript.dll", "Qt6Pdf.dll", "Qt6VirtualKeyboard.dll", "Qt6OpenGL.dll",
          "plugins\imageformats\qpdf.dll", "plugins\platforminputcontexts")
foreach ($j in $junk) {
    $p = Join-Path $internal $j
    if (Test-Path $p) { Remove-Item $p -Recurse -Force }
}
Get-ChildItem "$internal\translations" -Filter *.qm -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -notmatch "_(fr|en)\.qm$" } | Remove-Item -Force

$zip = "dist\MorningstarReader-$version-windows-x64.zip"
& $py -c "import shutil,sys; shutil.make_archive(sys.argv[1][:-4], 'zip', 'dist', 'MorningstarReader')" $zip
Write-Host "Built $zip"
