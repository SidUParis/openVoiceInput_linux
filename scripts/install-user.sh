#!/usr/bin/env bash
set -euo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_dir=$(dirname -- "$script_dir")
data_home=${XDG_DATA_HOME:-"$HOME/.local/share"}
config_home=${XDG_CONFIG_HOME:-"$HOME/.config"}
install_root="$data_home/murmur-ime"
package_dir="$install_root/murmur_ime_engine"
unit_dir="$config_home/systemd/user"
unit_path="$unit_dir/murmur-ime-engine.service"

python3 -c \
  "import gi; gi.require_version('IBus', '1.0'); from gi.repository import IBus"

install -d -m 0755 "$package_dir" "$unit_dir"
install -m 0755 \
  "$repo_dir/engine/murmur-ime-engine" \
  "$install_root/murmur-ime-engine"
for source in "$repo_dir"/engine/murmur_ime_engine/*.py; do
  install -m 0644 "$source" "$package_dir/$(basename -- "$source")"
done

temporary_unit=$(mktemp)
trap 'rm -f -- "$temporary_unit"' EXIT
escaped_exec=${install_root//&/\\&}
escaped_exec=${escaped_exec//|/\\|}
sed \
  "s|@ENGINE_EXEC@|$escaped_exec/murmur-ime-engine|g" \
  "$repo_dir/packaging/systemd/murmur-ime-engine.service.in" \
  >"$temporary_unit"
install -m 0644 "$temporary_unit" "$unit_path"

systemctl --user daemon-reload
systemctl --user enable --now murmur-ime-engine.service

printf '%s\n' \
  "Open Voice Input Linux was registered dynamically as murmur-voice." \
  "Choose it with: ibus engine murmur-voice" \
  "Restore Rime with: ibus engine rime"
