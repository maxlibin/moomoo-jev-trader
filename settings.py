"""Range-checked parsing of ``.env`` overrides onto frozen settings dataclasses.

Each parser turns one variable's text into a value inside its safe range and
raises ``ValueError`` describing the range otherwise; ``overrides_from_env``
applies every variable a mapping defines and names the variable and the
rejected value in the error, so a bad ``.env`` refuses to start instead of
trading on a misread number.
"""

import math
from dataclasses import replace
from typing import Callable, Mapping, TypeVar, Union

Settings = TypeVar("Settings")
Parser = Callable[[str], Union[int, float, str]]
EnvFields = Mapping[str, tuple[str, Parser]]


def fraction(text: str) -> float:
    """A share of the account in (0, 1]: 0.01 is one percent, 1 is all of it."""
    value = float(text)
    if not (math.isfinite(value) and 0 < value <= 1):
        raise ValueError("must be above 0 and at most 1 (0.01 is one percent)")
    return value


def probability(text: str) -> float:
    value = float(text)
    if not (math.isfinite(value) and 0 <= value <= 1):
        raise ValueError("must be between 0 and 1")
    return value


def positive(text: str) -> float:
    value = float(text)
    if not (math.isfinite(value) and value > 0):
        raise ValueError("must be above 0")
    return value


def non_negative(text: str) -> float:
    value = float(text)
    if not (math.isfinite(value) and value >= 0):
        raise ValueError("must be 0 or more")
    return value


def count(text: str) -> int:
    value = int(text)
    if value < 1:
        raise ValueError("must be a whole number of at least 1")
    return value


def name(text: str) -> str:
    value = text.strip()
    if not value:
        raise ValueError("must not be blank")
    return value


def overrides_from_env(env: Mapping[str, str], fields: EnvFields, base: Settings) -> Settings:
    """``base`` with every variable of ``fields`` that ``env`` defines parsed onto its dataclass field."""
    overrides = {}
    for variable, (field, parse) in fields.items():
        if variable not in env:
            continue
        try:
            overrides[field] = parse(env[variable])
        except ValueError as exc:
            raise ValueError(f"{variable}={env[variable]!r} is not a valid setting: {exc}") from exc
    return replace(base, **overrides)
