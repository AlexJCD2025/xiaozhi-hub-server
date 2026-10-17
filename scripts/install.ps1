# xiaozhi-hub-server 一键安装（Windows PowerShell）
# ⚠️ 未实测：本脚本按 macOS 上实测过的 install.sh 逐步对照编写，尚未在 Windows 上运行验证。
# 用法（在项目根目录）：powershell -ExecutionPolicy Bypass -File scripts\install.ps1
# 可选环境变量：$env:UV_INDEX_URL（Python 包镜像），$env:ASSUME_YES = "1"（非交互）
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$Server = Join-Path $Root "main\xiaozhi-server"
$Venv = Join-Path $Server ".venv"
$Py = Join-Path $Venv "Scripts\python.exe"
$Model = Join-Path $Server "models\SenseVoiceSmall\model.pt"
$ModelUrl = "https://modelscope.cn/models/iic/SenseVoiceSmall/resolve/master/model.pt"
$ModelMinBytes = 900000000
$Config = Join-Path $Server "data\.config.yaml"

function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Info($msg) { Write-Host "    $msg" }
function Fail($msg) { Write-Host "`n✗ $msg" -ForegroundColor Red; exit 1 }
function Ask($question, $default = "N") {
    if ($env:ASSUME_YES -eq "1") { $answer = $default }
    else {
        $hint = if ($default -eq "Y") { "[Y/n]" } else { "[y/N]" }
        $answer = Read-Host "    $question $hint"
        if (-not $answer) { $answer = $default }
    }
    return $answer -match "^[Yy]"
}
function Has($cmd) { return [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }

# ---------- 1. uv ----------
Step "检查 uv（用于管理 Python 版本和虚拟环境）"
if (-not (Has "uv")) {
    $userBin = Join-Path $env:USERPROFILE ".local\bin"
    if (Test-Path (Join-Path $userBin "uv.exe")) { $env:Path = "$userBin;$env:Path" }
}
if (-not (Has "uv")) {
    if (Ask "未找到 uv，是否用官方脚本安装？" "Y") {
        powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
        $env:Path = "$(Join-Path $env:USERPROFILE '.local\bin');$env:Path"
    } else { Fail "请先安装 uv：https://docs.astral.sh/uv/getting-started/installation/" }
}
Info (uv --version)

# ---------- 2. ffmpeg ----------
Step "检查 ffmpeg"
if (Has "ffmpeg") { Info "ffmpeg 已安装" }
elseif (Has "winget") {
    if (Ask "未找到 ffmpeg，是否用 winget 安装（Gyan.FFmpeg）？" "Y") {
        winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
        Info "安装后可能需要重新打开 PowerShell，ffmpeg 才会出现在 PATH 中"
    } else { Fail "请手动安装 ffmpeg 并加入 PATH 后重新运行本脚本" }
} else { Fail "未找到 ffmpeg 和 winget，请手动安装 ffmpeg 并加入 PATH：https://www.gyan.dev/ffmpeg/builds/" }

# ---------- 3. Python 3.10 虚拟环境 ----------
Step "准备 Python 3.10 虚拟环境"
if ((Test-Path $Py) -and ((& $Py -c "import sys; print(sys.version_info[:2] == (3, 10))") -eq "True")) {
    Info "已存在：$Venv"
} else { uv venv --python 3.10 $Venv }

# ---------- 4. Python 依赖 ----------
Step "安装 Python 依赖（首次较慢，torch 等体积较大）"
& $Py (Join-Path $Root "scripts\install_deps.py") --python $Py
if ($LASTEXITCODE -ne 0) { Fail "Python 依赖安装失败" }

# ---------- 5. opus ----------
Step "检查 opus"
& $Py -c "import opuslib_next" 2>$null
if ($LASTEXITCODE -eq 0) { Info "opus 可用" }
else {
    Info "未找到 opus.dll。可选的处理方式："
    Info "  1) 安装 Anaconda/Miniconda 后执行：conda install -c conda-forge libopus，并把 Library\bin 加入 PATH"
    Info "  2) 下载 Windows 版 opus.dll，放到 $Server 目录或任意 PATH 目录中"
    Info "处理后重新运行本脚本，或直接运行 scripts\start.ps1 查看自检结果"
}

# ---------- 6. 语音识别模型 ----------
Step "下载语音识别模型 SenseVoiceSmall（约 900MB）"
if ((Test-Path $Model) -and ((Get-Item $Model).Length -ge $ModelMinBytes)) { Info "已存在，跳过：$Model" }
else {
    New-Item -ItemType Directory -Force -Path (Split-Path $Model) | Out-Null
    $part = "$Model.part"
    if (Has "curl.exe") { curl.exe -L --fail --retry 3 -C - -o $part $ModelUrl }
    else { Invoke-WebRequest -Uri $ModelUrl -OutFile $part }
    if (-not (Test-Path $part) -or (Get-Item $part).Length -lt $ModelMinBytes) { Fail "模型下载失败或文件大小异常，请重新运行本脚本" }
    Move-Item -Force $part $Model
    Info "已下载：$Model"
}

# ---------- 7. 配置文件 ----------
Step "生成配置文件"
$claude = Get-Command claude.exe -ErrorAction SilentlyContinue
if (-not $claude) { $claude = Get-Command claude -ErrorAction SilentlyContinue }
if (Test-Path $Config) { Info "已存在，保持不变：$Config" }
else {
    New-Item -ItemType Directory -Force -Path (Split-Path $Config) | Out-Null
    $text = Get-Content (Join-Path $Root "scripts\config.template.yaml") -Raw -Encoding UTF8
    if ($claude -and (Ask "检测到 Claude Code，默认使用它作为大模型？" "Y")) { Info "大模型：ClaudeCLI（本机 Claude Code）" }
    else {
        $text = $text -replace "(\n  LLM: )ClaudeCLI[^\n]*", '${1}ChatGLMLLM        # 在管理后台填写 API Key'
        $text = $text -replace "(\n  Intent: )nointent[^\n]*", '${1}function_call'
        Info "大模型：ChatGLMLLM（智谱 glm-4-flash，需要在管理后台填写 API Key）"
    }
    [System.IO.File]::WriteAllText($Config, $text, (New-Object System.Text.UTF8Encoding $false))
    Info "已生成：$Config"
}

# ---------- 8. Claude Code ----------
Step "检查 Claude Code"
if ($claude) {
    Info "$(& $claude.Source --version)（$($claude.Source)）"
    Info "如果还没登录，请在终端运行 claude 并按提示登录（安装脚本不会代替你登录）"
} else { Info "未找到 Claude Code。如需使用，请先安装并登录，再在管理后台切换到 ClaudeCLI" }
New-Item -ItemType Directory -Force -Path (Join-Path $env:USERPROFILE "xiaozhi-workspace") | Out-Null

# ---------- 9. 自检 ----------
Step "环境自检"
& $Py (Join-Path $Root "scripts\doctor.py") --skip-ports
if ($LASTEXITCODE -ne 0) { Info "自检发现异常，请按上面的提示处理后再启动" }

Write-Host @"

安装完成。

  启动服务：  powershell -ExecutionPolicy Bypass -File scripts\start.ps1
  首次打开管理后台需要在本机浏览器访问 http://127.0.0.1:8003/admin 设置密码。

提醒：
  - 请在路由器里给这台电脑设置固定 IP，否则 IP 变了设备会连不上。
  - 请在 Windows 防火墙中放行 TCP 端口 8000 和 8003（仅限专用网络）。
  - 服务只应在局域网内使用，不要暴露到公网。
"@
