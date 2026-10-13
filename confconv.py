#!/usr/bin/env python3
"""confconv - convert configuration files between JSON, YAML, TOML, CSV, INI, XML
and SQL.

Formats are detected from the file extensions, the data is validated before
anything is written, and the result is summarised with rich.

    confconv.py settings.yaml settings.toml
    confconv.py settings.yaml --to json
    confconv.py validate settings.yaml
    confconv.py formats

See README.md for the mapping rules between nested and tabular formats.
"""

from __future__ import annotations

import configparser
import csv
import io
import json
import os
import re
import sqlite3
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import date, datetime, time
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import click
import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    ValidationError,
    field_validator,
    model_validator,
)
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

try:  # tomllib is in the standard library from Python 3.11
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.9 / 3.10
    import tomli as tomllib

import tomli_w

__version__ = "1.1.0"

console = Console()
err_console = Console(stderr=True)

# Preview limits: wide or long data is trimmed so the table stays readable.
MAX_PREVIEW_COLUMNS = 10
MAX_CELL_WIDTH = 48


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
# Every expected failure is a ConfconvError carrying the process exit code:
#   1 = the data is wrong (syntax, schema, not representable in the target)
#   2 = the command line is wrong (unknown format)
#   3 = the filesystem is in the way (missing file, would overwrite, ...)


class ConfconvError(Exception):
    exit_code = 1
    title = "Error"


class ParseError(ConfconvError):
    """The source file is not syntactically valid for its format."""

    title = "Syntax error"

    def __init__(
        self,
        message: str,
        path: Path,
        line: Optional[int] = None,
        column: Optional[int] = None,
        source_text: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.path = path
        self.line = line
        self.column = column
        self.source_text = source_text


class SchemaError(ConfconvError):
    """The file parsed, but its structure failed Pydantic validation."""

    title = "Validation error"

    def __init__(self, subject: str, problems: List[str]) -> None:
        super().__init__(subject)
        self.subject = subject
        self.problems = problems


class ConversionError(ConfconvError):
    """The data is valid but cannot be expressed in the target format."""

    title = "Conversion error"


class FormatError(ConfconvError):
    exit_code = 2
    title = "Unknown format"


class FileProblem(ConfconvError):
    exit_code = 3
    title = "File error"


# ---------------------------------------------------------------------------
# Format registry
# ---------------------------------------------------------------------------


class Format(str, Enum):
    JSON = "json"
    YAML = "yaml"
    TOML = "toml"
    CSV = "csv"
    INI = "ini"
    XML = "xml"
    SQL = "sql"


EXTENSIONS: Dict[str, Format] = {
    ".json": Format.JSON,
    ".yaml": Format.YAML,
    ".yml": Format.YAML,
    ".toml": Format.TOML,
    ".csv": Format.CSV,
    ".ini": Format.INI,
    ".cfg": Format.INI,
    ".xml": Format.XML,
    ".sql": Format.SQL,
}


def detect_format(path: Path, override: Optional[str], option: str) -> Format:
    """Return the format for *path*: the explicit override, else its extension."""
    if override:
        return Format(override.lower())
    try:
        return EXTENSIONS[path.suffix.lower()]
    except KeyError:
        known = ", ".join(sorted(EXTENSIONS))
        shown = path.suffix or "(no extension)"
        raise FormatError(
            f"Cannot tell the format of '{path}' from its extension {shown}.\n"
            f"Supported extensions: {known}\n"
            f"Use {option} to set the format explicitly."
        ) from None


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

_SCALARS = (str, int, float, bool, type(None), datetime, date, time)

# Element and attribute names confconv will write (no namespaces).
_XML_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.\-]*")


def _structure_problems(value: Any, where: str, problems: List[str]) -> None:
    """Walk a parsed document and record everything a config may not contain."""
    if len(problems) >= 20:  # enough to act on; don't flood the terminal
        return
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                problems.append(
                    f"{where or '<root>'}: key {key!r} is {type(key).__name__}, "
                    "keys must be strings"
                )
                continue
            if not key:
                problems.append(f"{where or '<root>'}: empty key")
                continue
            _structure_problems(child, f"{where}.{key}" if where else key, problems)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _structure_problems(child, f"{where}[{index}]", problems)
    elif not isinstance(value, _SCALARS):
        problems.append(f"{where}: unsupported value type {type(value).__name__}")


