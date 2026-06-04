"""FastAPI 화면 — Phase 3.5.

화면 3종으로 재편:
- `/list`  : 저장된 입찰공고 목록(메인). 공고명·공고일 기간·개찰 임박 필터 + 정렬·페이지네이션.
- `/config`: 수집 설정 편집·자동중단 재개·스케줄러 수동 시작/정지·최근 실행 이력.
- `/api-test`: Phase 1 의 수동 API 테스트 화면(기존 `/` 로직 이관).
`/` 는 `/list` 로 리다이렉트한다.

스케줄러는 **수동 모델**: 앱이 떠도 자동 수집하지 않는다(시작은 /config 또는 `python -m app.scheduler`).
앱 종료 시 켜져 있던 스케줄러만 정리한다.

실행: uvicorn app.main:app --reload
"""

from __future__ import annotations

import calendar
import html
import io
import logging
import zipfile
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel

from app import api_client, industry_codes, region_codes, repository, scheduler
from app.db import SessionLocal, init_db
from app.logging_config import setup_logging
from app.models import BidNotice, PreSpec
from app.field_labels import label as field_label
from app.api_client import (
    COMMON_PARAMS,
    ENDPOINTS,
    ENDPOINTS_BY_OP,
    ApiClientError,
    ApiResult,
    ParamSpec,
)
from app.analysis.analyzer_service import (
    AnalysisResult,
    UnsupportedFormatError,
    analyze_file,
    analyze_from_url,
    SUPPORTED_EXTENSIONS,
)

logger = logging.getLogger(__name__)


# --- 분석 API 응답 모델 (Phase 6.2a) ---
class AnalysisResponse(BaseModel):
    status: str  # "ok" | "no_file" | "unsupported" | "error"
    analysis: dict | None = None
    message: str = ""


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()  # 중앙 로깅 초기화 — 핸들러 없이 버려지는 로그 방지(멱등)
    try:
        init_db()  # 테이블+config seed 보장(멱등) 후 게이트 읽기
        with SessionLocal() as session:
            cfg = repository.get_config(session)
            enabled = cfg.enabled
            auto_halted = cfg.auto_halted
            pre_spec_enabled = cfg.pre_spec_enabled
            halt_code = cfg.halt_code
        if scheduler.should_autostart(enabled, auto_halted, pre_spec_enabled):
            logger.info(
                "자동시작 게이트 통과 → 스케줄러 시작(run_now=False): "
                "enabled=%s auto_halted=%s pre_spec_enabled=%s",
                enabled, auto_halted, pre_spec_enabled,
            )
            scheduler.start_scheduler(run_now=False)
        else:
            logger.info(
                "자동시작 게이트 미충족 → 스케줄러 미시작: "
                "enabled=%s auto_halted=%s halt_code=%s pre_spec_enabled=%s. "
                "/config 에서 [시작]으로 수동 기동 가능.",
                enabled, auto_halted, halt_code, pre_spec_enabled,
            )
    except Exception:  # noqa: BLE001 — 자동시작 실패가 앱 기동을 막지 않게(화면은 떠야 /config 로 복구).
        logger.exception("자동시작 처리 중 예외 — 스케줄러 미시작(앱은 계속 기동).")
    yield
    scheduler.shutdown_scheduler()
    logger.info("FastAPI 종료 — 스케줄러를 정리했습니다.")


app = FastAPI(title="나라장터 입찰공고 수집 (Phase 3)", lifespan=lifespan)

# 조회일시 영역에서 전용 위젯으로 렌더링하므로 일반 입력 그리드에서는 제외하는 파라미터.
# (inqryBgnDt/inqryEndDt 는 화면의 날짜 선택값으로부터 서버에서 조립한다)
DATETIME_PARAM_NAMES = {"inqryDiv", "inqryBgnDt", "inqryEndDt", "bidClseExcpYn"}


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _months_ago(d: date, n: int) -> date:
    """d 에서 n개월 전 날짜 (말일 보정)."""
    month = d.month - n
    year = d.year
    while month <= 0:
        month += 12
        year -= 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(d.day, last_day))


def _months_after(d: date, n: int) -> date:
    """d 에서 n개월 후 날짜 (말일 보정)."""
    month = d.month + n
    year = d.year
    while month > 12:
        month -= 12
        year += 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(d.day, last_day))


def _default_date_range() -> tuple[str, str]:
    """기본 조회 기간: 최근 한 달 (시작=한 달 전, 종료=오늘) ISO(yyyy-mm-dd)."""
    today = date.today()
    return _months_ago(today, 1).isoformat(), today.isoformat()


def _list_default_date_range(date_field: str, today: date | None = None) -> tuple[str, str]:
    """/list 날짜필드별 기본 기간(쿼리에 날짜가 없을 때 서버가 채움). ISO(yyyy-mm-dd).

    - 공고일(bid_ntce_dt): 오늘 − 1개월 ~ 오늘.
    - 개찰일(openg_dt):   오늘 ~ 오늘 + 1개월.
    today 는 테스트 결정성을 위해 주입 가능(기본 None → date.today()).
    """
    if today is None:
        today = date.today()
    if date_field == "openg_dt":
        return today.isoformat(), _months_after(today, 1).isoformat()
    return _months_ago(today, 1).isoformat(), today.isoformat()


def _render_field(p: ParamSpec, value: str = "") -> str:
    help_html = f'<span class="help">{_e(p.help)}</span>' if p.help else ""
    return f"""
      <label class="field">
        <span class="flabel">{_e(p.label)} <code>{_e(p.name)}</code></span>
        <input type="text" name="{_e(p.name)}" value="{_e(value)}" placeholder="{_e(p.placeholder)}">
        {help_html}
      </label>"""


def _render_endpoint_options(selected: str | None) -> str:
    opts = []
    for ep in ENDPOINTS:
        sel = " selected" if ep.operation == selected else ""
        opts.append(f'<option value="{_e(ep.operation)}"{sel}>{_e(ep.label)}</option>')
    return "\n".join(opts)


def _render_search_fields(operation: str | None, values: dict[str, str]) -> str:
    """선택된 엔드포인트의 검색 파라미터 입력 필드. 검색계열이 아니면 안내문."""
    spec = ENDPOINTS_BY_OP.get(operation or "")
    if spec is None or not spec.extra_params:
        return '<p class="muted">이 엔드포인트는 추가 검색 파라미터가 없습니다 (공통 파라미터만 사용).</p>'
    fields = [p for p in spec.extra_params if p.name not in DATETIME_PARAM_NAMES]
    if not fields:
        return '<p class="muted">이 엔드포인트는 추가 검색 파라미터가 없습니다 (공통 파라미터만 사용).</p>'
    return "\n".join(_render_field(p, values.get(p.name, "")) for p in fields)


def _render_datetime_block(operation: str | None, values: dict[str, str]) -> str:
    """공고/개찰일자 선택 + 날짜 범위 + 최근 1/3/6개월 + 입찰마감제외 영역.

    날짜는 입력 편의를 위해 date 위젯(yyyy-mm-dd)으로 받고, 호출 시 서버에서
    inqryBgnDt(=시작일 0000) / inqryEndDt(=종료일 2359) YYYYMMDDHHMM 으로 변환한다.
    """
    spec = ENDPOINTS_BY_OP.get(operation or "")
    inqry_div = values.get("inqryDiv", "1") or "1"
    def_bgn, def_end = _default_date_range()
    bgn = values.get("bgnDate") or def_bgn
    end = values.get("endDate") or def_end

    # 입찰마감제외 체크박스는 검색계열(11~14)에서만 의미가 있어 그때만 노출.
    clse_excp = ""
    if spec is not None and any(p.name == "bidClseExcpYn" for p in spec.extra_params):
        checked = " checked" if values.get("bidClseExcpYn") == "Y" else ""
        clse_excp = f"""
          <label class="chk">
            <input type="checkbox" name="bidClseExcpYn" value="Y"{checked}>
            입찰마감제외 <code>bidClseExcpYn</code>
          </label>"""

    # inqryDiv 의 의미는 서비스마다 다르다(라벨만 op별로 교체, 동작은 동일).
    #  - 입찰(검색/목록): 1=게시일자, 2=개찰일시
    #  - 사전규격(op15):  1=접수일시, 2=사전규격등록번호, 3=참조번호
    if spec is not None and spec.kind == "prespec":
        inqry_label = "조회구분"
        inqry_options = (("1", "접수일시"), ("2", "사전규격등록번호"), ("3", "참조번호"))
    else:
        inqry_label = "공고/개찰일자"
        inqry_options = (("1", "게시일자"), ("2", "개찰일시"))
    inqry_opt_html = "".join(
        f'<option value="{_e(v)}"{" selected" if inqry_div == v else ""}>{_e(t)}</option>'
        for v, t in inqry_options
    )

    return f"""
      <div class="row daterow">
        <label class="field">
          <span class="flabel">{_e(inqry_label)} <code>inqryDiv</code></span>
          <select name="inqryDiv">
            {inqry_opt_html}
          </select>
        </label>
        <label class="field">
          <span class="flabel">조회 시작</span>
          <input type="date" name="bgnDate" id="bgnDate" value="{_e(bgn)}">
        </label>
        <span class="tilde">~</span>
        <label class="field">
          <span class="flabel">조회 종료</span>
          <input type="date" name="endDate" id="endDate" value="{_e(end)}">
        </label>
        <div class="quick">
          <button type="button" onclick="setRecent(1)">최근1개월</button>
          <button type="button" onclick="setRecent(3)">최근3개월</button>
          <button type="button" onclick="setRecent(6)">최근6개월</button>
        </div>
        {clse_excp}
      </div>
      <span class="help">시작일은 00:00, 종료일은 23:59로
        inqryBgnDt/inqryEndDt(YYYYMMDDHHMM)에 매핑되어 호출됩니다.</span>"""


def _render_table(items: list[dict]) -> str:
    if not items:
        return '<p class="muted">표시할 item 이 없습니다.</p>'
    # 모든 item 의 키 합집합을 열로 사용 (등장 순서 유지)
    columns: list[str] = []
    seen = set()
    for it in items:
        for k in it.keys():
            if k not in seen:
                seen.add(k)
                columns.append(k)

    head = "".join(
        f'<th>{_e(field_label(c))}<br><code class="col-en">{_e(c)}</code></th>'
        for c in columns
    )
    rows = []
    for i, it in enumerate(items, start=1):
        cells = "".join(f"<td>{_e(it.get(c, ''))}</td>" for c in columns)
        rows.append(f"<tr><td>{i}</td>{cells}</tr>")
    return f"""
      <div class="table-wrap">
        <table>
          <thead><tr><th>#</th>{head}</tr></thead>
          <tbody>
            {"".join(rows)}
          </tbody>
        </table>
      </div>"""


def _render_result(result: ApiResult | None, error: str | None) -> str:
    if error:
        return f'<section class="result error"><h2>오류</h2><pre>{_e(error)}</pre></section>'
    if result is None:
        return ""

    if result.error:
        status_html = f'<span class="badge warn">{_e(result.error)}</span>'
    elif result.is_ok:
        status_html = f'<span class="badge ok">resultCode={_e(result.result_code)} {_e(result.result_code_desc)}</span>'
    elif result.result_code == "03":
        status_html = f'<span class="badge warn">resultCode=03 데이터 없음</span>'
    elif result.result_code is not None:
        status_html = f'<span class="badge err">resultCode={_e(result.result_code)} {_e(result.result_code_desc)}</span>'
    else:
        status_html = f'<span class="badge warn">resultCode 미확인 (HTTP {_e(result.status_code)})</span>'

    sent = "  ".join(f"{_e(k)}={_e(v)}" for k, v in result.sent_params.items()) or "(없음)"
    raw = api_client.pretty_raw(result)

    return f"""
    <section class="result">
      <h2>결과</h2>
      <div class="meta">
        {status_html}
        <span class="badge">HTTP {_e(result.status_code)}</span>
        <span class="badge">type={_e(result.response_type)}</span>
        <span class="badge">totalCount={_e(result.total_count if result.total_count is not None else '?')}</span>
        <span class="badge">items={_e(len(result.items))}</span>
      </div>
      <p class="reqline"><b>요청 URL</b>: <code>{_e(result.request_url)}</code></p>
      <p class="reqline"><b>전송 파라미터</b> (serviceKey 제외): <code>{_e(sent)}</code></p>

      <div class="tabs">
        <button type="button" class="tab active" data-target="tab-table">표 보기</button>
        <button type="button" class="tab" data-target="tab-raw">원문 보기</button>
      </div>
      <div id="tab-table" class="tabpane active">
        {_render_table(result.items)}
      </div>
      <div id="tab-raw" class="tabpane">
        <pre class="raw">{_e(raw)}</pre>
      </div>
    </section>"""


