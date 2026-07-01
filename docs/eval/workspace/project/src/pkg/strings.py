"""String helpers with a subtle bug — reverse() drops the last char."""


def reverse(s: str) -> str:
    return s[-2::-1]   # BUG: should be s[::-1]


def upper(s: str) -> str:
    return s.upper()


def word_count(s: str) -> int:
    return len(s.split())
