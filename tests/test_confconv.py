"""End-to-end tests: every case runs the real CLI on real files."""

import itertools
import json
import os
import sys
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# rich reads the width when confconv is imported; a wide terminal keeps it from
# wrapping long temp paths in the middle of the messages asserted on below.
os.environ["COLUMNS"] = "500"

import confconv  # noqa: E402

CONFIG = {
    "name": "demo",
    "debug": True,
    "retries": 3,
    "ratio": 0.5,
    "database": {"host": "localhost", "port": 5432, "tags": ["a", "b"]},
}
RECORDS = [
    {"id": 1, "name": "Ada", "admin": True, "score": 9.5, "address": {"city": "London"}},
    {"id": 2, "name": "Linus", "admin": False, "score": 7.25, "address": {"city": "Helsinki"}},
]
# Formats that can hold a list of records (INI holds a single config only).
EXT = {"json": ".json", "yaml": ".yaml", "toml": ".toml", "csv": ".csv", "xml": ".xml", "sql": ".sql"}


def run(*args):
    return CliRunner().invoke(confconv.cli, [str(a) for a in args])


def load(path: Path):
    """Parse a file independently of confconv's own readers where possible."""
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        return json.loads(text)
    if path.suffix == ".yaml":
        return yaml.safe_load(text)
    if path.suffix == ".toml":
        return tomllib.loads(text)
    return confconv.load_document(path, confconv.Format.CSV)[0]


def write_json(path: Path, data) -> Path:
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.mark.parametrize("src,dst", list(itertools.permutations(EXT, 2)))
def test_every_pair_preserves_records(tmp_path, src, dst):
    start = write_json(tmp_path / "start.json", RECORDS)
    source = tmp_path / f"source{EXT[src]}"
    if src != "json":
        assert run("convert", start, source).exit_code == 0
    else:
        source = start
    target = tmp_path / f"target{EXT[dst]}"
    result = run("convert", source, target)
    assert result.exit_code == 0, result.output

    back = tmp_path / "back.json"
    assert run("convert", target, back).exit_code == 0
    data = json.loads(back.read_text(encoding="utf-8"))
    if isinstance(data, dict):  # TOML, XML and SQL keep records under one key
        (data,) = data.values()
    assert data == RECORDS


def test_nested_config_chain_round_trips(tmp_path):
    a = write_json(tmp_path / "a.json", CONFIG)
    b, c, d, e, f = (tmp_path / name for name in ("b.yaml", "c.toml", "d.xml", "e.ini", "f.json"))
    for source, target in ((a, b), (b, c), (c, d), (d, e), (e, f)):
        result = run(source, target)  # also exercises the default command
        assert result.exit_code == 0, result.output
    assert load(b) == CONFIG
    assert load(c) == CONFIG
    assert load(f) == CONFIG


def test_config_to_csv_is_one_flattened_row(tmp_path):
    source = write_json(tmp_path / "a.json", CONFIG)
    target = tmp_path / "a.csv"
    assert run(source, target).exit_code == 0
    header = target.read_text(encoding="utf-8").splitlines()[0]
    assert header == "name,debug,retries,ratio,database.host,database.port,database.tags"
    assert load(target) == [CONFIG]


def test_csv_type_inference(tmp_path):
    source = tmp_path / "a.csv"
    source.write_text("id,zip,on,empty,ratio,code\n7,01234,TRUE,,1.50,1e5\n", encoding="utf-8")
    assert load(source) == [
        {"id": 7, "zip": "01234", "on": True, "empty": None, "ratio": 1.5, "code": "1e5"}
    ]
    target = tmp_path / "a.json"
    assert run(source, target, "--no-infer").exit_code == 0
    assert load(target) == [
        {"id": "7", "zip": "01234", "on": "TRUE", "empty": "", "ratio": "1.50", "code": "1e5"}
    ]