PAGE = """<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>나라장터 입찰공고 API 테스트 (Phase 1)</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
           margin: 0; background: #f4f5f7; color: #1f2430; }}
    header {{ background: #1f3a5f; color: #fff; padding: 16px 24px; }}
    header h1 {{ margin: 0; font-size: 18px; }}
    header p {{ margin: 4px 0 0; font-size: 12px; opacity: .8; }}
    main {{ max-width: 1180px; margin: 0 auto; padding: 20px; }}
    form {{ background: #fff; border-radius: 10px; padding: 20px; box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
    fieldset {{ border: 1px solid #e2e5ea; border-radius: 8px; margin: 0 0 16px; padding: 14px 16px; }}
    legend {{ font-weight: 600; font-size: 13px; color: #1f3a5f; padding: 0 6px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); gap: 12px; }}
    .field {{ display: flex; flex-direction: column; font-size: 13px; }}
    .flabel {{ margin-bottom: 4px; }}
    .flabel code {{ background: #eef1f6; padding: 1px 5px; border-radius: 4px; font-size: 11px; color: #4a5568; }}
    input, select {{ padding: 7px 8px; border: 1px solid #cbd2dc; border-radius: 6px; font-size: 13px; }}
    .help {{ font-size: 11px; color: #8a93a2; margin-top: 3px; }}
    .row {{ display: flex; gap: 14px; align-items: flex-end; flex-wrap: wrap; }}
    .daterow {{ align-items: flex-end; }}
    .tilde {{ padding-bottom: 8px; color: #8a93a2; }}
    .quick {{ display: flex; gap: 6px; padding-bottom: 1px; }}
    .quick button {{ border: 1px solid #cbd2dc; background: #f4f5f7; color: #4a5568;
                     padding: 7px 12px; border-radius: 6px; font-size: 13px; cursor: pointer; }}
    .quick button:hover {{ background: #e7ebf2; }}
    .chk {{ display: flex; align-items: center; gap: 6px; font-size: 13px; padding-bottom: 6px; }}
    .chk input {{ width: 16px; height: 16px; }}
    button.submit {{ background: #1f3a5f; color: #fff; border: 0; padding: 8px 22px;
                     border-radius: 7px; font-size: 14px; cursor: pointer; }}
    button.submit:hover {{ background: #16294a; }}
    .muted {{ color: #8a93a2; font-size: 13px; }}
    .result {{ background: #fff; border-radius: 10px; padding: 20px; margin-top: 20px;
               box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
    .result.error {{ border-left: 4px solid #d9534f; }}
    .result h2 {{ margin: 0 0 12px; font-size: 16px; }}
    .meta {{ display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 10px; }}
    .badge {{ font-size: 12px; padding: 3px 9px; border-radius: 20px; background: #eef1f6; color: #4a5568; }}
    .badge.ok {{ background: #e3f5e9; color: #1d7a43; }}
    .badge.warn {{ background: #fdf3df; color: #97720d; }}
    .badge.err {{ background: #fde3e1; color: #b02a25; }}
    .reqline {{ font-size: 12px; margin: 6px 0; word-break: break-all; }}
    code {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
    .tabs {{ display: flex; gap: 6px; margin: 14px 0 0; }}
    .tab {{ border: 1px solid #cbd2dc; background: #f4f5f7; padding: 6px 14px; border-radius: 6px 6px 0 0;
            cursor: pointer; font-size: 13px; }}
    .tab.active {{ background: #fff; border-bottom-color: #fff; font-weight: 600; }}
    .tabpane {{ display: none; border: 1px solid #cbd2dc; border-radius: 0 8px 8px 8px; padding: 12px; }}
    .tabpane.active {{ display: block; }}
    .table-wrap {{ overflow-x: auto; max-height: 520px; overflow-y: auto; }}
    table {{ border-collapse: collapse; font-size: 12px; white-space: nowrap; }}
    th, td {{ border: 1px solid #e2e5ea; padding: 5px 8px; text-align: left; max-width: 320px;
             overflow: hidden; text-overflow: ellipsis; }}
    th {{ background: #f4f5f7; position: sticky; top: 0; vertical-align: top; }}
    th .col-en {{ font-weight: 400; font-size: 10px; color: #97a0b0; }}
    pre.raw {{ margin: 0; max-height: 520px; overflow: auto; font-size: 12px;
               background: #1f2430; color: #e6e9ef; padding: 12px; border-radius: 6px; }}
  </style>
</head>
<body>
  <header>
    <h1>나라장터 입찰공고 API 테스트 <a href="/list" style="color:#cdd9ec;font-size:12px;font-weight:400;">← 목록으로</a></h1>
    <p>엔드포인트를 호출해 응답 형태를 확인합니다 · 베이스 URL: {base_url}</p>
  </header>
  <main>
    <form method="post" action="/api-test/call">
      <fieldset>
        <legend>엔드포인트</legend>
        <div class="row">
          <label class="field" style="min-width:320px;">
            <span class="flabel">오퍼레이션 선택</span>
            <select name="operation" onchange="window.location='/api-test?operation='+encodeURIComponent(this.value)">
              {endpoint_options}
            </select>
          </label>
          <label class="field">
            <span class="flabel">응답 타입 <code>type</code></span>
            <select name="response_type">
              <option value="json"{json_sel}>json</option>
              <option value="xml"{xml_sel}>xml</option>
            </select>
          </label>
          <span class="help">엔드포인트를 바꾸면 해당 검색 파라미터로 폼이 갱신됩니다.</span>
        </div>
      </fieldset>

      <fieldset>
        <legend>조회 기간 / 조회구분</legend>
        {datetime_block}
      </fieldset>

      <fieldset>
        <legend>기타 공통 파라미터</legend>
        <div class="grid">
          {common_fields}
        </div>
      </fieldset>

      <fieldset>
        <legend>검색 파라미터 (비우면 전송하지 않음)</legend>
        <div class="grid">
          {search_fields}
        </div>
      </fieldset>

      <button type="submit" class="submit">호출하기</button>
    </form>

    {result_html}
  </main>

  <script>
    function fmtDate(d) {{
      var m = ('0' + (d.getMonth() + 1)).slice(-2);
      var day = ('0' + d.getDate()).slice(-2);
      return d.getFullYear() + '-' + m + '-' + day;
    }}
    function setRecent(n) {{
      var today = new Date();
      var bgn = new Date(today.getFullYear(), today.getMonth() - n, today.getDate());
      var be = document.getElementById('bgnDate');
      var ee = document.getElementById('endDate');
      if (be) be.value = fmtDate(bgn);
      if (ee) ee.value = fmtDate(today);
    }}

    document.querySelectorAll('.tab').forEach(function (btn) {{
      btn.addEventListener('click', function () {{
        var pane = document.getElementById(btn.dataset.target);
        if (!pane) return;
        var root = btn.closest('.result');
        root.querySelectorAll('.tab').forEach(function (b) {{ b.classList.remove('active'); }});
        root.querySelectorAll('.tabpane').forEach(function (p) {{ p.classList.remove('active'); }});
        btn.classList.add('active');
        pane.classList.add('active');
      }});
    }});
  </script>
</body>
</html>"""


def _render_page(
    operation: str,
    response_type: str,
    values: dict[str, str],
    result: ApiResult | None = None,
    error: str | None = None,
) -> str:
    common_fields = "\n".join(
        _render_field(p, values.get(p.name, ""))
        for p in COMMON_PARAMS
        if p.name not in DATETIME_PARAM_NAMES
    )
    return PAGE.format(
        base_url=_e(api_client.BASE_URL),
        endpoint_options=_render_endpoint_options(operation),
        json_sel=" selected" if response_type == "json" else "",
        xml_sel=" selected" if response_type == "xml" else "",
        datetime_block=_render_datetime_block(operation, values),
        common_fields=common_fields,
        search_fields=_render_search_fields(operation, values),
        result_html=_render_result(result, error),
    )


def _default_values() -> dict[str, str]:
    """진입/엔드포인트 전환 시의 기본 입력값 (최근 한 달 + 입찰마감제외 기본 체크)."""
    bgn, end = _default_date_range()
    return {
        "inqryDiv": "1",
        "bgnDate": bgn,
        "endDate": end,
        "bidClseExcpYn": "Y",
        "pageNo": "1",
        "numOfRows": "10",
    }


def _dates_to_inqry(call_params: dict[str, str]) -> None:
    """화면의 bgnDate/endDate(yyyy-mm-dd)를 inqryBgnDt/inqryEndDt(YYYYMMDDHHMM)로 변환."""
    bgn = call_params.pop("bgnDate", "").strip()
    end = call_params.pop("endDate", "").strip()
    if bgn:
        call_params["inqryBgnDt"] = bgn.replace("-", "") + "0000"
    if end:
        call_params["inqryEndDt"] = end.replace("-", "") + "2359"


@app.get("/api-test", response_class=HTMLResponse)
def api_test(operation: str | None = None) -> HTMLResponse:
    op = operation if operation in ENDPOINTS_BY_OP else ENDPOINTS[0].operation
    return HTMLResponse(_render_page(op, api_client.DEFAULT_RESPONSE_TYPE, _default_values()))


@app.post("/api-test/call", response_class=HTMLResponse)
async def api_test_call(request: Request) -> HTMLResponse:
    form = await request.form()
    data = {k: str(v) for k, v in form.items()}
    operation = data.pop("operation", ENDPOINTS[0].operation)
    response_type = data.pop("response_type", api_client.DEFAULT_RESPONSE_TYPE)

    # 호출용 파라미터는 별도 사본으로 만들어 날짜를 변환한다.
    # 원본 data 는 화면 재표시(날짜 위젯 값 유지)에 그대로 사용.
    call_params = dict(data)
    _dates_to_inqry(call_params)

    result: ApiResult | None = None
    error: str | None = None
    try:
        result = api_client.call_endpoint(operation, call_params, response_type)
    except ApiClientError as exc:
        error = str(exc)
    except Exception as exc:  # noqa: BLE001
        error = f"예상치 못한 오류: {exc}"

    return HTMLResponse(_render_page(operation, response_type, data, result, error))


# =====================================================================
#  /list · /config  — Phase 3.5 화면(인라인 HTML + 공유 CSS)
# =====================================================================

# 새 화면(/list·/config)용 공유 CSS. PAGE(api-test)는 자체 CSS 를 유지한다.
BASE_CSS = """
  * { box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         margin: 0; background: #f4f5f7; color: #1f2430; }
  header { background: #1f3a5f; color: #fff; padding: 12px 24px;
           display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 10px; }
  header h1 { margin: 0; font-size: 18px; }
  header p { margin: 4px 0 0; font-size: 12px; opacity: .8; }
  /* 헤더 우측 설정 버튼 */
  .hdr-btn { background: #2d5286; color: #fff; text-decoration: none; font-size: 13px;
             padding: 7px 14px; border-radius: 7px; }
  .hdr-btn:hover { background: #3a6199; }
  .hdr-btn.active { background: #fff; color: #1f3a5f; font-weight: 600; }
  /* 탭바 — sticky 상단 고정(z-index 30, 불투명 배경) */
  .tab-bar { display: flex; gap: 0; border-bottom: 2px solid #1f3a5f;
             position: sticky; top: 0; z-index: 30; background: #f4f5f7; }
  .tab-bar a { text-decoration: none; color: #4a5568; font-size: 14px;
               padding: 10px 20px; border-radius: 8px 8px 0 0; }
  .tab-bar a:hover { background: #eef1f6; color: #1f3a5f; }
  .tab-bar a.active { background: #1f3a5f; color: #fff; font-weight: 600; }
  main { max-width: 1920px; margin: 0 auto; padding: 20px; }
  .card { background: #fff; border-radius: 10px; padding: 20px; margin-bottom: 20px;
          box-shadow: 0 1px 3px rgba(0,0,0,.08); }
  .card h2 { margin: 0 0 14px; font-size: 16px; }
  form.filter, form.cfg { display: block; }
  .row { display: flex; gap: 14px; align-items: flex-end; flex-wrap: wrap; }
  .field { display: flex; flex-direction: column; font-size: 13px; }
  .flabel { margin-bottom: 4px; }
  .flabel code { background: #eef1f6; padding: 1px 5px; border-radius: 4px; font-size: 11px; color: #4a5568; }
  input, select { padding: 7px 8px; border: 1px solid #cbd2dc; border-radius: 6px; font-size: 13px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 12px; }
  .tilde { padding-bottom: 8px; color: #8a93a2; }
  .quick { display: flex; gap: 6px; padding-bottom: 1px; }
  .quick button, button.ghost { border: 1px solid #cbd2dc; background: #f4f5f7; color: #4a5568;
                   padding: 7px 12px; border-radius: 6px; font-size: 13px; cursor: pointer; }
  .quick button:hover, button.ghost:hover { background: #e7ebf2; }
  .chk { display: flex; align-items: center; gap: 6px; font-size: 13px; padding-bottom: 6px; }
  .chk input { width: 16px; height: 16px; }
  button.submit { background: #1f3a5f; color: #fff; border: 0; padding: 8px 22px;
                  border-radius: 7px; font-size: 14px; cursor: pointer; }
  button.submit:hover { background: #16294a; }
  button.danger { background: #b02a25; }
  button.danger:hover { background: #8f211d; }
  button.go { background: #1d7a43; }
  button.go:hover { background: #166035; }
  .muted { color: #8a93a2; font-size: 13px; }
  .note { font-size: 12px; color: #6b7280; background: #f0f3f8; border-left: 3px solid #9bb0cf;
          padding: 8px 12px; border-radius: 4px; margin: 8px 0; }
  .badge { font-size: 12px; padding: 3px 9px; border-radius: 20px; background: #eef1f6; color: #4a5568; }
  .badge.ok { background: #e3f5e9; color: #1d7a43; }
  .badge.warn { background: #fdf3df; color: #97720d; }
  .badge.err { background: #fde3e1; color: #b02a25; }
  .msg { padding: 10px 14px; border-radius: 7px; font-size: 13px; margin-bottom: 14px; }
  .msg.ok { background: #e3f5e9; color: #1d7a43; }
  .msg.err { background: #fde3e1; color: #b02a25; }
  code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
  .table-wrap { overflow-x: auto; }
  table { border-collapse: collapse; font-size: 12px; width: 100%; }
  th, td { border: 1px solid #e2e5ea; padding: 6px 8px; text-align: left; vertical-align: top;
           max-width: 360px; overflow: hidden; text-overflow: ellipsis; }
  th { background: #f4f5f7; }
  th .col-en { font-weight: 400; font-size: 10px; color: #97a0b0; }
  tbody tr:hover { background: #f5f8ff; }
  tr.bad { background: #fdf1f0; }
  /* 정렬 가능한 컬럼 헤더(Phase 4.2): 클릭 가능 표시 + 방향 화살표 */
  th a.sortcol { text-decoration: none; color: inherit; display: inline-block; cursor: pointer; }
  th a.sortcol:hover { color: #1f3a5f; }
  th a.sortcol .arrow { color: #1f3a5f; font-size: 11px; }
  th a.sortcol .arrow.neutral { color: #b9c0cc; font-size: 11px; }
  /* 매칭업종 컬럼: 한글(코드) 세로 목록 + 너비 제한 + ellipsis(tooltip 으로 전체 확인) */
  td.matchind { max-width: 240px; }
  td.matchind .indrow { display: block; overflow: hidden; text-overflow: ellipsis;
                        white-space: nowrap; }
  /* 공고기관/수요기관 통합 셀: 위=공고기관(옅은 회색), 아래=수요기관(기본색), 세로 간격 ≥4px */
  td.insttcell { max-width: 260px; }
  td.insttcell .ntcorg, td.insttcell .dmnorg { display: block; overflow: hidden;
                        text-overflow: ellipsis; white-space: nowrap; }
  td.insttcell .ntcorg { color: #8a93a2; margin-bottom: 5px; }
  td.insttcell .dmnorg { color: inherit; }
  /* sticky 페이저 래퍼: 화면 하단 고정 */
  .pager-wrap { position: sticky; bottom: 0; background: #fff; padding: 8px 16px;
                box-shadow: 0 -2px 6px rgba(0,0,0,.08); border-top: 1px solid #e2e5ea; }
  .pager { display: flex; gap: 8px; align-items: center; font-size: 13px; flex-wrap: wrap; }
  .pager a, .pager-btn { text-decoration: none; color: #1f3a5f; border: 1px solid #cbd2dc;
             padding: 5px 10px; border-radius: 6px; background: #fff; font-size: 12px; }
  .pager a:hover, .pager-btn:hover { background: #eef1f6; }
  .pager a.disabled, .pager-btn.disabled { color: #b9c0cc; pointer-events: none; }
  .pager .cur-page { font-weight: 600; color: #fff; background: #1f3a5f;
                     border: 1px solid #1f3a5f; padding: 5px 10px; border-radius: 6px; font-size: 12px; }
  .pager .pager-info { color: #6b7280; font-size: 12px; }
  .pager .pager-ellipsis { color: #8a93a2; padding: 5px 4px; font-size: 12px; }
  fieldset { border: 1px solid #e2e5ea; border-radius: 8px; margin: 0 0 16px; padding: 14px 16px; }
  legend { font-weight: 600; font-size: 13px; color: #1f3a5f; padding: 0 6px; }
  /* 플로팅 필터 카드: 탭바 아래 sticky(--tabbar-h) + 접힘/펼침 */
  .filter-card { position: sticky; top: var(--tabbar-h, 42px); z-index: 20; background: #fff;
                 border-radius: 0 0 8px 8px; box-shadow: 0 2px 6px rgba(0,0,0,.1);
                 margin-bottom: 16px; }
  /* 버튼 세로정렬: 검색·토글 버튼이 좌측 input 하단과 정렬 */
  .filter-summary { display: flex; gap: 10px; align-items: flex-end; padding: 10px 16px;
                    flex-wrap: wrap; }
  .filter-detail { padding: 0 16px 14px; }
  .filter-collapsed .filter-detail { display: none; }
  .filter-toggle { background: #f4f5f7; border: 1px solid #cbd2dc; color: #4a5568;
                   padding: 7px 12px; border-radius: 6px; font-size: 13px; cursor: pointer; white-space: nowrap; }
  .filter-toggle:hover { background: #e7ebf2; }
  /* 파일 다운로드 drawer (Phase 4.1) */
  button.filebtn { border: 1px solid #b6c6df; background: #eef3fb; color: #1f3a5f; padding: 4px 10px;
                   border-radius: 6px; font-size: 12px; cursor: pointer; white-space: nowrap; }
  button.filebtn:hover { background: #dfe9f7; }
  .drawer-backdrop { position: fixed; inset: 0; background: rgba(0,0,0,.35); display: none; z-index: 40; }
  .drawer-backdrop.open { display: block; }
  .drawer { position: fixed; top: 0; right: 0; height: 100%; width: 500px; max-width: 100vw; background: #fff;
            box-shadow: -2px 0 10px rgba(0,0,0,.2); transform: translateX(100%); transition: transform .2s ease;
            z-index: 50; display: flex; flex-direction: column; }
  @media (max-width: 600px) { .drawer { width: 100%; } }
  .drawer.open { transform: translateX(0); }
  .drawer .dz-head { background: #1f3a5f; color: #fff; padding: 14px 16px; position: relative; }
  .drawer .dz-title { font-size: 14px; padding-right: 28px; word-break: break-all; }
  .drawer .dz-close { position: absolute; top: 10px; right: 12px; background: transparent; border: 0;
                      color: #fff; font-size: 22px; line-height: 1; cursor: pointer; }
  .drawer .dz-body { padding: 16px; overflow-y: auto; flex: 1; }
  .zipall { display: block; text-align: center; background: #1d7a43; color: #fff; text-decoration: none;
            padding: 9px; border-radius: 7px; margin-bottom: 14px; font-size: 13px; }
  .zipall:hover { background: #166035; }
  /* 파일 목록 테이블(Phase 4.2): 파일명 ellipsis + title(tooltip)으로 전체명 확인 */
  table.filetable { width: 100%; border-collapse: collapse; font-size: 13px; table-layout: fixed; }
  table.filetable th, table.filetable td { border: 1px solid #eef1f6; padding: 7px 8px; text-align: left;
                                           vertical-align: middle; }
  table.filetable th { background: #f4f5f7; font-size: 12px; color: #4a5568; }
  table.filetable th.c-dl, table.filetable td.c-dl { width: 88px; text-align: center; }
  table.filetable td.c-fn { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 0; }
  table.filetable a.dl { background: #1f3a5f; color: #fff; text-decoration: none; padding: 5px 10px;
                         border-radius: 6px; font-size: 12px; white-space: nowrap; }
  table.filetable a.dl:hover { background: #16294a; }
  /* 분석 버튼 (Phase 6.3) */
  button.btn-analyze { border: 1px solid #7e5cb0; background: #f3eeff; color: #5a3e8a; padding: 4px 10px;
                       border-radius: 6px; font-size: 12px; cursor: pointer; white-space: nowrap; }
  button.btn-analyze:hover { background: #e8dcff; }
  button.btn-analyze:disabled { opacity: .6; cursor: default; }
  button.btn-analyze.open { background: #5a3e8a; color: #fff; border-color: #5a3e8a; }
  /* 분석 결과 패널 */
  .analysis-result-row td { padding: 0; background: #f8f6ff; }
  .analysis-panel { padding: 20px 24px; border-top: 2px solid #c9b8ef; }
  .analysis-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
  .analysis-header h3 { margin: 0; font-size: 15px; color: #3d2870; }
  .analysis-close { background: transparent; border: 1px solid #b9b0cc; color: #5a3e8a;
                    padding: 4px 10px; border-radius: 6px; font-size: 13px; cursor: pointer; }
  .analysis-close:hover { background: #ede5ff; }
  .analysis-section { margin-bottom: 20px; border-top: 1px solid #e0d8f5; padding-top: 14px; }
  .analysis-section:first-of-type { border-top: none; padding-top: 0; }
  .analysis-section h4 { margin: 0 0 10px; font-size: 13px; color: #5a3e8a; font-weight: 600; }
  .info-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 8px; }
  .info-grid > div { font-size: 13px; }
  .info-grid .label { font-weight: 600; color: #6b7280; margin-right: 6px; }
  .analysis-section p { margin: 0; font-size: 13px; line-height: 1.6; }
  .analysis-section ol, .analysis-section ul { margin: 0; padding-left: 20px; font-size: 13px; line-height: 1.8; }
  .analysis-section table { border-collapse: collapse; width: 100%; font-size: 12px; }
  .analysis-section table th, .analysis-section table td { border: 1px solid #d8d0ee; padding: 5px 8px; text-align: left; }
  .analysis-section table th { background: #ede5ff; }
  /* Win Theme 카드 */
  .win-theme-cards { display: flex; gap: 12px; flex-wrap: wrap; }
  .win-theme-card { background: #fff; border: 1px solid #c9b8ef; border-radius: 8px;
                    padding: 12px 14px; min-width: 160px; max-width: 280px; flex: 1; }
  .win-theme-card .theme-title { font-weight: 600; font-size: 13px; color: #3d2870; margin-bottom: 6px; }
  .win-theme-card .theme-desc { font-size: 12px; color: #4a5568; line-height: 1.5; }
  /* 업로드 모달 */
  .modal-overlay { position: fixed; inset: 0; background: rgba(0,0,0,.4); display: none; z-index: 60;
                   align-items: center; justify-content: center; }
  .modal-overlay.open { display: flex; }
  .modal-content { background: #fff; border-radius: 12px; padding: 0; width: 480px; max-width: 95vw;
                   box-shadow: 0 4px 24px rgba(0,0,0,.18); }
  .modal-header { background: #3d2870; color: #fff; padding: 16px 20px; border-radius: 12px 12px 0 0;
                  display: flex; align-items: center; justify-content: space-between; }
  .modal-header h3 { margin: 0; font-size: 15px; }
  .modal-header .modal-close-btn { background: transparent; border: 0; color: #fff; font-size: 22px;
                                   line-height: 1; cursor: pointer; padding: 0 4px; }
  .modal-body { padding: 20px; }
  .modal-message { font-size: 13px; color: #6b7280; margin: 0 0 14px; }
  .upload-area { border: 2px dashed #c9b8ef; border-radius: 8px; padding: 28px 20px;
                 text-align: center; cursor: pointer; transition: border-color .15s, background .15s; }
  .upload-area:hover, .upload-area.dragover { border-color: #7e5cb0; background: #f8f4ff; }
  .upload-area p { margin: 0; font-size: 13px; color: #6b7280; }
  .upload-area .upload-hint { font-size: 11px; color: #9ca3af; margin-top: 6px; }
  .upload-filename { font-size: 13px; color: #3d2870; font-weight: 600; margin-top: 8px; }
  .modal-footer { display: flex; gap: 10px; justify-content: flex-end; padding: 14px 20px;
                  border-top: 1px solid #e5e7eb; }
  button.modal-submit { background: #5a3e8a; color: #fff; border: 0; padding: 8px 20px;
                        border-radius: 7px; font-size: 13px; cursor: pointer; }
  button.modal-submit:hover { background: #3d2870; }
  button.modal-submit:disabled { background: #b9b0cc; cursor: default; }
  button.modal-cancel { background: #f4f5f7; border: 1px solid #cbd2dc; color: #4a5568;
                        padding: 8px 16px; border-radius: 7px; font-size: 13px; cursor: pointer; }
  button.modal-cancel:hover { background: #e7ebf2; }
  /* 분석 로딩 오버레이 */
  .analysis-loading { text-align: center; padding: 24px; font-size: 13px; color: #5a3e8a; }
  /* 토스트(분석 에러) */
  #analyzeToast { position: fixed; bottom: 80px; left: 50%; transform: translateX(-50%);
                  background: #b02a25; color: #fff; padding: 10px 20px; border-radius: 8px;
                  font-size: 13px; display: none; z-index: 70; box-shadow: 0 2px 8px rgba(0,0,0,.2); }
"""


