# xiaozhi-hub-server 启动（Windows PowerShell）：先快速自检，再启动服务。
# ⚠️ 未实测：尚未在 Windows 上运行验证。
# 用法：powershell -ExecutionPolicy Bypass -File scripts\start.ps1   跳过自检：$env:SKIP_DOCTOR = "1"
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$Server = Join-Path $Root "main\xiaozhi-server"
$Py = Join-Path $Server ".venv\Scripts\python.exe"

if (-not (Test-Path $Py)) { Write-Host "未找到虚拟环境，请先运行 scripts\install.ps1" -ForegroundColor Red; exit 1 }
# 安装脚本把 opus.dll 放在虚拟环境的 Scripts 目录里，opuslib_next 在 PATH 里找它
$env:Path = "$(Join-Path $Server '.venv\Scripts');$env:Path"

if ($env:SKIP_DOCTOR -ne "1") {
    & $Py (Join-Path $Root "scripts\doctor.py")
    if ($LASTEXITCODE -ne 0) {
        $answer = Read-Host "自检有异常，仍要启动吗？[y/N]"
        if ($answer -notmatch "^[Yy]") { exit 1 }
    }
}

# Windows 控制台默认 GBK，统一用 UTF-8 输出中文和 emoji
$env:PYTHONIOENCODING = "utf-8"
Write-Host "正在启动服务，按 Ctrl+C 停止 ..."
Set-Location $Server
& $Py app.py
