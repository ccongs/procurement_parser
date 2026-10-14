"""SQLAlchemy ORM 모델 — Phase 3.1.

조달청 나라장터 입찰공고 수집의 DB 스키마.
계획서 `documents/phase3-implementation-plan.md` §3, 사양서
`documents/phase2-collection-spec.md` §5를 기준으로 한다.

- PostgreSQL 이전을 대비해 SQLAlchemy ORM 표준 타입만 사용한다(SQLite 전용 기능 금지).
- 테이블 4종: bid_notice(입찰공고) / collection_run(실행 이력) / app_config(수집 설정)
  / pre_spec(사전규격, Phase 5.1 추가).
- 수집/변환/스케줄러 로직은 이번 단계(3.1)에서 만들지 않는다.
- pre_spec 의 변환/수집/화면 로직은 Phase 5.2 이후에서 만든다(5.1은 모델/타입만).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    true,
)

from app.db import Base


def _utcnow() -> datetime:
    """DB 저장용 naive UTC 시각."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class BidNotice(Base):
    """입찰공고 — 단일 테이블. PK = bid_ntce_no(최신 차수만 유지)."""

    __tablename__ = "bid_notice"

    __table_args__ = (
        Index("ix_bid_notice_visible_ntce_dt", "list_visible", "bid_ntce_dt"),
        Index("ix_bid_notice_visible_openg_dt", "list_visible", "openg_dt"),
        Index("ix_bid_notice_indstryty_lmt_yn", "indstryty_lmt_yn"),
    )

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
    pub_prcrmnt_clsfc_nm = Column(String(100))          # pubPrcrmntClsfcNm
    bef_bid_bbanc_no = Column(String(40))               # befBidBbancNo (이전 공고번호)
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
    list_visible = Column(Boolean, nullable=False, default=True, server_default=true())
    match_basis = Column(String(10))                    # 표시 근거: 업종/분류/키워드
    match_detail = Column(String(200))                  # 분류명 또는 일치 키워드
    raw_json = Column(Text)                             # 호환용(원문은 bid_notice_raw 에 저장)
    collected_at = Column(DateTime, nullable=False)     # 최초 수집 시각
    updated_at = Column(DateTime, nullable=False)       # 최종 갱신 시각

    def __repr__(self) -> str:  # pragma: no cover - 디버그용
        return f"<BidNotice {self.bid_ntce_no!r} ord={self.bid_ntce_ord!r}>"


class BidNoticeRaw(Base):
    """입찰공고 원문 — 공고당 1행, 저장 순서에 의존하지 않도록 FK 없음."""

    __tablename__ = "bid_notice_raw"

    bid_ntce_no = Column(String(40), primary_key=True)
    raw_json = Column(Text)
    updated_at = Column(DateTime, nullable=False)


class BidLicenseLimit(Base):
    """면허제한 — op15 응답 1행당 1행(수집 로직은 Phase 13.3)."""

    __tablename__ = "bid_license_limit"

    bid_ntce_no = Column(String(40), primary_key=True)
    bid_ntce_ord = Column(String(3), primary_key=True)
    lmt_grp_no = Column(String(10), primary_key=True)
    lmt_sno = Column(String(10), primary_key=True)
    lcns_cd = Column(String(10), index=True)             # 면허제한명 명/코드 중 코드
    lcns_nm = Column(String(200))
    permsn_cds = Column(Text)                           # 허용업종 코드 CSV
    rgst_dt = Column(DateTime)
    collected_at = Column(DateTime, nullable=False)
    superseded_at = Column(DateTime, nullable=True)     # 대체된 면허는 보존하되 분류에서 제외


class LicenseVerification(Base):
    """공고·정규화 차수별 전체 면허 조회의 확인 대기·최종 적용 시각."""

    __tablename__ = "license_verification"

    bid_ntce_no = Column(String(40), primary_key=True)
    bid_ntce_ord = Column(String(3), primary_key=True)
    pending_since = Column(DateTime, nullable=True)
    attempts = Column(Integer, nullable=False, default=0, server_default="0")
    last_attempt_at = Column(DateTime, nullable=True)
    last_error = Column(String(200), nullable=True)
    verified_at = Column(DateTime, nullable=True)
    gave_up_at = Column(DateTime, nullable=True)