def _nav(active: str) -> str:
    """헤더 우측 설정 버튼. active in {'list','pre-spec','config','api-test'}.

    헤더 우측에는 '설정' 링크 하나만 노출. 탭바는 _shell 에서 <main> 최상단에 렌더한다.
    """
    cfg_cls = ' class="hdr-btn active"' if active == "config" else ' class="hdr-btn"'
    return f'<a href="/config"{cfg_cls}>설정</a>'


def _tab_bar(active: str) -> str:
    """<main> 최상단 탭바. active in {'list','pre-spec'} 일 때 해당 탭 강조."""
    list_cls = ' class="active"' if active == "list" else ""
    ps_cls = ' class="active"' if active == "pre-spec" else ""
    return f"""
    <nav class="tab-bar">
      <a href="/list"{list_cls}>입찰공고목록</a>
      <a href="/pre-spec"{ps_cls}>사전규격목록</a>
    </nav>"""


_STICKY_STACK_SCRIPT = """(function () {
  'use strict';
  function updateTabbarH() {
    var tabBar = document.querySelector('.tab-bar');
    var tabH = tabBar ? tabBar.getBoundingClientRect().height : 0;
    document.documentElement.style.setProperty('--tabbar-h', tabH + 'px');
  }
  updateTabbarH();
  if (typeof ResizeObserver !== 'undefined') {
    var ro = new ResizeObserver(updateTabbarH);
    var tabBar = document.querySelector('.tab-bar');
    if (tabBar) ro.observe(tabBar);
  }
})();"""


def _shell(title: str, subtitle: str, active: str, body: str) -> str:
    """공통 HTML 셸. subtitle 인자는 호환성 위해 유지하나 헤더에 렌더하지 않는다."""
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_e(title)}</title>
  <style>{BASE_CSS}</style>
</head>
<body>
  <header>
    <h1>{_e(title)}</h1>
    {_nav(active)}
  </header>
  <main>
    {_tab_bar(active)}
    {body}
  </main>
  <script>{_STICKY_STACK_SCRIPT}</script>
</body>
</html>"""


def _fmt_dt(value: Any) -> str:
    """DateTime 컬럼값을 'YYYY-MM-DD HH:MM' 로. None/빈값은 빈 문자열."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    return str(value)


def _fmt_amt(value: Any) -> str:
    """금액(Numeric)을 천단위 콤마 + ` 원` 으로(예: `1,000,000 원`). None/빈값은 빈 문자열."""
    if value is None or value == "":
        return ""
    try:
        return f"{int(value):,} 원"
    except (TypeError, ValueError):
        return str(value)


# --- /list -----------------------------------------------------------
# (컬럼명, 표시 종류, 한글 헤더). /list 는 한글 헤더만 노출한다(영문 병기 없음).
# field_labels.py(camelCase 키)는 /list 의 snake_case 와 맞지 않아 미해석되므로,
# 여기서 컬럼별 한글 헤더를 직접 큐레이션한다(field_labels.py 는 /api-test 전용으로 유지).
_LIST_COLUMNS: list[tuple[str, str, str]] = [
    ("bid_ntce_no", "text", "공고번호"),
    ("bid_ntce_nm", "link", "공고명"),
    ("ntce_instt_nm", "instt", "공고기관/수요기관"),
    ("asign_bdgt_amt", "amt", "배정예산"),
    ("presmpt_prce", "amt", "추정가격"),
    ("bid_ntce_dt", "dt", "공고일시"),
    ("openg_dt", "dt", "개찰일시"),
    ("matched_indstryty_cds", "matchind", "매칭업종"),
]

# 컬럼 헤더 클릭 정렬이 가능한 컬럼 → 정렬 sort 키 접두(asc/desc 토글에 사용).
_SORTABLE_COLUMNS: dict[str, str] = {
    "bid_ntce_dt": "bid_ntce_dt",
    "openg_dt": "openg_dt",
    "presmpt_prce": "presmpt_prce",
}

# /list 정렬 허용값(repository._SORT_COLUMNS 와 동일). 미허용·빈값은 기본으로 폴백.
_LIST_SORTS: frozenset[str] = frozenset(
    {
        "bid_ntce_dt_desc",
        "bid_ntce_dt_asc",
        "openg_dt_desc",
        "openg_dt_asc",
        "presmpt_prce_desc",
        "presmpt_prce_asc",
    }
)
_DEFAULT_SORT = "bid_ntce_dt_desc"

# 첨부 URL 컬럼명(파일 개수 계산·파일 목록 추출에 재사용).
_SPEC_URL_COLUMNS: list[str] = [f"ntce_spec_doc_url{i}" for i in range(1, 11)]


# --- /pre-spec (Phase 5.5) -------------------------------------------
# (컬럼명, 표시 종류, 한글 헤더). /pre-spec 도 한글 헤더만 노출(영문 병기 없음).
# instt2 = 발주기관(위)/실수요기관(아래) 2줄 셀(입찰 /list 의 instt 와 동형).
_PRE_SPEC_COLUMNS: list[tuple[str, str, str]] = [
    ("bf_spec_rgst_no", "text", "사전규격번호"),
    ("prdct_clsfc_no_nm", "text", "품명/사업명"),
    ("order_instt_nm", "instt2", "발주기관/실수요기관"),
    ("asign_bdgt_amt", "amt", "배정예산"),
    ("rcpt_dt", "dt", "접수일시"),
    ("opnin_rgst_clse_dt", "dt", "의견마감일시"),
]

# 컬럼 헤더 클릭 정렬이 가능한 컬럼 → 정렬 sort 키 접두(asc/desc 토글에 사용).
_PRE_SPEC_SORTABLE_COLUMNS: dict[str, str] = {
    "rcpt_dt": "rcpt_dt",
    "opnin_rgst_clse_dt": "opnin_rgst_clse_dt",
    "asign_bdgt_amt": "asign_bdgt_amt",
}

# /pre-spec 정렬 허용값(repository._PRE_SPEC_SORT_COLUMNS 와 동일). 미허용·빈값은 기본 폴백.
_PRE_SPEC_SORTS: frozenset[str] = frozenset(
    {
        "rcpt_dt_desc",
        "rcpt_dt_asc",
        "opnin_rgst_clse_dt_desc",
        "opnin_rgst_clse_dt_asc",
        "asign_bdgt_amt_desc",
        "asign_bdgt_amt_asc",
    }
)
_PRE_SPEC_DEFAULT_SORT = "rcpt_dt_desc"

# 사전규격 첨부 URL 컬럼명(파일 개수 계산·파일 목록 추출에 재사용).
_PRE_SPEC_FILE_URL_COLUMNS: list[str] = [f"spec_doc_file_url{i}" for i in range(1, 6)]


def _parse_date(s: str | None, *, end_of_day: bool = False) -> datetime | None:
    """yyyy-mm-dd 문자열 → datetime. 빈값/형식오류는 None. end_of_day=True 면 23:59:59."""
    if not s:
        return None
    s = s.strip()
    if not s:
        return None
    try:
        d = datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        return None
    if end_of_day:
        return d.replace(hour=23, minute=59, second=59)
    return d


def _render_matched_inds(csv: Any) -> str:
    """매칭업종 CSV → '업무명 [코드]' 세로 목록 셀.

    표시는 단축 라벨(`업무명 [코드]`), title(tooltip)은 전체명(`전체명 [코드]`).
    셀은 너비 제한+ellipsis 로 줄이되 title 로 전체 확인 가능.
    """
    pairs = industry_codes.matched_label_pairs(str(csv) if csv is not None else None)
    if not pairs:
        return "<td></td>"
    rows = "".join(
        f'<span class="indrow" title="{_e(full)}">{_e(short)}</span>'
        for short, full in pairs
    )
    return f'<td class="matchind">{rows}</td>'


def _render_instt(ntce: Any, dmin: Any) -> str:
    """공고기관/수요기관 통합 셀. 위=공고기관(옅은 회색), 아래=수요기관(기본색).

    한쪽 데이터가 없으면 그 줄은 생략. 둘 다 없으면 빈 셀.
    """
    ntce_s = "" if ntce is None else str(ntce).strip()
    dmin_s = "" if dmin is None else str(dmin).strip()
    lines = []
    if ntce_s:
        lines.append(f'<span class="ntcorg" title="{_e(ntce_s)}">{_e(ntce_s)}</span>')
    if dmin_s:
        lines.append(f'<span class="dmnorg" title="{_e(dmin_s)}">{_e(dmin_s)}</span>')
    return f'<td class="insttcell">{"".join(lines)}</td>'


def _sort_header(
    col: str,
    header: str,
    sort: str,
    qs: dict[str, str],
    *,
    base_path: str = "/list",
    sortable: dict[str, str] = _SORTABLE_COLUMNS,
) -> str:
    """정렬 가능한 컬럼 헤더 — 클릭 시 asc↔desc 토글, 현재 정렬 방향을 화살표로 표시.

    링크는 현재 필터(qs)를 보존하고 sort 만 교체한다. page 는 정렬 변경 시 1 로 초기화.

    base_path/sortable 은 하위호환 키워드(기본 = /list 동작 불변). 사전규격 화면은
    base_path="/pre-spec" + 사전규격 정렬맵을 넘겨 동일 로직을 재사용한다.
    """
    base = sortable[col]
    cur_desc = sort == f"{base}_desc"
    cur_asc = sort == f"{base}_asc"
    # 현재 이 컬럼으로 정렬 중이면 반대 방향, 아니면 처음엔 desc(추정가격·일시 모두 큰 값/최신 먼저).
    next_sort = f"{base}_asc" if cur_desc else f"{base}_desc"
    if cur_desc:
        arrow = ' <span class="arrow">▼</span>'
    elif cur_asc:
        arrow = ' <span class="arrow">▲</span>'
    else:
        # 미정렬: 중립 양방향 표시(흐린 색).
        arrow = ' <span class="arrow neutral">↕</span>'
    params = {**qs, "sort": next_sort, "page": "1"}
    query = "&".join(f"{_e(k)}={_e(v)}" for k, v in params.items() if v != "")
    return (
        f'<th><a class="sortcol" href="{base_path}?{query}">{_e(header)}{arrow}</a></th>'
    )


