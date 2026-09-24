<#
Run DAX queries (and optionally a TMSL full refresh) against the Analysis Services
instance that Power BI Desktop starts for an open file. Windows PowerShell 5.1.

  .\run_dax.ps1 -Port 51234 -QueriesFile q.json -OutFile out.json [-Refresh]

q.json: [{"name": "...", "dax": "EVALUATE ..."}]
#>
param(
    [Parameter(Mandatory = $true)][int]$Port,
    [Parameter(Mandatory = $true)][string]$QueriesFile,
    [Parameter(Mandatory = $true)][string]$OutFile,
    [switch]$Refresh
)
$ErrorActionPreference = "Stop"
$pbi = (Get-AppxPackage -Name "Microsoft.MicrosoftPowerBIDesktop").InstallLocation
if (-not $pbi) { $pbi = "C:\Program Files\Microsoft Power BI Desktop" }
$bin = Join-Path $pbi "bin"
[System.AppDomain]::CurrentDomain.add_AssemblyResolve({
    param($s, $e)
    $candidate = Join-Path $bin (($e.Name -split ",")[0] + ".dll")
    if (Test-Path $candidate) { return [System.Reflection.Assembly]::LoadFrom($candidate) }
    return $null
}.GetNewClosure())
Add-Type -Path (Join-Path $bin "Microsoft.PowerBI.AdomdClient.dll")

# Power BI Desktop loads the model after the engine starts: wait until the database has tables.
$db = $null; $tables = 0; $deadline = (Get-Date).AddSeconds(300)
while ((Get-Date) -lt $deadline) {
    try {
        $conn = New-Object Microsoft.AnalysisServices.AdomdClient.AdomdConnection("Data Source=localhost:$Port")
        $conn.Open()
        $cmd = $conn.CreateCommand()
        $cmd.CommandText = "SELECT [CATALOG_NAME] FROM `$SYSTEM.DBSCHEMA_CATALOGS"
        $r = $cmd.ExecuteReader(); if ($r.Read()) { $db = $r.GetValue(0) }; $r.Close()
        $cmd.CommandText = "SELECT [Name] FROM `$SYSTEM.TMSCHEMA_TABLES"
        $r = $cmd.ExecuteReader(); $tables = 0; while ($r.Read()) { $tables++ }; $r.Close()
        if ($db -and $tables -gt 0) { break }
        $conn.Close()
    } catch { }
    Start-Sleep -Seconds 3
}
if (-not ($db -and $tables -gt 0)) { throw "Model did not load within 300 s (database=$db, tables=$tables)" }

$result = [ordered]@{ database = $db; tables = $tables; refresh = $null; results = [ordered]@{} }
if ($Refresh) {
    try {
        $cmd = $conn.CreateCommand()
        $cmd.CommandText = '{"refresh": {"type": "full", "objects": [{"database": "' + $db + '"}]}}'
        [void]$cmd.ExecuteNonQuery()
        $result.refresh = "ok"
    } catch { $result.refresh = "error: " + $_.Exception.Message }
}
foreach ($q in (Get-Content $QueriesFile -Raw | ConvertFrom-Json)) {
    try {
        $cmd = $conn.CreateCommand(); $cmd.CommandText = $q.dax
        $reader = $cmd.ExecuteReader()
        $rows = New-Object System.Collections.ArrayList
        while ($reader.Read()) {
            $row = [ordered]@{}
            for ($i = 0; $i -lt $reader.FieldCount; $i++) {
                $v = $reader.GetValue($i); if ($v -is [System.DBNull]) { $v = $null }
                $row[$reader.GetName($i)] = $v
            }
            [void]$rows.Add($row)
        }
        $reader.Close()
        $result.results[$q.name] = @{ rows = $rows; error = $null }
    } catch { $result.results[$q.name] = @{ rows = @(); error = $_.Exception.Message } }
}
$conn.Close()
$result | ConvertTo-Json -Depth 6 | Set-Content -Path $OutFile -Encoding UTF8
