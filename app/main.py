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
import json
import logging
import math
import re
import zipfile
from collections.abc import Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import httpx
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from app import __version__, api_client, classifier, industry_codes, region_codes, repository, rule_test, scheduler
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
    AnalysisResult as ServiceAnalysisResult,
    analyze_documents,
    analyze_from_urls,
    SUPPORTED_EXTENSIONS,
)
from app.analysis.provider import analysis_enabled

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()  # 중앙 로깅 초기화 — 핸들러 없이 버려지는 로그 방지(멱등)
    try:
        init_db()  # 테이블+config seed 보장(멱등) 후 게이트 읽기
        with SessionLocal() as session:
            stale_count = repository.reset_stale_analyzing(session)
            logger.info("기동 시 중단된 분석 상태 정리: %d건", stale_count)
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


app = FastAPI(title="나라장터 입찰공고·사전규격 자동 수집", version=__version__, lifespan=lifespan)

# 조회일시 영역에서 전용 위젯으로 렌더링하므로 일반 입력 그리드에서는 제외하는 파라미터.
# (inqryBgnDt/inqryEndDt 는 화면의 날짜 선택값으로부터 서버에서 조립한다)
DATETIME_PARAM_NAMES = {"inqryDiv", "inqryBgnDt", "inqryEndDt", "bidClseExcpYn"}


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _link_query(params: dict[str, str]) -> str:
    """빈 값을 제외한 링크용 쿼리 문자열(HTML 이스케이프 전)."""
    return urlencode({k: v for k, v in params.items() if v != ""})


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


def _list_preset_date_range(
    date_field: str, dt_range: str, today: date | None = None,
) -> tuple[str, str]:
    """/list 기간 프리셋(today/1m/3m/6m)만 처리합니다. custom 등은 호출부에서 걸러야 합니다.
    공고일은 과거, 개찰일은 미래 방향(말일 보정).
    """
    if today is None:
        today = date.today()
    if dt_range == "today":
        return today.isoformat(), today.isoformat()
    months = {"1m": 1, "3m": 3, "6m": 6}[dt_range]
    if date_field == "openg_dt":
        return today.isoformat(), _months_after(today, months).isoformat()
    return _months_ago(today, months).isoformat(), today.isoformat()


