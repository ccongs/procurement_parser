"""Phase 6.1 — app/analysis/ 모듈 단위 테스트.

모든 테스트는 Claude API 호출 없이 green이어야 함.
"""
import os
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# ANTHROPIC_API_KEY 가 없어도 import 가능하도록 환경변수 미리 설정
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-placeholder")


# ---------------------------------------------------------------------------
# rfp_schema — import 확인
# ---------------------------------------------------------------------------
from app.analysis.rfp_schema import (
    RFPAnalysis,
    Requirement,
    EvaluationCriterion,
    Deliverable,
    TimelineInfo,
    BudgetInfo,
)


def test_rfp_analysis_defaults():
    """RFPAnalysis 기본값이 올바르게 설정되는지 확인."""
    a = RFPAnalysis(
        project_name="테스트 프로젝트",
        client_name="서울시",
        project_overview="개요",
    )
    assert a.project_name == "테스트 프로젝트"
    assert a.client_name == "서울시"
    assert a.key_requirements == []
    assert a.evaluation_criteria == []
    assert a.pain_points == []
    assert a.win_theme_candidates == []
    assert a.project_type == "general"


def test_rfp_analysis_with_requirements():
    """요구사항·평가기준 포함 RFPAnalysis 생성."""
    a = RFPAnalysis(
        project_name="P1",
        client_name="C1",
        project_overview="ov",
        key_requirements=[
            {"category": "기능", "requirement": "실시간 수집", "priority": "필수"}
        ],
        evaluation_criteria=[
            {"category": "기술", "item": "아키텍처", "weight": 30}
        ],
    )
    assert len(a.key_requirements) == 1
    assert a.key_requirements[0].requirement == "실시간 수집"
    assert a.evaluation_criteria[0].weight == 30


# ---------------------------------------------------------------------------
# doc_converter
# ---------------------------------------------------------------------------
from app.analysis.doc_converter import convert_to_pdf, DocConversionError


@patch("app.analysis.doc_converter.subprocess.run")
def test_convert_to_pdf_success(mock_run, tmp_path):
    """LibreOffice 정상 실행 시 PDF 경로 반환."""
    mock_run.return_value = MagicMock(returncode=0, stderr=b"")
    src = tmp_path / "test.hwp"
    src.write_bytes(b"fake hwp")
    # 변환 결과 파일 생성 시뮬레이션
    (tmp_path / "test.pdf").write_bytes(b"fake pdf")

    pdf = convert_to_pdf(src, tmp_path)

    assert pdf.suffix == ".pdf"
    assert pdf.name == "test.pdf"


@patch("app.analysis.doc_converter.subprocess.run")
def test_convert_to_pdf_nonzero_returncode(mock_run, tmp_path):
    """LibreOffice 실패 시 DocConversionError."""
    mock_run.return_value = MagicMock(returncode=1, stderr=b"error msg")
    src = tmp_path / "bad.doc"
    src.write_bytes(b"data")

    with pytest.raises(DocConversionError):
        convert_to_pdf(src, tmp_path)


@patch("app.analysis.doc_converter.subprocess.run")
def test_convert_to_pdf_no_output_file(mock_run, tmp_path):
    """returncode=0 이지만 PDF 파일이 없을 때 DocConversionError."""
    mock_run.return_value = MagicMock(returncode=0, stderr=b"")
    src = tmp_path / "test.hwp"
    src.write_bytes(b"fake hwp")
    # PDF 파일을 생성하지 않음

    with pytest.raises(DocConversionError, match="생성되지 않음"):
        convert_to_pdf(src, tmp_path)


@patch("app.analysis.doc_converter.subprocess.run")
def test_convert_to_pdf_stderr_error(mock_run, tmp_path):
    """returncode=0 이지만 stderr에 'could not be loaded' 에러 → DocConversionError."""
    mock_run.return_value = MagicMock(
        returncode=0,
        stderr=b"could not be loaded: /tmp/test.doc",
    )
    src = tmp_path / "test.doc"
    src.write_bytes(b"fake doc")

    with pytest.raises(DocConversionError, match="변환 오류"):
        convert_to_pdf(src, tmp_path)


