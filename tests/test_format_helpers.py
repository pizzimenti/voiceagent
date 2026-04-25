from voiceagent.downloaders import format_bytes, format_transfer_rate


def test_format_bytes_zero():
    assert format_bytes(0) == "0 B"


def test_format_bytes_negative_clamped_to_zero():
    assert format_bytes(-1) == "0 B"


def test_format_bytes_small_integer_bytes():
    assert format_bytes(512) == "512 B"


def test_format_bytes_just_under_kib_threshold():
    assert format_bytes(1023) == "1023 B"


def test_format_bytes_kib_threshold():
    assert format_bytes(1024) == "1.0 KiB"


def test_format_bytes_kib_fractional():
    assert format_bytes(1536) == "1.5 KiB"


def test_format_bytes_mib():
    assert format_bytes(1024 * 1024) == "1.0 MiB"


def test_format_bytes_gib():
    assert format_bytes(1024 ** 3) == "1.0 GiB"


def test_format_bytes_tib():
    assert format_bytes(1024 ** 4) == "1.0 TiB"


def test_format_bytes_above_tib_stays_in_tib():
    # The largest unit caps the scaling: 1 PiB still reports as TiB.
    assert format_bytes(1024 ** 5) == "1024.0 TiB"


def test_format_bytes_accepts_float_input():
    assert format_bytes(2048.0) == "2.0 KiB"


def test_format_transfer_rate_zero():
    assert format_transfer_rate(0) == "0 B/s"


def test_format_transfer_rate_kib():
    assert format_transfer_rate(2048) == "2.0 KiB/s"


def test_format_transfer_rate_mib():
    assert format_transfer_rate(5 * 1024 * 1024) == "5.0 MiB/s"
