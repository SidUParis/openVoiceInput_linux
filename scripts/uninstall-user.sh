#!/usr/bin/env bash
set -euo pipefail

data_home=${XDG_DATA_HOME:-"$HOME/.local/share"}
config_home=${XDG_CONFIG_HOME:-"$HOME/.config"}
case "$data_home:$config_home" in
  /*:/*) ;;
  *)
    printf '%s\n' "XDG_DATA_HOME and XDG_CONFIG_HOME must be absolute" >&2
    exit 2
    ;;
esac

install_root="$data_home/murmur-ime"
package_dir="$install_root/murmur_ime_engine"
voice_venv="$install_root/voice-venv"
voice_marker="$voice_venv/.murmur-ime-managed"
state_path="$install_root/previous-ibus-engine"
unit_dir="$config_home/systemd/user"
engine_unit_path="$unit_dir/murmur-ime-engine.service"
voice_unit_path="$unit_dir/murmur-ime-voice.service"
runtime_root=${XDG_RUNTIME_DIR:-}

if [[ -L $install_root ]]; then
  printf '%s\n' "Refusing to uninstall through a linked application directory" >&2
  exit 2
fi
if ! systemctl --user show-environment >/dev/null; then
  printf '%s\n' "A working systemd user manager is required for safe uninstall" >&2
  exit 2
fi

systemctl --user disable --now murmur-ime-voice.service 2>/dev/null || true
if systemctl --user is-active --quiet murmur-ime-voice.service; then
  printf '%s\n' "Refusing to remove files while the voice service is active" >&2
  exit 1
fi

saved_engine=""
if [[ -f $state_path && ! -L $state_path ]]; then
  state_uid=$(stat -c '%u' -- "$state_path" 2>/dev/null || true)
  state_mode=$(stat -c '%a' -- "$state_path" 2>/dev/null || true)
  state_size=$(stat -c '%s' -- "$state_path" 2>/dev/null || true)
  if [[ $state_uid == "$(id -u)" \
    && ($state_mode == 600 || $state_mode == 400) \
    && $state_size =~ ^[0-9]+$ && $state_size -le 257 ]]; then
    mapfile -t state_lines <"$state_path"
    if ((${#state_lines[@]} == 1)); then
      saved_engine=${state_lines[0]}
    fi
  fi
fi
if [[ ${#saved_engine} -gt 256 \
  || ! $saved_engine =~ ^[[:alnum:]][[:alnum:]_.:@/+:-]*$ \
  || $saved_engine == murmur-voice ]]; then
  saved_engine=""
fi

current_engine=$(ibus engine 2>/dev/null || true)
if [[ $current_engine == murmur-voice ]]; then
  if [[ -n $saved_engine ]]; then
    ibus engine "$saved_engine" >/dev/null 2>&1 || true
    if [[ $(ibus engine 2>/dev/null || true) != "$saved_engine" ]]; then
      printf 'Warning: IBus did not restore the recorded engine %s.\n' \
        "$saved_engine" >&2
    fi
  else
    printf '%s\n' \
      "Warning: current engine is murmur-voice, but no valid previous engine was recorded." >&2
  fi
fi

preserved_engine=$(ibus engine 2>/dev/null || true)
preserved_engine_valid=false
if [[ $preserved_engine != murmur-voice \
  && ${#preserved_engine} -le 256 \
  && $preserved_engine =~ ^[[:alnum:]][[:alnum:]_.:@/+:-]*$ ]]; then
  preserved_engine_valid=true
fi

systemctl --user disable --now murmur-ime-engine.service 2>/dev/null || true
if systemctl --user is-active --quiet murmur-ime-engine.service; then
  printf '%s\n' "Refusing to remove files while the engine service is active" >&2
  exit 1
fi
# Removing the dynamically registered component can clear IBus's global
# engine even when an unrelated engine was selected. Preserve and verify that
# exact non-voice engine; never guess or hard-code a fallback.
if [[ $preserved_engine_valid == true \
  && $(ibus engine 2>/dev/null || true) != "$preserved_engine" ]]; then
  ibus engine "$preserved_engine" >/dev/null 2>&1 || true
  if [[ $(ibus engine 2>/dev/null || true) != "$preserved_engine" ]]; then
    printf 'Warning: IBus did not preserve the active engine %s.\n' \
      "$preserved_engine" >&2
  fi
fi
rm -f -- "$voice_unit_path" "$engine_unit_path"
systemctl --user daemon-reload
systemctl --user reset-failed \
  murmur-ime-voice.service murmur-ime-engine.service 2>/dev/null || true

rm -f -- \
  "$install_root/murmur-voice-daemon" \
  "$install_root/murmur-ime-engine" \
  "$state_path"
if [[ -d $voice_venv && ! -L $voice_venv && -f $voice_marker ]]; then
  rm -rf --one-file-system -- "$voice_venv"
elif [[ -e $voice_venv || -L $voice_venv ]]; then
  printf '%s\n' \
    "Warning: leaving unmanaged voice environment at: $voice_venv" >&2
fi
if [[ -d $package_dir && ! -L $package_dir ]]; then
  rm -rf --one-file-system -- "$package_dir"
elif [[ -e $package_dir || -L $package_dir ]]; then
  printf '%s\n' \
    "Warning: leaving unsafe engine package path at: $package_dir" >&2
fi

if [[ $runtime_root == /* ]]; then
  runtime_dir="$runtime_root/murmur-ime"
  socket_path="$runtime_dir/voice.sock"
  if [[ -d $runtime_dir && ! -L $runtime_dir ]]; then
    if [[ -S $socket_path && ! -L $socket_path \
      && $(stat -c '%u' -- "$socket_path" 2>/dev/null || true) == "$(id -u)" ]]; then
      rm -f -- "$socket_path"
    elif [[ -e $socket_path || -L $socket_path ]]; then
      printf '%s\n' \
        "Warning: leaving unsafe runtime control path at: $socket_path" >&2
    fi
    rmdir -- "$runtime_dir" 2>/dev/null || true
  fi
fi
rmdir -- "$install_root" 2>/dev/null || true

printf '%s\n' \
  "Open Voice Input Linux user services and installed prototype code were removed." \
  "The private API-key and vocabulary files were retained under the XDG config directory." \
  "No IBus daemon, Rime installation, or ~/.config/ibus/rime data was removed."