# ---------------------------------------------------------------------------
# pdf_parser
# ---------------------------------------------------------------------------
from app.analysis.pdf_parser import PDFParser


def test_pdf_parser_extract_text_valid(tmp_path):
    """유효한 PDF → 텍스트 추출 (빈 텍스트여도 에러 없음)."""
    # 실제 pypdf는 빈 bytes를 PDF로 인식하지 못하므로 예외 → 빈 문자열 반환 확인
    fake_pdf = tmp_path / "fake.pdf"
    fake_pdf.write_bytes(b"not a real pdf")

    parser = PDFParser()
    text = parser.extract_text(fake_pdf)
    # 잘못된 PDF → "" 반환 (에러는 내부에서 처리)
    assert isinstance(text, str)


@patch("app.analysis.pdf_parser.pypdf.PdfReader")
def test_pdf_parser_extract_text_mocked(mock_reader_cls, tmp_path):
    """pypdf mock → 텍스트 추출 정상 동작."""
    fake_pdf = tmp_path / "doc.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4 mock")

    page1 = MagicMock()
    page1.extract_text.return_value = "페이지1 내용"
    page2 = MagicMock()
    page2.extract_text.return_value = "페이지2 내용"

    mock_reader = MagicMock()
    mock_reader.pages = [page1, page2]
    mock_reader_cls.return_value = mock_reader

    parser = PDFParser()
    text = parser.extract_text(fake_pdf)

    assert "페이지1 내용" in text
    assert "페이지2 내용" in text
    assert "페이지 1" in text
    assert "페이지 2" in text


# ---------------------------------------------------------------------------
# docx_parser
# ---------------------------------------------------------------------------
from app.analysis.docx_parser import DOCXParser


@patch("app.analysis.docx_parser.Document")
def test_docx_parser_extract_text(mock_doc_cls, tmp_path):
    """python-docx mock → 텍스트 추출."""
    para1 = MagicMock()
    para1.text = "첫 번째 문단"
    para2 = MagicMock()
    para2.text = "두 번째 문단"
    para3 = MagicMock()
    para3.text = ""  # 빈 문단은 제외

    mock_doc = MagicMock()
    mock_doc.paragraphs = [para1, para2, para3]
    mock_doc_cls.return_value = mock_doc

    fake_docx = tmp_path / "doc.docx"
    fake_docx.write_bytes(b"fake docx")

    parser = DOCXParser()
    text = parser.extract_text(fake_docx)

    assert "첫 번째 문단" in text
    assert "두 번째 문단" in text
    assert text.count("\n") == 1  # 두 문단 사이 개행 1개


@patch("app.analysis.docx_parser.Document")
def test_docx_parser_extract_text_error(mock_doc_cls, tmp_path):
    """Document() 예외 시 빈 문자열 반환."""
    mock_doc_cls.side_effect = Exception("corrupt file")

    fake_docx = tmp_path / "bad.docx"
    fake_docx.write_bytes(b"bad")

    parser = DOCXParser()
    text = parser.extract_text(fake_docx)
    assert text == ""


# ---------------------------------------------------------------------------
# base_agent — JSON 추출
# ---------------------------------------------------------------------------
from app.analysis.base_agent import BaseAgent


class _ConcreteAgent(BaseAgent):
    """테스트용 구체 클래스."""
    async def execute(self, input_data, progress_callback=None):
        return None


def test_base_agent_extract_json_code_block():
    """```json ... ``` 블록에서 JSON 추출."""
    agent = _ConcreteAgent()
    text = '```json\n{"key": "value", "num": 42}\n```'
    result = agent._extract_json(text)
    assert result == {"key": "value", "num": 42}


def test_base_agent_extract_json_plain():
    """중괄호 패턴으로 JSON 추출."""
    agent = _ConcreteAgent()
    text = '분석 결과: {"project_name": "테스트"}'
    result = agent._extract_json(text)
    assert result["project_name"] == "테스트"


def test_base_agent_extract_json_invalid():
    """JSON 없으면 빈 dict."""
    agent = _ConcreteAgent()
    result = agent._extract_json("plain text without json")
    assert result == {}


