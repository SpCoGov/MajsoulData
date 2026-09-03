from __future__ import annotations

import argparse
import json
import posixpath
import re
import shutil
import sys
import warnings
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin, urlsplit

warnings.filterwarnings(
    "ignore",
    message=r"urllib3 .* doesn't match a supported version!",
)

import requests
import UnityPy
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from src.client_data import (
    RAW_ASSET_SUBDIR,
    build_client_data_outputs,
    is_client_data_asset_path,
    is_repository_support_lua_asset_path,
)
from src.protocol import build_protocol_outputs


DEFAULT_CLIENT_URL = "https://game.maj-soul.com/1/"
DEFAULT_OUTPUT_DIR = "data"
DEFAULT_RAW_DIR = "raw"
DEFAULT_ASSET_DIR = "assets"
RAW_LUA_SUBDIR = Path("lua")
TABLES_SUBDIR = "tables"
LOCALES_SUBDIR = "locales"
DEFAULT_TEXTURE_PROFILES = ("ASTC", "DXT")
DEFAULT_ASSET_PREFIXES = (
    "LuaByte/Lua/Excels/",
    "LuaByte/Lua/Protol/",
)
LUA_XOR_KEY = b"wrelupqezdfrqdsd"
UNITY_FALLBACK_VERSION = "2022.3.62f2c1"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


UnityPy.config.FALLBACK_UNITY_VERSION = UNITY_FALLBACK_VERSION
warnings.filterwarnings("ignore", message="No valid Unity version found.*")


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    retries = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "HEAD"),
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def fetch_bytes(session: requests.Session, url: str, timeout: int) -> bytes:
    response = session.get(url, timeout=timeout)
    response.raise_for_status()
    return response.content


def fetch_text(session: requests.Session, url: str, timeout: int) -> str:
    response = session.get(url, timeout=timeout)
    response.raise_for_status()
    response.encoding = response.encoding or "utf-8"
    return response.text


def fetch_json(session: requests.Session, url: str, timeout: int) -> dict:
    response = session.get(url, timeout=timeout)
    response.raise_for_status()
    return response.json()


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def join_url(base: str, *parts: str) -> str:
    current = base.rstrip("/") + "/"
    for part in parts:
        current = urljoin(current, part.lstrip("/"))
    return current


def parse_client_html(html: str) -> dict[str, str | None]:
    loader_match = re.search(r"Build/([^\"']+?\.loader\.js)", html)
    product_match = re.search(r"productVersion\s*:\s*[\"']([^\"']+)[\"']", html)
    loader = loader_match.group(1) if loader_match else None
    issuer = None
    if loader and "-WebGL-release-" in loader:
        issuer = loader.split("-WebGL-release-", 1)[0]
    return {
        "loader": loader,
        "issuer": issuer,
        "product_version": product_match.group(1) if product_match else None,
    }


def default_client_settings_url(client_url: str, issuer: str | None) -> str:
    if issuer != "chs_t":
        raise ValueError(
            "Only chs_t can derive the client bundle settings URL automatically. "
            "Pass --client-settings-url for this locale."
        )
    return join_url(origin_of(client_url), "assetbundles/clientBundleSettings/chs_t-release.json")


def choose_url_entry(entries: list[dict]) -> str:
    if not entries:
        raise ValueError("URL entry list is empty")
    ordered = sorted(
        entries,
        key=lambda item: (item.get("Priority", 0), item.get("weight", 0)),
        reverse=True,
    )
    url = ordered[0].get("url")
    if not url:
        raise ValueError(f"URL entry has no url: {ordered[0]!r}")
    return url


def resolve_warehouse_url(client_settings: dict) -> str:
    warehouses = client_settings.get("warehouses") or []
    if not warehouses:
        raise ValueError("client bundle settings contains no warehouses")
    warehouse = warehouses[0]
    base_url = choose_url_entry(warehouse.get("urls") or [])
    setting_path = warehouse.get("warehouseSettingPath")
    if not setting_path:
        raise ValueError("warehouseSettingPath is missing")
    return join_url(base_url, setting_path)


def resolve_bundle_base_url(warehouse_settings: dict) -> str:
    base_url = choose_url_entry(warehouse_settings.get("urls") or [])
    bundle_path = warehouse_settings.get("bundlePath")
    if not bundle_path:
        raise ValueError("bundlePath is missing")
    bundle_base = join_url(base_url, bundle_path)
    return bundle_base.rstrip("/") + "/"


def resolve_texture_profile(
    session: requests.Session,
    bundle_base_url: str,
    profiles: Iterable[str],
    timeout: int,
) -> tuple[str, str, str]:
    errors: list[str] = []
    for profile in profiles:
        profile_base = join_url(bundle_base_url, profile) + "/"
        hash_url = join_url(profile_base, "bundle_hash.txt")
        try:
            bundle_hash = fetch_text(session, hash_url, timeout).strip()
        except requests.RequestException as exc:
            errors.append(f"{profile}: {exc}")
            continue
        if bundle_hash:
            return profile, profile_base, bundle_hash
        errors.append(f"{profile}: empty bundle_hash.txt")
    raise RuntimeError("No usable texture profile found. " + "; ".join(errors))


def parse_bundle_info(bundle_info_bytes: bytes) -> tuple[list[dict], list[dict]]:
    env = UnityPy.load(bundle_info_bytes)
    for obj in env.objects:
        if obj.type.name != "MonoBehaviour":
            continue
        tree = obj.read_typetree()
        if tree.get("m_Name") == "BundleInfoSO" or (
            "bundleInfos" in tree and "assetInfos" in tree
        ):
            return tree["bundleInfos"], tree["assetInfos"]
    raise ValueError("BundleInfoSO was not found in bundle_info_so.majset")


