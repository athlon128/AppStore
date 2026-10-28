#!/usr/bin/env bash
set -euo pipefail

for dependency in gh git python3 unzip; do
  if ! command -v "$dependency" >/dev/null 2>&1; then
    printf '错误：未安装 %s。请按 README 准备环境。\n' "$dependency" >&2
    exit 1
  fi
done

script_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$script_dir/publish_ipa.py" "$@"
