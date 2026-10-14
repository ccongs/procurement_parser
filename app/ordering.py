"""차수의 비교·면허 저장 표기를 공통으로 정규화한다."""


def normalize_order(value: str) -> int | str:
    """기존 비교 규칙을 유지한다(정수 우선, 아니면 3자리 0채움)."""
    value = value.strip()
    try:
        return int(value)
    except ValueError:
        return value.zfill(3)


def stored_order(value: str) -> str:
    """비교와 같은 규칙으로 정규화한 차수를 3자리 0채움해 저장한다."""
    return str(normalize_order(value)).zfill(3)
