"""Tests for the bounded stream-logging helpers."""

from __future__ import annotations

from src.core.stream_logging import ByteTailBuffer, LineLogBuffer


class TestLineLogBuffer:
    def test_emits_complete_lines_and_buffers_partial_line(self) -> None:
        lines: list[str] = []
        buffer = LineLogBuffer(lines.append)

        buffer.feed(b"hello\nwor")
        assert lines == ["hello"]

        buffer.feed(b"ld\n")
        assert lines == ["hello", "world"]

    def test_reassembles_line_split_across_chunks(self) -> None:
        lines: list[str] = []
        buffer = LineLogBuffer(lines.append)

        buffer.feed(b"par")
        buffer.feed(b"tial ")
        buffer.feed(b"line\n")

        assert lines == ["partial line"]

    def test_flush_emits_trailing_line_without_newline(self) -> None:
        lines: list[str] = []
        buffer = LineLogBuffer(lines.append)

        buffer.feed(b"no-newline-here")
        assert lines == []

        buffer.flush()
        assert lines == ["no-newline-here"]

    def test_flush_without_pending_emits_nothing(self) -> None:
        lines: list[str] = []
        buffer = LineLogBuffer(lines.append)

        buffer.feed(b"done\n")
        buffer.flush()

        assert lines == ["done"]

    def test_strips_carriage_return_from_crlf(self) -> None:
        lines: list[str] = []
        buffer = LineLogBuffer(lines.append)

        buffer.feed(b"windows\r\nline\r\n")

        assert lines == ["windows", "line"]

    def test_decodes_multibyte_character_split_across_chunks(self) -> None:
        lines: list[str] = []
        buffer = LineLogBuffer(lines.append)

        encoded = "grüße\n".encode("utf-8")
        split = encoded.index(b"\xc3") + 1
        buffer.feed(encoded[:split])
        buffer.feed(encoded[split:])

        assert lines == ["grüße"]


class TestByteTailBuffer:
    def test_keeps_full_content_under_cap(self) -> None:
        buffer = ByteTailBuffer(1024)
        buffer.feed(b"hello ")
        buffer.feed(b"world")

        assert buffer.decode() == "hello world"
        assert buffer.truncated is False

    def test_drops_oldest_bytes_beyond_cap(self) -> None:
        buffer = ByteTailBuffer(4)
        buffer.feed(b"abcdef")

        assert buffer.decode() == "cdef"
        assert buffer.truncated is True

    def test_trims_partial_oldest_chunk(self) -> None:
        buffer = ByteTailBuffer(3)
        buffer.feed(b"ab")
        buffer.feed(b"cde")

        assert buffer.decode() == "cde"
        assert buffer.truncated is True

    def test_ignores_empty_chunks(self) -> None:
        buffer = ByteTailBuffer(4)
        buffer.feed(b"")

        assert buffer.decode() == ""
        assert buffer.truncated is False

    def test_tolerates_tail_cut_mid_multibyte_character(self) -> None:
        buffer = ByteTailBuffer(3)
        buffer.feed("äöü".encode("utf-8"))

        decoded = buffer.decode()
        assert buffer.truncated is True
        assert decoded.endswith("ü")
