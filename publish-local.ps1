#Requires -Version 7
param(
    [switch]$Plan,
    [switch]$PublishOnly,
    [int]$ImportWorkers = 4,
    [int]$BuildWorkers = 8,
    [int]$UploadWorkers = 16
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

function Get-PublisherPython {
    $venv = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $venv) {
        return $venv
    }
    foreach ($name in @('py', 'python')) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($command) {
            return $command.Source
        }
    }
    throw '需要 Python 3.10+：安装 py 启动器，或先 py -m venv .venv'
}

$python = Get-PublisherPython
Write-Host "Python: $python" -ForegroundColor Cyan
& $python -m pip install -r (Join-Path $PSScriptRoot 'requirements.txt')
if ($LASTEXITCODE) { exit $LASTEXITCODE }

function Invoke-Publisher {
    param([Parameter(Mandatory)][string[]]$PublisherArgs)
    Write-Host ("rizline_publisher " + ($PublisherArgs -join ' ')) -ForegroundColor Cyan
    & $python -X utf8 -m rizline_publisher @PublisherArgs
    if ($LASTEXITCODE) { exit $LASTEXITCODE }
}

if (-not $Plan -and -not $env:AWS_PROFILE -and (-not $env:AWS_ACCESS_KEY_ID -or -not $env:AWS_SECRET_ACCESS_KEY)) {
    throw '实际上传需要 AWS_ACCESS_KEY_ID 与 AWS_SECRET_ACCESS_KEY，或 AWS_PROFILE'
}

if (-not $PublishOnly) {
    Invoke-Publisher @('import', '--workers', "$ImportWorkers")
    Invoke-Publisher @('validate')
    Invoke-Publisher @('build', '--workers', "$BuildWorkers")
    Invoke-Publisher @('validate', '--release', '--workers', "$BuildWorkers")
}
$publish = @('publish', '--workers', "$UploadWorkers")
if (-not $Plan) {
    $publish += '--execute'
}
Invoke-Publisher $publish
