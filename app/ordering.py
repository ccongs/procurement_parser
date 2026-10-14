"""차수의 비교·면허 저장 표기를 공통으로 정규화한다."""


def normalize_order(value: str) -> int | str:
    """기존 비교 규칙을 유지한다(정수 우선, 아니면 3자리 0채움)."""
    value = value.strip()
    try:
        return int(value)
    except ValueError:
        return value.zfill(3)


def stored_order(value: str) -> str:
    """숫자 차수는 API 표기인 3자리로 저장하고 비숫자는 공백만 제거한다."""
    value = value.strip()
    try:
        return str(int(value)).zfill(3)
    except ValueError:
        return value