def asset_name_for_path(asset_path: str) -> str:
    name = posixpath.basename(asset_path)
    if name.endswith(".bytes"):
        name = name[: -len(".bytes")]
    return name


def text_asset_name_candidates(asset_path: str) -> tuple[str, ...]:
    name = posixpath.basename(asset_path)
    candidates = [
        asset_name_for_path(asset_path),
        name,
        posixpath.splitext(asset_name_for_path(asset_path))[0],
    ]
    return tuple(dict.fromkeys(candidate for candidate in candidates if candidate))


def output_asset_path(asset_path: str) -> str:
    if asset_path.endswith(".lua.bytes"):
        return asset_path[: -len(".bytes")]
    return asset_path


def raw_lua_asset_path(asset_path: str) -> str:
    return posixpath.join(RAW_LUA_SUBDIR.as_posix(), output_asset_path(asset_path))


def raw_client_asset_path(asset_path: str) -> str:
    return posixpath.join(RAW_ASSET_SUBDIR.as_posix(), asset_path)


def repeating_xor(data: bytes, key: bytes) -> bytes:
    if not key:
        raise ValueError("XOR key must not be empty")
    return bytes(byte ^ key[index % len(key)] for index, byte in enumerate(data))


def looks_like_lua_source(data: bytes) -> bool:
    if not data:
        return False
    sample = data[:4096]
    printable_count = sum(
        byte in (9, 10, 13) or 32 <= byte <= 126
        for byte in sample
    )
    if printable_count / len(sample) < 0.85:
        return False
    stripped = sample.lstrip()
    if stripped.startswith((b"--", b"local", b"return", b"module", b"function")):
        return True
    markers = (b"local", b"require", b"return", b"module", b"function", b"=")
    return sum(marker in sample for marker in markers) >= 2


def decode_lua_asset(data: bytes) -> bytes:
    if looks_like_lua_source(data):
        return data
    return repeating_xor(data, LUA_XOR_KEY)


@dataclass(frozen=True)
class DmapRange:
    start: int
    end: int


@dataclass(frozen=True)
class PackCall:
    values: list[Any]


@dataclass
class SheetSchema:
    fields: dict[str, int]
    defaults: list[Any]
    table_name: str | None = None


@dataclass
class ParsedSheet:
    schema: SheetSchema
    rows: list[dict[str, Any]]