def test_toml_drops_nulls_with_a_warning(tmp_path):
    source = write_json(tmp_path / "a.json", {"keep": 1, "gone": None, "list": [1, None]})
    target = tmp_path / "a.toml"
    result = run(source, target)
    assert result.exit_code == 0
    assert "dropped 2 null" in result.output
    assert load(target) == {"keep": 1, "list": [1]}


def test_target_derived_from_to_option(tmp_path):
    source = write_json(tmp_path / "settings.json", CONFIG)
    assert run(source, "--to", "yaml").exit_code == 0
    assert load(tmp_path / "settings.yaml") == CONFIG


def test_default_command_accepts_leading_options(tmp_path):
    source = write_json(tmp_path / "a.json", CONFIG)
    result = run("-q", source, tmp_path / "a.yaml")
    assert result.exit_code == 0
    assert result.output == ""
    assert load(tmp_path / "a.yaml") == CONFIG
    assert run("--help").exit_code == 0
    assert "version 1." in run("--version").output


def test_dry_run_writes_nothing(tmp_path):
    source = write_json(tmp_path / "a.json", CONFIG)
    target = tmp_path / "a.yaml"
    result = run(source, target, "--dry-run")
    assert result.exit_code == 0
    assert "nothing written" in result.output
    assert not target.exists()


@pytest.mark.parametrize(
    "name,content,line",
    [
        ("bad.json", '{\n  "a": 1,\n  "b": \n}', 4),
        ("bad.yaml", "a: 1\nb: [1, 2\nc: 3\n", 3),
        ("bad.toml", 'a = 1\nb = "unterminated\n', 2),
        ("bad.csv", "a,b\n1,2\n3\n", 3),
        ("bad.ini", "[a]\nx = 1\n= value without a key\n", 3),
        ("header.ini", "[a]\nx = 1\n[b\ny = 2\n", 3),
        ("dup.ini", "[a]\nx = 1\nx = 2\n", 3),
        ("bad.xml", "<config>\n  <a>1</a>\n  <b>2</c>\n</config>\n", 3),
        ("bad.sql", "CREATE TABLE t (a INTEGER);\n\nINSERT INTO t VALUES (1, 2);\n", 3),
        ("attach.sql", "CREATE TABLE t (a);\nATTACH DATABASE 'x.db' AS x;\n", 2),
    ],
)
def test_syntax_errors_report_the_line_and_leave_no_output(tmp_path, name, content, line):
    source = tmp_path / name
    source.write_text(content, encoding="utf-8")
    target = tmp_path / "out.json" if not name.endswith(".json") else tmp_path / "out.yaml"
    result = run(source, target)
    assert result.exit_code == 1
    assert f"line {line}" in result.output
    assert sorted(p.name for p in tmp_path.iterdir()) == [name]


@pytest.mark.parametrize(
    "name,content,expected",
    [
        ("scalar.json", "42", "top level must be a mapping"),
        ("list.json", "[1, 2]", "must contain only mappings"),
        ("empty.yaml", "", "document is empty"),
        ("intkey.yaml", "servers:\n  1: a\n", "keys must be strings"),
        ("dup.csv", "a,a\n1,2\n", "duplicate column"),
    ],
)
def test_schema_errors(tmp_path, name, content, expected):
    source = tmp_path / name
    source.write_text(content, encoding="utf-8")
    result = run("validate", source)
    assert result.exit_code == 1
    assert expected in " ".join(result.output.split())


def test_refuses_to_overwrite_without_force(tmp_path):
    source = write_json(tmp_path / "a.json", CONFIG)
    target = tmp_path / "a.yaml"
    target.write_text("original: true\n", encoding="utf-8")
    assert run(source, target).exit_code == 3
    assert load(target) == {"original": True}
    assert run(source, target, "--force").exit_code == 0
    assert load(target) == CONFIG


def test_exit_codes_for_missing_file_and_unknown_extension(tmp_path):
    assert run(tmp_path / "nope.json", tmp_path / "out.yaml").exit_code == 3
    source = write_json(tmp_path / "a.json", CONFIG)
    assert run(source, tmp_path / "out.dat").exit_code == 2
    assert run(source).exit_code == 2  # no target and no --to
    assert run(source, tmp_path / "out.dat", "--to", "toml").exit_code == 0
    assert load_toml(tmp_path / "out.dat") == CONFIG


