"""PDF 문서 파서"""

import logging
from pathlib import Path
from typing import Any, Dict, List

import pypdf
import pdfplumber

logger = logging.getLogger("pdf_parser")


class PDFParser:
    """PDF 문서 파서"""

    def extract_text(self, file_path: Path) -> str:
        """pypdf를 사용한 텍스트 추출"""
        try:
            reader = pypdf.PdfReader(file_path)
            text_parts = []

            for i, page in enumerate(reader.pages):
                page_text = page.extract_text()
                if page_text:
                    text_parts.append(f"--- 페이지 {i + 1} ---\n{page_text}")

            return "\n\n".join(text_parts)
        except Exception as e:
            logger.error(f"텍스트 추출 실패: {e}")
            return ""

    def extract_tables(self, file_path: Path) -> List[Dict[str, Any]]:
        """pdfplumber를 사용한 테이블 추출"""
        tables = []

        try:
            with pdfplumber.open(file_path) as pdf:
                for i, page in enumerate(pdf.pages):
                    page_tables = page.extract_tables()

                    for j, table in enumerate(page_tables):
                        if table and len(table) > 1:
                            headers = [
                                str(cell).strip() if cell else ""
                                for cell in table[0]
                            ]
                            rows = [
                                [str(cell).strip() if cell else "" for cell in row]
                                for row in table[1:]
                            ]

                            tables.append(
                                {
                                    "page": i + 1,
                                    "table_index": j,
                                    "headers": headers,
                                    "rows": rows,
                                    "raw_data": table,
                                }
                            )
        except Exception as e:
            logger.error(f"테이블 추출 실패: {e}")

        return tables
