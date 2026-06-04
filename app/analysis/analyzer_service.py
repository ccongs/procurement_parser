"""파일 바이너리 → RFPAnalysis 변환 서비스.

지원 형식: .pdf, .hwp, .hwpx, .doc, .docx
미지원(.zip 등) → UnsupportedFormatError → 호출자가 no_file 처리
"""
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx

from app.analysis.doc_converter import DocConversionError, convert_to_pdf
from app.analysis.docx_parser import DOCXParser
from app.analysis.pdf_parser import PDFParser
from app.analysis.rfp_analyzer import RFPAnalyzer
from app.analysis.rfp_schema import RFPAnalysis


SUPPORTED_EXTENSIONS = {".pdf", ".hwp", ".hwpx", ".doc", ".docx"}
LIBREOFFICE_EXTENSIONS = {".hwp", ".hwpx", ".doc"}  # LibreOffice 변환 대상


class UnsupportedFormatError(Exception):
    pass


@dataclass
class AnalysisResult:
    status: Literal["ok", "no_file", "unsupported", "error"]
    analysis: RFPAnalysis | None = None
    message: str = ""


async def analyze_from_url(url: str) -> AnalysisResult:
    """URL에서 파일 다운로드 후 분석."""
    try:
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
        filename = _extract_filename(resp, url)
        return await analyze_file(resp.content, filename)
    except UnsupportedFormatError as e:
        return AnalysisResult(status="unsupported", message=str(e))
    except DocConversionError as e:
        return AnalysisResult(
            status="unsupported",
            message=f"파일 변환에 실패했습니다. PDF 또는 DOCX를 업로드해주세요. ({e})",
        )
    except Exception as e:
        return AnalysisResult(status="error", message=f"분석 중 오류: {e}")


async def analyze_file(file_bytes: bytes, filename: str) -> AnalysisResult:
    """파일 바이너리 → AnalysisResult.

    지원: .pdf / .hwp / .hwpx / .doc / .docx
    미지원 → UnsupportedFormatError (호출자가 no_file 처리 가능)
    """
    ext = Path(filename).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise UnsupportedFormatError(f"지원하지 않는 형식: {ext}")

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            src = tmp / filename
            src.write_bytes(file_bytes)

            if ext == ".pdf":
                text = PDFParser().extract_text(src)
            elif ext == ".docx":
                text = DOCXParser().extract_text(src)
            else:  # .hwp, .hwpx, .doc
                pdf_path = convert_to_pdf(src, tmp)
                text = PDFParser().extract_text(pdf_path)

        analysis = await RFPAnalyzer().execute({"text": text})
        return AnalysisResult(status="ok", analysis=analysis)

    except (UnsupportedFormatError, DocConversionError):
        raise
    except Exception as e:
        return AnalysisResult(status="error", message=f"분석 중 오류: {e}")


def _extract_filename(resp: httpx.Response, fallback_url: str) -> str:
    """Content-Disposition 헤더에서 파일명 추출, 없으면 URL 마지막 경로 세그먼트."""
    cd = resp.headers.get("content-disposition", "")
    for part in cd.split(";"):
        part = part.strip()
        if part.startswith("filename=") or part.startswith("filename*="):
            name = part.split("=", 1)[1].strip().strip('"')
            # RFC 5987 인코딩 처리
            if "UTF-8''" in name:
                from urllib.parse import unquote
                name = unquote(name.split("''", 1)[1])
            return name
    return Path(fallback_url.split("?")[0]).name or "document"
