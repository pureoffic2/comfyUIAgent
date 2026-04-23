[CmdletBinding()]
param(
    [string]$AgentUrl = "https://raw.githubusercontent.com/pureoffic2/comfyUIAgent/main/pc_agent.py",

    [string]$InstallDir = "",

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

function Resolve-InstallDirectory {
    param([string]$RequestedPath)

    if ($RequestedPath -and $RequestedPath.Trim()) {
        return [System.IO.Path]::GetFullPath(
            [Environment]::ExpandEnvironmentVariables($RequestedPath)
        )
    }

    $basePath = $env:LOCALAPPDATA
    if (-not ($basePath -and $basePath.Trim())) {
        $basePath = Join-Path $env:USERPROFILE "AppData\Local"
    }
    if (-not ($basePath -and $basePath.Trim())) {
        $basePath = Join-Path ([System.IO.Path]::GetTempPath()) "SystemPortal"
        return [System.IO.Path]::GetFullPath($basePath)
    }

    return [System.IO.Path]::GetFullPath((Join-Path $basePath "SystemPortal"))
}

function Ensure-Directory {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        New-Item -ItemType Directory -Path $Path -Force | Out-Null
    }
}

function Reset-BrokenVenv {
    param(
        [Parameter(Mandatory = $true)]
        [string]$VenvPath,

        [Parameter(Mandatory = $true)]
        [string]$VenvPython
    )

    if ((Test-Path -LiteralPath $VenvPath) -and -not (Test-Path -LiteralPath $VenvPython)) {
        Write-Step "Removing incomplete virtual environment"
        Remove-Item -LiteralPath $VenvPath -Recurse -Force
    }
}

function Resolve-PythonCommand {
    $pyCmd = Get-Command py -ErrorAction SilentlyContinue
    if ($pyCmd -and (Test-Path $pyCmd.Source)) {
        return @{
            FilePath   = $pyCmd.Source
            PrefixArgs = @("-3")
        }
    }

    $pythonCandidates = @()
    foreach ($candidate in @(
        (Get-Command python -ErrorAction SilentlyContinue),
        (Get-ChildItem "$env:LOCALAPPDATA\Programs\Python" -Filter python.exe -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1)
    )) {
        if ($null -eq $candidate) {
            continue
        }
        $source = if ($candidate.PSObject.Properties["Source"]) { $candidate.Source } else { $candidate.FullName }
        if (-not $source) {
            continue
        }
        if ($source -like "*\WindowsApps\python.exe") {
            continue
        }
        if (Test-Path $source) {
            $pythonCandidates += $source
        }
    }

    $pythonPath = $pythonCandidates | Select-Object -First 1
    if ($pythonPath) {
        return @{
            FilePath   = $pythonPath
            PrefixArgs = @()
        }
    }

    throw "Python 3 was not found. Install Python 3 and try again."
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,

        [string[]]$ArgumentList = @()
    )

    & $FilePath @ArgumentList
    if ($LASTEXITCODE -ne 0) {
        $commandText = ([string]::Join(" ", @($FilePath) + $ArgumentList))
        throw ("Command failed with exit code {0}: {1}" -f $LASTEXITCODE, $commandText)
    }
}

function Invoke-CheckedCommand {
    param(
        [Parameter(Mandatory = $true)]
        [hashtable]$Command,

        [string[]]$ArgumentList = @()
    )

    Invoke-Checked -FilePath $Command.FilePath -ArgumentList ($Command.PrefixArgs + $ArgumentList)
}

function Try-Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,

        [string[]]$ArgumentList = @()
    )

    try {
        Invoke-Checked -FilePath $FilePath -ArgumentList $ArgumentList
        return $true
    }
    catch {
        Write-Host "==> Warning: $($_.Exception.Message)"
        return $false
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
if (-not ($AgentUrl -and $AgentUrl.Trim())) {
    $AgentUrl = "https://raw.githubusercontent.com/pureoffic2/comfyUIAgent/main/pc_agent.py"
}
$pythonCmd = Resolve-PythonCommand
$installPath = Resolve-InstallDirectory -RequestedPath $InstallDir
$venvPath = Join-Path $installPath ".venv"
$agentPath = Join-Path $installPath "pc_agent.py"
$readmePath = Join-Path $installPath "INSTALL.txt"
$venvPython = Join-Path $venvPath "Scripts\python.exe"
$venvPythonw = Join-Path $venvPath "Scripts\pythonw.exe"

Write-Step "Preparing install folder $installPath"
Ensure-Directory -Path $installPath
Ensure-Directory -Path (Join-Path $installPath "logs")
Ensure-Directory -Path (Join-Path $installPath "data")
Ensure-Directory -Path (Join-Path $installPath "tmp")

Write-Step "Downloading agent"
Invoke-Download -Uri $AgentUrl -OutFile $agentPath

Reset-BrokenVenv -VenvPath $venvPath -VenvPython $venvPython
if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Step "Creating virtual environment"
    if (Test-Path $venvPath) {
        Remove-Item -LiteralPath $venvPath -Recurse -Force -ErrorAction SilentlyContinue
    }
    Invoke-CheckedCommand -Command $pythonCmd -ArgumentList @("-m", "venv", $venvPath)
    if (-not (Test-Path $venvPython)) {
        throw "Virtual environment creation failed: $venvPython was not created."
    }
}
else {
    Write-Step "Using existing virtual environment"
}
Write-Step "Bootstrapping pip"
Try-Invoke-Checked -FilePath $venvPython -ArgumentList @("-m", "ensurepip", "--upgrade") | Out-Null

Write-Step "Upgrading pip"
Try-Invoke-Checked -FilePath $venvPython -ArgumentList @("-m", "pip", "install", "--disable-pip-version-check", "--upgrade", "pip") | Out-Null

Write-Step "Installing dependencies"
Invoke-Checked -FilePath $venvPython -ArgumentList @("-m", "pip", "install", "--disable-pip-version-check", "requests", "psutil", "pillow")

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
