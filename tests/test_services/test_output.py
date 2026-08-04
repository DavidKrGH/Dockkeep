from src.services.output import limit_output


def test_limit_output_keeps_last_lines() -> None:
    text = "\n".join(["old"] * 3 + ["value super-secret"] * 3)

    output, truncated = limit_output(text, max_lines=2, max_bytes=1024)

    assert truncated is True
    assert "super-secret" in output
    assert len(output.splitlines()) == 2


def test_limit_output_keeps_last_bytes() -> None:
    output, truncated = limit_output("abcdef", max_lines=200, max_bytes=3)

    assert truncated is True
    assert output == "def"


def test_limit_output_truncates_utf8_safely() -> None:
    output, truncated = limit_output("abäöü", max_lines=200, max_bytes=5)

    assert truncated is True
    assert output == "öü"


def test_limit_output_returns_unmodified_text_when_within_limits() -> None:
    output, truncated = limit_output("line 1\nline 2", max_lines=2, max_bytes=100)

    assert truncated is False
    assert output == "line 1\nline 2"
