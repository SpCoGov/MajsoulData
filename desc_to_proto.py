#!/usr/bin/env python3
"""Convert a protobuf descriptor set into one readable .proto file."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from google.protobuf import descriptor_pb2


FIELD_TYPES = {
    descriptor_pb2.FieldDescriptorProto.TYPE_DOUBLE: "double",
    descriptor_pb2.FieldDescriptorProto.TYPE_FLOAT: "float",
    descriptor_pb2.FieldDescriptorProto.TYPE_INT64: "int64",
    descriptor_pb2.FieldDescriptorProto.TYPE_UINT64: "uint64",
    descriptor_pb2.FieldDescriptorProto.TYPE_INT32: "int32",
    descriptor_pb2.FieldDescriptorProto.TYPE_FIXED64: "fixed64",
    descriptor_pb2.FieldDescriptorProto.TYPE_FIXED32: "fixed32",
    descriptor_pb2.FieldDescriptorProto.TYPE_BOOL: "bool",
    descriptor_pb2.FieldDescriptorProto.TYPE_STRING: "string",
    descriptor_pb2.FieldDescriptorProto.TYPE_BYTES: "bytes",
    descriptor_pb2.FieldDescriptorProto.TYPE_UINT32: "uint32",
    descriptor_pb2.FieldDescriptorProto.TYPE_SFIXED32: "sfixed32",
    descriptor_pb2.FieldDescriptorProto.TYPE_SFIXED64: "sfixed64",
    descriptor_pb2.FieldDescriptorProto.TYPE_SINT32: "sint32",
    descriptor_pb2.FieldDescriptorProto.TYPE_SINT64: "sint64",
}


def indent(level: int, text: str) -> str:
    return "  " * level + text


def field_type(field: descriptor_pb2.FieldDescriptorProto) -> str:
    if field.type in (field.TYPE_MESSAGE, field.TYPE_ENUM, field.TYPE_GROUP):
        return field.type_name
    return FIELD_TYPES[field.type]


def default_json_name(name: str) -> str:
    result: list[str] = []
    capitalize = False
    for char in name:
        if char == "_":
            capitalize = True
        else:
            result.append(char.upper() if capitalize else char)
            capitalize = False
    return "".join(result)


def field_options(field: descriptor_pb2.FieldDescriptorProto) -> str:
    options: list[str] = []
    if field.HasField("default_value"):
        value = field.default_value
        if field.type == field.TYPE_STRING:
            value = json.dumps(value, ensure_ascii=False)
        elif field.type == field.TYPE_BYTES:
            value = f'"{value}"'
        options.append(f"default = {value}")
    if field.options.HasField("packed"):
        options.append(f"packed = {str(field.options.packed).lower()}")
    if field.options.deprecated:
        options.append("deprecated = true")
    if field.HasField("json_name") and field.json_name != default_json_name(field.name):
        options.append(f"json_name = {json.dumps(field.json_name, ensure_ascii=False)}")
    return f" [{', '.join(options)}]" if options else ""


def render_enum(enum: descriptor_pb2.EnumDescriptorProto, level: int) -> list[str]:
    lines = [indent(level, f"enum {enum.name} {{")]
    if enum.options.allow_alias:
        lines.append(indent(level + 1, "option allow_alias = true;"))
    if enum.options.deprecated:
        lines.append(indent(level + 1, "option deprecated = true;"))
    for value in enum.value:
        suffix = " [deprecated = true]" if value.options.deprecated else ""
        lines.append(indent(level + 1, f"{value.name} = {value.number}{suffix};"))
    if enum.reserved_range:
        ranges = [
            str(item.start) if item.start == item.end else f"{item.start} to {item.end}"
            for item in enum.reserved_range
        ]
        lines.append(indent(level + 1, f"reserved {', '.join(ranges)};"))
    if enum.reserved_name:
        names = ", ".join(json.dumps(name) for name in enum.reserved_name)
        lines.append(indent(level + 1, f"reserved {names};"))
    lines.append(indent(level, "}"))
    return lines


def render_message(
    message: descriptor_pb2.DescriptorProto,
    full_name: str,
    syntax: str,
    level: int,
) -> list[str]:
    lines = [indent(level, f"message {message.name} {{")]
    if message.options.deprecated:
        lines.append(indent(level + 1, "option deprecated = true;"))

    map_entries = {
        f"{full_name}.{nested.name}": nested
        for nested in message.nested_type
        if nested.options.map_entry
    }
    for enum in message.enum_type:
        lines.extend(render_enum(enum, level + 1))
        lines.append("")
    for nested in message.nested_type:
        if not nested.options.map_entry:
            lines.extend(
                render_message(nested, f"{full_name}.{nested.name}", syntax, level + 1)
            )
            lines.append("")

    synthetic_oneofs = {
        field.oneof_index
        for field in message.field
        if field.proto3_optional and field.HasField("oneof_index")
    }
    oneof_fields: dict[int, list[descriptor_pb2.FieldDescriptorProto]] = {}
    normal_fields: list[descriptor_pb2.FieldDescriptorProto] = []
    for field in message.field:
        if field.HasField("oneof_index") and field.oneof_index not in synthetic_oneofs:
            oneof_fields.setdefault(field.oneof_index, []).append(field)
        else:
            normal_fields.append(field)

    def render_field(field: descriptor_pb2.FieldDescriptorProto, in_oneof: bool = False) -> str:
        map_entry = map_entries.get(field.type_name)
        if map_entry is not None:
            key, value = map_entry.field
            type_name = f"map<{field_type(key)}, {field_type(value)}>"
            label = ""
        else:
            type_name = field_type(field)
            if in_oneof:
                label = ""
            elif field.proto3_optional:
                label = "optional "
            elif field.label == field.LABEL_REPEATED:
                label = "repeated "
            elif syntax != "proto3" and field.label == field.LABEL_REQUIRED:
                label = "required "
            elif syntax != "proto3" and field.label == field.LABEL_OPTIONAL:
                label = "optional "
            else:
                label = ""
        return f"{label}{type_name} {field.name} = {field.number}{field_options(field)};"

    for field in normal_fields:
        lines.append(indent(level + 1, render_field(field)))
    for index, fields in sorted(oneof_fields.items()):
        lines.append(indent(level + 1, f"oneof {message.oneof_decl[index].name} {{"))
        for field in fields:
            lines.append(indent(level + 2, render_field(field, in_oneof=True)))
        lines.append(indent(level + 1, "}"))

    if message.extension_range:
        ranges = []
        for item in message.extension_range:
            end = "max" if item.end == 536_870_912 else str(item.end - 1)
            ranges.append(str(item.start) if end == str(item.start) else f"{item.start} to {end}")
        lines.append(indent(level + 1, f"extensions {', '.join(ranges)};"))
    if message.reserved_range:
        ranges = []
        for item in message.reserved_range:
            end = item.end - 1
            ranges.append(str(item.start) if item.start == end else f"{item.start} to {end}")
        lines.append(indent(level + 1, f"reserved {', '.join(ranges)};"))
    if message.reserved_name:
        names = ", ".join(json.dumps(name) for name in message.reserved_name)
        lines.append(indent(level + 1, f"reserved {names};"))

    while len(lines) > 1 and not lines[-1]:
        lines.pop()
    lines.append(indent(level, "}"))
    return lines


def render_service(service: descriptor_pb2.ServiceDescriptorProto) -> list[str]:
    lines = [f"service {service.name} {{"]
    if service.options.deprecated:
        lines.append(indent(1, "option deprecated = true;"))
    for method in service.method:
        request = ("stream " if method.client_streaming else "") + method.input_type
        response = ("stream " if method.server_streaming else "") + method.output_type
        if method.options.deprecated:
            lines.append(indent(1, f"rpc {method.name} ({request}) returns ({response}) {{"))
            lines.append(indent(2, "option deprecated = true;"))
            lines.append(indent(1, "}"))
        else:
            lines.append(indent(1, f"rpc {method.name} ({request}) returns ({response});"))
    lines.append("}")
    return lines


def render_imports(files: list[descriptor_pb2.FileDescriptorProto]) -> list[str]:
    included = {file.name for file in files}
    imports: dict[str, str] = {}
    priority = {"": 0, "weak ": 1, "public ": 2}
    for file in files:
        public = set(file.public_dependency)
        weak = set(file.weak_dependency)
        for index, dependency in enumerate(file.dependency):
            if dependency in included:
                continue
            modifier = "public " if index in public else "weak " if index in weak else ""
            if priority[modifier] > priority.get(imports.get(dependency, ""), 0):
                imports[dependency] = modifier
            else:
                imports.setdefault(dependency, modifier)
    return [f'import {modifier}"{dependency}";' for dependency, modifier in sorted(imports.items())]


def convert(source: Path, package: str) -> str:
    descriptor_set = descriptor_pb2.FileDescriptorSet()
    descriptor_set.ParseFromString(source.read_bytes())
    if not descriptor_set.file:
        raise ValueError("descriptor set contains no files")

    files = [file for file in descriptor_set.file if file.package == package]
    if not files:
        packages = sorted({file.package or "<empty>" for file in descriptor_set.file})
        raise ValueError(f"package {package!r} not found; available packages: {packages}")
    syntaxes = {file.syntax or "proto2" for file in files}
    if len(syntaxes) != 1:
        raise ValueError(f"cannot merge files with different syntaxes: {sorted(syntaxes)}")
    syntax = syntaxes.pop()

    lines = [f'syntax = "{syntax}";', ""]
    if package:
        lines.extend([f"package {package};", ""])
    imports = render_imports(files)
    if imports:
        lines.extend([*imports, ""])

    for file in files:
        prefix = f".{file.package}" if file.package else ""
        for enum in file.enum_type:
            lines.extend([*render_enum(enum, 0), ""])
        for message in file.message_type:
            lines.extend([*render_message(message, f"{prefix}.{message.name}", syntax, 0), ""])
        for service in file.service:
            lines.extend([*render_service(service), ""])
    return "\n".join(lines).rstrip() + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="input FileDescriptorSet")
    parser.add_argument("output", type=Path, help="output .proto path")
    parser.add_argument("--package", default="lq", help="package to merge (default: lq)")
    args = parser.parse_args()

    output = convert(args.source, args.package)
    args.output.write_text(output, encoding="utf-8", newline="\n")
    print(f"wrote {args.output} ({len(output.encode('utf-8'))} bytes)")


if __name__ == "__main__":
    main()
