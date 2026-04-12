[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$AgentUrl,

    [string]$InstallDir = "$env:LOCALAPPDATA\SchoolPro",

    [string]$TaskName = "SchoolProAgent",

    [string]$DisplayName = "",

    [switch]$SkipStartupTask,

    [switch]$SkipLaunch
)

$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message)
    Write-Host "==> $Message"
}

function Resolve-PythonCommand {
    $pyCmd = Get-Command py -ErrorAction SilentlyContinue
    if ($pyCmd) {
        return @($pyCmd.Source, "-3")
    }

    $pythonCmd = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCmd) {
        return @($pythonCmd.Source)
    }

    throw "Python 3 was not found. Install Python 3 and try again."
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Command
    )

    & $Command[0] $Command[1..($Command.Length - 1)]
    if ($LASTEXITCODE -ne 0) {
        $commandText = [string]::Join(" ", $Command)
        throw ("Command failed with exit code {0}: {1}" -f $LASTEXITCODE, $commandText)
    }
}

$pythonCmd = Resolve-PythonCommand
$installPath = [System.IO.Path]::GetFullPath($InstallDir)
$venvPath = Join-Path $installPath ".venv"
$agentPath = Join-Path $installPath "pc_agent.py"
$readmePath = Join-Path $installPath "INSTALL.txt"
$venvPython = Join-Path $venvPath "Scripts\python.exe"
$venvPythonw = Join-Path $venvPath "Scripts\pythonw.exe"

Write-Step "Preparing install folder $installPath"
New-Item -ItemType Directory -Path $installPath -Force | Out-Null

Write-Step "Downloading agent"
Invoke-WebRequest -Uri $AgentUrl -OutFile $agentPath -UseBasicParsing

Write-Step "Creating virtual environment"
Invoke-Checked -Command ($pythonCmd + @("-m", "venv", $venvPath))

Write-Step "Upgrading pip"
Invoke-Checked -Command @($venvPython, "-m", "pip", "install", "--upgrade", "pip")

Write-Step "Installing dependencies"
Invoke-Checked -Command @($venvPython, "-m", "pip", "install", "requests", "psutil", "pillow")

if ($DisplayName.Trim()) {
    Write-Step "Setting device name"
    Invoke-Checked -Command @($venvPython, $agentPath, "--set-name", $DisplayName)
}

if (-not $SkipStartupTask) {
    Write-Step "Configuring startup entry"
    $env:PCBOT_STARTUP_TASK_NAME = $TaskName
    Invoke-Checked -Command @($venvPython, $agentPath, "--install-startup")
}

if (-not $SkipLaunch) {
    Write-Step "Starting agent"
    if (Test-Path $venvPythonw) {
        Start-Process -FilePath $venvPythonw -ArgumentList @($agentPath) -WorkingDirectory $installPath -WindowStyle Hidden
    }
    else {
        Start-Process -FilePath $venvPython -ArgumentList @($agentPath) -WorkingDirectory $installPath
    }
}

$installSummary = @"
SchoolPro installed.

Folder:
$installPath

Agent file:
$agentPath

Startup entry:
$TaskName
"@
$installSummary | Set-Content -Path $readmePath -Encoding UTF8

Write-Host ""
Write-Host "Done."
Write-Host "Folder: $installPath"
Write-Host "Startup entry: $TaskName"
Write-Host "The PC should appear in Telegram after the next heartbeat."
