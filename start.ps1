# Bilibili Downloader: one-command setup + start (Windows).
#
#   run.bat                  first run installs everything, every run starts the app
#   run.bat -Ai yes          install the AI add-ons without asking
#   run.bat -Ai no           never install them (plain downloader only)
#   run.bat -NoOpen          do not open the browser
#   run.bat -Port 5001       use another port
#   run.bat -DryRun          show what would be installed, install nothing
#
# Safe to run again and again: anything that is already installed is skipped.
param(
    [ValidateSet("ask", "yes", "no")] [string]$Ai = "ask",
    [int]$Port = 5000,
    [switch]$NoOpen,
    [switch]$DryRun,
    [switch]$Check   # start, test that the page answers, stop (used for testing the package)
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

# the AI add-ons (PyTorch, models) live outside the app folder: several GB, and the folder may be synced to a cloud drive
$AudioHome = if ($env:BILI_AUDIO_HOME) { $env:BILI_AUDIO_HOME } else { Join-Path $env:USERPROFILE "bili-audio" }
$env:BILI_AUDIO_HOME = $AudioHome
$env:BILI_PORT = "$Port"
$Interactive = [Environment]::UserInteractive -and -not [Console]::IsInputRedirected

function Say($text, $color = "Gray") { Write-Host $text -ForegroundColor $color }
function Step($text) { Write-Host ""; Write-Host "== $text" -ForegroundColor Cyan }
function Ask($question, $default = $true) {
    if (-not $Interactive) { return $default }
    $hint = if ($default) { "[Y/n]" } else { "[y/N]" }
    $answer = Read-Host "$question $hint"
    if (-not $answer) { return $default }
    return $answer.Trim().ToLower().StartsWith("y")
}
function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
}
function Have($name) { return [bool](Get-Command $name -ErrorAction SilentlyContinue) }

# Run a program, hide its output, and return only its exit code. Needed because with $ErrorActionPreference = "Stop",
# Windows PowerShell treats any text a program writes to stderr as a crash as soon as stderr is redirected, so a
# failing check (for example "are the packages installed?") would stop the whole script instead of just answering "no".
function Invoke-Quiet($exe, [string[]]$arguments) {
    $saved = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & $exe @arguments *> $null; return $LASTEXITCODE }
    catch { return 1 }
    finally { $ErrorActionPreference = $saved }
}

# Same, but return what the program printed (or $null if it failed).
function Invoke-Capture($exe, [string[]]$arguments) {
    $saved = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $out = & $exe @arguments 2>$null
        if ($LASTEXITCODE -ne 0) { return $null }
        return ("$out").Trim()
    }
    catch { return $null }
    finally { $ErrorActionPreference = $saved }
}

function Has-NvidiaGpu {
    if (-not (Have "nvidia-smi")) { return $false }
    return ((Invoke-Quiet "nvidia-smi" @("-L")) -eq 0)
}

function Find-Python {
    foreach ($candidate in @(@("py", "-3"), @("python"), @("python3"))) {
        try {
            $exe = $candidate[0]
            $extra = @($candidate | Select-Object -Skip 1)
            $version = Invoke-Capture $exe (@($extra) + @("-c", "import sys; print('%d.%d' % sys.version_info[:2])"))
            if ($version) {
                $parts = "$version".Trim().Split(".")
                if ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge 10) {
                    return [pscustomobject]@{ Exe = $exe; Args = $extra; Version = "$version".Trim() }
                }
            }
        } catch { }
    }
    return $null
}

function Pip($python, [string[]]$arguments) {
    & $python -m pip install --disable-pip-version-check --retries 10 --timeout 60 @arguments
    if ($LASTEXITCODE -ne 0) { throw "pip failed: $($arguments -join ' ')" }
}

# download with resume and retries, for big files on shaky connections
function Get-File($url, $dest, [long]$minBytes) {
    $name = Split-Path $dest -Leaf
    if ((Test-Path -LiteralPath $dest) -and ((Get-Item -LiteralPath $dest).Length -ge $minBytes)) { Say "  have $name"; return }
    if ($DryRun) { Say "  [dry run] would download $name from $url"; return }
    New-Item -ItemType Directory -Force (Split-Path $dest) | Out-Null
    Say "  downloading $name ..."
    for ($try = 0; $try -lt 60; $try++) {
        & curl.exe -L --fail --silent --show-error -C - --connect-timeout 20 --speed-limit 15000 --speed-time 40 -o $dest $url
        if ((Test-Path -LiteralPath $dest) -and ((Get-Item -LiteralPath $dest).Length -ge $minBytes)) { return }
        Start-Sleep -Seconds 3
    }
    throw "Could not download $name from $url"
}