class ConfigDocument(RootModel[Any]):
    """The basic schema every source file has to satisfy.

    A document is either one mapping (a config) or a list of mappings (records).
    Keys are non-empty strings; values are strings, numbers, booleans, nulls,
    dates/times, or lists and mappings of those.
    """

    @field_validator("root")
    @classmethod
    def _check_structure(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("the document is empty")
        if isinstance(value, list):
            bad = [i for i, item in enumerate(value) if not isinstance(item, dict)]
            if bad:
                shown = ", ".join(str(i) for i in bad[:5]) + (" ..." if len(bad) > 5 else "")
                raise ValueError(
                    "a top-level list must contain only mappings (records); "
                    f"item(s) at index {shown} are not"
                )
        elif not isinstance(value, dict):
            raise ValueError(
                "the top level must be a mapping or a list of mappings, "
                f"found {type(value).__name__}"
            )
        problems: List[str] = []
        _structure_problems(value, "", problems)
        if problems:
            raise ValueError("\n".join(problems))
        return value


class TabularDocument(BaseModel):
    """A CSV-shaped table: named columns and rows of text cells."""

    columns: List[str]
    rows: List[Dict[str, str]]

    @field_validator("columns")
    @classmethod
    def _check_columns(cls, columns: List[str]) -> List[str]:
        if not columns:
            raise ValueError("there are no columns")
        blank = [str(i + 1) for i, name in enumerate(columns) if not name.strip()]
        if blank:
            raise ValueError(f"column(s) {', '.join(blank)} have an empty name")
        seen, duplicates = set(), []
        for name in columns:
            if name in seen and name not in duplicates:
                duplicates.append(name)
            seen.add(name)
        if duplicates:
            raise ValueError(f"duplicate column name(s): {', '.join(duplicates)}")
        return columns

    @model_validator(mode="after")
    def _check_rows(self) -> "TabularDocument":
        known = set(self.columns)
        for number, row in enumerate(self.rows, start=1):
            extra = set(row) - known
            if extra:
                raise ValueError(f"row {number} has unknown column(s): {', '.join(sorted(extra))}")
        return self


class ConversionJob(BaseModel):
    """One validated conversion request, built from the command line."""

    model_config = ConfigDict(frozen=True)

    source: Path
    target: Path
    source_format: Format
    target_format: Format
    force: bool = False
    dry_run: bool = False
    indent: int = Field(default=2, ge=0, le=8)
    root_key: str = Field(default="items", min_length=1)
    xml_root: str = "config"
    infer_types: bool = True

    @field_validator("xml_root")
    @classmethod
    def _check_xml_root(cls, name: str) -> str:
        if not _XML_NAME.fullmatch(name):
            raise ValueError(f"{name!r} is not a valid XML element name")
        return name

    @model_validator(mode="after")
    def _check_paths(self) -> "ConversionJob":
        # FileProblem is not a ValueError, so Pydantic lets it propagate as-is
        # and the CLI can report it with the I/O exit code.
        if not self.source.exists():
            raise FileProblem(f"Source file not found: {self.source}")
        if not self.source.is_file():
            raise FileProblem(f"Source is not a file: {self.source}")
        if self.source.resolve() == self.target.resolve():
            raise FileProblem("Source and target are the same file.")
        if self.target.is_dir():
            raise FileProblem(f"Target is a directory: {self.target}")
        if not self.target.resolve().parent.is_dir():
            raise FileProblem(f"Target directory does not exist: {self.target.resolve().parent}")
        if self.target.exists() and not (self.force or self.dry_run):
            raise FileProblem(f"Target already exists: {self.target}\nUse --force to overwrite it.")
        return self


def _as_schema_error(exc: ValidationError, subject: str) -> SchemaError:
    """Flatten a Pydantic ValidationError into readable one-line problems."""
    problems: List[str] = []
    for error in exc.errors():
        message = error["msg"]
        if message.startswith("Value error, "):
            message = message[len("Value error, "):]
        location = ".".join(str(part) for part in error["loc"])
        for line in message.split("\n"):
            problems.append(f"{location}: {line}" if location else line)
    return SchemaError(subject, problems)


# ---------------------------------------------------------------------------
# Readers - each returns plain Python data or raises ParseError
# ---------------------------------------------------------------------------


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise FileProblem(f"Cannot read {path}: {exc.strerror or exc}") from exc


def _decode(raw: bytes, path: Path) -> str:
    try:
        return raw.decode("utf-8-sig")  # accepts and strips a UTF-8 BOM
    except UnicodeDecodeError as exc:
        raise ParseError(f"File is not valid UTF-8 (byte {exc.start}).", path) from exc


def read_json(text: str, path: Path) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ParseError(exc.msg, path, exc.lineno, exc.colno, text) from exc


def read_yaml(text: str, path: Path) -> Any:
    try:
        return yaml.safe_load(text)  # safe_load: never constructs arbitrary objects
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        message = getattr(exc, "problem", None) or str(exc)
        context = getattr(exc, "context", None)
        if context:
            message = f"{message} ({context})"
        if mark is not None:
            raise ParseError(message, path, mark.line + 1, mark.column + 1, text) from exc
        raise ParseError(message, path) from exc


_TOML_POSITION = re.compile(r"\s*\(at line (\d+), column (\d+)\)$")


def read_toml(text: str, path: Path) -> Any:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        # The position is only available inside the message text.
        message = str(exc)
        match = _TOML_POSITION.search(message)
        if match:
            line, column = int(match.group(1)), int(match.group(2))
            raise ParseError(message[: match.start()], path, line, column, text) from exc
        raise ParseError(message, path) from exc


_INT = re.compile(r"[+-]?\d+")
# A float needs a decimal point, or an explicitly signed exponent ("1e-07"),
# so identifiers such as "1e5" are left alone.
_FLOAT = re.compile(r"[+-]?(\d+\.\d*([eE][+-]?\d+)?|\.\d+([eE][+-]?\d+)?|\d+[eE][+-]\d+)")


def infer_cell(cell: str, parse_json: bool = True) -> Any:
    """Turn text from a typeless format (CSV, INI, XML) back into the value it
    most plausibly represents. *parse_json* also restores lists stored as JSON.
    """
    stripped = cell.strip()
    if stripped == "":
        return None
    lowered = stripped.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    if _INT.fullmatch(stripped):
        digits = stripped.lstrip("+-")
        if len(digits) > 1 and digits.startswith("0"):
            return cell  # "007", zip codes: leading zeros mean it is an identifier
        return int(stripped)
    if _FLOAT.fullmatch(stripped):
        return float(stripped)
    if parse_json and stripped[0] in "[{":
        try:
            parsed = json.loads(stripped)
        except ValueError:
            return cell
        if isinstance(parsed, (list, dict)):
            return parsed
    return cell


def unflatten(flat: Dict[str, Any]) -> Dict[str, Any]:
    """Rebuild nesting from dotted keys: {"db.host": "x"} -> {"db": {"host": "x"}}.

    Raises KeyError when the keys cannot form a tree (e.g. both "a" and "a.b").
    """
    nested: Dict[str, Any] = {}
    for dotted, value in flat.items():
        parts = dotted.split(".")
        if "" in parts:
            raise KeyError(dotted)
        node = nested
        for part in parts[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                raise KeyError(dotted)
        if parts[-1] in node:
            raise KeyError(dotted)
        node[parts[-1]] = value
    return nested


def read_csv(text: str, path: Path, infer_types: bool) -> Tuple[Any, List[str]]:
    """Parse CSV into a list of records. Returns (records, notes)."""
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    header: Optional[List[str]] = None
    rows: List[Dict[str, str]] = []
    try:
        for fields in reader:
            if not fields or all(field.strip() == "" for field in fields):
                continue  # blank line
            if header is None:
                header = [name.strip() for name in fields]
                continue
            if len(fields) != len(header):
                raise ParseError(
                    f"Row has {len(fields)} field(s) but the header has {len(header)}.",
                    path,
                    reader.line_num,
                    source_text=text,
                )
            rows.append(dict(zip(header, fields)))
    except csv.Error as exc:
        raise ParseError(str(exc), path, reader.line_num, source_text=text) from exc
    if header is None:
        raise ParseError("The file has no header row.", path)

    try:
        table = TabularDocument(columns=header, rows=rows)
    except ValidationError as exc:
        raise _as_schema_error(exc, f"{path} is not a valid table") from exc

    notes: List[str] = []
    records: List[Dict[str, Any]] = [
        {name: infer_cell(cell) if infer_types else cell for name, cell in row.items()}
        for row in table.rows
    ]
    return _nest_dotted_columns(records, table.columns, notes), notes


def _nest_dotted_columns(
    records: List[Dict[str, Any]], columns: List[str], notes: List[str]
) -> List[Dict[str, Any]]:
    """Turn dotted column names back into nesting, or keep them flat if they clash."""
    if not any("." in name for name in columns):
        return records
    try:
        return [unflatten(record) for record in records]
    except KeyError as exc:
        notes.append(
            f"Column {exc.args[0]!r} conflicts with another column, "
            "so dotted column names were kept flat instead of being nested."
        )
        return records


# What a .sql file may do while it is being loaded: build tables and fill them.
# Everything else (ATTACH, PRAGMA, extensions, ...) is refused by the authorizer,
# so reading a file can never touch anything outside the in-memory database.
_SQL_ALLOWED = {
    sqlite3.SQLITE_CREATE_TABLE,
    sqlite3.SQLITE_CREATE_INDEX,
    sqlite3.SQLITE_INSERT,
    sqlite3.SQLITE_UPDATE,
    sqlite3.SQLITE_DELETE,
    sqlite3.SQLITE_DROP_TABLE,
    sqlite3.SQLITE_DROP_INDEX,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_FUNCTION,
    sqlite3.SQLITE_TRANSACTION,
}


def _sql_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def read_sql(text: str, path: Path, infer_types: bool) -> Tuple[Any, List[str]]:
    """Run CREATE TABLE / INSERT statements in a throwaway in-memory SQLite
    database and return its contents as {table: [records]}.
    """
    trusted = [False]  # flipped on for confconv's own queries after loading

    def authorizer(action: int, *_: Any) -> int:
        return sqlite3.SQLITE_OK if trusted[0] or action in _SQL_ALLOWED else sqlite3.SQLITE_DENY

    connection = sqlite3.connect(":memory:", isolation_level=None)
    try:
        connection.set_authorizer(authorizer)

        def execute(statement: str, line: int) -> None:
            if statement.lstrip().upper().startswith("PRAGMA"):
                return  # harmless header of SQLite dumps
            try:
                connection.execute(statement)
            except (sqlite3.Error, sqlite3.Warning) as exc:
                message = str(exc)
                if "not authorized" in message or "prohibited" in message:
                    message = "This kind of statement is not supported; only CREATE TABLE and INSERT are read."
                raise ParseError(message, path, line, source_text=text) from exc

        # Feed one statement at a time so an error can be tied to its line.
        # complete_statement() knows about quotes and comments, so a ";" inside
        # a string does not end the statement.
        buffer, start = "", 1
        for number, line in enumerate(text.splitlines(keepends=True), start=1):
            pieces = line.split(";")
            for index, piece in enumerate(pieces):
                if not buffer.strip():
                    start = number
                ends_with_semicolon = index < len(pieces) - 1
                buffer += piece + (";" if ends_with_semicolon else "")
                if ends_with_semicolon and sqlite3.complete_statement(buffer):
                    execute(buffer, start)
                    buffer = ""
        if buffer.strip():
            execute(buffer, start)

        trusted[0] = True
        notes: List[str] = []
        tables: Dict[str, Any] = {}
        names = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY rowid"
            )
        ]
        for name in names:
            quoted = _sql_identifier(name)
            booleans = {
                column[1]
                for column in connection.execute(f"PRAGMA table_info({quoted})")
                if "BOOL" in (column[2] or "").upper()
            }
            cursor = connection.execute(f"SELECT * FROM {quoted}")
            columns = [description[0] for description in cursor.description]
            records = []
            for row in cursor:
                record: Dict[str, Any] = {}
                for column, value in zip(columns, row):
                    if column in booleans and value in (0, 1):
                        value = bool(value)
                    elif infer_types and isinstance(value, str) and value[:1] in "[{":
                        value = infer_cell(value)  # lists are stored as JSON text
                    elif isinstance(value, bytes):
                        value = value.hex()
                    record[column] = value
                records.append(record)
            tables[name] = _nest_dotted_columns(records, columns, notes)
    finally:
        connection.close()
    if not tables:
        raise ParseError("The file does not create any tables.", path)
    return tables, notes


