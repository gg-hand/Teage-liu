# =============================================================
# teage_liu2 start script (PowerShell, Windows 本地开发，前台运行)
# Usage:
#   .\start_liu2.ps1                       # default 127.0.0.1:7878
#   $env:TEAGE_PORT=7979; .\start_liu2.ps1 # custom port
#
# 与 start.ps1（老系统）的区别：
#   - 端口 7878（8000 归老系统，见 CODEBUDDY.md 端口约定）
#   - 配置走 TEAGE2_CONFIG=config-liu2.yaml（两系统配置解耦，2026-09-18）
#   - 以 --factory 指向 teage_liu2.server.app:create_app
# 两系统可同时在各自端口运行，配置/存储互不影响。
# =============================================================
$ErrorActionPreference = "Continue"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

# ========== 0. Load .env ==========
# 两系统共用根 .env（密钥单一来源，与解耦前的 ${VAR} 注入语义一致）
$envFile = Join-Path $ScriptDir ".env"
if (Test-Path $envFile) {
    Write-Host "  Loading .env file" -ForegroundColor DarkGray
    Get-Content $envFile | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith('#') -and $line.Contains('=')) {
            $parts = $line.Split('=', 2)
            $key   = $parts[0].Trim()
            $val   = $parts[1].Trim()
            [Environment]::SetEnvironmentVariable($key, $val, 'Process')
        }
    }
}

# ========== 1. Defaults (env-first) ==========
if (-not $env:TEAGE_HOST)   { $env:TEAGE_HOST   = "127.0.0.1" }
if (-not $env:TEAGE_PORT)   { $env:TEAGE_PORT   = "7878" }
if (-not $env:TEAGE2_CONFIG) { $env:TEAGE2_CONFIG = "config-liu2.yaml" }

$port = [int]$env:TEAGE_PORT

# ========== 2. 解释器（python/py 在部分环境是 Store stub，不可用）==========
$py = $env:TEAGE_PYTHON
if (-not $py) { $py = "D:\soft\Python311\python.exe" }
if (-not (Test-Path $py)) { $py = "python" }

# ========== 3. Config file check ==========
if (-not (Test-Path (Join-Path $ScriptDir $env:TEAGE2_CONFIG))) {
    Write-Host "[ERROR] 配置文件不存在: $env:TEAGE2_CONFIG" -ForegroundColor Red
    Write-Host "        liu2 独立配置需存在（见两系统配置解耦评估文档）"
    exit 1
}

# ========== 4. API Key validation ==========
# config-liu2.yaml 优先使用 ${LLM_MAIN_API_KEY}，兼容 provider 命名
$hasKey = $false
foreach ($k in @('LLM_MAIN_API_KEY', 'DEEPSEEK_API_KEY', 'ANTHROPIC_API_KEY', 'OPENAI_API_KEY', 'DASHSCOPE_API_KEY')) {
    $val = [Environment]::GetEnvironmentVariable($k, 'Process')
    if ($val) { $hasKey = $true; break }
}
if (-not $hasKey) {
    Write-Host "[ERROR] No API Key set (LLM_MAIN_API_KEY / DEEPSEEK_API_KEY / ANTHROPIC_API_KEY ...)." -ForegroundColor Red
    Write-Host "        Please configure .env, see .env.example"
    exit 1
}

# ========== 5. 端口占用检查 ==========
$portConns = Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue
$listener  = $portConns | Where-Object { $_.State -eq 'Listen' } | Select-Object -First 1
if ($listener) {
    Write-Host "[ERROR] 端口 $port 已被占用（PID: $($listener.OwningProcess)）。" -ForegroundColor Red
    Write-Host "        停止旧进程： Stop-Process -Id $($listener.OwningProcess) -Force"
    exit 1
}

# ========== 6. Print banner ==========
Write-Host "------------------------------------------------------------" -ForegroundColor Cyan
Write-Host " Teage Liu2 本地开发模式（前台）"
Write-Host "   Host:     $env:TEAGE_HOST"
Write-Host "   Port:     $env:TEAGE_PORT"
Write-Host "   Config:   $env:TEAGE2_CONFIG"
Write-Host "   健康检查: http://127.0.0.1:$port/health"
Write-Host "   前端 UI:  http://127.0.0.1:$port/ui"
Write-Host "   退出:     Ctrl+C"
Write-Host "------------------------------------------------------------" -ForegroundColor Cyan

# ========== 7. Start service (foreground) ==========
# --factory：create_app 读取 TEAGE2_CONFIG（见 teage_liu2/server/app.py）
& $py -m uvicorn teage_liu2.server.app:create_app `
    --factory `
    --host $env:TEAGE_HOST `
    --port $port `
    --workers 1