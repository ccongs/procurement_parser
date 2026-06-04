"""LibreOffice를 이용한 문서 → PDF 변환.
지원 대상: .doc (HWP/HWPX는 hwp_parser로 직접 처리, DOCX는 python-docx로 직접 처리)
"""
import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

SOFFICE = os.environ.get("SOFFICE_PATH", "soffice")


class DocConversionError(Exception):
    pass


def convert_to_pdf(src_path: Path, out_dir: Path) -> Path:
    """LibreOffice로 src_path → PDF 변환 후 PDF 경로 반환.
    실패 시 DocConversionError.
    out_dir는 caller가 관리하는 tempfile.TemporaryDirectory() 경로여야 함.
    """
    logger.info("[변환] LibreOffice 시작: %s → PDF", src_path.name)
    result = subprocess.run(
        [SOFFICE, "--headless", "--convert-to", "pdf",
         "--outdir", str(out_dir), str(src_path)],
        capture_output=True,
        timeout=120,
    )
    stderr_text = result.stderr.decode(errors="replace")
    if result.returncode != 0:
        logger.error("[변환] LibreOffice 실패(rc=%d): %s", result.returncode, stderr_text[:200])
        raise DocConversionError(f"LibreOffice 변환 실패: {stderr_text}")
    # returncode=0이어도 "could not be loaded" 등 에러가 stderr에 찍힐 수 있음
    if "could not be loaded" in stderr_text or "Error" in stderr_text:
        logger.error("[변환] LibreOffice 변환 오류(rc=0): %s", stderr_text[:200])
        raise DocConversionError(f"LibreOffice 변환 오류: {stderr_text.strip()}")
    pdf_path = out_dir / (src_path.stem + ".pdf")
    if not pdf_path.exists():
        logger.error("[변환] PDF 미생성: %s", pdf_path)
        raise DocConversionError("변환 결과 PDF 파일이 생성되지 않음")
    logger.info("[변환] 완료: %s", pdf_path.name)
    return pdf_path