def _merge_sections(root: Dict[str, Any], sections: Dict[str, Dict[str, Any]], nest: bool) -> Dict[str, Any]:
    """Place INI sections into the document; dotted names nest when *nest* is set.

    Raises KeyError when a section collides with a key that is already there.
    """
    document = dict(root)
    for name, body in sections.items():
        parts = name.split(".") if nest else [name]
        if "" in parts:
            raise KeyError(name)
        node = document
        for part in parts:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                raise KeyError(name)
        if set(node) & set(body):
            raise KeyError(name)
        node.update(body)
    return document


_INI_ROOT = "\x00root"  # synthetic section collecting keys that precede any [section]


def read_ini(text: str, path: Path, infer_types: bool) -> Tuple[Any, List[str]]:
    """Parse INI into a mapping of sections. Returns (data, notes)."""
    parser = configparser.ConfigParser(
        interpolation=None,  # "%" is an ordinary character
        allow_no_value=True,  # a bare key is a null
        default_section="\x00default",  # [DEFAULT] is an ordinary section
    )
    parser.optionxform = str  # keep the case of keys
    for number, line in enumerate(text.splitlines(), start=1):
        # configparser would quietly read "[server" as a key with no value.
        if line.startswith("[") and "]" not in line:
            raise ParseError("Section header is missing its closing ']'.", path, number, source_text=text)
    try:
        parser.read_string(f"[{_INI_ROOT}]\n{text}")
    except configparser.Error as exc:
        # Line numbers are shifted by the synthetic first line.
        if isinstance(exc, configparser.DuplicateOptionError):
            message, line = f"Key {exc.option!r} appears twice in the same section.", exc.lineno
        elif isinstance(exc, configparser.DuplicateSectionError):
            message, line = f"Section [{exc.section}] appears twice.", exc.lineno
        elif isinstance(exc, configparser.ParsingError) and getattr(exc, "errors", None):
            message, line = "This line is not a [section] header or a key = value pair.", exc.errors[0][0]
        else:
            message, line = str(exc), None
        raise ParseError(message, path, line - 1 if line else None, source_text=text) from exc

    def convert(value: Optional[str]) -> Any:
        if not value or not infer_types:
            return value  # None (bare key) and "" stay as they are
        return infer_cell(value)

    root: Dict[str, Any] = {}
    sections: Dict[str, Dict[str, Any]] = {}
    for name in parser.sections():
        body = {key: convert(value) for key, value in parser.items(name, raw=True)}
        if name == _INI_ROOT:
            root = body
        else:
            sections[name] = body

    notes: List[str] = []
    try:
        return _merge_sections(root, sections, nest=True), notes
    except KeyError as exc:
        notes.append(
            f"Section [{exc.args[0]}] conflicts with another section or key, "
            "so dotted section names were kept flat instead of being nested."
        )
    try:
        return _merge_sections(root, sections, nest=False), notes
    except KeyError as exc:
        raise ParseError(
            f"Section [{exc.args[0]}] has the same name as a key outside any section.", path
        ) from None