def _render_list_rows(rows: list[dict], sort: str, qs: dict[str, str]) -> str:
    if not rows:
        return '<p class="muted">조건에 맞는 공고가 없습니다.</p>'
    head_cells = []
    for col, _kind, header in _LIST_COLUMNS:
        if col in _SORTABLE_COLUMNS:
            head_cells.append(_sort_header(col, header, sort, qs))
        else:
            head_cells.append(f"<th>{_e(header)}</th>")
    head = "".join(head_cells)
    head += "<th>파일</th><th>분석</th>"
    # 전체 컬럼 수 = # + _LIST_COLUMNS + 파일 + 분석
    total_cols = 1 + len(_LIST_COLUMNS) + 2
    body_rows = []
    for i, r in enumerate(rows, start=1):
        row_id = _e(r.get("bid_ntce_no") or "")
        cells = [f"<td>{i}</td>"]
        for col, kind, _header in _LIST_COLUMNS:
            val = r.get(col)
            if kind == "amt":
                cells.append(f"<td>{_e(_fmt_amt(val))}</td>")
            elif kind == "dt":
                cells.append(f"<td>{_e(_fmt_dt(val))}</td>")
            elif kind == "matchind":
                cells.append(_render_matched_inds(val))
            elif kind == "instt":
                cells.append(_render_instt(val, r.get("dminstt_nm")))
            elif kind == "link":
                url = r.get("bid_ntce_dtl_url")
                text = _e(val)
                if url:
                    cells.append(
                        f'<td><a href="{_e(url)}" target="_blank" rel="noopener">{text}</a></td>'
                    )
                else:
                    cells.append(f"<td>{text}</td>")
            else:
                cells.append(f"<td>{_e(val)}</td>")
        # 파일 컬럼: 첨부 개수>0 이면 drawer 호출 버튼, 없으면 '-'.
        cnt = r.get("_file_count", 0)
        if cnt:
            cells.append(
                f'<td><button type="button" class="filebtn" '
                f'data-no="{row_id}">파일 {cnt}</button></td>'
            )
        else:
            cells.append('<td>-</td>')
        # 분석 버튼 컬럼 (Phase 6.3)
        cells.append(
            f'<td><button type="button" class="btn-analyze" '
            f'data-type="bid" data-id="{row_id}" '
            f'data-colspan="{total_cols}" '
            f'aria-label="제안요청서 분석">분석</button></td>'
        )
        body_rows.append(f"<tr data-row-id=\"{row_id}\">{''.join(cells)}</tr>")
    return f"""
    <div class="table-wrap">
      <table>
        <thead><tr><th>#</th>{head}</tr></thead>
        <tbody>{''.join(body_rows)}</tbody>
      </table>
    </div>"""


