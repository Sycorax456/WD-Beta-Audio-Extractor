@echo off
setlocal enabledelayedexpansion
set "carpeta=%~1"
if "%carpeta%"=="" set "carpeta=%cd%"

for %%F in ("%carpeta%\*.fat") do (
    echo Revisando %%~nxF ...
    powershell -NoProfile -Command ^
        "$p='%%F';" ^
        "$b=[System.IO.File]::ReadAllBytes($p);" ^
        "if($b[8] -ne 0x02 -or $b[9] -ne 0x05 -or $b[10] -ne 0x37){" ^
        "  $b[8]=0x02; $b[9]=0x05; $b[10]=0x37;" ^
        "  [System.IO.File]::WriteAllBytes($p,$b);" ^
        "  Write-Host '  -> Corregido' -ForegroundColor Green" ^
        "} else { Write-Host '  -> Ya estaba OK' -ForegroundColor Yellow }"
)
echo.
echo Listo.
pause