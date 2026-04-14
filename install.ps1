[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$AgentUrl,

    [string]$InstallDir = "$env:LOCALAPPDATA\SystemPortal",

    [string]$TaskName = "SystemPortalAgent",

    [string]$DisplayName = "",

    [switch]$SkipStartupTask,

    [switch]$SkipLaunch
)

$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message)
    Write-Host "==> $Message"
}

function Enable-Tls12IfPossible {
    try {
        [System.Net.ServicePointManager]::SecurityProtocol = `
            [System.Net.ServicePointManager]::SecurityProtocol -bor `
            [System.Net.SecurityProtocolType]::Tls12
    }
    catch {
    }
}

function Invoke-Download {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Uri,

        [Parameter(Mandatory = $true)]
        [string]$OutFile
    )

    $params = @{
        Uri     = $Uri
        OutFile = $OutFile
    }

    $iwr = Get-Command Invoke-WebRequest -ErrorAction Stop
    if ($iwr.Parameters.ContainsKey("UseBasicParsing")) {
        $params.UseBasicParsing = $true
    }

    Invoke-WebRequest @params
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

function Invoke-HiddenPython {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,

        [Parameter(Mandatory = $true)]
        [string[]]$ArgumentList
    )

    $escapedArgs = foreach ($arg in $ArgumentList) {
        if ($null -eq $arg) {
            '""'
            continue
        }
        '"' + ($arg -replace '(\\*)"', '$1$1\"' -replace '(\\+)$', '$1$1') + '"'
    }
    $process = Start-Process -FilePath $FilePath -ArgumentList ($escapedArgs -join ' ') -WindowStyle Hidden -PassThru -Wait
    if ($process.ExitCode -ne 0) {
        $commandText = ([string]::Join(" ", @($FilePath) + $ArgumentList))
        throw ("Command failed with exit code {0}: {1}" -f $process.ExitCode, $commandText)
    }
}

Enable-Tls12IfPossible
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
Invoke-Download -Uri $AgentUrl -OutFile $agentPath

Write-Step "Creating virtual environment"
Invoke-Checked -Command ($pythonCmd + @("-m", "venv", $venvPath))

Write-Step "Upgrading pip"
Invoke-Checked -Command @($venvPython, "-m", "pip", "install", "--upgrade", "pip")

Write-Step "Installing dependencies"
Invoke-Checked -Command @($venvPython, "-m", "pip", "install", "requests", "psutil", "pillow")

Write-Step "Saving update source"
Invoke-HiddenPython -FilePath $venvPythonw -ArgumentList @($agentPath, "--set-update-url", $AgentUrl)

if ($DisplayName.Trim()) {
    Write-Step "Setting device name"
    Invoke-HiddenPython -FilePath $venvPythonw -ArgumentList @($agentPath, "--set-name", $DisplayName)
}

if (-not $SkipStartupTask) {
    Write-Step "Configuring startup entry"
    $env:PCBOT_STARTUP_TASK_NAME = $TaskName
    Invoke-HiddenPython -FilePath $venvPythonw -ArgumentList @($agentPath, "--install-startup")
}

if (-not $SkipLaunch) {
    Write-Step "Starting agent"
    if (Test-Path $venvPythonw) {
        Start-Process -FilePath $venvPythonw -ArgumentList @($agentPath) -WorkingDirectory $installPath -WindowStyle Hidden
    }
    else {
        $launchVbs = Join-Path $installPath "launch_hidden.vbs"
        @"
Set shell = CreateObject("WScript.Shell")
shell.Run Chr(34) & "$venvPython" & Chr(34) & " " & Chr(34) & "$agentPath" & Chr(34), 0
"@ | Set-Content -Path $launchVbs -Encoding Ascii
        Start-Process -FilePath "wscript.exe" -ArgumentList @($launchVbs) -WindowStyle Hidden
    }
}

$installSummary = @"
SystemPortal installed.

Folder:
$installPath

Agent file:
$agentPath

Startup entry:
$TaskName

Autostart mode:
per-user hidden logon start

Update URL:
$AgentUrl
"@
$installSummary | Set-Content -Path $readmePath -Encoding UTF8

Write-Host ""
Write-Host "Done."
Write-Host "Folder: $installPath"
Write-Host "Startup entry: $TaskName"
Write-Host "The PC should appear in Telegram after the next heartbeat."