def _render_pager(
    total: int,
    page: int,
    page_size: int,
    qs: dict[str, str],
    *,
    base_path: str = "/list",
) -> str:
    """sticky 하단 고정 페이저. 현재 페이지 중심 최대 5개 번호 링크 + … 처리."""
    pages = max(1, (total + page_size - 1) // page_size)
    page = min(max(1, page), pages)

    def page_link(target: int, label: str, *, disabled: bool = False, current: bool = False) -> str:
        if current:
            return f'<span class="cur-page">{_e(label)}</span>'
        params = {**qs, "page": str(target)}
        query = "&".join(f"{_e(k)}={_e(v)}" for k, v in params.items() if v != "")
        cls = " disabled" if disabled else ""
        return f'<a class="pager-btn{cls}" href="{base_path}?{query}">{_e(label)}</a>'

    # 번호 링크: 현재 페이지 중심 최대 5개, 범위 밖은 … 처리.
    num_parts: list[str] = []
    if pages <= 7:
        # 전체 페이지가 7 이하면 모두 표시.
        show_range = range(1, pages + 1)
    else:
        # 현재 페이지 중심 ±2 (= 최대 5개).
        lo = max(1, page - 2)
        hi = min(pages, page + 2)
        # 윈도우가 5개 미만이면 한쪽으로 당김.
        if hi - lo < 4:
            if lo == 1:
                hi = min(pages, lo + 4)
            else:
                lo = max(1, hi - 4)
        show_range = range(lo, hi + 1)

    first_shown = show_range[0] if show_range else 1
    last_shown = show_range[-1] if show_range else pages

    if pages > 7:
        if first_shown > 1:
            num_parts.append(page_link(1, "1"))
            if first_shown > 2:
                num_parts.append('<span class="pager-ellipsis">…</span>')
        for p in show_range:
            num_parts.append(page_link(p, str(p), current=(p == page)))
        if last_shown < pages:
            if last_shown < pages - 1:
                num_parts.append('<span class="pager-ellipsis">…</span>')
            num_parts.append(page_link(pages, str(pages)))
    else:
        for p in show_range:
            num_parts.append(page_link(p, str(p), current=(p == page)))

    nums_html = " ".join(num_parts)

    return f"""
    <div class="pager-wrap">
      <div class="pager">
        {page_link(page - 1, '← 이전', disabled=(page <= 1))}
        {nums_html}
        {page_link(page + 1, '다음 →', disabled=(page >= pages))}
        <span class="pager-info">전체 {total:,}건 · {page} / {pages} 페이지</span>
      </div>
    </div>"""


def _filter_card(
    *,
    action: str,
    summary_html: str,
    detail_html: str,
    title: str = "검색",
    card_id: str = "filterCard",
) -> str:
    """플로팅 필터 카드 헬퍼 (Wave A 신설 — B-1/B-2 에서 list_page/pre_spec_page 에 적용).

    동작:
    - 상단 sticky (position: sticky; top: 0).
    - 기본 접힘(filter-collapsed 클래스) → 접힘 시 summary_html 만 노출.
    - 토글 버튼으로 detail_html 펼침/접힘(인라인 onclick, 클래스 토글).
    - 전체를 <form method="get" action="{action}"> 으로 감싸 한 폼에서 제출.
    - CSS 클래스는 BASE_CSS 에 정의(.filter-card, .filter-collapsed, .filter-toggle, .filter-detail).

    Args:
        action: 폼 제출 경로 (예: "/list", "/pre-spec").
        summary_html: 접힘 상태에서도 항상 보이는 핵심 입력
                      (검색어 input + 검색 버튼 + 펼침/접힘 토글 포함).
        detail_html: 토글로 펼쳐지는 나머지 필터 영역.
        title: 카드 aria-label(기본 "검색").
        card_id: 카드 element id(기본 "filterCard").
    """
    toggle_js = (
        f"var c=document.getElementById('{card_id}');"
        f"c.classList.toggle('filter-collapsed');"
        f"var b=c.querySelector('.filter-toggle');"
        f"b.textContent=c.classList.contains('filter-collapsed')?'▾ 필터 펼침':'▴ 필터 접힘';"
    )
    return (
        f'<div class="filter-card filter-collapsed" id="{_e(card_id)}" aria-label="{_e(title)}">'
        f'<form method="get" action="{_e(action)}">'
        f'<div class="filter-summary">'
        f'{summary_html}'
        f'<button type="button" class="filter-toggle" onclick="{_e(toggle_js)}">▾ 필터 펼침</button>'
        f'</div>'
        f'<div class="filter-detail">{detail_html}</div>'
        f'</form>'
        f'</div>'
    )


# /list 클라이언트 스크립트(setRecent + 파일 drawer). 평문 상수라 중괄호 이스케이프 불필요.
# 파일명은 서버 JSON에서 받아 textContent 로 삽입(XSS 방지). 파일 URL 은 DB 저장 URL 만 사용.
_LIST_SCRIPT = """
  function fmtDate(d) {
    var m = ('0' + (d.getMonth() + 1)).slice(-2);
    var day = ('0' + d.getDate()).slice(-2);
    return d.getFullYear() + '-' + m + '-' + day;
  }
  function setRecent(n) {
    var today = new Date();
    var bgn = new Date(today.getFullYear(), today.getMonth() - n, today.getDate());
    var f = document.getElementById('dt_from');
    var t = document.getElementById('dt_to');
    if (f) f.value = fmtDate(bgn);
    if (t) t.value = fmtDate(today);
  }
  function setToday() {
    var today = new Date();
    var s = fmtDate(today);
    var f = document.getElementById('dt_from');
    var t = document.getElementById('dt_to');
    if (f) f.value = s;
    if (t) t.value = s;
  }

  // 날짜 기준(공고일/개찰일) 변경 시 날짜 입력 기본값을 해당 필드 기본 기간으로 갱신.
  // 공고일: 오늘-1개월 ~ 오늘 / 개찰일: 오늘 ~ 오늘+1개월. (서버도 동일 기본 적용)
  (function () {
    var sel = document.getElementById('date_field');
    if (!sel) return;
    sel.addEventListener('change', function () {
      var today = new Date();
      var f = document.getElementById('dt_from');
      var t = document.getElementById('dt_to');
      if (sel.value === 'openg_dt') {
        var after = new Date(today.getFullYear(), today.getMonth() + 1, today.getDate());
        if (f) f.value = fmtDate(today);
        if (t) t.value = fmtDate(after);
      } else {
        var before = new Date(today.getFullYear(), today.getMonth() - 1, today.getDate());
        if (f) f.value = fmtDate(before);
        if (t) t.value = fmtDate(today);
      }
    });
  })();

  (function () {
    var backdrop = document.getElementById('drawerBackdrop');
    var drawer = document.getElementById('drawer');
    var titleEl = document.getElementById('drawerTitle');
    var listEl = document.getElementById('fileList');
    var zipAll = document.getElementById('zipAll');

    function closeDrawer() {
      drawer.classList.remove('open');
      backdrop.classList.remove('open');
      drawer.setAttribute('aria-hidden', 'true');
    }
    function openDrawer() {
      drawer.classList.add('open');
      backdrop.classList.add('open');
      drawer.setAttribute('aria-hidden', 'false');
    }

    document.getElementById('drawerClose').addEventListener('click', closeDrawer);
    backdrop.addEventListener('click', closeDrawer);
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') closeDrawer();
    });

    document.querySelectorAll('.filebtn').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var no = btn.getAttribute('data-no');
        listEl.innerHTML = '';
        titleEl.textContent = '불러오는 중…';
        zipAll.setAttribute('href', '/list/' + encodeURIComponent(no) + '/files.zip');
        openDrawer();
        fetch('/list/' + encodeURIComponent(no) + '/files')
          .then(function (r) { return r.json(); })
          .then(function (d) {
            titleEl.textContent = d.bid_ntce_nm || no;
            var files = d.files || [];
            if (!files.length) {
              var emptyTr = document.createElement('tr');
              var emptyTd = document.createElement('td');
              emptyTd.colSpan = 2;
              emptyTd.textContent = '첨부가 없습니다.';
              emptyTr.appendChild(emptyTd);
              listEl.appendChild(emptyTr);
              return;
            }
            files.forEach(function (f) {
              var tr = document.createElement('tr');
              var tdName = document.createElement('td');
              tdName.className = 'c-fn';
              // 표시 텍스트는 ellipsis 로 줄이고, title 에 전체 파일명(hover tooltip). XSS 방지 위해 textContent.
              tdName.textContent = f.name;
              tdName.title = f.name;
              var tdDl = document.createElement('td');
              tdDl.className = 'c-dl';
              var a = document.createElement('a');
              a.className = 'dl';
              a.href = f.url;
              a.target = '_blank';
              a.rel = 'noopener';
              a.textContent = '다운로드';
              tdDl.appendChild(a);
              tr.appendChild(tdName);
              tr.appendChild(tdDl);
              listEl.appendChild(tr);
            });
          })
          .catch(function () { titleEl.textContent = '불러오기 실패'; });
      });
    });
  })();
"""

# 분석 UI 스크립트 (Phase 6.3) — 분석 버튼 동작, 결과 패널, 업로드 모달.
# 평문 상수라 중괄호 이스케이프 불필요.
_ANALYSIS_SCRIPT = """
  (function () {
    'use strict';

    var _openBtnEl = null;  // 현재 열린 패널의 버튼 element

    // --- 유틸 -------------------------------------------------------

    function esc(s) {
      if (!s) return '';
      return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
                      .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
    }

    function showToast(message) {
      var t = document.getElementById('analyzeToast');
      if (!t) return;
      t.textContent = message;
      t.style.display = 'block';
      setTimeout(function () { t.style.display = 'none'; }, 4000);
    }

    // --- 결과 패널 --------------------------------------------------

    function closeResultPanel() {
      var existing = document.querySelector('.analysis-result-row');
      if (existing) existing.remove();
      if (_openBtnEl) {
        _openBtnEl.textContent = '분석';
        _openBtnEl.classList.remove('open');
        _openBtnEl.disabled = false;
        _openBtnEl = null;
      }
    }

    function buildList(items, tag) {
      if (!items || !items.length) return '<li>(없음)</li>';
      return items.map(function (s) { return '<' + tag + '>' + esc(s) + '</' + tag + '>'; }).join('');
    }

    function buildEvalTable(criteria) {
      if (!criteria || !criteria.length) return '<p class="muted">없음</p>';
      var rows = criteria.map(function (c) {
        return '<tr><td>' + esc(c.item) + '</td><td>' + esc(c.weight) + '</td><td>' + esc(c.details) + '</td></tr>';
      }).join('');
      return '<table><thead><tr><th>평가항목</th><th>배점</th><th>세부기준</th></tr></thead><tbody>' + rows + '</tbody></table>';
    }

    function buildWinThemeCards(themes) {
      if (!themes || !themes.length) return '<p class="muted">없음</p>';
      return themes.slice(0, 3).map(function (t) {
        return '<div class="win-theme-card"><div class="theme-title">' + esc(t.theme) + '</div>'
             + '<div class="theme-desc">' + esc(t.rationale) + '</div></div>';
      }).join('');
    }

    function buildPainPoints(pts) {
      if (!pts || !pts.length) return '<li>(없음)</li>';
      return pts.map(function (p) {
        var label = typeof p === 'string' ? p : (p.point || '');
        var detail = typeof p === 'object' ? (p.description || '') : '';
        return '<li><b>' + esc(label) + '</b>' + (detail ? ': ' + esc(detail) : '') + '</li>';
      }).join('');
    }

    function showResultPanel(btnEl, analysis) {
      closeResultPanel();  // 기존 패널 닫기

      var a = analysis || {};
      var colspan = btnEl.getAttribute('data-colspan') || '10';

      var panelHtml = '<div class="analysis-panel">'
        + '<div class="analysis-header"><h3>RFP 분석 결과</h3>'
        + '<button type="button" class="analysis-close">× 닫기</button></div>'
        // 섹션 1: 기본 정보
        + '<section class="analysis-section"><h4>프로젝트 기본 정보</h4>'
        + '<div class="info-grid">'
        + (a.project_name ? '<div><span class="label">프로젝트명</span><span>' + esc(a.project_name) + '</span></div>' : '')
        + (a.client_name  ? '<div><span class="label">발주처</span><span>' + esc(a.client_name) + '</span></div>' : '')
        + (a.budget       ? '<div><span class="label">예산</span><span>' + esc(a.budget) + '</span></div>' : '')
        + (a.timeline     ? '<div><span class="label">기간</span><span>' + esc(a.timeline) + '</span></div>' : '')
        + '</div></section>'
        // 섹션 2: 사업 개요
        + '<section class="analysis-section"><h4>사업 개요</h4><p>' + esc(a.project_overview || '') + '</p></section>'
        // 섹션 3: 핵심 요구사항
        + '<section class="analysis-section"><h4>핵심 요구사항</h4><ol>' + buildList(a.key_requirements, 'li') + '</ol></section>'
        // 섹션 4: 평가 기준
        + '<section class="analysis-section"><h4>평가 기준</h4>' + buildEvalTable(a.evaluation_criteria) + '</section>'
        // 섹션 5: 납품물
        + '<section class="analysis-section"><h4>납품물</h4><ul>' + buildList(a.deliverables, 'li') + '</ul></section>'
        // 섹션 6: Win Theme
        + '<section class="analysis-section"><h4>Win Theme 후보</h4><div class="win-theme-cards">' + buildWinThemeCards(a.win_theme_candidates) + '</div></section>'
        // 섹션 7: Pain Points
        + '<section class="analysis-section"><h4>Pain Points</h4><ul>' + buildPainPoints(a.pain_points) + '</ul></section>'
        // 섹션 8: 숨겨진 니즈
        + '<section class="analysis-section"><h4>숨겨진 니즈</h4><ul>' + buildList(a.hidden_needs, 'li') + '</ul></section>'
        + '</div>';

      // 해당 행(data-row-id) 바로 다음에 결과 <tr> 삽입
      var rowEl = btnEl.closest('tr');
      var resultRow = document.createElement('tr');
      resultRow.className = 'analysis-result-row';
      resultRow.innerHTML = '<td colspan="' + colspan + '">' + panelHtml + '</td>';
      rowEl.parentNode.insertBefore(resultRow, rowEl.nextSibling);

      // 닫기 버튼 이벤트
      resultRow.querySelector('.analysis-close').addEventListener('click', function () {
        closeResultPanel();
      });

      // 버튼 상태 변경
      btnEl.textContent = '닫기';
      btnEl.classList.add('open');
      _openBtnEl = btnEl;
    }

    // --- 업로드 모달 ------------------------------------------------

    var _uploadType = '';
    var _uploadId = '';
    var _uploadFile = null;
    var _uploadBtnEl = null;

    function openUploadModal(message, type, id, btnEl) {
      _uploadType = type;
      _uploadId = id;
      _uploadFile = null;
      _uploadBtnEl = btnEl;

      var modal = document.getElementById('analysisUploadModal');
      var msgEl = document.getElementById('uploadModalMessage');
      var filenameEl = document.getElementById('uploadFilename');
      var submitBtn = document.getElementById('uploadSubmitBtn');
      var input = document.getElementById('uploadFileInput');

      if (msgEl) msgEl.textContent = message || '파일을 직접 업로드해 분석하세요.';
      if (filenameEl) filenameEl.textContent = '';
      if (submitBtn) submitBtn.disabled = true;
      if (input) input.value = '';
      _uploadFile = null;

      if (modal) modal.classList.add('open');
    }

    function closeUploadModal() {
      var modal = document.getElementById('analysisUploadModal');
      if (modal) modal.classList.remove('open');
      _uploadFile = null;
      if (_uploadBtnEl) {
        _uploadBtnEl.textContent = '분석';
        _uploadBtnEl.classList.remove('open');
        _uploadBtnEl.disabled = false;
      }
      _uploadBtnEl = null;
    }

    // --- 핵심 분석 함수 ---------------------------------------------

    async function runAnalysis(type, id, btnEl) {
      // 이미 결과 패널이 열려 있으면 토글(닫기)
      if (btnEl.classList.contains('open')) {
        closeResultPanel();
        return;
      }

      btnEl.disabled = true;
      btnEl.textContent = '분석 중...';

      try {
        var resp = await fetch('/api/analysis/' + type + '/' + encodeURIComponent(id), { method: 'POST' });
        var data = await resp.json();

        if (data.status === 'ok') {
          showResultPanel(btnEl, data.analysis);
        } else if (data.status === 'no_file' || data.status === 'unsupported') {
          openUploadModal(data.message, type, id, btnEl);
        } else {
          showToast(data.message || '분석 중 오류가 발생했습니다.');
          btnEl.disabled = false;
          btnEl.textContent = '분석';
        }
      } catch (e) {
        showToast('서버 요청 중 오류가 발생했습니다.');
        btnEl.disabled = false;
        btnEl.textContent = '분석';
      }
    }

    // --- 이벤트 바인딩 (이벤트 위임) --------------------------------

    document.addEventListener('click', function (e) {
      var btn = e.target.closest('.btn-analyze');
      if (btn) {
        var type = btn.getAttribute('data-type');
        var id = btn.getAttribute('data-id');
        runAnalysis(type, id, btn);
      }
    });

    // 업로드 모달 파일 입력
    var uploadInput = document.getElementById('uploadFileInput');
    var dropZone = document.getElementById('uploadDropZone');
    var submitBtn = document.getElementById('uploadSubmitBtn');
    var filenameEl = document.getElementById('uploadFilename');

    function handleFile(file) {
      _uploadFile = file;
      if (filenameEl) filenameEl.textContent = file.name;
      if (submitBtn) submitBtn.disabled = false;
    }

    if (uploadInput) {
      uploadInput.addEventListener('change', function () {
        if (uploadInput.files[0]) handleFile(uploadInput.files[0]);
      });
    }

    if (dropZone) {
      dropZone.addEventListener('click', function () {
        if (uploadInput) uploadInput.click();
      });
      dropZone.addEventListener('dragover', function (e) {
        e.preventDefault();
        dropZone.classList.add('dragover');
      });
      dropZone.addEventListener('dragleave', function () {
        dropZone.classList.remove('dragover');
      });
      dropZone.addEventListener('drop', function (e) {
        e.preventDefault();
        dropZone.classList.remove('dragover');
        var file = e.dataTransfer.files[0];
        if (file) handleFile(file);
      });
    }

    if (submitBtn) {
      submitBtn.addEventListener('click', async function () {
        if (!_uploadFile) return;
        submitBtn.disabled = true;
        submitBtn.textContent = '업로드 중...';

        var form = new FormData();
        form.append('file', _uploadFile);

        try {
          var resp = await fetch('/api/analysis/upload', { method: 'POST', body: form });
          var data = await resp.json();

          if (data.status === 'ok') {
            var modal = document.getElementById('analysisUploadModal');
            if (modal) modal.classList.remove('open');
            // 업로드 후 결과 패널 표시: _uploadBtnEl 이 없으면 임시 처리
            var targetBtn = _uploadBtnEl;
            _uploadBtnEl = null;
            if (targetBtn) {
              targetBtn.disabled = false;
              showResultPanel(targetBtn, data.analysis);
            } else {
              showToast('분석 완료!');
            }
          } else if (data.status === 'unsupported' || data.status === 'no_file') {
            var msgEl = document.getElementById('uploadModalMessage');
            if (msgEl) msgEl.textContent = data.message || '지원하지 않는 파일 형식입니다.';
            submitBtn.disabled = false;
            submitBtn.textContent = '분석 시작';
          } else {
            var msgEl = document.getElementById('uploadModalMessage');
            if (msgEl) msgEl.textContent = data.message || '분석 중 오류가 발생했습니다.';
            submitBtn.disabled = false;
            submitBtn.textContent = '분석 시작';
          }
        } catch (e) {
          var msgEl = document.getElementById('uploadModalMessage');
          if (msgEl) msgEl.textContent = '서버 요청 중 오류가 발생했습니다.';
          submitBtn.disabled = false;
          submitBtn.textContent = '분석 시작';
        }
      });
    }

    // 모달 닫기 버튼들
    document.querySelectorAll('.modal-close-btn, .modal-cancel').forEach(function (btn) {
      btn.addEventListener('click', closeUploadModal);
    });

    // 모달 오버레이 클릭 시 닫기
    var modalOverlay = document.getElementById('analysisUploadModal');
    if (modalOverlay) {
      modalOverlay.addEventListener('click', function (e) {
        if (e.target === modalOverlay) closeUploadModal();
      });
    }

    // ESC 키로 모달 닫기
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') {
        var modal = document.getElementById('analysisUploadModal');
        if (modal && modal.classList.contains('open')) closeUploadModal();
      }
    });

  })();
"""

# 업로드 모달 + 토스트 HTML (list·pre-spec 공용, 스크립트 앞에 한 번만 삽입)
_ANALYSIS_MODAL_HTML = """
    <!-- 분석 업로드 모달 (Phase 6.3) -->
    <div id="analysisUploadModal" class="modal-overlay" aria-label="제안요청서 업로드" role="dialog">
      <div class="modal-content">
        <div class="modal-header">
          <h3>제안요청서 직접 업로드</h3>
          <button type="button" class="modal-close-btn" aria-label="닫기">&times;</button>
        </div>
        <div class="modal-body">
          <p class="modal-message" id="uploadModalMessage">파일을 직접 업로드해 분석하세요.</p>
          <div class="upload-area" id="uploadDropZone" role="button" tabindex="0"
               aria-label="파일 선택 영역">
            <p>PDF, HWP, HWPX, DOC, DOCX 파일을 끌어다 놓거나 클릭해 선택</p>
            <p class="upload-hint">최대 50MB</p>
            <input type="file" id="uploadFileInput"
                   accept=".pdf,.hwp,.hwpx,.doc,.docx" hidden>
          </div>
          <p class="upload-filename" id="uploadFilename"></p>
        </div>
        <div class="modal-footer">
          <button type="button" id="uploadSubmitBtn" class="modal-submit" disabled>분석 시작</button>
          <button type="button" class="modal-cancel">취소</button>
        </div>
      </div>
    </div>
    <!-- 에러 토스트 (Phase 6.3) -->
    <div id="analyzeToast" role="alert"></div>
"""


def _render_pre_spec_rows(rows: list[dict], sort: str, qs: dict[str, str]) -> str:
    """사전규격 목록 테이블 렌더(_render_list_rows 패턴). 파일 컬럼 포함(4.9-B2)."""
    if not rows:
        return '<p class="muted">조건에 맞는 사전규격이 없습니다.</p>'
    head_cells = []
    for col, _kind, header in _PRE_SPEC_COLUMNS:
        if col in _PRE_SPEC_SORTABLE_COLUMNS:
            head_cells.append(
                _sort_header(
                    col,
                    header,
                    sort,
                    qs,
                    base_path="/pre-spec",
                    sortable=_PRE_SPEC_SORTABLE_COLUMNS,
                )
            )
        else:
            head_cells.append(f"<th>{_e(header)}</th>")
    head = "".join(head_cells)
    head += "<th>파일</th><th>분석</th>"
    # 전체 컬럼 수 = # + _PRE_SPEC_COLUMNS + 파일 + 분석
    total_cols = 1 + len(_PRE_SPEC_COLUMNS) + 2
    body_rows = []
    for i, r in enumerate(rows, start=1):
        row_id = _e(r.get("bf_spec_rgst_no") or "")
        cells = [f"<td>{i}</td>"]
        for col, kind, _header in _PRE_SPEC_COLUMNS:
            val = r.get(col)
            if kind == "amt":
                cells.append(f"<td>{_e(_fmt_amt(val))}</td>")
            elif kind == "dt":
                cells.append(f"<td>{_e(_fmt_dt(val))}</td>")
            elif kind == "instt2":
                # 위=발주기관(order_instt_nm), 아래=실수요기관(rl_dminstt_nm). 입찰 instt 셀 재사용.
                cells.append(_render_instt(val, r.get("rl_dminstt_nm")))
            else:
                # 품명/사업명은 평문 + 전체값 tooltip(긴 값 ellipsis 대비).
                text = "" if val is None else str(val)
                cells.append(f'<td title="{_e(text)}">{_e(text)}</td>')
        # 파일 컬럼: 첨부 개수>0 이면 drawer 호출 버튼, 없으면 '-'.
        cnt = r.get("_file_count", 0)
        if cnt:
            cells.append(
                f'<td><button type="button" class="filebtn" '
                f'data-no="{row_id}">파일 {cnt}</button></td>'
            )
        else:
            cells.append('<td>-</td>')
        # 분석 버튼 컬럼 (Phase 6.3)
        cells.append(
            f'<td><button type="button" class="btn-analyze" '
            f'data-type="pre-spec" data-id="{row_id}" '
            f'data-colspan="{total_cols}" '
            f'aria-label="제안요청서 분석">분석</button></td>'
        )
        body_rows.append(f"<tr data-row-id=\"{row_id}\">{''.join(cells)}</tr>")
    return f"""
    <div class="table-wrap">
      <table>
        <thead><tr><th>#</th>{head}</tr></thead>
        <tbody>{''.join(body_rows)}</tbody>
      </table>
    </div>"""


# /pre-spec 클라이언트 스크립트(날짜 버튼 + 파일 drawer). 평문 상수라 중괄호 이스케이프 불필요.
# 파일명은 서버 JSON에서 받아 textContent 로 삽입(XSS 방지). 파일 URL 은 DB 저장 URL 만 사용.
_PRE_SPEC_SCRIPT = """
  function fmtDate(d) {
    var m = ('0' + (d.getMonth() + 1)).slice(-2);
    var day = ('0' + d.getDate()).slice(-2);
    return d.getFullYear() + '-' + m + '-' + day;
  }
  function setRecent(n) {
    var today = new Date();
    var bgn = new Date(today.getFullYear(), today.getMonth() - n, today.getDate());
    var f = document.getElementById('dt_from');
    var t = document.getElementById('dt_to');
    if (f) f.value = fmtDate(bgn);
    if (t) t.value = fmtDate(today);
  }
  function setToday() {
    var today = new Date();
    var f = document.getElementById('dt_from');
    var t = document.getElementById('dt_to');
    if (f) f.value = fmtDate(today);
    if (t) t.value = fmtDate(today);
  }

  (function () {
    var backdrop = document.getElementById('psDrawerBackdrop');
    var drawer = document.getElementById('psDrawer');
    var titleEl = document.getElementById('psDrawerTitle');
    var listEl = document.getElementById('psFileList');
    var zipAll = document.getElementById('psZipAll');

    function closeDrawer() {
      drawer.classList.remove('open');
      backdrop.classList.remove('open');
      drawer.setAttribute('aria-hidden', 'true');
    }
    function openDrawer() {
      drawer.classList.add('open');
      backdrop.classList.add('open');
      drawer.setAttribute('aria-hidden', 'false');
    }

    document.getElementById('psDrawerClose').addEventListener('click', closeDrawer);
    backdrop.addEventListener('click', closeDrawer);
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') closeDrawer();
    });

    document.querySelectorAll('.filebtn').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var no = btn.getAttribute('data-no');
        listEl.innerHTML = '';
        titleEl.textContent = '불러오는 중…';
        zipAll.setAttribute('href', '/pre-spec/' + encodeURIComponent(no) + '/files.zip');
        openDrawer();
        fetch('/pre-spec/' + encodeURIComponent(no) + '/files')
          .then(function (r) { return r.json(); })
          .then(function (d) {
            titleEl.textContent = d.bf_spec_rgst_no || no;
            var files = d.files || [];
            if (!files.length) {
              var emptyTr = document.createElement('tr');
              var emptyTd = document.createElement('td');
              emptyTd.colSpan = 2;
              emptyTd.textContent = '첨부가 없습니다.';
              emptyTr.appendChild(emptyTd);
              listEl.appendChild(emptyTr);
              return;
            }
            files.forEach(function (f) {
              var tr = document.createElement('tr');
              var tdName = document.createElement('td');
              tdName.className = 'c-fn';
              // 표시 텍스트는 ellipsis 로 줄이고, title 에 전체 파일명(hover tooltip). XSS 방지 위해 textContent.
              tdName.textContent = f.name;
              tdName.title = f.name;
              var tdDl = document.createElement('td');
              tdDl.className = 'c-dl';
              var a = document.createElement('a');
              a.className = 'dl';
              a.href = f.url;
              a.target = '_blank';
              a.rel = 'noopener';
              a.textContent = '다운로드';
              tdDl.appendChild(a);
              tr.appendChild(tdName);
              tr.appendChild(tdDl);
              listEl.appendChild(tr);
            });
          })
          .catch(function () { titleEl.textContent = '불러오기 실패'; });
      });
    });
  })();
"""


@app.get("/pre-spec", response_class=HTMLResponse)
def pre_spec_page(
    q: str | None = None,
    dt_from: str | None = None,
    dt_to: str | None = None,
    instt: str | None = None,
    price_min: str | None = None,    # 배정예산액 최소(Phase 4.9-R2-D)
    price_max: str | None = None,    # 배정예산액 최대(Phase 4.9-R2-D)
    include_past: str | None = None,   # 체크 시 의견마감 지난 항목 포함
    sort: str = "rcpt_dt_desc",
    page: int = 1,
    page_size: int = 50,
) -> HTMLResponse:
    # page_size 허용값: {10, 30, 50, 100}. 그 외는 50으로 폴백.
    if page_size not in (10, 30, 50, 100):
        page_size = 50
    # 기본 동작 = 의견마감 지난 항목 숨김. "지난 마감 포함" 체크 시에만 전체 노출(NULL 은 항상 표시).
    include_past_flag = include_past in ("1", "on", "true", "Y", "y")
    # 정렬: 헤더 클릭 6종. 미허용·빈값은 기본(최신 접수일).
    sort = sort if sort in _PRE_SPEC_SORTS else _PRE_SPEC_DEFAULT_SORT
    try:
        page = max(1, int(page))
    except (TypeError, ValueError):
        page = 1

    # 접수일 기본 기간: 쿼리에 값이 없을 때만 최근 1개월(오늘-1개월 ~ 오늘)을 채운다(사용자 입력 우선).
    today = date.today()
    def_from = _months_ago(today, 1).isoformat()
    def_to = today.isoformat()
    dt_from_eff = dt_from if (dt_from and dt_from.strip()) else def_from
    dt_to_eff = dt_to if (dt_to and dt_to.strip()) else def_to

    df = _parse_date(dt_from_eff)
    dtto = _parse_date(dt_to_eff, end_of_day=True)

    # 가격: 화면 입력값 우선, 비었으면 설정 기본값(cfg.pre_spec_amt_bgn/end).
    # list_page 의 cfg 기본값 로직과 동형.
    with SessionLocal() as session:
        cfg = repository.get_config(session)
        cfg_amt_bgn = cfg.pre_spec_amt_bgn
        cfg_amt_end = cfg.pre_spec_amt_end

        price_min_in = price_min if (price_min is not None and str(price_min).strip()) else None
        price_max_in = price_max if (price_max is not None and str(price_max).strip()) else None
        # 입력칸에 표시할 값(숫자 정규화). 입력이 없으면 설정 기본값.
        price_min_disp = _parse_price(price_min_in) if price_min_in is not None else _parse_price(cfg_amt_bgn)
        price_max_disp = _parse_price(price_max_in) if price_max_in is not None else _parse_price(cfg_amt_end)

        objs, total = repository.search_pre_specs(
            session,
            q=q,
            instt=instt,
            dt_from=df,
            dt_to=dtto,
            price_min=price_min_disp,
            price_max=price_max_disp,
            include_past_opnin=include_past_flag,
            sort=sort,
            page=page,
            page_size=page_size,
        )
        # 세션 종료 후 lazy 접근 금지 → 필요한 스칼라만 dict 로 추출.
        # 첨부 URL 컬럼은 파일 개수 계산용으로만 읽고(이미 조회한 ORM 행에서 → N+1 없음),
        # 화면에는 개수만 전달한다(drawer 열 때 /pre-spec/.../files 로 상세 조회).
        cols = (
            [c for c, _, _ in _PRE_SPEC_COLUMNS]
            + ["rl_dminstt_nm"]
            + _PRE_SPEC_FILE_URL_COLUMNS
        )
        rows = []
        for o in objs:
            d = {c: getattr(o, c, None) for c in cols}
            d["_file_count"] = sum(1 for c in _PRE_SPEC_FILE_URL_COLUMNS if d.get(c))
            rows.append(d)

    # 입력칸 표시값(문자열). 정규화된 정수를 그대로 보여준다(없으면 빈칸).
    price_min_field = "" if price_min_disp is None else str(price_min_disp)
    price_max_field = "" if price_max_disp is None else str(price_max_disp)

    # 쿼리스트링 보존(페이지 이동·헤더 정렬 시 다른 필터 유지). 유효 날짜(서버 기본 포함)를 싣는다.
    qs = {
        "q": q or "",
        "dt_from": dt_from_eff or "",
        "dt_to": dt_to_eff or "",
        "instt": instt or "",
        "price_min": price_min_field,
        "price_max": price_max_field,
        "include_past": "1" if include_past_flag else "",
        "sort": sort,
        "page_size": str(page_size),
    }

    # 표기개수 select — 10/30/50/100 옵션
    ps_options = "".join(
        f'<option value="{v}"{" selected" if v == page_size else ""}>{v}건</option>'
        for v in (10, 30, 50, 100)
    )

    # _filter_card summary: 품명/사업명 검색어 + 검색 버튼.
    summary_html = f"""
      <label class="field" style="min-width:260px;">
        <span class="flabel">품명/사업명 부분검색</span>
        <input type="text" name="q" value="{_e(q or '')}" placeholder="예: 소프트웨어 유지보수">
      </label>
      <button type="submit" class="submit">검색</button>"""

    # _filter_card detail: 기관·날짜·가격·지난마감·표기개수.
    detail_html = f"""
      <div class="row" style="margin-bottom:10px;">
        <label class="field" style="min-width:220px;">
          <span class="flabel">발주/실수요기관 부분검색</span>
          <input type="text" name="instt" value="{_e(instt or '')}" placeholder="예: 행정안전부">
        </label>
        <label class="field">
          <span class="flabel">접수 시작</span>
          <input type="date" name="dt_from" id="dt_from" value="{_e(dt_from_eff or '')}">
        </label>
        <span class="tilde">~</span>
        <label class="field">
          <span class="flabel">접수 종료</span>
          <input type="date" name="dt_to" id="dt_to" value="{_e(dt_to_eff or '')}">
        </label>
        <div class="quick">
          <button type="button" onclick="setRecent(1)">최근1개월</button>
          <button type="button" onclick="setRecent(3)">최근3개월</button>
          <button type="button" onclick="setRecent(6)">최근6개월</button>
          <button type="button" onclick="setToday()">1일</button>
        </div>
      </div>
      <div class="row" style="margin-bottom:10px;">
        <label class="field">
          <span class="flabel">배정예산액 최소</span>
          <input type="number" name="price_min" id="ps_price_min" value="{_e(price_min_field)}" placeholder="예: 10000000" min="0">
        </label>
        <span class="tilde">~</span>
        <label class="field">
          <span class="flabel">배정예산액 최대</span>
          <input type="number" name="price_max" id="ps_price_max" value="{_e(price_max_field)}" placeholder="예: 500000000" min="0">
        </label>
      </div>
      <div class="row">
        <label class="chk">
          <input type="checkbox" name="include_past" value="1"{' checked' if include_past_flag else ''}>
          지난 마감 포함
        </label>
        <label class="field">
          <span class="flabel">표기개수</span>
          <select name="page_size" onchange="this.form.submit()">{ps_options}</select>
        </label>
        <input type="hidden" name="sort" value="{_e(sort)}">
      </div>"""

    filter_card = _filter_card(
        action="/pre-spec",
        summary_html=summary_html,
        detail_html=detail_html,
        title="사전규격 검색",
        card_id="filterCard",
    )

    body = f"""
    {filter_card}

    <div class="card">
      <h2>수집된 사전규격</h2>
      {_render_pre_spec_rows(rows, sort, qs)}
      {_render_pager(total, page, page_size, qs, base_path="/pre-spec")}
    </div>

    <div id="psDrawerBackdrop" class="drawer-backdrop"></div>
    <aside id="psDrawer" class="drawer" aria-hidden="true">
      <div class="dz-head">
        <button type="button" class="dz-close" id="psDrawerClose" aria-label="닫기">&times;</button>
        <div class="dz-title" id="psDrawerTitle">첨부 파일</div>
      </div>
      <div class="dz-body">
        <a id="psZipAll" class="zipall" href="#">전체 다운로드 (.zip)</a>
        <table class="filetable">
          <thead><tr><th class="c-fn">파일명</th><th class="c-dl">다운로드</th></tr></thead>
          <tbody id="psFileList"></tbody>
        </table>
      </div>
    </aside>

    {_ANALYSIS_MODAL_HTML}
    <script>{_PRE_SPEC_SCRIPT}</script>
    <script>{_ANALYSIS_SCRIPT}</script>"""

    return HTMLResponse(
        _shell("나라장터 사전규격 목록", "수집·저장된 사전규격을 조회합니다.", "pre-spec", body)
    )


@app.get("/", response_class=HTMLResponse)
def root() -> RedirectResponse:
    return RedirectResponse(url="/list", status_code=307)


def _parse_price(s: str | None) -> int | None:
    """가격 입력(문자열, 콤마 허용) → int. 빈값/형식오류는 None."""
    if not s:
        return None
    s = str(s).strip().replace(",", "")
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        try:
            # "1234.0" 같은 소수 표기 방어(설정값이 그렇게 저장될 수 있음).
            return int(float(s))
        except ValueError:
            return None


@app.get("/list", response_class=HTMLResponse)
def list_page(
    q: str | None = None,
    dt_from: str | None = None,
    dt_to: str | None = None,
    date_field: str = "bid_ntce_dt",
    price_min: str | None = None,
    price_max: str | None = None,
    include_past: str | None = None,
    sort: str = "bid_ntce_dt_desc",
    page: int = 1,
    page_size: int = 50,
) -> HTMLResponse:
    # page_size 정규화: 허용값 {10,30,50,100}, 그 외/오류는 50.
    page_size = page_size if page_size in (10, 30, 50, 100) else 50
    # 기본 동작 = 개찰 지난 공고 숨김. "지난 개찰 포함" 체크 시에만 전체 노출.
    include_past_flag = include_past in ("1", "on", "true", "Y", "y")
    # 정렬: 헤더 클릭 6종. 미허용·빈값은 기본(최신 공고일).
    sort = sort if sort in _LIST_SORTS else _DEFAULT_SORT
    # 날짜 적용 컬럼: 공고일/개찰일.
    date_field = date_field if date_field in ("bid_ntce_dt", "openg_dt") else "bid_ntce_dt"
    try:
        page = max(1, int(page))
    except (TypeError, ValueError):
        page = 1

    # 날짜 기본값: 쿼리에 값이 없을 때만 필드별 기본 기간을 채운다(사용자 입력 우선).
    def_from, def_to = _list_default_date_range(date_field)
    dt_from_eff = dt_from if (dt_from and dt_from.strip()) else def_from
    dt_to_eff = dt_to if (dt_to and dt_to.strip()) else def_to

    df = _parse_date(dt_from_eff)
    dtto = _parse_date(dt_to_eff, end_of_day=True)

    # 가격: 화면 입력값 우선, 비었으면 설정 기본값(presmpt_prce_bgn/end).
    with SessionLocal() as session:
        cfg = repository.get_config(session)
        cfg_price_bgn = cfg.presmpt_prce_bgn
        cfg_price_end = cfg.presmpt_prce_end

        price_min_in = price_min if (price_min is not None and str(price_min).strip()) else None
        price_max_in = price_max if (price_max is not None and str(price_max).strip()) else None
        # 입력칸에 표시할 값(숫자 정규화). 입력이 없으면 설정 기본값.
        price_min_disp = _parse_price(price_min_in) if price_min_in is not None else _parse_price(cfg_price_bgn)
        price_max_disp = _parse_price(price_max_in) if price_max_in is not None else _parse_price(cfg_price_end)

        objs, total = repository.search_bid_notices(
            session,
            q=q,
            dt_from=df,
            dt_to=dtto,
            date_field=date_field,
            price_min=price_min_disp,
            price_max=price_max_disp,
            include_past_openg=include_past_flag,
            sort=sort,
            page=page,
            page_size=page_size,
        )
        # 세션 종료 후 lazy 접근 금지 → 필요한 스칼라만 dict 로 추출.
        # 첨부 URL 컬럼은 파일 개수 계산용으로만 읽고(이미 조회한 ORM 행에서 → N+1 없음),
        # 화면에는 개수만 전달한다(drawer 열 때 /files 로 상세 조회).
        cols = (
            [c for c, _, _ in _LIST_COLUMNS]
            + ["dminstt_nm", "bid_ntce_dtl_url"]
            + _SPEC_URL_COLUMNS
        )
        rows = []
        for o in objs:
            d = {c: getattr(o, c, None) for c in cols}
            d["_file_count"] = sum(1 for c in _SPEC_URL_COLUMNS if d.get(c))
            rows.append(d)

    # 입력칸 표시값(문자열). 정규화된 정수를 그대로 보여준다(없으면 빈칸).
    price_min_field = "" if price_min_disp is None else str(price_min_disp)
    price_max_field = "" if price_max_disp is None else str(price_max_disp)

    # 쿼리스트링 보존(페이지 이동·헤더 정렬 시 다른 필터 유지). 유효 날짜(서버 기본 포함)를 싣는다.
    qs = {
        "q": q or "",
        "dt_from": dt_from_eff or "",
        "dt_to": dt_to_eff or "",
        "date_field": date_field,
        "price_min": price_min_field,
        "price_max": price_max_field,
        "include_past": "1" if include_past_flag else "",
        "sort": sort,
        "page_size": str(page_size),
    }

    nf_sel = " selected" if date_field == "bid_ntce_dt" else ""
    og_sel = " selected" if date_field == "openg_dt" else ""

    # 표기개수 select 옵션
    ps_opts = "".join(
        f'<option value="{v}"{" selected" if v == page_size else ""}>{v}건</option>'
        for v in (10, 30, 50, 100)
    )

    # summary_html: 접힘 상태에서도 항상 보이는 검색어 입력 + 검색 버튼
    summary_html = f"""
      <label class="field" style="min-width:260px;">
        <span class="flabel">공고명 부분검색</span>
        <input type="text" name="q" value="{_e(q or '')}" placeholder="예: 소프트웨어 유지보수">
      </label>
      <button type="submit" class="submit">검색</button>"""

    # detail_html: 토글로 펼쳐지는 상세 필터
    detail_html = f"""
      <div class="row">
        <label class="field">
          <span class="flabel">날짜 기준</span>
          <select name="date_field" id="date_field">
            <option value="bid_ntce_dt"{nf_sel}>공고일</option>
            <option value="openg_dt"{og_sel}>개찰일</option>
          </select>
        </label>
        <label class="field">
          <span class="flabel">시작</span>
          <input type="date" name="dt_from" id="dt_from" value="{_e(dt_from_eff or '')}">
        </label>
        <span class="tilde">~</span>
        <label class="field">
          <span class="flabel">종료</span>
          <input type="date" name="dt_to" id="dt_to" value="{_e(dt_to_eff or '')}">
        </label>
        <div class="quick">
          <button type="button" onclick="setToday()">1일</button>
          <button type="button" onclick="setRecent(1)">최근1개월</button>
          <button type="button" onclick="setRecent(3)">최근3개월</button>
          <button type="button" onclick="setRecent(6)">최근6개월</button>
        </div>
      </div>
      <div class="row" style="margin-top:12px;">
        <label class="field">
          <span class="flabel">추정가격 최소</span>
          <input type="number" name="price_min" id="price_min" value="{_e(price_min_field)}" placeholder="예: 10000000" min="0">
        </label>
        <span class="tilde">~</span>
        <label class="field">
          <span class="flabel">추정가격 최대</span>
          <input type="number" name="price_max" id="price_max" value="{_e(price_max_field)}" placeholder="예: 500000000" min="0">
        </label>
      </div>
      <div class="row" style="margin-top:12px;">
        <label class="chk">
          <input type="checkbox" name="include_past" value="1"{' checked' if include_past_flag else ''}>
          지난 개찰 포함
        </label>
        <label class="field">
          <span class="flabel">표기개수</span>
          <select name="page_size" onchange="this.form.submit()">{ps_opts}</select>
        </label>
        <input type="hidden" name="sort" value="{_e(sort)}">
      </div>"""

    body = f"""
    {_filter_card(action="/list", summary_html=summary_html, detail_html=detail_html, title="공고 검색", card_id="filterCard")}

    <div class="card">
      <h2>수집된 공고</h2>
      {_render_list_rows(rows, sort, qs)}
      {_render_pager(total, page, page_size, qs)}
    </div>

    <div id="drawerBackdrop" class="drawer-backdrop"></div>
    <aside id="drawer" class="drawer" aria-hidden="true">
      <div class="dz-head">
        <button type="button" class="dz-close" id="drawerClose" aria-label="닫기">&times;</button>
        <div class="dz-title" id="drawerTitle">첨부 파일</div>
      </div>
      <div class="dz-body">
        <a id="zipAll" class="zipall" href="#">전체 다운로드 (.zip)</a>
        <table class="filetable">
          <thead><tr><th class="c-fn">파일명</th><th class="c-dl">다운로드</th></tr></thead>
          <tbody id="fileList"></tbody>
        </table>
      </div>
    </aside>

    {_ANALYSIS_MODAL_HTML}
    <script>{_LIST_SCRIPT}</script>
    <script>{_ANALYSIS_SCRIPT}</script>"""

    return HTMLResponse(
        _shell("나라장터 입찰공고 목록", "수집·저장된 공고를 조회합니다.", "list", body)
    )


# --- /list 파일 다운로드 (Phase 4.1) ---------------------------------
# zip 스트리밍 가드: 외부 다운로드 타임아웃·누적 총량 상한(메모리 보호).
_FILE_HTTP_TIMEOUT = 30.0
_ZIP_TOTAL_LIMIT_BYTES = 300 * 1024 * 1024  # 300MB 누적 상한


@app.get("/list/{bid_ntce_no}/files")
def list_files(bid_ntce_no: str):
    """공고의 첨부 목록 JSON. drawer 가 fetch 한다. URL 은 DB 저장값만 노출(SSRF 방지)."""
    with SessionLocal() as session:
        notice = session.get(BidNotice, bid_ntce_no)
        if notice is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        nm = notice.bid_ntce_nm
        files = repository.get_notice_files(session, bid_ntce_no)
    return JSONResponse(
        {
            "bid_ntce_no": bid_ntce_no,
            "bid_ntce_nm": nm,
            "files": [{"name": f["name"], "url": f["url"]} for f in files],
        }
    )


@app.get("/list/{bid_ntce_no}/files.zip")
async def list_files_zip(bid_ntce_no: str):
    """공고 첨부를 서버가 받아 zip 으로 묶어 반환.

    - URL 은 **DB 에 저장된 그 공고의 첨부 URL만** 사용(임의 URL 프록시 금지 = SSRF 방지).
    - 실패 파일은 skip(부분 성공). 성공 0건이면 안내. 파일명 충돌·빈값은 인덱스 접두로 회피.
    - 리다이렉트 따라감·타임아웃·누적 총량 상한 적용. 한글 파일명 UTF-8.
    """
    with SessionLocal() as session:
        notice = session.get(BidNotice, bid_ntce_no)
        if notice is None:
            return Response("공고를 찾을 수 없습니다.", status_code=404)
        files = repository.get_notice_files(session, bid_ntce_no)
    if not files:
        return Response("첨부가 없습니다.", status_code=404)

    buf = io.BytesIO()
    ok = 0
    total_bytes = 0
    used_names: set[str] = set()
    async with httpx.AsyncClient(follow_redirects=True, timeout=_FILE_HTTP_TIMEOUT) as client:
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in files:
                if total_bytes >= _ZIP_TOTAL_LIMIT_BYTES:
                    logger.warning(
                        "zip 누적 총량 상한 도달 — 이후 첨부 skip: no=%s", bid_ntce_no
                    )
                    break
                try:
                    resp = await client.get(f["url"])
                    resp.raise_for_status()
                    content = resp.content
                except Exception:  # noqa: BLE001 — 실패 파일은 건너뛰고 부분 성공.
                    logger.warning(
                        "첨부 다운로드 실패 skip: no=%s idx=%s", bid_ntce_no, f["idx"]
                    )
                    continue
                # 파일명 충돌·빈값 방지: 인덱스 접두.
                base = f["name"] or f"첨부{f['idx']}"
                arc = f"{f['idx']}_{base}"
                if arc in used_names:
                    arc = f"{f['idx']}_{ok}_{base}"
                used_names.add(arc)
                zf.writestr(arc, content)
                total_bytes += len(content)
                ok += 1

    if ok == 0:
        return Response("첨부 다운로드에 모두 실패했습니다.", status_code=502)

    filename = quote(f"{bid_ntce_no}.zip")
    headers = {"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"}
    return Response(content=buf.getvalue(), media_type="application/zip", headers=headers)


# --- /pre-spec 파일 다운로드 (Phase 4.9-B2) --------------------------
# 입찰 패턴 이식. SSRF 가드(DB 저장 URL 만)·타임아웃·누적 상한 동일.

@app.get("/pre-spec/{bf_spec_rgst_no}/files")
def pre_spec_files(bf_spec_rgst_no: str):
    """사전규격 첨부 목록 JSON. drawer 가 fetch 한다. URL 은 DB 저장값만 노출(SSRF 방지)."""
    with SessionLocal() as session:
        spec = session.get(PreSpec, bf_spec_rgst_no)
        if spec is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        files = repository.get_pre_spec_files(session, bf_spec_rgst_no)
    return JSONResponse(
        {
            "bf_spec_rgst_no": bf_spec_rgst_no,
            "files": [{"name": f["name"], "url": f["url"]} for f in files],
        }
    )


@app.get("/pre-spec/{bf_spec_rgst_no}/files.zip")
async def pre_spec_files_zip(bf_spec_rgst_no: str):
    """사전규격 첨부를 서버가 받아 zip 으로 묶어 반환.

    - URL 은 **DB 에 저장된 그 사전규격의 첨부 URL만** 사용(임의 URL 프록시 금지 = SSRF 방지).
    - 실패 파일은 skip(부분 성공). 성공 0건이면 안내. 파일명 충돌·빈값은 인덱스 접두로 회피.
    - 리다이렉트 따라감·타임아웃·누적 총량 상한 적용. 한글 파일명 UTF-8.
    """
    with SessionLocal() as session:
        spec = session.get(PreSpec, bf_spec_rgst_no)
        if spec is None:
            return Response("사전규격을 찾을 수 없습니다.", status_code=404)
        files = repository.get_pre_spec_files(session, bf_spec_rgst_no)
    if not files:
        return Response("첨부가 없습니다.", status_code=404)

    buf = io.BytesIO()
    ok = 0
    total_bytes = 0
    used_names: set[str] = set()
    async with httpx.AsyncClient(follow_redirects=True, timeout=_FILE_HTTP_TIMEOUT) as client:
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in files:
                if total_bytes >= _ZIP_TOTAL_LIMIT_BYTES:
                    logger.warning(
                        "zip 누적 총량 상한 도달 — 이후 첨부 skip: no=%s", bf_spec_rgst_no
                    )
                    break
                try:
                    resp = await client.get(f["url"])
                    resp.raise_for_status()
                    content = resp.content
                except Exception:  # noqa: BLE001 — 실패 파일은 건너뛰고 부분 성공.
                    logger.warning(
                        "첨부 다운로드 실패 skip: no=%s idx=%s", bf_spec_rgst_no, f["idx"]
                    )
                    continue
                # 파일명 충돌·빈값 방지: 인덱스 접두.
                base = f["name"] or f"첨부{f['idx']}"
                arc = f"{f['idx']}_{base}"
                if arc in used_names:
                    arc = f"{f['idx']}_{ok}_{base}"
                used_names.add(arc)
                zf.writestr(arc, content)
                total_bytes += len(content)
                ok += 1

    if ok == 0:
        return Response("첨부 다운로드에 모두 실패했습니다.", status_code=502)

    filename = quote(f"{bf_spec_rgst_no}.zip")
    headers = {"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"}
    return Response(content=buf.getvalue(), media_type="application/zip", headers=headers)


# --- 분석 API (Phase 6.2a) -------------------------------------------

_ANALYSIS_MAX_BYTES = 50 * 1024 * 1024  # 업로드 최대 50MB


@app.post("/api/analysis/pre-spec/{bf_spec_rgst_no}", response_model=AnalysisResponse)
async def analysis_pre_spec(bf_spec_rgst_no: str):
    """사전규격 파일 URL → RFP 분석.

    DB에서 `bf_spec_rgst_no`로 첨부 파일 URL 목록 조회 후
    지원 형식(.pdf/.hwp/.hwpx/.doc/.docx)을 첫 번째부터 탐색·분석한다.
    """
    with SessionLocal() as session:
        spec = session.get(PreSpec, bf_spec_rgst_no)
        if spec is None:
            return JSONResponse(
                {"status": "no_file", "analysis": None, "message": "사전규격을 찾을 수 없습니다."},
                status_code=404,
            )
        files = repository.get_pre_spec_files(session, bf_spec_rgst_no)

    # 지원 형식 URL 순서대로 탐색
    target_url: str | None = None
    for f in files:
        url = f.get("url", "")
        from pathlib import Path as _Path
        ext = _Path(url.split("?")[0]).suffix.lower()
        if ext in SUPPORTED_EXTENSIONS:
            target_url = url
            break

    if target_url is None:
        return AnalysisResponse(
            status="no_file",
            analysis=None,
            message="제안요청서 파일을 찾을 수 없습니다. 파일을 직접 업로드해주세요.",
        )

    result: AnalysisResult = await analyze_from_url(target_url)

    if result.status == "unsupported":
        return AnalysisResponse(
            status="unsupported",
            analysis=None,
            message=result.message or "파일 변환에 실패했습니다. PDF 또는 DOCX를 업로드해주세요.",
        )

    analysis_dict = result.analysis.model_dump() if result.analysis is not None else None
    return AnalysisResponse(
        status=result.status,
        analysis=analysis_dict,
        message=result.message,
    )


@app.post("/api/analysis/upload", response_model=AnalysisResponse)
async def analysis_upload(file: UploadFile):
    """수동 파일 업로드 → RFP 분석.

    지원 형식: .pdf / .hwp / .hwpx / .doc / .docx
    최대 파일 크기: 50MB
    """
    file_bytes = await file.read()
    if len(file_bytes) > _ANALYSIS_MAX_BYTES:
        return AnalysisResponse(
            status="error",
            analysis=None,
            message="파일 크기가 50MB를 초과합니다.",
        )

    try:
        result: AnalysisResult = await analyze_file(file_bytes, file.filename or "upload")
    except UnsupportedFormatError:
        return AnalysisResponse(
            status="no_file",
            analysis=None,
            message="지원하지 않는 파일 형식입니다. PDF, HWP, HWPX, DOC, DOCX만 가능합니다.",
        )

    analysis_dict = result.analysis.model_dump() if result.analysis is not None else None
    return AnalysisResponse(
        status=result.status,
        analysis=analysis_dict,
        message=result.message,
    )


# --- /config ---------------------------------------------------------
# (필드명, 라벨, 정수 최소, 정수 최대) — 정수 설정 항목.
_CONFIG_INT_FIELDS: list[tuple[str, str, int, int]] = [
    ("interval_minutes", "수집 주기(분)", 1, 100000),
    ("window_overlap_minutes", "윈도우 겹침(분)", 0, 100000),
    ("backfill_days", "백필 기간(일)", 1, 366),
    ("num_of_rows", "페이지 크기", 1, 1000),
    ("max_retries", "재시도 한도", 0, 10),
]


def _render_runs_table(runs: list[dict]) -> str:
    if not runs:
        return '<p class="muted">아직 실행 이력이 없습니다.</p>'
    headers = [
        ("id", "ID"),
        ("source", "수집원"),
        ("trigger", "트리거"),
        ("status", "상태"),
        ("window", "윈도우"),
        ("counts", "fetched/new/updated"),
        ("retry_count", "재시도"),
        ("error_code", "에러코드"),
    ]
    head = "".join(f"<th>{_e(lbl)}</th>" for _, lbl in headers)
    body_rows = []
    for r in runs:
        bad = r.get("status") in ("failed", "partial")
        window = f"{_fmt_dt(r.get('window_bgn_dt'))} ~ {_fmt_dt(r.get('window_end_dt'))}"
        counts = f"{r.get('total_fetched') or 0}/{r.get('total_new') or 0}/{r.get('total_updated') or 0}"
        status = r.get("status") or ""
        if status == "success":
            badge = f'<span class="badge ok">{_e(status)}</span>'
        elif bad:
            badge = f'<span class="badge err">{_e(status)}</span>'
        else:
            badge = f'<span class="badge">{_e(status)}</span>'
        # 수집원(Phase 5.5): bid|pre_spec. NULL/빈값은 입찰(bid)로 표기.
        source = r.get("source") or "bid"
        source_badge = f'<span class="badge">{_e(source)}</span>'
        cells = [
            f"<td>{_e(r.get('id'))}</td>",
            f"<td>{source_badge}</td>",
            f"<td>{_e(r.get('trigger'))}</td>",
            f"<td>{badge}</td>",
            f"<td>{_e(window)}</td>",
            f"<td>{_e(counts)}</td>",
            f"<td>{_e(r.get('retry_count'))}</td>",
            f"<td>{_e(r.get('error_code'))}</td>",
        ]
        cls = ' class="bad"' if bad else ""
        body_rows.append(f"<tr{cls}>{''.join(cells)}</tr>")
    return f"""
    <div class="table-wrap">
      <table>
        <thead><tr>{head}</tr></thead>
        <tbody>{''.join(body_rows)}</tbody>
      </table>
    </div>"""


def _render_config_page(
    cfg: dict, runs: list[dict], sched: dict, msg: str | None, err: str | None
) -> str:
    msg_html = ""
    if err:
        msg_html = f'<div class="msg err">{_e(err)}</div>'
    elif msg:
        msg_html = f'<div class="msg ok">{_e(msg)}</div>'

    # 정수 설정 입력
    int_fields = "\n".join(
        f"""
        <label class="field">
          <span class="flabel">{_e(lbl)} <code>{_e(name)}</code></span>
          <input type="number" name="{_e(name)}" value="{_e(cfg.get(name))}" min="{lo}" max="{hi}">
        </label>"""
        for name, lbl, lo, hi in _CONFIG_INT_FIELDS
    )

    enabled_checked = " checked" if cfg.get("enabled") else ""
    pre_spec_enabled_checked = " checked" if cfg.get("pre_spec_enabled") else ""

    # 참가제한지역 select(Phase 4.3). 현재값(빈값=전체) selected. REGION_OPTIONS 순서.
    cur_rgn = cfg.get("prtcpt_lmt_rgn_cd")
    cur_rgn = "" if cur_rgn is None else str(cur_rgn)
    rgn_options = "\n".join(
        f'<option value="{_e(code)}"{" selected" if code == cur_rgn else ""}>{_e(label)}</option>'
        for code, label in region_codes.REGION_OPTIONS
    )

    # 자동중단(halt) 상태
    if cfg.get("auto_halted"):
        halt_html = f"""
        <div class="msg err">
          <b>자동 중단됨</b> — 비재시도 에러로 스케줄이 멈췄습니다.
          halt_code=<code>{_e(cfg.get('halt_code'))}</code>,
          사유: {_e(cfg.get('halt_reason'))}
        </div>
        <form method="post" action="/config/resume" style="margin-bottom:16px;">
          <button type="submit" class="submit danger">자동중단 해제(재개)</button>
        </form>"""
    else:
        halt_html = '<p class="muted" style="margin-bottom:16px;">자동 중단 상태가 아닙니다.</p>'

    # 스케줄러 제어
    running = sched.get("running")
    if running:
        sched_status = '<span class="badge ok">실행 중</span>'
        next_run = _fmt_dt(sched.get("next_run")) or "(미정)"
        sched_extra = f" · 다음 실행(입찰): {_e(next_run)}"
        pre_next_run = _fmt_dt(sched.get("pre_spec_next_run")) or "(미정)"
        sched_extra += f" · 다음 실행(사전규격): {_e(pre_next_run)}"
    else:
        sched_status = '<span class="badge">정지</span>'
        sched_extra = ""

    if cfg.get("auto_halted"):
        start_block = (
            '<p class="muted">자동 중단 상태에서는 시작할 수 없습니다. 먼저 위에서 [재개]하세요.</p>'
        )
    else:
        start_block = """
        <form method="post" action="/config/scheduler/start" style="display:inline-flex; gap:10px; align-items:center;">
          <label class="chk"><input type="checkbox" name="run_now" value="1"> 시작 직후 1회 즉시 수집</label>
          <button type="submit" class="submit go">시작</button>
        </form>"""

    stop_block = """
        <form method="post" action="/config/scheduler/stop" style="display:inline;">
          <button type="submit" class="submit danger">정지</button>
        </form>"""

    body = f"""
    {msg_html}

    <div class="card">
      <h2>스케줄러 제어 (수동)</h2>
      <p>현재 상태: {sched_status}{sched_extra}</p>
      <div class="row" style="gap:12px;">
        {start_block}
        {stop_block}
      </div>
      <div class="note">
        스케줄러가 떠 있어도 설정 <code>enabled=False</code> 면 매 주기 tick 이 수집을 건너뜁니다(게이트).
        <code>enabled</code>(수집 의도)와 스케줄러 구동(시작/정지)은 별개입니다.
      </div>
      <div class="note">
        <code>last_success_dt</code> 가 비어 있으면 첫 정규 수집은 최근 <b>{_e(cfg.get('backfill_days'))}</b>일 백필입니다
        (현재 last_success_dt=<code>{_e(_fmt_dt(cfg.get('last_success_dt')) or '없음')}</code>).
        interval 이 길면 첫 tick 은 그만큼 뒤이므로, 즉시 보려면 [시작 직후 1회] 체크 또는
        <code>python -m app.collector</code> 백필을 쓰세요.
      </div>
      <div class="note">
        사전규격 마지막 성공 시각: <code>{_e(_fmt_dt(cfg.get('pre_spec_last_success_dt')) or '없음')}</code>
        (사전규격 잡은 <code>pre_spec_enabled</code> 로만 게이트되며 입찰 자동중단과 무관합니다).
      </div>
    </div>

    <div class="card">
      <h2>자동중단 상태</h2>
      {halt_html}
    </div>

    <div class="card">
      <h2>수집 설정</h2>
      <form class="cfg" method="post" action="/config">
        <fieldset>
          <legend>주기·페이징·재시도</legend>
          <div class="grid">{int_fields}</div>
        </fieldset>
        <fieldset>
          <legend>조회 파라미터</legend>
          <div class="grid">
            <label class="field">
              <span class="flabel">조회구분 <code>inqry_div</code></span>
              <select name="inqry_div">
                <option value="1"{' selected' if cfg.get('inqry_div') == '1' else ''}>1 · 공고게시일시</option>
                <option value="2"{' selected' if cfg.get('inqry_div') == '2' else ''}>2 · 개찰일시</option>
              </select>
            </label>
            <label class="field">
              <span class="flabel">국내/국제 <code>intrntnl_div_cd</code></span>
              <select name="intrntnl_div_cd">
                <option value="1"{' selected' if cfg.get('intrntnl_div_cd') == '1' else ''}>1 · 국내</option>
                <option value="2"{' selected' if cfg.get('intrntnl_div_cd') == '2' else ''}>2 · 국제</option>
              </select>
            </label>
            <label class="field" style="min-width:260px;">
              <span class="flabel">참가제한지역 <code>prtcptLmtRgnCd</code></span>
              <select name="prtcpt_lmt_rgn_cd">
                {rgn_options}
              </select>
              <span class="help">00=전국(지역제한 없는 공고만), 빈값=전체(필터 안 함). 다음 수집부터 적용.</span>
            </label>
            <label class="field" style="min-width:260px;">
              <span class="flabel">업종코드 CSV <code>indstryty_cds</code></span>
              <input type="text" name="indstryty_cds" value="{_e(cfg.get('indstryty_cds'))}" placeholder="예: 1426,1468,1469,1470">
            </label>
          </div>
          <div class="note" style="margin-top:8px;">입찰공고 검색 기본값(추정가격)</div>
          <div class="grid" style="margin-top:4px;">
            <label class="field">
              <span class="flabel">추정가격 기본 하한 <code>presmpt_prce_bgn</code></span>
              <input type="number" name="presmpt_prce_bgn" value="{_e(cfg.get('presmpt_prce_bgn'))}" placeholder="비우면 미적용" min="0">
            </label>
            <label class="field">
              <span class="flabel">추정가격 기본 상한 <code>presmpt_prce_end</code></span>
              <input type="number" name="presmpt_prce_end" value="{_e(cfg.get('presmpt_prce_end'))}" placeholder="비우면 미적용" min="0">
            </label>
          </div>
          <div class="note" style="margin-top:8px;">사전규격 검색 기본값(배정예산액)</div>
          <div class="grid" style="margin-top:4px;">
            <label class="field">
              <span class="flabel">배정예산액 기본 하한 <code>pre_spec_amt_bgn</code></span>
              <input type="number" name="pre_spec_amt_bgn" value="{_e(cfg.get('pre_spec_amt_bgn'))}" placeholder="비우면 미적용" min="0">
            </label>
            <label class="field">
              <span class="flabel">배정예산액 기본 상한 <code>pre_spec_amt_end</code></span>
              <input type="number" name="pre_spec_amt_end" value="{_e(cfg.get('pre_spec_amt_end'))}" placeholder="비우면 미적용" min="0">
            </label>
          </div>
          <div class="note">목록(/list) 추정가격·(/pre-spec) 배정예산액 검색의 기본값으로 쓰입니다(비우면 기본 미적용).</div>
        </fieldset>
        <label class="chk" style="margin-bottom:8px;">
          <input type="checkbox" name="enabled" value="1"{enabled_checked}>
          수집 활성화 <code>enabled</code> (체크 해제 시 스케줄러가 떠 있어도 매 tick 건너뜀)
        </label>
        <label class="chk" style="margin-bottom:14px;">
          <input type="checkbox" name="pre_spec_enabled" value="1"{pre_spec_enabled_checked}>
          사전규격 수집 활성화 <code>pre_spec_enabled</code> (입찰 수집과 독립 토글 · 체크 해제 시 사전규격 잡만 매 tick 건너뜀)
        </label>
        <div class="note">사전규격 토글은 입찰 <code>auto_halted</code> 와 무관합니다(독립 게이트).</div>
        <div><button type="submit" class="submit">설정 저장</button></div>
      </form>
    </div>

    <div class="card">
      <h2>도구</h2>
      <fieldset>
        <legend>외부 도구</legend>
        <a href="/api-test" target="_blank" rel="noopener"
           style="display:inline-block; background:#1f3a5f; color:#fff; text-decoration:none;
                  padding:9px 18px; border-radius:7px; font-size:13px;">
          API테스트 열기 ↗
        </a>
        <p class="muted" style="margin:8px 0 0; font-size:12px;">
          원시 API 응답을 확인하거나 엔드포인트를 직접 호출할 때 사용합니다 (새 탭으로 열림).
        </p>
      </fieldset>
    </div>

    <div class="card">
      <h2>최근 실행 이력</h2>
      {_render_runs_table(runs)}
    </div>"""

    return _shell(
        "나라장터 입찰공고 수집 설정", "수집 설정·스케줄러·실행 이력을 관리합니다.", "config", body
    )


def _load_config_view() -> tuple[dict, list[dict]]:
    """/config 렌더에 필요한 cfg·runs 스칼라를 세션 안에서 추출(세션 종료 후 lazy 접근 방지)."""
    cfg_fields = [
        "enabled", "auto_halted", "halt_code", "halt_reason",
        "interval_minutes", "window_overlap_minutes", "backfill_days",
        "num_of_rows", "max_retries", "inqry_div", "intrntnl_div_cd",
        "indstryty_cds", "prtcpt_lmt_rgn_cd", "last_success_dt",
        "presmpt_prce_bgn", "presmpt_prce_end",
        # Phase 5.5: 사전규격 잡 토글·마지막 성공 시각.
        "pre_spec_enabled", "pre_spec_last_success_dt",
        # Phase 4.9-R2-D: 사전규격 배정예산액 기본 범위.
        "pre_spec_amt_bgn", "pre_spec_amt_end",
    ]
    run_fields = [
        "id", "trigger", "status", "window_bgn_dt", "window_end_dt",
        "total_fetched", "total_new", "total_updated", "retry_count", "error_code",
        # Phase 5.5: 실행이력 수집원(bid|pre_spec) 표시.
        "source",
    ]
    with SessionLocal() as session:
        cfg_obj = repository.get_config(session)
        cfg = {f: getattr(cfg_obj, f, None) for f in cfg_fields}
        run_objs = repository.list_recent_runs(session, limit=20)
        runs = [{f: getattr(r, f, None) for f in run_fields} for r in run_objs]
    return cfg, runs


def _sched_view() -> dict:
    return {
        "running": scheduler.is_running(),
        "next_run": scheduler.get_next_run_time(),
        # Phase 5.5: 사전규격 잡 다음 실행 시각(5.4에서 get_next_run_time 에 job_id 추가).
        "pre_spec_next_run": scheduler.get_next_run_time("collect_pre_spec"),
    }


@app.get("/config", response_class=HTMLResponse)
def config_page(saved: str | None = None, err: str | None = None) -> HTMLResponse:
    cfg, runs = _load_config_view()
    msg = "설정을 저장했습니다." if saved else None
    return HTMLResponse(_render_config_page(cfg, runs, _sched_view(), msg, err))


@app.post("/config")
async def config_save(request: Request):
    form = await request.form()
    data = {k: str(v) for k, v in form.items()}

    updates: dict[str, Any] = {}
    errors: list[str] = []

    # 정수 필드 검증
    for name, lbl, lo, hi in _CONFIG_INT_FIELDS:
        raw = data.get(name, "").strip()
        if raw == "":
            errors.append(f"{lbl}: 값이 비어 있습니다.")
            continue
        try:
            num = int(raw)
        except ValueError:
            errors.append(f"{lbl}: 정수가 아닙니다('{raw}').")
            continue
        if not (lo <= num <= hi):
            errors.append(f"{lbl}: {lo}~{hi} 범위를 벗어났습니다({num}).")
            continue
        updates[name] = num

    # inqry_div / intrntnl_div_cd
    inqry_div = data.get("inqry_div", "").strip()
    if inqry_div not in ("1", "2"):
        errors.append("조회구분(inqry_div)은 1 또는 2 여야 합니다.")
    else:
        updates["inqry_div"] = inqry_div

    intrntnl = data.get("intrntnl_div_cd", "").strip()
    if intrntnl not in ("1", "2"):
        errors.append("국내/국제(intrntnl_div_cd)는 1 또는 2 여야 합니다.")
    else:
        updates["intrntnl_div_cd"] = intrntnl

    # indstryty_cds CSV (숫자 코드 콤마 구분)
    csv_raw = data.get("indstryty_cds", "").strip()
    codes = [c.strip() for c in csv_raw.split(",") if c.strip()]
    if not codes:
        errors.append("업종코드(indstryty_cds)는 최소 1개가 필요합니다.")
    elif not all(c.isdigit() for c in codes):
        errors.append("업종코드(indstryty_cds)는 숫자 코드의 콤마 구분이어야 합니다.")
    else:
        updates["indstryty_cds"] = ",".join(codes)

    # 참가제한지역(Phase 4.3) — 허용 코드 ∪ {""}(빈값=전체)만 통과. 그 외 문자열 거부.
    rgn = data.get("prtcpt_lmt_rgn_cd", "").strip()
    if region_codes.is_valid_region(rgn):
        # 빈값은 None(=전체, 필터 안 함)으로 저장.
        updates["prtcpt_lmt_rgn_cd"] = rgn or None
    else:
        errors.append(
            f"참가제한지역(prtcpt_lmt_rgn_cd)이 허용 코드가 아닙니다('{rgn}')."
        )

    # 추정가격 기본 하한/상한 + 사전규격 배정예산액 기본 하한/상한 —
    # 숫자(콤마 허용) 또는 빈값. 빈값은 None(미적용)으로 저장.
    for name, lbl in (
        ("presmpt_prce_bgn", "추정가격 기본 하한"),
        ("presmpt_prce_end", "추정가격 기본 상한"),
        ("pre_spec_amt_bgn", "배정예산액 기본 하한"),
        ("pre_spec_amt_end", "배정예산액 기본 상한"),
    ):
        raw = data.get(name, "").strip().replace(",", "")
        if raw == "":
            updates[name] = None
            continue
        try:
            num = int(raw)
        except ValueError:
            errors.append(f"{lbl}({name}): 숫자 또는 빈값이어야 합니다('{data.get(name)}').")
            continue
        if num < 0:
            errors.append(f"{lbl}({name}): 0 이상이어야 합니다({num}).")
            continue
        updates[name] = str(num)

    # enabled 체크박스(없으면 False)
    updates["enabled"] = data.get("enabled") in ("1", "on", "true")
    # pre_spec_enabled 체크박스(Phase 5.5, 없으면 False) — 입찰 enabled 와 독립 토글.
    updates["pre_spec_enabled"] = data.get("pre_spec_enabled") in ("1", "on", "true")

    if errors:
        cfg, runs = _load_config_view()
        # 입력값을 화면에 되살려 사용자가 고치게 한다.
        cfg.update({k: v for k, v in updates.items()})
        html_page = _render_config_page(
            cfg, runs, _sched_view(), None, " / ".join(errors)
        )
        return HTMLResponse(html_page, status_code=400)

    with SessionLocal() as session:
        repository.update_config(session, **updates)

    # interval 변경을 실행 중 스케줄러에 즉시 반영.
    if scheduler.is_running():
        scheduler.reschedule(updates["interval_minutes"])

    return RedirectResponse(url="/config?saved=1", status_code=303)


@app.post("/config/resume")
async def config_resume():
    with SessionLocal() as session:
        repository.clear_halt(session)
    return RedirectResponse(url="/config?saved=1", status_code=303)


@app.post("/config/scheduler/start")
async def scheduler_start(request: Request):
    form = await request.form()
    run_now = form.get("run_now") in ("1", "on", "true")

    # 자동중단 상태면 시작을 막는다.
    with SessionLocal() as session:
        cfg = repository.get_config(session)
        halted = cfg.auto_halted
    if halted:
        return RedirectResponse(
            url="/config?err=" + quote("자동 중단 상태입니다. 먼저 재개하세요."),
            status_code=303,
        )

    scheduler.start_scheduler(run_now=run_now)
    return RedirectResponse(url="/config?saved=1", status_code=303)


@app.post("/config/scheduler/stop")
async def scheduler_stop():
    scheduler.shutdown_scheduler()
    return RedirectResponse(url="/config?saved=1", status_code=303)


# --- Phase 6.2b: 입찰공고 분석 API ------------------------------------------

# 파일 형식 우선순위 (낮을수록 우선).
_ANALYSIS_PRIORITY: dict[str, int] = {
    ".pdf": 0,
    ".docx": 1,
    ".doc": 2,
    ".hwp": 3,
    ".hwpx": 4,
}


@app.post("/api/analysis/bid/{bid_ntce_no}", response_model=AnalysisResponse)
async def analyze_bid(bid_ntce_no: str) -> AnalysisResponse:
    """입찰공고 첨부파일에서 제안요청서를 찾아 RFP 분석 결과를 반환한다.

    1. DB에서 첨부파일 목록 조회
    2. '제안요청서' 키워드 포함 파일명 필터
    3. 지원 형식 우선순위로 정렬 후 최우선 파일 선택
    4. analyze_from_url() 로 분석 후 결과 반환
    """
    with SessionLocal() as session:
        notice = session.get(BidNotice, bid_ntce_no)
        if notice is None:
            raise HTTPException(status_code=404, detail=f"입찰공고 '{bid_ntce_no}'를 찾을 수 없습니다.")
        files = repository.get_notice_files(session, bid_ntce_no)

    # '제안요청서' 포함 + 지원 형식 필터
    candidates = [
        f for f in files
        if "제안요청서" in f["name"]
        and Path(f["url"].split("?")[0]).suffix.lower() in SUPPORTED_EXTENSIONS
    ]
    # 우선순위 정렬(낮은 값 우선)
    candidates.sort(
        key=lambda f: _ANALYSIS_PRIORITY.get(
            Path(f["url"].split("?")[0]).suffix.lower(), 99
        )
    )

    if not candidates:
        return AnalysisResponse(
            status="no_file",
            message="제안요청서 파일을 찾을 수 없습니다. 파일을 직접 업로드해주세요.",
        )

    target = candidates[0]
    result: AnalysisResult = await analyze_from_url(target["url"])

    if result.status == "unsupported":
        return AnalysisResponse(
            status="unsupported",
            message="파일 변환에 실패했습니다. PDF 또는 DOCX를 업로드해주세요.",
        )
    if result.status == "error":
        return AnalysisResponse(status="error", message=result.message)

    analysis_dict = result.analysis.model_dump() if result.analysis is not None else None
    return AnalysisResponse(status="ok", analysis=analysis_dict)