def test_base_agent_truncate_text():
    """텍스트 길이 제한."""
    agent = _ConcreteAgent()
    long_text = "a" * 40000
    truncated = agent._truncate_text(long_text, max_chars=30000)
    assert len(truncated) < 40000
    assert "잘렸습니다" in truncated


def test_base_agent_truncate_text_short():
    """짧은 텍스트는 그대로."""
    agent = _ConcreteAgent()
    short = "hello"
    assert agent._truncate_text(short) == short


# ---------------------------------------------------------------------------
# analyzer_service — 지원/미지원 형식 경계 + 분석 흐름
# ---------------------------------------------------------------------------
from app.analysis.analyzer_service import (
    analyze_file,
    analyze_from_url,
    UnsupportedFormatError,
    AnalysisResult,
    _extract_filename,
)


@pytest.mark.asyncio
async def test_analyze_file_unsupported_formats():
    """.zip, .xlsx, .txt → UnsupportedFormatError."""
    for ext in [".zip", ".xlsx", ".txt"]:
        with pytest.raises(UnsupportedFormatError):
            await analyze_file(b"data", f"file{ext}")


@pytest.mark.asyncio
async def test_analyze_file_pdf_ok():
    """PDF 파일 → status=ok (PDFParser·RFPAnalyzer mock)."""
    mock_rfp = RFPAnalysis(
        project_name="테스트 프로젝트",
        client_name="서울시",
        project_overview="개요",
    )

    with patch("app.analysis.analyzer_service.PDFParser") as mock_parser_cls, \
         patch("app.analysis.analyzer_service.RFPAnalyzer") as mock_analyzer_cls:

        mock_parser_cls.return_value.extract_text.return_value = "제안요청서 내용..."
        mock_analyzer_cls.return_value.execute = AsyncMock(return_value=mock_rfp)

        result = await analyze_file(b"%PDF-1.4...", "test.pdf")

    assert result.status == "ok"
    assert result.analysis is not None
    assert result.analysis.project_name == "테스트 프로젝트"


@pytest.mark.asyncio
async def test_analyze_file_docx_ok():
    """DOCX 파일 → status=ok (DOCXParser·RFPAnalyzer mock)."""
    mock_rfp = RFPAnalysis(
        project_name="DOCX 프로젝트",
        client_name="기관명",
        project_overview="DOCX 개요",
    )

    with patch("app.analysis.analyzer_service.DOCXParser") as mock_parser_cls, \
         patch("app.analysis.analyzer_service.RFPAnalyzer") as mock_analyzer_cls:

        mock_parser_cls.return_value.extract_text.return_value = "DOCX 내용..."
        mock_analyzer_cls.return_value.execute = AsyncMock(return_value=mock_rfp)

        result = await analyze_file(b"PK\x03\x04...", "document.docx")

    assert result.status == "ok"
    assert result.analysis.project_name == "DOCX 프로젝트"


@pytest.mark.asyncio
async def test_analyze_file_hwp_ok():
    """HWP 파일 → hwp_parser 직접 파싱 + RFPAnalyzer (모두 mock)."""
    mock_rfp = RFPAnalysis(
        project_name="HWP 프로젝트",
        client_name="발주처",
        project_overview="HWP 개요",
    )

    with patch("app.analysis.analyzer_service.hwp_extract_text", return_value="HWP 본문 내용...") as mock_hwp, \
         patch("app.analysis.analyzer_service.RFPAnalyzer") as mock_analyzer_cls:

        mock_analyzer_cls.return_value.execute = AsyncMock(return_value=mock_rfp)

        result = await analyze_file(b"HWP data", "test.hwp")

    assert result.status == "ok"
    assert result.analysis.project_name == "HWP 프로젝트"
    mock_hwp.assert_called_once()


