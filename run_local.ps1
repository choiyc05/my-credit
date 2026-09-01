# .env 를 읽어 환경변수로 올린 뒤 report.py 를 실행한다.
if (-not (Test-Path .env)) {
    Write-Error ".env 파일이 없습니다. .env.example 을 복사해서 값을 채우세요."
    exit 1
}

Get-Content .env | ForEach-Object {
    $line = $_.Trim()
    if ($line -eq "" -or $line.StartsWith("#")) { return }
    $idx = $line.IndexOf("=")
    if ($idx -lt 1) { return }
    $name = $line.Substring(0, $idx).Trim()
    $value = $line.Substring($idx + 1).Trim()
    Set-Item -Path "env:$name" -Value $value
}

python report.py