class ListRule(Base):
    """목록 표시 규칙 — 기본값 시드·편집은 Phase 13.2·13.4."""

    __tablename__ = "list_rule"

    id = Column(Integer, primary_key=True, autoincrement=True)
    rule_type = Column(String(20), nullable=False)       # clsfc/keyword/negative
    value = Column(String(100), nullable=False)
    enabled = Column(Boolean, nullable=False, default=True, server_default=true())
    created_at = Column(DateTime, nullable=False, default=_utcnow)

    __table_args__ = (
        UniqueConstraint("rule_type", "value", name="uq_list_rule_type_value"),
    )


class SchemaMigration(Base):
    """데이터 보정 완료 기록 — 중단 후 재시도·완료 후 재실행 방지용."""

    __tablename__ = "schema_migration"

    name = Column(String(100), primary_key=True)
    applied_at = Column(DateTime, nullable=False, default=_utcnow)


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
    detail_json = Column(Text)              # 호출 종류별 결과(페이지수·resultCode·건수)
    source = Column(String(10), nullable=True, default="bid")  # 수집원: bid|pre_spec (Phase 5.4)

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
    presmpt_prce_bgn = Column(String(25))                    # 입찰공고 배정예산 기본 하한
    presmpt_prce_end = Column(String(25))                    # 입찰공고 배정예산 기본 상한
    last_success_dt = Column(DateTime)                       # 마지막 성공 윈도우 종료 시각
    updated_at = Column(DateTime)                            # 설정 변경 시각
    # --- 사전규격 잡(Phase 5.4) — 입찰과 독립 토글·윈도우 기준 ---
    pre_spec_enabled = Column(Boolean, nullable=False, default=True)  # 사전규격 잡 독립 on/off
    pre_spec_last_success_dt = Column(DateTime)              # 사전규격 윈도우 산정 기준(마지막 성공 종료 시각)
    # --- 사전규격 검색 기본값(Phase 4.9-R2-D) — /pre-spec 배정예산액 기본 범위 ---
    pre_spec_amt_bgn = Column(String(25))                    # 배정예산액 기본 하한(미사용=null)
    pre_spec_amt_end = Column(String(25))                    # 배정예산액 기본 상한(미사용=null)

    def __repr__(self) -> str:  # pragma: no cover - 디버그용
        return f"<AppConfig id={self.id} enabled={self.enabled} auto_halted={self.auto_halted}>"


class PreSpec(Base):
    """사전규격(용역) — 단일 테이블. PK = bf_spec_rgst_no(자연키, 차수 없음).

    op15 getPublicPrcureThngInfoServcPPSSrch 응답을 저장한다(Phase 5.1).
    변환 규칙(날짜 2형식·금액 콤마·빈값 NULL)·수집/화면은 Phase 5.2 이후.
    """

    __tablename__ = "pre_spec"

    # --- 식별 (PK = 사전규격등록번호 단일) ---
    # ⚠️ 명세 크기 10이나 실데이터가 13자 영숫자(예: R26BD00222439, 5.0 실호출 확인) → String(30).
    bf_spec_rgst_no = Column(String(30), primary_key=True)  # bfSpecRgstNo

    # --- 분류·기관 ---
    bsns_div_nm = Column(String(20))                    # bsnsDivNm (업무구분명=용역)
    ref_no = Column(String(105))                        # refNo (참조번호)
    prdct_clsfc_no_nm = Column(String(200))             # prdctClsfcNoNm (품명/사업명)
    order_instt_nm = Column(String(200))                # orderInsttNm (발주기관명)
    rl_dminstt_nm = Column(String(200))                 # rlDminsttNm (실수요기관명)

    # --- 금액 ---
    asign_bdgt_amt = Column(Numeric(20, 0))             # asignBdgtAmt (배정예산액, 원)

    # --- 일정 (문자열 → DateTime) ---
    rcpt_dt = Column(DateTime, index=True)              # rcptDt (접수일시, 기간 조회)
    opnin_rgst_clse_dt = Column(DateTime, index=True)   # opninRgstClseDt (의견등록마감, 임박 조회)

    # --- 담당자 ---
    ofcl_nm = Column(String(35))                        # ofclNm (담당자명)
    ofcl_tel_no = Column(String(25))                    # ofclTelNo (담당자전화번호)

    # --- 분류 필터 ---
    sw_biz_obj_yn = Column(String(1), index=True)       # swBizObjYn (SW사업대상여부, 필터)

    # --- 납품 ---
    dlvr_tmlmt_dt = Column(DateTime)                    # dlvrTmlmtDt (납품기한일시)
    dlvr_daynum = Column(Integer)                       # dlvrDaynum (납품일수)

    # --- 첨부 규격서 (1~5 고정 컬럼) ---
    spec_doc_file_url1 = Column(String(255))            # specDocFileUrl1
    spec_doc_file_url2 = Column(String(255))            # specDocFileUrl2
    spec_doc_file_url3 = Column(String(255))            # specDocFileUrl3
    spec_doc_file_url4 = Column(String(255))            # specDocFileUrl4
    spec_doc_file_url5 = Column(String(255))            # specDocFileUrl5

    # --- 물품상세·연계 (원문 보존) ---
    prdct_dtl_list = Column(Text)                       # prdctDtlList (물품상세목록, 최대 4000자)
    bid_ntce_no_list = Column(Text)                     # bidNtceNoList (관련 입찰공고번호 CSV — 연계 키, 저장만)

    # --- 일정(등록·변경) ---
    rgst_dt = Column(DateTime)                          # rgstDt (등록일시)
    chg_dt = Column(DateTime)                           # chgDt (변경일시)

    # --- 수집 메타데이터 ---
    raw_json = Column(Text)                             # 응답 item 원문 전체(재파싱 대비)
    collected_at = Column(DateTime, nullable=False)     # 최초 수집 시각(변환/수집 단계에서 부여)
    updated_at = Column(DateTime, nullable=False)       # 최종 갱신 시각

    def __repr__(self) -> str:  # pragma: no cover - 디버그용
        return f"<PreSpec {self.bf_spec_rgst_no!r} sw={self.sw_biz_obj_yn!r}>"