class _DoctypeFound(Exception):
    pass


class _SafeTreeBuilder(ET.TreeBuilder):
    """Refuses DTDs: they can declare entities that expand without limit."""

    def doctype(self, name: str, pubid: Optional[str], system: Optional[str]) -> None:
        raise _DoctypeFound()


_XML_POSITION = re.compile(r": line \d+, column \d+$")


def _local_name(name: str, state: Dict[str, bool]) -> str:
    if name.startswith("{"):
        state["namespaces"] = True
        return name.rsplit("}", 1)[1]
    return name


def _element_value(element: ET.Element, infer_types: bool, state: Dict[str, bool]) -> Any:
    """Convert one element: attributes become "@name" keys, child elements become
    keys (repeated tags become a list), and text beside them becomes "#text".
    """

    def convert(text: str) -> Any:
        return infer_cell(text, parse_json=False) if infer_types else text

    result: Dict[str, Any] = {}
    for name, value in element.attrib.items():
        result["@" + _local_name(name, state)] = convert(value)
    repeated = set()
    for child in element:
        key = _local_name(child.tag, state)
        value = _element_value(child, infer_types, state)
        if key not in result:
            result[key] = value
        elif key in repeated:
            result[key].append(value)
        else:
            result[key] = [result[key], value]
            repeated.add(key)
    text = element.text or ""
    if not result:
        return convert(text) if text.strip() else None
    if text.strip():
        result["#text"] = convert(text.strip())
    return result


def read_xml(raw: bytes, path: Path, infer_types: bool) -> Tuple[Any, List[str]]:
    """Parse XML into the contents of its root element. Returns (data, notes)."""
    try:
        snippet: Optional[str] = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        snippet = None  # another encoding; the XML parser handles it itself
    parser = ET.XMLParser(target=_SafeTreeBuilder())
    try:
        parser.feed(raw)
        root = parser.close()
    except _DoctypeFound:
        raise ParseError(
            "DOCTYPE declarations are not supported, because they can define "
            "entities that expand without limit. Remove the <!DOCTYPE ...> block.",
            path,
        ) from None
    except ET.ParseError as exc:
        line, column = exc.position
        message = _XML_POSITION.sub("", str(exc))
        raise ParseError(message, path, line, column + 1, snippet) from exc

    state = {"namespaces": False}
    value = _element_value(root, infer_types, state)
    notes = []
    if state["namespaces"]:
        notes.append("XML namespaces were dropped; only the local element names are kept.")
    if value is None:
        value = {}  # an empty root element is an empty config
    return value, notes


def load_document(path: Path, fmt: Format, infer_types: bool = True) -> Tuple[Any, List[str]]:
    """Read, parse and schema-check *path*. Returns (data, notes)."""
    raw_bytes = _read_bytes(path)
    notes: List[str] = []
    if fmt is Format.XML:
        raw, notes = read_xml(raw_bytes, path, infer_types)
    else:
        text = _decode(raw_bytes, path)
        if fmt is Format.JSON:
            raw = read_json(text, path)
        elif fmt is Format.YAML:
            raw = read_yaml(text, path)
        elif fmt is Format.TOML:
            raw = read_toml(text, path)
        elif fmt is Format.INI:
            raw, notes = read_ini(text, path, infer_types)
        elif fmt is Format.SQL:
            raw, notes = read_sql(text, path, infer_types)
        else:
            raw, notes = read_csv(text, path, infer_types)
    try:
        document = ConfigDocument.model_validate(raw)
    except ValidationError as exc:
        raise _as_schema_error(exc, f"{path} does not have a valid config structure") from exc
    return document.root, notes