def load_toml(path: Path):
    return tomllib.loads(path.read_text(encoding="utf-8"))


def test_json_cannot_hold_nan(tmp_path):
    source = tmp_path / "a.yaml"
    source.write_text("value: .nan\n", encoding="utf-8")
    result = run(source, tmp_path / "a.json")
    assert result.exit_code == 1
    assert not (tmp_path / "a.json").exists()


def test_dates_survive_yaml_to_toml_and_become_strings_in_json(tmp_path):
    source = tmp_path / "a.yaml"
    source.write_text("released: 2024-05-17\n", encoding="utf-8")
    assert run(source, tmp_path / "a.toml").exit_code == 0
    assert "released = 2024-05-17" in (tmp_path / "a.toml").read_text(encoding="utf-8")
    assert run(source, tmp_path / "a.json").exit_code == 0
    assert load(tmp_path / "a.json") == {"released": "2024-05-17"}


def test_preview_does_not_interpret_data_as_markup(tmp_path):
    source = write_json(tmp_path / "a.json", {"label": "[bold]not markup[/bold] [/oops]"})
    result = run("validate", source)
    assert result.exit_code == 0
    assert "[bold]not markup[/bold] [/oops]" in result.output


def read(path: Path, fmt: str):
    return confconv.load_document(path, confconv.Format(fmt))


def test_ini_reads_sections_bare_keys_and_keys_outside_sections(tmp_path):
    source = tmp_path / "a.ini"
    source.write_text(
        "; comment\n"
        "name = demo\n"
        "\n"
        "[server]\n"
        "Port = 8080\n"
        "debug = true\n"
        "motd =\n"
        "skip-checks\n"
        "rate = 50%\n"
        "\n"
        "[server.tls]\n"
        "cert: /etc/cert.pem\n"
        "\n"
        "[DEFAULT]\n"
        "x = 1\n",
        encoding="utf-8",
    )
    data, notes = read(source, "ini")
    assert notes == []
    assert data == {
        "name": "demo",
        "server": {
            "Port": 8080,
            "debug": True,
            "motd": "",
            "skip-checks": None,
            "rate": "50%",
            "tls": {"cert": "/etc/cert.pem"},
        },
        "DEFAULT": {"x": 1},
    }
    # Writing it back and reading again gives the same data.
    assert run(source, tmp_path / "b.ini").exit_code == 0
    assert read(tmp_path / "b.ini", "ini")[0] == data


def test_ini_cannot_hold_records(tmp_path):
    source = write_json(tmp_path / "a.json", RECORDS)
    result = run(source, tmp_path / "a.ini")
    assert result.exit_code == 1
    assert "cannot be written to INI" in result.output
    assert not (tmp_path / "a.ini").exists()


def test_xml_attributes_text_repeats_and_namespaces(tmp_path):
    source = tmp_path / "a.xml"
    source.write_text(
        '<?xml version="1.0"?>\n'
        '<!-- comment -->\n'
        '<settings xmlns:x="urn:example" version="2">\n'
        '  <name>demo &amp; co</name>\n'
        '  <server port="8080">main</server>\n'
        '  <tag>a</tag>\n'
        '  <tag>b</tag>\n'
        '  <x:extra>007</x:extra>\n'
        '  <empty/>\n'
        '</settings>\n',
        encoding="utf-8",
    )
    data, notes = read(source, "xml")
    assert data == {
        "@version": 2,
        "name": "demo & co",
        "server": {"@port": 8080, "#text": "main"},
        "tag": ["a", "b"],
        "extra": "007",
        "empty": None,
    }
    assert len(notes) == 1 and "namespaces" in notes[0]
    # XML -> XML keeps attributes, text and repeated elements.
    assert run(source, tmp_path / "b.xml", "--xml-root", "settings").exit_code == 0
    written = (tmp_path / "b.xml").read_text(encoding="utf-8")
    assert '<settings version="2">' in written
    assert '<server port="8080">main</server>' in written
    assert read(tmp_path / "b.xml", "xml")[0] == data


