from decimal import Decimal

import pytest

from app.core.money import Money


def test_from_dollars_str_avoids_float_error():
    assert Money.from_dollars_str("15.00").cents == 1500
    assert Money.from_dollars_str("0.25").cents == 25


def test_arithmetic():
    assert (Money(1500) - Money(400)) == Money(1100)
    assert (Money(100) + Money(-40)) == Money(60)
    assert -Money(500) == Money(-500)


def test_fraction_floors_down():
    # 20% of $15.00 (1500 cents) is exactly 300.
    assert Money(1500).fraction(Decimal("0.20")) == Money(300)
    # 20% of $6.03 (603 cents) is 120.6 -> floors to 120, never rounds up.
    assert Money(603).fraction(Decimal("0.20")) == Money(120)


def test_rejects_non_int_cents():
    with pytest.raises(TypeError):
        Money(15.0)  # type: ignore[arg-type]


def test_ordering_and_min():
    assert Money(100) < Money(200)
    assert Money(300).min(Money(200)) == Money(200)


def test_floor_to_increment():
    assert Money(603).floor_to_increment(Money(25)) == Money(600)
    assert Money(624).floor_to_increment(Money(25)) == Money(600)
    assert Money(625).floor_to_increment(Money(25)) == Money(625)
    assert Money(18).floor_to_increment(Money(25)) == Money(0)
    with pytest.raises(ValueError):
        Money(100).floor_to_increment(Money(0))


def test_as_dollars_str():
    assert Money(1500).as_dollars_str() == "$15.00"
    assert Money(-25).as_dollars_str() == "-$0.25"
