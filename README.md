# procurementParser — 조달청 나라장터 입찰공고·사전규격 자동 수집

조달청 나라장터 **OpenAPI**(입찰공고정보서비스 + 사전규격정보서비스)를 주기적으로 호출해
**회사 조건(업종 등)에 맞는 공고·사전규격을 자동 수집·중복제거·DB 누적**하고, 웹 화면에서 조회한다.

- 스케줄러가 주기적으로 API 호출 → 회사 조건 필터 → 중복 제거 후 SQLite 누적.
- FastAPI 웹 화면에서 적재분 조회·필터·정렬·첨부 다운로드, 수집 설정·이력 확인.
- 제안요청서(RFP) 자동 분석(선택 기능) 및 검토항목 장바구니 → 엑셀 일괄 다운로드.

## 기술 스택

| 영역 | 선택 |
|---|---|
| 언어 | Python 3.11+ |
| 웹/백엔드 | FastAPI |
| HTTP | httpx |
| 스케줄러 | APScheduler (`<4`) |
| 저장소/ORM | SQLite + SQLAlchemy (표준 타입만 → PostgreSQL 이전 용이) |
| 설정/시크릿 | python-dotenv (`.env`) |

## 동작 개요

- **수집 대상 2종**: 입찰공고 `…/ad/BidPublicInfoService`(용역 등) + 사전규격 `…/ao/HrcspSsstndrdInfoService`(SW사업대상 `swBizObjYn=Y`).
- **스케줄러는 web 프로세스 안에서 자동 시작**된다(FastAPI `lifespan`). 별도 수집 데몬/프로세스 불필요.
  - 기동 시 게이트(`enabled && !auto_halted` 또는 `pre_spec_enabled`) 충족 시 `start_scheduler(run_now=False)`.
  - 첫 tick 은 `now + interval`(즉시 아님), 증분 윈도우 + 겹침으로 누락/폭주 방지.
  - 일시 장애(특정 resultCode)는 재시도, 비재시도 에러는 자동 중단(`auto_halted`) 후 화면에서 재개.
- **수집 설정은 코드/ENV 가 아니라 DB(`app_config` 단일 행)** 에 저장되고 **`/config` 화면**에서 편집한다
  (수집 on/off `enabled`·`pre_spec_enabled`, 주기 `interval_minutes`(기본 60), 업종코드 `indstryty_cds`(기본 `1426,1468,1469,1470`), 백필 등).
- **RFP 분석(선택 기능)**: 목록의 "분석" 버튼 → 제안요청서 첨부 자동 탐색·다운로드 → 텍스트 추출(PDF/HWP/HWPX/DOC/DOCX) → LLM 분석 → 인라인 결과 패널. 프로바이더는 `ANALYSIS_PROVIDER`(claude/openai/gemini)로 교체. **`USE_ANALYSIS_PROVIDER` 는 기본 `false`** 라 분석 컬럼·버튼이 숨겨지며, 명시적으로 `true` 일 때만 노출(AI 미사용 환경 대비). `.hwp`/`.hwpx`는 olefile 로 직접 파싱하고 `.doc`만 LibreOffice 변환을 쓴다.
- **검토항목 장바구니**: 목록에서 행을 체크해 "담기" → 헤더 **"검토항목 장바구니"** 에 모아 보고 → 엑셀(xlsx) 일괄 다운로드.

## 화면 / 엔드포인트

| 경로 | 설명 |
|---|---|
| `GET /` | `/list` 로 리다이렉트 |
| `GET /list` | 입찰공고 목록(검색필터·정렬·페이지네이션·첨부 다운로드) |
| `GET /list/{bid_ntce_no}/files`·`/files.zip` | 공고 첨부 목록(JSON)·zip 다운로드(서버가 받아 묶음, SSRF 가드) |
| `GET /pre-spec` | 사전규격 목록(품명·기관·접수일·배정예산 범위 필터, 첨부 다운로드) |
| `GET /pre-spec/{bf_spec_rgst_no}/files`·`/files.zip` | 사전규격 첨부 |
| `GET /config` · `POST /config` | 수집 설정·스케줄러 제어·실행 이력. 검색 기본값(입찰 추정가격 / 사전규격 배정예산액) 분리 설정 |
| `POST /config/scheduler/start`·`/stop`·`/config/resume` | 스케줄러 수동 시작/정지·중단 재개 |
| `GET /api-test` | 원시 API 호출·응답 확인용 화면 |
| `POST /api/analysis/bid/{bid_ntce_no}` · `/pre-spec/{bf_spec_rgst_no}` | 제안요청서 자동 탐색·다운로드 후 RFP 분석(선택 기능, `USE_ANALYSIS_PROVIDER=true` 시) |
| `POST /api/analysis/upload` | 파일 직접 업로드 후 RFP 분석 |
| `GET·POST /api/export-cart` · `DELETE /api/export-cart/{id}`·`/all` | 검토항목 장바구니 조회·담기·삭제 |
| `GET /api/export-cart/download` | 검토항목 장바구니 엑셀(xlsx) 다운로드 |

