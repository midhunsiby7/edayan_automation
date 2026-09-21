import pytest
from src.normalization.date_normalizer import DateNormalizer
from src.normalization.text_normalizer import TextNormalizer


def test_explicit_date_parsing():
    normalizer = DateNormalizer()
    res, inherited, err = normalizer.normalize("01/04/2024")
    assert res == "2024-04-01"
    assert inherited is False
    assert err is None
    assert normalizer.last_resolved_date == "2024-04-01"


def test_ditto_mark_inheritance():
    normalizer = DateNormalizer()
    # Row 1: Explicit date
    r1, inh1, _ = normalizer.normalize("02/04/2024")
    assert r1 == "2024-04-02"
    assert inh1 is False

    # Row 2: Ditto mark '"'
    r2, inh2, _ = normalizer.normalize('"')
    assert r2 == "2024-04-02"
    assert inh2 is True

    # Row 3: Continuation mark 'do'
    r3, inh3, _ = normalizer.normalize("do")
    assert r3 == "2024-04-02"
    assert inh3 is True


def test_dash_in_date_vs_voucher():
    """Verify requirement 2: Date continuation is strictly scoped to date field.
    '-' in voucher number must NOT be treated as a ditto mark.
    """
    normalizer = DateNormalizer()
    normalizer.normalize("05/04/2024")

    # In date field, '-' represents continuation
    date_res, is_inh, _ = normalizer.normalize("-")
    assert date_res == "2024-04-05"
    assert is_inh is True

    # In voucher field, '-' must be normalized as None (no voucher), NOT inheriting date
    voucher_val, v_err = TextNormalizer.normalize_voucher_number("-")
    assert voucher_val is None
    assert v_err is None


def test_cross_page_date_persistence():
    """Verify requirement 3: Date inheritance state persists across pages within one job."""
    normalizer = DateNormalizer()

    # Simulate Page 1 last row
    p1_date, _, _ = normalizer.normalize("15/04/2024")
    assert p1_date == "2024-04-15"

    # Simulate Page 2 first row starting with ditto mark
    p2_date, is_inh, _ = normalizer.normalize('"')
    assert p2_date == "2024-04-15"
    assert is_inh is True

    # Resetting job state clears inheritance
    normalizer.reset_job_state()
    p3_date, _, err = normalizer.normalize('"')
    assert p3_date is None
    assert "no prior resolved date" in err
