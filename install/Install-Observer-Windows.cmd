@echo off
rem Observer installer for Windows: double-click to run. It downloads and runs install\install-windows.ps1 from the company repository.
chcp 65001 >nul
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; $s = Invoke-RestMethod -UseBasicParsing 'https://raw.githubusercontent.com/LoctekIT-AI/observer/main/install/install-windows.ps1'; Invoke-Expression $s"
echo.
pause