function Wait-Port([int]$port, [int]$seconds) {
    $end = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $end) {
        try { $client = New-Object Net.Sockets.TcpClient; $client.Connect("127.0.0.1", $port); $client.Close(); return $true }
        catch { Start-Sleep -Milliseconds 400 }
    }
    return $false
}

Write-Host ""
Say "Bilibili Downloader" "White"
Say "  app folder : $Root"
Say "  AI add-ons : $AudioHome"
if ($DryRun) { Say "  DRY RUN: nothing will be installed or started" "Yellow" }

# ---------------------------------------------------------------- already running?
if (-not $DryRun -and (Wait-Port $Port 1)) {
    Say "Something is already running on port $Port. Opening it." "Yellow"
    if (-not $NoOpen) { Start-Process "http://127.0.0.1:$Port" }
    exit 0
}

# ---------------------------------------------------------------- Python
Step "Python"
$python = Find-Python
if (-not $python) {
    Say "Python 3.10 or newer was not found." "Yellow"
    if ((Have "winget") -and (Ask "Install Python 3.12 now with winget?")) {
        if (-not $DryRun) { & winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements }
        Refresh-Path
        $python = Find-Python
    }
}
if (-not $python) {
    Say "Please install Python 3.12 from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'), then run this again." "Red"
    exit 1
}
Say "  using Python $($python.Version)"

# ---------------------------------------------------------------- app environment
Step "App environment (Flask + yt-dlp)"
# kept next to the AI add-ons, in a short folder: Windows fails on very long paths (a deep unzip location would break pip)
$AppVenv = Join-Path $AudioHome "app-venv"
$venvPy = Join-Path $AppVenv "Scripts\python.exe"
$stamp = Join-Path $AppVenv "requirements.stamp"
$wanted = (Get-FileHash (Join-Path $Root "requirements.txt")).Hash
if ($AppVenv.Length -gt 150) { throw "The folder path is too long ($AppVenv). Set a shorter one, for example:  set BILI_AUDIO_HOME=C:\bili  and run again." }
if ($Root.Length -gt 150) { Say "  Warning: the app folder path is very long; if anything fails, move the folder to something like C:\bili-downloader." "Yellow" }
if ($DryRun) {
    Say "  [dry run] would create $AppVenv and install requirements.txt (skipped if already done)"
} else {
    if (-not (Test-Path $venvPy)) {
        New-Item -ItemType Directory -Force $AudioHome | Out-Null
        & $python.Exe @($python.Args) -m venv $AppVenv
        if ($LASTEXITCODE -ne 0) { throw "could not create the app environment in $AppVenv" }
    }
    if (-not (Test-Path $stamp) -or (Get-Content $stamp -ErrorAction SilentlyContinue) -ne $wanted) {
        Say "  installing requirements ..."
        Pip $venvPy @("-r", (Join-Path $Root "requirements.txt"))
        Set-Content -Path $stamp -Value $wanted
    } else { Say "  up to date" }
    # Bilibili changes often; a current yt-dlp keeps downloads working. A failure here is not fatal.
    $null = Invoke-Quiet $venvPy @("-m", "pip", "install", "--disable-pip-version-check", "-q", "-U", "yt-dlp")
}

# ---------------------------------------------------------------- ffmpeg
Step "ffmpeg (needed for good quality, crop, music removal, captions, voice)"
if (-not (Have "ffmpeg")) {
    Say "ffmpeg was not found." "Yellow"
    if ((Have "winget") -and (Ask "Install ffmpeg now with winget?")) {
        if (-not $DryRun) { & winget install -e --id Gyan.FFmpeg --accept-package-agreements --accept-source-agreements }
        Refresh-Path
    }
}
if (Have "ffmpeg") { Say "  found" } else { Say "  not found: the app still works, but only with basic downloads. Install ffmpeg (https://www.gyan.dev/ffmpeg/builds/) and run this again." "Yellow" }

