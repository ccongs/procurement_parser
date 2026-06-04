"""LibreOffice를 이용한 문서 → PDF 변환.
지원 대상: .hwp, .hwpx, .doc (DOCX는 python-docx로 직접 처리)
"""
import os
import subprocess
from pathlib import Path


SOFFICE = os.environ.get("SOFFICE_PATH", "soffice")


class DocConversionError(Exception):
    pass


def convert_to_pdf(src_path: Path, out_dir: Path) -> Path:
    """LibreOffice로 src_path → PDF 변환 후 PDF 경로 반환.
    실패 시 DocConversionError.
    out_dir는 caller가 관리하는 tempfile.TemporaryDirectory() 경로여야 함.
    """
    result = subprocess.run(
        [SOFFICE, "--headless", "--convert-to", "pdf",
         "--outdir", str(out_dir), str(src_path)],
        capture_output=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise DocConversionError(
            f"LibreOffice 변환 실패: {result.stderr.decode(errors='replace')}"
        )
    pdf_path = out_dir / (src_path.stem + ".pdf")
    if not pdf_path.exists():
        raise DocConversionError("변환 결과 PDF 파일이 생성되지 않음")
    return pdf_path
