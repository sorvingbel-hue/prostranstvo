"""Bounded exact answer parsing. No expression evaluation or I/O."""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from math import gcd
import re

MAX_ANSWER_CHARS = 80
MAX_NUMBER = 1_000_000
_ANSWER = re.compile(r"\s*([+-]?)\s*([0-9]+)\s*(?:[/\u2044]\s*([+-]?)\s*([0-9]+)\s*)?", re.ASCII)


class MathInputError(ValueError):
    """Stable error code; never includes a supplied answer or exception text."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ParsedAnswer:
    value: Fraction
    numerator: int
    denominator: int
    is_simplest: bool
    normalized: str


def fraction_text(value: Fraction) -> str:
    """Canonical positive-denominator display, using integers where possible."""
    return str(value.numerator) if value.denominator == 1 else f"{value.numerator}/{value.denominator}"


def parse_answer(text: str) -> ParsedAnswer:
    if not isinstance(text, str):
        raise MathInputError("invalid_number")
    if len(text) > MAX_ANSWER_CHARS:
        raise MathInputError("answer_too_long")
    match = _ANSWER.fullmatch(text)
    if match is None:
        raise MathInputError("invalid_number")
    numerator = int(match[2]) * (-1 if match[1] == "-" else 1)
    denominator = (int(match[4]) * (-1 if match[3] == "-" else 1)) if match[4] is not None else 1
    if abs(numerator) > MAX_NUMBER or abs(denominator) > MAX_NUMBER:
        raise MathInputError("number_out_of_range")
    if denominator == 0:
        raise MathInputError("zero_denominator")
    value = Fraction(numerator, denominator)
    return ParsedAnswer(
        value=value,
        numerator=numerator,
        denominator=denominator,
        is_simplest=denominator > 0 and gcd(abs(numerator), denominator) == 1,
        normalized=fraction_text(value),
    )
