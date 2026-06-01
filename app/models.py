"""SQLAlchemy ORM 모델 — Phase 3.1.

조달청 나라장터 입찰공고 수집의 DB 스키마.
계획서 `documents/phase3-implementation-plan.md` §3, 사양서
`documents/phase2-collection-spec.md` §5를 기준으로 한다.

- PostgreSQL 이전을 대비해 SQLAlchemy ORM 표준 타입만 사용한다(SQLite 전용 기능 금지).
- 테이블 3종: bid_notice(입찰공고) / collection_run(실행 이력) / app_config(수집 설정).
- 수집/변환/스케줄러 로직은 이번 단계(3.1)에서 만들지 않는다.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    Numeric,
    String,
    Text,
)

from app.db import Base


class BidNotice(Base):
    """입찰공고 — 단일 테이블. PK = bid_ntce_no(최신 차수만 유지)."""

    __tablename__ = "bid_notice"

    # --- 식별 (PK = 공고번호 단일) ---
    bid_ntce_no = Column(String(40), primary_key=True)  # bidNtceNo
    bid_ntce_ord = Column(String(3))                    # bidNtceOrd (현재 보유 차수)
    unty_ntce_no = Column(String(40))                   # untyNtceNo

    # --- 분류·상태 ---
    bid_ntce_nm = Column(String(1000))                  # bidNtceNm
    ntce_kind_nm = Column(String(100))                  # ntceKindNm
    re_ntce_yn = Column(String(1))                      # reNtceYn
    srvce_div_nm = Column(String(30))                   # srvceDivNm
    info_biz_yn = Column(String(1))                     # infoBizYn
    intrbid_yn = Column(String(1))                      # intrbidYn (국제입찰여부, 검증용)
    bid_methd_nm = Column(String(500))                  # bidMethdNm
    cntrct_cncls_mthd_nm = Column(String(500))          # cntrctCnclsMthdNm
    sucsfbid_mthd_nm = Column(String(700))              # sucsfbidMthdNm
    indstryty_lmt_yn = Column(String(1))                # indstrytyLmtYn
    chg_ntce_rsn = Column(Text)                         # chgNtceRsn (변경공고사유)

    # --- 기관·담당자 ---
    ntce_instt_cd = Column(String(7))                   # ntceInsttCd
    ntce_instt_nm = Column(String(400))                 # ntceInsttNm
    dminstt_cd = Column(String(7), index=True)          # dminsttCd (기관별 조회)
    dminstt_nm = Column(String(400))                    # dminsttNm
    ntce_instt_ofcl_nm = Column(String(35))             # ntceInsttOfclNm
    ntce_instt_ofcl_tel_no = Column(String(25))         # ntceInsttOfclTelNo
    ntce_instt_ofcl_email = Column(String(100))         # ntceInsttOfclEmailAdrs

    # --- 일정 (문자열 "YYYY-MM-DD HH:MM:SS" → DateTime) ---
    bid_ntce_dt = Column(DateTime, index=True)          # bidNtceDt (기간 조회)
    rgst_dt = Column(DateTime)                          # rgstDt
    bid_qlfct_rgst_dt = Column(DateTime)                # bidQlfctRgstDt
    bid_begin_dt = Column(DateTime)                     # bidBeginDt
    bid_clse_dt = Column(DateTime)                      # bidClseDt
    openg_dt = Column(DateTime, index=True)             # opengDt (개찰 임박)
    chg_dt = Column(DateTime)                           # chgDt

    # --- 금액·평가 (문자열 → Numeric) ---
    presmpt_prce = Column(Numeric(20, 0))               # presmptPrce
    asign_bdgt_amt = Column(Numeric(20, 0))             # asignBdgtAmt
    vat = Column(Numeric(20, 0))                        # VAT
    sucsfbid_lwlt_rate = Column(Numeric(6, 3))          # sucsfbidLwltRate
    tech_ablt_evl_rt = Column(Numeric(6, 3))            # techAbltEvlRt
    bid_prce_evl_rt = Column(Numeric(6, 3))             # bidPrceEvlRt

    # --- 분류·링크 ---
    pub_prcrmnt_lrgclsfc_nm = Column(String(100))       # pubPrcrmntLrgclsfcNm
    pub_prcrmnt_midclsfc_nm = Column(String(100))       # pubPrcrmntMidclsfcNm
    pub_prcrmnt_clsfc_no = Column(String(10))           # pubPrcrmntClsfcNo
    bid_ntce_url = Column(String(500))                  # bidNtceUrl
    bid_ntce_dtl_url = Column(String(512))              # bidNtceDtlUrl
    std_ntce_doc_url = Column(String(800))              # stdNtceDocUrl

    # --- 첨부 규격서 (1~10 고정 컬럼) ---
    ntce_spec_doc_url1 = Column(String(800))            # ntceSpecDocUrl1
    ntce_spec_doc_url2 = Column(String(800))            # ntceSpecDocUrl2
    ntce_spec_doc_url3 = Column(String(800))            # ntceSpecDocUrl3
    ntce_spec_doc_url4 = Column(String(800))            # ntceSpecDocUrl4
    ntce_spec_doc_url5 = Column(String(800))            # ntceSpecDocUrl5
    ntce_spec_doc_url6 = Column(String(800))            # ntceSpecDocUrl6
    ntce_spec_doc_url7 = Column(String(800))            # ntceSpecDocUrl7
    ntce_spec_doc_url8 = Column(String(800))            # ntceSpecDocUrl8
    ntce_spec_doc_url9 = Column(String(800))            # ntceSpecDocUrl9
    ntce_spec_doc_url10 = Column(String(800))           # ntceSpecDocUrl10
    ntce_spec_file_nm1 = Column(String(400))            # ntceSpecFileNm1
    ntce_spec_file_nm2 = Column(String(400))            # ntceSpecFileNm2
    ntce_spec_file_nm3 = Column(String(400))            # ntceSpecFileNm3
    ntce_spec_file_nm4 = Column(String(400))            # ntceSpecFileNm4
    ntce_spec_file_nm5 = Column(String(400))            # ntceSpecFileNm5
    ntce_spec_file_nm6 = Column(String(400))            # ntceSpecFileNm6
    ntce_spec_file_nm7 = Column(String(400))            # ntceSpecFileNm7
    ntce_spec_file_nm8 = Column(String(400))            # ntceSpecFileNm8
    ntce_spec_file_nm9 = Column(String(400))            # ntceSpecFileNm9
    ntce_spec_file_nm10 = Column(String(400))           # ntceSpecFileNm10

    # --- 구매대상물품목록 (가변 0..n → 원문 보존) ---
    purchs_obj_prdct_list = Column(Text)                # purchsObjPrdctList

    # --- 수집 메타데이터 ---
    matched_indstryty_cds = Column(String(50))          # 이 공고를 잡아낸 업종코드(예: "1468,1470")
    raw_json = Column(Text)                             # 응답 item 원문 전체(재파싱 대비)
    collected_at = Column(DateTime, nullable=False)     # 최초 수집 시각
    updated_at = Column(DateTime, nullable=False)       # 최종 갱신 시각

    def __repr__(self) -> str:  # pragma: no cover - 디버그용
        return f"<BidNotice {self.bid_ntce_no!r} ord={self.bid_ntce_ord!r}>"


class CollectionRun(Base):
    """스케줄 실행 이력. 실패 점검은 status in (failed, partial)로 조회."""

    __tablename__ = "collection_run"

    id = Column(Integer, primary_key=True, autoincrement=True)
    trigger = Column(String(20))            # scheduled / manual / backfill
    run_started_at = Column(DateTime)       # 실행 시작
    run_finished_at = Column(DateTime)      # 실행 종료(실패 포함)
    window_bgn_dt = Column(DateTime)        # 조회 윈도우 시작
    window_end_dt = Column(DateTime)        # 조회 윈도우 종료
    status = Column(String(12))             # running / success / partial / failed
    total_fetched = Column(Integer)         # 중복 제거 후 건수
    total_new = Column(Integer)             # 신규 insert
    total_updated = Column(Integer)         # upsert 갱신
    retry_count = Column(Integer)           # 누적 재시도 횟수
    error_code = Column(String(2))          # 마지막 에러 resultCode
    error_msg = Column(Text)                # 실패 메시지
    detail_json = Column(Text)              # 업종코드별 결과(페이지수·resultCode·건수)

    def __repr__(self) -> str:  # pragma: no cover - 디버그용
        return f"<CollectionRun id={self.id} status={self.status!r}>"


class AppConfig(Base):
    """수집 설정 — 단일 행(id=1). /config 화면에서 편집(3.5)."""

    __tablename__ = "app_config"

    id = Column(Integer, primary_key=True)                      # 단일 행 = 1
    enabled = Column(Boolean, nullable=False, default=True)     # 스케줄 on/off(사용자 의도)
    auto_halted = Column(Boolean, nullable=False, default=False)  # 비재시도 에러로 자동 중단
    halt_code = Column(String(2))                              # 중단 유발 resultCode
    halt_reason = Column(Text)                                 # 중단 사유(사람 확인용)
    interval_minutes = Column(Integer, nullable=False, default=60)        # 수집 주기
    window_overlap_minutes = Column(Integer, nullable=False, default=90)  # 윈도우 겹침
    backfill_days = Column(Integer, nullable=False, default=30)           # 백필 기간(최대 1개월)
    num_of_rows = Column(Integer, nullable=False, default=20)             # 페이지 크기
    max_retries = Column(Integer, nullable=False, default=2)              # 일시 장애 재시도 한도
    inqry_div = Column(String(1), nullable=False, default="1")            # 조회구분
    intrntnl_div_cd = Column(String(1), nullable=False, default="1")      # 국내(1)/국제(2)/전체(빈값)
    indstryty_cds = Column(String(100), nullable=False, default="1426,1468,1469,1470")  # 업종코드 CSV
    prtcpt_lmt_rgn_cd = Column(String(2))                     # 참가제한지역(미사용=null)
    presmpt_prce_bgn = Column(String(25))                    # 추정가격 하한(미사용)
    presmpt_prce_end = Column(String(25))                    # 추정가격 상한(미사용)
    last_success_dt = Column(DateTime)                       # 마지막 성공 윈도우 종료 시각
    updated_at = Column(DateTime)                            # 설정 변경 시각

    def __repr__(self) -> str:  # pragma: no cover - 디버그용
        return f"<AppConfig id={self.id} enabled={self.enabled} auto_halted={self.auto_halted}>"
