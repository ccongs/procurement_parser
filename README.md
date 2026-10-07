# procurementParser — 조달청 나라장터 입찰공고·사전규격 자동 수집

조달청 나라장터 **OpenAPI**(입찰공고정보서비스 + 사전규격정보서비스)를 주기적으로 호출해
**용역 입찰공고 전체와 사전규격을 자동 수집·중복제거·DB 누적**하고, **회사 조건(매칭 업종·IT 분류·SW 키워드)에 맞는 공고만 목록에 표시**한다.

- 스케줄러가 주기적으로 용역 입찰공고 전체 + 면허제한 정보를 받아 SQLite 에 누적 → 표시 규칙으로 목록 대상 판정.
- FastAPI 웹 화면에서 적재분 조회·필터·정렬·첨부 다운로드, 수집 설정·이력 확인.
- 제안요청서(RFP) 비동기 자동 분석(선택 기능, 결과 캐싱·새 창 보기) 및 검토항목 장바구니 → 엑셀 일괄 다운로드.

> 버전별 변경 이력은 [CHANGELOG.md](CHANGELOG.md) 참고.

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
  (수집 on/off `enabled`·`pre_spec_enabled`, 주기 `interval_minutes`(기본 60), 매칭 업종코드 `indstryty_cds`(기본 `1426,1468,1469,1470` — 목록 표시 판정 기준, 수집 조건 아님), 백필 등).
- **RFP 분석(선택 기능, 비동기)**: 목록의 분석 버튼 → 제안요청서 첨부 자동 탐색·다운로드 → 텍스트 추출(PDF/HWP/HWPX/DOC/DOCX) → LLM 분석. 분석은 **백그라운드(BackgroundTasks)로 비동기** 실행되며 **결과는 `analysis_result` 테이블에 영속화**된다(재분석은 같은 행 갱신, 서버 재시작으로 중단된 `analyzing` 행은 기동 시 자동 리셋). 목록 버튼은 상태별로 렌더된다 — **분석 → 분석중 → 완료 ▾**(드롭다운: 분석보기·재분석) / **재분석**(오류 시). 분석·재분석 클릭 시 **비용 확인 알럿**, 진행 중에는 **~5초 폴링**으로 상태 갱신, 완료 결과는 **새 창의 서버 렌더 보고서 페이지**(`/analysis/{type}/{id}`)로 표시한다. 프로바이더는 `ANALYSIS_PROVIDER`(claude/openai/gemini)로 교체. **`USE_ANALYSIS_PROVIDER` 는 기본 `false`** 라 분석 컬럼·버튼이 숨겨지며 `true` 일 때만 노출(AI 미사용 환경 대비). `.hwp`/`.hwpx`는 olefile 로 직접 파싱하고 `.doc`만 LibreOffice 변환을 쓴다.
- **목록 표시 규칙**: 수집은 업종 조건 없이 전체를 저장하고, 목록에는 ① 면허제한 코드가 매칭 업종코드와 일치 ② 업종 제한 없는 공고 중 공공조달분류가 IT 분류 목록과 일치 ③ 업종 제한 없는 공고 중 공고명이 SW 키워드와 일치(판별 제외어가 있으면 제외)하는 공고만 표시한다. ②·③은 매칭업종 칸에 **제한없음** 배지·근거 툴팁. 규칙은 `/config` **목록 표시 규칙** 카드에서 편집(저장 시 전체 재판정)하고, **규칙 테스트(새 창)** 에서 초안을 기간별로 미리 적용해 새로 표시·숨김 공고를 확인한 뒤 적용할 수 있다.
- **검토항목 장바구니**: 목록에서 행을 체크해 "담기" → 헤더 **"검토항목 장바구니"** 에 모아 보고 → 엑셀(xlsx) 일괄 다운로드. 장바구니에서 공고명 클릭 시 나라장터 상세(새 탭), 선택 항목 첨부 다운로드·선택 삭제, 항목별 목록 검색을 지원한다.
- **공고명 제외 키워드**: `/list`(공고명)·`/pre-spec`(품명/사업명) 검색필터에서 특정 단어가 포함된 항목을 제외한다. 입력창은 **칩(chip) 형태** — 단어를 입력하면 ✓/✕ 아이콘이 뜨고 ✓(또는 Enter)로 칩 추가, 칩 `×`로 제거하며 등록된 기본 목록은 select 로 선택한다. 목록 관리는 `/config` **"공고명 제외 키워드 목록"**(추가·삭제 + **기본 적용** 체크) — 기본 적용한 단어는 목록 화면 첫 진입에서 자동으로 칩에 채워지고, 화면에서 개별 해제할 수 있다.

