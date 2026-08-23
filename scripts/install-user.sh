#!/usr/bin/env bash
set -eEuo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/install-user.sh [--wheelhouse DIR | --allow-network]

By default the installer uses ./wheelhouse and refuses network downloads.
--wheelhouse DIR  Install the voice wheel and dependencies only from DIR.
--allow-network   Explicit developer mode: let pip resolve from package indexes.
EOF
}

die() {
  printf '%s\n' "$1" >&2
  exit "${2:-1}"
}

valid_engine_name() {
  local value=${1:-}
  [[ $value != murmur-voice \
    && ${#value} -le 256 \
    && $value =~ ^[[:alnum:]][[:alnum:]_.:@/+:-]*$ ]]
}

service_active() {
  systemctl --user is-active --quiet "$1"
}

service_enabled() {
  systemctl --user is-enabled --quiet "$1"
}

read_engine_state() {
  local path=$1
  local state_uid state_mode state_size
  local -a state_lines=()
  [[ -f $path && ! -L $path ]] || return 1
  state_uid=$(stat -c '%u' -- "$path" 2>/dev/null || true)
  state_mode=$(stat -c '%a' -- "$path" 2>/dev/null || true)
  state_size=$(stat -c '%s' -- "$path" 2>/dev/null || true)
  [[ $state_uid == "$(id -u)" \
    && ($state_mode == 600 || $state_mode == 400) \
    && $state_size =~ ^[0-9]+$ && $state_size -le 257 ]] || return 1
  mapfile -t state_lines <"$path"
  ((${#state_lines[@]} == 1)) || return 1
  valid_engine_name "${state_lines[0]}" || return 1
  printf '%s\n' "${state_lines[0]}"
}

allow_network=false
wheelhouse=""
while (($#)); do
  case "$1" in
    --wheelhouse)
      (($# >= 2)) || die "--wheelhouse requires a directory" 2
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
  die "--wheelhouse and --allow-network are mutually exclusive" 2
fi

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_dir=$(dirname -- "$script_dir")
manifest_helper="$script_dir/install_manifest.py"
data_home=${XDG_DATA_HOME:-"$HOME/.local/share"}
config_home=${XDG_CONFIG_HOME:-"$HOME/.config"}
case "$data_home:$config_home" in
  /*:/*) ;;
  *) die "XDG_DATA_HOME and XDG_CONFIG_HOME must be absolute" 2 ;;
esac

if [[ -z $wheelhouse && $allow_network == false ]]; then
  wheelhouse="$repo_dir/wheelhouse"
fi
voice_wheel=""
if [[ $allow_network == false ]]; then
  [[ -d $wheelhouse ]] || die \
    "No offline wheelhouse found at: $wheelhouse
Provide --wheelhouse DIR, or explicitly opt into developer downloads with --allow-network." 2
  wheelhouse=$(CDPATH= cd -- "$wheelhouse" && pwd)
  mapfile -t voice_wheels < <(
    find "$wheelhouse" -maxdepth 1 -type f \
      -name 'murmur_ime_voice-*.whl' -print | sort
  )
  ((${#voice_wheels[@]} == 1)) || die \
    "The wheelhouse must contain exactly one murmur_ime_voice wheel" 2
  voice_wheel=${voice_wheels[0]}
else
  printf '%s\n' \
    "Developer mode: pip may download voice dependencies from configured package indexes." >&2
fi

install_root="$data_home/murmur-ime"
unit_dir="$config_home/systemd/user"
engine_unit_path="$unit_dir/murmur-ime-engine.service"
voice_unit_path="$unit_dir/murmur-ime-voice.service"
voice_config="$config_home/murmur-ime/voice.json"
voice_vocabulary="$config_home/murmur-ime/vocabulary.json"

stage_root=""
unit_stage_dir=""
data_rollback_dir=""
unit_rollback_dir=""
root_replacement_started=false
engine_unit_replacement_started=false
voice_unit_replacement_started=false
transaction_started=false
commit_complete=false
rollback_failed=false
engine_was_active=false
voice_was_active=false
engine_was_enabled=false
voice_was_enabled=false
exact_engine=""

remove_private_tree() {
  local path=${1:-}
  [[ -n $path && -d $path && ! -L $path ]] || return 0
  rm -rf --one-file-system -- "$path"
}

restore_service_state() {
  local failed=false
  if [[ $engine_was_enabled == true ]]; then
    systemctl --user enable murmur-ime-engine.service >/dev/null 2>&1 || failed=true
  else
    systemctl --user disable murmur-ime-engine.service >/dev/null 2>&1 || true
  fi
  if [[ $voice_was_enabled == true ]]; then
    systemctl --user enable murmur-ime-voice.service >/dev/null 2>&1 || failed=true
  else
    systemctl --user disable murmur-ime-voice.service >/dev/null 2>&1 || true
  fi
  if [[ $engine_was_active == true ]]; then
    systemctl --user start murmur-ime-engine.service >/dev/null 2>&1 || failed=true
  else
    systemctl --user stop murmur-ime-engine.service >/dev/null 2>&1 || true
  fi
  if valid_engine_name "$exact_engine"; then
    ibus engine "$exact_engine" >/dev/null 2>&1 || failed=true
    [[ $(ibus engine 2>/dev/null || true) == "$exact_engine" ]] || failed=true
  fi
  if [[ $voice_was_active == true ]]; then
    systemctl --user start murmur-ime-voice.service >/dev/null 2>&1 || failed=true
  else
    systemctl --user stop murmur-ime-voice.service >/dev/null 2>&1 || true
  fi
  [[ $failed == false ]]
}

rollback_install() {
  local failed=false
  printf '%s\n' "Installation failed; restoring the previous managed runtime." >&2
  systemctl --user stop murmur-ime-voice.service >/dev/null 2>&1 || true
  systemctl --user stop murmur-ime-engine.service >/dev/null 2>&1 || true

  if [[ $voice_unit_replacement_started == true ]]; then
    if [[ -f $unit_rollback_dir/voice.service ]]; then
      rm -f -- "$voice_unit_path" || failed=true
      mv -- "$unit_rollback_dir/voice.service" "$voice_unit_path" || failed=true
    elif [[ $existing_install == false ]]; then
      rm -f -- "$voice_unit_path" || failed=true
    fi
  fi
  if [[ $engine_unit_replacement_started == true ]]; then
    if [[ -f $unit_rollback_dir/engine.service ]]; then
      rm -f -- "$engine_unit_path" || failed=true
      mv -- "$unit_rollback_dir/engine.service" "$engine_unit_path" || failed=true
    elif [[ $existing_install == false ]]; then
      rm -f -- "$engine_unit_path" || failed=true
    fi
  fi
  if [[ $root_replacement_started == true ]]; then
    if [[ -d $data_rollback_dir/root ]]; then
      remove_private_tree "$install_root" || failed=true
      mv -- "$data_rollback_dir/root" "$install_root" || failed=true
    elif [[ $existing_install == false ]]; then
      remove_private_tree "$install_root" || failed=true
    fi
  fi
  systemctl --user daemon-reload >/dev/null 2>&1 || failed=true
  restore_service_state || failed=true
  [[ $failed == false ]]
}

finalize() {
  local status=$?
  trap - EXIT ERR INT TERM
  set +e
  if [[ $transaction_started == true && $commit_complete == false ]]; then
    rollback_install || rollback_failed=true
  fi
  remove_private_tree "$stage_root"
  remove_private_tree "$unit_stage_dir"
  if [[ $rollback_failed == true ]]; then
    printf '%s\n' \
      "Automatic rollback was incomplete; do not remove the retained installation files." >&2
    status=1
  else
    remove_private_tree "$data_rollback_dir"
    remove_private_tree "$unit_rollback_dir"
  fi
  exit "$status"
}
trap finalize EXIT
trap 'failure_status=$?; exit "$failure_status"' ERR
trap 'exit 130' INT
trap 'exit 143' TERM

python3 -c \
  "import gi; gi.require_version('IBus', '1.0'); from gi.repository import IBus"
python3 -m venv --help >/dev/null
[[ -f $manifest_helper && ! -L $manifest_helper ]] || die \
  "The installation manifest helper is unavailable" 2
systemctl --user show-environment >/dev/null || die \
  "A working systemd user manager is required" 2

python3 "$manifest_helper" secure-dir \
  --path "$data_home" --kind "XDG data" --create
python3 "$manifest_helper" secure-dir \
  --path "$config_home" --kind "XDG config" --create
python3 "$manifest_helper" secure-dir \
  --path "$unit_dir" --kind "systemd user unit" --create
python3 "$manifest_helper" require-disjoint \
  --left "$data_home" --right "$config_home"
python3 "$manifest_helper" require-disjoint \
  --left "$install_root" --right "$config_home"
python3 "$manifest_helper" require-disjoint \
  --left "$data_home" --right "$unit_dir"
python3 "$manifest_helper" require-disjoint \
  --left "$install_root" --right "$unit_dir"

root_present=false
engine_unit_present=false
voice_unit_present=false
[[ -e $install_root || -L $install_root ]] && root_present=true
[[ -e $engine_unit_path || -L $engine_unit_path ]] && engine_unit_present=true
[[ -e $voice_unit_path || -L $voice_unit_path ]] && voice_unit_present=true

existing_install=false
install_id=""
if [[ $root_present == true || $engine_unit_present == true || $voice_unit_present == true ]]; then
  if [[ $root_present != true || $engine_unit_present != true || $voice_unit_present != true ]]; then
    die "Refusing to replace a partial or unowned installation" 2
  fi
  if ! install_id=$(python3 "$manifest_helper" verify \
    --root "$install_root" \
    --engine-unit "$engine_unit_path" \
    --voice-unit "$voice_unit_path"); then
    die "Refusing to replace files without a trusted ownership manifest" 2
  fi
  existing_install=true
else
  install_id=$(python3 "$manifest_helper" new-id)
fi
for specification in \
  "murmur-ime-engine.service:$engine_unit_path" \
  "murmur-ime-voice.service:$voice_unit_path"; do
  unit_name=${specification%%:*}
  expected_path=${specification#*:}
  fragment=$(systemctl --user show "$unit_name" \
    --property FragmentPath --value 2>/dev/null || true)
  if [[ -n $fragment && $fragment != "$expected_path" ]]; then
    die "Refusing to stop or shadow a same-name service loaded from an unowned path" 2
  fi
done

recorded_engine=""
if [[ $existing_install == true ]]; then
  recorded_engine=$(read_engine_state "$install_root/previous-ibus-engine" || true)
  valid_engine_name "$recorded_engine" || die \
    "The trusted installation has an invalid previous-engine state" 2
fi

# Build and validate the complete replacement while the old services continue
# to run. Both stage roots are siblings of their final destinations, so later
# renames stay on one filesystem.
stage_root=$(mktemp -d "$data_home/.murmur-ime.stage.XXXXXX")
unit_stage_dir=$(mktemp -d "$unit_dir/.murmur-ime.stage.XXXXXX")
chmod 0700 "$stage_root" "$unit_stage_dir"
stage_package="$stage_root/murmur_ime_engine"
stage_venv="$stage_root/voice-venv"
stage_marker="$stage_venv/.murmur-ime-managed"
stage_engine_unit="$unit_stage_dir/murmur-ime-engine.service"
stage_voice_unit="$unit_stage_dir/murmur-ime-voice.service"

install -d -m 0755 "$stage_package"
install -m 0755 \
  "$repo_dir/engine/murmur-ime-engine" \
  "$stage_root/murmur-ime-engine"
for source in "$repo_dir"/engine/murmur_ime_engine/*.py; do
  install -m 0644 "$source" "$stage_package/$(basename -- "$source")"
done
python3 -m venv --system-site-packages "$stage_venv"
if [[ $allow_network == true ]]; then
  PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 "$stage_venv/bin/python" -m pip install \
    --disable-pip-version-check --no-input --upgrade "$repo_dir/voice"
else
  PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 "$stage_venv/bin/python" -m pip install \
    --disable-pip-version-check --no-input --no-index \
    --find-links "$wheelhouse" "$voice_wheel"
fi
printf '%s\n' "$install_id" >"$stage_marker"
chmod 0600 "$stage_marker"
PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 "$stage_venv/bin/python" -c \
  "import gi, sounddevice, websockets, murmur_voice; gi.require_version('Gio', '2.0'); gi.require_version('Gtk', '4.0'); from gi.repository import Gio, Gtk"
install -m 0755 "$repo_dir/packaging/murmur-voice-daemon" \
  "$stage_root/murmur-voice-daemon"
install -m 0755 "$repo_dir/packaging/open-voice-input-settings" \
  "$stage_root/open-voice-input-settings"

python3 "$script_dir/render_systemd_units.py" \
  --template "$repo_dir/packaging/systemd/murmur-ime-engine.service.in" \
  --output "$stage_engine_unit" \
  --set "ENGINE_EXEC=$install_root/murmur-ime-engine"
python3 "$script_dir/render_systemd_units.py" \
  --template "$repo_dir/packaging/systemd/murmur-ime-voice.service.in" \
  --output "$stage_voice_unit" \
  --set "VOICE_EXEC=$install_root/murmur-voice-daemon" \
  --set "VOICE_CONFIG=$voice_config" \
  --set "VOICE_VOCABULARY=$voice_vocabulary"
chmod 0644 "$stage_engine_unit" "$stage_voice_unit"

service_active murmur-ime-engine.service && engine_was_active=true
service_active murmur-ime-voice.service && voice_was_active=true
service_enabled murmur-ime-engine.service && engine_was_enabled=true
service_enabled murmur-ime-voice.service && voice_was_enabled=true

observed_engine=$(ibus engine 2>/dev/null || true)
if valid_engine_name "$observed_engine"; then
  exact_engine=$observed_engine
elif [[ $observed_engine == murmur-voice ]] && valid_engine_name "$recorded_engine"; then
  exact_engine=$recorded_engine
fi
transaction_started=true
if [[ $voice_was_active == true ]]; then
  systemctl --user stop murmur-ime-voice.service
  service_active murmur-ime-voice.service && die \
    "The voice service did not stop before replacement"
fi
post_voice_engine=$(ibus engine 2>/dev/null || true)
if ! valid_engine_name "$post_voice_engine"; then
  if [[ $post_voice_engine == murmur-voice ]] && valid_engine_name "$recorded_engine"; then
    ibus engine "$recorded_engine" >/dev/null 2>&1 || true
    post_voice_engine=$(ibus engine 2>/dev/null || true)
  fi
fi
valid_engine_name "$post_voice_engine" || die \
  "A precise non-voice IBus engine could not be captured; no files were replaced"
exact_engine=$post_voice_engine

state_engine=$recorded_engine
valid_engine_name "$state_engine" || state_engine=$exact_engine
printf '%s\n' "$state_engine" >"$stage_root/previous-ibus-engine"
chmod 0600 "$stage_root/previous-ibus-engine"
python3 "$manifest_helper" create \
  --root "$stage_root" \
  --engine-unit "$stage_engine_unit" \
  --voice-unit "$stage_voice_unit" \
  --output "$stage_root/install-manifest.json" \
  --install-id "$install_id"
python3 "$manifest_helper" verify \
  --root "$stage_root" \
  --engine-unit "$stage_engine_unit" \
  --voice-unit "$stage_voice_unit" >/dev/null

# Capture once more immediately before stopping the engine so rollback restores
# the exact observable desktop state, even if the user changed engines during
# the comparatively long staging build.
latest_engine=$(ibus engine 2>/dev/null || true)
valid_engine_name "$latest_engine" || die \
  "The active IBus engine became unavailable before replacement"
exact_engine=$latest_engine

if [[ $engine_was_active == true ]]; then
  systemctl --user stop murmur-ime-engine.service
  service_active murmur-ime-engine.service && die \
    "The engine service did not stop before replacement"
fi
systemctl --user disable murmur-ime-voice.service >/dev/null 2>&1 || true
systemctl --user disable murmur-ime-engine.service >/dev/null 2>&1 || true

data_rollback_dir=$(mktemp -d "$data_home/.murmur-ime.rollback.XXXXXX")
unit_rollback_dir=$(mktemp -d "$unit_dir/.murmur-ime.rollback.XXXXXX")
chmod 0700 "$data_rollback_dir" "$unit_rollback_dir"
if [[ $existing_install == true ]]; then
  root_replacement_started=true
  mv -- "$install_root" "$data_rollback_dir/root"
  engine_unit_replacement_started=true
  mv -- "$engine_unit_path" "$unit_rollback_dir/engine.service"
  voice_unit_replacement_started=true
  mv -- "$voice_unit_path" "$unit_rollback_dir/voice.service"
fi
root_replacement_started=true
mv -- "$stage_root" "$install_root"
stage_root=""
engine_unit_replacement_started=true
mv -- "$stage_engine_unit" "$engine_unit_path"
voice_unit_replacement_started=true
mv -- "$stage_voice_unit" "$voice_unit_path"

systemctl --user daemon-reload
systemctl --user enable murmur-ime-engine.service
systemctl --user start murmur-ime-engine.service
service_active murmur-ime-engine.service || die \
  "The installed engine service did not become active"
ibus engine "$exact_engine" >/dev/null 2>&1 || true
[[ $(ibus engine 2>/dev/null || true) == "$exact_engine" ]] || die \
  "The exact previous IBus engine could not be restored"

voice_config_ready=false
if [[ -f $voice_config ]] && \
  PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 \
    "$install_root/voice-venv/bin/python" -c \
    "from murmur_voice.config import load_config; import sys; load_config(sys.argv[1])" \
    "$voice_config" >/dev/null 2>&1; then
  voice_config_ready=true
fi
voice_vocabulary_ready=false
if PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 \
  "$install_root/voice-venv/bin/python" -c \
  "from murmur_voice.config import load_vocabulary; import sys; load_vocabulary(sys.argv[1])" \
  "$voice_vocabulary" >/dev/null 2>&1; then
  voice_vocabulary_ready=true
fi
if [[ $voice_config_ready == true && $voice_vocabulary_ready == true ]]; then
  systemctl --user enable murmur-ime-voice.service
  systemctl --user start murmur-ime-voice.service
  service_active murmur-ime-voice.service || die \
    "The installed voice service did not become active"
else
  systemctl --user disable murmur-ime-voice.service >/dev/null 2>&1 || true
fi

commit_complete=true
printf '%s\n' \
  "Open Voice Input Linux engine and standalone voice daemon were installed." \
  "The daemon is idle until an explicit start/toggle command; it never opens the microphone at login."
if [[ $voice_config_ready == false ]]; then
  printf '%s\n' \
    "Configure the key with:" \
    "  $install_root/murmur-voice-daemon configure --config $voice_config"
fi
if [[ $voice_vocabulary_ready == false ]]; then
  printf '%s\n' \
    "Replace or clear the invalid private vocabulary with:" \
    "  $install_root/murmur-voice-daemon vocabulary --vocabulary $voice_vocabulary"
fi
if [[ $voice_config_ready == false || $voice_vocabulary_ready == false ]]; then
  printf '%s\n' \
    "Then enable and start the idle service with:" \
    "  systemctl --user enable --now murmur-ime-voice.service"
fi
printf '%s\n' \
  "Open the native settings window with: $install_root/open-voice-input-settings" \
  "Bind a desktop shortcut to: $install_root/murmur-voice-daemon toggle" \
  "Inspect service state with: systemctl --user status murmur-ime-voice.service"
