$key = $env:AZURE_SEARCH_API_KEY
if ([string]::IsNullOrWhiteSpace($key)) {
    throw "Set AZURE_SEARCH_API_KEY before running this script."
}
$svc = "srch-dmp37qvympsea"
$base = "https://" + $svc + ".search.windows.net"
$idx = "ragindex-dmp37qvympsea"

$body = @{
    search = "*"
    filter = "separation_status eq '2'"
    select = "emp_name,assessment_year,separation_status"
    top = 5
    count = $true
} | ConvertTo-Json

$resp = Invoke-RestMethod ($base + "/indexes/" + $idx + "/docs/search?api-version=2023-11-01") `
    -Method Post `
    -Headers @{"api-key" = $key; "Content-Type" = "application/json"} `
    -Body $body

Write-Host "Total sep=2 docs in index: $($resp.'@odata.count')"
$resp.value | Select-Object emp_name, assessment_year, separation_status | Format-Table -AutoSize

# Also break down by year
Write-Host "`n--- Count by assessment_year ---"
foreach ($yr in @("2024","2025","2026")) {
    $b2 = @{
        search = "*"
        filter = "separation_status eq '2' and assessment_year eq '" + $yr + "'"
        select = "emp_name"
        top = 0
        count = $true
    } | ConvertTo-Json
    $r2 = Invoke-RestMethod ($base + "/indexes/" + $idx + "/docs/search?api-version=2023-11-01") `
        -Method Post `
        -Headers @{"api-key" = $key; "Content-Type" = "application/json"} `
        -Body $b2
    Write-Host "$yr : $($r2.'@odata.count') docs"
}
