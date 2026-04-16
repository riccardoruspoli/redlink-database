from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import Enum, auto

MYSQL_ESCAPE_MAP = {
    "0": "\\0",
    "b": "\\b",
    "n": "\\n",
    "r": "\\r",
    "t": "\\t",
    "Z": "\\x1a",
    "\\": "\\",
    "'": "'",
    '"': '"',
}


class _InsertRowParseMode(Enum):
    OUTSIDE_TUPLE = auto()
    IN_FIELD = auto()
    IN_STRING = auto()


@dataclass
class _InsertRowParser:
    mode: _InsertRowParseMode = _InsertRowParseMode.OUTSIDE_TUPLE
    escape: bool = False
    field_chars: list[str] = field(default_factory=list)
    row: list[str | None] = field(default_factory=list)
    quoted_field: bool = False

    def start_tuple(self) -> None:
        self.mode = _InsertRowParseMode.IN_FIELD
        self.escape = False
        self.field_chars = []
        self.row = []
        self.quoted_field = False

    def flush_current_field(self) -> None:
        self.row.append(_flush_parsed_field(self.field_chars, self.quoted_field))
        self.field_chars = []
        self.quoted_field = False

    def finish_tuple(self) -> list[str | None]:
        self.flush_current_field()
        row = self.row
        self.mode = _InsertRowParseMode.OUTSIDE_TUPLE
        self.escape = False
        self.field_chars = []
        self.row = []
        self.quoted_field = False
        return row

    def consume_string_character(self, char: str) -> None:
        if self.escape:
            self.field_chars.append("\\")
            self.field_chars.append(char)
            self.escape = False
            return
        if char == "\\":
            self.escape = True
            return
        if char == "'":
            self.mode = _InsertRowParseMode.IN_FIELD
            return
        self.field_chars.append(char)

    def consume_field_character(self, char: str) -> list[str | None] | None:
        if char == "'":
            self.mode = _InsertRowParseMode.IN_STRING
            self.quoted_field = True
            return None
        if char == ",":
            self.flush_current_field()
            return None
        if char == ")":
            return self.finish_tuple()

        self.field_chars.append(char)
        return None

    def consume_character(self, char: str) -> list[str | None] | None:
        if self.mode is _InsertRowParseMode.OUTSIDE_TUPLE:
            if char == "(":
                self.start_tuple()
            return None
        if self.mode is _InsertRowParseMode.IN_STRING:
            self.consume_string_character(char)
            return None
        return self.consume_field_character(char)


def _decode_mysql_string(raw_value: str) -> str:
    result: list[str] = []
    idx = 0
    length = len(raw_value)

    while idx < length:
        char = raw_value[idx]
        if char == "\\" and idx + 1 < length:
            nxt = raw_value[idx + 1]
            result.append(MYSQL_ESCAPE_MAP.get(nxt, nxt))
            idx += 2
            continue
        result.append(char)
        idx += 1

    return "".join(result)


def _flush_parsed_field(field_chars: list[str], quoted_field: bool) -> str | None:
    raw = "".join(field_chars)
    if quoted_field:
        return _decode_mysql_string(raw)
    value = raw.strip()
    if value.upper() == "NULL":
        return None
    return value


def _extract_insert_payload(raw_line: str) -> str | None:
    marker = " VALUES "
    marker_index = raw_line.find(marker)
    if marker_index == -1:
        return None

    payload = raw_line[marker_index + len(marker) :].rstrip()
    if payload.endswith(";"):
        return payload[:-1]
    return payload


def iter_insert_rows(sql_path: str) -> Iterator[list[str | None]]:
    """Yield parsed row values from SQL dump files without loading them into memory."""

    with open(
        sql_path, encoding="utf-8", errors="replace", buffering=1024 * 1024
    ) as handle:
        for raw_line in handle:
            if not raw_line.startswith("INSERT INTO "):
                continue

            payload = _extract_insert_payload(raw_line)
            if payload is None:
                continue

            parser = _InsertRowParser()

            # This state machine stays deliberately local and streaming-oriented:
            # it only understands the subset of MySQL dump syntax emitted in the
            # Wikimedia SQL files we process.
            for char in payload:
                parsed_row = parser.consume_character(char)
                if parsed_row is not None:
                    yield parsed_row