# ---------------------------------------------------------------------------
# Shape adapters - make the data fit what the target format can express
# ---------------------------------------------------------------------------


def flatten(mapping: Dict[str, Any], prefix: str = "") -> Dict[str, Any]:
    """Collapse nested mappings into dotted keys. Lists are kept as values."""
    flat: Dict[str, Any] = {}
    for key, value in mapping.items():
        dotted = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict) and value:
            children = flatten(value, dotted)
        else:
            children = {dotted: value}
        for child_key, child_value in children.items():
            if child_key in flat:
                raise ConversionError(
                    f"Two different values flatten to the same column name {child_key!r}."
                )
            flat[child_key] = child_value
    return flat


def _stringify_temporals(value: Any, only_time: bool = False) -> Any:
    """Replace date/time objects with ISO-8601 strings, recursively."""
    if isinstance(value, dict):
        return {key: _stringify_temporals(child, only_time) for key, child in value.items()}
    if isinstance(value, list):
        return [_stringify_temporals(child, only_time) for child in value]
    if isinstance(value, time) or (not only_time and isinstance(value, (datetime, date))):
        return value.isoformat()
    return value


def _strip_nulls(value: Any, dropped: List[str], where: str = "") -> Any:
    """Remove nulls (TOML has none), recording where each one was."""
    if isinstance(value, dict):
        kept = {}
        for key, child in value.items():
            path = f"{where}.{key}" if where else key
            if child is None:
                dropped.append(path)
            else:
                kept[key] = _strip_nulls(child, dropped, path)
        return kept
    if isinstance(value, list):
        kept_items = []
        for index, child in enumerate(value):
            path = f"{where}[{index}]"
            if child is None:
                dropped.append(path)
            else:
                kept_items.append(_strip_nulls(child, dropped, path))
        return kept_items
    return value


def adapt(data: Any, target: Format, root_key: str) -> Tuple[Any, List[str]]:
    """Reshape validated data for the target format. Returns (data, notes)."""
    notes: List[str] = []

    if target is Format.CSV:
        # CSV holds records. A mapping that only wraps a list of records
        # ({"items": [...]}, which is how TOML has to store a list) is unwrapped;
        # any other mapping becomes a single row.
        if isinstance(data, dict):
            only = next(iter(data.values())) if len(data) == 1 else None
            if isinstance(only, list) and only and all(isinstance(i, dict) for i in only):
                notes.append(f"Unwrapped the top-level key {next(iter(data))!r} into rows.")
                data = only
            else:
                data = [data]
        return [flatten(_stringify_temporals(record)) for record in data], notes

    if target is Format.TOML:
        # TOML needs a table at the top level and cannot store null.
        if isinstance(data, list):
            notes.append(f"TOML needs a top-level table, so the records were put under {root_key!r}.")
            data = {root_key: data}
        dropped: List[str] = []
        data = _strip_nulls(data, dropped)
        if dropped:
            shown = ", ".join(dropped[:5]) + (", ..." if len(dropped) > 5 else "")
            notes.append(f"TOML has no null: dropped {len(dropped)} null value(s) ({shown}).")
        return data, notes

    if target is Format.SQL:
        # SQL holds tables. {"users": [...], "roles": [...]} is one table per key,
        # a list of records is one table, and any other mapping is a one-row table.
        if isinstance(data, list):
            tables = {root_key: data}
        elif data and all(
            isinstance(value, list) and value and all(isinstance(item, dict) for item in value)
            for value in data.values()
        ):
            tables = data
        else:
            notes.append(f"The config was written as a single row in table {root_key!r}.")
            tables = {root_key: [data]}
        return {
            name: [flatten(_stringify_temporals(record)) for record in records]
            for name, records in tables.items()
        }, notes

    if target is Format.INI:
        if isinstance(data, list):
            raise ConversionError(
                "INI files hold sections of key = value pairs, so a list of records "
                "cannot be written to INI. Convert a single config mapping instead."
            )
        return _stringify_temporals(data), notes

    if target is Format.XML:
        # XML needs one root element; records become repeated <item> elements in it.
        if isinstance(data, list):
            notes.append("XML needs a single root element, so the records were written as <item> elements.")
            data = {"item": data}
        return _stringify_temporals(data), notes

    if target is Format.JSON:
        return _stringify_temporals(data), notes
    return _stringify_temporals(data, only_time=True), notes  # YAML has native dates


# ---------------------------------------------------------------------------
# Writers - serialise to text first, then write the file in one step
# ---------------------------------------------------------------------------


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


def write_csv(records: List[Dict[str, Any]]) -> str:
    columns: List[str] = []
    for record in records:
        for name in record:
            if name not in columns:
                columns.append(name)
    rows = [{name: _cell_text(value) for name, value in record.items()} for record in records]
    try:
        table = TabularDocument(columns=columns, rows=rows)
    except ValidationError as exc:
        raise _as_schema_error(exc, "The data cannot be written as a CSV table") from exc
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=table.columns, restval="")
    writer.writeheader()
    writer.writerows(table.rows)
    return buffer.getvalue()


def write_ini(data: Dict[str, Any]) -> str:
    """Top-level scalars are written before any section; nested mappings become
    [dotted.section] names; lists are stored as JSON text.
    """
    blocks: List[List[str]] = []

    def emit(section: str, mapping: Dict[str, Any]) -> None:
        lines = []
        children = []
        for key, value in mapping.items():
            if isinstance(value, dict):
                children.append((f"{section}.{key}" if section else key, value))
                continue
            where = f"{section}.{key}" if section else key
            if key != key.strip() or key[0] in "[#;" or any(char in key for char in "=:\n"):
                raise ConversionError(f"The key {where!r} cannot be written as an INI key.")
            if value is None:
                lines.append(key)  # a bare key reads back as null
                continue
            text = _cell_text(value)
            if "\n" in text or "\r" in text:
                raise ConversionError(f"The value of {where!r} spans several lines, which INI cannot store reliably.")
            lines.append(f"{key} = {text}".rstrip())
        if section:
            if "]" in section or "\n" in section:
                raise ConversionError(f"The key {section!r} cannot be written as an INI section name.")
            if lines or not children:  # skip headers that only exist to hold sub-sections
                blocks.append([f"[{section}]", *lines])
        elif lines:
            blocks.append(lines)
        for name, child in children:
            emit(name, child)

    emit("", data)
    return "\n\n".join("\n".join(block) for block in blocks) + "\n"


