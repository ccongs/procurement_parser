"""HWP 5.x 바이너리 텍스트 추출 (olefile 기반, LibreOffice 불필요).

HWP 레코드 구조:
  - 4바이트 헤더: bits[0:9]=tag_id, bits[10:19]=level, bits[20:31]=size
  - size==0xFFF 이면 다음 4바이트에 실제 크기
  - HWPTAG_PARA_TEXT (tag_id=67): 2바이트씩 UTF-16LE 문자
    - 0x000D: 줄바꿈 / 0xFFFF: 표 셀 구분 등 특수값
"""
import re
import struct
import zlib
from pathlib import Path

import olefile


class HWPParseError(Exception):
    pass


def extract_text(hwp_path: Path) -> str:
    """HWP 파일에서 텍스트 추출.

    반환: 줄바꿈 정리된 평문 문자열.
    실패 시 HWPParseError.
    """
    try:
        ole = olefile.OleFileIO(str(hwp_path))
    except Exception as e:
        raise HWPParseError(f"OLE 파일 열기 실패: {e}") from e

    try:
        # FileHeader: 압축 여부는 byte[36]의 bit 0
        fh = ole.openstream("FileHeader").read()
        compressed = bool(fh[36] & 1)

        raw = ole.openstream("BodyText/Section0").read()
        data = zlib.decompress(raw, -15) if compressed else raw
    except Exception as e:
        raise HWPParseError(f"스트림 읽기 실패: {e}") from e
    finally:
        ole.close()

    texts = _parse_records(data)
    text = re.sub(r"[ \t]+", " ", "".join(texts))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def _parse_records(data: bytes) -> list[str]:
    """BodyText 레코드에서 HWPTAG_PARA_TEXT(67) 페이로드만 추출."""
    PARA_TEXT_ID = 67
    texts: list[str] = []
    pos = 0

    while pos + 4 <= len(data):
        header = struct.unpack_from("<I", data, pos)[0]
        tag_id = header & 0x3FF
        size = (header >> 20) & 0xFFF
        pos += 4

        if size == 0xFFF:
            if pos + 4 > len(data):
                break
            size = struct.unpack_from("<I", data, pos)[0]
            pos += 4

        payload = data[pos : pos + size]
        pos += size

        if tag_id != PARA_TEXT_ID:
            continue

        i = 0
        while i + 1 < len(payload):
            ch = struct.unpack_from("<H", payload, i)[0]
            i += 2
            if ch == 0x000D:          # 문단 종료
                texts.append("\n")
            elif 0x0020 <= ch <= 0x007E:   # ASCII 출력 가능
                texts.append(chr(ch))
            elif 0xAC00 <= ch <= 0xD7A3:   # 한글 완성형
                texts.append(chr(ch))
            elif 0x3131 <= ch <= 0x318E:   # 한글 자모
                texts.append(chr(ch))
            elif 0x2160 <= ch <= 0x2BFF:   # 특수기호(로마숫자 등)
                texts.append(chr(ch))
            # 그 외 바이너리 제어문자는 무시

    return texts
