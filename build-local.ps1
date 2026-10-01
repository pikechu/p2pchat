param([switch]$DebugBuild)

$ErrorActionPreference = 'Stop'
$buildRoot = 'F:\beam-build'
$pythonRoot = Join-Path $buildRoot 'toolchain\python311'
$pythonExe = Join-Path $pythonRoot 'python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) {
    throw "请先准备 F 盘 Python 工具链：$pythonExe"
}
if ([IO.Path]::GetPathRoot($PSScriptRoot) -ne 'F:\') {
    throw '请从 F 盘项目目录运行本地打包脚本。'
}

# 仅为本次构建设置环境，工具、下载缓存和临时文件均落在 F 盘。
$settings = @{
    # 不继承桌面应用附加的 Poppler 等 DLL 搜索路径。
    PATH = (@($pythonRoot, (Join-Path $pythonRoot 'DLLs'), (Join-Path $env:SystemRoot 'System32'), $env:SystemRoot) -join ';')
    PYTHONHOME = $pythonRoot
    PYTHONPATH = ''
    PYTHONNOUSERSITE = '1'
    PYTHONUTF8 = '1'
    PYTHONIOENCODING = 'utf-8'
    PYTHONDONTWRITEBYTECODE = '1'
    TEMP = (Join-Path $buildRoot 'tmp')
    TMP = (Join-Path $buildRoot 'tmp')
    TMPDIR = (Join-Path $buildRoot 'tmp')
    PIP_CACHE_DIR = (Join-Path $buildRoot 'cache\pip')
    PYINSTALLER_CONFIG_DIR = (Join-Path $buildRoot 'cache\pyinstaller')
    BEAM_BUILD_DIR = $buildRoot
    BEAM_PYINSTALLER_DIST_DIR = (Join-Path $buildRoot 'local-build\dist')
}
$previous = @{}
foreach ($name in $settings.Keys) {
    $previous[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
    [Environment]::SetEnvironmentVariable($name, $settings[$name], 'Process')
}
try {
    foreach ($directory in @($settings.TEMP, $settings.PIP_CACHE_DIR, $settings.PYINSTALLER_CONFIG_DIR, $settings.BEAM_PYINSTALLER_DIST_DIR)) {
        New-Item -ItemType Directory -Path $directory -Force | Out-Null
    }
    Write-Host "构建工具：$pythonExe"
    Write-Host "缓存目录：$buildRoot\cache"
    Write-Host "临时目录：$($settings.TEMP)"
    Write-Host "项目中间文件：$PSScriptRoot\build、$PSScriptRoot\BeamChat.spec"
    $arguments = @('-B', (Join-Path $PSScriptRoot 'build.py'))
    if ($DebugBuild) { $arguments += '--debug' }
    $log = Join-Path $buildRoot 'build-local.log'
    & $pythonExe @arguments 2>&1 | Tee-Object -FilePath $log
    if ($LASTEXITCODE -ne 0) { throw "本地打包失败，日志：$log" }
    $builtExe = Join-Path $settings.BEAM_PYINSTALLER_DIST_DIR 'BeamChat.exe'
    $builtHash = (Get-FileHash -LiteralPath $builtExe -Algorithm SHA256).Hash
    foreach ($exe in @((Join-Path $PSScriptRoot 'dist\BeamChat.exe'), (Join-Path $buildRoot 'BeamChat.exe'))) {
        if ((Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash -ne $builtHash) {
            throw "构建产物复制校验失败：$exe"
        }
        $digest = (Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash.ToLowerInvariant()
        [IO.File]::WriteAllText("$exe.sha256", "$digest  BeamChat.exe", [Text.Encoding]::ASCII)
    }
    Write-Host "本地打包完成：$buildRoot\BeamChat.exe"
} finally {
    foreach ($name in $previous.Keys) {
        [Environment]::SetEnvironmentVariable($name, $previous[$name], 'Process')
    }
}