class LuaLiteralParser:
    def __init__(self, source: str):
        self.source = source
        self.index = 0

    def parse(self) -> Any:
        value = self.parse_value()
        self.skip_space()
        if self.index != len(self.source):
            raise ValueError(f"Unexpected trailing Lua literal at {self.index}")
        return value

    def skip_space(self) -> None:
        while self.index < len(self.source) and self.source[self.index].isspace():
            self.index += 1

    def peek(self) -> str:
        return self.source[self.index] if self.index < len(self.source) else ""

    def consume(self, expected: str) -> None:
        self.skip_space()
        if not self.source.startswith(expected, self.index):
            raise ValueError(f"Expected {expected!r} at {self.index}")
        self.index += len(expected)

    def parse_value(self) -> Any:
        self.skip_space()
        char = self.peek()
        if char == "{":
            return self.parse_table()
        if char in {'"', "'"}:
            return self.parse_string()
        if char == "-" or char.isdigit():
            return self.parse_number()
        if char.isalpha() or char == "_":
            return self.parse_identifier_value()
        raise ValueError(f"Unexpected Lua value at {self.index}: {self.source[self.index:self.index + 20]!r}")

    def parse_identifier(self) -> str:
        self.skip_space()
        start = self.index
        if not (self.peek().isalpha() or self.peek() == "_"):
            raise ValueError(f"Expected identifier at {self.index}")
        self.index += 1
        while self.index < len(self.source):
            char = self.source[self.index]
            if not (char.isalnum() or char == "_"):
                break
            self.index += 1
        return self.source[start:self.index]

    def parse_identifier_value(self) -> Any:
        name = self.parse_identifier()
        if name == "nil":
            return None
        if name == "false":
            return False
        if name == "true":
            return True

        self.skip_space()
        if self.peek() == "[":
            self.consume("[")
            key = self.parse_value()
            self.consume("]")
            if not isinstance(key, int):
                raise ValueError(f"Dmap index must be an integer: {key!r}")
            return DmapRange(key // 1000, key % 1000)

        if self.peek() == "(":
            self.consume("(")
            args: list[Any] = []
            self.skip_space()
            if self.peek() != ")":
                while True:
                    if self.peek().isalpha() or self.peek() == "_":
                        checkpoint = self.index
                        identifier = self.parse_identifier()
                        self.skip_space()
                        if self.peek() not in "({[\"'-0123456789":
                            args.append(identifier)
                        else:
                            self.index = checkpoint
                            args.append(self.parse_value())
                    else:
                        args.append(self.parse_value())
                    self.skip_space()
                    if self.peek() != ",":
                        break
                    self.index += 1
            self.consume(")")
            if name == "b":
                if not args or not isinstance(args[0], list):
                    raise ValueError("Pack call must receive a Lua array as its first argument")
                return PackCall(args[0])
            if name == "c":
                return args[0] if args else []
            return args[0] if args else None

        return name

    def parse_string(self) -> str:
        quote = self.peek()
        self.index += 1
        chars: list[str] = []
        while self.index < len(self.source):
            char = self.source[self.index]
            self.index += 1
            if char == quote:
                return "".join(chars)
            if char != "\\":
                chars.append(char)
                continue
            if self.index >= len(self.source):
                raise ValueError("Unterminated Lua string escape")
            escaped = self.source[self.index]
            self.index += 1
            escapes = {
                "a": "\a",
                "b": "\b",
                "f": "\f",
                "n": "\n",
                "r": "\r",
                "t": "\t",
                "v": "\v",
                "\\": "\\",
                '"': '"',
                "'": "'",
            }
            if escaped in escapes:
                chars.append(escapes[escaped])
            elif escaped.isdigit():
                digits = escaped
                for _ in range(2):
                    if self.index < len(self.source) and self.source[self.index].isdigit():
                        digits += self.source[self.index]
                        self.index += 1
                    else:
                        break
                chars.append(chr(int(digits, 10)))
            else:
                chars.append(escaped)
        raise ValueError("Unterminated Lua string")

    def parse_number(self) -> int | float:
        self.skip_space()
        start = self.index
        if self.peek() == "-":
            self.index += 1
        while self.index < len(self.source) and self.source[self.index].isdigit():
            self.index += 1
        if self.index < len(self.source) and self.source[self.index] == ".":
            self.index += 1
            while self.index < len(self.source) and self.source[self.index].isdigit():
                self.index += 1
        if self.index < len(self.source) and self.source[self.index] in "eE":
            self.index += 1
            if self.index < len(self.source) and self.source[self.index] in "+-":
                self.index += 1
            while self.index < len(self.source) and self.source[self.index].isdigit():
                self.index += 1
        raw = self.source[start:self.index]
        return float(raw) if any(part in raw for part in ".eE") else int(raw)

    def parse_table_key(self) -> Any:
        self.consume("[")
        key = self.parse_value()
        self.consume("]")
        return key

    def parse_table(self) -> Any:
        self.consume("{")
        array_values: list[Any] = []
        keyed_values: dict[Any, Any] = {}
        next_array_index = 1

        while True:
            self.skip_space()
            if self.peek() == "}":
                self.index += 1
                break

            if self.peek() == "[":
                key = self.parse_table_key()
                self.consume("=")
                value = self.parse_value()
                keyed_values[key] = value
            elif self.peek().isalpha() or self.peek() == "_":
                checkpoint = self.index
                identifier = self.parse_identifier()
                self.skip_space()
                if self.peek() == "=":
                    self.index += 1
                    keyed_values[identifier] = self.parse_value()
                else:
                    self.index = checkpoint
                    array_values.append(self.parse_value())
            else:
                array_values.append(self.parse_value())

            self.skip_space()
            if self.peek() in ",;":
                self.index += 1

        if not keyed_values:
            return array_values
        for value in array_values:
            while next_array_index in keyed_values:
                next_array_index += 1
            keyed_values[next_array_index] = value
            next_array_index += 1
        return keyed_values


def parse_lua_literal(source: str) -> Any:
    return LuaLiteralParser(source).parse()


def lua_field_position(raw_index: int) -> int:
    index = abs(raw_index)
    return index % 1000 if index >= 1000 else index


def pack_excel_row(values: list[Any], fields: dict[str, int], defaults: list[Any]) -> dict[str, Any]:
    position_names: dict[int, list[str]] = defaultdict(list)
    for field_name, raw_index in fields.items():
        position_names[lua_field_position(raw_index)].append(field_name)

    max_position = max(position_names, default=0)
    row_values = {
        position: defaults[position - 1] if position - 1 < len(defaults) else None
        for position in range(1, max_position + 1)
    }

    skipped_positions: set[int] = set()
    position = 1
    for value in values:
        if isinstance(value, DmapRange):
            skipped_positions.update(range(value.start, value.end + 1))
            continue
        while position in skipped_positions:
            position += 1
        if position > max_position:
            break
        row_values[position] = (
            defaults[position - 1]
            if value is False and position - 1 < len(defaults)
            else value
        )
        position += 1

    row: dict[str, Any] = {}
    for field_name, raw_index in sorted(
        fields.items(),
        key=lambda item: (lua_field_position(item[1]), item[0]),
    ):
        row[field_name] = row_values.get(lua_field_position(raw_index))
    return row


def find_balanced(source: str, start: int, opening: str = "{", closing: str = "}") -> int:
    if source[start] != opening:
        raise ValueError(f"Expected {opening!r} at {start}")
    depth = 0
    index = start
    quote: str | None = None
    escaped = False
    while index < len(source):
        char = source[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        else:
            if char in {'"', "'"}:
                quote = char
            elif char == opening:
                depth += 1
            elif char == closing:
                depth -= 1
                if depth == 0:
                    return index + 1
        index += 1
    raise ValueError(f"Unbalanced {opening!r} at {start}")


def extract_local_tables(source: str) -> dict[str, Any]:
    tables: dict[str, Any] = {}
    for match in re.finditer(r"local\s+([A-Za-z_]\w*)\s*=\s*\{", source):
        name = match.group(1)
        table_start = match.end() - 1
        table_end = find_balanced(source, table_start)
        tables[name] = parse_lua_literal(source[table_start:table_end])
    return tables


def extract_schema(source: str) -> SheetSchema | None:
    locals_by_name = extract_local_tables(source)
    lang_match = re.search(
        r"return\s+\w+\([^)]*?,\s*([A-Za-z_]\w*)\s*,\s*([A-Za-z_]\w*)\s*,\s*\"([^\"]+)\"\s*\)",
        source,
    )
    if lang_match:
        fields_var, defaults_var, table_name = lang_match.groups()
    else:
        pack_match = re.search(
            r"return\s+\w+\([^)]*?,\s*([A-Za-z_]\w*)\s*,\s*([A-Za-z_]\w*)\s*\)",
            source,
        )
        if not pack_match:
            return None
        fields_var, defaults_var = pack_match.groups()
        table_name = None

    fields = locals_by_name.get(fields_var)
    defaults = locals_by_name.get(defaults_var)
    if not isinstance(fields, dict) or not isinstance(defaults, list):
        return None
    return SheetSchema(fields=fields, defaults=defaults, table_name=table_name)


def parse_assignment_rows(source: str, schema: SheetSchema) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for match in re.finditer(r"\b[A-Za-z_]\w*\[[^\]]+\]\s*=", source):
        parser = LuaLiteralParser(source[match.end():])
        value = parser.parse_value()
        if isinstance(value, PackCall):
            rows.append(pack_excel_row(value.values, schema.fields, schema.defaults))
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, PackCall):
                    rows.append(pack_excel_row(item.values, schema.fields, schema.defaults))
    return rows


def parse_lua_sheet(source: str, schema: SheetSchema | None = None) -> ParsedSheet:
    resolved_schema = schema or extract_schema(source)
    if resolved_schema is None:
        raise ValueError("Lua sheet schema was not found")
    return ParsedSheet(
        schema=resolved_schema,
        rows=parse_assignment_rows(source, resolved_schema),
    )


def parse_lua_module_name(lua_path: Path, root: Path) -> str:
    relative = lua_path.relative_to(root).with_suffix("")
    return "Excels." + ".".join(relative.parts)


def required_data_module(source: str) -> str | None:
    match = re.search(r'require\("((?:Excels\.Data\.)[^"]+)"\)', source)
    return match.group(1) if match else None


def module_to_table_path(module_name: str) -> tuple[str, str]:
    prefix = "Excels.Data."
    if not module_name.startswith(prefix):
        raise ValueError(f"Not an Excel data module: {module_name}")
    parts = module_name[len(prefix):].split(".")
    if len(parts) != 2:
        raise ValueError(f"Unexpected Excel data module path: {module_name}")
    return parts[0], parts[1]


def sorted_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def key(row: dict[str, Any]) -> tuple[int, Any]:
        row_id = row.get("id")
        return (0, row_id) if isinstance(row_id, (int, float, str)) else (1, 0)

    return sorted(rows, key=key)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )


