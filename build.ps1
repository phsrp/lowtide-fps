# Builds Lowtide: fetches dependencies, compiles the sensor helper, makes the exe and the installer.
# Usage: powershell -ExecutionPolicy Bypass -File build.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$LhmVersion = "0.9.6"
$PresentMonVersion = "2.6.0"
$PawnIOVersion = "2.2.0"
$Version = (Select-String -Path lowtide.py -Pattern '^VERSION = "(.+)"').Matches[0].Groups[1].Value
Write-Host "Building Lowtide $Version"

# 1. LibreHardwareMonitor (.NET Framework build) -> lib\
if (-not (Test-Path lib\LibreHardwareMonitorLib.dll)) {
    Write-Host "Downloading LibreHardwareMonitor $LhmVersion"
    $tmp = Join-Path $env:TEMP "lowtide-lhm"
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
    New-Item -ItemType Directory $tmp | Out-Null
    Invoke-WebRequest "https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases/download/v$LhmVersion/LibreHardwareMonitor.zip" -OutFile "$tmp\lhm.zip"
    Expand-Archive "$tmp\lhm.zip" "$tmp\x"
    Get-ChildItem "$tmp\x\*.dll" | Where-Object { $_.Name -notmatch '^(Aga\.Controls|OxyPlot|Microsoft\.Win32\.TaskScheduler)' } |
        Copy-Item -Destination lib
    Copy-Item "$tmp\x\LibreHardwareMonitor.exe.config" lib\SensorHelper.exe.config
}

# 2. PresentMon console app -> vendor\
if (-not (Test-Path vendor\presentmon.exe)) {
    Write-Host "Downloading PresentMon $PresentMonVersion"
    New-Item -ItemType Directory vendor -Force | Out-Null
    Invoke-WebRequest "https://github.com/GameTechDev/PresentMon/releases/download/v$PresentMonVersion/PresentMon-$PresentMonVersion-x64.exe" -OutFile vendor\presentmon.exe
}

# 2b. PawnIO driver setup (the installer offers it; needed for CPU temperatures)
if (-not (Test-Path installer\deps\PawnIO_setup.exe)) {
    Write-Host "Downloading PawnIO setup $PawnIOVersion"
    New-Item -ItemType Directory installer\deps -Force | Out-Null
    Invoke-WebRequest "https://github.com/namazso/PawnIO.Setup/releases/download/$PawnIOVersion/PawnIO_setup.exe" -OutFile installer\deps\PawnIO_setup.exe
}

# 3. Sensor helper (uses the C# compiler that ships with Windows)
& "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe" /nologo /platform:x64 /out:lib\SensorHelper.exe /r:lib\LibreHardwareMonitorLib.dll lib\SensorHelper.cs
if ($LASTEXITCODE) { throw "SensorHelper build failed" }

# 4. Exe (one folder, asks for admin through its manifest)
python -m PyInstaller --noconfirm --clean --windowed --onedir --uac-admin `
    --name Lowtide --icon assets\lowtide.ico `
    --add-data "assets;assets" --add-data "lib;lib" --add-data "vendor;vendor" `
    --hidden-import pystray._win32 `
    --exclude-module numpy `
    lowtide.py
if ($LASTEXITCODE) { throw "PyInstaller failed" }
Remove-Item dist\Lowtide\_internal\lib\SensorHelper.cs -ErrorAction SilentlyContinue

# 5. Installer
$iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe") |
    Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "Inno Setup 6 not found (winget install JRSoftware.InnoSetup)" }
& $iscc /Q "/DAppVersion=$Version" installer\lowtide.iss
if ($LASTEXITCODE) { throw "Installer build failed" }
Write-Host "Done: release\Lowtide-Setup-$Version.exe"
