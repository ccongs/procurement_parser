"""Phase 6.5 — app/analysis/hwp_parser 단위 테스트.

Claude API 호출 없이 green이어야 함.
"""
import struct
import zlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.analysis.hwp_parser import HWPParseError, _parse_records, extract_text


# ---------------------------------------------------------------------------
# extract_text — 에러 경로
# ---------------------------------------------------------------------------


def test_extract_text_invalid_file(tmp_path):
    """OLE 아닌 파일 → HWPParseError."""
    bad = tmp_path / "bad.hwp"
    bad.write_bytes(b"not an ole file")
    with pytest.raises(HWPParseError):
        extract_text(bad)


def test_extract_text_missing_file(tmp_path):
    """존재하지 않는 파일 → HWPParseError."""
    missing = tmp_path / "nonexistent.hwp"
    with pytest.raises(HWPParseError):
        extract_text(missing)


# ---------------------------------------------------------------------------
# _parse_records — 단위 테스트
# ---------------------------------------------------------------------------


def test_parse_records_empty():
    """빈 데이터 → 빈 리스트."""
    assert _parse_records(b"") == []


def test_parse_records_no_para_text():
    """PARA_TEXT(67) 이외 레코드만 있으면 빈 리스트."""
    # tag_id=1, level=0, size=4 → header = (4 << 20) | (0 << 10) | 1
    header = struct.pack("<I", (4 << 20) | 1)
    payload = b"\x00\x00\x00\x00"
    data = header + payload
    assert _parse_records(data) == []


def _make_para_text_record(chars: list[int]) -> bytes:
    """tag_id=67(PARA_TEXT), 주어진 UTF-16LE 코드포인트 시퀀스로 레코드 생성."""
    payload = b"".join(struct.pack("<H", ch) for ch in chars)
    size = len(payload)
    # tag_id=67, level=0, size in bits[20:32]
    header = struct.pack("<I", (size << 20) | 67)
    return header + payload


def test_parse_records_ascii():
    """ASCII 문자(0x20-0x7E) 정상 추출."""
    chars = [ord("H"), ord("e"), ord("l"), ord("l"), ord("o")]
    data = _make_para_text_record(chars)
    result = _parse_records(data)
    assert "".join(result) == "Hello"


def test_parse_records_paragraph_end():
    """0x000D(문단 종료) → '\n' 추가."""
    chars = [ord("A"), 0x000D, ord("B")]
    data = _make_para_text_record(chars)
    result = _parse_records(data)
    assert result == ["A", "\n", "B"]


def test_parse_records_hangul():
    """한글 완성형(0xAC00-0xD7A3) 정상 추출."""
    chars = [0xAC00, 0xB098, 0xB2E4]  # 가나다
    data = _make_para_text_record(chars)
    result = _parse_records(data)
    assert "".join(result) == "가나다"


def test_parse_records_jamo():
    """한글 자모(0x3131-0x318E) 정상 추출."""
    chars = [0x3131, 0x3134]  # ㄱ, ㄴ
    data = _make_para_text_record(chars)
    result = _parse_records(data)
    assert "".join(result) == "ㄱㄴ"


def test_parse_records_special_symbol():
    """특수기호(0x2160-0x2BFF) 정상 추출 — 로마숫자 Ⅰ."""
    chars = [0x2160]  # Ⅰ
    data = _make_para_text_record(chars)
    result = _parse_records(data)
    assert result == [chr(0x2160)]


def test_parse_records_control_chars_ignored():
    """제어문자(0x0001 등) → 무시(리스트에 포함 안 됨)."""
    chars = [0x0001, ord("A"), 0xFFFF]
    data = _make_para_text_record(chars)
    result = _parse_records(data)
    assert result == ["A"]


def test_parse_records_extended_size():
    """size==0xFFF → 다음 4바이트가 실제 크기(extended size 처리)."""
    chars = [ord("X"), ord("Y")]
    payload = b"".join(struct.pack("<H", ch) for ch in chars)
    size = len(payload)
    # size 필드를 0xFFF로 설정 → extended size 모드
    header = struct.pack("<I", (0xFFF << 20) | 67)
    ext_size = struct.pack("<I", size)
    data = header + ext_size + payload
    result = _parse_records(data)
    assert "".join(result) == "XY"