def load_metadata(output_dir: Path) -> dict[str, Any]:
    meta_path = output_dir / "meta.json"
    if not meta_path.exists():
        return {}
    try:
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return metadata if isinstance(metadata, dict) else {}


def is_current_client_metadata(
    metadata: dict[str, Any],
    client_info: dict[str, str | None],
    bundle_profile: str,
    bundle_hash: str,
    asset_mode: str | None = None,
) -> bool:
    if metadata.get("bundle_profile") != bundle_profile:
        return False
    if metadata.get("bundle_hash") != bundle_hash:
        return False
    if asset_mode is not None and metadata.get("asset_mode") != asset_mode:
        return False
    product_version = client_info.get("product_version")
    return not product_version or metadata.get("product_version") == product_version


def parse_all_index(raw_excels_dir: Path) -> list[Any]:
    all_lua = raw_excels_dir / "All.lua"
    if not all_lua.exists():
        return []
    source = all_lua.read_text(encoding="utf-8", errors="surrogateescape")
    match = re.search(r"local\s+\w+\s*=\s*\{", source)
    if not match:
        return []
    table_start = match.end() - 1
    table_end = find_balanced(source, table_start)
    return parse_lua_literal(source[table_start:table_end])


def locale_field_name(module_name: str, index_entry: dict[str, Any], locale: str) -> str | None:
    prefix = "Excels.Langs."
    if not module_name.startswith(prefix):
        return None
    stem = module_name[len(prefix):]
    suffix = "_" + locale
    if not stem.endswith(suffix):
        return None
    table_name = index_entry.get("TableName")
    sheet_name = index_entry.get("SheetName")
    if not isinstance(table_name, str) or not isinstance(sheet_name, str):
        return None
    sheet_prefix = f"{table_name}_{sheet_name}_"
    stem_without_locale = stem[: -len(suffix)]
    if not stem_without_locale.startswith(sheet_prefix):
        return None
    return stem_without_locale[len(sheet_prefix):]


def localized_value(values: list[Any], index: Any) -> Any:
    if not isinstance(index, int) or index <= 0:
        return None
    position = index - 1
    if position >= len(values):
        return None
    return values[position]


def merge_localized_fields(
    row: dict[str, Any],
    index_entry: dict[str, Any] | None,
    locale_values: dict[str, list[Any]],
) -> dict[str, Any]:
    if not index_entry:
        return dict(row)

    merged = dict(row)
    localized_fields: dict[str, dict[str, Any]] = defaultdict(dict)

    for locale in ("chs", "chs_t", "jp", "en", "kr"):
        modules = index_entry.get(locale)
        if not isinstance(modules, list):
            continue
        for module_name in modules:
            if not isinstance(module_name, str):
                continue
            base_field = locale_field_name(module_name, index_entry, locale)
            if not base_field:
                continue
            row_field = f"{base_field}_{locale}"
            index = merged.pop(row_field, None)
            value = localized_value(locale_values.get(module_name, []), index)
            if value is not None:
                localized_fields[base_field][locale] = value

    for field_name, translations in localized_fields.items():
        if translations:
            merged[field_name] = dict(translations)
    return merged