def _sql_column_type(values: List[Any]) -> str:
    kinds = {type(value) for value in values if value is not None}
    if kinds == {bool}:
        return "BOOLEAN"
    if kinds == {int}:
        return "INTEGER"
    if kinds and kinds <= {int, float}:
        return "REAL"
    return "TEXT"


def _sql_literal(value: Any, column_type: str, where: str) -> str:
    if value is None:
        return "NULL"
    if column_type == "BOOLEAN":
        return "TRUE" if value else "FALSE"
    if column_type in ("INTEGER", "REAL"):
        if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            raise ConversionError(f"{where!r} is NaN or Infinity, which SQL cannot store as a number.")
        return repr(value)
    return "'" + _cell_text(value).replace("'", "''") + "'"


def write_sql(tables: Dict[str, List[Dict[str, Any]]]) -> str:
    """Standard SQL: a CREATE TABLE per table, then one INSERT per row."""
    blocks = []
    for table, records in tables.items():
        columns: List[str] = []
        for record in records:
            for name in record:
                if name not in columns:
                    columns.append(name)
        if not columns:
            raise ConversionError(f"Table {table!r} would have no columns.")
        types = {name: _sql_column_type([record.get(name) for record in records]) for name in columns}
        quoted_table = _sql_identifier(table)
        quoted_columns = ", ".join(_sql_identifier(name) for name in columns)
        lines = [f"CREATE TABLE {quoted_table} ("]
        lines.append(",\n".join(f"  {_sql_identifier(name)} {types[name]}" for name in columns))
        lines.append(");")
        for number, record in enumerate(records):
            values = ", ".join(
                _sql_literal(record.get(name), types[name], f"{table}[{number}].{name}") for name in columns
            )
            lines.append(f"INSERT INTO {quoted_table} ({quoted_columns}) VALUES ({values});")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"


_XML_FORBIDDEN = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f]")


def write_xml(data: Dict[str, Any], root_name: str, indent: int) -> str:
    """The inverse of read_xml: "@name" keys become attributes, "#text" becomes
    element text, lists become repeated elements.
    """

    def scalar_text(value: Any, where: str) -> str:
        text = _cell_text(value)
        if _XML_FORBIDDEN.search(text):
            raise ConversionError(f"The value of {where!r} contains control characters that XML cannot store.")
        return text

    def add(parent: ET.Element, key: str, value: Any, where: str) -> None:
        if not _XML_NAME.fullmatch(key):
            raise ConversionError(
                f"The key {where!r} is not a valid XML element name "
                "(letters, digits, '_', '-', '.'; not starting with a digit)."
            )
        element = ET.SubElement(parent, key)
        if isinstance(value, dict):
            fill(element, value, where)
        elif value is not None:
            element.text = scalar_text(value, where)

    def fill(element: ET.Element, mapping: Dict[str, Any], prefix: str) -> None:
        for key, value in mapping.items():
            where = f"{prefix}.{key}" if prefix else key
            if key.startswith("@") or key == "#text":
                if isinstance(value, (dict, list)):
                    raise ConversionError(f"{where!r} must be a single value to be written as XML.")
                if key == "#text":
                    element.text = scalar_text(value, where)
                elif not _XML_NAME.fullmatch(key[1:]):
                    raise ConversionError(f"The key {where!r} is not a valid XML attribute name.")
                else:
                    element.set(key[1:], scalar_text(value, where))
            elif isinstance(value, list):
                for index, item in enumerate(value):
                    if isinstance(item, list):
                        raise ConversionError(f"{where!r} is a list inside a list, which XML cannot express.")
                    add(element, key, item, f"{where}[{index}]")
            else:
                add(element, key, value, where)

    root = ET.Element(root_name)
    fill(root, data, "")
    if indent:
        ET.indent(root, space=" " * indent)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"


def serialise(data: Any, fmt: Format, indent: int = 2, xml_root: str = "config") -> str:
    """Render adapted data as text in the target format."""
    try:
        if fmt is Format.JSON:
            return json.dumps(data, indent=indent or None, ensure_ascii=False, allow_nan=False) + "\n"
        if fmt is Format.YAML:
            return yaml.safe_dump(
                data,
                sort_keys=False,
                allow_unicode=True,
                default_flow_style=False,
                indent=max(indent, 2),
            )
        if fmt is Format.TOML:
            return tomli_w.dumps(data)
        if fmt is Format.INI:
            return write_ini(data)
        if fmt is Format.XML:
            return write_xml(data, xml_root, indent)
        if fmt is Format.SQL:
            return write_sql(data)
        return write_csv(data)
    except ConfconvError:
        raise
    except (ValueError, TypeError, yaml.YAMLError) as exc:
        # e.g. NaN/Infinity into JSON, or a timezone-aware time into TOML
        raise ConversionError(f"The data cannot be represented as {fmt.value.upper()}: {exc}") from exc


def write_atomic(target: Path, text: str) -> int:
    """Write *text* to *target* via a temp file, so a failure leaves no partial file."""
    payload = text.encode("utf-8")
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            "wb", dir=target.resolve().parent, prefix=f".{target.name}.", suffix=".tmp", delete=False
        ) as handle:
            temp_name = handle.name
            handle.write(payload)
        os.replace(temp_name, target)
    except OSError as exc:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)
        raise FileProblem(f"Cannot write {target}: {exc.strerror or exc}") from exc
    return len(payload)


# ---------------------------------------------------------------------------
# Rich output
# ---------------------------------------------------------------------------


