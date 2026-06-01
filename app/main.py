"""Phase 1 - 브라우저 기반 API 수동 테스트 화면.

엔드포인트 선택 + 파라미터 입력 폼 → api_client 호출 → 응답을 표/원문으로 표시.
실행: uvicorn app.main:app --reload
"""

from __future__ import annotations

import calendar
import html
from datetime import date
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from app import api_client
from app.field_labels import label as field_label
from app.api_client import (
    COMMON_PARAMS,
    ENDPOINTS,
    ENDPOINTS_BY_OP,
    ApiClientError,
    ApiResult,
    ParamSpec,
)

app = FastAPI(title="나라장터 입찰공고 API 테스트 (Phase 1)")

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


def _default_date_range() -> tuple[str, str]:
    """기본 조회 기간: 최근 한 달 (시작=한 달 전, 종료=오늘) ISO(yyyy-mm-dd)."""
    today = date.today()
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

    return f"""
      <div class="row daterow">
        <label class="field">
          <span class="flabel">공고/개찰일자 <code>inqryDiv</code></span>
          <select name="inqryDiv">
            <option value="1"{' selected' if inqry_div == '1' else ''}>게시일자</option>
            <option value="2"{' selected' if inqry_div == '2' else ''}>개찰일시</option>
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
    button.submit {{ background: #1f3a5f; color: #fff; border: 0; padding: 10px 22px;
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
    <h1>나라장터 입찰공고 API 테스트</h1>
    <p>Phase 1 · 엔드포인트를 호출해 응답 형태를 확인합니다 · 베이스 URL: {base_url}</p>
  </header>
  <main>
    <form method="post" action="/call">
      <fieldset>
        <legend>엔드포인트</legend>
        <div class="row">
          <label class="field" style="min-width:320px;">
            <span class="flabel">오퍼레이션 선택</span>
            <select name="operation" onchange="window.location='/?operation='+encodeURIComponent(this.value)">
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


@app.get("/", response_class=HTMLResponse)
def index(operation: str | None = None) -> HTMLResponse:
    op = operation if operation in ENDPOINTS_BY_OP else ENDPOINTS[0].operation
    return HTMLResponse(_render_page(op, api_client.DEFAULT_RESPONSE_TYPE, _default_values()))


@app.post("/call", response_class=HTMLResponse)
async def call(request: Request) -> HTMLResponse:
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
