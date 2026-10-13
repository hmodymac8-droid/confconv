# confconv

[![tests](https://github.com/hmodymac8-droid/confconv/actions/workflows/tests.yml/badge.svg)](https://github.com/hmodymac8-droid/confconv/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

Convert configuration files between **JSON, YAML, TOML, CSV, INI, XML and SQL** in any direction.

- Formats are detected from the file extensions.
- The source is checked for syntax and structure errors before anything is written.
- Results are shown as a colour-coded summary and a table of the converted data.
- A failed conversion never leaves a half-written file, and existing files are not
  overwritten unless you say so.

```text
$ confconv users.json users.csv
╭─ Success ──────────────────────────────────────────────╮
│ Converted users.json (JSON) -> users.csv (CSV)         │
│ 2 record(s), 71 bytes written                          │
╰────────────────────────────────────────────────────────╯
        Converted data: users.csv
╭───┬────┬───────┬───────┬──────────────╮
│ # │ id │ name  │ admin │ address.city │
├───┼────┼───────┼───────┼──────────────┤
│ 1 │ 1  │ Ada   │ true  │ London       │
│ 2 │ 2  │ Linus │ false │ Helsinki     │
╰───┴────┴───────┴───────┴──────────────╯
```

## Install

Requires Python 3.9 or newer.

**As a command** (recommended). This puts `confconv` on your PATH:

```bash
pip install git+https://github.com/hmodymac8-droid/confconv.git
```

**As a single script.** Everything lives in `confconv.py`, so you can also just
download that file and install its dependencies:

```bash
pip install -r requirements.txt
python confconv.py --help
```

On Windows, if `python` opens the Microsoft Store, use the launcher instead:
`py -m pip install ...` and `py confconv.py ...`.

The examples below use the `confconv` command; with the single script, write
`python confconv.py` in its place.

## Usage

### Convert

```bash
confconv SOURCE TARGET
```

```bash
confconv settings.yaml settings.toml     # YAML -> TOML
confconv users.json users.csv            # JSON -> CSV
confconv settings.ini settings.xml       # INI -> XML
confconv users.csv users.sql             # CSV -> CREATE TABLE + INSERT statements
confconv settings.yaml --to json         # no TARGET: writes settings.json next to the source
confconv settings.toml --to yaml --dry-run   # check and preview, write nothing
confconv export.txt out.json --from csv  # extension is wrong or missing: say what it is
```

`confconv convert SOURCE TARGET` is the long form of the same command.

| Option | Effect |
|---|---|
| `--from FORMAT`, `--to FORMAT` | Set a format explicitly (`json`, `yaml`, `toml`, `csv`, `ini`, `xml`, `sql`) instead of using the extension. With `--to` and no TARGET, the output goes next to SOURCE with the new extension. |
| `-f`, `--force` | Overwrite TARGET if it exists. Without it, confconv refuses. |
| `-n`, `--dry-run` | Validate and preview the result without writing a file. |
| `--indent N` | Indent for JSON, YAML and XML output, 0 to 8 (default 2; 0 gives compact JSON and XML). |
| `--root-key NAME` | Key that holds the records when a list is written to TOML, and the table name when it is written to SQL (default `items`). |
| `--xml-root NAME` | Name of the root element when writing XML (default `config`). |
| `--no-infer` | Keep CSV, INI and XML values as text instead of detecting numbers, booleans and nulls. |
| `--max-rows N` | Rows shown in the preview table (default 20). |
| `--no-preview` | Skip the preview table. |
| `-q`, `--quiet` | Print nothing on success. |

### Validate

Check a file without converting it:

```bash
confconv validate settings.yaml
```

Syntax errors point at the line:

```text
╭─ Syntax error ─────────────────────────────────────────╮
│ bad.json, line 4, column 1                             │
│ Expecting value                                        │
│                                                        │
│ 4 | }                                                  │
│     ^                                                  │
╰────────────────────────────────────────────────────────╯
```

### List formats

```bash
confconv formats
```

## What counts as a valid file

After parsing, every file must match the same basic schema:

- The top level is a mapping (a config), or a list of mappings (records).
- Keys are non-empty strings.
- Values are strings, numbers, booleans, nulls, dates/times, or lists and mappings of those.

So a JSON file containing just `42` or `[1, 2, 3]`, an empty YAML file, or YAML with a
numeric key such as `1: value` is rejected with a message saying where the problem is.
CSV files additionally need a header row with unique, non-empty column names, and every
row must have the same number of fields as the header.

| Format | Extensions | Holds |
|---|---|---|
| JSON | `.json` | nested config or list of records |
| YAML | `.yaml`, `.yml` | nested config or list of records |
| TOML | `.toml` | nested config |
| CSV | `.csv` | flat records |
| INI | `.ini`, `.cfg` | a config in sections |
| XML | `.xml` | nested config under one root element |
| SQL | `.sql` | tables of flat records |

## How the formats map onto each other

JSON and YAML can hold the same structures, so those conversions are direct. The other
formats are more restricted; this is what confconv does about it.

**Into CSV**

- A list of records becomes one row per record. A single mapping becomes one row.
- A mapping that only wraps a list of records, such as `{"items": [...]}`, is unwrapped
  into rows.
- Nested keys become dotted column names: `{"address": {"city": "London"}}` gives a column
  `address.city`.
- A list inside a record is stored in its cell as JSON text, e.g. `["a", "b"]`.
- Nulls become empty cells; booleans become `true` / `false`.

**Out of CSV**

- Rows become a list of records, and dotted column names are turned back into nesting.
- Cell text is converted to the type it looks like: `42` to an integer, `1.5` to a float,
  `true`/`false` to a boolean, an empty cell to null, and JSON lists back to lists.
  Numbers with leading zeros (`007`, zip codes) stay as text. Use `--no-infer` to keep
  every cell as text.

**Into TOML**

- TOML needs a table at the top, so a list of records is placed under one key
  (`items` by default, see `--root-key`).
- TOML has no null. Null values are dropped and confconv prints a warning listing them.

**INI**

- Top-level mappings become sections. Mappings nested deeper become dotted section names:
  `{"server": {"tls": {"cert": "x"}}}` is written as `[server.tls]`, and read back the
  same way.
- Top-level values that are not mappings are written before the first section header.
  confconv reads such files too; note that some INI parsers require every key to be in
  a section.
- A null is written as a bare key with no `=`; a list is stored as JSON text.
- INI stores only text, so on reading, values are converted to the type they look like,
  as for CSV. `--no-infer` keeps them as text.
- `[DEFAULT]` is treated as an ordinary section, `%` as an ordinary character, and the
  case of keys is kept. Keys may use `=` or `:`; comments start with `#` or `;`.
- INI holds one config, so a list of records cannot be written to INI. Values that span
  several lines cannot be written either. Both stop with an error.

**XML**

- When writing, the data goes inside one root element, `<config>` unless you set
  `--xml-root`. When reading, the root element's name is dropped and its contents become
  the document.
- A list becomes a repeated element, and repeated elements are read back as a list.
  A list of records is written as repeated `<item>` elements.
- Attributes are read as keys starting with `@`, and text next to attributes or child
  elements as `#text`: `<server port="8080">main</server>` becomes
  `{"server": {"@port": 8080, "#text": "main"}}`. Writing does the reverse.
- An empty element is a null. Values are converted to the type they look like unless
  `--no-infer` is given.
- Keys must be valid element names (letters, digits, `_`, `-`, `.`, not starting with a
  digit); otherwise the conversion stops with an error naming the key.
- Namespace prefixes are dropped with a warning, and comments are ignored.
- Files containing a `<!DOCTYPE>` are refused, because a DTD can define entities that
  expand without limit.

**SQL**

- Writing produces standard SQL: one `CREATE TABLE` and one `INSERT` per row, with quoted
  identifiers. Column types are chosen from the values: `INTEGER`, `REAL`, `BOOLEAN` or
  `TEXT`.
- A list of records becomes one table, named by `--root-key` (default `items`). A mapping
  whose values are all lists of records, such as `{"users": [...], "roles": [...]}`,
  becomes one table per key. Any other mapping becomes a single row.
- As with CSV, nested keys become dotted column names and lists are stored as JSON text.
- Reading runs the file's `CREATE TABLE` and `INSERT` statements in a temporary in-memory
  SQLite database and returns `{table: [rows]}`. Columns declared `BOOLEAN` come back as
  booleans.
- Only table creation and inserts are allowed while reading. Anything else, such as
  `ATTACH`, is refused, so a `.sql` file cannot touch other files. `PRAGMA` lines are
  skipped.
- The file has to be SQL that SQLite understands. Plain `CREATE TABLE` / `INSERT` scripts
  and SQLite dumps work; MySQL and PostgreSQL dumps that use their own syntax
  (`ENGINE=`, `COPY`, backslash escapes) generally do not.

**Dates and times**

- YAML and TOML have native dates; they are carried across between those two.
- The other formats have none, so dates are written as ISO 8601 text such as `2024-05-17`.

### What does not survive a conversion

- **Comments and formatting.** Only the data is converted, in every direction.
- **Nulls going into TOML** (see above).
- **Some types on a CSV, INI or XML round trip.** These store only text, so the text
  `"true"` or `"42"` comes back as a boolean or a number. In CSV and XML an empty string
  comes back as null.
- **Key names containing a dot**, when passing through CSV, SQL or INI sections:
  `{"a.b": 1}` comes back as `{"a": {"b": 1}}`.
- **Short lists in XML.** A list with one item comes back as a single value, and an empty
  list disappears, because XML only shows a list by repeating an element.
- **The XML root element's name**, which the other formats have no place for. Pass
  `--xml-root` to set it when converting back.
- **SQL constraints and types.** Keys, indexes, defaults and exact column types are not
  carried into other formats; only the rows are.
- **NaN and Infinity into JSON.** JSON cannot represent them, so the conversion stops with
  an error instead of writing an invalid file.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | The data is the problem: syntax error, failed validation, or not representable in the target format |
| 2 | The command line is the problem: unknown option, unknown file extension, no target given |
| 3 | The filesystem is the problem: source missing, target exists without `--force`, cannot read or write |

Errors are printed to stderr, so `confconv -q a.json b.yaml && next-step` works in scripts.

## Development

```bash
git clone https://github.com/hmodymac8-droid/confconv.git
cd confconv
python -m venv .venv
.venv\Scripts\activate        # Windows;  source .venv/bin/activate  elsewhere
pip install -e ".[test]"
pytest
```

The tests run the real command on real files: every pair of formats that can hold records,
round trips through the formats, each kind of syntax and schema error, and the overwrite and dry-run behaviour.

## License

[MIT](LICENSE)