# ---------------------------------------------------------------- AI add-ons
Step "AI add-ons (remove background music, English captions, English voice)"
$audioPy = Join-Path $AudioHome "venv\Scripts\python.exe"
$ready = Join-Path $AudioHome ".ready"
$declined = Join-Path $AudioHome ".declined"
$checkImports = "import torch, torchaudio, numpy, rapidocr_onnxruntime, cv2, transformers, sentencepiece, kokoro_onnx"
$installed = (Test-Path $ready) -and (Test-Path $audioPy)
$install = $false
if ($Ai -eq "yes") { $install = $true }
elseif ($Ai -eq "ask" -and -not $installed -and -not (Test-Path $declined)) {
    Say "  Needs about 7 GB of disk space and a long first-time download (PyTorch, speech and translation models)."
    Say "  Works best with an NVIDIA graphics card; without one it runs on the processor, which is much slower."
    $install = Ask "Install the AI add-ons now?" $true
    if (-not $install -and -not $DryRun) { New-Item -ItemType Directory -Force $AudioHome | Out-Null; Set-Content $declined "declined" }
}
if ($installed -and -not $install) { Say "  installed" }
elseif (-not $install) { Say "  skipped (run  run.bat -Ai yes  to install them later)" }
else {
    $free = [math]::Round((Get-PSDrive -Name ((Split-Path $AudioHome -Qualifier).TrimEnd(":")) ).Free / 1GB, 1)
    Say "  free disk space: $free GB"
    if ($free -lt 9) { Say "  Warning: less than 9 GB free; the installation may fail." "Yellow" }
    $gpu = Has-NvidiaGpu
    $torchIndex = if ($gpu) { "https://download.pytorch.org/whl/cu128" } else { "https://download.pytorch.org/whl/cpu" }
    Say $(if ($gpu) { "  NVIDIA graphics card found: installing the GPU version of PyTorch (about 3 GB)" } else { "  no NVIDIA graphics card found: installing the CPU version (slower processing)" })

    $importsOk = $false
    if ((Test-Path $audioPy) -and -not $DryRun) { $importsOk = ((Invoke-Quiet $audioPy @("-c", $checkImports)) -eq 0) }
    if ($importsOk) { Say "  python packages already installed" }
    elseif ($DryRun) {
        Say "  [dry run] would create $AudioHome\venv"
        Say "  [dry run] would run: pip install torch torchaudio --index-url $torchIndex"
        Say "  [dry run] would run: pip install numpy rapidocr-onnxruntime opencv-python-headless transformers sentencepiece safetensors kokoro-onnx"
    } else {
        New-Item -ItemType Directory -Force $AudioHome | Out-Null
        if (-not (Test-Path $audioPy)) { & $python.Exe @($python.Args) -m venv (Join-Path $AudioHome "venv"); if ($LASTEXITCODE -ne 0) { throw "could not create the AI environment" } }
        Say "  installing PyTorch (large, please wait) ..."
        Pip $audioPy @("torch", "torchaudio", "--index-url", $torchIndex)
        Say "  installing the other packages ..."
        Pip $audioPy @("numpy", "rapidocr-onnxruntime", "opencv-python-headless", "transformers", "sentencepiece", "safetensors", "kokoro-onnx")
    }

    Say "  models:"
    Get-File "https://github.com/ZFTurbo/Music-Source-Separation-Training/releases/download/v.1.0.3/model_bandit_plus_dnr_sdr_11.47.chpt" (Join-Path $Root "models\model_bandit_plus_dnr.chpt") 148000000
    Get-File "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx" (Join-Path $AudioHome "tts\kokoro-v1.0.onnx") 300000000
    Get-File "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin" (Join-Path $AudioHome "tts\voices-v1.0.bin") 20000000
    if ($DryRun) { Say "  [dry run] would download the translation model (about 300 MB, from Hugging Face)" }
    else {
        Say "  translation model (first time only) ..."
        & $audioPy (Join-Path $Root "audio\captions.py") --prefetch | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "could not download the translation model" }
        Set-Content -Path $ready -Value "ok"
        Remove-Item -LiteralPath $declined -ErrorAction SilentlyContinue
        Say "  AI add-ons ready" "Green"
    }
}

# ---------------------------------------------------------------- start
if ($DryRun) { Say ""; Say "Dry run finished." "Green"; exit 0 }
Step "Starting"
$server = Start-Process -FilePath $venvPy -ArgumentList @("app.py") -WorkingDirectory $Root -NoNewWindow -PassThru
try {
    if (-not (Wait-Port $Port 60)) { throw "The app did not start. To see the error, run:  `"$venvPy`" app.py" }
    $url = "http://127.0.0.1:$Port"
    Say "  ready: $url" "Green"
    if ($Check) {
        $page = Invoke-WebRequest "$url/" -UseBasicParsing -TimeoutSec 30
        $api = Invoke-RestMethod "$url/api/settings" -TimeoutSec 120
        Say "  page: HTTP $($page.StatusCode) | music removal available: $($api.music_available) | captions: $($api.captions_available) | english voice: $($api.dub_available)"
        exit 0
    }
    if (-not $NoOpen) { Start-Process $url }
    Say "  downloads are saved in: $(Join-Path $Root 'downloads')"
    Say "  Press Ctrl+C in this window to stop the app."
    $server.WaitForExit()
}
finally {
    if ($server -and -not $server.HasExited) { Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue }
}