def test_xml_honours_its_own_encoding(tmp_path):
    source = tmp_path / "a.xml"
    source.write_bytes('<?xml version="1.0" encoding="utf-16"?><c><name>caf\u00e9</name></c>'.encode("utf-16"))
    assert read(source, "xml")[0] == {"name": "caf\u00e9"}


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])
def test_xml_with_a_doctype_is_refused(tmp_path, encoding):
    bomb = (
        '<?xml version="1.0" encoding="' + encoding + '"?>\n'
        '<!DOCTYPE lolz [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;">]>\n'
        "<c><v>&b;</v></c>\n"
    )
    source = tmp_path / "bomb.xml"
    source.write_bytes(bomb.encode(encoding))
    result = run(source, tmp_path / "out.json")
    assert result.exit_code == 1
    assert "DOCTYPE" in result.output
    assert not (tmp_path / "out.json").exists()


def test_xml_rejects_keys_that_are_not_element_names(tmp_path):
    source = write_json(tmp_path / "a.json", {"ok": 1, "nested": {"two words": 2}})
    result = run(source, tmp_path / "a.xml")
    assert result.exit_code == 1
    assert "nested.two words" in result.output
    assert not (tmp_path / "a.xml").exists()


def test_sql_output_is_typed_and_escaped(tmp_path):
    rows = [
        {"id": 1, "name": "O'Brien", "admin": True, "score": 1, "note": None, "tags": ["a", "b"]},
        {"id": 2, "name": "x; DROP TABLE users;--", "admin": False, "score": 2.5, "note": None, "tags": []},
    ]
    source = write_json(tmp_path / "a.json", {"users": rows})
    assert run(source, tmp_path / "a.sql").exit_code == 0
    text = (tmp_path / "a.sql").read_text(encoding="utf-8")
    assert 'CREATE TABLE "users" (' in text
    for column in ('"id" INTEGER', '"name" TEXT', '"admin" BOOLEAN', '"score" REAL', '"note" TEXT'):
        assert column in text
    assert "'O''Brien'" in text
    assert read(tmp_path / "a.sql", "sql")[0] == {"users": rows}


def test_sql_reads_a_hand_written_dump_with_several_tables(tmp_path):
    source = tmp_path / "dump.sql"
    source.write_text(
        "PRAGMA foreign_keys=OFF;\n"
        "BEGIN TRANSACTION;\n"
        "-- users\n"
        "CREATE TABLE users (\n"
        "  id INTEGER PRIMARY KEY,\n"
        "  name TEXT NOT NULL,\n"
        "  active BOOLEAN\n"
        ");\n"
        "INSERT INTO users VALUES (1, 'Ada; Lovelace', TRUE), (2, 'Linus', 0);\n"
        "CREATE TABLE roles (name TEXT);\n"
        "INSERT INTO roles (name) VALUES ('admin');\n"
        "COMMIT;\n",
        encoding="utf-8",
    )
    assert read(source, "sql")[0] == {
        "users": [
            {"id": 1, "name": "Ada; Lovelace", "active": True},
            {"id": 2, "name": "Linus", "active": False},
        ],
        "roles": [{"name": "admin"}],
    }


def test_config_to_sql_is_one_row(tmp_path):
    source = write_json(tmp_path / "a.json", CONFIG)
    assert run(source, tmp_path / "a.sql", "--root-key", "settings").exit_code == 0
    assert read(tmp_path / "a.sql", "sql")[0] == {"settings": [CONFIG]}


def test_sql_file_cannot_reach_outside_the_memory_database(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "a.sql"
    source.write_text("ATTACH DATABASE 'leak.db' AS leak;\nCREATE TABLE leak.t (a);\n", encoding="utf-8")
    assert run(source, tmp_path / "out.json").exit_code == 1
    assert sorted(path.name for path in tmp_path.iterdir()) == ["a.sql"]