@pytest.mark.asyncio
async def test_analyze_file_doc_ok():
    """.doc 파일 → LibreOffice 변환 경로 (mock)."""
    mock_rfp = RFPAnalysis(
        project_name="DOC 프로젝트",
        client_name="발주처",
        project_overview="개요",
    )

    with patch("app.analysis.analyzer_service.convert_to_pdf") as mock_convert, \
         patch("app.analysis.analyzer_service.PDFParser") as mock_parser_cls, \
         patch("app.analysis.analyzer_service.RFPAnalyzer") as mock_analyzer_cls:

        mock_convert.return_value = Path("/tmp/test.pdf")
        mock_parser_cls.return_value.extract_text.return_value = "DOC 변환 텍스트"
        mock_analyzer_cls.return_value.execute = AsyncMock(return_value=mock_rfp)

        result = await analyze_file(b"DOC data", "test.doc")

    assert result.status == "ok"


@pytest.mark.asyncio
async def test_analyze_file_hwpx_ok():
    """.hwpx 파일 → hwp_parser 직접 파싱 + RFPAnalyzer (모두 mock)."""
    mock_rfp = RFPAnalysis(
        project_name="HWPX 프로젝트",
        client_name="발주처",
        project_overview="개요",
    )

    with patch("app.analysis.analyzer_service.hwp_extract_text", return_value="HWPX 텍스트") as mock_hwp, \
         patch("app.analysis.analyzer_service.RFPAnalyzer") as mock_analyzer_cls:

        mock_analyzer_cls.return_value.execute = AsyncMock(return_value=mock_rfp)

        result = await analyze_file(b"HWPX data", "test.hwpx")

    assert result.status == "ok"
    mock_hwp.assert_called_once()


@pytest.mark.asyncio
async def test_analyze_file_hwp_parse_error():
    """HWP 파싱 실패(HWPParseError) 시 DocConversionError로 래핑되어 re-raise."""
    from app.analysis.doc_converter import DocConversionError
    from app.analysis.hwp_parser import HWPParseError

    with patch("app.analysis.analyzer_service.hwp_extract_text") as mock_hwp:
        mock_hwp.side_effect = HWPParseError("OLE 파일 열기 실패")
        with pytest.raises(DocConversionError):
            await analyze_file(b"data", "test.hwp")


@pytest.mark.asyncio
async def test_analyze_file_doc_conversion_error():
    """.doc LibreOffice 변환 실패 시 DocConversionError가 re-raise 됨."""
    from app.analysis.doc_converter import DocConversionError

    with patch("app.analysis.analyzer_service.convert_to_pdf") as mock_convert:
        mock_convert.side_effect = DocConversionError("변환 실패")
        with pytest.raises(DocConversionError):
            await analyze_file(b"data", "test.doc")


@pytest.mark.asyncio
async def test_analyze_from_url_unsupported():
    """URL에서 다운로드한 파일이 미지원 형식이면 status=unsupported."""
    mock_resp = MagicMock()
    mock_resp.content = b"zip data"
    mock_resp.headers = {}
    mock_resp.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch("app.analysis.analyzer_service.httpx.AsyncClient", return_value=mock_client):
        # URL 끝이 .zip → UnsupportedFormatError → status=unsupported
        result = await analyze_from_url("http://example.com/files/document.zip")

    assert result.status == "unsupported"


@pytest.mark.asyncio
async def test_analyze_from_url_ok():
    """URL에서 PDF 다운로드 → 정상 분석."""
    mock_rfp = RFPAnalysis(
        project_name="URL 프로젝트",
        client_name="발주처",
        project_overview="개요",
    )

    mock_resp = MagicMock()
    mock_resp.content = b"%PDF-1.4..."
    mock_resp.headers = {}
    mock_resp.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch("app.analysis.analyzer_service.httpx.AsyncClient", return_value=mock_client), \
         patch("app.analysis.analyzer_service.PDFParser") as mock_parser_cls, \
         patch("app.analysis.analyzer_service.RFPAnalyzer") as mock_analyzer_cls:

        mock_parser_cls.return_value.extract_text.return_value = "PDF 내용"
        mock_analyzer_cls.return_value.execute = AsyncMock(return_value=mock_rfp)

        result = await analyze_from_url("http://example.com/files/rfp.pdf")

    assert result.status == "ok"
    assert result.analysis.project_name == "URL 프로젝트"


