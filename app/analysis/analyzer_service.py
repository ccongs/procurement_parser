"""파일 바이너리 → RFPAnalysis 변환 서비스.

지원 형식: .pdf, .hwp, .hwpx, .doc, .docx
미지원(.zip 등) → UnsupportedFormatError → 호출자가 no_file 처리
"""
import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx

from app.analysis.doc_converter import DocConversionError, convert_to_pdf
from app.analysis.docx_parser import DOCXParser
from app.analysis.hwp_parser import HWPParseError
from app.analysis.hwp_parser import extract_hwpx_text, extract_text as hwp_extract_text
from app.analysis.pdf_parser import PDFParser
from app.analysis.provider import active_provider_name
from app.analysis.rfp_analyzer import RFPAnalyzer
from app.analysis.rfp_schema import RFPAnalysis

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".pdf", ".hwp", ".hwpx", ".doc", ".docx"}
LIBREOFFICE_EXTENSIONS = {".doc"}  # LibreOffice 변환 대상 (.hwp/.hwpx는 olefile 직접 파싱)


class UnsupportedFormatError(Exception):
    pass


@dataclass
class AnalysisResult:
    status: Literal["ok", "no_file", "unsupported", "error"]
    analysis: RFPAnalysis | None = None
    message: str = ""
    error_kind: str | None = None


def _classify_error(exc: Exception) -> str:
    """LLM/provider 예외를 SDK 의존 없이 HTTP 매핑용으로 분류."""
    exc_name = type(exc).__name__
    if exc_name in {"ResourceExhausted", "RateLimitError", "TooManyRequests"}:
        return "rate_limit"
    if getattr(exc, "status_code", None) == 429 or getattr(exc, "code", None) == 429:
        return "rate_limit"
    message = str(exc).lower()
    if any(token in message for token in ("429", "quota", "rate limit", "resourceexhausted")):
        return "rate_limit"
    return "processing"


async def analyze_from_url(url: str) -> AnalysisResult:
    """URL에서 파일 다운로드 후 분석."""
    return await analyze_from_urls([{"url": url}])


async def analyze_file(file_bytes: bytes, filename: str) -> AnalysisResult:
    """파일 바이너리 → AnalysisResult.

    지원: .pdf / .hwp / .hwpx / .doc / .docx
    미지원 → UnsupportedFormatError (호출자가 no_file 처리 가능)
    """
    logger.info("[분석] 파일 수신: %s (%d bytes)", filename, len(file_bytes))

    try:
        text = extract_text_from_file(file_bytes, filename)
        provider_name = active_provider_name()
        logger.info("[분석] LLM(%s) 호출 시작 (텍스트 %d자)", provider_name, len(text))
        analysis = await RFPAnalyzer().execute({"text": text})
        logger.info("[분석] LLM(%s) 완료: project_name=%s", provider_name, getattr(analysis, "project_name", "?"))
        return AnalysisResult(status="ok", analysis=analysis)

    except (UnsupportedFormatError, DocConversionError):
        raise
    except Exception as e:
        logger.error("[분석] 처리 중 예외: %s", e, exc_info=True)
        return AnalysisResult(status="error", message=f"분석 중 오류: {e}", error_kind=_classify_error(e))


def extract_text_from_file(file_bytes: bytes, filename: str) -> str:
    """파일 바이너리에서 텍스트만 추출한다."""
    safe_filename = Path(filename or "document").name or "document"
    ext = Path(safe_filename).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        logger.warning("[분석] 미지원 형식: %s", ext)
        raise UnsupportedFormatError(f"지원하지 않는 형식: {ext}")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        src = tmp / safe_filename
        src.write_bytes(file_bytes)

        if ext == ".pdf":
            logger.info("[분석] PDF 텍스트 추출 시작")
            text = PDFParser().extract_text(src)
            logger.info("[분석] PDF 추출 완료: %d자", len(text))
            return text

        if ext == ".docx":
            logger.info("[분석] DOCX 텍스트 추출 시작")
            text = DOCXParser().extract_text(src)
            logger.info("[분석] DOCX 추출 완료: %d자", len(text))
            return text

        if ext == ".hwp":
            logger.info("[분석] HWP(OLE) 직접 파싱 시작")
            try:
                text = hwp_extract_text(src)
                logger.info("[분석] HWP 파싱 완료: %d자", len(text))
                return text
            except HWPParseError as e:
                raise DocConversionError(f"HWP 파싱 실패: {e}") from e

        if ext == ".hwpx":
            logger.info("[분석] HWPX(ZIP+XML) 직접 파싱 시작")
            try:
                text = extract_hwpx_text(src)
                logger.info("[분석] HWPX 파싱 완료: %d자", len(text))
                return text
            except HWPParseError as e:
                raise DocConversionError(f"HWPX 파싱 실패: {e}") from e

        logger.info("[분석] LibreOffice 변환 시작: %s", ext)
        pdf_path = convert_to_pdf(src, tmp)
        logger.info("[분석] LibreOffice 변환 완료")
        return PDFParser().extract_text(pdf_path)


