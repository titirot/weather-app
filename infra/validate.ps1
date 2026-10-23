[CmdletBinding()]
param(
    [string]$BicepCommand = 'bicep',
    [string]$PythonCommand = (Join-Path $PSScriptRoot '..\.venv\Scripts\python.exe')
)

$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$templateOutput = Join-Path ([IO.Path]::GetTempPath()) ("weather-template-{0}.json" -f [guid]::NewGuid())
$parameterOutput = Join-Path ([IO.Path]::GetTempPath()) ("weather-parameters-{0}.json" -f [guid]::NewGuid())
$bootstrapOutput = Join-Path ([IO.Path]::GetTempPath()) ("weather-network-{0}.json" -f [guid]::NewGuid())
$bootstrapAppOutput = Join-Path ([IO.Path]::GetTempPath()) ("weather-bootstrap-app-{0}.json" -f [guid]::NewGuid())
$validationValues = @{
    AZURE_TENANT_ID = '11111111-1111-1111-1111-111111111111'
    ENTRA_CLIENT_ID = '22222222-2222-2222-2222-222222222222'
    ENTRA_CLIENT_SECRET = 'validation-placeholder-not-a-secret'
    OPENWEATHER_API_KEY = 'validation-placeholder-not-an-api-key'
    DATABASE_ADMIN_GROUP_OBJECT_ID = '33333333-3333-3333-3333-333333333333'
    DATABASE_ADMIN_GROUP_NAME = 'weather-database-admins'
    ALERT_EMAIL = 'oncall@example.invalid'
    COMPILED_INFRA_TEMPLATE = $templateOutput
    COMPILED_NETWORK_BOOTSTRAP = $bootstrapOutput
    COMPILED_BOOTSTRAP_APP = $bootstrapAppOutput
}
$previousValues = @{}

try {
    & $BicepCommand build (Join-Path $PSScriptRoot 'main.bicep') --outfile $templateOutput
    if ($LASTEXITCODE -ne 0) { throw 'Bicep template compilation failed.' }

    & $BicepCommand build (Join-Path $PSScriptRoot 'bootstrap-network.bicep') --outfile $bootstrapOutput
    if ($LASTEXITCODE -ne 0) { throw 'Network bootstrap compilation failed.' }

    & $BicepCommand build (Join-Path $PSScriptRoot 'bootstrap-app.bicep') --outfile $bootstrapAppOutput
    if ($LASTEXITCODE -ne 0) { throw 'Bootstrap app compilation failed.' }

    # Compile the parameter example only with dummy values, never real credentials.
    foreach ($name in $validationValues.Keys) {
        $previousValues[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
        [Environment]::SetEnvironmentVariable($name, $validationValues[$name], 'Process')
    }
    & $BicepCommand build-params (Join-Path $PSScriptRoot 'main.bicepparam') --outfile $parameterOutput
    if ($LASTEXITCODE -ne 0) { throw 'Bicep parameter compilation failed.' }

    Push-Location $root
    try {
        & $PythonCommand -m unittest discover -s tests -p test_infrastructure.py -v
        if ($LASTEXITCODE -ne 0) { throw 'Infrastructure contract tests failed.' }
    } finally {
        Pop-Location
    }
    Write-Host 'Local infrastructure validation passed. No Azure deployment was performed.'
} finally {
    foreach ($name in $previousValues.Keys) {
        [Environment]::SetEnvironmentVariable($name, $previousValues[$name], 'Process')
    }
    foreach ($path in @($templateOutput, $parameterOutput, $bootstrapOutput, $bootstrapAppOutput)) {
        if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path }
    }
}