def _value_text(value: Any) -> Text:
    """A colour-coded, length-limited rendering of one value."""
    if value is None:
        text, style = "null", "dim italic"
    elif isinstance(value, bool):
        text, style = ("true", "green") if value else ("false", "red")
    elif isinstance(value, (int, float)):
        text, style = str(value), "cyan"
    elif isinstance(value, (list, dict)):
        text, style = json.dumps(value, ensure_ascii=False, default=str), "yellow"
    elif isinstance(value, str):
        text, style = value, ""
    else:  # dates and times
        text, style = str(value), "magenta"
    text = text.replace("\n", "\\n")
    if len(text) > MAX_CELL_WIDTH:
        text = text[: MAX_CELL_WIDTH - 3] + "..."
    return Text(text, style=style)  # Text, not a markup string: data is never parsed as markup


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, list):
        return f"list[{len(value)}]"
    return {"str": "string", "dict": "table"}.get(type(value).__name__, type(value).__name__)


def build_preview(data: Any, title: str, max_rows: int) -> Table:
    """Records become one column per field; a single mapping becomes key/value rows."""
    table = Table(title=title, box=box.ROUNDED, header_style="bold cyan", title_style="bold")
    hidden_columns = 0
    wrapped = _wrapped_records(data)
    if wrapped:
        table.title = f"{title} ({wrapped[0]})"
        data = wrapped[1]

    if isinstance(data, list):
        records = [flatten(record) for record in data]
        columns: List[str] = []
        for record in records:
            for name in record:
                if name not in columns:
                    columns.append(name)
        hidden_columns = max(len(columns) - MAX_PREVIEW_COLUMNS, 0)
        columns = columns[:MAX_PREVIEW_COLUMNS]
        table.add_column("#", style="dim", justify="right")
        for name in columns:
            table.add_column(Text(name), overflow="fold")
        for number, record in enumerate(records[:max_rows], start=1):
            cells = [_value_text(record[name]) if name in record else Text("") for name in columns]
            table.add_row(str(number), *cells)
        total = len(records)
    else:
        entries = list(flatten(data).items())
        table.add_column("Key", style="bold", overflow="fold")
        table.add_column("Value", overflow="fold")
        table.add_column("Type", style="dim")
        for key, value in entries[:max_rows]:
            table.add_row(Text(key), _value_text(value), _type_name(value))
        total = len(entries)

    captions = []
    if total > max_rows:
        captions.append(f"... {total - max_rows} more row(s) not shown")
    if hidden_columns:
        captions.append(f"... {hidden_columns} more column(s) not shown")
    if total == 0:
        captions.append("(no data)")
    if captions:
        table.caption = "  ".join(captions)
    return table


def _wrapped_records(data: Any) -> Optional[Tuple[str, List[Dict[str, Any]]]]:
    """(key, records) when *data* is a mapping whose only value is a list of records."""
    if isinstance(data, dict) and len(data) == 1:
        key, value = next(iter(data.items()))
        if isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
            return key, value
    return None


def _describe(data: Any) -> str:
    if isinstance(data, list):
        return f"{len(data)} record(s)"
    wrapped = _wrapped_records(data)
    if wrapped:
        return f"{len(wrapped[1])} record(s) under {wrapped[0]!r}"
    return f"{len(data)} top-level key(s)"


def print_notes(notes: List[str]) -> None:
    for note in notes:
        console.print(Text.assemble(("! ", "bold yellow"), (note, "yellow")))


def print_error(exc: ConfconvError) -> None:
    body = Text()
    if isinstance(exc, ParseError):
        position = str(exc.path)
        if exc.line is not None:
            position += f", line {exc.line}"
            if exc.column is not None:
                position += f", column {exc.column}"
        body.append(position + "\n", style="bold")
        body.append(str(exc))
        if exc.source_text is not None and exc.line is not None:
            lines = exc.source_text.splitlines()
            if 0 < exc.line <= len(lines):
                gutter = f"{exc.line} | "
                body.append(f"\n\n{gutter}", style="dim")
                body.append(lines[exc.line - 1].expandtabs(1)[:200])
                if exc.column is not None and exc.column <= 200:
                    body.append("\n" + " " * (len(gutter) + exc.column - 1) + "^", style="bold red")
    elif isinstance(exc, SchemaError):
        body.append(exc.subject + "\n", style="bold")
        for problem in exc.problems:
            body.append(f"\n - {problem}")
    else:
        body.append(str(exc))
    err_console.print(Panel(body, title=exc.title, title_align="left", border_style="red"))


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


class GuardedGroup(click.Group):
    """A click group that reports ConfconvError with rich and its exit code.

    "convert" is the default command: `confconv a.json b.yaml` works without it.
    """

    def parse_args(self, ctx: click.Context, args: List[str]) -> List[str]:
        own_options = {"-h", "--help", "-V", "--version"}
        if args and args[0] not in self.commands and args[0] not in own_options:
            args = ["convert", *args]
        return super().parse_args(ctx, args)

    def invoke(self, ctx: click.Context) -> Any:
        try:
            return super().invoke(ctx)
        except ConfconvError as exc:
            print_error(exc)
            ctx.exit(exc.exit_code)


FORMAT_CHOICE = click.Choice([fmt.value for fmt in Format], case_sensitive=False)
FILE_ARGUMENT = click.Path(path_type=Path)


@click.group(cls=GuardedGroup, context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, "-V", "--version", prog_name="confconv")
def cli() -> None:
    """Convert configuration files between JSON, YAML, TOML, CSV, INI, XML and SQL.

    Formats are detected from the file extensions. "convert" is the default
    command, so `confconv SOURCE TARGET` is enough.
    """