## 환경변수 (`.env`)

| 키 | 설명 |
|---|---|
| `PROCUREMENT_SERVICE_KEY` | 공공데이터포털 서비스 키(**디코딩 키**). **시크릿 — 커밋 금지** |
| `PROCUREMENT_BASE_URL` | 입찰공고 베이스 URL(기본 `…/ad/BidPublicInfoService`) |
| `PROCUREMENT_PRESPEC_BASE_URL` | 사전규격 베이스 URL(기본 `…/ao/HrcspSsstndrdInfoService`). 서비스키는 동일 키 재사용 |
| `PROCUREMENT_RESPONSE_TYPE` | 응답 타입 `json`(기본) 또는 `xml` |
| `DATABASE_URL` | DB 경로(기본 `sqlite:///<프로젝트루트>/procurement.db`) |
| `LOG_LEVEL` | 로그 레벨(기본 `INFO`) |
| `TZ` | 시간대(`Asia/Seoul` 권장 — 수집 윈도우가 로컬 시각 `datetime.now()` 기준) |
| `ANALYSIS_PROVIDER` | RFP 분석 LLM 프로바이더 `claude`(기본)/`openai`/`gemini` |
| `USE_ANALYSIS_PROVIDER` | 분석 컬럼·버튼 노출 여부. **기본 `false`(숨김)**, `true` 일 때만 표시 |
| `CLAUDE_API_KEY` / `OPENAI_API_KEY` / `GEMINI_API_KEY` | 선택한 프로바이더 API 키(분석 사용 시). **시크릿 — 커밋 금지** |
| `ANALYSIS_MODEL` | 분석 모델 오버라이드(비우면 프로바이더 기본값) |

- 시크릿은 `.env` 에서만 로드하며 저장소에 커밋하지 않는다. 키 목록은 `.env.example` 참조.
- 로깅은 콘솔 + 회전 파일(`logs/app.log`). `serviceKey` 는 로그에서 마스킹된다.

## 로컬 실행

```bash
pip install -r requirements.txt
python -m app.db                 # 테이블 생성 + 멱등 마이그레이션 + 시드(app_config)
uvicorn app.main:app --reload    # http://127.0.0.1:8000
```

- `python -m app.db` 는 기존 DB에 누락 컬럼이 있으면 `ALTER TABLE` 로 자동 보강(멱등).
- RFP 분석(선택)을 쓰려면 `.env` 에 `USE_ANALYSIS_PROVIDER=true` + 프로바이더 키를 설정한다(기본 `false` 면 분석 컬럼·버튼이 숨겨짐). `.doc` 변환에는 LibreOffice(`soffice`)가 PATH 에 있어야 하며(`.hwp`/`.hwpx` 는 불필요), 분석 패키지(`anthropic`/`pypdf`/`pdfplumber`/`openpyxl`/`olefile` 등)는 `requirements.txt` 에 포함된다.

## 데이터 / 주의

- DB 는 **SQLite 파일**(`procurement.db`). SQLite 동시성: WAL + `busy_timeout` 적용(첫 기동에서 `*.db-wal`/`*.db-shm` 생성).
- **기동 = 수집 시작**: 앱이 뜨면 스케줄러 게이트 충족 시 수집이 시작된다(입찰·사전규격 기본 둘 다 ON). 원치 않으면 `/config` 에서 토글.
- PK: 입찰=`bid_ntce_no`, 사전규격=`bf_spec_rgst_no`(자연키). 응답 원문은 `raw_json` 으로 보존.

## 디렉터리 구조 (요약)

```
app/
  main.py            # FastAPI 앱·lifespan(스케줄러 자동시작)·전 화면·라우트
  api_client.py      # OpenAPI 호출(입찰/사전규격), 시크릿 로드
  db.py              # 엔진·세션·init_db(생성+멱등 마이그레이션+시드)
  models.py          # ORM(BidNotice / PreSpec / AppConfig / CollectionRun / ExportCartItem)
  transform.py       # API 응답 → ORM 값 변환(입찰/사전규격)
  collector.py       # 입찰 수집기(윈도우·재시도·partial 판정). __main__=수동 백필
  pre_spec_collector.py  # 사전규격 수집기. __main__=수동 백필
  repository.py      # 조회/upsert/설정/파일 목록 등 DB 접근
  scheduler.py       # APScheduler 잡(collect / collect_pre_spec)·게이트
  logging_config.py  # 콘솔+회전파일 로깅, serviceKey 마스킹
  analysis/          # RFP 분석(선택): 파일 파싱(pdf/hwp/docx)·LibreOffice(.doc)변환·프로바이더(claude/openai/gemini)·분석 서비스
  field_labels.py / industry_codes.py / region_codes.py  # 라벨·코드 매핑
tests/               # pytest
requirements.txt
.env.example
```

## 테스트

```bash
pytest -q
```

시간 의존 로직은 `now` 주입으로 결정적 테스트, 외부 다운로드는 monkeypatch.