class AnalysisResult(Base):
    """RFP 분석 결과 캐시 — 공고/사전규격 1건당 최신 분석 1행."""

    __tablename__ = "analysis_result"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source_type = Column(Text, nullable=False)       # "bid" | "pre_spec"
    source_id = Column(Text, nullable=False)         # bid_ntce_no | bf_spec_rgst_no
    status = Column(Text, nullable=False)            # "analyzing" | "done" | "error"
    result_json = Column(Text, nullable=True)
    error_message = Column(Text, nullable=True)
    source_kind = Column(Text, nullable=True)        # "auto" | "upload"
    created_at = Column(DateTime, nullable=False, default=_utcnow)
    updated_at = Column(DateTime, nullable=False, default=_utcnow)

    __table_args__ = (
        UniqueConstraint("source_type", "source_id", name="uq_analysis_result_source"),
    )

    def __repr__(self) -> str:  # pragma: no cover - 디버그용
        return f"<AnalysisResult {self.source_type!r}/{self.source_id!r} status={self.status!r}>"


# --- Phase 10: 공고명 제외 키워드 -------------------------------------------
class ExcludeKeyword(Base):
    """공고명 제외 키워드 목록 — /list·/pre-spec 검색필터의 단어 필터링 기본 목록.

    - keyword: 제외할 단어(공고명/품명 부분일치). 대소문자 무시·부분일치로 제외한다.
    - is_default: 체크 시 목록 화면 첫 진입에서 자동 적용(칩으로 미리 채워짐).
    """

    __tablename__ = "exclude_keyword"

    id = Column(Integer, primary_key=True, autoincrement=True)
    keyword = Column(String(50), nullable=False)
    is_default = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, nullable=False, default=_utcnow)

    __table_args__ = (
        UniqueConstraint("keyword", name="uq_exclude_keyword_keyword"),
    )

    def __repr__(self) -> str:  # pragma: no cover - 디버그용
        return f"<ExcludeKeyword {self.keyword!r} default={self.is_default}>"


# --- Phase 7.1: 검토 목록 장바구니 -----------------------------------------
class ExportCartItem(Base):
    """검토 목록 장바구니 — 입찰공고·사전규격 혼합 저장."""

    __tablename__ = "export_cart"

    id = Column(Integer, primary_key=True, autoincrement=True)
    item_type = Column(String(10), nullable=False)   # "bid" | "pre_spec"
    item_id = Column(String(100), nullable=False)    # bid_ntce_no 또는 bf_spec_rgst_no
    added_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("item_type", "item_id", name="uq_export_cart_item"),
    )

    def __repr__(self) -> str:  # pragma: no cover - 디버그용
        return f"<ExportCartItem id={self.id} type={self.item_type!r} item={self.item_id!r}>"