# ---------------------------------------------------------------------------
# _extract_filename
# ---------------------------------------------------------------------------
def test_extract_filename_from_content_disposition():
    """Content-Disposition: filename= 에서 파일명 추출."""
    resp = MagicMock()
    resp.headers = {"content-disposition": 'attachment; filename="제안요청서.pdf"'}
    name = _extract_filename(resp, "http://example.com/dl?id=1")
    assert name == "제안요청서.pdf"


def test_extract_filename_from_url_fallback():
    """Content-Disposition 없으면 URL 경로 마지막 세그먼트."""
    resp = MagicMock()
    resp.headers = {}
    name = _extract_filename(resp, "http://example.com/files/rfp_doc.pdf?token=abc")
    assert name == "rfp_doc.pdf"


def test_extract_filename_rfc5987():
    """RFC 5987 UTF-8 인코딩 파일명 처리."""
    resp = MagicMock()
    resp.headers = {
        "content-disposition": "attachment; filename*=UTF-8''%EC%A0%9C%EC%95%88%EC%9A%94%EC%B2%AD%EC%84%9C.pdf"
    }
    name = _extract_filename(resp, "http://example.com/dl")
    assert "제안요청서" in name


# ---------------------------------------------------------------------------
# 디렉토리·파일 존재 확인
# ---------------------------------------------------------------------------
def test_analysis_module_files_exist():
    """app/analysis/ 필수 파일 모두 존재하는지 확인."""
    base = Path(__file__).parent.parent / "app" / "analysis"
    required = [
        "__init__.py",
        "pdf_parser.py",
        "docx_parser.py",
        "base_agent.py",
        "rfp_analyzer.py",
        "rfp_schema.py",
        "doc_converter.py",
        "analyzer_service.py",
        "prompts/rfp_analysis.txt",
    ]
    for f in required:
        assert (base / f).exists(), f"파일 없음: app/analysis/{f}"


def test_prompt_file_nonempty():
    """rfp_analysis.txt가 비어있지 않은지 확인."""
    prompt_path = Path(__file__).parent.parent / "app" / "analysis" / "prompts" / "rfp_analysis.txt"
    content = prompt_path.read_text(encoding="utf-8")
    assert len(content) > 100, "프롬프트 파일이 너무 짧음"


# ---------------------------------------------------------------------------
# rfp_analyzer — provider 주입 패턴 (Phase 6.4)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_rfp_analyzer_with_provider_mock():
    """RFPAnalyzer — AnalysisProvider.complete mock 으로 실제 API 없이 동작 확인."""
    from app.analysis.rfp_analyzer import RFPAnalyzer
    from app.analysis.rfp_schema import RFPAnalysis

    mock_provider = AsyncMock()
    mock_provider.complete = AsyncMock(
        return_value='{"project_name": "테스트 프로젝트", "client_name": "서울시", "project_overview": "개요"}'
    )

    analyzer = RFPAnalyzer(provider=mock_provider)
    result = await analyzer.execute({"text": "RFP 내용입니다."})

    assert isinstance(result, RFPAnalysis)
    assert result.project_name == "테스트 프로젝트"
    assert result.client_name == "서울시"
    mock_provider.complete.assert_awaited_once()


@pytest.mark.asyncio
async def test_rfp_analyzer_create_provider_called_when_no_provider(monkeypatch):
    """provider 인자 없을 때 create_provider() 가 호출된다."""
    from app.analysis.rfp_schema import RFPAnalysis

    mock_provider = AsyncMock()
    mock_provider.complete = AsyncMock(
        return_value='{"project_name": "자동 프로바이더", "client_name": "기관", "project_overview": "개요"}'
    )

    with patch("app.analysis.rfp_analyzer.create_provider", return_value=mock_provider) as mock_factory:
        from app.analysis.rfp_analyzer import RFPAnalyzer
        analyzer = RFPAnalyzer()
        result = await analyzer.execute({"text": "내용"})

    mock_factory.assert_called_once()
    assert result.project_name == "자동 프로바이더"
