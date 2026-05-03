$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$BaseUrl = 'https://raw.githubusercontent.com/pureoffic2/comfyUIAgent/main'
$Ts = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
$Installer = Join-Path $env:TEMP ("systemportal-install-$Ts.ps1")
$AgentUrl = "$BaseUrl/pc_agent.py?ts=$Ts"
$InstallUrl = "$BaseUrl/install.ps1?ts=$Ts"

if ([string]::IsNullOrWhiteSpace($env:SYSTEMPORTAL_DISPLAY_NAME)) {
    $DisplayName = $env:COMPUTERNAME
} else {
    $DisplayName = $env:SYSTEMPORTAL_DISPLAY_NAME
}

try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -UseBasicParsing -Uri $InstallUrl -OutFile $Installer
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Installer -AgentUrl $AgentUrl -DisplayName $DisplayName
    $ExitCode = if ($LASTEXITCODE -is [int]) { $LASTEXITCODE } else { 0 }
    exit $ExitCode
} catch {
    Write-Host ''
    Write-Host 'SystemPortal install failed:'
    Write-Host $_.Exception.Message
    Read-Host 'Press Enter to close'
    exit 1
}