def load_locale_values(langs_dir: Path) -> tuple[dict[str, list[Any]], int]:
    locale_values: dict[str, list[Any]] = {}
    if not langs_dir.exists():
        return locale_values, 0

    for lua_path in sorted(langs_dir.glob("*.lua")):
        source = lua_path.read_text(encoding="utf-8", errors="surrogateescape")
        match = re.search(r"local\s+\w+\s*=\s*\{", source)
        if not match:
            continue
        table_start = match.end() - 1
        table_end = find_balanced(source, table_start)
        module_name = "Excels.Langs." + lua_path.stem
        values = parse_lua_literal(source[table_start:table_end])
        if isinstance(values, list):
            locale_values[module_name] = values
    return locale_values, len(locale_values)


def build_json_outputs(
    output_dir: Path,
    raw_dir: Path,
    metadata: dict[str, Any],
) -> dict[str, int]:
    raw_excels_dir = raw_dir / RAW_LUA_SUBDIR / "LuaByte" / "Lua" / "Excels"
    data_dir = raw_excels_dir / "Data"
    langs_dir = raw_excels_dir / "Langs"
    tables_dir = output_dir / TABLES_SUBDIR
    locales_dir = output_dir / LOCALES_SUBDIR

    for generated_dir in (tables_dir, locales_dir):
        if generated_dir.exists():
            shutil.rmtree(generated_dir)

    index = parse_all_index(raw_excels_dir)
    index_by_table = {
        (entry.get("TableName"), entry.get("SheetName")): entry
        for entry in index
        if isinstance(entry, dict)
    }
    locale_values, locale_source_count = load_locale_values(langs_dir)

    schemas: dict[str, SheetSchema] = {}
    rows_by_module: dict[str, list[dict[str, Any]]] = defaultdict(list)
    lua_files = sorted(data_dir.rglob("*.lua")) if data_dir.exists() else []

    for lua_path in lua_files:
        source = lua_path.read_text(encoding="utf-8", errors="surrogateescape")
        schema = extract_schema(source)
        if schema is None:
            continue
        module_name = parse_lua_module_name(lua_path, raw_excels_dir)
        schemas[module_name] = schema
        rows_by_module[module_name].extend(parse_lua_sheet(source, schema).rows)

    skipped_modules = 0
    for lua_path in lua_files:
        module_name = parse_lua_module_name(lua_path, raw_excels_dir)
        if module_name in schemas:
            continue
        source = lua_path.read_text(encoding="utf-8", errors="surrogateescape")
        target_module = required_data_module(source)
        if not target_module or target_module not in schemas:
            skipped_modules += 1
            continue
        rows_by_module[target_module].extend(parse_lua_sheet(source, schemas[target_module]).rows)

    table_count = 0
    row_count = 0
    for module_name, rows in sorted(rows_by_module.items()):
        table_name, sheet_name = module_to_table_path(module_name)
        out_path = tables_dir / table_name / f"{sheet_name}.json"
        index_entry = index_by_table.get((table_name, sheet_name))
        final_rows = sorted_rows(
            [
                merge_localized_fields(row, index_entry, locale_values)
                for row in rows
            ]
        )
        write_json(out_path, final_rows)
        table_count += 1
        row_count += len(final_rows)

    write_json(output_dir / "index.json", index)
    write_json(
        output_dir / "meta.json",
        {
            **metadata,
            "tables": table_count,
            "rows": row_count,
            "locale_sources": locale_source_count,
            "skipped_modules": skipped_modules,
        },
    )
    return {
        "tables": table_count,
        "rows": row_count,
        "locale_sources": locale_source_count,
        "skipped_modules": skipped_modules,
    }


def group_assets_by_bundle(
    asset_infos: Iterable[dict],
    prefixes: tuple[str, ...],
) -> dict[int, list[str]]:
    grouped: dict[int, list[str]] = defaultdict(list)
    for asset in asset_infos:
        asset_path = asset.get("assetPath")
        owner_bundle_index = asset.get("ownerBundleIndex")
        if not isinstance(asset_path, str) or owner_bundle_index is None:
            continue
        if not asset_path.endswith(".lua.bytes"):
            continue
        if not any(asset_path.startswith(prefix) for prefix in prefixes):
            continue
        grouped[int(owner_bundle_index)].append(asset_path)
    return dict(sorted(grouped.items()))


def group_selected_assets_by_bundle(
    asset_infos: Iterable[dict],
    lua_prefixes: tuple[str, ...],
) -> dict[int, list[str]]:
    grouped: dict[int, list[str]] = defaultdict(list)
    for asset in asset_infos:
        asset_path = asset.get("assetPath")
        owner_bundle_index = asset.get("ownerBundleIndex")
        if not isinstance(asset_path, str) or owner_bundle_index is None:
            continue
        if not is_selected_text_asset_path(asset_path, lua_prefixes):
            continue
        grouped[int(owner_bundle_index)].append(asset_path)
    return dict(sorted(grouped.items()))


def is_selected_text_asset_path(asset_path: str, lua_prefixes: tuple[str, ...]) -> bool:
    if asset_path.endswith(".lua.bytes") and any(
        asset_path.startswith(prefix) for prefix in lua_prefixes
    ):
        return True
    return is_repository_support_lua_asset_path(asset_path) or is_client_data_asset_path(asset_path)


