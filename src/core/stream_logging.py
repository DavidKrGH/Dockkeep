"""Helpers for bounded, line-oriented logging of streamed subprocess output.

These keep operational subprocess output out of unbounded in-memory buffers:

- :class:`LineLogBuffer` decodes byte chunks incrementally and emits only
  complete text lines, so live stdout logging never holds the whole stream.
- :class:`ByteTailBuffer` keeps only the last ``max_bytes`` bytes, used for
  return values and error messages where a bounded tail is enough.
"""

from __future__ import annotations

import codecs
from collections import deque
from collections.abc import Callable


class LineLogBuffer:
    """Decode chunked bytes and emit complete text lines to a callback.

    Chunks may split mid-line or mid-multibyte-character. An incomplete trailing
    line is buffered until the next chunk completes it or :meth:`flush` is
    called. Memory stays bounded by the longest single line, never the whole
    stream.

    Attributes:
        _emit: Callback invoked once per complete line (without trailing
            newline).
    """

    def __init__(self, emit: Callable[[str], None]) -> None:
        """Initialize the buffer.

        Args:
            emit: Callback invoked with each complete decoded line.
        """
        self._emit = emit
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._pending = ""

    def feed(self, chunk: bytes) -> None:
        """Decode a chunk and emit any complete lines it completes.

        Args:
            chunk: Raw bytes read from the stream.
        """
        text = self._decoder.decode(chunk)
        if not text:
            return
        self._pending += text
        if "\n" not in self._pending:
            return
        lines = self._pending.split("\n")
        self._pending = lines.pop()
        for line in lines:
            self._emit(line.rstrip("\r"))

    def flush(self) -> None:
        """Emit any buffered trailing line that was not newline-terminated."""
        self._pending += self._decoder.decode(b"", final=True)
        if self._pending:
            self._emit(self._pending.rstrip("\r"))
            self._pending = ""


class ByteTailBuffer:
    """Keep only the last ``max_bytes`` bytes fed into it.

    Bounds memory for output a caller still wants a tail of (return values,
    error messages) without retaining the full stream. Decoding happens once at
    the end with ``errors="replace"`` so a tail cut mid-character is tolerated.

    Attributes:
        truncated: Whether any bytes were dropped to honour the cap.
    """

    def __init__(self, max_bytes: int) -> None:
        """Initialize the tail buffer.

        Args:
            max_bytes: Maximum number of trailing bytes to retain.
        """
        self._max_bytes = max(0, max_bytes)
        self._chunks: deque[bytes] = deque()
        self._size = 0
        self._truncated = False

    def feed(self, chunk: bytes) -> None:
        """Append a chunk and drop the oldest bytes beyond the cap.

        Args:
            chunk: Raw bytes read from the stream.
        """
        if not chunk:
            return
        self._chunks.append(chunk)
        self._size += len(chunk)
        while self._size > self._max_bytes and self._chunks:
            overflow = self._size - self._max_bytes
            oldest = self._chunks[0]
            if len(oldest) <= overflow:
                self._chunks.popleft()
                self._size -= len(oldest)
            else:
                self._chunks[0] = oldest[overflow:]
                self._size -= overflow
            self._truncated = True

    @property
    def truncated(self) -> bool:
        return self._truncated

    def decode(self) -> str:
        return b"".join(self._chunks).decode("utf-8", errors="replace")
