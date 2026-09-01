# 결제 내보내기 테이블이 생겼는지 / 데이터가 들어왔는지 한 번에 확인한다.
#
#   .\check_export.ps1
#   .\check_export.ps1 -Table other.dataset.table
#
# 테이블 경로는 -Table, 환경변수 BQ_BILLING_TABLE, .env 파일 순으로 찾는다.

param([string]$Table = $env:BQ_BILLING_TABLE)

$ErrorActionPreference = "Stop"

# Git Bash 등에서 파이프로 받을 때 한글이 깨지지 않도록 출력 인코딩을 고정한다.
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

if (-not $Table -and (Test-Path ".env")) {
    Get-Content .env | ForEach-Object {
        if ($_ -match '^\s*BQ_BILLING_TABLE\s*=\s*(.+?)\s*$') { $Table = $Matches[1] }
    }
}
if (-not $Table) {
    Write-Error "테이블 경로를 찾을 수 없습니다. -Table 로 넘기거나 .env 에 BQ_BILLING_TABLE 을 넣으세요."
    exit 1
}

$parts = $Table.Split(".")
if ($parts.Count -ne 3) {
    Write-Error "형식이 올바르지 않습니다: $Table (project.dataset.table)"
    exit 1
}
$project, $dataset, $tableId = $parts

Write-Host "대상: $Table`n" -ForegroundColor Cyan

# 1) 테이블 존재 여부
$listed = (bq ls --format=prettyjson "${project}:${dataset}") -join "`n"
if (-not $listed.Trim()) { $listed = "[]" }
$found = ($listed | ConvertFrom-Json) | Where-Object { $_.tableReference.tableId -eq $tableId }

if (-not $found) {
    Write-Host "[X] 테이블 없음" -ForegroundColor Red
    Write-Host "    결제 내보내기를 켠 직후라면 생성까지 몇 시간 걸립니다."
    Write-Host "    설정 확인: 결제 -> 결제 내보내기 -> BigQuery 내보내기 -> 표준 사용량 비용"
    exit 2
}

# 2) 행 수
$info = ((bq show --format=prettyjson "${project}:${dataset}.${tableId}") -join "`n") | ConvertFrom-Json
$rows = [int64]$info.numRows
$created = [DateTimeOffset]::FromUnixTimeMilliseconds([int64]$info.creationTime).ToLocalTime()

Write-Host "[O] 테이블 존재" -ForegroundColor Green
Write-Host ("    생성  : {0:yyyy-MM-dd HH:mm:ss}" -f $created.DateTime)
Write-Host "    행 수 : $rows"

if ($rows -eq 0) {
    Write-Host "`n[대기중] 테이블은 있지만 아직 비어 있습니다." -ForegroundColor Yellow
    Write-Host "         첫 배치까지 최대 24시간. 이 상태에서는 디스코드로 안내 카드만 갑니다."
    exit 3
}

# 3) 데이터 요약
$sql = @"
SELECT
  COUNT(*) AS rows_total,
  FORMAT_TIMESTAMP('%Y-%m-%d %H:%M', MIN(usage_start_time), 'Asia/Seoul') AS first_usage,
  FORMAT_TIMESTAMP('%Y-%m-%d %H:%M', MAX(usage_start_time), 'Asia/Seoul') AS last_usage,
  FORMAT_TIMESTAMP('%Y-%m-%d %H:%M', MAX(export_time), 'Asia/Seoul') AS last_export,
  ROUND(-COALESCE(SUM((SELECT COALESCE(SUM(c.amount),0) FROM UNNEST(credits) c
                       WHERE c.type = 'PROMOTION')), 0)) AS promo_used
FROM ``$Table``
"@

# bq 는 파이프로 넘기면 PowerShell 이 BOM 을 붙여 깨진다. 임시 파일 + cmd 리다이렉션으로 넘긴다.
$tmp = Join-Path $env:TEMP "check_export_$PID.sql"
[System.IO.File]::WriteAllText($tmp, $sql, (New-Object System.Text.UTF8Encoding($false)))
try {
    $out = (cmd /c "bq query --use_legacy_sql=false --format=prettyjson < `"$tmp`"") -join "`n"
} finally {
    Remove-Item $tmp -ErrorAction SilentlyContinue
}

$r = ($out | ConvertFrom-Json)[0]
Write-Host "`n[준비됨] 데이터가 들어왔습니다." -ForegroundColor Green
Write-Host "         사용 기간   : $($r.first_usage) ~ $($r.last_usage)"
Write-Host "         마지막 export: $($r.last_export)"
Write-Host "         차감된 크레딧: $([int64]$r.promo_used)"
Write-Host "`n리포트를 지금 보내려면: gh workflow run credit-report.yml --repo choiyc05/my-credit"
