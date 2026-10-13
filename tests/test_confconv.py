"""End-to-end tests: every case runs the real CLI on real files."""

import itertools
import json
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
EXT = {"json": ".json", "yaml": ".yaml", "toml": ".toml", "csv": ".csv"}


def run(*args):
    # A wide terminal keeps rich from wrapping long temp paths mid-message.
    return CliRunner().invoke(confconv.cli, [str(a) for a in args], env={"COLUMNS": "500"})


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
    if isinstance(data, dict):  # TOML keeps records under the root key
        data = data["items"]
    assert data == RECORDS


def test_nested_config_chain_round_trips(tmp_path):
    a = write_json(tmp_path / "a.json", CONFIG)
    b, c, d = tmp_path / "b.yaml", tmp_path / "c.toml", tmp_path / "d.json"
    for source, target in ((a, b), (b, c), (c, d)):
        assert run(source, target).exit_code == 0  # also exercises the default command
    assert load(b) == CONFIG
    assert load(c) == CONFIG
    assert load(d) == CONFIG


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
    assert run(source, tmp_path / "out.ini").exit_code == 2
    assert run(source).exit_code == 2  # no target and no --to
    assert run(source, tmp_path / "out.ini", "--to", "toml").exit_code == 0
    assert load_toml(tmp_path / "out.ini") == CONFIG


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
