"""사전규격 수집기(`app.pre_spec_collector`) + `repository.upsert_pre_specs` 단위테스트 — Phase 5.3.

네트워크/실 DB(procurement.db) 비의존·결정적:
- `api_client.call_endpoint` 를 monkeypatch 해서 가짜 페이지 시퀀스를 돌려준다.
- DB 는 인메모리 SQLite engine 으로 격리(SessionLocal 을 테스트 엔진으로 바꿔치기).
- 윈도우는 고정 입력. collected_at 보존은 1·2회차 상대 비교로 검증.
실행: `pytest tests/test_pre_spec_collector.py`
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app import api_client, pre_spec_collector, repository
from app.db import Base
from app.models import AppConfig, CollectionRun, PreSpec
from app.pre_spec_collector import (
    INQRY_DIV,
    OPERATION,
    SW_BIZ_OBJ_YN,
    _fetch_pre_spec,
    _PreSpecConfigSnapshot,
)

WIN_BGN = datetime(2026, 5, 1, 0, 0)
WIN_END = datetime(2026, 6, 1, 0, 0)


# --- 가짜 ApiResult 헬퍼 -------------------------------------------------
def _result(result_code, items=None, total_count="0", error=None):
    return api_client.ApiResult(
        operation=OPERATION,
        request_url="",
        sent_params={},
        response_type="json",
        status_code=200,
        raw_text="",
        parsed=None,
        result_code=result_code,
        result_msg="",
        items=items or [],
        total_count=total_count,
        error=error,
    )


def _item(rgst_no, **extra):
    """변환 가능한 최소 사전규격 item(PK + 임의 필드)."""
    base = {
        "bfSpecRgstNo": rgst_no,
        "prdctClsfcNoNm": f"사업명-{rgst_no}",
        "swBizObjYn": "Y",
    }
    base.update(extra)
    return base


# --- repository.upsert_pre_specs 인메모리 SQLite -------------------------
@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    s = Session()
    try:
        yield s
    finally:
        s.close()


def _count(session) -> int:
    return session.execute(select(func.count()).select_from(PreSpec)).scalar_one()


def test_upsert_pre_specs_empty_list(session):
    assert repository.upsert_pre_specs(session, []) == (0, 0)
    assert _count(session) == 0


def test_upsert_pre_specs_insert_then_update_preserves_collected_at(session):
    t1 = datetime(2026, 6, 1, 10, 0, 0)
    t2 = datetime(2026, 6, 1, 11, 0, 0)

    new, updated = repository.upsert_pre_specs(
        session,
        [
            {
                "bf_spec_rgst_no": "R26BD0001",
                "prdct_clsfc_no_nm": "최초 사업명",
                "bid_ntce_no_list": "20260000001",
                "collected_at": t1,
                "updated_at": t1,
            }
        ],
    )
    assert (new, updated) == (1, 0)
    assert _count(session) == 1

    # 같은 PK 재삽입 → update. collected_at 보존, 나머지 갱신.
    new, updated = repository.upsert_pre_specs(
        session,
        [
            {
                "bf_spec_rgst_no": "R26BD0001",
                "prdct_clsfc_no_nm": "변경된 사업명",
                "bid_ntce_no_list": "20260000001,20260000002",
                "collected_at": t2,  # 무시되어야 함
                "updated_at": t2,
            }
        ],
    )
    assert (new, updated) == (0, 1)
    assert _count(session) == 1

    row = session.get(PreSpec, "R26BD0001")
    assert row.prdct_clsfc_no_nm == "변경된 사업명"
    assert row.bid_ntce_no_list == "20260000001,20260000002"
    assert row.collected_at == t1  # 최초 수집 시각 보존
    assert row.updated_at == t2  # 갱신


def test_upsert_pre_specs_mixed_new_and_existing(session):
    t = datetime(2026, 6, 1, 10, 0, 0)
    repository.upsert_pre_specs(
        session,
        [{"bf_spec_rgst_no": "P1", "collected_at": t, "updated_at": t}],
    )
    new, updated = repository.upsert_pre_specs(
        session,
        [
            {"bf_spec_rgst_no": "P1", "collected_at": t, "updated_at": t},  # 기존
            {"bf_spec_rgst_no": "P2", "collected_at": t, "updated_at": t},  # 신규
        ],
    )
    assert (new, updated) == (1, 1)
    assert _count(session) == 2


def test_upsert_pre_specs_ignores_unknown_keys_and_missing_pk(session):
    t = datetime(2026, 6, 1, 10, 0, 0)
    new, updated = repository.upsert_pre_specs(
        session,
        [
            # 화이트리스트 외 키(zzz_not_a_column)는 무시되고 insert 성공.
            {
                "bf_spec_rgst_no": "K1",
                "zzz_not_a_column": "X",
                "collected_at": t,
                "updated_at": t,
            },
            # PK 없는 값은 방어적으로 skip(전체 죽지 않음).
            {"collected_at": t, "updated_at": t},
        ],
    )
    assert (new, updated) == (1, 0)
    assert _count(session) == 1
    assert session.get(PreSpec, "K1") is not None


def test_upsert_pre_specs_dedup_within_batch(session):
    """같은 배치에 PK 중복 → row 1건(PK 중복 0)."""
    t = datetime(2026, 6, 1, 10, 0, 0)
    new, updated = repository.upsert_pre_specs(
        session,
        [
            {"bf_spec_rgst_no": "D1", "prdct_clsfc_no_nm": "a", "collected_at": t, "updated_at": t},
            {"bf_spec_rgst_no": "D1", "prdct_clsfc_no_nm": "b", "collected_at": t, "updated_at": t},
        ],
    )
    assert (new, updated) == (1, 1)
    assert _count(session) == 1
    assert session.get(PreSpec, "D1").prdct_clsfc_no_nm == "b"


# --- _fetch_pre_spec: 요청 파라미터·페이징·종료 -------------------------
def _snapshot(num_of_rows=20, max_retries=2):
    return _PreSpecConfigSnapshot(num_of_rows=num_of_rows, max_retries=max_retries)


def test_fetch_base_params_fixed_filters(monkeypatch):
    """첫 페이지 raw_params 에 inqryDiv=1·swBizObjYn=Y·기간 12자리 포함."""
    captured: list[dict] = []

    def _fake_call(operation, raw_params, response_type="json"):
        captured.append(dict(raw_params))
        return _result("03")  # No Data → 1페이지 후 종료

    monkeypatch.setattr(api_client, "call_endpoint", _fake_call)
    _fetch_pre_spec(WIN_BGN, WIN_END, _snapshot())

    p = captured[0]
    assert p["inqryDiv"] == INQRY_DIV == "1"
    assert p["swBizObjYn"] == SW_BIZ_OBJ_YN == "Y"
    assert p["numOfRows"] == "20"
    assert p["inqryBgnDt"] == "202605010000"
    assert p["inqryEndDt"] == "202606010000"
    assert len(p["inqryBgnDt"]) == 12 and len(p["inqryEndDt"]) == 12
    assert p["pageNo"] == "1"


def test_fetch_paging_until_last_page(monkeypatch):
    """total_count 로 페이지 수 계산, 마지막 페이지 후 종료. pageNo 시퀀스 확인."""
    pages_seen: list[str] = []

    def _fake_call(operation, raw_params, response_type="json"):
        pages_seen.append(raw_params["pageNo"])
        page = int(raw_params["pageNo"])
        # total_count=3, numOfRows=2 → 2페이지.
        if page == 1:
            return _result("00", items=[_item("A"), _item("B")], total_count="3")
        return _result("00", items=[_item("C")], total_count="3")

    monkeypatch.setattr(api_client, "call_endpoint", _fake_call)
    r = _fetch_pre_spec(WIN_BGN, WIN_END, _snapshot(num_of_rows=2))

    assert pages_seen == ["1", "2"]
    assert r["outcome"] == "ok"
    assert r["pages"] == 2
    assert [i["bfSpecRgstNo"] for i in r["items"]] == ["A", "B", "C"]


def test_fetch_stops_on_no_data(monkeypatch):
    """resultCode=03(No Data) 즉시 종료(추가 호출 없음)."""
    calls = {"n": 0}

    def _fake_call(operation, raw_params, response_type="json"):
        calls["n"] += 1
        return _result("03")

    monkeypatch.setattr(api_client, "call_endpoint", _fake_call)
    r = _fetch_pre_spec(WIN_BGN, WIN_END, _snapshot())
    assert calls["n"] == 1
    assert r["outcome"] == "ok"
    assert r["items"] == []


def test_fetch_retry_then_ok(monkeypatch):
    """일시 코드(01) 1회 후 00 → 재시도 누적·정상 수집(백오프는 패치로 즉시)."""
    monkeypatch.setattr(pre_spec_collector.time, "sleep", lambda s: None)
    seq = ["01", "00"]
    calls = {"n": 0}

    def _fake_call(operation, raw_params, response_type="json"):
        code = seq[calls["n"]] if calls["n"] < len(seq) else "00"
        calls["n"] += 1
        if code == "00":
            return _result("00", items=[_item("R1")], total_count="1")
        return _result(code)

    monkeypatch.setattr(api_client, "call_endpoint", _fake_call)
    r = _fetch_pre_spec(WIN_BGN, WIN_END, _snapshot(num_of_rows=20, max_retries=2))
    assert r["outcome"] == "ok"
    assert r["retry_count"] == 1
    assert [i["bfSpecRgstNo"] for i in r["items"]] == ["R1"]


def test_fetch_retry_exhausted_failed(monkeypatch):
    """일시 코드(01)가 max_retries 초과 지속 → failed."""
    monkeypatch.setattr(pre_spec_collector.time, "sleep", lambda s: None)

    def _fake_call(operation, raw_params, response_type="json"):
        return _result("01", error="일시 장애")

    monkeypatch.setattr(api_client, "call_endpoint", _fake_call)
    r = _fetch_pre_spec(WIN_BGN, WIN_END, _snapshot(max_retries=2))
    assert r["outcome"] == "failed"
    assert r["retry_count"] == 2
    assert r["last_code"] == "01"


def test_fetch_halt_on_nonretry_code(monkeypatch):
    """비재시도 코드(30) → halt."""
    def _fake_call(operation, raw_params, response_type="json"):
        return _result("30")

    monkeypatch.setattr(api_client, "call_endpoint", _fake_call)
    r = _fetch_pre_spec(WIN_BGN, WIN_END, _snapshot())
    assert r["outcome"] == "halt"
    assert r["halt_code"] == "30"


def test_fetch_api_client_error_is_failed(monkeypatch):
    """ApiClientError → failed 처리."""
    def _fake_call(operation, raw_params, response_type="json"):
        raise api_client.ApiClientError("bad param")

    monkeypatch.setattr(api_client, "call_endpoint", _fake_call)
    r = _fetch_pre_spec(WIN_BGN, WIN_END, _snapshot())
    assert r["outcome"] == "failed"
    assert "ApiClientError" in r["error_msg"]


# --- collect_pre_spec_window: 통합(임시 engine·SessionLocal 패치) --------
@pytest.fixture
def db_engine(monkeypatch):
    """인메모리 SQLite engine 으로 SessionLocal 을 바꿔치기 + app_config 시드.

    pre_spec_collector·repository 모두 `app.db.SessionLocal` 을 import 했으므로
    그 모듈 속성과 양쪽 참조를 모두 교체한다.
    """
    import app.db as dbmod

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)

    # app_config(id=1) 시드(get_config 전제). num_of_rows=2 로 두어 페이징 분기를
    # 작은 total_count 로 결정적으로 검증할 수 있게 한다.
    seed = Session()
    seed.add(
        AppConfig(
            id=1,
            backfill_days=30,
            num_of_rows=2,
            max_retries=2,
        )
    )
    seed.commit()
    seed.close()

    monkeypatch.setattr(dbmod, "SessionLocal", Session)
    monkeypatch.setattr(pre_spec_collector, "SessionLocal", Session)
    monkeypatch.setattr(repository, "SessionLocal", Session, raising=False)
    return Session


def _session_of(SessionFactory):
    return SessionFactory()


def test_collect_window_success_loads_and_no_dup(monkeypatch, db_engine):
    """2페이지 합쳐 적재 → row N건·PK 중복0·bid_ntce_no_list 보존·status=success."""
    def _fake_call(operation, raw_params, response_type="json"):
        page = int(raw_params["pageNo"])
        if page == 1:
            return _result(
                "00",
                items=[
                    _item("A", bidNtceNoList="20260000001"),
                    _item("B"),
                ],
                total_count="3",
            )
        return _result("00", items=[_item("C")], total_count="3")

    monkeypatch.setattr(api_client, "call_endpoint", _fake_call)

    run = pre_spec_collector.collect_pre_spec_window(
        WIN_BGN, WIN_END, trigger="manual"
    )
    assert run.status == "success"
    assert run.total_fetched == 3
    assert run.total_new == 3
    assert run.total_updated == 0
    assert run.trigger == "manual"

    s = _session_of(db_engine)
    try:
        assert s.execute(select(func.count()).select_from(PreSpec)).scalar_one() == 3
        # PK 중복 0
        distinct_pks = s.execute(select(PreSpec.bf_spec_rgst_no)).scalars().all()
        assert len(distinct_pks) == len(set(distinct_pks)) == 3
        # bid_ntce_no_list 보존
        assert s.get(PreSpec, "A").bid_ntce_no_list == "20260000001"
        # collected_at/updated_at 부여됨
        assert s.get(PreSpec, "A").collected_at is not None
        # detail_json 기록
        cr = s.get(CollectionRun, run.id)
        detail = json.loads(cr.detail_json)
        assert detail["outcome"] == "ok"
        assert detail["items"] == 3
    finally:
        s.close()


def test_collect_window_idempotent_rerun(monkeypatch, db_engine):
    """같은 응답으로 2회 → 2회차 new=0·updated=N, collected_at 보존·updated_at 갱신."""
    def _fake_call(operation, raw_params, response_type="json"):
        return _result("00", items=[_item("X"), _item("Y")], total_count="2")

    monkeypatch.setattr(api_client, "call_endpoint", _fake_call)

    run1 = pre_spec_collector.collect_pre_spec_window(WIN_BGN, WIN_END)
    assert (run1.total_new, run1.total_updated) == (2, 0)

    s = _session_of(db_engine)
    first_collected = s.get(PreSpec, "X").collected_at
    first_updated = s.get(PreSpec, "X").updated_at
    s.close()

    run2 = pre_spec_collector.collect_pre_spec_window(WIN_BGN, WIN_END)
    assert run2.status == "success"
    assert (run2.total_new, run2.total_updated) == (0, 2)

    s = _session_of(db_engine)
    try:
        row = s.get(PreSpec, "X")
        assert row.collected_at == first_collected  # 보존
        assert row.updated_at >= first_updated  # 갱신(>= 동일 보장)
        # 행 수 그대로(중복 0)
        assert s.execute(select(func.count()).select_from(PreSpec)).scalar_one() == 2
    finally:
        s.close()


def test_collect_window_skips_missing_pk(monkeypatch, db_engine):
    """bfSpecRgstNo 없는 item 은 변환 ValueError 로 skip, 나머지 저장(전체 죽지 않음)."""
    def _fake_call(operation, raw_params, response_type="json"):
        return _result(
            "00",
            items=[
                {"prdctClsfcNoNm": "PK없음", "swBizObjYn": "Y"},  # PK 누락 → skip
                _item("OK1"),
            ],
            total_count="2",
        )

    monkeypatch.setattr(api_client, "call_endpoint", _fake_call)
    run = pre_spec_collector.collect_pre_spec_window(WIN_BGN, WIN_END)
    assert run.status == "success"
    assert run.total_fetched == 2  # 받은 item 수(스킵 전)
    assert run.total_new == 1  # 실제 저장은 1건

    s = _session_of(db_engine)
    try:
        assert s.execute(select(func.count()).select_from(PreSpec)).scalar_one() == 1
        assert s.get(PreSpec, "OK1") is not None
    finally:
        s.close()


def test_collect_window_halt_marks_failed_no_save_no_set_halt(monkeypatch, db_engine):
    """halt 코드(30) → run failed·total_fetched=0·저장0·set_halt 미호출(auto_halted 불변)."""
    set_halt_called = {"n": 0}
    orig_set_halt = repository.set_halt

    def _spy_set_halt(*a, **k):
        set_halt_called["n"] += 1
        return orig_set_halt(*a, **k)

    monkeypatch.setattr(repository, "set_halt", _spy_set_halt)

    def _fake_call(operation, raw_params, response_type="json"):
        return _result("30")

    monkeypatch.setattr(api_client, "call_endpoint", _fake_call)

    run = pre_spec_collector.collect_pre_spec_window(WIN_BGN, WIN_END)
    assert run.status == "failed"
    assert run.total_fetched == 0
    assert run.total_new == 0
    assert run.error_code == "30"

    s = _session_of(db_engine)
    try:
        assert s.execute(select(func.count()).select_from(PreSpec)).scalar_one() == 0
        cfg = s.get(AppConfig, 1)
        assert cfg.auto_halted in (False, None)  # set_halt 미호출 → 불변
    finally:
        s.close()
    assert set_halt_called["n"] == 0  # set_halt 호출 안 함


def test_collect_window_retry_exhausted_partial_saves_received(monkeypatch, db_engine):
    """1페이지 ok 후 2페이지 재시도 소진 → 그때까지 저장·run partial."""
    monkeypatch.setattr(pre_spec_collector.time, "sleep", lambda s: None)

    def _fake_call(operation, raw_params, response_type="json"):
        page = int(raw_params["pageNo"])
        if page == 1:
            return _result(
                "00", items=[_item("P1"), _item("P2")], total_count="4"
            )
        return _result("01", error="일시 장애 지속")  # 2페이지부터 일시 장애

    monkeypatch.setattr(api_client, "call_endpoint", _fake_call)
    run = pre_spec_collector.collect_pre_spec_window(WIN_BGN, WIN_END)
    assert run.status == "partial"
    assert run.total_fetched == 2  # 1페이지에서 받은 2건
    assert run.total_new == 2
    assert run.error_code == "01"

    s = _session_of(db_engine)
    try:
        assert s.execute(select(func.count()).select_from(PreSpec)).scalar_one() == 2
    finally:
        s.close()


def test_collect_window_does_not_call_update_last_success(monkeypatch, db_engine):
    """정상 success 여도 update_last_success_dt 미호출(§2 — 5.4 분리)."""
    called = {"n": 0}
    orig = repository.update_last_success_dt

    def _spy(*a, **k):
        called["n"] += 1
        return orig(*a, **k)

    monkeypatch.setattr(repository, "update_last_success_dt", _spy)

    def _fake_call(operation, raw_params, response_type="json"):
        return _result("00", items=[_item("Z")], total_count="1")

    monkeypatch.setattr(api_client, "call_endpoint", _fake_call)
    run = pre_spec_collector.collect_pre_spec_window(WIN_BGN, WIN_END)
    assert run.status == "success"
    assert called["n"] == 0  # update_last_success_dt 호출 안 함

    s = _session_of(db_engine)
    try:
        assert s.get(AppConfig, 1).last_success_dt is None
    finally:
        s.close()
