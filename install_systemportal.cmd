@echo off
setlocal
powershell -ExecutionPolicy Bypass -Command "$ts=[DateTimeOffset]::UtcNow.ToUnixTimeSeconds(); $tmp=Join-Path $env:TEMP ('systemportal-install-' + $ts + '.ps1'); Invoke-WebRequest ('https://raw.githubusercontent.com/pureoffic2/comfyUIAgent/main/install.ps1?ts=' + $ts) -OutFile $tmp; powershell -ExecutionPolicy Bypass -File $tmp -AgentUrl ('https://raw.githubusercontent.com/pureoffic2/comfyUIAgent/main/pc_agent.py?ts=' + $ts) -DisplayName 'Pc Home'"
