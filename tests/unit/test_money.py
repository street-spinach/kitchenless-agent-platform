import pytest

from services.common import money
from services.common.money import CurrencyMismatch, Money


def test_mixed_currency_raises() -> None:
    usd = Money(100, "USD")
    lkr = Money(100, "LKR")
    with pytest.raises(CurrencyMismatch):
        usd + lkr
    with pytest.raises(CurrencyMismatch):
        usd - lkr
    with pytest.raises(CurrencyMismatch):
        _ = usd < lkr
    with pytest.raises(CurrencyMismatch):
        _ = usd == lkr


def test_minor_unit_arithmetic() -> None:
    a = Money(1999, "USD")
    b = Money(1, "USD")
    assert a + b == Money(2000, "USD")
    assert b - a == Money(-1998, "USD")
    assert (a + b).amount_minor == 2000
    assert b < a and a > b and a >= Money(1999, "USD")
    assert sum([b, b, b], Money(0, "USD")) == Money(3, "USD")
    with pytest.raises(TypeError):
        Money(19.99, "USD")  # type: ignore[arg-type]
    assert money.format(Money(123450, "USD"), "en_US") == "$1,234.50"
    assert money.format(Money(-5, "LKR"), "en_LK") == "-Rs 0.05"
