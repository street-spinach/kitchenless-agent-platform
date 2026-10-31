"""Money in integer minor units (architecture item 6). Mixing currencies raises."""

import functools
from dataclasses import dataclass
from typing import Self

# ISO 4217 minor-unit exponents for the currencies the launch regions use; others default to 2.
_EXPONENT = {"JPY": 0, "KRW": 0}
_SYMBOL = {"USD": "$", "LKR": "Rs ", "EUR": "€", "GBP": "£"}


class CurrencyMismatch(ValueError):
    """Raised when an operation combines amounts in different currencies."""


@functools.total_ordering
@dataclass(frozen=True)
class Money:
    amount_minor: int
    currency: str

    def __post_init__(self) -> None:
        if not isinstance(self.amount_minor, int) or isinstance(self.amount_minor, bool):
            raise TypeError("amount_minor must be an int")
        if len(self.currency) != 3 or not self.currency.isalpha() or not self.currency.isupper():
            raise ValueError(f"currency must be an ISO 4217 code, got {self.currency!r}")

    def _check(self, other: object) -> "Money":
        if not isinstance(other, Money):
            raise TypeError(f"expected Money, got {type(other).__name__}")
        if other.currency != self.currency:
            raise CurrencyMismatch(f"{self.currency} and {other.currency}")
        return other

    def __add__(self, other: "Money") -> Self:
        return type(self)(self.amount_minor + self._check(other).amount_minor, self.currency)

    def __sub__(self, other: "Money") -> Self:
        return type(self)(self.amount_minor - self._check(other).amount_minor, self.currency)

    def __lt__(self, other: "Money") -> bool:
        return self.amount_minor < self._check(other).amount_minor

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        return self.amount_minor == self._check(other).amount_minor

    def __hash__(self) -> int:
        return hash((self.amount_minor, self.currency))


def format(money: Money, locale: str) -> str:
    """Plain text for WhatsApp, e.g. `$1,234.50` or `Rs 1,234.50`.

    Locales starting with `de`, `fr`, `es`, `it` or `pt` swap the separators.
    """
    exp = _EXPONENT.get(money.currency, 2)
    sign = "-" if money.amount_minor < 0 else ""
    major, minor = divmod(abs(money.amount_minor), 10**exp)
    text = f"{major:,}" + (f".{minor:0{exp}d}" if exp else "")
    if locale.split("_")[0].split("-")[0].lower() in {"de", "fr", "es", "it", "pt"}:
        text = text.translate(str.maketrans(",.", ".,"))
    symbol = _SYMBOL.get(money.currency)
    return f"{sign}{symbol}{text}" if symbol else f"{sign}{text} {money.currency}"
