# my-credit

GCP 크레딧 잔액을 하루 2회 디스코드로 자동 보고한다.

```
Cloud Billing ──(BigQuery 내보내기)──> BigQuery 테이블
                                            │
       GitHub Actions cron (KST 09:00/21:00) ┘
                     │  잔액 · 어제 사용액 · 서비스 TOP5 · 소진 예상일
                     ▼
              Discord Webhook
```

## 왜 BigQuery를 거치나

콘솔 첫 화면의 `₩246 / ₩435,523 크레딧 사용됨` 은 **API로 조회할 수 없다.** Cloud Billing API에
크레딧 잔액 엔드포인트가 없다. 대신 빌링 데이터를 BigQuery로 내보내면 사용 라인마다 차감된
프로모션 크레딧 금액(`credits[].amount`)이 들어오므로

```
남은 크레딧 = TOTAL_CREDIT − SUM(차감된 프로모션 크레딧)
```

으로 같은 숫자를 재현할 수 있다. 이 리포트의 모든 금액은 "실제 차감된 크레딧" 기준이라
콘솔 표시와 동일한 축으로 읽으면 된다.

> **주의:** 내보내기는 켠 시점부터 데이터가 쌓이고 과거분은 소급되지 않는다.
> 첫 데이터 도착까지 최대 24시간 걸린다. 그동안은 "데이터 없음" 메시지가 전송된다.

---

## 설정

### 1. Cloud Billing → BigQuery 내보내기 켜기

1. BigQuery에서 데이터셋 생성 (예: `daengs` 프로젝트에 `billing_export`, 위치는 아무거나)
2. 콘솔 → **결제 → 결제 내보내기 → BigQuery 내보내기**
3. **표준 사용량 비용**(Standard usage cost) 항목에서 `수정` → 위 프로젝트/데이터셋 선택 → 저장

몇 시간~하루 뒤 `gcp_billing_export_v1_<결제계정ID>` 테이블이 생긴다.
전체 경로(`프로젝트.데이터셋.테이블`)를 복사해 둔다.

### 2. 디스코드 웹후크 발급

받을 채널 → **채널 편집 → 연동 → 웹후크 → 새 웹후크 → 웹후크 URL 복사**

### 3. 서비스 계정 만들기

```bash
gcloud config set project daengs

gcloud iam service-accounts create credit-reporter \
  --display-name="Discord credit reporter"

SA=credit-reporter@daengs.iam.gserviceaccount.com

# 쿼리 실행 권한
gcloud projects add-iam-policy-binding daengs \
  --member="serviceAccount:$SA" --role="roles/bigquery.jobUser"

# 빌링 데이터셋 읽기 권한 (데이터셋 단위로만 주면 충분)
bq add-iam-policy-binding \
  --member="serviceAccount:$SA" \
  --role="roles/bigquery.dataViewer" \
  daengs:billing_export

gcloud iam service-accounts keys create sa-key.json --iam-account="$SA"
```

`sa-key.json` 은 커밋하지 않는다 (`.gitignore` 에 이미 등록됨).

### 4. GitHub 설정

**Settings → Secrets and variables → Actions**

Secrets:

| 이름 | 값 |
|---|---|
| `GCP_SA_KEY` | `sa-key.json` 파일 내용 전체 |
| `DISCORD_WEBHOOK_URL` | 웹후크 URL |

Variables:

| 이름 | 값 |
|---|---|
| `BQ_BILLING_TABLE` | `daengs.billing_export.gcp_billing_export_v1_XXXXXX_XXXXXX_XXXXXX` |
| `TOTAL_CREDIT` | `435523` |
| `CREDIT_EXPIRY` | `2026-11-17` |
| `CURRENCY` | `KRW` |
| `PROJECT_LABEL` | `DAENGS` |
| `MENTION_THRESHOLDS` | `50,80,90` |

등록 후 **Actions → GCP credit report → Run workflow** 로 즉시 테스트할 수 있다.

---

## 동작 방식

- **스케줄**: `.github/workflows/credit-report.yml` 의 cron `0 0,12 * * *` = KST 09:00 / 21:00.
  횟수를 바꾸면 `MENTION_LOOKBACK_HOURS` 도 실행 간격(시간)에 맞춰 바꾼다.
- **임계치 멘션**: 매번 시끄럽지 않도록, 직전 실행 구간에서 **새로 넘어선** 임계치가 있을 때만
  `@everyone` 을 붙인다. 이미 90%를 넘은 상태로 계속 있으면 다시 멘션하지 않는다.
- **소진 예상일**: 최근 7일 평균 사용량 기준. 예상일이 크레딧 만료일보다 늦으면
  "만료일이 먼저 도래" 를 함께 표시한다.
- **TOP 5**: 어제 기준. 어제 사용 내역이 없으면 최근 7일 기준으로 자동 전환한다.

## 내보내기 상태 확인

```powershell
.\check_export.ps1
```

테이블 생성 여부 → 행 수 → 데이터 요약(사용 기간, 차감된 크레딧)을 차례로 확인한다.
종료 코드로도 구분된다: `0` 준비됨 / `2` 테이블 없음 / `3` 테이블은 있으나 비어 있음.

> `.ps1` 파일은 **UTF-8 BOM** 으로 저장해야 한다. Windows PowerShell 5.1 은 BOM 없는 스크립트를
> 시스템 ANSI(한국어 환경은 CP949)로 읽는데, CP949 는 더블바이트 코드페이지라 UTF-8 한글
> 바이트가 뒤따르는 따옴표를 삼켜 파싱이 깨진다.

## 로컬 테스트

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env   # 값 채우기
.\run_local.ps1
```

## 비용

BigQuery 저장 10GB/월, 쿼리 1TB/월까지 무료 구간이라 이 규모에서는 사실상 0원이다.
GitHub Actions 도 퍼블릭 리포는 무료, 프라이빗도 월 2000분 무료 안에서 충분하다.
