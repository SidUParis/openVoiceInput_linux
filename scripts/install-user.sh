#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/install-user.sh [--wheelhouse DIR | --allow-network]

By default the installer uses ./wheelhouse and refuses network downloads.
--wheelhouse DIR  Install the voice wheel and dependencies only from DIR.
--allow-network   Explicit developer mode: let pip resolve from package indexes.
EOF
}

allow_network=false
wheelhouse=""
while (($#)); do
  case "$1" in
    --wheelhouse)
      if (($# < 2)); then
        printf '%s\n' "--wheelhouse requires a directory" >&2
        exit 2
      fi
      wheelhouse=$2
      shift 2
      ;;
    --allow-network)
      allow_network=true
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      printf 'Unknown option: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done
if [[ $allow_network == true && -n $wheelhouse ]]; then
  printf '%s\n' "--wheelhouse and --allow-network are mutually exclusive" >&2
  exit 2
fi

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_dir=$(dirname -- "$script_dir")
data_home=${XDG_DATA_HOME:-"$HOME/.local/share"}
config_home=${XDG_CONFIG_HOME:-"$HOME/.config"}
case "$data_home:$config_home" in
  /*:/*) ;;
  *)
    printf '%s\n' "XDG_DATA_HOME and XDG_CONFIG_HOME must be absolute" >&2
    exit 2
    ;;
esac

if [[ -z $wheelhouse && $allow_network == false ]]; then
  wheelhouse="$repo_dir/wheelhouse"
fi
voice_wheel=""
if [[ $allow_network == false ]]; then
  if [[ ! -d $wheelhouse ]]; then
    printf '%s\n' \
      "No offline wheelhouse found at: $wheelhouse" \
      "Provide --wheelhouse DIR, or explicitly opt into developer downloads with --allow-network." >&2
    exit 2
  fi
  wheelhouse=$(CDPATH= cd -- "$wheelhouse" && pwd)
  mapfile -t voice_wheels < <(
    find "$wheelhouse" -maxdepth 1 -type f \
      -name 'murmur_ime_voice-*.whl' -print | sort
  )
  if ((${#voice_wheels[@]} != 1)); then
    printf '%s\n' "The wheelhouse must contain exactly one murmur_ime_voice wheel" >&2
    exit 2
  fi
  voice_wheel=${voice_wheels[0]}
else
  printf '%s\n' \
    "Developer mode: pip may download voice dependencies from configured package indexes." >&2
fi

install_root="$data_home/murmur-ime"
package_dir="$install_root/murmur_ime_engine"
voice_venv="$install_root/voice-venv"
voice_marker="$voice_venv/.murmur-ime-managed"
voice_launcher="$install_root/murmur-voice-daemon"
state_path="$install_root/previous-ibus-engine"
unit_dir="$config_home/systemd/user"
engine_unit_path="$unit_dir/murmur-ime-engine.service"
voice_unit_path="$unit_dir/murmur-ime-voice.service"
voice_config="$config_home/murmur-ime/voice.json"
voice_vocabulary="$config_home/murmur-ime/vocabulary.json"
temporary_state=""
temporary_engine_unit=""
temporary_voice_unit=""
cleanup() {
  rm -f -- \
    "${temporary_state:-}" \
    "${temporary_engine_unit:-}" \
    "${temporary_voice_unit:-}"
}
trap cleanup EXIT

python3 -c \
  "import gi; gi.require_version('IBus', '1.0'); from gi.repository import IBus"
python3 -m venv --help >/dev/null
if ! systemctl --user show-environment >/dev/null; then
  printf '%s\n' "A working systemd user manager is required" >&2
  exit 2
fi

if [[ -L $install_root ]]; then
  printf '%s\n' "Refusing to install through a linked application directory" >&2
  exit 2
fi
if [[ -L $package_dir ]]; then
  printf '%s\n' "Refusing to replace a linked engine package directory" >&2
  exit 2
fi
if [[ -d $state_path && ! -L $state_path ]]; then
  printf '%s\n' "Refusing to replace a directory at the engine-state path" >&2
  exit 2
fi
if [[ -L $voice_venv \
  || (-e $voice_venv && (! -f $voice_marker || -L $voice_marker)) ]]; then
  printf '%s\n' \
    "Refusing to replace an unmanaged voice environment at: $voice_venv" >&2
  exit 2
fi

# Render and validate all path quoting before stopping or replacing a running
# installation. The temporary units are installed only after package success.
temporary_engine_unit=$(mktemp)
temporary_voice_unit=$(mktemp)
python3 "$script_dir/render_systemd_units.py" \
  --template "$repo_dir/packaging/systemd/murmur-ime-engine.service.in" \
  --output "$temporary_engine_unit" \
  --set "ENGINE_EXEC=$install_root/murmur-ime-engine"
python3 "$script_dir/render_systemd_units.py" \
  --template "$repo_dir/packaging/systemd/murmur-ime-voice.service.in" \
  --output "$temporary_voice_unit" \
  --set "VOICE_EXEC=$voice_launcher" \
  --set "VOICE_CONFIG=$voice_config" \
  --set "VOICE_VOCABULARY=$voice_vocabulary"

engine_was_active=false
voice_was_active=false
if systemctl --user is-active --quiet murmur-ime-engine.service; then
  engine_was_active=true
fi
if systemctl --user is-active --quiet murmur-ime-voice.service; then
  voice_was_active=true
fi
previous_engine=$(ibus engine 2>/dev/null || true)
previous_engine_valid=false
if [[ $previous_engine != murmur-voice \
  && ${#previous_engine} -le 256 \
  && $previous_engine =~ ^[[:alnum:]][[:alnum:]_.:@/+:-]*$ ]]; then
  previous_engine_valid=true
fi

recorded_engine=""
state_is_valid=false
if [[ -f $state_path && ! -L $state_path ]]; then
  state_uid=$(stat -c '%u' -- "$state_path" 2>/dev/null || true)
  state_mode=$(stat -c '%a' -- "$state_path" 2>/dev/null || true)
  state_size=$(stat -c '%s' -- "$state_path" 2>/dev/null || true)
  if [[ $state_uid == "$(id -u)" \
    && ($state_mode == 600 || $state_mode == 400) \
    && $state_size =~ ^[0-9]+$ && $state_size -le 257 ]]; then
    mapfile -t state_lines <"$state_path"
    if ((${#state_lines[@]} == 1)); then
      recorded_engine=${state_lines[0]}
    fi
    if [[ ${#recorded_engine} -le 256 \
      && $recorded_engine != murmur-voice \
      && $recorded_engine =~ ^[[:alnum:]][[:alnum:]_.:@/+:-]*$ ]]; then
      state_is_valid=true
    fi
  fi
fi

# Never let an active process execute files while its package or venv is being
# replaced. Voice stops first so it can cancel preedit and restore the engine.
if [[ $voice_was_active == true ]]; then
  systemctl --user stop murmur-ime-voice.service
fi
# A running voice session temporarily selects murmur-voice. Stopping the
# daemon should restore the real keyboard engine, so capture it again before
# the dynamic engine process is restarted.
if [[ $previous_engine_valid == false ]]; then
  restored_candidate=$(ibus engine 2>/dev/null || true)
  if [[ $restored_candidate != murmur-voice \
    && ${#restored_candidate} -le 256 \
    && $restored_candidate =~ ^[[:alnum:]][[:alnum:]_.:@/+:-]*$ ]]; then
    previous_engine=$restored_candidate
    previous_engine_valid=true
  fi
fi
if [[ $engine_was_active == true ]]; then
  systemctl --user stop murmur-ime-engine.service
fi
# Prevent a login/target transition from starting old unit files during a
# failed or interrupted replacement. Successful installation re-enables the
# intended units below.
systemctl --user disable murmur-ime-voice.service 2>/dev/null || true
systemctl --user disable murmur-ime-engine.service 2>/dev/null || true

if [[ -d $package_dir ]]; then
  rm -rf --one-file-system -- "$package_dir"
fi
if [[ -d $voice_venv ]]; then
  rm -rf --one-file-system -- "$voice_venv"
fi
install -d -m 0755 "$package_dir" "$unit_dir"
install -m 0755 \
  "$repo_dir/engine/murmur-ime-engine" \
  "$install_root/murmur-ime-engine"
for source in "$repo_dir"/engine/murmur_ime_engine/*.py; do
  install -m 0644 "$source" "$package_dir/$(basename -- "$source")"
done

if [[ $state_is_valid == false && $previous_engine_valid == true ]]; then
  if [[ -e $state_path || -L $state_path ]]; then
    rm -f -- "$state_path"
  fi
  temporary_state=$(mktemp "$install_root/.previous-ibus-engine.XXXXXX")
  chmod 0600 "$temporary_state"
  printf '%s\n' "$previous_engine" >"$temporary_state"
  mv -f -- "$temporary_state" "$state_path"
  temporary_state=""
fi

python3 -m venv --system-site-packages "$voice_venv"
install -m 0644 /dev/null "$voice_marker"
if [[ $allow_network == true ]]; then
  PYTHONNOUSERSITE=1 "$voice_venv/bin/python" -m pip install \
    --disable-pip-version-check \
    --no-input \
    --upgrade \
    "$repo_dir/voice"
else
  PYTHONNOUSERSITE=1 "$voice_venv/bin/python" -m pip install \
    --disable-pip-version-check \
    --no-input \
    --no-index \
    --find-links "$wheelhouse" \
    "$voice_wheel"
fi
PYTHONNOUSERSITE=1 "$voice_venv/bin/python" -c \
  "import gi, sounddevice, websockets, murmur_voice; gi.require_version('Gio', '2.0')"
install -m 0755 "$repo_dir/packaging/murmur-voice-daemon" "$voice_launcher"

install -m 0644 "$temporary_engine_unit" "$engine_unit_path"
install -m 0644 "$temporary_voice_unit" "$voice_unit_path"

systemctl --user daemon-reload
systemctl --user enable murmur-ime-engine.service
if [[ $engine_was_active == true ]]; then
  systemctl --user restart murmur-ime-engine.service
else
  systemctl --user start murmur-ime-engine.service
fi
if ! systemctl --user is-active --quiet murmur-ime-engine.service; then
  printf '%s\n' "The installed engine service did not become active" >&2
  exit 1
fi
# Dynamic IBus component teardown can clear the desktop's global engine even
# when another engine was selected. Restore exactly the validated engine that
# was active when this upgrade began, and verify the observable result.
if [[ $previous_engine_valid == true \
  && $(ibus engine 2>/dev/null || true) != "$previous_engine" ]]; then
  ibus engine "$previous_engine" >/dev/null 2>&1 || true
  if [[ $(ibus engine 2>/dev/null || true) != "$previous_engine" ]]; then
    printf 'The previous IBus engine could not be restored: %s\n' \
      "$previous_engine" >&2
    exit 1
  fi
fi
voice_config_ready=false
if [[ -f $voice_config ]] && PYTHONNOUSERSITE=1 "$voice_venv/bin/python" -c \
  "from murmur_voice.config import load_config; import sys; load_config(sys.argv[1])" \
  "$voice_config" >/dev/null 2>&1; then
  voice_config_ready=true
fi
voice_vocabulary_ready=false
if PYTHONNOUSERSITE=1 "$voice_venv/bin/python" -c \
  "from murmur_voice.config import load_vocabulary; import sys; load_vocabulary(sys.argv[1])" \
  "$voice_vocabulary" >/dev/null 2>&1; then
  voice_vocabulary_ready=true
fi
if [[ $voice_config_ready == true && $voice_vocabulary_ready == true ]]; then
  systemctl --user enable murmur-ime-voice.service
  if [[ $voice_was_active == true ]]; then
    systemctl --user restart murmur-ime-voice.service
  else
    systemctl --user start murmur-ime-voice.service
  fi
  if ! systemctl --user is-active --quiet murmur-ime-voice.service; then
    printf '%s\n' "The installed voice service did not become active" >&2
    exit 1
  fi
else
  systemctl --user disable murmur-ime-voice.service 2>/dev/null || true
fi

printf '%s\n' \
  "Open Voice Input Linux engine and standalone voice daemon were installed." \
  "The daemon is idle until an explicit start/toggle command; it never opens the microphone at login."
if [[ $voice_config_ready == false ]]; then
  printf '%s\n' \
    "Configure the key with:" \
    "  $voice_launcher configure --config $voice_config"
fi
if [[ $voice_vocabulary_ready == false ]]; then
  printf '%s\n' \
    "Replace or clear the invalid private vocabulary with:" \
    "  $voice_launcher vocabulary --vocabulary $voice_vocabulary"
fi
if [[ $voice_config_ready == false || $voice_vocabulary_ready == false ]]; then
  printf '%s\n' \
    "Then enable and start the idle service with:" \
    "  systemctl --user enable --now murmur-ime-voice.service"
fi
printf '%s\n' \
  "Bind a desktop shortcut to: $voice_launcher toggle" \
  "Inspect service state with: systemctl --user status murmur-ime-voice.service"