## 화면 / 엔드포인트

| 경로 | 설명 |
|---|---|
| `GET /` | `/list` 로 리다이렉트 |
| `GET /list` | 입찰공고 목록(검색필터·정렬·페이지네이션·첨부 다운로드) |
| `GET /list/{bid_ntce_no}/files`·`/files.zip` | 공고 첨부 목록(JSON)·zip 다운로드(서버가 받아 묶음, SSRF 가드) |
| `GET /pre-spec` | 사전규격 목록(품명·기관·접수일·배정예산 범위 필터, 첨부 다운로드) |
| `GET /pre-spec/{bf_spec_rgst_no}/files`·`/files.zip` | 사전규격 첨부 |
| `GET /list/{bid_ntce_no}/files/single` · `/pre-spec/{bf_spec_rgst_no}/files/single` | 첨부 1건 원본 다운로드(장바구니 파일다운로드, 크기 상한) |
| `GET /config` · `POST /config` | 수집 설정·스케줄러 제어·실행 이력. 검색 기본값(입찰 배정예산 / 사전규격 배정예산액) 분리 설정 |
| `POST /config/keywords` | 공고명 제외 키워드 목록 관리(추가·삭제·기본 적용 저장) |
| `POST /config/rules` | 목록 표시 규칙(IT 분류·SW 키워드·판별 제외어) 추가·삭제·켜기/끄기 + 전체 재판정 |
| `GET /config/rules/test` · `POST /config/rules/test/apply` | 규칙 테스트(새 창): 초안 미리보기(DB 무변경)·적용 |
| `POST /config/scheduler/start`·`/stop`·`/config/resume` | 스케줄러 수동 시작/정지·중단 재개 |
| `GET /api-test` | 원시 API 호출·응답 확인용 화면 |
| `POST /api/analysis/{type}/{id}` (type=`bid`·`pre_spec`) | 제안요청서 자동 탐색 후 **비동기 분석 트리거**. `{status: analyzing}`(시작·이미 진행 중) 또는 `{status: need_upload}`(후보 없음) 반환 (선택 기능, `USE_ANALYSIS_PROVIDER=true` 시) |
| `POST /api/analysis/upload` | 파일 직접 업로드 후 비동기 분석 트리거(`{status: analyzing}`) |
| `GET /api/analysis/{type}/{id}/status` | 분석 상태 폴링(`none`/`analyzing`/`done`/`error`) |
| `GET /analysis/{type}/{id}` | **새 창 서버 렌더 분석 보고서**(영속화된 결과 표시) |
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
| `ANALYSIS_MAX_TOKENS` | 분석 출력 토큰 상한(기본 `16384`) |
| `USER_RFP_ANALYZER_LOG` | `true` 시 `rfp_analyzer_logs/` 에 분석 요청/응답 진단 md 기록(기본 `false`) |

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
- PK: 입찰=`bid_ntce_no`, 사전규격=`bf_spec_rgst_no`(자연키). 응답 원문은 입찰=`bid_notice_raw` 테이블, 사전규격=`raw_json` 컬럼으로 보존. 면허제한=`bid_license_limit`, 표시 규칙=`list_rule`.
- 나라장터 API 의 `numOfRows` 상한은 **999**(1000 이상은 오류 없이 10행만 응답). 입찰공고·면허 일괄 조회는 999행 고정이며, 받은 건수가 전체 건수보다 적으면 해당 수집을 partial 로 기록하고 다음 회차에 다시 받는다.

## 디렉터리 구조 (요약)

```
app/
  main.py            # FastAPI 앱·lifespan(스케줄러 자동시작)·전 화면·라우트
  api_client.py      # OpenAPI 호출(입찰/사전규격), 시크릿 로드
  db.py              # 엔진·세션·init_db(생성+멱등 마이그레이션+시드)
  models.py          # ORM(BidNotice / BidNoticeRaw / BidLicenseLimit / ListRule / PreSpec / AppConfig / CollectionRun / ExportCartItem / AnalysisResult 등)
  transform.py       # API 응답 → ORM 값 변환(입찰/사전규격)
  collector.py       # 입찰 수집기(전체 검색·면허제한 일괄·누락 보정·분류 연결·partial 판정). __main__=수동 백필
  classifier.py      # 목록 표시 판정(업종→분류→키워드)·규칙 로드·재분류
  rule_test.py       # 규칙 테스트 페이지의 초안 검증·미리보기·적용 계획
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