def test_parse_records_multiple_records():
    """여러 레코드 — PARA_TEXT 2개 + 다른 태그 1개."""
    rec1 = _make_para_text_record([ord("A"), 0x000D])
    # tag_id=1 (non-PARA_TEXT)
    other_header = struct.pack("<I", (0 << 20) | 1)  # size=0, tag=1
    rec2 = _make_para_text_record([ord("B")])
    data = rec1 + other_header + rec2
    result = _parse_records(data)
    assert "".join(result) == "A\nB"


# ---------------------------------------------------------------------------
# extract_text — 정상 경로 (OLE mock)
# ---------------------------------------------------------------------------


def test_extract_text_ok(tmp_path):
    """정상 HWP — OLE mock으로 텍스트 추출 검증."""
    # 압축 없는 BodyText 구성
    chars = [ord("안"), ord("녕")]  # 안(0xC548), 녕(0xB155) — 완성형 범위
    # 실제 유니코드 값: 안=0xC548, 녕=0xB155 (모두 0xAC00-0xD7A3 범위)
    payload = b"".join(struct.pack("<H", ch) for ch in [0xC548, 0xB155])
    size = len(payload)
    header = struct.pack("<I", (size << 20) | 67)
    section_data = header + payload

    # FileHeader: byte[36]의 bit 0 = 0 → 비압축
    fh_bytes = bytearray(64)
    fh_bytes[36] = 0  # compressed=False

    mock_fh_stream = MagicMock()
    mock_fh_stream.read.return_value = bytes(fh_bytes)

    mock_section_stream = MagicMock()
    mock_section_stream.read.return_value = section_data

    mock_ole = MagicMock()
    mock_ole.openstream.side_effect = lambda name: (
        mock_fh_stream if name == "FileHeader" else mock_section_stream
    )

    hwp_file = tmp_path / "sample.hwp"
    hwp_file.write_bytes(b"fake")

    with patch("app.analysis.hwp_parser.olefile.OleFileIO", return_value=mock_ole):
        text = extract_text(hwp_file)

    assert "안" in text
    assert "녕" in text


def test_extract_text_compressed(tmp_path):
    """압축된 BodyText — zlib 압축 데이터 정상 처리."""
    payload = struct.pack("<H", ord("Z"))  # 'Z'
    size = len(payload)
    header = struct.pack("<I", (size << 20) | 67)
    section_data = header + payload

    compressed = zlib.compress(section_data)[2:-4]  # raw deflate (wbits=-15)

    fh_bytes = bytearray(64)
    fh_bytes[36] = 1  # compressed=True

    mock_fh_stream = MagicMock()
    mock_fh_stream.read.return_value = bytes(fh_bytes)

    mock_section_stream = MagicMock()
    mock_section_stream.read.return_value = compressed

    mock_ole = MagicMock()
    mock_ole.openstream.side_effect = lambda name: (
        mock_fh_stream if name == "FileHeader" else mock_section_stream
    )

    hwp_file = tmp_path / "sample.hwp"
    hwp_file.write_bytes(b"fake")

    with patch("app.analysis.hwp_parser.olefile.OleFileIO", return_value=mock_ole):
        text = extract_text(hwp_file)

    assert "Z" in text


def test_extract_text_stream_error(tmp_path):
    """BodyText/Section0 스트림 읽기 실패 → HWPParseError."""
    mock_ole = MagicMock()
    mock_ole.openstream.side_effect = Exception("스트림 없음")

    hwp_file = tmp_path / "broken.hwp"
    hwp_file.write_bytes(b"fake")

    with patch("app.analysis.hwp_parser.olefile.OleFileIO", return_value=mock_ole):
        with pytest.raises(HWPParseError, match="스트림 읽기 실패"):
            extract_text(hwp_file)