async def analyze_documents(docs: list[dict]) -> AnalysisResult:
    """여러 문서를 추출한 뒤 하나의 분석 결과로 종합한다."""
    parsed_docs: list[dict[str, str]] = []
    failures: list[tuple[str, Exception]] = []

    for index, doc in enumerate(docs or [], start=1):
        filename = str(doc.get("filename") or doc.get("label") or f"document-{index}")
        label = str(doc.get("label") or filename)
        file_bytes = doc.get("bytes")
        if file_bytes is None:
            failures.append(("unsupported", UnsupportedFormatError("파일 데이터가 없습니다.")))
            logger.warning("[분석] 문서 데이터 없음: filename=%s", filename)
            continue

        try:
            text = extract_text_from_file(file_bytes, filename)
        except (UnsupportedFormatError, DocConversionError) as e:
            failures.append(("unsupported", e))
            logger.warning("[분석] 문서 추출 실패: filename=%s, error=%s", filename, e)
            continue
        except Exception as e:  # noqa: BLE001 — 다른 문서가 성공하면 계속 진행한다.
            failures.append(("error", e))
            logger.error("[분석] 문서 처리 예외: filename=%s, error=%s", filename, e, exc_info=True)
            continue

        parsed_docs.append({"label": label, "filename": filename, "text": text})

    if not parsed_docs:
        if failures:
            message = "; ".join(str(exc) for _, exc in failures[:3])
            status = "error" if any(kind == "error" for kind, _ in failures) else "unsupported"
            return AnalysisResult(status=status, message=message or "분석 가능한 문서가 없습니다.")
        return AnalysisResult(status="no_file", message="분석할 문서가 없습니다.")

    try:
        total_chars = sum(len(doc["text"]) for doc in parsed_docs)
        provider_name = active_provider_name()
        logger.info(
            "[분석] LLM(%s) 호출 시작 (문서 %d건, 텍스트 %d자)",
            provider_name,
            len(parsed_docs),
            total_chars,
        )
        analysis = await RFPAnalyzer().execute({"documents": parsed_docs})
        logger.info(
            "[분석] LLM(%s) 완료: project_name=%s",
            provider_name,
            getattr(analysis, "project_name", "?"),
        )
        return AnalysisResult(status="ok", analysis=analysis)
    except Exception as e:
        logger.error("[분석] 처리 중 예외: %s", e, exc_info=True)
        return AnalysisResult(status="error", message=f"분석 중 오류: {e}", error_kind=_classify_error(e))


async def analyze_from_urls(items: list[dict]) -> AnalysisResult:
    """여러 URL에서 파일을 내려받아 하나의 분석 결과로 종합한다."""
    docs: list[dict[str, object]] = []
    failures: list[Exception] = []

    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        for item in items or []:
            url = str(item.get("url") or "")
            if not url:
                failures.append(ValueError("URL이 없습니다."))
                continue
            logger.info("[분석] URL 다운로드 시작: %s", url[:80])
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                item_name = str(item.get("filename") or item.get("name") or "")
                filename = (
                    item_name
                    if Path(item_name).suffix.lower() in SUPPORTED_EXTENSIONS
                    else _extract_filename(resp, url)
                )
                label = str(item.get("label") or item.get("doc_kind") or filename)
                logger.info(
                    "[분석] 다운로드 완료: filename=%s, size=%d bytes",
                    filename,
                    len(resp.content),
                )
                docs.append({"filename": filename, "bytes": resp.content, "label": label})
            except Exception as e:  # noqa: BLE001 — 다른 URL이 성공하면 계속 진행한다.
                failures.append(e)
                logger.error("[분석] URL 다운로드 실패: %s, error=%s", url[:80], e, exc_info=True)

    if not docs:
        message = "; ".join(str(exc) for exc in failures[:3])
        return AnalysisResult(status="error", message=message or "다운로드 가능한 문서가 없습니다.")

    return await analyze_documents(docs)


def _extract_filename(resp: httpx.Response, fallback_url: str) -> str:
    """Content-Disposition 헤더에서 파일명 추출, 없으면 URL 마지막 경로 세그먼트."""
    from urllib.parse import unquote
    cd = resp.headers.get("content-disposition", "")
    for part in cd.split(";"):
        part = part.strip()
        if part.startswith("filename*="):
            name = part.split("=", 1)[1].strip()
            # RFC 5987: filename*=UTF-8''encoded-name
            if "UTF-8''" in name.upper():
                name = unquote(name.split("''", 1)[1])
            return name
        if part.startswith("filename="):
            name = part.split("=", 1)[1].strip().strip('"')
            # 나라장터는 filename=에 percent-encode 사용
            if "%" in name:
                name = unquote(name)
            return name
    return Path(fallback_url.split("?")[0]).name or "document"