def safe_output_path(output_dir: Path, asset_path: str) -> Path:
    if "\\" in asset_path:
        raise ValueError(f"Backslashes are not allowed in asset paths: {asset_path}")
    parts = [part for part in asset_path.split("/") if part]
    if not parts or any(part in {".", ".."} for part in parts):
        raise ValueError(f"Unsafe asset path: {asset_path}")
    target = output_dir.joinpath(*parts).resolve()
    root = output_dir.resolve()
    if target != root and root not in target.parents:
        raise ValueError(f"Asset path escapes output directory: {asset_path}")
    return target


def reset_output_dir(output_dir: Path) -> None:
    root = Path.cwd().resolve()
    target = output_dir.resolve()
    if target == root or (target != root and root not in target.parents):
        raise ValueError(f"Refusing to reset unsafe output directory: {target}")
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)


def text_asset_bytes(text_asset) -> bytes:
    script = getattr(text_asset, "m_Script", "")
    if isinstance(script, bytes):
        return script
    if isinstance(script, str):
        return script.encode("utf-8", "surrogateescape")
    raise TypeError(f"Unsupported TextAsset script type: {type(script)!r}")


def extract_text_assets(bundle_bytes: bytes) -> dict[str, bytes]:
    env = UnityPy.load(bundle_bytes)
    assets: dict[str, bytes] = {}
    for obj in env.objects:
        if obj.type.name != "TextAsset":
            continue
        text_asset = obj.read()
        name = getattr(text_asset, "m_Name", "")
        if name:
            assets[name] = text_asset_bytes(text_asset)
    return assets


