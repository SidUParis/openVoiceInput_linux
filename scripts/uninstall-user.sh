#!/usr/bin/env bash
set -euo pipefail

data_home=${XDG_DATA_HOME:-"$HOME/.local/share"}
config_home=${XDG_CONFIG_HOME:-"$HOME/.config"}
install_root="$data_home/murmur-ime"
package_dir="$install_root/murmur_ime_engine"
unit_path="$config_home/systemd/user/murmur-ime-engine.service"

if [[ $(ibus engine 2>/dev/null || true) == "murmur-voice" ]]; then
  ibus engine rime 2>/dev/null || true
fi
systemctl --user disable --now murmur-ime-engine.service 2>/dev/null || true
rm -f -- "$unit_path"
systemctl --user daemon-reload

rm -f -- "$install_root/murmur-ime-engine"
for module in \
  __init__.py constants.py dbus_service.py ibus_engine.py main.py \
  policy.py registry.py session.py; do
  rm -f -- "$package_dir/$module"
done
if [[ -d "$package_dir/__pycache__" ]]; then
  find "$package_dir/__pycache__" -maxdepth 1 -type f -name '*.pyc' -delete
  rmdir -- "$package_dir/__pycache__" 2>/dev/null || true
fi
rmdir -- "$package_dir" 2>/dev/null || true
rmdir -- "$install_root" 2>/dev/null || true

printf '%s\n' \
  "Open Voice Input Linux user service and prototype code were removed." \
  "No IBus daemon or Rime data was removed."
