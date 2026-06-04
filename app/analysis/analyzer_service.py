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


async def analyze_from_url(url: str) -> AnalysisResult:
    """URL에서 파일 다운로드 후 분석."""
    logger.info("[분석] URL 다운로드 시작: %s", url[:80])
    try:
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
        filename = _extract_filename(resp, url)
        logger.info("[분석] 다운로드 완료: filename=%s, size=%d bytes", filename, len(resp.content))
        return await analyze_file(resp.content, filename)
    except UnsupportedFormatError as e:
        logger.warning("[분석] 미지원 형식: %s", e)
        return AnalysisResult(status="unsupported", message=str(e))
    except DocConversionError as e:
        logger.warning("[분석] 변환 실패: %s", e)
        return AnalysisResult(
            status="unsupported",
            message=f"파일 변환에 실패했습니다. PDF 또는 DOCX를 업로드해주세요. ({e})",
        )
    except Exception as e:
        logger.error("[분석] URL 처리 예외: %s", e, exc_info=True)
        return AnalysisResult(status="error", message=f"분석 중 오류: {e}")


async def analyze_file(file_bytes: bytes, filename: str) -> AnalysisResult:
    """파일 바이너리 → AnalysisResult.

    지원: .pdf / .hwp / .hwpx / .doc / .docx
    미지원 → UnsupportedFormatError (호출자가 no_file 처리 가능)
    """
    ext = Path(filename).suffix.lower()
    logger.info("[분석] 파일 수신: %s (%d bytes)", filename, len(file_bytes))

    if ext not in SUPPORTED_EXTENSIONS:
        logger.warning("[분석] 미지원 형식: %s", ext)
        raise UnsupportedFormatError(f"지원하지 않는 형식: {ext}")

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            src = tmp / filename
            src.write_bytes(file_bytes)

            if ext == ".pdf":
                logger.info("[분석] PDF 텍스트 추출 시작")
                text = PDFParser().extract_text(src)
                logger.info("[분석] PDF 추출 완료: %d자", len(text))

            elif ext == ".docx":
                logger.info("[분석] DOCX 텍스트 추출 시작")
                text = DOCXParser().extract_text(src)
                logger.info("[분석] DOCX 추출 완료: %d자", len(text))

            elif ext == ".hwp":
                logger.info("[분석] HWP(OLE) 직접 파싱 시작")
                try:
                    text = hwp_extract_text(src)
                    logger.info("[분석] HWP 파싱 완료: %d자", len(text))
                except HWPParseError as e:
                    raise DocConversionError(f"HWP 파싱 실패: {e}") from e

            elif ext == ".hwpx":
                logger.info("[분석] HWPX(ZIP+XML) 직접 파싱 시작")
                try:
                    text = extract_hwpx_text(src)
                    logger.info("[분석] HWPX 파싱 완료: %d자", len(text))
                except HWPParseError as e:
                    raise DocConversionError(f"HWPX 파싱 실패: {e}") from e

            else:  # .doc
                logger.info("[분석] LibreOffice 변환 시작: %s", ext)
                pdf_path = convert_to_pdf(src, tmp)
                logger.info("[분석] LibreOffice 변환 완료")
                text = PDFParser().extract_text(pdf_path)

        logger.info("[분석] Claude API 호출 시작 (텍스트 %d자)", len(text))
        analysis = await RFPAnalyzer().execute({"text": text})
        logger.info("[분석] Claude API 완료: project_name=%s", getattr(analysis, "project_name", "?"))
        return AnalysisResult(status="ok", analysis=analysis)

    except (UnsupportedFormatError, DocConversionError):
        raise
    except Exception as e:
        logger.error("[분석] 처리 중 예외: %s", e, exc_info=True)
        return AnalysisResult(status="error", message=f"분석 중 오류: {e}")


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