def safe_file_component(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    return (cleaned or "unnamed")[:120]


def audio_sample_extension(sample_name: str) -> str:
    extension = Path(sample_name).suffix.lower()
    return extension if extension in {".m4a", ".ogg", ".wav"} else ".wav"


def unity_asset_output_path(
    asset_dir: Path,
    type_name: str,
    bundle_index: int,
    path_id: int,
    name: str,
    extension: str,
) -> Path:
    filename = f"{path_id}_{safe_file_component(name)}{extension}"
    return safe_output_path(asset_dir, f"{type_name}/{bundle_index:04d}/{filename}")


def export_unity_objects(
    bundle_bytes: bytes, asset_dir: Path, bundle_index: int
) -> dict[str, int]:
    counts = {"Texture2D": 0, "Sprite": 0, "AudioClip": 0, "failed": 0}
    env = UnityPy.load(bundle_bytes)
    for obj in env.objects:
        type_name = obj.type.name
        if type_name not in counts or type_name == "failed":
            continue
        try:
            value = obj.read()
            name = getattr(value, "m_Name", "") or type_name
            if type_name in {"Texture2D", "Sprite"}:
                target = unity_asset_output_path(
                    asset_dir, type_name, bundle_index, obj.path_id, name, ".png"
                )
                target.parent.mkdir(parents=True, exist_ok=True)
                value.image.save(target, "PNG")
                counts[type_name] += 1
                continue

            samples = value.samples
            if not samples:
                raise ValueError("AudioClip contains no samples")
            if len(samples) == 1:
                sample_name, sample_data = next(iter(samples.items()))
                target = unity_asset_output_path(
                    asset_dir,
                    type_name,
                    bundle_index,
                    obj.path_id,
                    name,
                    audio_sample_extension(sample_name),
                )
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(sample_data)
                counts[type_name] += 1
            else:
                clip_dir = unity_asset_output_path(
                    asset_dir, type_name, bundle_index, obj.path_id, name, ""
                )
                clip_dir.mkdir(parents=True, exist_ok=True)
                for index, (sample_name, sample_data) in enumerate(samples.items()):
                    sample = safe_file_component(Path(sample_name).stem)
                    extension = audio_sample_extension(sample_name)
                    (clip_dir / f"{index:03d}_{sample}{extension}").write_bytes(sample_data)
                    counts[type_name] += 1
        except Exception as exc:
            counts["failed"] += 1
            print(f"  Failed {type_name} path_id={obj.path_id}: {exc}", file=sys.stderr)
    return counts


def extract_unity_assets(
    session: requests.Session,
    profile_base_url: str,
    bundle_infos: list[dict],
    asset_dir: Path,
    timeout: int,
) -> dict[str, int]:
    totals = {"Texture2D": 0, "Sprite": 0, "AudioClip": 0, "failed": 0}
    total_bundles = len(bundle_infos)
    for bundle_index, bundle_info in enumerate(bundle_infos):
        bundle_name = bundle_info["name"]
        print(f"[{bundle_index + 1}/{total_bundles}] Exporting {bundle_name}")
        bundle_bytes = fetch_bytes(
            session, join_url(profile_base_url, bundle_name), timeout
        )
        counts = export_unity_objects(bundle_bytes, asset_dir, bundle_index)
        for key, count in counts.items():
            totals[key] += count
    return totals


def collect_web_audio_paths(output_dir: Path) -> list[str]:
    def rows(relative_path: str) -> list[dict]:
        path = output_dir / TABLES_SUBDIR / relative_path
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []

    paths: set[str] = set()

    def add(path: Any, prefix: str = "") -> None:
        if not isinstance(path, str) or not path:
            return
        logical_path = posixpath.join(prefix, path).lstrip("/")
        if posixpath.splitext(logical_path)[1].lower() not in {
            ".m4a",
            ".mp3",
            ".ogg",
            ".wav",
        }:
            logical_path += ".mp3"
        paths.add(logical_path)

    for relative_path in (
        "audio/audio.json",
        "spot/audio_spot.json",
        "voice/event.json",
        "voice/spot.json",
    ):
        for row in rows(relative_path):
            add(row.get("path"))

    for row in rows("audio/bgm.json"):
        add(row.get("path"), "audio")

    sound_folders = {
        row.get("sound"): row.get("sound_folder")
        for row in rows("item_definition/character.json")
        if row.get("sound") is not None and row.get("sound_folder")
    }
    for row in rows("voice/sound.json"):
        folder = sound_folders.get(row.get("id"))
        if folder:
            add(row.get("path"), f"audio/sound/{folder}")

    return sorted(paths)


def extract_web_audio(
    profile_base_url: str,
    audio_paths: list[str],
    asset_dir: Path,
    timeout: int,
) -> tuple[int, int]:
    def download_chunk(chunk: list[str]) -> tuple[int, list[str]]:
        session = make_session()
        downloaded = 0
        missing: list[str] = []
        for audio_path in chunk:
            try:
                data = fetch_bytes(
                    session,
                    join_url(
                        profile_base_url,
                        posixpath.join("Assets/MyAssets", audio_path),
                    ),
                    timeout,
                )
                target = safe_output_path(
                    asset_dir, posixpath.join("AudioClip", audio_path)
                )
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                downloaded += 1
            except (OSError, requests.RequestException, ValueError):
                missing.append(audio_path)
        return downloaded, missing

    if not audio_paths:
        return 0, 0
    worker_count = min(8, len(audio_paths))
    chunks = [audio_paths[index::worker_count] for index in range(worker_count)]
    downloaded = 0
    missing: list[str] = []
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        for chunk_downloaded, chunk_missing in executor.map(download_chunk, chunks):
            downloaded += chunk_downloaded
            missing.extend(chunk_missing)
    for audio_path in missing[:20]:
        print(f"  Missing web audio: {audio_path}", file=sys.stderr)
    if len(missing) > 20:
        print(f"  ... and {len(missing) - 20} more missing audio files", file=sys.stderr)
    return downloaded, len(missing)


def write_asset(raw_dir: Path, asset_path: str, data: bytes) -> None:
    if asset_path.startswith("LuaByte/Lua/"):
        target = safe_output_path(raw_dir, raw_lua_asset_path(asset_path))
        data = decode_lua_asset(data)
    else:
        target = safe_output_path(raw_dir, raw_client_asset_path(asset_path))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def extract_selected_assets(
    session: requests.Session,
    profile_base_url: str,
    bundle_infos: list[dict],
    grouped_assets: dict[int, list[str]],
    raw_dir: Path,
    timeout: int,
) -> tuple[int, int]:
    extracted_count = 0
    missing_count = 0
    total_bundles = len(grouped_assets)

    for position, (bundle_index, asset_paths) in enumerate(grouped_assets.items(), start=1):
        bundle_name = bundle_infos[bundle_index]["name"]
        bundle_url = join_url(profile_base_url, bundle_name)
        print(f"[{position}/{total_bundles}] Fetching {bundle_name}")
        bundle_bytes = fetch_bytes(session, bundle_url, timeout)
        text_assets = extract_text_assets(bundle_bytes)

        for asset_path in asset_paths:
            data = None
            for text_name in text_asset_name_candidates(asset_path):
                data = text_assets.get(text_name)
                if data is not None:
                    break
            if data is None:
                candidates = ", ".join(text_asset_name_candidates(asset_path))
                print(f"  Missing TextAsset for {asset_path} ({candidates}) in {bundle_name}")
                missing_count += 1
                continue
            write_asset(raw_dir, asset_path, data)
            extracted_count += 1

    return extracted_count, missing_count


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download raw Mahjong Soul Unity Lua TextAssets into data/."
    )
    parser.add_argument("--client-url", default=DEFAULT_CLIENT_URL)
    parser.add_argument("--client-settings-url")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--raw-dir",
        default=DEFAULT_RAW_DIR,
        help="Directory for extracted raw Unity TextAssets.",
    )
    parser.add_argument(
        "--asset-dir",
        default=DEFAULT_ASSET_DIR,
        help="Directory for exported Texture2D, Sprite, and AudioClip assets.",
    )
    parser.add_argument(
        "--asset-mode",
        choices=("text", "all"),
        default="text",
        help="Use 'all' to scan every bundle for Texture2D, Sprite, and AudioClip assets.",
    )
    parser.add_argument(
        "--texture-profile",
        action="append",
        dest="texture_profiles",
        help="Texture profile to try, e.g. ASTC or DXT. Can be passed multiple times.",
    )
    parser.add_argument(
        "--asset-prefix",
        action="append",
        dest="asset_prefixes",
        help="Logical Unity asset prefix to save. Can be passed multiple times.",
    )
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument(
        "--skip-if-current",
        action="store_true",
        help="Skip extraction when data/meta.json already matches the remote client.",
    )
    parser.add_argument(
        "--keep-existing",
        action="store_true",
        help="Do not clear the output directory before writing assets.",
    )
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    session = make_session()
    client_url = args.client_url
    output_dir = Path(args.output_dir)
    raw_dir = Path(args.raw_dir)
    asset_dir = Path(args.asset_dir)
    texture_profiles = tuple(args.texture_profiles or DEFAULT_TEXTURE_PROFILES)
    asset_prefixes = tuple(args.asset_prefixes or DEFAULT_ASSET_PREFIXES)

    print(f"Client URL: {client_url}")
    html = fetch_text(session, client_url, args.timeout)
    client_info = parse_client_html(html)
    print(
        "Unity client: "
        f"issuer={client_info.get('issuer') or 'unknown'}, "
        f"productVersion={client_info.get('product_version') or 'unknown'}, "
        f"loader={client_info.get('loader') or 'unknown'}"
    )

    client_settings_url = args.client_settings_url or default_client_settings_url(
        client_url,
        client_info.get("issuer"),
    )
    print(f"Client bundle settings: {client_settings_url}")
    client_settings = fetch_json(session, client_settings_url, args.timeout)

    warehouse_url = resolve_warehouse_url(client_settings)
    print(f"Warehouse settings: {warehouse_url}")
    warehouse_settings = fetch_json(session, warehouse_url, args.timeout)

    bundle_base_url = resolve_bundle_base_url(warehouse_settings)
    profile, profile_base_url, bundle_hash = resolve_texture_profile(
        session,
        bundle_base_url,
        texture_profiles,
        args.timeout,
    )
    print(f"Bundle profile: {profile}")
    print(f"Bundle hash: {bundle_hash}")

    metadata = load_metadata(output_dir)
    if (
        args.skip_if_current
        and is_current_client_metadata(
            metadata,
            client_info,
            profile,
            bundle_hash,
            args.asset_mode,
        )
        and (args.asset_mode != "all" or (asset_dir / "manifest.json").exists())
    ):
        print("Remote client matches data/meta.json; skipping data update.")
        return 0

    bundle_info_url = join_url(profile_base_url, "bundle_info_so.majset")
    print(f"Bundle info: {bundle_info_url}")
    bundle_info_bytes = fetch_bytes(session, bundle_info_url, args.timeout)
    bundle_infos, asset_infos = parse_bundle_info(bundle_info_bytes)

    grouped_assets = group_selected_assets_by_bundle(asset_infos, asset_prefixes)
    asset_count = sum(len(paths) for paths in grouped_assets.values())
    print(f"Selected assets: {asset_count} files in {len(grouped_assets)} bundles")
    for prefix in asset_prefixes:
        print(f"  lua prefix: {prefix}")
    print("  client data: docs json, spot scripts, deco json, spine csv, UI atlas config")
    print("  support lua: ProtoDeclare, ExcelDeclare, Game/**/Data, Game/UIData")

    if not args.keep_existing:
        reset_output_dir(output_dir)
        reset_output_dir(raw_dir)
        if args.asset_mode == "all":
            reset_output_dir(asset_dir)
    else:
        output_dir.mkdir(parents=True, exist_ok=True)
        raw_dir.mkdir(parents=True, exist_ok=True)

    extracted_count, missing_count = extract_selected_assets(
        session,
        profile_base_url,
        bundle_infos,
        grouped_assets,
        raw_dir.resolve(),
        args.timeout,
    )
    print(f"Extracted: {extracted_count}")
    if missing_count:
        print(f"Missing: {missing_count}", file=sys.stderr)
        return 1
    unity_asset_counts: dict[str, int] = {}
    if args.asset_mode == "all":
        unity_asset_counts = extract_unity_assets(
            session,
            profile_base_url,
            bundle_infos,
            asset_dir.resolve(),
            args.timeout,
        )
    json_counts = build_json_outputs(
        output_dir.resolve(),
        raw_dir.resolve(),
        {
            "client_url": client_url,
            "issuer": client_info.get("issuer"),
            "product_version": client_info.get("product_version"),
            "loader": client_info.get("loader"),
            "bundle_profile": profile,
            "bundle_hash": bundle_hash,
            "raw_dir": raw_dir.as_posix(),
            "asset_mode": args.asset_mode,
        },
    )
    if args.asset_mode == "all":
        audio_paths = collect_web_audio_paths(output_dir.resolve())
        print(f"Web audio paths: {len(audio_paths)}")
        downloaded, missing = extract_web_audio(
            profile_base_url,
            audio_paths,
            asset_dir.resolve(),
            args.timeout,
        )
        unity_asset_counts["AudioClip"] += downloaded
        unity_asset_counts["AudioClipMissing"] = missing
        write_json(
            asset_dir.resolve() / "manifest.json",
            {
                "product_version": client_info.get("product_version"),
                "bundle_profile": profile,
                "bundle_hash": bundle_hash,
                "bundles": len(bundle_infos),
                **unity_asset_counts,
            },
        )
        print(
            "Exported Unity assets: "
            + ", ".join(f"{key}={value}" for key, value in unity_asset_counts.items())
        )
    print(
        "Generated JSON: "
        f"{json_counts['tables']} tables, "
        f"{json_counts['rows']} rows, "
        f"{json_counts['locale_sources']} locale sources merged"
    )
    if json_counts["skipped_modules"]:
        print(f"Skipped Lua modules: {json_counts['skipped_modules']}")
    protocol_counts = build_protocol_outputs(output_dir.resolve(), raw_dir.resolve())
    print(
        "Generated protocol: "
        f"{protocol_counts['proto_files']} proto files, "
        f"{protocol_counts['messages']} messages, "
        f"{protocol_counts['enums']} enums, "
        f"{protocol_counts['fields']} fields, "
        f"doc={protocol_counts['doc']}"
    )
    client_counts = build_client_data_outputs(output_dir.resolve(), raw_dir.resolve())
    print(
        "Generated client data: "
        f"{sum(client_counts.values())} files "
        f"({', '.join(f'{key}={value}' for key, value in client_counts.items())})"
    )
    meta_path = output_dir.resolve() / "meta.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["protocol"] = protocol_counts
        meta["client_data"] = client_counts
        if unity_asset_counts:
            meta["unity_assets"] = unity_asset_counts
        write_json(meta_path, meta)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
