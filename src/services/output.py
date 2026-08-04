"""Bounded output for operational command responses."""


def limit_output(
    text: str,
    max_lines: int = 200,
    max_bytes: int = 65536,
) -> tuple[str, bool]:
    """Keep the last lines and bytes of output with UTF-8-safe truncation."""
    truncated = False

    lines = text.splitlines()
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
        text = "\n".join(lines)
        truncated = True

    encoded = text.encode("utf-8")
    if len(encoded) > max_bytes:
        encoded = encoded[-max_bytes:]
        while encoded:
            try:
                text = encoded.decode("utf-8")
                break
            except UnicodeDecodeError:
                encoded = encoded[1:]
        else:
            text = ""
        truncated = True

    return text, truncated
