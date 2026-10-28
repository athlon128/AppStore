#!/usr/bin/env python3
"""Validate the pinned upstream schema plus the repository's publishing rules.

The upstream Draft 7 schema uses only the keywords handled below. This deliberately
small interpreter fails closed if the vendored schema introduces a new keyword.
No third-party packages or network access are needed.
"""

import argparse
import datetime as dt
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

SCHEMA_PATH = Path(__file__).with_name("sidestore-source.schema.json")


class ValidationError(ValueError):
    pass


def fail(path, message):
    raise ValidationError(f"{path}: {message}")


def no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError(f"JSON: 重复字段 {key!r}")
        result[key] = value
    return result


def load_json(path):
    with Path(path).open(encoding="utf-8") as stream:
        return json.load(stream, object_pairs_hook=no_duplicate_keys)


def check_schema(value, schema, root, path="$"):
    supported = {"$ref", "$schema", "definitions", "description", "deprecated",
                 "type", "enum", "required", "properties", "additionalProperties", "items"}
    unknown = set(schema) - supported
    if unknown:
        fail(path, f"校验器不支持 schema 关键字 {sorted(unknown)}")
    if "$ref" in schema:
        ref = schema["$ref"]
        if not ref.startswith("#/definitions/"):
            fail(path, f"不支持外部 schema 引用 {ref}")
        return check_schema(value, root["definitions"][ref.rsplit("/", 1)[1]], root, path)
    types = {"object": dict, "array": list, "string": str, "boolean": bool,
             "number": (int, float)}
    kind = schema.get("type")
    if kind and (not isinstance(value, types[kind]) or
                 (kind == "number" and isinstance(value, bool))):
        fail(path, f"需要 {kind}，实际为 {type(value).__name__}")
    if "enum" in schema and value not in schema["enum"]:
        fail(path, f"必须为 {schema['enum']} 之一")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                fail(f"{path}.{key}", "缺少必需字段")
        props = schema.get("properties", {})
        for key, item in value.items():
            if key in props:
                check_schema(item, props[key], root, f"{path}.{key}")
            elif schema.get("additionalProperties") is False:
                fail(f"{path}.{key}", "官方 schema 不支持此字段")
    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            check_schema(item, schema["items"], root, f"{path}[{index}]")


def nonempty(value, path):
    if not isinstance(value, str) or not value.strip():
        fail(path, "必须为非空字符串")


def https_url(value, path):
    nonempty(value, path)
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme == "https" and parsed.hostname and parsed.port != 0
                 and not parsed.username and not parsed.password
                 and not parsed.fragment and not re.search(r"\s|[\x00-\x1f\x7f]", value))
    except ValueError:
        valid = False
    if not valid:
        fail(path, "必须为有效 HTTPS URL（不含凭据、空白或片段）")


def timestamp(value, path):
    nonempty(value, path)
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return dt.datetime.combine(dt.date.fromisoformat(value), dt.time(), dt.timezone.utc)
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", value):
            raise ValueError("invalid timestamp")
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        fail(path, "需要有效 YYYY-MM-DD 或带时区的 ISO 8601 时间")


def check_urls(value, path="$"):
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{path}.{key}"
            if key.endswith("URL") or key == "url":
                https_url(item, child)
            elif key.endswith("URLs"):
                for index, url in enumerate(item):
                    https_url(url, f"{child}[{index}]")
            check_urls(item, child)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            check_urls(item, f"{path}[{index}]")


def validate_source(source):
    schema = load_json(SCHEMA_PATH)
    check_schema(source, schema, schema)
    for field in ("name", "identifier", "sourceURL"):
        if field not in source:
            fail(f"$.{field}", "本仓库需要此字段")
        nonempty(source[field], f"$.{field}")
    check_urls(source)
    bundle_ids = set()
    for index, app in enumerate(source["apps"]):
        path = f"$.apps[{index}]"
        for field in ("name", "bundleIdentifier", "developerName", "localizedDescription"):
            nonempty(app[field], f"{path}.{field}")
        bundle = app["bundleIdentifier"]
        if bundle in bundle_ids:
            fail(f"{path}.bundleIdentifier", f"重复应用 {bundle}")
        bundle_ids.add(bundle)
        if not app["versions"]:
            fail(f"{path}.versions", "至少需要一个真实版本")
        versions = set()
        previous_date = None
        for number, version in enumerate(app["versions"]):
            vpath = f"{path}.versions[{number}]"
            nonempty(version["version"], f"{vpath}.version")
            if re.search(r"\s", version["version"]):
                fail(f"{vpath}.version", "不能包含空白")
            if version["version"] in versions:
                fail(f"{vpath}.version", "重复版本号")
            versions.add(version["version"])
            if type(version["size"]) is not int or version["size"] <= 0:
                fail(f"{vpath}.size", "必须为正整数（IPA 字节数）")
            date = timestamp(version["date"], f"{vpath}.date")
            if previous_date and date > previous_date:
                fail(f"{vpath}.date", "版本须按发布日期降序排列，最新版本在最前")
            previous_date = date
            for field in ("minOSVersion", "maxOSVersion"):
                if field in version and not re.fullmatch(r"\d+(?:\.\d+){0,2}", version[field]):
                    fail(f"{vpath}.{field}", "需要数字 iOS 版本，如 16.0")
    identifiers = set()
    for index, news in enumerate(source.get("news", [])):
        path = f"$.news[{index}]"
        for field in ("title", "identifier", "caption"):
            nonempty(news[field], f"{path}.{field}")
        if news["identifier"] in identifiers:
            fail(f"{path}.identifier", "重复新闻标识符")
        identifiers.add(news["identifier"])
        timestamp(news["date"], f"{path}.date")
    return source


def main():
    parser = argparse.ArgumentParser(description="校验 SideStore 源（离线、无第三方依赖）")
    parser.add_argument("source", nargs="?", default=str(Path(__file__).resolve().parents[1] / "source.json"))
    args = parser.parse_args()
    try:
        source = validate_source(load_json(args.source))
    except (OSError, ValueError, KeyError) as error:
        print(f"校验失败：{error}", file=sys.stderr)
        return 1
    print(f"校验通过：{args.source}（{len(source['apps'])} 个应用）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