def _render_field(p: ParamSpec, value: str = "") -> str:
    help_html = f'<span class="help">{_e(p.help)}</span>' if p.help else ""
    if p.name == "numOfRows":
        help_html += '<span class="help">최대 999 — 1000 이상이면 API 가 기본 10행만 돌려줍니다</span>'
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
    elif spec is not None and spec.kind == "license":
        inqry_label, inqry_options = "조회구분", (("1", "등록일시"), ("2", "입찰공고번호"))
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
  <title>나라장터 입찰공고 API 테스트</title>
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
  .badge { font-size: 12px; padding: 3px 9px; border-radius: 20px; background: #eef1f6; color: #4a5568;
           white-space: nowrap; }
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
  /* /list 표: 데이터는 세로 가운데, 헤더는 왼쪽, 배정예산만 오른쪽 정렬 */
  table.listtable > tbody > tr > td { vertical-align: middle; }
  table.listtable > thead > tr > th:not(.col-chk) { text-align: left; vertical-align: middle; }
  table.listtable > thead > tr > th { white-space: nowrap; }
  table.listtable > tbody > tr > td.budgetcell { text-align: right; white-space: nowrap; }
  /* 매칭업종은 코드만 표시하고 콤마 뒤에서 줄바꿈 허용 */
  table.listtable td.matchind { max-width: 240px; }
  table.listtable .indcode { white-space: nowrap; }
  /* /list 내용 폭이 일정한 컬럼은 가로 스크롤 안에서 px 폭을 유지한다. */
  table.listtable .col-chk { width: 36px; min-width: 36px; max-width: 36px; text-align: center; }
  table.listtable .col-chk input[type=checkbox] { padding: 0; margin: 0; }
  table:not(.listtable) .col-chk { width: 36px; }
  table.listtable .rownum-col { width: 44px; min-width: 44px; max-width: 44px; text-align: center; }
  table.listtable .kind-col { width: 82px; min-width: 82px; max-width: 82px; }
  table.listtable > tbody > tr > td.kind-col { padding-left: 4px; padding-right: 4px; }
  table.listtable .bidtime-col { width: 150px; min-width: 150px; max-width: 150px; }
  table.listtable .file-col { width: 80px; min-width: 80px; max-width: 80px; }
  table.listtable .analysis-col { width: 80px; min-width: 80px; max-width: 80px; }
  table.listtable > thead > tr > th.rownum-col { text-align: center; }
  table.listtable > tbody > tr > td.file-col,
  table.listtable > tbody > tr > td.analysis-col { padding: 6px 4px; text-align: center; vertical-align: middle; }
  table.listtable button.filebtn { padding-left: 4px; padding-right: 4px; }
  table:not(.listtable) > tbody > tr > td.analysis-col { text-align: center; vertical-align: middle; }
  /* 구분 셀: 위=공고종류명 배지, 아래=입찰방식명(배지 아닌 옅은 회색 텍스트) */
  td.kindcell { text-align: center; vertical-align: middle; }
  td.kindcell .bidmethd { display: block; margin-top: 4px; color: #8a93a2;
                          overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  /* 공고번호/공고명 통합 셀: 기존 글꼴·링크 스타일을 유지하고 두 줄로 표시 */
  td.ntcecell .ntceno, td.ntcecell .ntcenm { display: block; }
  td.ntcecell .ntceno { margin-bottom: 4px; }
  td.ntcecell .ntcenum { white-space: nowrap; }
  td.ntcecell .relntce { color: #8a93a2; }
  td.ntcecell .ntcenm { width: fit-content; }
  /* 공고일시·제출일시는 각 두 줄이며 정렬 기준을 헤더에서 선택한다. */
  td.bidtime-cell, th.bidtime-head { white-space: nowrap; }
  td.bidtime-cell .bt-line { display: block; line-height: 1.45; }
  th.bidtime-head .bidtime-sortfield { margin-left: 2px; padding: 1px 0; font-size: 12px; max-width: 58px; }
  th.bidtime-head .time-dir { margin-left: 2px; }
  table.listtable .sucsfcell, table.listtable button.filebtn,
  table.listtable button.btn-analyze, table.listtable .analysis-actions button { white-space: nowrap; }
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
  /* /list 전용 아웃라인 필드: 레이블이 입력 테두리 상단에 겹친다 */
  .list-filter .field, .list-filter .kw-field { position: relative; padding-top: 7px; }
  .list-filter .flabel { position: absolute; top: 0; left: 8px; z-index: 1;
                         margin: 0; padding: 0 4px; background: #fff; font-size: 12px;
                         line-height: 14px; white-space: nowrap; }
  .list-filter .field input, .list-filter .field select { background: #fff; }
  .list-filter .filter-summary { display: block; padding: 10px 16px; }
  .list-filter .list-summary-main { display: flex; gap: 10px; align-items: flex-end; flex-wrap: wrap; }
  .list-filter .list-summary-main .field input,
  .list-filter .list-summary-main .field select { height: 31px; box-sizing: border-box; }
  .list-filter .list-summary-main > button.submit,
  .list-filter .list-summary-main > button.filter-toggle,
  .list-filter .list-summary-main > button#btn-add-to-cart {
    height: 31px; box-sizing: border-box; padding-top: 0; padding-bottom: 0; line-height: 29px;
  }
  .list-filter .list-filter-row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin: 8px 0 0; }
  .list-filter .list-group-label { flex: 0 0 96px; width: 96px; min-width: max-content;
                                   font-size: 13px; font-weight: 600; line-height: 1.35; white-space: nowrap; }
  .list-filter .list-summary-main .query-field { min-width: 260px; }
  .list-filter .list-summary-main .price-max-field { min-width: 190px; }
  .list-filter .list-row-controls { display: flex; flex: 1 1 320px; min-width: 0; gap: 8px;
                                    align-items: center; flex-wrap: wrap; }
  .list-filter .date-controls, .list-filter .preset-buttons { display: flex; gap: 6px; align-items: center;
                                                               flex-wrap: wrap; }
  .list-filter .date-controls input, .list-filter .date-controls select { min-width: 0; }
  .list-filter .preset-buttons button { border: 1px solid #cbd2dc; background: #fff; color: #4a5568;
                                       padding: 6px 10px; border-radius: 6px; font-size: 13px; cursor: pointer;
                                       white-space: nowrap; }
  .list-filter .preset-buttons button:hover { background: #eef1f6; }
  .list-filter .preset-buttons button.is-active { background: #1f3a5f; border-color: #1f3a5f;
                                                  color: #fff; font-weight: 600; }
  .list-filter .list-toggle-chip { display: inline-flex; align-items: center; gap: 6px; padding: 6px 10px;
                                   border: 1px solid #cbd2dc; border-radius: 18px; background: #fff;
                                   font-size: 13px; cursor: pointer; white-space: nowrap; position: relative; }
  .list-filter .list-toggle-chip:has(input:checked) { border-color: #1f3a5f; background: #eaf0f8;
                                                      color: #1f3a5f; font-weight: 600; }
  .list-filter .list-toggle-chip input[type=checkbox] { position: absolute; width: 1px; height: 1px;
      padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); clip-path: inset(50%);
      white-space: nowrap; border: 0; }
  .list-filter .list-toggle-chip span { display: inline-flex; align-items: center; }
  .list-filter .list-toggle-chip span::before { content: "✓"; display: inline-flex; flex: 0 0 14px;
      width: 14px; height: 14px;
      margin-right: 6px; align-items: center; justify-content: center; border: 1px solid #9aa5b5;
      border-radius: 3px; background: #fff; color: transparent; box-sizing: border-box;
      font-size: 11px; line-height: 1; }
  .list-filter .list-toggle-chip:has(input:checked) span::before { content: "✓"; border-color: #1f3a5f;
      background: #1f3a5f; color: #fff; }
  .list-filter .list-toggle-chip:has(input:focus-visible) { outline: 2px solid #2767b1; outline-offset: 2px; }
  .list-filter .list-detail-rows { display: grid; grid-template-columns: minmax(0, 1fr); gap: 8px; }
  .list-filter .list-detail-rows > * { min-width: 0; }
  .list-filter .list-detail-rows .list-filter-row { margin: 0; }
  .list-filter .list-filter-row.list-input-row { padding-left: 104px; box-sizing: border-box; }
  .list-filter .list-filter-row .kw-field { flex: 1 1 360px; min-width: 220px; }
  .list-filter .list-filter-row .kw-box { background: #fff; }
  .list-filter .kw-chip, .list-filter .kw-chip-x, .list-filter .kw-opt, .list-filter .filter-toggle,
  .list-filter .kw-sum, .list-filter #btn-add-to-cart { white-space: nowrap; }
  @media (max-width: 700px) {
    .list-filter .list-group-label { flex-basis: 80px; width: 80px; }
    .list-filter .list-row-controls { flex: 1 1 0; min-width: 0; }
    .list-filter .list-filter-row.list-input-row { padding-left: 0; }
    .list-filter .list-filter-row .kw-field { flex: 1 1 0; width: 100%; max-width: none; min-width: 0; }
    .list-filter .list-filter-row .kw-box { width: 100%; min-width: 0; box-sizing: border-box; }
    .list-filter .list-filter-row .kw-input { min-width: 0; }
    .list-filter .list-filter-row .field { flex: 1 1 0; min-width: 0; }
    .list-filter .list-filter-row .field input { width: 100%; min-width: 0; box-sizing: border-box; }
  }
  @media (max-width: 480px) {
    .list-filter .list-group-label { flex-basis: 80px; width: 80px; }
    .list-filter .list-summary-main .query-field,
    .list-filter .list-summary-main .price-max-field { min-width: 0; width: 100%; }
  }
  .filter-card form[action="/pre-spec"] .query-field { min-width: 260px; }
  .filter-card form[action="/pre-spec"] .instt-field { min-width: 220px; }
  @media (max-width: 700px) {
    .filter-card form[action="/pre-spec"] .field,
    .filter-card form[action="/pre-spec"] .kw-field { min-width: 0; width: 100%; max-width: 100%; }
    .filter-card form[action="/pre-spec"] .field input,
    .filter-card form[action="/pre-spec"] .field select,
    .filter-card form[action="/pre-spec"] .kw-wrap,
    .filter-card form[action="/pre-spec"] .kw-box { width: 100%; min-width: 0; box-sizing: border-box; }
    .filter-card form[action="/pre-spec"] .kw-input { min-width: 0; }
    .filter-card form[action="/pre-spec"] .quick { flex-wrap: wrap; min-width: 0; }
    .filter-card form[action="/pre-spec"] .kw-chip { box-sizing: border-box; }
  }
  .list-card-heading { display: flex; justify-content: space-between; align-items: center;
                        flex-wrap: wrap; gap: 8px; margin-bottom: 14px; }
  .list-card-heading h2 { margin: 0; }
  .list-card-heading .rangeinfo { color: #8a93a2; font-size: 12px; margin-left: auto; }
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
  .zipall-msg { color: #b42318; margin: 6px 0; }
  .zipall-msg.info { color: #667085; }
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
  /* 분석 버튼 (Phase 8.2) */
  button.btn-analyze { border: 1px solid #7e5cb0; background: #f3eeff; color: #5a3e8a; padding: 4px 10px;
                       border-radius: 6px; font-size: 12px; cursor: pointer; white-space: nowrap; }
  button.btn-analyze:hover { background: #e8dcff; }
  button.btn-analyze:disabled { opacity: .6; cursor: default; }
  button.btn-analyze.is-analyzing { border-color: #e0a050; background: #fff4e0; color: #9a5b00; }
  button.btn-analyze.is-analyzing:disabled { opacity: .85; }
  button.btn-analyze.is-done { border-color: #4a9d68; background: #e7f6ec; color: #2f7d4f; }
  button.btn-analyze.is-done:hover { background: #d8f0df; }
  /* 분석 완료 드롭다운 */
  .analysis-actions { position: relative; display: inline-flex; align-items: center; justify-content: center; gap: 4px; }
  .analysis-actions .analysis-menu-toggle { padding: 4px 7px; }
  .analysis-menu { display: none; position: fixed; min-width: 96px;
                   background: #fff; border: 1px solid #cbd2dc; border-radius: 6px;
                   box-shadow: 0 4px 14px rgba(0,0,0,.14); z-index: 80; overflow: hidden; }
  .analysis-actions.open .analysis-menu { display: block; }
  .analysis-menu button { display: block; width: 100%; border: 0; background: #fff; color: #1f2430;
                          padding: 7px 10px; text-align: left; font-size: 12px; cursor: pointer; white-space: nowrap; }
  .analysis-menu button:hover { background: #f3eeff; color: #5a3e8a; }
  .analysis-menu button:focus-visible { background: #f3eeff; color: #5a3e8a;
                                        outline: 2px solid #2767b1; outline-offset: -2px; }
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
  /* 토스트(분석 에러 + 장바구니) */
  #analyzeToast { position: fixed; bottom: 80px; left: 50%; transform: translateX(-50%);
                  background: #b02a25; color: #fff; padding: 10px 20px; border-radius: 8px;
                  font-size: 13px; display: none; z-index: 70; box-shadow: 0 2px 8px rgba(0,0,0,.2); }
  #cartToast { position: fixed; bottom: 80px; left: 50%; transform: translateX(-50%);
               background: #2E7D32; color: #fff; padding: 10px 20px; border-radius: 8px;
               font-size: 13px; display: none; z-index: 70; box-shadow: 0 2px 8px rgba(0,0,0,.2); }
  /* 장바구니 체크박스 컬럼 */
  .col-chk { text-align: center; }
  .col-chk input[type=checkbox] { cursor: pointer; width: 16px; height: 16px; }
  /* 담기 버튼 (필터 summary 우측) */
  .btn-cart-add {
    background: #2E7D32; color: #fff; border: none;
    padding: 8px 16px; border-radius: 6px; cursor: pointer;
    font-weight: bold; white-space: nowrap; font-size: 13px;
  }
  .btn-cart-add:hover { background: #1B5E20; }
  /* 선택항목 헤더 버튼 */
  .btn-cart-menu {
    position: relative; background: #1565C0; color: #fff;
    border: none; padding: 6px 14px; border-radius: 6px;
    cursor: pointer; font-size: 13px;
  }
  .btn-cart-menu:hover { background: #0D47A1; }
  .btn-cart-menu.has-items { background: #E65100; }
  .cart-badge {
    display: inline-block; background: #fff; color: #E65100;
    border-radius: 10px; padding: 0 6px; font-size: 11px;
    font-weight: bold; margin-left: 4px;
  }
  /* 장바구니 모달 */
  .cart-modal-content { width: min(600px, 92vw); max-height: 70vh; display: flex; flex-direction: column; }
  .cart-modal-body { flex: 1; overflow-y: auto; padding: 8px 0; }
  .cart-item-row {
    display: flex; align-items: center; gap: 8px;
    padding: 8px 12px; border-bottom: 1px solid #eee;
  }
  .cart-item-type { font-size: 11px; padding: 2px 6px; border-radius: 4px; white-space: nowrap; flex-shrink: 0; }
  .type-bid { background: #E3F2FD; color: #1565C0; }
  .type-pre { background: #F3E5F5; color: #6A1B9A; }
  .cart-item-title { flex: 1; font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .cart-select-chk { cursor: pointer; width: 16px; height: 16px; flex-shrink: 0; }
  .btn-cart-search { flex-shrink: 0; background: #fff; color: #1565C0; border: 1px solid #1565C0; padding: 3px 10px; border-radius: 4px; cursor: pointer; font-size: 12px; }
  .btn-cart-search:hover { background: #E3F2FD; }
  .btn-cart-del { flex-shrink: 0; background: #EF5350; color: #fff; border: none; padding: 3px 10px; border-radius: 4px; cursor: pointer; font-size: 12px; }
  .btn-cart-del:hover { background: #C62828; }
  .cart-empty { text-align: center; color: #9E9E9E; padding: 24px; }
  .modal-footer { display: flex; gap: 8px; justify-content: flex-end; padding-top: 8px; border-top: 1px solid #eee; }
  .cart-modal-footer { justify-content: initial; }
  .cart-footer-left, .cart-footer-right { display: flex; gap: 8px; }
  .cart-footer-left { margin-right: auto; }
  .cart-modal-footer button:disabled { opacity: .5; cursor: default; }
  .btn-primary { background: #1565C0; color: #fff; border: none; padding: 8px 18px; border-radius: 6px; cursor: pointer; }
  .btn-secondary { background: #757575; color: #fff; border: none; padding: 8px 18px; border-radius: 6px; cursor: pointer; }
  /* 공고명 제외 키워드(Phase 10): 칩 입력 + 등록 목록 드롭다운(입력창 클릭 시 아래로) */
  .kw-field { display: flex; flex-direction: column; font-size: 13px;
              flex: 0 1 520px; max-width: 50%; min-width: 280px; }
  .kw-wrap { display: flex; gap: 8px; align-items: flex-start; }
  .kw-box { position: relative; display: flex; flex-wrap: wrap; align-items: center; gap: 6px;
            background: #fff; border: 1px solid #cbd2dc; border-radius: 6px; padding: 4px 8px;
            min-height: 34px; flex: 1 1 auto; cursor: text; }
  .kw-box.open { border-color: #9bb0cf; }
  /* 등록 목록 드롭다운 — 입력창 아래로 펼침 */
  .kw-menu { position: absolute; top: calc(100% + 4px); left: 0; right: 0; background: #fff;
             border: 1px solid #cbd2dc; border-radius: 6px; box-shadow: 0 4px 14px rgba(0,0,0,.14);
             max-height: 220px; overflow-y: auto; padding: 4px; z-index: 40; }
  .kw-menu[hidden] { display: none; }
  .kw-opt { display: flex; align-items: center; justify-content: space-between; gap: 8px; width: 100%;
            min-width: 0; border: 0; background: transparent; color: #1f2430; text-align: left; font-size: 12px;
            padding: 6px 8px; border-radius: 5px; cursor: pointer; }
  .kw-opt-text { display: block; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .kw-opt:hover { background: #eef1f6; }
  .kw-opt[hidden] { display: none; }
  .kw-opt.is-default { color: #97720d; }                       /* 기본 적용 키워드 = warning */
  .kw-opt.is-selected { background: #e3f5e9; color: #1d7a43; font-weight: 600; }  /* 적용 중 = success */
  .kw-opt.is-selected:hover { background: #d8f0df; }
  .kw-opt .kw-opt-mark { flex: 0 0 auto; font-size: 11px; visibility: hidden; }
  .kw-opt.is-selected .kw-opt-mark { visibility: visible; }
  .kw-menu-empty { font-size: 12px; color: #8a93a2; padding: 6px 8px; }
  .kw-menu-empty[hidden] { display: none; }
  .kw-chip { display: inline-flex; align-items: center; gap: 2px; background: #eef3fb; color: #1f3a5f;
             border: 1px solid #b6c6df; border-radius: 14px; padding: 2px 4px 2px 10px; font-size: 12px;
             max-width: 100%; min-width: 0; white-space: nowrap; }
  .kw-chip-text { display: block; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .kw-chip-x { border: 0; background: transparent; color: #4a5568; cursor: pointer; font-size: 14px;
               flex-shrink: 0; line-height: 1; padding: 0 4px; }
  .kw-chip-x:hover { color: #b02a25; }
  .kw-input { border: 0; outline: none; padding: 4px 2px; font-size: 13px; min-width: 110px; flex: 1 1 110px; }
  .kw-confirm { display: inline-flex; gap: 4px; }
  .kw-confirm[hidden] { display: none; }
  .kw-ok, .kw-cancel { border: 1px solid #cbd2dc; border-radius: 5px; cursor: pointer;
                       font-size: 12px; line-height: 1; padding: 4px 8px; }
  .kw-ok { color: #1d7a43; border-color: #a9d6bb; background: #e7f6ec; }
  .kw-ok:hover { background: #d8f0df; }
  .kw-cancel { color: #b02a25; border-color: #e6b6b3; background: #fde3e1; }
  .kw-cancel:hover { background: #fbd3d0; }
  .kw-help { font-size: 11px; color: #8a93a2; margin-top: 4px; }
  .kw-sum { font-size: 12px; padding: 4px 10px; border-radius: 20px; background: #fdf3df; color: #97720d;
            white-space: nowrap; margin-bottom: 6px; }
  .kw-sum[hidden] { display: none; }
  /* /config 키워드 관리 표 */
  table.kwtable { width: auto; min-width: 360px; }
  table.kwtable td, table.kwtable th { vertical-align: middle; }
  table.kwtable .kw-del { background: #b02a25; color: #fff; border: 0; padding: 3px 10px;
                          border-radius: 5px; font-size: 12px; cursor: pointer; }
  table.kwtable .kw-del:hover { background: #8f211d; }
"""

# /list 매칭업종 코드 전용 힌트와 툴팁 스타일.
_LIST_MATCHIND_TIP_CSS = """
  .indcode[data-tip] { cursor: help; text-decoration: underline dotted; text-underline-offset: 2px; }
  .indcode.nolimit { display: inline-block; padding: 2px 7px; border-radius: 12px;
                     background: #e9eef2; color: #455b6b; font-size: inherit; white-space: nowrap;
                     text-decoration: none; }
  .sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden;
             clip: rect(0, 0, 0, 0); clip-path: inset(50%); white-space: nowrap; border: 0; }
  .ind-tip { position: fixed; z-index: 90; padding: 4px 8px; border-radius: 4px;
             background: #1f2430; color: #fff; font-size: 12px; white-space: nowrap;
             pointer-events: none; }
"""


def _nav(active: str) -> str:
    """헤더 우측 설정 버튼. active in {'list','pre-spec','config','api-test'}.

    헤더 우측에는 '검토항목 장바구니' 버튼 + '설정' 링크 노출. 탭바는 _shell 에서 <main> 최상단에 렌더한다.
    """
    cfg_cls = ' class="hdr-btn active"' if active == "config" else ' class="hdr-btn"'
    cart_btn = (
        '<button id="btn-cart-menu" class="btn-cart-menu" onclick="toggleCartModal()">'
        '검토항목 장바구니 <span id="cart-badge" class="cart-badge" style="display:none">0</span>'
        '</button>'
    )
    return f'<div style="display:flex;align-items:center;gap:8px;">{cart_btn}<a href="/config"{cfg_cls}>설정</a></div>'


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


def _shell(title: str, subtitle: str, active: str, body: str, *, extra_css: str = "") -> str:
    """공통 HTML 셸. subtitle 인자는 호환성 위해 유지하나 헤더에 렌더하지 않는다."""
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_e(title)}</title>
  <style>{BASE_CSS}{extra_css}</style>
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


# 공고종류명(ntceKindNm) 값별 배지 클래스. 등록공고=기본, 재공고=warn, 변경공고=ok, 취소공고=err.
_NTCE_KIND_BADGE: dict[str, str] = {
    "재공고": "warn",
    "변경공고": "ok",
    "취소공고": "err",
}


def _render_ntce_kind(
    value: Any, bid_methd: Any = None, *, cell_class: str
) -> str:
    """'구분' 셀: 위=공고종류명(ntceKindNm) 배지, 아래=입찰방식명(bidMethdNm) 텍스트.

    입찰방식명은 배지가 아니라 옅은 회색 텍스트 한 줄로만 표시한다.
    한쪽만 있으면 그 줄만, 둘 다 없으면 '-'.
    """
    s = "" if value is None else str(value).strip()
    m = "" if bid_methd is None else str(bid_methd).strip()
    td_class = _e(cell_class)
    if not s and not m:
        return f'<td class="{td_class}">-</td>'
    lines = []
    if s:
        cls = _NTCE_KIND_BADGE.get(s, "")
        cls_attr = f"badge {cls}".strip()
        lines.append(f'<span class="{cls_attr}">{_e(s)}</span>')
    if m:
        lines.append(f'<span class="bidmethd" title="{_e(m)}">{_e(m)}</span>')
    return f'<td class="{td_class}">{"".join(lines)}</td>'


def _sucsfbid_mthd_short(value: Any) -> str:
    """낙찰방법의 첫 하이픈 앞을 표시하고, 앞이 비면 원문 전체로 폴백한다."""
    full = "" if value is None else str(value).strip()
    return full.split("-", 1)[0].strip() or full or "-"


# --- /list -----------------------------------------------------------
# (컬럼명, 표시 종류, 한글 헤더). /list 는 한글 헤더만 노출한다(영문 병기 없음).
# field_labels.py(camelCase 키)는 /list 의 snake_case 와 맞지 않아 미해석되므로,
# 여기서 컬럼별 한글 헤더를 직접 큐레이션한다(field_labels.py 는 /api-test 전용으로 유지).
_LIST_COLUMNS: list[tuple[str, str, str]] = [
    ("ntce_kind_nm", "ntcekind", "구분"),
    ("sucsfbid_mthd_nm", "sucsf", "낙찰방법"),
    ("bid_ntce_no", "ntce", "공고명"),
    ("ntce_instt_nm", "instt", "공고/수요기관"),
    ("asign_bdgt_amt", "amt", "배정예산"),
    ("notice_times", "notice_time", "공고일시"),
    ("submit_times", "submit_time", "제출일시"),
    ("matched_indstryty_cds", "matchind", "매칭업종"),
]

# 공고일시·제출일시 셀의 필드와 표시 라벨. 셀·헤더별로 두 필드만 공유한다.
_LIST_TIME_FIELDS: dict[str, list[tuple[str, str]]] = {
    "notice_time": [("bid_ntce_dt", "게시"), ("openg_dt", "개찰")],
    "submit_time": [("bid_begin_dt", "시작"), ("bid_clse_dt", "마감")],
}

# 컬럼 헤더 클릭 정렬이 가능한 컬럼 → 정렬 sort 키 접두(asc/desc 토글에 사용).
_SORTABLE_COLUMNS: dict[str, str] = {
    "asign_bdgt_amt": "asign_bdgt_amt",
}

# /list 정렬 허용값(repository._SORT_COLUMNS 와 동일). 미허용·빈값은 기본으로 폴백.
# 공고일시·제출일시 헤더에서 각 컬럼의 두 필드 방향을 선택한다.
# 추정가격 정렬은 화면 헤더 없이 기존 북마크 URL 호환을 위해 유지한다.
_LIST_SORTS: frozenset[str] = frozenset(
    {
        "bid_ntce_dt_desc",
        "bid_ntce_dt_asc",
        "bid_begin_dt_desc",
        "bid_begin_dt_asc",
        "bid_clse_dt_desc",
        "bid_clse_dt_asc",
        "openg_dt_desc",
        "openg_dt_asc",
        "asign_bdgt_amt_desc",
        "asign_bdgt_amt_asc",
        "presmpt_prce_desc",
        "presmpt_prce_asc",
    }
)
_DEFAULT_SORT = "bid_ntce_dt_desc"

_LIST_KINDS = {"reg": "등록공고", "re": "재공고", "chg": "변경공고", "cncl": "취소공고"}
_LIST_DEFAULT_KINDS = ("reg", "re", "chg")
_LIST_DATE_RANGES = (("today", "오늘"), ("1m", "1개월"), ("3m", "3개월"),
                     ("6m", "6개월"), ("custom", "직접입력"))

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
    """매칭업종 CSV → [코드,코드] 셀. 코드별 툴팁에 업종 전체명을 표시."""
    codes = industry_codes.parse_matched_codes(str(csv) if csv is not None else None)
    if not codes:
        return '<td class="matchind">-</td>'
    spans = []
    for code in codes:
        name = industry_codes.industry_name(code)
        if name:
            spans.append(
                f'<span class="indcode" data-tip="{_e(name)}" tabindex="0">{_e(code)}'
                f'<span class="sr-only">({_e(name)})</span></span>'
            )
        else:
            spans.append(f'<span class="indcode">{_e(code)}</span>')
    return f'<td class="matchind">[{",<wbr>".join(spans)}]</td>'


def _render_list_match(row: dict, match_codes: frozenset[str]) -> str:
    """현재 표시 근거로 업종 교집합 또는 제한없음 배지를 표시한다."""
    basis = row.get("match_basis")
    if basis == "업종":
        codes = industry_codes.parse_matched_codes(row.get("matched_indstryty_cds"))
        return _render_matched_inds(",".join(code for code in codes if code in match_codes))
    if basis in {"분류", "키워드"}:
        label = "공공조달분류" if basis == "분류" else "공고명 키워드"
        detail = row.get("match_detail")
        tip = f"업종 제한 없음 · 표시 근거: {label}"
        if detail:
            tip += f" '{detail}'"
        return (
            f'<td class="matchind"><span class="indcode nolimit" data-tip="{_e(tip)}" '
            f'tabindex="0">제한없음<span class="sr-only">({_e(tip)})</span></span></td>'
        )
    return '<td class="matchind">-</td>'


def _related_ntce_no(kind: Any, bef_bid_bbanc_no: Any, bid_ntce_no: Any) -> str | None:
    """재공고·변경공고 컬럼에서 공백·중복이 아닌 이전 공고번호를 읽는다."""
    if kind not in {"재공고", "변경공고"}:
        return None
    related_no = "" if bef_bid_bbanc_no is None else str(bef_bid_bbanc_no).strip()
    current_no = "" if bid_ntce_no is None else str(bid_ntce_no).strip()
    if not related_no or related_no == current_no:
        return None
    return related_no


def _display_ntce_no(kind: Any, bid_ntce_no: Any, bid_ntce_ord: Any = None) -> str:
    """변경공고에 차수가 있으면 공고번호와 붙여 표시한다."""
    number = "" if bid_ntce_no is None else str(bid_ntce_no).strip()
    order = "" if bid_ntce_ord is None else str(bid_ntce_ord).strip()
    notice_kind = "" if kind is None else str(kind).strip()
    if notice_kind == "변경공고" and number and order:
        return f"{number}-{order}"
    return number


def _render_ntce(
    no: Any,
    name: Any,
    url: Any,
    *,
    kind: Any = None,
    bef_bid_bbanc_no: Any = None,
    bid_ntce_ord: Any = None,
) -> str:
    """공고번호/공고명 통합 셀. 관련공고번호와 상세 링크를 조건에 따라 표시한다."""
    number = _display_ntce_no(kind, no, bid_ntce_ord)
    name = "" if name is None else str(name).strip()
    related = _related_ntce_no(kind, bef_bid_bbanc_no, no)
    number_line = f'<span class="ntcenum">{_e(number or "-")}</span>'
    if related:
        number_line += (
            f'<span class="relntce" title="관련공고(이전 공고번호)">'
            f' - [{_e(related)}]</span>'
        )
    text = _e(name or "-")
    if url:
        name_line = (
            f'<a class="ntcenm" href="{_e(url)}" '
            f'target="_blank" rel="noopener">{text}</a>'
        )
    else:
        name_line = f'<span class="ntcenm">{text}</span>'
    return (
        f'<td class="ntcecell"><span class="ntceno">{number_line}</span>'
        f'{name_line}</td>'
    )


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


def _render_list_time_cell(
    row: dict[str, Any], fields: list[tuple[str, str]], column_class: str
) -> str:
    """공고일시 또는 제출일시를 날짜 두 줄로 표시하고 원문 시각을 툴팁에 둔다."""
    lines = []
    for field, label in fields:
        full = _fmt_dt(row.get(field))
        if full:
            lines.append(
                f'<span class="bt-line" title="{_e(full)}">'
                f'{_e(label)} : {_e(full[:10])}</span>'
            )
        else:
            lines.append(f'<span class="bt-line">{_e(label)} : -</span>')
    return f'<td class="bidtime-cell bidtime-col {_e(column_class)}">{"".join(lines)}</td>'


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
    # 현재 이 컬럼으로 정렬 중이면 반대 방향, 아니면 처음엔 desc(금액·일시 모두 큰 값/최신 먼저).
    next_sort = f"{base}_asc" if cur_desc else f"{base}_desc"
    if cur_desc:
        arrow = ' <span class="arrow">▼</span>'
    elif cur_asc:
        arrow = ' <span class="arrow">▲</span>'
    else:
        # 미정렬: 중립 양방향 표시(흐린 색).
        arrow = ' <span class="arrow neutral">↕</span>'
    params = {**qs, "sort": next_sort, "page": "1"}
    url = f"{base_path}?{_link_query(params)}"
    return (
        f'<th><a class="sortcol" href="{_e(url)}">{_e(header)}{arrow}</a></th>'
    )


def _bid_time_header(
    header: str,
    sort: str,
    qs: dict[str, str],
    fields: list[tuple[str, str]],
    column_class: str,
) -> str:
    """일시 컬럼의 두 정렬 기준 select와 현재 방향을 토글하는 링크를 렌더한다."""
    field_names = {field for field, _label in fields}
    active_field = next(
        (field for field in field_names if sort in (f"{field}_asc", f"{field}_desc")),
        None,
    )
    selected_field = active_field or fields[0][0]
    direction = "asc" if active_field and sort.endswith("_asc") else "desc"

    options = []
    for field, label in fields:
        option_sort = f"{field}_{direction}"
        params = {**qs, "sort": option_sort, "page": "1"}
        url = f"/list?{_link_query(params)}"
        selected = " selected" if field == selected_field else ""
        options.append(
            f'<option value="{_e(url)}"{selected}>{_e(label)}</option>'
        )

    if active_field:
        next_direction = "desc" if direction == "asc" else "asc"
        next_sort = f"{active_field}_{next_direction}"
        arrow = "▲" if direction == "asc" else "▼"
        arrow_class = "arrow"
    else:
        next_sort = f"{selected_field}_desc"
        arrow = "↕"
        arrow_class = "arrow neutral"
    params = {**qs, "sort": next_sort, "page": "1"}
    url = f"/list?{_link_query(params)}"

    return (
        f'<th class="bidtime-head bidtime-col {_e(column_class)}">{_e(header)} '
        f'<select class="bidtime-sortfield" autocomplete="off" aria-label="{_e(header)} 정렬 기준">'
        f'{"".join(options)}</select> '
        f'<a class="sortcol time-dir" href="{_e(url)}" title="정렬 방향 전환">'
        f'<span class="{arrow_class}">{arrow}</span></a></th>'
    )


def _analysis_data_attrs(
    *,
    source_type: str,
    source_id: str,
    source_name: str,
    status: str,
) -> str:
    return (
        f'data-status="{_e(status)}" '
        f'data-type="{_e(source_type)}" '
        f'data-id="{_e(source_id)}" '
        f'data-name="{_e(source_name)}"'
    )


def _render_analysis_cell(
    *,
    source_type: str,
    source_id: str,
    source_name: str,
    status: str,
    cell_class: str = "",
) -> str:
    """분석 상태별 목록 셀을 렌더한다."""
    safe_status = status if status in {"none", "analyzing", "done", "error"} else "none"
    attrs = _analysis_data_attrs(
        source_type=source_type,
        source_id=source_id,
        source_name=source_name,
        status=safe_status,
    )
    td_open = f'<td class="{_e(cell_class)}">' if cell_class else "<td>"
    if safe_status == "analyzing":
        return (
            f'{td_open}<button type="button" class="btn-analyze is-analyzing" {attrs} '
            f'disabled aria-label="제안요청서 분석">분석중</button></td>'
        )
    if safe_status == "done":
        return (
            f'{td_open}<div class="analysis-actions" {attrs}>'
            f'<button type="button" class="btn-analyze is-done analysis-menu-toggle" '
            f'aria-label="분석 완료, 분석 메뉴" aria-expanded="false">완료 ▾</button>'
            f'<div class="analysis-menu" role="menu">'
            f'<button type="button" data-action="view" role="menuitem">분석보기</button>'
            f'<button type="button" data-action="auto" role="menuitem">자동 재분석</button>'
            f'<button type="button" data-action="upload" role="menuitem">파일 업로드</button>'
            f'</div></div></td>'
        )
    if safe_status == "error":
        return (
            f'{td_open}<div class="analysis-actions" {attrs}>'
            f'<button type="button" class="btn-analyze analysis-menu-toggle" '
            f'aria-label="분석 실패, 재분석 메뉴" aria-expanded="false">재분석 ▾</button>'
            f'<div class="analysis-menu" role="menu">'
            f'<button type="button" data-action="auto" role="menuitem">자동</button>'
            f'<button type="button" data-action="upload" role="menuitem">파일 업로드</button>'
            f'</div></div></td>'
        )
    return (
        f'{td_open}<div class="analysis-actions" {attrs}>'
        f'<button type="button" class="btn-analyze analysis-menu-toggle" '
        f'aria-label="분석 메뉴" aria-expanded="false">분석 ▾</button>'
        f'<div class="analysis-menu" role="menu">'
        f'<button type="button" data-action="auto" role="menuitem">자동</button>'
        f'<button type="button" data-action="upload" role="menuitem">파일 업로드</button>'
        f'</div></div></td>'
    )


def _render_list_rows(
    rows: list[dict],
    sort: str,
    qs: dict[str, str],
    status_map: dict[str, str] | None = None,
    *, match_codes: frozenset[str],
) -> str:
    if not rows:
        return '<p class="muted">조건에 맞는 공고가 없습니다.</p>'
    analysis_on = analysis_enabled()
    # 전체선택 체크박스 헤더
    chk_head = '<th class="col-chk"><input type="checkbox" id="chk-all" title="전체선택"></th>'
    head_cells = []
    for col, kind, header in _LIST_COLUMNS:
        if kind in _LIST_TIME_FIELDS:
            head_cells.append(
                _bid_time_header(header, sort, qs, _LIST_TIME_FIELDS[kind], col)
            )
        elif kind == "ntcekind":
            head_cells.append(f'<th class="kind-col kindhead">{_e(header)}</th>')
        elif col in _SORTABLE_COLUMNS:
            head_cells.append(_sort_header(col, header, sort, qs))
        else:
            head_cells.append(f"<th>{_e(header)}</th>")
    head = "".join(head_cells)
    head += '<th class="file-col">파일</th>'
    if analysis_on:
        head += '<th class="analysis-col">분석</th>'
    body_rows = []
    for i, r in enumerate(rows, start=1):
        raw_id = str(r.get("bid_ntce_no") or "")
        row_id = _e(raw_id)
        chk_cell = (
            f'<td class="col-chk">'
            f'<input type="checkbox" class="row-select-chk" '
            f'data-item-type="bid" data-item-id="{row_id}">'
            f'</td>'
        )
        cells = [chk_cell, f'<td class="rownum-col">{i}</td>']
        for col, kind, _header in _LIST_COLUMNS:
            val = r.get(col)
            if kind in _LIST_TIME_FIELDS:
                cells.append(_render_list_time_cell(r, _LIST_TIME_FIELDS[kind], col))
            elif kind == "amt":
                cells.append(f'<td class="budgetcell">{_e(_fmt_amt(val))}</td>')
            elif kind == "matchind":
                cells.append(_render_list_match(r, match_codes))
            elif kind == "ntcekind":
                cells.append(_render_ntce_kind(
                    val, r.get("bid_methd_nm"), cell_class="kind-col kindcell"
                ))
            elif kind == "instt":
                cells.append(_render_instt(val, r.get("dminstt_nm")))
            elif kind == "ntce":
                cells.append(_render_ntce(
                    val,
                    r.get("bid_ntce_nm"),
                    r.get("bid_ntce_dtl_url"),
                    kind=r.get("ntce_kind_nm"),
                    bef_bid_bbanc_no=r.get("bef_bid_bbanc_no"),
                    bid_ntce_ord=r.get("bid_ntce_ord"),
                ))
            elif kind == "sucsf":
                full = "" if val is None else str(val)
                title = f' title="{_e(full.strip())}"' if full.strip() else ""
                cells.append(
                    f'<td class="sucsfcell"{title}>{_e(_sucsfbid_mthd_short(val))}</td>'
                )
            else:
                cells.append(f"<td>{_e(val)}</td>")
        # 파일 컬럼: 첨부 개수>0 이면 drawer 호출 버튼, 없으면 '-'.
        cnt = r.get("_file_count", 0)
        if cnt:
            cells.append(
                f'<td class="file-col"><button type="button" class="filebtn" '
                f'data-no="{row_id}">파일 {cnt}</button></td>'
            )
        else:
            cells.append('<td class="file-col">-</td>')
        # 분석 버튼 컬럼 (Phase 8.2, USE_ANALYSIS_PROVIDER 토글)
        if analysis_on:
            status = (status_map or {}).get(raw_id, "none")
            cells.append(_render_analysis_cell(
                source_type="bid",
                source_id=raw_id,
                source_name=r.get("bid_ntce_nm") or "",
                status=status,
                cell_class="analysis-col",
            ))
        body_rows.append(f"<tr data-row-id=\"{row_id}\">{''.join(cells)}</tr>")
    return f"""
    <div class="table-wrap">
      <table class="listtable">
      <thead><tr>{chk_head}<th class="rownum-col">#</th>{head}</tr></thead>
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
        url = f"{base_path}?{_link_query(params)}"
        cls = " disabled" if disabled else ""
        return f'<a class="pager-btn{cls}" href="{_e(url)}">{_e(label)}</a>'

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


def _filter_toggle_button(card_id: str) -> str:
    """검색 필터 카드의 펼침/접힘 토글 버튼 마크업을 만든다."""
    toggle_js = (
        f"var c=document.getElementById('{card_id}');"
        f"c.classList.toggle('filter-collapsed');"
        f"var b=c.querySelector('.filter-toggle');"
        f"b.textContent=c.classList.contains('filter-collapsed')?'▾ 필터 펼침':'▴ 필터 접힘';"
    )
    return (
        f'<button type="button" class="filter-toggle" onclick="{_e(toggle_js)}">'
        "▾ 필터 펼침</button>"
    )


def _filter_card(
    *,
    action: str,
    summary_html: str,
    detail_html: str,
    title: str = "검색",
    card_id: str = "filterCard",
    right_html: str = "",
    extra_class: str = "",
    toggle_inside_summary: bool = False,
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
        extra_class: 화면 전용 클래스(기본값이면 기존 HTML 유지).
        toggle_inside_summary: True이면 토글 버튼을 summary_html 안에 직접 넣었음을 표시한다.
    """
    toggle_button = "" if toggle_inside_summary else _filter_toggle_button(card_id)
    return (
        f'<div class="filter-card filter-collapsed{(" " + _e(extra_class)) if extra_class else ""}" id="{_e(card_id)}" aria-label="{_e(title)}">'
        f'<form method="get" action="{_e(action)}">'
        f'<div class="filter-summary">'
        f'{summary_html}'
        f'{toggle_button}'
        f'{right_html}'
        f'</div>'
        f'<div class="filter-detail">{detail_html}</div>'
        f'</form>'
        f'</div>'
    )


# --- 공고명 제외 키워드 필터 (Phase 10) -------------------------------
# 화면 파라미터: `excl_kw`(콤마 구분 CSV) + `excl_kw_set`(폼 제출 표식).
#  - `excl_kw_set` 없음(첫 진입·외부 링크) → 설정의 기본 적용 키워드를 자동 적용.
#  - `excl_kw_set=1`(폼/페이저 링크) → 사용자가 정한 칩 목록 그대로(빈 값이면 필터 없음).
_EXCL_KW_SET_TRUE = ("1", "on", "true", "Y", "y")


def _parse_excl_kw(raw: str | None) -> list[str]:
    """CSV 파라미터 → 정규화된 키워드 목록(빈값·중복 제거, 상한 적용)."""
    if not raw:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for part in str(raw).split(","):
        kw = repository.normalize_keyword(part)
        if not kw or kw in seen:
            continue
        seen.add(kw)
        out.append(kw)
        if len(out) >= repository.EXCLUDE_KEYWORD_MAX:
            break
    return out


def _excl_kw_summary(keywords: list[str]) -> str:
    """접힌 필터 카드에서도 보이는 적용 개수 배지(칩은 상세 영역에 있음)."""
    hidden = "" if keywords else " hidden"
    title = _e(", ".join(keywords)) if keywords else ""
    return (
        f'<span class="kw-sum" id="exclKwCount" title="{title}"{hidden}>'
        f'제외 키워드 {len(keywords)}개</span>'
    )


def _excl_kw_field(keywords: list[str], options: list[dict[str, Any]], label: str) -> str:
    """칩 입력 위젯 — 칩 + 입력(✓/✕) + 등록 목록 드롭다운 + hidden(excl_kw/excl_kw_set).

    입력창과 목록을 한 박스로 합쳐 클릭 시 아래로 드롭다운이 열린다. 드롭다운 항목은
    기본 적용 키워드=warning 색, 현재 적용 중(칩에 있는) 키워드=success 하이라이트로 구분한다.
    """
    chips = "".join(
        f'<span class="kw-chip" data-kw="{_e(kw)}" title="{_e(kw)}">'
        f'<span class="kw-chip-text">{_e(kw)}</span>'
        f'<button type="button" class="kw-chip-x" aria-label="{_e(kw)} 제외 해제">&times;</button>'
        f"</span>"
        for kw in keywords
    )
    selected = {kw.lower() for kw in keywords}
    opts = "".join(
        '<button type="button" class="kw-opt'
        f'{" is-default" if o.get("is_default") else ""}'
        f'{" is-selected" if str(o["keyword"]).lower() in selected else ""}'
        f'" data-kw="{_e(o["keyword"])}" role="option">'
        f'<span class="kw-opt-text">{_e(o["keyword"])}</span>'
        f'<span class="kw-opt-mark">&#10003;</span>'
        "</button>"
        for o in options
    )
    empty_msg = (
        "일치하는 키워드가 없습니다."
        if options
        else "등록된 키워드가 없습니다 · [설정]에서 추가하세요."
    )
    return f"""
      <div class="kw-field">
        <span class="flabel">{_e(label)}</span>
        <div class="kw-wrap">
          <div class="kw-box" id="exclKwBox">
            {chips}
            <input type="text" class="kw-input" id="exclKwInput" placeholder="제외할 단어 입력"
                   maxlength="{repository.EXCLUDE_KEYWORD_MAX_LEN}" autocomplete="off"
                   role="combobox" aria-expanded="false" aria-controls="exclKwMenu"
                   aria-label="제외 키워드 입력">
            <span class="kw-confirm" id="exclKwConfirm" hidden>
              <button type="button" class="kw-ok" id="exclKwOk" title="추가" aria-label="제외 키워드 추가">&#10003;</button>
              <button type="button" class="kw-cancel" id="exclKwCancel" title="취소" aria-label="입력 취소">&#10005;</button>
            </span>
            <div class="kw-menu" id="exclKwMenu" role="listbox" aria-label="등록된 제외 키워드" hidden>
              {opts}
              <div class="kw-menu-empty" id="exclKwMenuEmpty"{"" if not options else " hidden"}>{_e(empty_msg)}</div>
            </div>
          </div>
        </div>
        <span class="kw-help">입력창을 누르면 등록 목록이 열립니다 · 목록 클릭으로 추가/해제 · 입력 후 ✓(또는 Enter)로 추가 · 목록 관리는 [설정]</span>
        <input type="hidden" name="excl_kw" id="exclKwHidden" value="{_e(",".join(keywords))}">
        <input type="hidden" name="excl_kw_set" value="1">
      </div>"""


# 칩 입력 위젯 스크립트(/list·/pre-spec 공용). 평문 상수라 중괄호 이스케이프 불필요.
_EXCL_KW_SCRIPT = """
  (function () {
    var box = document.getElementById('exclKwBox');
    if (!box) return;
    var input = document.getElementById('exclKwInput');
    var hidden = document.getElementById('exclKwHidden');
    var confirmBox = document.getElementById('exclKwConfirm');
    var okBtn = document.getElementById('exclKwOk');
    var cancelBtn = document.getElementById('exclKwCancel');
    var menu = document.getElementById('exclKwMenu');
    var menuEmpty = document.getElementById('exclKwMenuEmpty');
    var badge = document.getElementById('exclKwCount');
    var MAX = %(max)d;
    var MAXLEN = %(maxlen)d;

    function words() {
      return Array.prototype.map.call(box.querySelectorAll('.kw-chip'), function (c) {
        return c.getAttribute('data-kw') || '';
      });
    }

    function sync() {
      var list = words();
      hidden.value = list.join(',');
      if (badge) {
        badge.textContent = '제외 키워드 ' + list.length + '개';
        badge.title = list.join(', ');
        if (list.length) { badge.removeAttribute('hidden'); }
        else { badge.setAttribute('hidden', 'hidden'); }
      }
      markSelected(list);
    }

    // 드롭다운 항목 하이라이트 — 현재 적용 중(칩에 있는) 키워드에 success 표시.
    function markSelected(list) {
      if (!menu) return;
      var lower = list.map(function (w) { return w.toLowerCase(); });
      Array.prototype.forEach.call(menu.querySelectorAll('.kw-opt'), function (opt) {
        var kw = (opt.getAttribute('data-kw') || '').toLowerCase();
        if (lower.indexOf(kw) >= 0) { opt.classList.add('is-selected'); }
        else { opt.classList.remove('is-selected'); }
      });
    }

    // 입력 중인 글자로 목록을 좁힌다(빈 입력이면 전체 노출).
    function filterMenu() {
      if (!menu) return;
      var q = input.value.trim().toLowerCase();
      var shown = 0;
      Array.prototype.forEach.call(menu.querySelectorAll('.kw-opt'), function (opt) {
        var kw = (opt.getAttribute('data-kw') || '').toLowerCase();
        var hit = !q || kw.indexOf(q) >= 0;
        if (hit) { opt.removeAttribute('hidden'); shown += 1; }
        else { opt.setAttribute('hidden', 'hidden'); }
      });
      if (menuEmpty) {
        if (shown) { menuEmpty.setAttribute('hidden', 'hidden'); }
        else { menuEmpty.removeAttribute('hidden'); }
      }
    }

    function openMenu() {
      if (!menu) return;
      filterMenu();
      menu.removeAttribute('hidden');
      box.classList.add('open');
      input.setAttribute('aria-expanded', 'true');
    }

    function closeMenu() {
      if (!menu) return;
      menu.setAttribute('hidden', 'hidden');
      box.classList.remove('open');
      input.setAttribute('aria-expanded', 'false');
    }

    function menuOpen() {
      return !!menu && !menu.hasAttribute('hidden');
    }

    function removeWord(word) {
      var target = String(word || '').toLowerCase();
      Array.prototype.forEach.call(box.querySelectorAll('.kw-chip'), function (chip) {
        if ((chip.getAttribute('data-kw') || '').toLowerCase() === target) {
          chip.parentNode.removeChild(chip);
        }
      });
      sync();
    }

    function addWord(raw) {
      var w = String(raw == null ? '' : raw).replace(/,/g, ' ').trim().slice(0, MAXLEN);
      if (!w) return;
      var list = words();
      if (list.length >= MAX) { window.alert('제외 키워드는 최대 ' + MAX + '개까지 지정할 수 있습니다.'); return; }
      for (var i = 0; i < list.length; i += 1) {
        if (list[i].toLowerCase() === w.toLowerCase()) return;  // 중복 무시
      }
      var chip = document.createElement('span');
      chip.className = 'kw-chip';
      chip.setAttribute('data-kw', w);
      chip.setAttribute('title', w);
      var chipText = document.createElement('span');
      chipText.className = 'kw-chip-text';
      chipText.textContent = w;
      chip.appendChild(chipText);
      var x = document.createElement('button');
      x.type = 'button';
      x.className = 'kw-chip-x';
      x.setAttribute('aria-label', w + ' 제외 해제');
      x.innerHTML = '&times;';
      chip.appendChild(x);
      box.insertBefore(chip, input);
      sync();
    }

    function clearInput() {
      input.value = '';
      confirmBox.setAttribute('hidden', 'hidden');
    }

    function toggleConfirm() {
      if (input.value.trim()) { confirmBox.removeAttribute('hidden'); }
      else { confirmBox.setAttribute('hidden', 'hidden'); }
    }

    // 한글 등 IME 조합 상태 추적 — 조합 중 Enter 는 '조합 확정'용이라 칩 추가로 처리하면 안 된다
    // (확정 전 글자로 칩이 하나 만들어지고, 확정된 잔여 글자가 다시 칩이 되어 중복 입력됨).
    var composing = false;
    input.addEventListener('compositionstart', function () { composing = true; });
    input.addEventListener('compositionend', function () { composing = false; toggleConfirm(); });

    input.addEventListener('input', function () { toggleConfirm(); filterMenu(); openMenu(); });
    input.addEventListener('focus', openMenu);
    input.addEventListener('keydown', function (e) {
      if (composing || e.isComposing || e.keyCode === 229) return;  // IME 조합 중 키는 무시
      if (e.key === 'Enter') {
        // 필터 폼 제출 대신 칩 추가로 처리.
        e.preventDefault();
        addWord(input.value);
        clearInput();
        filterMenu();
      } else if (e.key === 'Escape') {
        e.preventDefault();
        if (menuOpen()) { closeMenu(); }
        else { clearInput(); }
      }
    });
    okBtn.addEventListener('click', function () {
      addWord(input.value); clearInput(); filterMenu(); input.focus();
    });
    cancelBtn.addEventListener('click', function () { clearInput(); filterMenu(); input.focus(); });

    box.addEventListener('click', function (e) {
      var x = e.target.closest ? e.target.closest('.kw-chip-x') : null;
      if (x) {
        var chip = x.closest('.kw-chip');
        if (chip) { chip.parentNode.removeChild(chip); sync(); }
        return;
      }
      // 드롭다운 항목 클릭 = 적용/해제 토글(메뉴는 열어 둔다).
      var opt = e.target.closest ? e.target.closest('.kw-opt') : null;
      if (opt) {
        var kw = opt.getAttribute('data-kw') || '';
        if (opt.classList.contains('is-selected')) { removeWord(kw); }
        else { addWord(kw); }
        return;
      }
      if (!menuOpen()) openMenu();
      if (e.target === box) input.focus();
    });

    // 위젯 밖 클릭 시 닫기.
    document.addEventListener('click', function (e) {
      if (!box.contains(e.target)) closeMenu();
    });

    sync();
  })();
""" % {"max": repository.EXCLUDE_KEYWORD_MAX, "maxlen": repository.EXCLUDE_KEYWORD_MAX_LEN}


# /list 매칭업종 전용 툴팁. 이벤트 위임으로 표의 코드 span 을 처리한다.
_LIST_MATCHIND_TIP_SCRIPT = """
  (function () {
    var tooltip = document.getElementById('indTip');
    if (!tooltip) return;

    var hovered = null;
    var focused = null;

    function matchedCode(target) {
      return target && target.closest ? target.closest('.indcode[data-tip]') : null;
    }

    function hideTooltip() {
      hovered = null;
      focused = null;
      tooltip.hidden = true;
      tooltip.textContent = '';
      tooltip.style.left = '';
      tooltip.style.top = '';
    }

    function updateTooltip() {
      var target = focused || hovered;
      if (!target) {
        tooltip.hidden = true;
        tooltip.textContent = '';
        tooltip.style.left = '';
        tooltip.style.top = '';
        return;
      }

      tooltip.textContent = target.getAttribute('data-tip') || '';
      tooltip.hidden = false;
      var rect = target.getBoundingClientRect();
      var tipRect = tooltip.getBoundingClientRect();
      var gap = 6;
      var edge = 8;
      var left = rect.left + (rect.width - tipRect.width) / 2;
      left = Math.max(edge, Math.min(left, window.innerWidth - tipRect.width - edge));
      var top = rect.top - tipRect.height - gap;
      if (top < edge) top = rect.bottom + gap;
      top = Math.max(edge, Math.min(top, window.innerHeight - tipRect.height - edge));
      tooltip.style.left = left + 'px';
      tooltip.style.top = top + 'px';
    }

    document.addEventListener('mouseover', function (event) {
      var code = matchedCode(event.target);
      if (code) {
        hovered = code;
        updateTooltip();
      }
    });
    document.addEventListener('mouseout', function (event) {
      var code = matchedCode(event.target);
      if (!code || (event.relatedTarget && code.contains(event.relatedTarget))) return;
      if (hovered === code) {
        hovered = null;
        updateTooltip();
      }
    });
    document.addEventListener('focusin', function (event) {
      var code = matchedCode(event.target);
      if (code) {
        focused = code;
        updateTooltip();
      }
    });
    document.addEventListener('focusout', function (event) {
      var code = matchedCode(event.target);
      if (!code || (event.relatedTarget && code.contains(event.relatedTarget))) return;
      if (focused === code) {
        focused = null;
        updateTooltip();
      }
    });
    document.addEventListener('scroll', hideTooltip, true);
    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape') hideTooltip();
    });
  })();
"""


# /list 클라이언트 스크립트(날짜 프리셋·일시 정렬 select·가격 콤마·파일 drawer).
# 평문 상수라 중괄호 이스케이프 불필요.
# 파일명은 서버 JSON에서 받아 textContent 로 삽입(XSS 방지). 파일 URL 은 DB 저장 URL 만 사용.
_LIST_SCRIPT = """
  window.addEventListener('pageshow', function () {
    document.querySelectorAll('select.bidtime-sortfield').forEach(function (select) {
      Array.from(select.options).forEach(function (option) {
        option.selected = option.defaultSelected;
      });
    });
  });

  document.addEventListener('change', function (e) {
    if (e.target.matches('select.bidtime-sortfield') && e.target.value) {
      location.href = e.target.value;
    }
  });

  // 프리셋은 서버가 계산한 기간을 사용해 월말 보정과 날짜 기준별 방향을 일치시킨다.
  (function () {
    var sel = document.getElementById('date_field');
    var range = document.getElementById('dt_range');
    var group = document.getElementById('list-date-group');
    if (!sel || !range || !group) return;
    var presets = JSON.parse(group.dataset.presetRanges);
    var f = document.getElementById('dt_from');
    var t = document.getElementById('dt_to');
    var buttons = Array.from(group.querySelectorAll('[data-date-preset]'));

    function fillDates(preset) {
      var dates = presets[sel.value][preset];
      f.value = dates[0];
      t.value = dates[1];
    }
    function syncPreset() {
      buttons.forEach(function (button) {
        var active = button.dataset.datePreset === range.value;
        button.classList.toggle('is-active', active);
        button.setAttribute('aria-pressed', active ? 'true' : 'false');
      });
    }
    buttons.forEach(function (button) {
      button.addEventListener('click', function () {
        range.value = button.dataset.datePreset;
        fillDates(range.value);
        syncPreset();
      });
    });
    sel.addEventListener('change', function () {
      if (range.value !== 'custom') fillDates(range.value);
    });
    [f, t].forEach(function (input) {
      input.addEventListener('change', function () {
        range.value = 'custom';
        syncPreset();
      });
    });
    syncPreset();
  })();

  // 예산 입력은 숫자만 남기고 천 단위 콤마를 표시한다.
  (function () {
    function caretAfterDigits(value, count) {
      if (count <= 0) return 0;
      var seen = 0;
      for (var i = 0; i < value.length; i++) {
        if (/[0-9]/.test(value.charAt(i)) && ++seen === count) return i + 1;
      }
      return value.length;
    }
    function formatPrice(input, digitCount, forceCaret, stripLeadingZeros) {
      var rawDigits = input.value.replace(/[^0-9]/g, '');
      var digits = stripLeadingZeros ? rawDigits.replace(/^0+(?=\\d)/, '') : rawDigits;
      var removedLeadingZeros = stripLeadingZeros ? rawDigits.length - digits.length : 0;
      var formatted = digits.replace(/\\B(?=(\\d{3})+(?!\\d))/g, ',');
      var changed = input.value !== formatted;
      if (changed) {
        input.value = formatted;
      }
      if (changed || forceCaret) {
        digitCount = Math.max(0, digitCount - removedLeadingZeros);
        var caret = caretAfterDigits(formatted, digitCount);
        input.setSelectionRange(caret, caret);
      }
    }
    document.querySelectorAll('input.price-comma').forEach(function (input) {
      input.addEventListener('input', function () {
        var cursor = input.selectionStart === null ? input.value.length : input.selectionStart;
        var digitCount = input.value.slice(0, cursor).replace(/[^0-9]/g, '').length;
        formatPrice(input, digitCount);
      });
      // 편집 중에는 0 자리를 보존하고, 값 확정 시에만 선행 0 을 정리한다.
      input.addEventListener('change', function () {
        var cursor = input.selectionStart === null ? input.value.length : input.selectionStart;
        var digitCount = input.value.slice(0, cursor).replace(/[^0-9]/g, '').length;
        formatPrice(input, digitCount, false, true);
      });
      input.addEventListener('keydown', function (event) {
        if ((event.key !== 'Backspace' && event.key !== 'Delete') || input.selectionStart !== input.selectionEnd) return;
        var cursor = input.selectionStart;
        var digit;
        if (event.key === 'Backspace' && cursor && input.value.charAt(cursor - 1) === ',') {
          digit = cursor - 2;
        } else if (event.key === 'Delete' && input.value.charAt(cursor) === ',') {
          digit = cursor + 1;
        } else {
          return;
        }
        while (digit >= 0 && !/[0-9]/.test(input.value.charAt(digit))) digit--;
        if (digit < 0 || digit >= input.value.length || !/[0-9]/.test(input.value.charAt(digit))) return;
        event.preventDefault();
        var digitCount = input.value.slice(0, digit).replace(/[^0-9]/g, '').length;
        input.value = input.value.slice(0, digit) + input.value.slice(digit + 1);
        formatPrice(input, digitCount, true);
      });
    });
    document.querySelectorAll('.list-filter form').forEach(function (form) {
      form.addEventListener('submit', function () {
        form.querySelectorAll('input.price-comma').forEach(function (input) {
          var cursor = input.selectionStart === null ? input.value.length : input.selectionStart;
          var digitCount = input.value.slice(0, cursor).replace(/[^0-9]/g, '').length;
          formatPrice(input, digitCount, false, true);
        });
      });
    });
  })();

  (function () {
    var backdrop = document.getElementById('drawerBackdrop');
    var drawer = document.getElementById('drawer');
    var titleEl = document.getElementById('drawerTitle');
    var listEl = document.getElementById('fileList');
    var zipAll = document.getElementById('zipAll');
    var zipAllMsg = document.getElementById('zipAllMsg');
    var zipDownloads = Object.create(null);
    zipAll.addEventListener('click', function (event) {
      if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      var url = zipAll.getAttribute('href');
      if (!url || url === '#') return;
      zipAllMsg.textContent = '다운로드를 요청했습니다. 첨부가 많으면 시작까지 시간이 걸릴 수 있습니다.';
      zipAllMsg.classList.add('info');
      zipAllMsg.hidden = false;
      if (zipDownloads[url]) return;
      var timer = setTimeout(function () { delete zipDownloads[url]; }, 60 * 1000);
      zipDownloads[url] = timer;
      downloadInFrame(url, function (message) {
        if (zipDownloads[url] === timer) {
          clearTimeout(timer);
          delete zipDownloads[url];
        }
        if (zipAll.getAttribute('href') !== url) return;
        zipAllMsg.classList.remove('info');
        zipAllMsg.textContent = message;
        zipAllMsg.hidden = false;
      });
    });

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
        zipAllMsg.textContent = '';
        zipAllMsg.hidden = true;
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

# 분석 UI 스크립트 (Phase 8.2) — 상태 버튼, 새 창 보기, 폴링, 업로드 모달.
# 평문 상수라 중괄호 이스케이프 불필요.
_ANALYSIS_SCRIPT = """
  (function () {
    'use strict';

    var _pollTimers = {};
    var _requesting = {};
    var _uploadMeta = null;
    var _uploadRoot = null;
    var _uploadFiles = [];
    var _uploadConfirmed = false;

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

    function readJson(resp) {
      return resp.json().catch(function () { return {}; });
    }

    function pollKey(type, id) {
      return type + ':' + id;
    }

    // 메뉴 항목의 동작 정보는 감싸는 .analysis-actions에서 읽는다.
    function getRoot(el) {
      if (!el) return null;
      return el.closest('.analysis-actions') || el.closest('.btn-analyze');
    }

    function getMeta(el) {
      var root = getRoot(el);
      if (!root) return null;
      return {
        root: root,
        type: root.getAttribute('data-type') || '',
        id: root.getAttribute('data-id') || '',
        name: root.getAttribute('data-name') || '',
        status: root.getAttribute('data-status') || ''
      };
    }

    function dataAttrs(meta, status) {
      return 'data-status="' + esc(status) + '" '
        + 'data-type="' + esc(meta.type) + '" '
        + 'data-id="' + esc(meta.id) + '" '
        + 'data-name="' + esc(meta.name || '') + '"';
    }

    function renderControl(meta, status) {
      var attrs = dataAttrs(meta, status);
      if (status === 'analyzing') {
        return '<button type="button" class="btn-analyze is-analyzing" ' + attrs
          + ' disabled aria-label="제안요청서 분석">분석중</button>';
      }
      if (status === 'done') {
        return '<div class="analysis-actions" ' + attrs + '>'
          + '<button type="button" class="btn-analyze is-done analysis-menu-toggle" aria-label="분석 완료, 분석 메뉴" aria-expanded="false">완료 ▾</button>'
          + '<div class="analysis-menu" role="menu">'
          + '<button type="button" data-action="view" role="menuitem">분석보기</button>'
          + '<button type="button" data-action="auto" role="menuitem">자동 재분석</button>'
          + '<button type="button" data-action="upload" role="menuitem">파일 업로드</button>'
          + '</div></div>';
      }
      if (status === 'error') {
        return '<div class="analysis-actions" ' + attrs + '>'
          + '<button type="button" class="btn-analyze analysis-menu-toggle" aria-label="분석 실패, 재분석 메뉴" aria-expanded="false">재분석 ▾</button>'
          + '<div class="analysis-menu" role="menu">'
          + '<button type="button" data-action="auto" role="menuitem">자동</button>'
          + '<button type="button" data-action="upload" role="menuitem">파일 업로드</button>'
          + '</div></div>';
      }
      return '<div class="analysis-actions" ' + attrs + '>'
        + '<button type="button" class="btn-analyze analysis-menu-toggle" aria-label="분석 메뉴" aria-expanded="false">분석 ▾</button>'
        + '<div class="analysis-menu" role="menu">'
        + '<button type="button" data-action="auto" role="menuitem">자동</button>'
        + '<button type="button" data-action="upload" role="menuitem">파일 업로드</button>'
        + '</div></div>';
    }

    function setBusy(root, busy) {
      var holder = getRoot(root);
      if (!holder) return;
      var buttons = holder.classList.contains('analysis-actions')
        ? holder.querySelectorAll('button')
        : [holder];
      Array.prototype.forEach.call(buttons, function (btn) {
        btn.disabled = busy;
      });
    }

    function setCellStatus(root, status) {
      var meta = getMeta(root);
      if (!meta) return null;
      var cell = meta.root.closest('td');
      if (!cell) return null;
      cell.innerHTML = renderControl(meta, status);
      return findControl(meta.type, meta.id);
    }

    function findControl(type, id) {
      var controls = document.querySelectorAll('.analysis-actions, .btn-analyze[data-status]');
      for (var i = 0; i < controls.length; i += 1) {
        if (controls[i].getAttribute('data-type') === type && controls[i].getAttribute('data-id') === id) {
          return controls[i];
        }
      }
      return null;
    }

    function setStatusByKey(type, id, status) {
      var control = findControl(type, id);
      if (control) setCellStatus(control, status);
    }

    function closeMenu(root) {
      if (!root) return;
      root.classList.remove('open');
      var toggle = root.querySelector('.analysis-menu-toggle');
      var menu = root.querySelector('.analysis-menu');
      if (toggle) toggle.setAttribute('aria-expanded', 'false');
      if (menu) {
        menu.style.left = '';
        menu.style.top = '';
      }
    }

    function closeMenus(exceptRoot) {
      document.querySelectorAll('.analysis-actions.open').forEach(function (root) {
        if (exceptRoot && root === exceptRoot) return;
        closeMenu(root);
      });
    }

    function positionMenu(root) {
      var toggle = root && root.querySelector('.analysis-menu-toggle');
      var menu = root && root.querySelector('.analysis-menu');
      if (!toggle || !menu) return;
      var gap = 4;
      var edge = 8;
      var rect = toggle.getBoundingClientRect();
      var menuRect = menu.getBoundingClientRect();
      var left = Math.max(edge, Math.min(rect.right - menuRect.width, window.innerWidth - menuRect.width - edge));
      var top = rect.bottom + gap;
      if (top + menuRect.height > window.innerHeight - edge) {
        top = Math.max(edge, rect.top - menuRect.height - gap);
      }
      menu.style.left = left + 'px';
      menu.style.top = top + 'px';
    }

    function confirmCost(meta) {
      return window.confirm(
        (meta.id || '') + '\\n' + (meta.name || '') +
        '\\n\\n분석 1건당 API 비용이 발생합니다. 진행할까요?'
      );
    }

    function openAnalysisWindow(meta) {
      if (!meta || !meta.type || !meta.id) return;
      window.open('/analysis/' + meta.type + '/' + encodeURIComponent(meta.id), '_blank');
    }

    function stopPolling(type, id) {
      var key = pollKey(type, id);
      if (_pollTimers[key]) {
        window.clearInterval(_pollTimers[key]);
        delete _pollTimers[key];
      }
    }

    function startPolling(type, id) {
      if (!type || !id) return;
      var key = pollKey(type, id);
      if (_pollTimers[key]) return;

      async function pollOnce() {
        try {
          var resp = await fetch('/api/analysis/' + type + '/' + encodeURIComponent(id) + '/status');
          var data = await readJson(resp);
          if (data.status === 'done') {
            stopPolling(type, id);
            setStatusByKey(type, id, 'done');
          } else if (data.status === 'error') {
            stopPolling(type, id);
            setStatusByKey(type, id, 'error');
            showToast('분석 실패');
          }
        } catch (e) {
          // 일시적 네트워크 오류는 다음 폴링에서 재시도한다.
        }
      }

      _pollTimers[key] = window.setInterval(pollOnce, 5000);
      pollOnce();
    }

    async function runAnalysis(meta, triggerEl) {
      if (!meta || !meta.type || !meta.id) return;
      if (!confirmCost(meta)) return;

      var key = pollKey(meta.type, meta.id);
      if (_requesting[key]) return;
      _requesting[key] = true;
      setBusy(triggerEl || meta.root, true);

      try {
        var resp = await fetch('/api/analysis/' + meta.type + '/' + encodeURIComponent(meta.id), { method: 'POST' });
        var data = await readJson(resp);
        if (!resp.ok) {
          showToast(data.message || data.detail || '분석 요청 중 오류가 발생했습니다.');
          setBusy(triggerEl || meta.root, false);
        } else if (data.status === 'analyzing') {
          setCellStatus(triggerEl || meta.root, 'analyzing');
          startPolling(meta.type, meta.id);
        } else if (data.status === 'need_upload') {
          setBusy(triggerEl || meta.root, false);
          openUploadModal(data.message, meta, triggerEl || meta.root, true);
        } else {
          showToast(data.message || '분석 요청 중 오류가 발생했습니다.');
          setBusy(triggerEl || meta.root, false);
        }
      } catch (e) {
        showToast('서버 요청 중 오류가 발생했습니다.');
        setBusy(triggerEl || meta.root, false);
      } finally {
        delete _requesting[key];
      }
    }

    function openUploadModal(message, meta, root, alreadyConfirmed) {
      if (!meta) return;
      _uploadMeta = {
        type: meta.type,
        id: meta.id,
        name: meta.name
      };
      _uploadRoot = getRoot(root);
      _uploadFiles = [];
      _uploadConfirmed = !!alreadyConfirmed;

      var modal = document.getElementById('analysisUploadModal');
      var msgEl = document.getElementById('uploadModalMessage');
      var filenameEl = document.getElementById('uploadFilename');
      var submitBtn = document.getElementById('uploadSubmitBtn');
      var input = document.getElementById('uploadFileInput');

      if (msgEl) msgEl.textContent = message || '파일을 직접 업로드해 분석하세요. 여러 개 선택할 수 있습니다.';
      if (filenameEl) filenameEl.textContent = '';
      if (submitBtn) submitBtn.disabled = true;
      if (input) input.value = '';

      if (modal) modal.classList.add('open');
    }

    function closeUploadModal() {
      var modal = document.getElementById('analysisUploadModal');
      if (modal) modal.classList.remove('open');
      _uploadFiles = [];
      _uploadMeta = null;
      _uploadRoot = null;
      _uploadConfirmed = false;
      var filenameEl = document.getElementById('uploadFilename');
      var input = document.getElementById('uploadFileInput');
      if (filenameEl) filenameEl.textContent = '';
      if (input) input.value = '';
      var submitBtn = document.getElementById('uploadSubmitBtn');
      if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.textContent = '분석 시작';
      }
    }

    document.addEventListener('click', function (e) {
      if (!e.target.closest('.analysis-actions')) closeMenus();

      var toggle = e.target.closest('.analysis-menu-toggle');
      if (toggle) {
        var root = toggle.closest('.analysis-actions');
        var willOpen = root && !root.classList.contains('open');
        closeMenus(root);
        if (root && willOpen) {
          root.classList.add('open');
          toggle.setAttribute('aria-expanded', 'true');
          positionMenu(root);
          var firstItem = root.querySelector('[role="menuitem"]');
          if (firstItem) firstItem.focus({ preventScroll: true });
        } else {
          closeMenu(root);
        }
        return;
      }

      var action = e.target.closest('[data-action]');
      if (action && action.closest('.analysis-actions')) {
        var actionMeta = getMeta(action);
        var actionName = action.getAttribute('data-action');
        closeMenus();
        if (actionName === 'view') {
          openAnalysisWindow(actionMeta);
        } else if (actionName === 'auto') {
          runAnalysis(actionMeta, action);
        } else if (actionName === 'upload') {
          openUploadModal('분석할 파일을 업로드하세요. 여러 개 선택할 수 있습니다.', actionMeta, action);
        }
        return;
      }
    });

    window.addEventListener('scroll', function () { closeMenus(); }, true);
    window.addEventListener('resize', function () { closeMenus(); });

    var uploadInput = document.getElementById('uploadFileInput');
    var dropZone = document.getElementById('uploadDropZone');
    var submitBtn = document.getElementById('uploadSubmitBtn');
    var filenameEl = document.getElementById('uploadFilename');

    function handleFiles(fileList) {
      _uploadFiles = Array.prototype.slice.call(fileList || []).filter(Boolean);
      if (filenameEl) {
        if (_uploadFiles.length === 0) {
          filenameEl.textContent = '';
        } else if (_uploadFiles.length === 1) {
          filenameEl.textContent = _uploadFiles[0].name;
        } else {
          filenameEl.textContent = _uploadFiles.length + '개 파일: '
            + _uploadFiles.map(function (file) { return file.name; }).join(', ');
        }
      }
      if (submitBtn) submitBtn.disabled = _uploadFiles.length === 0;
    }

    if (uploadInput) {
      uploadInput.addEventListener('change', function () {
        handleFiles(uploadInput.files);
      });
    }

    if (dropZone) {
      dropZone.addEventListener('click', function () {
        if (uploadInput) uploadInput.click();
      });
      dropZone.addEventListener('keydown', function (e) {
        if ((e.key === 'Enter' || e.key === ' ') && uploadInput) {
          e.preventDefault();
          uploadInput.click();
        }
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
        handleFiles(e.dataTransfer.files);
      });
    }

    if (submitBtn) {
      submitBtn.addEventListener('click', async function () {
        if (!_uploadFiles.length || !_uploadMeta) return;
        if (!_uploadConfirmed && !confirmCost(_uploadMeta)) return;
        _uploadConfirmed = true;
        submitBtn.disabled = true;
        submitBtn.textContent = '업로드 중...';

        var form = new FormData();
        _uploadFiles.forEach(function (file) {
          form.append('files', file);
        });
        form.append('source_type', _uploadMeta.type);
        form.append('source_id', _uploadMeta.id);
        form.append('type', _uploadMeta.type);
        form.append('id', _uploadMeta.id);

        try {
          var resp = await fetch('/api/analysis/upload', { method: 'POST', body: form });
          var data = await readJson(resp);
          var msgEl = document.getElementById('uploadModalMessage');

          if (!resp.ok) {
            if (msgEl) msgEl.textContent = data.message || data.detail || '업로드 중 오류가 발생했습니다.';
            submitBtn.disabled = false;
            submitBtn.textContent = '분석 시작';
          } else if (data.status === 'analyzing') {
            var meta = _uploadMeta;
            var root = findControl(meta.type, meta.id) || _uploadRoot;
            closeUploadModal();
            if (root) setCellStatus(root, 'analyzing');
            startPolling(meta.type, meta.id);
          } else {
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

    document.addEventListener('keydown', function (e) {
      var root = e.target.closest('.analysis-actions.open');
      if (root) {
        var items = Array.prototype.slice.call(root.querySelectorAll('[role="menuitem"]'));
        var index = items.indexOf(e.target);
        if (['ArrowDown', 'ArrowUp', 'Home', 'End'].indexOf(e.key) !== -1 && items.length) {
          e.preventDefault();
          if (index < 0) index = (e.key === 'ArrowUp') ? items.length : -1;
          if (e.key === 'Home') index = 0;
          else if (e.key === 'End') index = items.length - 1;
          else if (e.key === 'ArrowDown') index = (index + 1) % items.length;
          else index = (index - 1 + items.length) % items.length;
          items[index].focus({ preventScroll: true });
          return;
        }
        if (e.key === 'Escape') {
          e.preventDefault();
          closeMenu(root);
          root.querySelector('.analysis-menu-toggle').focus();
          return;
        }
        if (e.key === 'Tab') closeMenu(root);
      }
      if (e.key === 'Escape') {
        var modal = document.getElementById('analysisUploadModal');
        if (modal && modal.classList.contains('open')) closeUploadModal();
        closeMenus();
      }
    });

    function startInitialPolling() {
      document.querySelectorAll('.btn-analyze[data-status="analyzing"]').forEach(function (btn) {
        startPolling(btn.getAttribute('data-type'), btn.getAttribute('data-id'));
      });
    }

    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', startInitialPolling);
    } else {
      startInitialPolling();
    }

  })();
"""

# 업로드 모달 + 토스트 HTML (list·pre-spec 공용, 스크립트 앞에 한 번만 삽입)
_ANALYSIS_MODAL_HTML = """
    <!-- 분석 업로드 모달 (Phase 8.2) -->
    <div id="analysisUploadModal" class="modal-overlay" aria-label="제안요청서 업로드" role="dialog">
      <div class="modal-content">
        <div class="modal-header">
          <h3>제안요청서 직접 업로드</h3>
          <button type="button" class="modal-close-btn" aria-label="닫기">&times;</button>
        </div>
        <div class="modal-body">
          <p class="modal-message" id="uploadModalMessage">파일을 직접 업로드해 분석하세요. 여러 개 선택할 수 있습니다.</p>
          <div class="upload-area" id="uploadDropZone" role="button" tabindex="0"
               aria-label="파일 선택 영역">
            <p>PDF, HWP, HWPX, DOC, DOCX 파일을 끌어다 놓거나 클릭해 선택</p>
            <p class="upload-hint">최대 10개, 파일당 최대 50MB, 합산 최대 100MB</p>
            <input type="file" id="uploadFileInput"
                   accept=".pdf,.hwp,.hwpx,.doc,.docx" multiple hidden>
          </div>
          <p class="upload-filename" id="uploadFilename"></p>
        </div>
        <div class="modal-footer">
          <button type="button" id="uploadSubmitBtn" class="modal-submit" disabled>분석 시작</button>
          <button type="button" class="modal-cancel">취소</button>
        </div>
      </div>
    </div>
    <!-- 에러 토스트 (Phase 8.2) -->
    <div id="analyzeToast" role="alert"></div>
    <!-- 장바구니 토스트 (Phase 7.2) -->
    <div id="cartToast" role="alert"></div>
"""

# 장바구니 모달 HTML (list·pre-spec 공용, Phase 7.2)
_CART_MODAL_HTML = """
    <!-- 장바구니 모달 (Phase 7.2) -->
    <div id="cart-modal" class="modal-overlay" style="display:none" role="dialog" aria-label="검토항목 장바구니">
      <div class="modal-content cart-modal-content">
        <div class="modal-header" style="background:#1565C0;">
          <h3>검토항목 장바구니</h3>
          <button class="modal-close-btn" onclick="toggleCartModal()" aria-label="닫기">&times;</button>
        </div>
        <div class="cart-modal-body">
          <div id="cart-items-list"></div>
        </div>
        <div class="modal-footer cart-modal-footer">
          <div class="cart-footer-left">
            <button id="btn-cart-files" onclick="downloadCartFiles()" class="btn-primary" disabled>파일다운로드</button>
            <button id="btn-cart-del-selected" onclick="deleteSelectedCartItems()" class="btn-secondary" disabled>선택삭제</button>
          </div>
          <div class="cart-footer-right">
            <button onclick="clearCart()" class="btn-secondary">전체 삭제</button>
            <button onclick="downloadCart()" class="btn-primary">엑셀 다운로드</button>
          </div>
        </div>
      </div>
    </div>
"""

# 장바구니 공통 JS (list·pre-spec 페이지 양쪽에 삽입, Phase 7.2)
_CART_SCRIPT = """
  var cartFilesDownloading = false;
  var cartSelectedDeleting = false;
  var cartToastTimer = null;

  function downloadInFrame(url, onError) {
    var frame = document.createElement('iframe');
    frame.hidden = true;
    var cleanup = setTimeout(function () { frame.remove(); }, 30 * 60 * 1000);
    frame.addEventListener('load', function () {
      var message;
      try {
        message = frame.contentDocument.body.textContent.trim();
      } catch (error) {
        return;
      }
      if (message) {
        clearTimeout(cleanup);
        frame.remove();
        onError(message);
      }
    });
    document.body.appendChild(frame);
    frame.src = url;
  }

  function escapeHtml(str) {
    return (str || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }

  function showCartToast(msg, ms) {
    var t = document.getElementById('cartToast');
    if (!t) return;
    clearTimeout(cartToastTimer);
    t.textContent = msg;
    t.style.display = 'block';
    cartToastTimer = setTimeout(function () { t.style.display = 'none'; }, ms === undefined ? 3000 : ms);
  }

  function updateCartBadge(count) {
    var badge = document.getElementById('cart-badge');
    var menuBtn = document.getElementById('btn-cart-menu');
    if (!badge) return;
    badge.textContent = count || 0;
    badge.style.display = (count > 0) ? '' : 'none';
    if (menuBtn) menuBtn.classList.toggle('has-items', count > 0);
  }

  function updateCartAddButton() {
    var checked = document.querySelectorAll('.row-select-chk:checked');
    var btn = document.getElementById('btn-add-to-cart');
    var countEl = document.getElementById('cart-add-count');
    if (btn) {
      btn.style.display = checked.length > 0 ? '' : 'none';
      if (countEl) countEl.textContent = checked.length;
    }
  }

  function updateCartSelectionButtons() {
    var hasSelection = document.querySelectorAll('.cart-select-chk:checked').length > 0;
    var filesBtn = document.getElementById('btn-cart-files');
    var deleteBtn = document.getElementById('btn-cart-del-selected');
    if (filesBtn) filesBtn.disabled = !hasSelection || cartFilesDownloading;
    if (deleteBtn) deleteBtn.disabled = !hasSelection || cartSelectedDeleting;
  }

  function renderCartItems(items) {
    var el = document.getElementById('cart-items-list');
    if (!el) return;
    if (!items.length) {
      el.innerHTML = '<p class="cart-empty">담긴 항목이 없습니다.</p>';
      updateCartSelectionButtons();
      return;
    }
    el.innerHTML = items.map(function (item) {
      var typeLabel = item.item_type === 'bid' ? '입찰' : '사전규격';
      var typeCls = item.item_type === 'bid' ? 'type-bid' : 'type-pre';
      var detailUrl = typeof item.detail_url === 'string' ? item.detail_url : '';
      var titleHtml = escapeHtml(item.title);
      if (item.item_type === 'bid'
          && (detailUrl.indexOf('http://') === 0 || detailUrl.indexOf('https://') === 0)) {
        titleHtml = '<a href="' + escapeHtml(detailUrl) + '" target="_blank" rel="noopener">' + titleHtml + '</a>';
      }
      return '<div class="cart-item-row" data-cart-id="' + item.cart_id + '">'
        + '<input type="checkbox" class="cart-select-chk"'
        + ' data-cart-id="' + escapeHtml(String(item.cart_id)) + '"'
        + ' data-item-type="' + escapeHtml(item.item_type) + '"'
        + ' data-item-id="' + escapeHtml(item.item_id) + '"'
        + ' data-file-count="' + escapeHtml(String(item.file_count || 0)) + '">'
        + '<span class="cart-item-type ' + typeCls + '">' + typeLabel + '</span>'
        + '<span class="cart-item-title" title="' + escapeHtml(item.title) + '">' + titleHtml + '</span>'
        + '<button class="btn-cart-search" data-search-url="' + escapeHtml(item.search_url) + '"'
        + ' onclick="openCartSearch(this)">검색</button>'
        + '<button class="btn-cart-del" onclick="deleteCartItem(' + item.cart_id + ')">삭제</button>'
        + '</div>';
    }).join('');
    updateCartSelectionButtons();
  }

  function openCartSearch(button) {
    window.open(button.dataset.searchUrl, '_blank', 'noopener');
  }

  async function loadCartItems() {
    var resp = await fetch('/api/export-cart');
    var data = await resp.json();
    renderCartItems(data.items);
    updateCartBadge(data.count);
  }

  async function toggleCartModal() {
    var modal = document.getElementById('cart-modal');
    if (!modal) return;
    var opening = modal.style.display === 'none';
    modal.style.display = opening ? 'flex' : 'none';
    if (opening) await loadCartItems();
  }

  async function deleteCartItem(cartId) {
    await fetch('/api/export-cart/' + cartId, { method: 'DELETE' });
    await loadCartItems();
  }

  async function downloadCartFiles() {
    if (cartFilesDownloading) return;
    var selected = Array.from(document.querySelectorAll('.cart-select-chk:checked')).map(function (c) {
      return {
        itemType: c.dataset.itemType,
        itemId: c.dataset.itemId,
        fileCount: parseInt(c.dataset.fileCount || '0', 10),
      };
    });
    var targets = selected.filter(function (item) { return item.fileCount > 0; });
    var excluded = selected.length - targets.length;
    if (!targets.length) {
      showCartToast('선택한 항목에 첨부파일이 없습니다.');
      return;
    }
    cartFilesDownloading = true;
    updateCartSelectionButtons();
    var failures = 0;
    try {
      var message = targets.length + '건 다운로드를 시작했습니다.';
      if (excluded) message += ' (첨부 없음 ' + excluded + '건 제외)';
      showCartToast(message);
      for (var i = 0; i < targets.length; i++) {
        var item = targets[i];
        var base = item.itemType === 'bid'
          ? '/list/' + encodeURIComponent(item.itemId)
          : '/pre-spec/' + encodeURIComponent(item.itemId);
        var url = base + (item.fileCount >= 2 ? '/files.zip' : '/files/single');
        (function (itemId) {
          downloadInFrame(url, function (message) {
            failures++;
            showCartToast('다운로드 실패 ' + failures + '건 — ' + itemId + ': ' + message, 6000);
          });
        })(item.itemId);
        if (i < targets.length - 1) {
          await new Promise(function (resolve) { setTimeout(resolve, 800); });
        }
      }
    } finally {
      cartFilesDownloading = false;
      updateCartSelectionButtons();
    }
  }

  async function deleteSelectedCartItems() {
    if (cartSelectedDeleting) return;
    var selected = Array.from(document.querySelectorAll('.cart-select-chk:checked'));
    if (!selected.length) return;
    if (!confirm('선택한 ' + selected.length + '건을 장바구니에서 삭제할까요?')) return;
    var succeeded = 0;
    var failed = 0;
    cartSelectedDeleting = true;
    updateCartSelectionButtons();
    try {
      for (var i = 0; i < selected.length; i++) {
        try {
          var resp = await fetch('/api/export-cart/' + selected[i].dataset.cartId, { method: 'DELETE' });
          if (resp.ok) succeeded += 1;
          else failed += 1;
        } catch (error) {
          failed += 1;
        }
      }
    } finally {
      try {
        await loadCartItems();
      } finally {
        cartSelectedDeleting = false;
        updateCartSelectionButtons();
        showCartToast('선택삭제 완료: ' + succeeded + '건 성공, ' + failed + '건 실패');
      }
    }
  }

  async function clearCart() {
    if (!confirm('선택 항목을 모두 삭제할까요?')) return;
    await fetch('/api/export-cart/all', { method: 'DELETE' });
    await loadCartItems();
  }

  function downloadCart() {
    window.location.href = '/api/export-cart/download';
  }

  async function addToCart() {
    var items = Array.from(document.querySelectorAll('.row-select-chk:checked')).map(function (c) {
      return { item_type: c.dataset.itemType, item_id: c.dataset.itemId };
    });
    if (!items.length) return;
    var resp = await fetch('/api/export-cart', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ items: items }),
    });
    var data = await resp.json();
    // 체크 해제
    document.querySelectorAll('.row-select-chk:checked').forEach(function (c) { c.checked = false; });
    var chkAll = document.getElementById('chk-all');
    if (chkAll) chkAll.checked = false;
    updateCartAddButton();
    // 배지 갱신
    fetch('/api/export-cart').then(function (r) { return r.json(); }).then(function (d) { updateCartBadge(d.count); });
    showCartToast('담기 완료: ' + (data.added || 0) + '건 추가');
  }

  // 체크박스 변경 이벤트 위임
  document.addEventListener('change', function (e) {
    if (e.target.id === 'chk-all') {
      document.querySelectorAll('.row-select-chk').forEach(function (c) { c.checked = e.target.checked; });
      updateCartAddButton();
    } else if (e.target.classList.contains('row-select-chk')) {
      updateCartAddButton();
    } else if (e.target.classList.contains('cart-select-chk')) {
      updateCartSelectionButtons();
    }
  });

  // 장바구니 모달 바깥 클릭 시 닫기
  document.addEventListener('click', function (e) {
    var modal = document.getElementById('cart-modal');
    if (modal && e.target === modal) modal.style.display = 'none';
  });

  // ESC 키로 장바구니 모달 닫기
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') {
      var modal = document.getElementById('cart-modal');
      if (modal && modal.style.display !== 'none') modal.style.display = 'none';
    }
  });

  // 페이지 로드 시 배지 초기화
  document.addEventListener('DOMContentLoaded', function () {
    fetch('/api/export-cart').then(function (r) { return r.json(); }).then(function (d) { updateCartBadge(d.count); });
  });
"""


def _render_pre_spec_rows(
    rows: list[dict],
    sort: str,
    qs: dict[str, str],
    status_map: dict[str, str] | None = None,
) -> str:
    """사전규격 목록 테이블 렌더(_render_list_rows 패턴). 파일 컬럼 포함(4.9-B2)."""
    if not rows:
        return '<p class="muted">조건에 맞는 사전규격이 없습니다.</p>'
    analysis_on = analysis_enabled()
    # 전체선택 체크박스 헤더
    chk_head = '<th class="col-chk"><input type="checkbox" id="chk-all" title="전체선택"></th>'
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
    head += "<th>파일</th>"
    if analysis_on:
        head += "<th>분석</th>"
    body_rows = []
    for i, r in enumerate(rows, start=1):
        raw_id = str(r.get("bf_spec_rgst_no") or "")
        row_id = _e(raw_id)
        chk_cell = (
            f'<td class="col-chk">'
            f'<input type="checkbox" class="row-select-chk" '
            f'data-item-type="pre_spec" data-item-id="{row_id}">'
            f'</td>'
        )
        cells = [chk_cell, f"<td>{i}</td>"]
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
        # 분석 버튼 컬럼 (Phase 8.2, USE_ANALYSIS_PROVIDER 토글)
        if analysis_on:
            status = (status_map or {}).get(raw_id, "none")
            cells.append(_render_analysis_cell(
                source_type="pre-spec",
                source_id=raw_id,
                source_name=r.get("prdct_clsfc_no_nm") or "",
                status=status,
                cell_class="analysis-col",
            ))
        body_rows.append(f"<tr data-row-id=\"{row_id}\">{''.join(cells)}</tr>")
    return f"""
    <div class="table-wrap">
      <table>
        <thead><tr>{chk_head}<th>#</th>{head}</tr></thead>
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
    var zipAllMsg = document.getElementById('psZipAllMsg');
    var zipDownloads = Object.create(null);
    zipAll.addEventListener('click', function (event) {
      if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      var url = zipAll.getAttribute('href');
      if (!url || url === '#') return;
      zipAllMsg.textContent = '다운로드를 요청했습니다. 첨부가 많으면 시작까지 시간이 걸릴 수 있습니다.';
      zipAllMsg.classList.add('info');
      zipAllMsg.hidden = false;
      if (zipDownloads[url]) return;
      var timer = setTimeout(function () { delete zipDownloads[url]; }, 60 * 1000);
      zipDownloads[url] = timer;
      downloadInFrame(url, function (message) {
        if (zipDownloads[url] === timer) {
          clearTimeout(timer);
          delete zipDownloads[url];
        }
        if (zipAll.getAttribute('href') !== url) return;
        zipAllMsg.classList.remove('info');
        zipAllMsg.textContent = message;
        zipAllMsg.hidden = false;
      });
    });

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
        zipAllMsg.textContent = '';
        zipAllMsg.hidden = true;
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
    price_set: str | None = None,    # 참이면 빈 가격도 한도 없음으로 명시
    include_past: str | None = None,   # 체크 시 의견마감 지난 항목 포함
    excl_kw: str | None = None,        # 품명/사업명 제외 키워드 CSV(Phase 10)
    excl_kw_set: str | None = None,    # 폼 제출 표식(없으면 설정 기본값 적용)
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

    analysis_on = analysis_enabled()
    status_map: dict[str, str] = {}
    # 가격: 화면 입력값 우선. 빈칸은 price_set 표식이 있으면 한도 없음, 없으면(첫 진입) 설정 기본값.
    # list_page 의 cfg 기본값 로직과 동형.
    with SessionLocal() as session:
        cfg = repository.get_config(session)
        cfg_amt_bgn = cfg.pre_spec_amt_bgn
        cfg_amt_end = cfg.pre_spec_amt_end

        price_min_in = price_min if (price_min is not None and str(price_min).strip()) else None
        price_max_in = price_max if (price_max is not None and str(price_max).strip()) else None
        # 명시한 검색은 빈칸을 한도 없음으로, 첫 진입은 설정 기본값으로 해석한다.
        price_min_disp = (_parse_price(price_min_in)
                          if price_set in _EXCL_KW_SET_TRUE or price_min_in is not None
                          else _parse_price(cfg_amt_bgn))
        price_max_disp = (_parse_price(price_max_in)
                          if price_set in _EXCL_KW_SET_TRUE or price_max_in is not None
                          else _parse_price(cfg_amt_end))

        # 제외 키워드(Phase 10): /list 와 동일 규칙(폼 제출이면 입력값, 아니면 설정 기본값).
        kw_options = repository.list_exclude_keywords(session)
        if excl_kw_set in _EXCL_KW_SET_TRUE:
            excl_kws = _parse_excl_kw(excl_kw)
        else:
            excl_kws = repository.get_default_exclude_keywords(session)

        objs, total = repository.search_pre_specs(
            session,
            q=q,
            instt=instt,
            dt_from=df,
            dt_to=dtto,
            price_min=price_min_disp,
            price_max=price_max_disp,
            include_past_opnin=include_past_flag,
            exclude_keywords=excl_kws,
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
        if analysis_on:
            status_map = repository.get_analysis_status_map(
                session,
                "pre_spec",
                [str(r.get("bf_spec_rgst_no")) for r in rows if r.get("bf_spec_rgst_no")],
            )

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
        # 원문 CSV를 보존하고 링크를 만들 때 한 번만 URL 인코딩한다.
        "excl_kw": ",".join(excl_kws),
        "excl_kw_set": "1",
        "price_set": "1",
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
      <input type="hidden" name="price_set" value="1">
      <label class="field query-field">
        <span class="flabel">품명/사업명 부분검색</span>
        <input type="text" name="q" value="{_e(q or '')}" placeholder="예: 소프트웨어 유지보수">
      </label>
      <button type="submit" class="submit">검색</button>
      {_excl_kw_summary(excl_kws)}"""

    # _filter_card detail: 제외 키워드·기관·날짜·가격·지난마감·표기개수.
    detail_html = f"""
      <div class="row" style="margin-bottom:10px;">
        {_excl_kw_field(excl_kws, kw_options, "품명/사업명 제외 키워드")}
      </div>
      <div class="row" style="margin-bottom:10px;">
        <label class="field instt-field">
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
        right_html='<button id="btn-add-to-cart" class="btn-cart-add" style="display:none;margin-left:auto;" onclick="addToCart()">담기 <span id="cart-add-count">0</span>건</button>',
    )
    analysis_ui_html = _ANALYSIS_MODAL_HTML if analysis_on else '    <div id="cartToast" role="alert"></div>'
    analysis_script_html = f"<script>{_ANALYSIS_SCRIPT}</script>" if analysis_on else ""

    body = f"""
    {filter_card}

    <div class="card">
      <h2>수집된 사전규격</h2>
      {_render_pre_spec_rows(rows, sort, qs, status_map)}
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
        <p id="psZipAllMsg" class="zipall-msg" role="status" hidden></p>
        <table class="filetable">
          <thead><tr><th class="c-fn">파일명</th><th class="c-dl">다운로드</th></tr></thead>
          <tbody id="psFileList"></tbody>
        </table>
      </div>
    </aside>

    {analysis_ui_html}
    {_CART_MODAL_HTML}
    <script>{_PRE_SPEC_SCRIPT}</script>
    <script>{_EXCL_KW_SCRIPT}</script>
    {analysis_script_html}
    <script>{_CART_SCRIPT}</script>"""

    return HTMLResponse(
        _shell("나라장터 사전규격 목록", "수집·저장된 사전규격을 조회합니다.", "pre-spec", body)
    )


@app.get("/", response_class=HTMLResponse)
def root() -> RedirectResponse:
    return RedirectResponse(url="/list", status_code=307)


def _parse_price(s: str | None) -> int | None:
    """가격 입력(문자열, 콤마 허용) → 유한한 0 이상 정수. 빈값·오류는 None."""
    if not s:
        return None
    s = str(s).strip().replace(",", "")
    if not s:
        return None
    try:
        value = int(s)
    except ValueError:
        try:
            # "1234.0" 같은 소수 표기 방어(설정값이 그렇게 저장될 수 있음).
            numeric = float(s)
            if not math.isfinite(numeric):
                return None
            value = int(numeric)
        except (ValueError, OverflowError):
            return None
    # SQLite 정수 비교 범위를 벗어나는 값은 필터에서 무시한다.
    if value < 0 or value > 9_223_372_036_854_775_807:
        return None
    return value


@app.get("/list", response_class=HTMLResponse)
def list_page(
    q: str | None = None,
    dt_from: str | None = None,
    dt_to: str | None = None,
    date_field: str = "bid_ntce_dt",
    dt_range: str | None = None,
    kind: list[str] | None = Query(None),
    price_min: str | None = None,
    price_max: str | None = None,
    price_set: str | None = None,    # 참이면 빈 가격도 한도 없음으로 명시
    include_past: str | None = None,
    excl_privt: list[str] | None = Query(None),
    excl_kw: str | None = None,
    excl_kw_set: str | None = None,
    sort: str = "bid_ntce_dt_desc",
    page: int = 1,
    page_size: int = 50,
) -> HTMLResponse:
    # page_size 정규화: 허용값 {10,30,50,100}, 그 외/오류는 50.
    page_size = page_size if page_size in (10, 30, 50, 100) else 50
    # 기본 동작 = 개찰 지난 공고 숨김. "지난 개찰 포함" 체크 시에만 전체 노출.
    include_past_flag = include_past in ("1", "on", "true", "Y", "y")
    # 수의계약 제외(기본 True). 체크 해제와 "파라미터 없음(첫 진입)"을 구분하기 위해
    # 폼은 hidden(0)+checkbox(1) 쌍으로 전송한다: 없음=기본(제외), "1" 포함=제외, 그 외=포함.
    excl_privt_flag = True if not excl_privt else ("1" in excl_privt)
    # 정렬: 일시 필드 4종·배정예산·추정가격(북마크 호환)의 asc/desc 12종. 미허용·빈값은 기본.
    sort = sort if sort in _LIST_SORTS else _DEFAULT_SORT
    # 날짜 적용 컬럼: 공고일/개찰일.
    date_field = date_field if date_field in ("bid_ntce_dt", "openg_dt") else "bid_ntce_dt"
    try:
        page = max(1, int(page))
    except (TypeError, ValueError):
        page = 1

    # 다중 파라미터와 CSV를 합쳐 유효 종류만 남긴다. 전체 해제는 기본 3종으로 복원.
    requested_kinds = {code.strip() for value in (kind or []) for code in value.split(",")}
    selected_kinds = [code for code in _LIST_KINDS if code in requested_kinds]
    if not selected_kinds:
        selected_kinds = list(_LIST_DEFAULT_KINDS)

    # 기존 직접 날짜 URL을 보존하고 명시한 프리셋은 요청 날짜보다 우선한다.
    if dt_range not in dict(_LIST_DATE_RANGES):
        dt_range = "custom" if dt_from or dt_to else "1m"
    today = date.today()
    date_presets = {
        field: {preset: _list_preset_date_range(field, preset, today)
                for preset, _label in _LIST_DATE_RANGES if preset != "custom"}
        for field in ("bid_ntce_dt", "openg_dt")
    }
    if dt_range == "custom":
        def_from, def_to = _list_default_date_range(date_field, today)
        dt_from_eff = dt_from.strip() if (dt_from and dt_from.strip()) else def_from
        dt_to_eff = dt_to.strip() if (dt_to and dt_to.strip()) else def_to
    else:
        dt_from_eff, dt_to_eff = date_presets[date_field][dt_range]

    df = _parse_date(dt_from_eff)
    dtto = _parse_date(dt_to_eff, end_of_day=True)

    analysis_on = analysis_enabled()
    status_map: dict[str, str] = {}
    # 배정예산: 화면 입력값 우선. 빈칸은 price_set 표식이 있으면 한도 없음, 없으면(첫 진입) 설정 기본값.
    with SessionLocal() as session:
        cfg = repository.get_config(session)
        cfg_price_bgn = cfg.presmpt_prce_bgn
        cfg_price_end = cfg.presmpt_prce_end
        match_codes = classifier.load_match_codes(session)

        price_min_in = price_min if (price_min is not None and str(price_min).strip()) else None
        price_max_in = price_max if (price_max is not None and str(price_max).strip()) else None
        # 명시한 검색은 빈칸을 한도 없음으로, 첫 진입은 설정 기본값으로 해석한다.
        price_min_disp = (_parse_price(price_min_in)
                          if price_set in _EXCL_KW_SET_TRUE or price_min_in is not None
                          else _parse_price(cfg_price_bgn))
        price_max_disp = (_parse_price(price_max_in)
                          if price_set in _EXCL_KW_SET_TRUE or price_max_in is not None
                          else _parse_price(cfg_price_end))

        # 제외 키워드(Phase 10): 폼 제출(excl_kw_set)이면 입력값 그대로, 아니면 설정 기본값.
        kw_options = repository.list_exclude_keywords(session)
        if excl_kw_set in _EXCL_KW_SET_TRUE:
            excl_kws = _parse_excl_kw(excl_kw)
        else:
            excl_kws = repository.get_default_exclude_keywords(session)

        objs, total = repository.search_bid_notices(
            session,
            q=q,
            dt_from=df,
            dt_to=dtto,
            date_field=date_field,
            price_min=price_min_disp,
            price_max=price_max_disp,
            include_past_openg=include_past_flag,
            exclude_privt_cntrct=excl_privt_flag,
            exclude_keywords=excl_kws,
            ntce_kinds=[_LIST_KINDS[code] for code in selected_kinds],
            sort=sort,
            page=page,
            page_size=page_size,
        )
        # 세션 종료 후 lazy 접근 금지 → 필요한 스칼라만 dict 로 추출.
        # 첨부 URL 컬럼은 파일 개수 계산용으로만 읽고(이미 조회한 ORM 행에서 → N+1 없음),
        # 화면에는 개수만 전달한다(drawer 열 때 /files 로 상세 조회).
        cols = (
            [c for c, k, _ in _LIST_COLUMNS if k not in _LIST_TIME_FIELDS]
            + [field for fields in _LIST_TIME_FIELDS.values() for field, _label in fields]
            + [
                "bid_ntce_nm", "bid_ntce_ord", "dminstt_nm",
                "bid_ntce_dtl_url", "bid_methd_nm", "bef_bid_bbanc_no",
                "match_basis", "match_detail",
            ]
            + _SPEC_URL_COLUMNS
        )
        rows = []
        for o in objs:
            d = {c: getattr(o, c, None) for c in cols}
            d["_file_count"] = sum(1 for c in _SPEC_URL_COLUMNS if d.get(c))
            rows.append(d)
        if analysis_on:
            status_map = repository.get_analysis_status_map(
                session,
                "bid",
                [str(r.get("bid_ntce_no")) for r in rows if r.get("bid_ntce_no")],
            )

    # 입력칸에는 천 단위 콤마, URL 필터값에는 숫자만 유지한다.
    price_min_field = "" if price_min_disp is None else f"{price_min_disp:,}"
    price_max_field = "" if price_max_disp is None else f"{price_max_disp:,}"
    price_min_qs = "" if price_min_disp is None else str(price_min_disp)
    price_max_qs = "" if price_max_disp is None else str(price_max_disp)

    # 쿼리스트링 보존(페이지 이동·헤더 정렬 시 다른 필터 유지). 유효 날짜(서버 기본 포함)를 싣는다.
    qs = {
        "q": q or "",
        "dt_from": dt_from_eff or "",
        "dt_to": dt_to_eff or "",
        "date_field": date_field,
        "dt_range": dt_range,
        "kind": ",".join(selected_kinds),
        "price_min": price_min_qs,
        "price_max": price_max_qs,
        "include_past": "1" if include_past_flag else "",
        "excl_privt": "1" if excl_privt_flag else "0",
        # 원문 CSV를 보존하고 링크를 만들 때 한 번만 URL 인코딩한다.
        "excl_kw": ",".join(excl_kws),
        "excl_kw_set": "1",
        "price_set": "1",
        "sort": sort,
        "page_size": str(page_size),
    }

    nf_sel = " selected" if date_field == "bid_ntce_dt" else ""
    og_sel = " selected" if date_field == "openg_dt" else ""
    preset_buttons = "".join(
        f'<button type="button" class="{"is-active" if value == dt_range else ""}" '
        f'data-date-preset="{value}" aria-pressed="{"true" if value == dt_range else "false"}">{label}</button>'
        for value, label in _LIST_DATE_RANGES if value != "custom"
    )
    kind_checks = "".join(
        f'<label class="list-toggle-chip"><input type="checkbox" name="kind" value="{code}"'
        f'{" checked" if code in selected_kinds else ""}><span>{label}</span></label>'
        for code, label in _LIST_KINDS.items()
    )

    # 표기개수 select 옵션
    ps_opts = "".join(
        f'<option value="{v}"{" selected" if v == page_size else ""}>{v}건</option>'
        for v in (10, 30, 50, 100)
    )

    # 접힌 상태에는 상단 입력 한 줄과 날짜 기준·입력·프리셋 행을 항상 표시한다.
    summary_html = f"""
      <input type="hidden" name="price_set" value="1">
      <div class="list-summary-main">
        <label class="field query-field">
          <span class="flabel">공고명</span>
          <input type="text" name="q" value="{_e(q or '')}" placeholder="예: 소프트웨어 유지보수">
        </label>
        <label class="field price-max-field">
          <span class="flabel">배정예산 최대</span>
          <input type="text" inputmode="numeric" class="price-comma" name="price_max" id="price_max" value="{_e(price_max_field)}" placeholder="예: 100,000,000">
        </label>
        <label class="field">
          <select name="page_size" aria-label="표기개수" onchange="this.form.submit()">{ps_opts}</select>
        </label>
        <button type="submit" class="submit">검색</button>
        {_filter_toggle_button("filterCard")}
        {_excl_kw_summary(excl_kws)}
        <button id="btn-add-to-cart" class="btn-cart-add" style="display:none;margin-left:auto;" onclick="addToCart()">담기 <span id="cart-add-count">0</span>건</button>
      </div>
      <div class="list-filter-row list-date-group" id="list-date-group" data-preset-ranges="{_e(json.dumps(date_presets))}">
        <span class="list-group-label">공고/개찰일자</span>
        <div class="list-row-controls date-controls">
          <select name="date_field" id="date_field" aria-label="날짜 기준">
            <option value="bid_ntce_dt"{nf_sel}>공고일</option>
            <option value="openg_dt"{og_sel}>개찰일</option>
          </select>
          <input type="date" name="dt_from" id="dt_from" value="{_e(dt_from_eff)}" aria-label="시작 날짜">
          <span class="tilde">~</span>
          <input type="date" name="dt_to" id="dt_to" value="{_e(dt_to_eff)}" aria-label="종료 날짜">
          <input type="hidden" name="dt_range" id="dt_range" value="{_e(dt_range)}">
          <div class="preset-buttons" aria-label="날짜 기간 프리셋">{preset_buttons}</div>
        </div>
      </div>"""

    # detail_html: 토글로 펼쳐지는 상세 필터(핵심 입력과 같은 form).
    detail_html = f"""
      <div class="list-detail-rows">
        <div class="list-filter-row list-input-row">
          <div class="list-row-controls">{_excl_kw_field(excl_kws, kw_options, "공고명 제외 키워드")}</div>
        </div>
        <div class="list-filter-row">
          <span class="list-group-label">공고종류</span>
          <div class="list-row-controls">{kind_checks}</div>
        </div>
        <div class="list-filter-row">
          <span class="list-group-label">조회옵션</span>
          <div class="list-row-controls">
            <label class="list-toggle-chip"><input type="checkbox" name="include_past" value="1"{' checked' if include_past_flag else ''}><span>지난 개찰 포함</span></label>
            <label class="list-toggle-chip">
              <input type="hidden" name="excl_privt" value="0">
              <input type="checkbox" name="excl_privt" value="1"{' checked' if excl_privt_flag else ''}><span>수의계약제외</span>
            </label>
          </div>
        </div>
        <div class="list-filter-row list-input-row">
          <div class="list-row-controls">
            <label class="field">
              <span class="flabel">배정예산 최소</span>
              <input type="text" inputmode="numeric" class="price-comma" name="price_min" id="price_min" value="{_e(price_min_field)}" placeholder="예: 10,000,000">
            </label>
          </div>
        </div>
        <input type="hidden" name="sort" value="{_e(sort)}">
      </div>"""
    analysis_ui_html = _ANALYSIS_MODAL_HTML if analysis_on else '    <div id="cartToast" role="alert"></div>'
    analysis_script_html = f"<script>{_ANALYSIS_SCRIPT}</script>" if analysis_on else ""

    body = f"""
    {_filter_card(action="/list", summary_html=summary_html, detail_html=detail_html, title="공고 검색", card_id="filterCard", extra_class="list-filter",
        toggle_inside_summary=True)}

    <div class="card">
      <div class="list-card-heading">
        <h2>수집된 공고</h2>
        <span class="rangeinfo">{'개찰일' if date_field == 'openg_dt' else '공고일'} {_e(dt_from_eff)} ~ {_e(dt_to_eff)}</span>
      </div>
      {_render_list_rows(rows, sort, qs, status_map, match_codes=match_codes)}
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
        <p id="zipAllMsg" class="zipall-msg" role="status" hidden></p>
        <table class="filetable">
          <thead><tr><th class="c-fn">파일명</th><th class="c-dl">다운로드</th></tr></thead>
          <tbody id="fileList"></tbody>
        </table>
      </div>
    </aside>

    {analysis_ui_html}
    {_CART_MODAL_HTML}
    <div id="indTip" class="ind-tip" role="tooltip" hidden></div>
    <script>{_LIST_MATCHIND_TIP_SCRIPT}</script>
    <script>{_LIST_SCRIPT}</script>
    <script>{_EXCL_KW_SCRIPT}</script>
    {analysis_script_html}
    <script>{_CART_SCRIPT}</script>"""

    return HTMLResponse(
        _shell(
            "나라장터 입찰공고 목록",
            "수집·저장된 공고를 조회합니다.",
            "list",
            body,
            extra_css=_LIST_MATCHIND_TIP_CSS,
        )
    )


# --- /list 파일 다운로드 (Phase 4.1) ---------------------------------
# zip 스트리밍 가드: 외부 다운로드 타임아웃·누적 총량 상한(메모리 보호).
_FILE_HTTP_TIMEOUT = 30.0
_ZIP_TOTAL_LIMIT_BYTES = 300 * 1024 * 1024  # 300MB 누적 상한


def _filename_from_cd(resp: httpx.Response, fallback: str) -> str:
    """Content-Disposition 헤더에서 파일명 추출. 없거나 실패하면 fallback 반환."""
    from urllib.parse import unquote as _unquote
    cd = resp.headers.get("content-disposition", "")
    for part in cd.split(";"):
        part = part.strip()
        if part.startswith("filename*="):
            name = part.split("=", 1)[1].strip()
            if "UTF-8''" in name.upper():
                return _unquote(name.split("''", 1)[1])
            return name
        if part.startswith("filename="):
            name = part.split("=", 1)[1].strip().strip('"')
            if "%" in name:
                return _unquote(name)
            return name or fallback
    return fallback


async def _single_file_response(item_id: str, file: dict[str, Any]) -> Response:
    """DB 첨부 목록에서 고른 단일 파일을 원본 응답으로 반환."""
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=_FILE_HTTP_TIMEOUT
        ) as client:
            async with client.stream("GET", file["url"]) as resp:
                resp.raise_for_status()
                content_length = resp.headers.get("content-length")
                try:
                    declared_size = int(content_length) if content_length is not None else None
                except (TypeError, ValueError):
                    declared_size = None
                if declared_size is not None and declared_size > _ZIP_TOTAL_LIMIT_BYTES:
                    logger.warning(
                        "단일 첨부 크기 상한 초과: no=%s idx=%s size=%s",
                        item_id, file["idx"], declared_size,
                    )
                    return Response("첨부 크기가 제한을 초과했습니다.", status_code=502, media_type="text/plain; charset=utf-8")

                chunks = []
                total_bytes = 0
                async for chunk in resp.aiter_bytes():
                    total_bytes += len(chunk)
                    if total_bytes > _ZIP_TOTAL_LIMIT_BYTES:
                        logger.warning(
                            "단일 첨부 크기 상한 초과: no=%s idx=%s size=%s",
                            item_id, file["idx"], total_bytes,
                        )
                        return Response("첨부 크기가 제한을 초과했습니다.", status_code=502, media_type="text/plain; charset=utf-8")
                    chunks.append(chunk)
                content = b"".join(chunks)
                filename = _filename_from_cd(resp, file["name"] or f"첨부{file['idx']}")
                media_type = resp.headers.get("content-type") or "application/octet-stream"
    except Exception:  # noqa: BLE001 — 외부 첨부 다운로드 실패를 502로 변환.
        logger.warning("단일 첨부 다운로드 실패: no=%s idx=%s", item_id, file["idx"])
        return Response("첨부 다운로드에 실패했습니다.", status_code=502, media_type="text/plain; charset=utf-8")

    headers = {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"}
    return Response(content=content, media_type=media_type, headers=headers)


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


@app.get("/list/{bid_ntce_no}/files/single")
async def list_file_single(bid_ntce_no: str):
    """공고의 첫 번째 첨부를 원본 파일로 반환."""
    with SessionLocal() as session:
        notice = session.get(BidNotice, bid_ntce_no)
        if notice is None:
            return Response("공고를 찾을 수 없습니다.", status_code=404, media_type="text/plain; charset=utf-8")
        files = repository.get_notice_files(session, bid_ntce_no)
    if not files:
        return Response("첨부가 없습니다.", status_code=404, media_type="text/plain; charset=utf-8")
    return await _single_file_response(bid_ntce_no, files[0])


async def _zip_file_content(
    client: httpx.AsyncClient, url: str, total_bytes: int,
) -> tuple[bytes, httpx.Response] | None:
    """남은 ZIP 한도 안의 완전한 파일만 반환한다. 초과하면 이후 파일도 중단한다."""
    async with client.stream("GET", url) as resp:
        resp.raise_for_status()
        try:
            declared_size = int(resp.headers.get("content-length", ""))
        except (TypeError, ValueError):
            declared_size = None
        if declared_size is not None and total_bytes + declared_size > _ZIP_TOTAL_LIMIT_BYTES:
            return None
        chunks = []
        file_bytes = 0
        async for chunk in resp.aiter_bytes():
            file_bytes += len(chunk)
            if total_bytes + file_bytes > _ZIP_TOTAL_LIMIT_BYTES:
                return None
            chunks.append(chunk)
        return b"".join(chunks), resp


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
            return Response("공고를 찾을 수 없습니다.", status_code=404, media_type="text/plain; charset=utf-8")
        files = repository.get_notice_files(session, bid_ntce_no)
        zip_label = notice.bid_ntce_nm or bid_ntce_no
    if not files:
        return Response("첨부가 없습니다.", status_code=404, media_type="text/plain; charset=utf-8")

    buf = io.BytesIO()
    ok = 0
    total_bytes = 0
    limit_hit = False
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
                    downloaded = await _zip_file_content(client, f["url"], total_bytes)
                    if downloaded is None:
                        logger.warning(
                            "zip 누적 총량 상한 초과 — 이후 첨부 skip: no=%s idx=%s total_bytes=%s",
                            bid_ntce_no, f["idx"], total_bytes,
                        )
                        limit_hit = True
                        break
                    content, resp = downloaded
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
        if limit_hit:
            return Response("첨부 크기가 제한을 초과했습니다.", status_code=502, media_type="text/plain; charset=utf-8")
        return Response("첨부 다운로드에 모두 실패했습니다.", status_code=502, media_type="text/plain; charset=utf-8")

    filename = quote(f"{zip_label}.zip")
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


@app.get("/pre-spec/{bf_spec_rgst_no}/files/single")
async def pre_spec_file_single(bf_spec_rgst_no: str):
    """사전규격의 첫 번째 첨부를 원본 파일로 반환."""
    with SessionLocal() as session:
        spec = session.get(PreSpec, bf_spec_rgst_no)
        if spec is None:
            return Response("사전규격을 찾을 수 없습니다.", status_code=404, media_type="text/plain; charset=utf-8")
        files = repository.get_pre_spec_files(session, bf_spec_rgst_no)
    if not files:
        return Response("첨부가 없습니다.", status_code=404, media_type="text/plain; charset=utf-8")
    return await _single_file_response(bf_spec_rgst_no, files[0])


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
            return Response("사전규격을 찾을 수 없습니다.", status_code=404, media_type="text/plain; charset=utf-8")
        files = repository.get_pre_spec_files(session, bf_spec_rgst_no)
        zip_label = spec.prdct_clsfc_no_nm or bf_spec_rgst_no
    if not files:
        return Response("첨부가 없습니다.", status_code=404, media_type="text/plain; charset=utf-8")

    buf = io.BytesIO()
    ok = 0
    total_bytes = 0
    limit_hit = False
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
                    downloaded = await _zip_file_content(client, f["url"], total_bytes)
                    if downloaded is None:
                        logger.warning(
                            "zip 누적 총량 상한 초과 — 이후 첨부 skip: no=%s idx=%s total_bytes=%s",
                            bf_spec_rgst_no, f["idx"], total_bytes,
                        )
                        limit_hit = True
                        break
                    content, resp = downloaded
                except Exception:  # noqa: BLE001 — 실패 파일은 건너뛰고 부분 성공.
                    logger.warning(
                        "첨부 다운로드 실패 skip: no=%s idx=%s", bf_spec_rgst_no, f["idx"]
                    )
                    continue
                # 실제 파일명은 Content-Disposition 헤더에서 추출 (URL에는 확장자 없음)
                base = _filename_from_cd(resp, f["name"] or f"첨부{f['idx']}")
                arc = f"{f['idx']}_{base}"
                if arc in used_names:
                    arc = f"{f['idx']}_{ok}_{base}"
                used_names.add(arc)
                zf.writestr(arc, content)
                total_bytes += len(content)
                ok += 1

    if ok == 0:
        if limit_hit:
            return Response("첨부 크기가 제한을 초과했습니다.", status_code=502, media_type="text/plain; charset=utf-8")
        return Response("첨부 다운로드에 모두 실패했습니다.", status_code=502, media_type="text/plain; charset=utf-8")

    filename = quote(f"{zip_label}.zip")
    headers = {"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"}
    return Response(content=buf.getvalue(), media_type="application/zip", headers=headers)


# --- 분석 API (Phase 8.1) -------------------------------------------

_ANALYSIS_MAX_BYTES = 50 * 1024 * 1024  # 업로드 파일당 최대 50MB
_ANALYSIS_UPLOAD_TOTAL_MAX_BYTES = 100 * 1024 * 1024  # 업로드 합산 최대 100MB
_ANALYSIS_UPLOAD_MAX_FILES = 10  # 업로드 파일 개수 최대

# 파일 형식 우선순위 (낮을수록 우선).
_ANALYSIS_PRIORITY: dict[str, int] = {
    ".pdf": 0,
    ".docx": 1,
    ".doc": 2,
    ".hwp": 3,
    ".hwpx": 4,
}


def _normalize_source_type(source_type: str) -> str:
    """URL/form source_type 값을 DB 저장용 표준값으로 정규화한다."""
    normalized = (source_type or "").strip().replace("-", "_")
    if normalized not in {"bid", "pre_spec"}:
        raise HTTPException(status_code=400, detail="source_type은 bid 또는 pre_spec이어야 합니다.")
    return normalized


def _source_exists(session, source_type: str, source_id: str) -> bool:  # noqa: ANN001
    """분석 대상 엔티티 존재 여부를 확인한다."""
    if source_type == "bid":
        return session.get(BidNotice, source_id) is not None
    return session.get(PreSpec, source_id) is not None


def _analysis_file_priority(file_info: dict[str, Any]) -> tuple[int, int, str]:
    """자동 분석 후보 정렬 키."""
    name = str(file_info.get("name") or "")
    return (
        _ANALYSIS_PRIORITY.get(Path(name).suffix.lower(), 99),
        int(file_info.get("idx") or 0),
        name,
    )


def _find_auto_analysis_urls(session, source_type: str, source_id: str) -> list[dict[str, str]]:  # noqa: ANN001
    """기존 첨부 목록 로직으로 자동 분석 대상 URL 목록을 찾는다."""
    if source_type == "bid":
        files = repository.get_notice_files(session, source_id)
        rfp_candidates: list[dict[str, Any]] = []
        task_candidates: list[dict[str, Any]] = []
        for file_info in files:
            name = str(file_info.get("name") or "")
            if Path(name).suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            if "제안요청서" in name:
                rfp_candidates.append(file_info)
            elif "과업지시서" in name:
                task_candidates.append(file_info)

        selected: list[dict[str, str]] = []
        seen_urls: set[str] = set()
        for doc_kind, candidates in (
            ("제안요청서", rfp_candidates),
            ("과업지시서", task_candidates),
        ):
            candidates.sort(key=_analysis_file_priority)
            for file_info in candidates:
                url = str(file_info.get("url") or "")
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                selected.append({
                    "url": url,
                    "name": str(file_info.get("name") or doc_kind),
                    "doc_kind": doc_kind,
                })
                break
        return selected

    files = repository.get_pre_spec_files(session, source_id)
    # 사전규격 파일 URL은 확장자 없는 downloadFile.do 형태가 많아 첫 첨부를 그대로 분석한다.
    if not files:
        return []
    first = files[0]
    return [{
        "url": str(first.get("url") or ""),
        "name": str(first.get("name") or "첨부1"),
        "doc_kind": "기타",
    }]


def _need_upload_message(source_type: str) -> str:
    if source_type == "bid":
        return "제안요청서/과업지시서 파일을 찾을 수 없습니다. 파일을 직접 업로드해주세요."
    return "첨부 파일이 없습니다. 파일을 직접 업로드해주세요."


async def _run_analysis_bg(
    source_type: str,
    source_id: str,
    *,
    items: list[dict[str, Any]] | None = None,
    url: str | None = None,
    file_bytes: bytes | None = None,
    filename: str | None = None,
) -> None:
    """BackgroundTasks 실행 함수. 요청 세션 대신 자체 SessionLocal을 사용한다."""
    # 두 호출부에서 이미 정규화한 source_type을 넘긴다.
    try:
        if items is None:
            if url:
                items = [{"url": url}]
            elif file_bytes is not None:
                items = [{"bytes": file_bytes, "filename": filename or "upload", "label": filename or "upload"}]
            else:
                items = []

        url_items = [item for item in items if item.get("url")]
        byte_items = [item for item in items if item.get("bytes") is not None]
        if url_items:
            result: ServiceAnalysisResult = await analyze_from_urls(url_items)
        elif byte_items:
            result = await analyze_documents(byte_items)
        else:
            raise ValueError("분석할 URL 또는 파일이 없습니다.")

        with SessionLocal() as session:
            if result.status == "ok" and result.analysis is not None:
                result_json = json.dumps(
                    result.analysis.model_dump(),
                    ensure_ascii=False,
                )
                repository.set_analysis_done(session, source_type, source_id, result_json)
            else:
                repository.set_analysis_error(
                    session,
                    source_type,
                    source_id,
                    result.message or "분석에 실패했습니다.",
                )
    except Exception as exc:  # noqa: BLE001 — analyzing 고착 방지.
        logger.exception("분석 백그라운드 작업 실패: %s/%s", source_type, source_id)
        with SessionLocal() as session:
            repository.set_analysis_error(session, source_type, source_id, f"분석 중 오류: {exc}")


@app.post("/api/analysis/upload")
async def analysis_upload(
    background_tasks: BackgroundTasks,
    response: Response,
    files: list[UploadFile] | None = File(None),
    legacy_file: UploadFile | None = File(None, alias="file"),
    source_type: str | None = Form(None),
    source_id: str | None = Form(None),
    type_alias: str | None = Form(None, alias="type"),
    id_alias: str | None = Form(None, alias="id"),
):
    """수동 파일 업로드 → 분석 시작. 실제 분석은 BackgroundTasks가 수행한다."""
    source_type_value = source_type or type_alias
    source_id_value = source_id or id_alias
    if not source_type_value or not source_id_value:
        response.status_code = 400
        return {"status": "error", "message": "source_type과 source_id가 필요합니다."}

    normalized_type = _normalize_source_type(source_type_value)
    source_id_value = str(source_id_value)

    upload_files = list(files or [])
    if legacy_file is not None:
        upload_files.append(legacy_file)
    if not upload_files:
        response.status_code = 400
        return {"status": "error", "message": "업로드할 파일이 필요합니다."}
    if len(upload_files) > _ANALYSIS_UPLOAD_MAX_FILES:
        response.status_code = 413
        return {
            "status": "error",
            "message": f"업로드 파일은 최대 {_ANALYSIS_UPLOAD_MAX_FILES}개까지 가능합니다.",
        }

    with SessionLocal() as session:
        if not _source_exists(session, normalized_type, source_id_value):
            raise HTTPException(status_code=404, detail="분석 대상을 찾을 수 없습니다.")
        existing = repository.get_analysis(session, normalized_type, source_id_value)
        if existing is not None and existing.status == "analyzing":
            return {"status": "analyzing"}

    items: list[dict[str, Any]] = []
    total_bytes = 0
    for upload_file in upload_files:
        declared_size = getattr(upload_file, "size", None)
        if declared_size is not None and declared_size > _ANALYSIS_MAX_BYTES:
            response.status_code = 413
            return {"status": "error", "message": "파일 크기가 50MB를 초과합니다."}
        file_bytes = await upload_file.read()
        filename = upload_file.filename or "upload"
        if len(file_bytes) > _ANALYSIS_MAX_BYTES:
            response.status_code = 413
            return {"status": "error", "message": "파일 크기가 50MB를 초과합니다."}
        total_bytes += len(file_bytes)
        if total_bytes > _ANALYSIS_UPLOAD_TOTAL_MAX_BYTES:
            response.status_code = 413
            return {"status": "error", "message": "파일 합산 크기가 100MB를 초과합니다."}
        items.append({"filename": filename, "bytes": file_bytes, "label": filename})

    with SessionLocal() as session:
        repository.start_analysis(session, normalized_type, source_id_value, "upload")
    background_tasks.add_task(
        _run_analysis_bg,
        normalized_type,
        source_id_value,
        items=items,
    )
    return {"status": "analyzing"}


@app.post("/api/analysis/{source_type}/{source_id}")
async def analysis_trigger(
    source_type: str,
    source_id: str,
    background_tasks: BackgroundTasks,
):
    """자동 후보 탐색 후 분석을 비동기로 시작한다."""
    normalized_type = _normalize_source_type(source_type)
    with SessionLocal() as session:
        if not _source_exists(session, normalized_type, source_id):
            raise HTTPException(status_code=404, detail="분석 대상을 찾을 수 없습니다.")
        existing = repository.get_analysis(session, normalized_type, source_id)
        if existing is not None and existing.status == "analyzing":
            return {"status": "analyzing"}

        target_urls = _find_auto_analysis_urls(session, normalized_type, source_id)
        if not target_urls:
            return {
                "status": "need_upload",
                "message": _need_upload_message(normalized_type),
            }
        repository.start_analysis(session, normalized_type, source_id, "auto")

    background_tasks.add_task(
        _run_analysis_bg,
        normalized_type,
        source_id,
        items=target_urls,
    )
    return {"status": "analyzing"}


@app.get("/api/analysis/{source_type}/{source_id}/status")
def analysis_status(source_type: str, source_id: str):
    """분석 상태 폴링용 JSON."""
    normalized_type = _normalize_source_type(source_type)
    with SessionLocal() as session:
        row = repository.get_analysis(session, normalized_type, source_id)
        if row is None:
            return {"status": "none"}
        updated_at = row.updated_at.isoformat() if row.updated_at else None
        return {
            "status": row.status,
            "source_kind": row.source_kind,
            "updated_at": updated_at,
        }


def _analysis_page_shell(title: str, body: str, head_title: str | None = None) -> str:
    page_title = head_title or title
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_e(page_title)}</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
           margin: 0; background: #f6f4fb; color: #2f2740; }}
    header {{ background: #5a3e8a; color: #fff; padding: 14px 24px; }}
    header h1 {{ margin: 0; font-size: 18px; }}
    main {{ max-width: 1120px; margin: 0 auto; padding: 20px; }}
    .card {{ background: #fff; border-radius: 8px; padding: 18px 20px; margin-bottom: 16px;
             border: 1px solid #e0d8f5; border-top: 3px solid #c9b8ef;
             box-shadow: 0 1px 3px rgba(61,40,112,.08); }}
    h2 {{ margin: 0 0 12px; font-size: 16px; color: #5a3e8a; }}
    h3 {{ margin: 12px 0 8px; font-size: 14px; color: #3d2870; }}
    p {{ font-size: 13px; line-height: 1.6; margin: 6px 0; }}
    ul, ol {{ margin: 0; padding-left: 22px; font-size: 13px; line-height: 1.7; }}
    li {{ margin: 3px 0; }}
    table {{ border-collapse: collapse; width: 100%; font-size: 12px; }}
    th, td {{ border: 1px solid #d8d0ee; padding: 7px 9px; text-align: left; vertical-align: top; }}
    th {{ background: #ede5ff; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 8px 14px; }}
    .label {{ color: #5a3e8a; font-weight: 600; margin-right: 6px; }}
    .muted {{ color: #7d728f; }}
    .theme {{ border: 1px solid #e0d8f5; border-radius: 8px; padding: 10px 12px; margin-bottom: 8px; }}
    .kv-list {{ margin: 0; font-size: 13px; line-height: 1.6; }}
    .kv-list dt {{ color: #3d2870; font-weight: 600; margin-top: 8px; }}
    .kv-list dd {{ margin: 2px 0 8px 0; }}
    .kv-list.compact {{ margin-left: 0; }}
    .pre {{ white-space: pre-wrap; word-break: break-word; font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
            background: #fbf8ff; border: 1px solid #e0d8f5; border-radius: 6px; padding: 10px; font-size: 12px; }}
  </style>
</head>
<body>
  <header><h1>{_e(title)}</h1></header>
  <main>{body}</main>
</body>
</html>"""


def _li_items(items: list[Any]) -> str:
    if not items:
        return '<li class="muted">없음</li>'
    return "".join(f"<li>{_e(item)}</li>" for item in items)


def _render_requirements(title: str, items: list[dict[str, Any]]) -> str:
    if not items:
        content = '<li class="muted">없음</li>'
    else:
        parts = []
        for item in items:
            head = (
                f"[{_e(item.get('category'))}] "
                f"{_e(item.get('requirement'))} "
                f"({_e(item.get('priority'))})"
            )
            notes = item.get("notes")
            tail = f" — {_e(notes)}" if notes else ""
            parts.append(f"<li>{head}{tail}</li>")
        content = "".join(parts)
    return f'<section class="card"><h2>{_e(title)}</h2><ol>{content}</ol></section>'


def _render_budget(budget: dict[str, Any] | None) -> str:
    if not budget:
        return '<section class="card"><h2>예산 정보</h2><p class="muted">없음</p></section>'
    breakdown = budget.get("budget_breakdown")
    breakdown_html = ""
    if isinstance(breakdown, dict) and breakdown:
        breakdown_html = "<h3>세부 예산</h3><ul>" + "".join(
            f"<li><span class=\"label\">{_e(k)}</span>{_e(v)}</li>"
            for k, v in breakdown.items()
        ) + "</ul>"
    return f"""
    <section class="card">
      <h2>예산 정보</h2>
      <div class="grid">
        <p><span class="label">총 예산</span>{_e(budget.get('total_budget'))}</p>
        <p><span class="label">지급 조건</span>{_e(budget.get('payment_terms'))}</p>
        <p><span class="label">비고</span>{_e(budget.get('notes'))}</p>
      </div>
      {breakdown_html}
    </section>"""


def _render_timeline(timeline: dict[str, Any] | None) -> str:
    if not timeline:
        return '<section class="card"><h2>일정 정보</h2><p class="muted">없음</p></section>'
    phases = timeline.get("phases") or []
    phase_items = []
    if isinstance(phases, list):
        for phase in phases:
            if isinstance(phase, dict):
                phase_items.append(
                    f"<li>{_e(phase.get('name'))} — {_e(phase.get('duration'))}</li>"
                )
    milestones = timeline.get("milestones") or []
    milestone_items = []
    if isinstance(milestones, list):
        for milestone in milestones:
            if isinstance(milestone, dict):
                milestone_items.append(
                    f"<li>{_e(milestone.get('name'))} — {_e(milestone.get('date'))}</li>"
                )
    return f"""
    <section class="card">
      <h2>일정 정보</h2>
      <div class="grid">
        <p><span class="label">전체 기간</span>{_e(timeline.get('total_duration'))}</p>
        <p><span class="label">시작일</span>{_e(timeline.get('start_date'))}</p>
        <p><span class="label">종료일</span>{_e(timeline.get('end_date'))}</p>
      </div>
      <h3>단계</h3><ul>{''.join(phase_items) if phase_items else '<li class="muted">없음</li>'}</ul>
      <h3>마일스톤</h3><ul>{''.join(milestone_items) if milestone_items else '<li class="muted">없음</li>'}</ul>
    </section>"""


def _render_eval_table(items: list[dict[str, Any]]) -> str:
    if not items:
        return '<section class="card"><h2>평가 기준</h2><p class="muted">없음</p></section>'
    rows = "".join(
        "<tr>"
        f"<td>{_e(item.get('category'))}</td>"
        f"<td>{_e(item.get('item'))}</td>"
        f"<td>{_e(item.get('weight'))}</td>"
        f"<td>{_e(item.get('description'))}</td>"
        "</tr>"
        for item in items
    )
    return f"""
    <section class="card">
      <h2>평가 기준</h2>
      <table>
        <thead><tr><th>구분</th><th>항목</th><th>배점</th><th>설명</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </section>"""


def _render_readable_value(value: Any) -> str:
    if isinstance(value, dict):
        if not value:
            return '<span class="muted">없음</span>'
        return '<dl class="kv-list compact">' + "".join(
            f"<dt>{_e(k)}</dt><dd>{_render_readable_value(v)}</dd>"
            for k, v in value.items()
        ) + "</dl>"
    if isinstance(value, list):
        if not value:
            return '<span class="muted">없음</span>'
        return "<ul>" + "".join(
            f"<li>{_render_readable_value(item)}</li>"
            for item in value
        ) + "</ul>"
    return _e(value)


def _render_eval_strategy(value: Any) -> str:
    if not value:
        return '<section class="card"><h2>평가 전략</h2><p class="muted">없음</p></section>'
    if not isinstance(value, dict):
        return (
            '<section class="card"><h2>평가 전략</h2>'
            f"{_render_readable_value(value)}</section>"
        )

    high_items = value.get("high_weight_items")
    mapping = value.get("emphasis_mapping")
    parts: list[str] = []
    if isinstance(high_items, list) and high_items:
        rows = []
        for item in high_items:
            if isinstance(item, dict):
                if any(k in item for k in ("item", "weight", "proposal_emphasis")):
                    rows.append(
                        "<tr>"
                        f"<td>{_e(item.get('item'))}</td>"
                        f"<td>{_e(item.get('weight'))}</td>"
                        f"<td>{_e(item.get('proposal_emphasis'))}</td>"
                        "</tr>"
                    )
                else:
                    rows.append(
                        "<tr>"
                        f'<td colspan="3">{_render_readable_value(item)}</td>'
                        "</tr>"
                    )
            else:
                rows.append(
                    "<tr>"
                    f"<td>{_e(item)}</td>"
                    "<td></td><td></td>"
                    "</tr>"
                )
        parts.append(
            "<h3>고배점 항목</h3><table>"
            "<thead><tr><th>항목</th><th>배점</th><th>제안 강조점</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>"
        )
    elif high_items:
        parts.append(
            "<h3>고배점 항목</h3>"
            f"{_render_readable_value(high_items)}"
        )

    if isinstance(mapping, dict) and mapping:
        items = "".join(
            f"<dt>{_e(phase)}</dt><dd>{_render_readable_value(text)}</dd>"
            for phase, text in mapping.items()
        )
        parts.append(f'<h3>전략 매핑</h3><dl class="kv-list">{items}</dl>')
    elif mapping:
        parts.append(
            "<h3>전략 매핑</h3>"
            f"{_render_readable_value(mapping)}"
        )

    extra = {
        k: v
        for k, v in value.items()
        if k not in {"high_weight_items", "emphasis_mapping"}
    }
    if extra:
        parts.append(f"<h3>기타</h3>{_render_readable_value(extra)}")
    if not parts:
        parts.append(_render_readable_value(value))
    return f'<section class="card"><h2>평가 전략</h2>{"".join(parts)}</section>'


def _render_deliverables(items: list[dict[str, Any]]) -> str:
    if not items:
        content = '<li class="muted">없음</li>'
    else:
        content = "".join(
            f"<li>{_e(item.get('name'))} — {_e(item.get('description'))} ({_e(item.get('phase'))})</li>"
            for item in items
        )
    return f'<section class="card"><h2>산출물</h2><ul>{content}</ul></section>'


def _render_win_themes(items: list[dict[str, Any]]) -> str:
    if not items:
        body = '<p class="muted">없음</p>'
    else:
        body = "".join(
            "<div class=\"theme\">"
            f"<h3>{_e(item.get('name'))}</h3>"
            f"<p><span class=\"label\">근거</span>{_e(item.get('rationale'))}</p>"
            f"<p><span class=\"label\">RFP 정합성</span>{_e(item.get('rfp_alignment'))}</p>"
            "</div>"
            for item in items
        )
    return f'<section class="card"><h2>Win Theme 후보</h2>{body}</section>'


def _render_json_block(title: str, value: Any) -> str:
    if value is None:
        return f'<section class="card"><h2>{_e(title)}</h2><p class="muted">없음</p></section>'
    text = json.dumps(value, ensure_ascii=False, indent=2)
    return f'<section class="card"><h2>{_e(title)}</h2><div class="pre">{_e(text)}</div></section>'


def _render_analysis_html(data: dict[str, Any]) -> str:
    string_list_fields = [
        ("핵심 성공 요인", "key_success_factors"),
        ("잠재 리스크", "potential_risks"),
        ("차별화 포인트", "differentiation_points"),
        ("Pain Points", "pain_points"),
        ("숨겨진 니즈", "hidden_needs"),
    ]
    list_sections = "".join(
        f'<section class="card"><h2>{_e(title)}</h2><ul>{_li_items(data.get(key) or [])}</ul></section>'
        for title, key in string_list_fields
    )
    return f"""
    <section class="card">
      <h2>프로젝트 기본 정보</h2>
      <div class="grid">
        <p><span class="label">프로젝트명</span>{_e(data.get('project_name'))}</p>
        <p><span class="label">발주처</span>{_e(data.get('client_name'))}</p>
        <p><span class="label">유형</span>{_e(data.get('project_type'))}</p>
      </div>
      <h3>사업 개요</h3>
      <p>{_e(data.get('project_overview'))}</p>
    </section>
    {_render_budget(data.get('budget'))}
    {_render_timeline(data.get('timeline'))}
    {_render_requirements('핵심 요구사항', data.get('key_requirements') or [])}
    {_render_requirements('기술 요구사항', data.get('technical_requirements') or [])}
    {_render_requirements('기능 요구사항', data.get('functional_requirements') or [])}
    {_render_eval_table(data.get('evaluation_criteria') or [])}
    {_render_deliverables(data.get('deliverables') or [])}
    {list_sections}
    <section class="card"><h2>수주 전략</h2><p>{_e(data.get('winning_strategy'))}</p></section>
    <section class="card"><h2>예상 경쟁 환경</h2><p>{_e(data.get('competitive_landscape'))}</p></section>
    {_render_eval_strategy(data.get('evaluation_strategy'))}
    {_render_win_themes(data.get('win_theme_candidates') or [])}
    {_render_json_block('원본 섹션', data.get('raw_sections'))}
    """


@app.get("/analysis/{source_type}/{source_id}", response_class=HTMLResponse)
def analysis_page(source_type: str, source_id: str) -> HTMLResponse:
    """새 창에서 보는 서버 렌더 분석 결과 페이지."""
    normalized_type = _normalize_source_type(source_type)
    with SessionLocal() as session:
        row = repository.get_analysis(session, normalized_type, source_id)

    head_title = "RFP 분석 결과"
    if row is None:
        body = '<section class="card"><h2>분석 결과 없음</h2><p>아직 분석을 시작하지 않았습니다.</p></section>'
    elif row.status == "analyzing":
        body = '<section class="card"><h2>분석 중</h2><p>분석이 아직 완료되지 않았습니다.</p></section>'
    elif row.status == "error":
        body = (
            '<section class="card"><h2>분석 오류</h2>'
            f"<p>{_e(row.error_message or '분석 중 오류가 발생했습니다.')}</p></section>"
        )
    else:
        try:
            parsed = json.loads(row.result_json or "{}")
        except json.JSONDecodeError:
            parsed = None
        if not isinstance(parsed, dict):
            body = '<section class="card"><h2>분석 결과 오류</h2><p>저장된 결과를 읽을 수 없습니다.</p></section>'
        else:
            project_name = parsed.get("project_name")
            if project_name:
                head_title = f"RFP 분석 : {project_name}"
            body = _render_analysis_html(parsed)

    return HTMLResponse(_analysis_page_shell("RFP 분석 결과", body, head_title=head_title))


# --- /config ---------------------------------------------------------
# (필드명, 라벨, 정수 최소, 정수 최대) — 정수 설정 항목.
_CONFIG_INT_FIELDS: list[tuple[str, str, int, int]] = [
    ("interval_minutes", "수집 주기(분)", 1, 100000),
    ("window_overlap_minutes", "윈도우 겹침(분)", 0, 100000),
    ("backfill_days", "백필 기간(일)", 1, 366),
    ("num_of_rows", "사전규격 수집·면허 누락 보정 조회 페이지 크기", 1, 999),
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


def _render_keyword_card(keywords: list[dict[str, Any]]) -> str:
    """/config — 공고명 제외 키워드 목록 관리 카드(추가·삭제·기본 적용 저장)."""
    if keywords:
        rows = "".join(
            f"<tr>"
            f'<td><label class="chk" style="padding-bottom:0;">'
            f'<input type="checkbox" name="default_ids" value="{int(k["id"])}"'
            f'{" checked" if k.get("is_default") else ""}> 기본 적용</label></td>'
            f'<td>{_e(k["keyword"])}</td>'
            f'<td><button type="submit" class="kw-del" name="delete_id" '
            f'value="{int(k["id"])}">삭제</button></td>'
            f"</tr>"
            for k in keywords
        )
        table_html = f"""
      <div class="table-wrap">
        <table class="kwtable">
          <thead><tr><th>기본 적용</th><th>키워드</th><th>관리</th></tr></thead>
          <tbody>{rows}</tbody>
        </table>
      </div>
      <div style="margin-top:12px;">
        <button type="submit" class="submit" name="action" value="save">기본 적용 저장</button>
      </div>"""
    else:
        table_html = '<p class="muted">등록된 키워드가 없습니다. 위에서 추가하세요.</p>'

    return f"""
    <div class="card" id="keywords">
      <h2>공고명 제외 키워드 목록</h2>
      <form method="post" action="/config/keywords">
        <div class="row" style="margin-bottom:14px;">
          <label class="field" style="min-width:240px;">
            <span class="flabel">키워드 추가</span>
            <input type="text" name="new_keyword" maxlength="{repository.EXCLUDE_KEYWORD_MAX_LEN}"
                   placeholder="예: 감리">
          </label>
          <button type="submit" class="submit" name="action" value="add">추가</button>
        </div>
        {table_html}
      </form>
      <div class="note">
        입찰공고목록·사전규격목록 검색필터의 제외 키워드 기본 목록입니다.
        <b>기본 적용</b>에 체크한 단어는 목록 화면 첫 진입에서 자동으로 칩에 채워집니다(화면에서 개별 해제 가능).
      </div>
    </div>"""


def _render_rule_card(
    rules: list[dict], clsfc_names: list[str], msg: str | None = None, err: str | None = None,
) -> str:
    """/config — 목록 표시 규칙 세 종류를 한 폼에서 편집한다."""
    sections = []
    message = (f'<div class="msg err">{_e(err)}</div>' if err else
               f'<div class="msg ok">{_e(msg)}</div>' if msg else "")
    references = {rule["id"]: f'{rule["id"]}:{repository.list_rule_fingerprint(rule)}' for rule in rules}
    for rule_type, title, help_text in repository.RULE_FIELDS:
        rows = "".join(
            f'<tr><td><label class="chk" style="padding-bottom:0;white-space:nowrap;">'
            f'<input type="checkbox" name="enabled_ids" value="{references[rule["id"]]}"'
            f'{" checked" if rule["enabled"] else ""}> 사용</label></td>'
            f'<td>{_e(rule["value"])}</td>'
            f'<td><button type="submit" class="kw-del" name="delete_id" '
            f'value="{references[rule["id"]]}">삭제</button></td></tr>'
            for rule in rules if rule["rule_type"] == rule_type
        )
        if not rows:
            rows = '<tr><td colspan="3" class="muted">등록된 규칙이 없습니다</td></tr>'
        datalist = ' list="clsfc-names"' if rule_type == "clsfc" else ""
        sections.append(f"""
        <div class="row" style="margin-top:18px;gap:12px;">
          <h3 style="white-space:nowrap;">{title}</h3>
          <input type="text" name="new_{rule_type}" maxlength="100"{datalist}
                 data-add-btn="add-{rule_type}"
                 aria-label="{title} 추가" placeholder="{title} 추가">
          <button type="submit" class="submit" id="add-{rule_type}"
                  name="action" value="add_{rule_type}">추가</button>
        </div>
        <p class="muted">{help_text}</p>
        <div class="table-wrap">
          <table class="kwtable">
            <thead><tr><th>사용</th><th>값</th><th>삭제</th></tr></thead>
            <tbody>{rows}</tbody>
          </table>
        </div>""")
    options = "".join(f'<option value="{_e(name)}"></option>' for name in clsfc_names)
    rule_ids = "".join(
        f'<input type="hidden" name="rule_ids" value="{references[rule["id"]]}">' for rule in rules
    ) or '<input type="hidden" name="rule_ids" value="">'
    return f"""
    <div class="card" id="rules">
      <h2>목록 표시 규칙</h2>
      <a href="/config/rules/test" target="_blank" rel="noopener">규칙 테스트 (새 창)</a>
      {message}
      <p class="muted">
        ① 매칭 업종코드와 면허제한 일치 → ② 업종 제한 없는 공고 중 공공조달분류 일치 →<br>
        ③ 업종 제한 없는 공고 중 공고명 키워드 일치(판별 제외어가 있으면 ③ 제외).<br>
        저장 시 저장된 전체 공고를 다시 분류합니다.
      </p>
      <form method="post" action="/config/rules">
        {rule_ids}
        <datalist id="clsfc-names">{options}</datalist>
        {''.join(sections)}
        <div style="margin-top:12px;">
          <button type="submit" class="submit" name="action" value="save">표시 규칙 저장</button>
        </div>
      </form>
      <script>
      document.querySelectorAll('#rules input[data-add-btn]').forEach(function(input) {{
        input.addEventListener('keydown', function(event) {{
          if (event.key !== 'Enter' || event.isComposing) return;
          event.preventDefault();
          const button = document.getElementById(input.dataset.addBtn);
          if (input.form.requestSubmit) input.form.requestSubmit(button);
          else button.click();
        }});
      }});
      </script>
    </div>"""


def _render_config_page(
    cfg: dict,
    runs: list[dict],
    sched: dict,
    msg: str | None,
    err: str | None,
    keywords: list[dict[str, Any]] | None = None,
    rules: list[dict[str, Any]] | None = None,
    clsfc_names: list[str] | None = None,
    rules_msg: str | None = None,
    rules_err: str | None = None,
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
          {'<span class="help">입찰공고·면허 일괄 조회는 999행 고정. API 상한 999 — 1000 이상이면 오류 없이 10행만 응답합니다.</span>' if name == 'num_of_rows' else ''}
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
              <span class="flabel">매칭 업종코드 CSV <code>indstryty_cds</code></span>
              <input type="text" name="indstryty_cds" value="{_e(cfg.get('indstryty_cds'))}" placeholder="예: 1426,1468,1469,1470">
              <span class="help">목록 표시 판정 기준(면허제한 코드와 비교) — 수집 조건이 아닙니다.</span>
            </label>
          </div>
          <div class="note" style="margin-top:8px;">입찰공고 검색 기본값(배정예산)</div>
          <div class="grid" style="margin-top:4px;">
            <label class="field">
              <span class="flabel">배정예산 기본 하한 <code>presmpt_prce_bgn</code></span>
              <input type="number" name="presmpt_prce_bgn" value="{_e(cfg.get('presmpt_prce_bgn'))}" placeholder="비우면 미적용" min="0">
            </label>
            <label class="field">
              <span class="flabel">배정예산 기본 상한 <code>presmpt_prce_end</code></span>
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
          <div class="note">목록(/list)·사전규격(/pre-spec) 배정예산 검색의 기본값으로 쓰입니다(비우면 기본 미적용).</div>
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

    {_render_rule_card(rules or [], clsfc_names or [], rules_msg, rules_err)}

    {_render_keyword_card(keywords or [])}

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


def _load_keywords() -> list[dict[str, Any]]:
    """/config 제외 키워드 카드용 목록(세션 안에서 스칼라 dict 로 추출)."""
    with SessionLocal() as session:
        return repository.list_exclude_keywords(session)


def _load_rules_view() -> tuple[list[dict], list[str]]:
    with SessionLocal() as session:
        return repository.list_list_rules(session), repository.list_clsfc_names(session)


def _classification_params(stats: dict) -> dict[str, int]:
    """재분류 통계를 저장 메시지용 쿼리 파라미터로 변환한다."""
    basis = stats["by_basis"]
    return {"vis": stats["visible"], "u": basis["업종"],
            "c": basis["분류"], "k": basis["키워드"]}


def _sched_view() -> dict:
    return {
        "running": scheduler.is_running(),
        "next_run": scheduler.get_next_run_time(),
        # Phase 5.5: 사전규격 잡 다음 실행 시각(5.4에서 get_next_run_time 에 job_id 추가).
        "pre_spec_next_run": scheduler.get_next_run_time("collect_pre_spec"),
    }


@app.get("/config", response_class=HTMLResponse)
def config_page(
    saved: str | None = None, err: str | None = None, rules: str | None = None,
    vis: str | None = None, u: str = "0", c: str = "0", k: str = "0",
    rules_err: str | None = None,
) -> HTMLResponse:
    cfg, runs = _load_config_view()
    msg = "설정을 저장했습니다." if saved else None
    if rules:
        msg = "표시 규칙을 저장했습니다"
    try:
        counts = [int(value) for value in (vis, u, c, k)]
    except (TypeError, ValueError):
        counts = None
    if msg and counts is not None:
        visible, industry, clsfc, keyword = counts
        msg = f"{msg.rstrip('.')} — 표시 {visible}건(업종 {industry} · 분류 {clsfc} · 키워드 {keyword})"
    list_rules, clsfc_names = _load_rules_view()
    return HTMLResponse(
        _render_config_page(cfg, runs, _sched_view(), None if rules or rules_err else msg,
                            None if rules or rules_err else err, _load_keywords(),
                            list_rules, clsfc_names, msg if rules else None, rules_err)
    )


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
    elif not all(re.fullmatch(r"[0-9]+", c) for c in codes):
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

    # 입찰공고 배정예산 기본 하한/상한 + 사전규격 배정예산액 기본 하한/상한 —
    # 숫자(콤마 허용) 또는 빈값. 빈값은 None(미적용)으로 저장.
    for name, lbl in (
        ("presmpt_prce_bgn", "입찰공고 배정예산 기본 하한"),
        ("presmpt_prce_end", "입찰공고 배정예산 기본 상한"),
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
        list_rules, clsfc_names = _load_rules_view()
        html_page = _render_config_page(
            cfg, runs, _sched_view(), None, " / ".join(errors), _load_keywords(), list_rules, clsfc_names
        )
        return HTMLResponse(html_page, status_code=400)

    def save() -> dict:
        params = {"saved": "1"}
        # 잠금 획득 후 세션을 열고 변경·재분류·커밋을 한 트랜잭션으로 묶는다.
        with classifier.CLASSIFY_WRITE_LOCK, SessionLocal() as session, session.no_autoflush:
            try:
                codes_changed = repository.get_config(session).indstryty_cds != updates["indstryty_cds"]
                rules = classifier.load_rule_set(session) if codes_changed else None
                repository.update_config(session, commit=False, **updates)
                if codes_changed:
                    params.update(_classification_params(classifier.reclassify_notices(
                        session, rules=rules, match_codes=frozenset(codes),
                    )))
                else:
                    session.commit()
            except Exception:
                session.rollback()
                raise
        return params

    try:
        params = await run_in_threadpool(save)
    except Exception:
        logger.exception("설정 저장·재분류 실패 — 변경을 롤백했습니다.")
        return RedirectResponse(
            url="/config?err=" + quote("설정 저장에 실패했습니다. 변경을 취소했습니다. 다시 시도해 주세요."),
            status_code=303,
        )

    # interval 변경을 실행 중 스케줄러에 즉시 반영.
    if scheduler.is_running():
        scheduler.reschedule(updates["interval_minutes"])

    return RedirectResponse(url=f"/config?{urlencode(params)}", status_code=303)


@app.post("/config/rules")
async def config_rules_save(request: Request):
    """체크 상태를 먼저 저장하고 추가·삭제 후 전체 공고를 재분류한다."""
    form = await request.form()
    action = str(form.get("action", "save"))
    try:
        if action not in {"save", "add_clsfc", "add_keyword", "add_negative"}:
            raise ValueError("알 수 없는 규칙 작업입니다.")
        delete_ref = _parse_rule_id(form["delete_id"]) if "delete_id" in form else None
        enabled_refs = [_parse_rule_id(value) for value in form.getlist("enabled_ids")]
        if "rule_ids" not in form:
            raise ValueError("규칙 목록이 바뀌었습니다. 새로고침 후 다시 시도하세요.")
        rendered_ids = form.getlist("rule_ids")
        rule_refs = [] if rendered_ids == [""] else [_parse_rule_id(value) for value in rendered_ids]
    except ValueError as exc:
        return _rules_error_redirect(str(exc))
    rule_type = action.removeprefix("add_")
    value = repository.normalize_rule_value(str(form.get(f"new_{rule_type}") or ""))

    def save() -> tuple[dict | None, str | None]:
        err = None
        with classifier.CLASSIFY_WRITE_LOCK, SessionLocal() as session, session.no_autoflush:
            try:
                draft = repository.list_list_rules(session)
                current_refs = {(rule["id"], repository.list_rule_fingerprint(rule)) for rule in draft}
                submitted_refs = [*rule_refs, *enabled_refs]
                if delete_ref is not None:
                    submitted_refs.append(delete_ref)
                if (not set(submitted_refs).issubset(current_refs)
                        or not set(enabled_refs).issubset(rule_refs)):
                    return None, "규칙 목록이 바뀌었습니다. 새로고침 후 다시 시도하세요."
                rule_ids = [rule_id for rule_id, _ in rule_refs]
                enabled_ids = [rule_id for rule_id, _ in enabled_refs]
                delete_id = delete_ref[0] if delete_ref is not None else None
                match_codes = classifier.load_match_codes(session)
                repository.set_list_rules_enabled(session, rule_ids, enabled_ids, commit=False)
                for rule in draft:
                    if rule["id"] in rule_ids:
                        rule["enabled"] = rule["id"] in enabled_ids
                if delete_id is not None:
                    repository.delete_list_rule(session, delete_id, commit=False)
                    draft = [rule for rule in draft if rule["id"] != delete_id]
                elif action != "save":
                    try:
                        if not repository.add_list_rule(session, rule_type, value, commit=False):
                            existing = repository.find_list_rule_duplicate(session, rule_type, value)
                            err = f"이미 등록된 규칙입니다('{existing}')."
                        else:
                            # 새 규칙은 같은 종류의 기존 id 순 뒤에 붙는다.
                            draft.append({"rule_type": rule_type, "value": value, "enabled": True})
                    except ValueError as exc:
                        err = str(exc)
                # 추가 입력 오류여도 체크 편집분과 재분류는 함께 커밋한다.
                rules = classifier.rule_set_from_rows(draft)
                stats = classifier.reclassify_notices(session, rules=rules, match_codes=match_codes)
            except Exception:
                session.rollback()
                raise
        return stats, err

    try:
        stats, err = await run_in_threadpool(save)
    except Exception:
        logger.exception("표시 규칙 저장·재분류 실패 — 변경을 롤백했습니다.")
        return _rules_error_redirect("표시 규칙 저장에 실패했습니다. 변경을 취소했습니다. 다시 시도해 주세요.")
    if err:
        return _rules_error_redirect(err)
    params = {"rules": "1", **_classification_params(stats)}
    return RedirectResponse(url=f"/config?{urlencode(params)}#rules", status_code=303)


def _rule_test_verdict(verdict: tuple, match_codes: frozenset[str]) -> str:
    visible, basis, detail, codes = verdict
    if not visible:
        return '<span class="badge">숨김</span>'
    if basis == "업종":
        detail = ", ".join(code.strip() for code in (codes or "").split(",")
                           if code.strip() in match_codes)
    return f'<span class="badge ok">표시 · {_e(basis or "근거 없음")}</span> {_e(detail)}'


def _rule_test_table(title: str, rows: list[dict], total: int, match_codes: frozenset[str]) -> str:
    body = "".join(
        f'<tr><td>{_e(row["no"])}</td><td>{_e(row["title"])}</td>'
        f'<td>{_e(row["date"].strftime("%Y-%m-%d %H:%M") if row["date"] else "")}</td>'
        f'<td>{_rule_test_verdict(row["current"], match_codes)}</td>'
        f'<td>{_rule_test_verdict(row["draft"], match_codes)}</td></tr>' for row in rows
    ) or '<tr><td colspan="5" class="muted">해당 공고가 없습니다.</td></tr>'
    more = f'<p class="muted">외 {total - len(rows)}건</p>' if total > len(rows) else ""
    return f'''<section class="card"><h2>{_e(title)} ({total}건)</h2><div class="table-wrap">
      <table><thead><tr><th>공고번호</th><th>공고명</th><th>공고일</th>
      <th>현재 근거</th><th>초안 근거</th></tr></thead><tbody>{body}</tbody></table></div>{more}</section>'''


@dataclass
class RuleTestView:
    data: dict[str, str]
    match_codes: frozenset[str]
    result: dict | None = None
    err: str | None = None
    msg: str | None = None
    stale: bool = False
    focus_preview: bool = False


def _rule_test_filter_params(data: Mapping[str, Any]) -> dict[str, str]:
    return {key: value if isinstance(value := data.get(field, ""), str) else ""
            for key, field in (("dt_from", "dt_from"), ("dt_to", "dt_to"), ("q", "search"))}


def _rule_test_reload_redirect(data: Mapping[str, Any], err: str) -> RedirectResponse:
    params = {"err": err, **_rule_test_filter_params(data)}
    return RedirectResponse(url=f"/config/rules/test?{urlencode(params)}", status_code=303)


def _render_rule_test_page(view: RuleTestView) -> str:
    data, match_codes = view.data, view.match_codes
    result, err, msg = view.result, view.err, view.msg
    fields = "".join(
        f'<label class="field"><span class="flabel">{_e(title)} — 한 줄에 값 하나</span>'
        f'<textarea name="{kind}" rows="10">{_e(data.get(kind, ""))}</textarea>'
        f'<span class="help">{_e(help_text)}</span></label>'
        for kind, title, help_text in repository.RULE_FIELDS
    )
    message = (f'<div class="msg err">{_e(err)}</div>' if err else
               f'<div class="msg ok">{_e(msg)}</div>' if msg else "")
    results = ""
    warning = ""
    if view.stale:
        reload_url = "/config/rules/test?" + urlencode(_rule_test_filter_params(data))
        if view.err != rule_test.STALE_MESSAGE:
            warning = '<div class="msg err">다른 곳에서 규칙(또는 매칭 업종코드)이 바뀌었습니다.'
            if result is not None:
                warning += ' 현재 상태로 계산한 미리보기입니다.'
            warning += '</div>'
        warning += f'<a href="{_e(reload_url)}">현재 규칙 다시 불러오기(초안 버림)</a>'
    if result is not None:
        def basis_counts(key):
            counts = result[key + "_basis"]
            return f'업종 {counts["업종"]} · 분류 {counts["분류"]} · 키워드 {counts["키워드"]}'

        results = f'''<section class="card"><h2>미리보기 결과 (대상 기간 기준)</h2>
          <p>대상 공고 {result["total"]}건</p>
          <p>현재 표시 {result["current"]}건 → 초안 표시 {result["draft"]}건</p>
          <p>현재: {basis_counts("current")}<br>초안: {basis_counts("draft")}</p>
          <p>새로 표시 {result["added"]}건 · 새로 숨김 {result["removed"]}건</p></section>'''
        results += _rule_test_table("새로 표시", result["added_rows"], result["added"], match_codes)
        results += _rule_test_table("새로 숨김", result["removed_rows"], result["removed"], match_codes)
        if data.get("search", "").strip():
            results += _rule_test_table("공고명 검색 결과", result["search_rows"],
                                        result["search_total"], match_codes)
    focus_script = ""
    if result is not None or view.focus_preview:
        results = f'<div id="preview">{message}{warning}{results}</div>'
        message = ""
        warning = ""
        focus_script = '<script>window.location.hash = "preview";</script>'
    return f'''<!doctype html><html lang="ko"><head><meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>규칙 테스트</title><style>{BASE_CSS}
      main {{ max-width:1200px; }}
      .draft-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(250px,1fr)); gap:16px; }}
      textarea {{ width:100%; padding:8px; border:1px solid #cbd2dc; border-radius:6px; font:inherit; }}
      .help {{ margin-top:6px; color:#6b7280; font-size:12px; }}
      </style></head><body><header><h1>규칙 테스트</h1><a class="hdr-btn" href="/config#rules">설정</a></header>
      <main><section class="card"><h2>초안 편집</h2>{message}{warning}
      <p>현재 매칭 업종코드: <code>{_e(", ".join(sorted(match_codes)) or "없음")}</code>
      <span class="muted">(읽기 전용 · 변경은 설정에서)</span></p>
      <form method="post" action="/config/rules/test#preview">
      <input type="hidden" name="fingerprint" value="{_e(data.get("fingerprint", ""))}">
      <div class="draft-grid">{fields}</div>
      <div class="row" style="margin-top:18px;">
      <label class="field"><span class="flabel">공고일 시작</span>
        <input type="date" name="dt_from" value="{_e(data.get("dt_from", ""))}"></label>
      <label class="field"><span class="flabel">공고일 끝</span>
        <input type="date" name="dt_to" value="{_e(data.get("dt_to", ""))}"></label>
      <label class="field"><span class="flabel">공고명 검색 (선택)</span>
        <input type="text" name="search" value="{_e(data.get("search", ""))}"></label>
      <button type="submit" class="submit">미리보기</button>
      <button type="submit" class="submit go" formaction="/config/rules/test/apply#preview"
        onclick="return confirm('규칙을 저장하고 저장된 전체 공고를 다시 분류합니다. 초안에 없는 켜진 규칙은 꺼집니다.');">적용</button></div>
      <p class="note">미리보기는 DB에 저장하지 않습니다. 날짜를 비우면 전체 기간을 조회합니다.
      공고명 검색은 선택 기간 안의 판정 확인 표를 보여줍니다.<br>
      적용하면 규칙을 저장하고 기간·검색 조건과 관계없이 저장된 전체 공고를 다시 분류합니다.
      초안에서 뺀 규칙은 삭제하지 않고 끕니다.</p></form></section>{results}</main>{focus_script}</body></html>'''


def _rule_test_view(data: Mapping[str, Any] | None = None, *, preview: bool = False,
                    err: str | None = None, msg: str | None = None) -> RuleTestView:
    with SessionLocal() as session, session.no_autoflush:
        rows = repository.list_list_rules(session)
        codes = classifier.load_match_codes(session)
        fingerprint = rule_test.rules_fingerprint(rows, codes)
        if data is None:
            data = {"fingerprint": fingerprint}
            data.update({kind: "\n".join(row["value"] for row in rows
                                        if row["rule_type"] == kind and row["enabled"])
                         for kind, _, _ in repository.RULE_FIELDS})
        result = None
        stale = preview and data.get("fingerprint") != fingerprint
        if preview:
            try:
                plan = rule_test.rule_plan(rows, rule_test.draft_values(data))
                result = rule_test.preview(session, plan, codes, data)
            except rule_test.FormStructureError:
                # 라우트에서 먼저 걸러지며, 여기서는 일반 실패로 삼키지 않는다.
                raise
            except rule_test.DraftValidationError as exc:
                err = str(exc)
            except Exception:
                logger.exception("규칙 테스트 미리보기 실패")
                err = "미리보기에 실패했습니다. 다시 시도해 주세요."
        return RuleTestView(data=dict(data), match_codes=codes, result=result, err=err,
                            msg=msg, stale=stale, focus_preview=result is not None)


@app.get("/config/rules/test", response_class=HTMLResponse)
async def config_rules_test(request: Request):
    view = await run_in_threadpool(_rule_test_view)
    today = date.today()
    view.data.update({"dt_from": request.query_params.get("dt_from", (today - timedelta(days=29)).isoformat()),
                 "dt_to": request.query_params.get("dt_to", today.isoformat()),
                 "search": request.query_params.get("q", "")})
    msg = None
    if request.query_params.get("applied") == "1":
        msg = "규칙을 적용했습니다"
        counts = [request.query_params.get(key, "") for key in ("vis", "u", "c", "k")]
        if all(re.fullmatch(r"[0-9]{1,12}", value) for value in counts):
            vis, u, c, k = map(int, counts)
            msg += f" — 표시 {vis}건(업종 {u} · 분류 {c} · 키워드 {k})"
        msg += " (저장된 전체 공고 기준)"
    view.msg = msg
    if request.query_params.get("err") == "form":
        view.err = rule_test.FORM_MESSAGE
    return HTMLResponse(_render_rule_test_page(view))


@app.post("/config/rules/test", response_class=HTMLResponse)
async def config_rules_test_preview(request: Request):
    data = await request.form()
    try:
        rule_test.validate_form_structure(data)
    except rule_test.FormStructureError:
        return _rule_test_reload_redirect(data, "form")
    view = await run_in_threadpool(_rule_test_view, data, preview=True)
    return HTMLResponse(_render_rule_test_page(view), status_code=400 if view.err else 200)


@app.post("/config/rules/test/apply")
async def config_rules_test_apply(request: Request):
    data = await request.form()
    try:
        rule_test.validate_form_structure(data)
    except rule_test.FormStructureError:
        return _rule_test_reload_redirect(data, "form")

    def save():
        with classifier.CLASSIFY_WRITE_LOCK, SessionLocal() as session, session.no_autoflush:
            try:
                rows = repository.list_list_rules(session)
                codes = classifier.load_match_codes(session)
                if data.get("fingerprint") != rule_test.rules_fingerprint(rows, codes):
                    raise rule_test.StaleRulesError
                values = rule_test.draft_values(data)
                # 적용은 전체 공고를 재분류하지만 되돌려 줄 폼의 기간 입력도 검증한다.
                rule_test.date_bounds(data)
                plan = rule_test.rule_plan(rows, values)
                rules = classifier.rule_set_from_rows(plan)
                rule_test.apply_plan(session, plan)
                return classifier.reclassify_notices(session, rules=rules, match_codes=codes, commit=True)
            except Exception:
                session.rollback()
                raise

    try:
        stats = await run_in_threadpool(save)
    except rule_test.StaleRulesError:
        view = await run_in_threadpool(_rule_test_view, data, err=rule_test.STALE_MESSAGE)
        view.stale = True
        return HTMLResponse(_render_rule_test_page(view), status_code=409)
    except rule_test.DraftValidationError as exc:
        err = str(exc)
    except Exception:
        logger.exception("규칙 테스트 적용 실패 — 변경을 롤백했습니다.")
        err = "규칙 적용에 실패했습니다. 변경을 취소했습니다. 다시 시도해 주세요."
    else:
        params = {"applied": "1", **_classification_params(stats),
                  **_rule_test_filter_params(data)}
        return RedirectResponse(url=f"/config/rules/test?{urlencode(params)}", status_code=303)
    view = await run_in_threadpool(_rule_test_view, data, err=err)
    return HTMLResponse(_render_rule_test_page(view), status_code=400)


def _parse_rule_id(raw: Any) -> tuple[int, str]:
    """규칙 참조는 ASCII 숫자 id와 12자리 16진 지문만 허용한다."""
    value = str(raw)
    if re.fullmatch(r"[0-9]+:[0-9a-fA-F]{12}", value) is None:
        raise ValueError("규칙 목록이 바뀌었습니다. 새로고침 후 다시 시도하세요.")
    rule_id, fingerprint = value.split(":")
    return int(rule_id), fingerprint.lower()


def _rules_error_redirect(err: str) -> RedirectResponse:
    return RedirectResponse(url=f"/config?rules_err={quote(err)}#rules", status_code=303)


@app.post("/config/keywords")
async def config_keywords_save(request: Request):
    """제외 키워드 목록 관리 — 체크(기본 적용) 반영 + 추가 또는 삭제.

    한 폼에서 세 동작을 처리한다(제출 버튼의 name/value 로 구분):
    - `delete_id=<id>`: 해당 키워드 삭제.
    - `action=add`: `new_keyword` 추가(빈값·중복은 에러 메시지).
    - `action=save`(기본): 체크 상태만 저장.
    어느 경우든 화면에 체크된 `default_ids` 를 먼저 반영해 사용자의 체크 편집을 잃지 않는다.
    """
    form = await request.form()
    action = str(form.get("action") or "")
    delete_raw = str(form.get("delete_id") or "").strip()
    default_ids = [
        int(str(v).strip()) for v in form.getlist("default_ids")
        if re.fullmatch(r"[0-9]{1,12}", str(v).strip())
    ]

    err: str | None = None
    with SessionLocal() as session:
        # 체크 상태는 항상 먼저 저장(추가·삭제와 함께 제출돼도 편집분 유지).
        repository.set_default_exclude_keywords(session, default_ids)

        if re.fullmatch(r"[0-9]{1,12}", delete_raw):
            repository.delete_exclude_keyword(session, int(delete_raw))
        elif action == "add":
            raw = str(form.get("new_keyword") or "")
            kw = repository.normalize_keyword(raw)
            if not kw:
                err = "추가할 키워드를 입력하세요(콤마는 사용할 수 없습니다)."
            elif not repository.add_exclude_keyword(session, kw):
                err = f"이미 등록된 키워드입니다('{kw}')."

    if err:
        return RedirectResponse(url=f"/config?err={quote(err)}#keywords", status_code=303)
    return RedirectResponse(url="/config?saved=1#keywords", status_code=303)


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


# --- Phase 7.1: 검토 목록 장바구니 ---

def _cart_search_url(item_type: str, title: str) -> str:
    """기본 숨김 필터를 해제하고 장바구니 항목 제목으로 검색하는 목록 URL."""
    if item_type == "bid":
        path = "/list"
        params = [
            ("q", title),
            ("date_field", "bid_ntce_dt"),
            ("kind", "reg,re,chg,cncl"),
            ("dt_from", "2000-01-01"),
            ("dt_to", "2099-12-31"),
            ("include_past", "1"),
            ("excl_privt", "0"),
            ("excl_kw", ""),
            ("excl_kw_set", "1"),
            ("price_set", "1"),
        ]
    else:
        path = "/pre-spec"
        params = [
            ("q", title),
            ("dt_from", "2000-01-01"),
            ("dt_to", "2099-12-31"),
            ("include_past", "1"),
            ("excl_kw", ""),
            ("excl_kw_set", "1"),
            ("price_set", "1"),
        ]
    # 빈 가격을 명시해 설정 기본 한도를 해제하고 가격 없는 공고도 표시한다.
    return f"{path}?{urlencode(params)}"

@app.get("/api/export-cart")
def api_export_cart_list():
    with SessionLocal() as session:
        items = repository.get_export_cart(session)
    for item in items:
        item["search_url"] = _cart_search_url(item["item_type"], item["title"])
    return {"items": items, "count": len(items)}


@app.post("/api/export-cart")
def api_export_cart_add(body: dict):
    items = body.get("items", [])
    with SessionLocal() as session:
        added = repository.add_export_cart_items(session, items)
    return {"added": added}


@app.delete("/api/export-cart/all")
def api_export_cart_clear():
    with SessionLocal() as session:
        repository.clear_export_cart(session)
    return {"cleared": True}


@app.delete("/api/export-cart/{cart_id}")
def api_export_cart_delete(cart_id: int):
    with SessionLocal() as session:
        ok = repository.delete_export_cart_item(session, cart_id)
    return {"deleted": ok}


@app.get("/api/export-cart/download")
def api_export_cart_download():
    from io import BytesIO
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
    from urllib.parse import quote as _quote

    with SessionLocal() as session:
        items = repository.get_export_cart(session)

    wb = Workbook()
    ws = wb.active
    ws.title = "검토목록"

    HEADERS = ["구분", "공고번호", "제목", "발주기관", "수요기관",
               "배정예산", "추정가격", "공고일시", "개찰/의견마감일시"]
    COL_WIDTHS = [10, 20, 50, 20, 20, 15, 15, 18, 18]

    # 헤더 행 스타일
    header_fill = PatternFill(fill_type="solid", fgColor="1F4E79")
    header_font = Font(bold=True, color="FFFFFF", size=11)
    for col, (h, w) in enumerate(zip(HEADERS, COL_WIDTHS), 1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
        ws.column_dimensions[get_column_letter(col)].width = w
    ws.row_dimensions[1].height = 20

    # 데이터 행
    link_font = Font(color="0563C1", underline="single")
    for row_idx, item in enumerate(items, 2):
        is_bid = item["item_type"] == "bid"
        label = "입찰공고" if is_bid else "사전규격"
        bid_url = (
            f"https://www.g2b.go.kr/link/PNPE027_01/single/?bidPbancNo={item['item_id']}&bidPbancOrd=000"
            if is_bid else None
        )

        values = [
            label,
            item["item_id"],
            item["title"],
            item.get("ntce_instt_nm") or "",
            item.get("dminstt_nm") or "",
            item.get("asign_bdgt_amt") or "",
            item.get("presmpt_prce") or "-",
            item.get("start_dt") or "",
            item.get("close_dt") or "",
        ]
        for col, val in enumerate(values, 1):
            cell = ws.cell(row=row_idx, column=col, value=val)
            cell.alignment = Alignment(vertical="center", wrap_text=(col == 3))
            # 짝수행 연회색
            if row_idx % 2 == 0:
                cell.fill = PatternFill(fill_type="solid", fgColor="F5F5F5")

        # 제목 셀 하이퍼링크 (입찰공고만)
        title_cell = ws.cell(row=row_idx, column=3)
        if bid_url:
            title_cell.hyperlink = bid_url
            title_cell.font = link_font

    # 헤더 행 고정
    ws.freeze_panes = "A2"

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)

    fname = _quote("검토목록.xlsx")
    headers = {"Content-Disposition": f"attachment; filename*=UTF-8''{fname}"}
    return Response(
        content=buf.read(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=headers,
    )
