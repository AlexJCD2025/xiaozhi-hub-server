# xiaozhi-hub-server 卸载（Windows PowerShell）
# 用法：powershell -ExecutionPolicy Bypass -File scripts\uninstall.ps1
#   删除整个安装目录（代码、虚拟环境、语音识别模型、配置和 API Key），询问是否删除工作目录 %USERPROFILE%\xiaozhi-workspace（默认保留）。
#   Windows 目前没有开机自启，不需要取消。uv、Python 3.10、ffmpeg、Claude Code 可能被别的程序共用，不删。
# 可选环境变量：$env:ASSUME_YES = "1" 不询问，直接卸载（工作目录仍然保留）
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$Server = Join-Path $Root "main\xiaozhi-server"
$Workspace = Join-Path $env:USERPROFILE "xiaozhi-workspace"

function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Info($msg) { Write-Host "    $msg" }
function Fail($msg) { Write-Host "`n✗ $msg" -ForegroundColor Red; exit 1 }
function AskNo($question) { return (Read-Host "    $question [y/N]") -match "^[Yy]" }

# 确认是本项目的安装目录，避免删错
if (-not ((Test-Path (Join-Path $Server "app.py")) -and (Test-Path (Join-Path $Root "scripts\install.ps1"))) -or $Root -eq $env:USERPROFILE) {
    Fail "$Root 看起来不是 xiaozhi-hub-server 的安装目录，已停止"
}

# 开发用的完整仓库（不是浅克隆）里可能有没推送的提交，不替用户删
$devRepo = $false
if ((Get-Command git -ErrorAction SilentlyContinue) -and (Test-Path (Join-Path $Root ".git"))) {
    $old = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    $shallow = (& git -C $Root rev-parse --is-shallow-repository 2>$null | Out-String).Trim()
    $ErrorActionPreference = $old
    $devRepo = ($shallow -eq "false")
}

Write-Host "`n将要卸载 xiaozhi-hub-server："
if ($devRepo) { Info "这是完整的 git 仓库，可能是开发用的，不删除目录：$Root" }
else {
    Info "删除安装目录：$Root"
    Info "（包括虚拟环境、语音识别模型、配置文件 data\.config.yaml 和里面的 API Key）"
}
if ($env:ASSUME_YES -ne "1" -and -not (AskNo "确定卸载？")) { Write-Host "已取消，没有做任何改动。"; exit 0 }

# 服务还在运行时先让用户停掉，不在它运行时删文件
$port = 16688
$cfg = Join-Path $Server "data\.config.yaml"
$t = Get-Content $cfg -Raw -Encoding UTF8 -ErrorAction SilentlyContinue
if ($t -match "(?m)^\s+http_port:\s*(\d+)") { $port = [int]$Matches[1] }
$running = $true
try { Invoke-WebRequest -Uri "http://127.0.0.1:$port/xiaozhi/ota/" -UseBasicParsing -TimeoutSec 2 | Out-Null } catch { $running = $false }
if ($running) { Fail "服务还在运行（端口 $port）。请在运行 start.ps1 的窗口按 Ctrl+C 停止后，重新运行本脚本" }

if ($devRepo) { Write-Host "`n目录 $Root 没有删除，确认不需要后请手动删除。" }
else {
    Step "删除安装目录"
    Set-Location $env:USERPROFILE
    Remove-Item -Recurse -Force $Root
    Info "已删除：$Root"
}

if (Test-Path $Workspace) {
    Step "Claude Code 的工作目录"
    if ($env:ASSUME_YES -ne "1" -and (AskNo "是否删除 ${Workspace}？里面是 Claude Code 帮你读写过的文件")) {
        Remove-Item -Recurse -Force $Workspace
        Info "已删除：$Workspace"
    } else { Info "保留：$Workspace（不需要的话可以手动删除）" }
}

Write-Host @"

卸载完成。下面这些可能被别的程序共用，没有删除，确认不需要的话可以手动处理：
  - uv 的下载缓存（约 500MB）：uv cache clean
  - uv 安装的 Python 3.10：uv python uninstall 3.10
  - uv 本身、ffmpeg、Claude Code：用当初安装它们的方式卸载（例如 winget uninstall Gyan.FFmpeg）
"@
