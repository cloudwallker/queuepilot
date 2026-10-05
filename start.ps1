[CmdletBinding()]
param(
    [switch]$Worker
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Import-QueuePilotEnvironment {
    param([string[]]$Lines)

    $lineNumber = 0
    foreach ($rawLine in $Lines) {
        $lineNumber++
        $line = $rawLine.Trim()
        if (-not $line -or $line.StartsWith('#')) {
            continue
        }
        if ($line -notmatch '^(?:export\s+)?(QUEUEPILOT_[A-Z0-9_]+)\s*=\s*(.*)$') {
            throw "Invalid .env assignment at line $lineNumber. Use QUEUEPILOT_NAME=value."
        }

        $key = $Matches[1]
        $value = $Matches[2].Trim()
        if ($value.StartsWith('"') -or $value.StartsWith("'")) {
            if ($value.Length -lt 2 -or $value[$value.Length - 1] -ne $value[0]) {
                throw "Unclosed .env quote at line $lineNumber."
            }
            $value = $value.Substring(1, $value.Length - 2)
        }
        else {
            $value = ($value -replace '\s+#.*$', '').Trim()
        }

        # Values remain literal: no dot-sourcing, expansion, or Invoke-Expression.
        if ($null -eq [Environment]::GetEnvironmentVariable($key, 'Process')) {
            [Environment]::SetEnvironmentVariable($key, $value, 'Process')
        }
    }
}

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw 'uv is required. Install it first, then run this script again.'
}

Push-Location $PSScriptRoot
try {
    $envFile = Join-Path $PSScriptRoot '.env'
    if (Test-Path -LiteralPath $envFile -PathType Leaf) {
        Import-QueuePilotEnvironment -Lines (Get-Content -LiteralPath $envFile -Encoding UTF8)
    }

    & uv sync --locked
    if ($LASTEXITCODE -ne 0) {
        throw "uv sync failed with exit code $LASTEXITCODE."
    }

    $python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw 'The project Python environment was not created by uv.'
    }
    $arguments = @('-m', 'queuepilot')
    if ($Worker) {
        $arguments += 'worker'
    }
    & $python @arguments
    $exitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}

exit $exitCode