@cli.command()
@click.argument("source", type=FILE_ARGUMENT)
@click.argument("target", type=FILE_ARGUMENT, required=False)
@click.option("--from", "from_format", type=FORMAT_CHOICE, help="Source format, if the extension is missing or wrong.")
@click.option("--to", "to_format", type=FORMAT_CHOICE, help="Target format. Without TARGET, writes next to SOURCE with the new extension.")
@click.option("-f", "--force", is_flag=True, help="Overwrite TARGET if it already exists.")
@click.option("-n", "--dry-run", is_flag=True, help="Validate and preview the result without writing anything.")
@click.option("--indent", type=click.IntRange(0, 8), default=2, show_default=True, help="Indent for JSON, YAML and XML output (0 = compact JSON and XML).")
@click.option("--root-key", default="items", show_default=True, help="Key that holds the records when a list is written to TOML; table name when it is written to SQL.")
@click.option("--xml-root", default="config", show_default=True, help="Name of the root element when writing XML.")
@click.option("--no-infer", is_flag=True, help="Keep CSV, INI and XML values as text instead of detecting numbers, booleans and nulls; keep JSON text in SQL as text.")
@click.option("--max-rows", type=click.IntRange(min=1), default=20, show_default=True, help="Rows shown in the preview table.")
@click.option("--no-preview", is_flag=True, help="Do not print the preview table.")
@click.option("-q", "--quiet", is_flag=True, help="Print nothing on success (errors are still shown).")
def convert(
    source: Path,
    target: Optional[Path],
    from_format: Optional[str],
    to_format: Optional[str],
    force: bool,
    dry_run: bool,
    indent: int,
    root_key: str,
    xml_root: str,
    no_infer: bool,
    max_rows: int,
    no_preview: bool,
    quiet: bool,
) -> None:
    """Convert SOURCE to TARGET.

    \b
    Examples:
      confconv settings.yaml settings.toml
      confconv settings.yaml --to json        (writes settings.json)
      confconv users.json users.csv --force
      confconv data.txt out.json --from csv
      confconv settings.toml --to yaml --dry-run
    """
    if target is None:
        if not to_format:
            raise FormatError("Give a TARGET file, or --to FORMAT to write next to SOURCE.")
        target = source.with_suffix(f".{to_format.lower()}")
    try:
        job = ConversionJob(
            source=source,
            target=target,
            source_format=detect_format(source, from_format, "--from"),
            target_format=detect_format(target, to_format, "--to"),
            force=force,
            dry_run=dry_run,
            indent=indent,
            root_key=root_key,
            xml_root=xml_root,
            infer_types=not no_infer,
        )
    except ValidationError as exc:
        raise _as_schema_error(exc, "Invalid options") from exc

    data, notes = load_document(job.source, job.source_format, job.infer_types)
    converted, adapt_notes = adapt(data, job.target_format, job.root_key)
    text = serialise(converted, job.target_format, job.indent, job.xml_root)
    size = len(text.encode("utf-8")) if job.dry_run else write_atomic(job.target, text)

    if quiet:
        return
    print_notes(notes + adapt_notes)
    colour = "yellow" if job.dry_run else "green"
    summary = Text.assemble(
        ("Would convert " if job.dry_run else "Converted ", f"bold {colour}"),
        (str(job.source), "bold"),
        (f" ({job.source_format.value.upper()})", "cyan"),
        (" -> ", f"bold {colour}"),
        (str(job.target), "bold"),
        (f" ({job.target_format.value.upper()})", "cyan"),
        (f"\n{_describe(converted)}, {size:,} bytes", colour),
        (" (dry run, nothing written)" if job.dry_run else " written", colour),
    )
    title = "Dry run" if job.dry_run else "Success"
    console.print(Panel(summary, title=title, title_align="left", border_style=colour))
    if not no_preview:
        console.print(build_preview(converted, f"Converted data: {job.target.name}", max_rows))


@cli.command()
@click.argument("file", type=FILE_ARGUMENT)
@click.option("--from", "from_format", type=FORMAT_CHOICE, help="File format, if the extension is missing or wrong.")
@click.option("--no-infer", is_flag=True, help="Keep CSV, INI and XML values as text.")
@click.option("--max-rows", type=click.IntRange(min=1), default=20, show_default=True, help="Rows shown in the preview table.")
@click.option("--no-preview", is_flag=True, help="Do not print the preview table.")
def validate(file: Path, from_format: Optional[str], no_infer: bool, max_rows: int, no_preview: bool) -> None:
    """Check FILE for syntax and structure errors without converting it."""
    fmt = detect_format(file, from_format, "--from")
    if not file.is_file():
        raise FileProblem(f"File not found: {file}")
    data, notes = load_document(file, fmt, not no_infer)
    print_notes(notes)
    summary = Text.assemble(
        (str(file), "bold"),
        (f" is valid {fmt.value.upper()}", "bold green"),
        (f"\n{_describe(data)}", "green"),
    )
    console.print(Panel(summary, title="Valid", title_align="left", border_style="green"))
    if not no_preview:
        console.print(build_preview(data, f"Contents: {file.name}", max_rows))


@cli.command()
def formats() -> None:
    """List the supported formats and their file extensions."""
    table = Table(box=box.ROUNDED, header_style="bold cyan")
    table.add_column("Format", style="bold")
    table.add_column("Extensions")
    table.add_column("Holds")
    holds = {
        Format.JSON: "nested config or list of records",
        Format.YAML: "nested config or list of records",
        Format.TOML: "nested config (lists go under a key, no nulls)",
        Format.CSV: "flat records (nesting becomes dotted columns)",
        Format.INI: "config in sections (nesting becomes dotted section names)",
        Format.XML: "nested config under one root element",
        Format.SQL: "tables of flat records (CREATE TABLE + INSERT)",
    }
    for fmt in Format:
        extensions = ", ".join(ext for ext, owner in EXTENSIONS.items() if owner is fmt)
        table.add_row(fmt.value.upper(), extensions, holds[fmt])
    console.print(table)


def main() -> None:
    # When output is piped on Windows the stream may use a legacy code page;
    # replace unencodable characters rather than crash while printing data.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    cli(prog_name="confconv")


if __name__ == "__main__":
    main()
