#!/usr/bin/env bash
set -eEuo pipefail

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

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
manifest_helper="$script_dir/install_manifest.py"
data_home=${XDG_DATA_HOME:-"$HOME/.local/share"}
config_home=${XDG_CONFIG_HOME:-"$HOME/.config"}
case "$data_home:$config_home" in
  /*:/*) ;;
  *) die "XDG_DATA_HOME and XDG_CONFIG_HOME must be absolute" 2 ;;
esac

install_root="$data_home/murmur-ime"
unit_dir="$config_home/systemd/user"
engine_unit_path="$unit_dir/murmur-ime-engine.service"
voice_unit_path="$unit_dir/murmur-ime-voice.service"
voice_launcher="$install_root/murmur-voice-daemon"
state_path="$install_root/previous-ibus-engine"
runtime_root=${XDG_RUNTIME_DIR:-}

data_quarantine=""
unit_quarantine=""
root_move_started=false
engine_unit_move_started=false
voice_unit_move_started=false
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

rollback_uninstall() {
  local failed=false
  printf '%s\n' "Uninstall failed; restoring the trusted installation." >&2
  if [[ $root_move_started == true && -d $data_quarantine/root ]]; then
    if [[ -e $install_root || -L $install_root ]]; then
      failed=true
    else
      mv -- "$data_quarantine/root" "$install_root" || failed=true
    fi
  fi
  if [[ $engine_unit_move_started == true && -f $unit_quarantine/engine.service ]]; then
    if [[ -e $engine_unit_path || -L $engine_unit_path ]]; then
      failed=true
    else
      mv -- "$unit_quarantine/engine.service" "$engine_unit_path" || failed=true
    fi
  fi
  if [[ $voice_unit_move_started == true && -f $unit_quarantine/voice.service ]]; then
    if [[ -e $voice_unit_path || -L $voice_unit_path ]]; then
      failed=true
    else
      mv -- "$unit_quarantine/voice.service" "$voice_unit_path" || failed=true
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
    rollback_uninstall || rollback_failed=true
  fi
  if [[ $rollback_failed == true ]]; then
    printf '%s\n' \
      "Automatic uninstall rollback was incomplete; retain all remaining files." >&2
    status=1
  else
    remove_private_tree "$data_quarantine"
    remove_private_tree "$unit_quarantine"
  fi
  exit "$status"
}
trap finalize EXIT
trap 'failure_status=$?; exit "$failure_status"' ERR
trap 'exit 130' INT
trap 'exit 143' TERM

[[ -f $manifest_helper && ! -L $manifest_helper ]] || die \
  "The installation manifest helper is unavailable" 2

root_present=false
engine_unit_present=false
voice_unit_present=false
[[ -e $install_root || -L $install_root ]] && root_present=true
[[ -e $engine_unit_path || -L $engine_unit_path ]] && engine_unit_present=true
[[ -e $voice_unit_path || -L $voice_unit_path ]] && voice_unit_present=true
if [[ $root_present == false && $engine_unit_present == false && $voice_unit_present == false ]]; then
  printf '%s\n' "No trusted Open Voice Input Linux user installation was found."
  exit 0
fi
if [[ $root_present != true || $engine_unit_present != true || $voice_unit_present != true ]]; then
  die "Refusing to remove a partial or unowned installation" 2
fi

python3 "$manifest_helper" secure-dir \
  --path "$data_home" --kind "XDG data"
python3 "$manifest_helper" secure-dir \
  --path "$config_home" --kind "XDG config"
python3 "$manifest_helper" secure-dir \
  --path "$unit_dir" --kind "systemd user unit"
python3 "$manifest_helper" require-disjoint \
  --left "$data_home" --right "$config_home"
python3 "$manifest_helper" require-disjoint \
  --left "$install_root" --right "$config_home"
python3 "$manifest_helper" require-disjoint \
  --left "$data_home" --right "$unit_dir"
python3 "$manifest_helper" require-disjoint \
  --left "$install_root" --right "$unit_dir"
python3 "$manifest_helper" verify \
  --root "$install_root" \
  --engine-unit "$engine_unit_path" \
  --voice-unit "$voice_unit_path" >/dev/null || die \
  "Refusing to remove files without a trusted ownership manifest" 2

systemctl --user show-environment >/dev/null || die \
  "A working systemd user manager is required for safe uninstall" 2
[[ $runtime_root == /* ]] || die \
  "XDG_RUNTIME_DIR is required and must be absolute for safe uninstall" 2
for specification in \
  "murmur-ime-engine.service:$engine_unit_path" \
  "murmur-ime-voice.service:$voice_unit_path"; do
  unit_name=${specification%%:*}
  expected_path=${specification#*:}
  fragment=$(systemctl --user show "$unit_name" \
    --property FragmentPath --value 2>/dev/null || true)
  if [[ -n $fragment && $fragment != "$expected_path" ]]; then
    die "Refusing to stop a same-name service loaded from an unowned path" 2
  fi
done

saved_engine=$(read_engine_state "$state_path" || true)
valid_engine_name "$saved_engine" || die \
  "The trusted installation has no valid previous IBus engine" 2
initial_engine=$(ibus engine 2>/dev/null || true)
if [[ $initial_engine == murmur-voice ]]; then
  # Any failure after a daemon shutdown must leave a usable keyboard engine,
  # even if that shutdown already cleared the temporary voice engine.
  exact_engine=$saved_engine
elif valid_engine_name "$initial_engine"; then
  exact_engine=$initial_engine
else
  die "The current IBus engine could not be determined; no files were removed" 2
fi

service_active murmur-ime-engine.service && engine_was_active=true
service_active murmur-ime-voice.service && voice_was_active=true
service_enabled murmur-ime-engine.service && engine_was_enabled=true
service_enabled murmur-ime-voice.service && voice_was_enabled=true
transaction_started=true

# A manually launched installed daemon is not represented by systemd state.
# Probe only the fixed, private socket; a live peer must acknowledge the fixed
# shutdown argv and actually release the socket before any installed file moves.
runtime_dir="$runtime_root/murmur-ime"
socket_path="$runtime_dir/voice.sock"
socket_status=$(python3 "$manifest_helper" socket-state \
  --runtime-root "$runtime_root" --path "$socket_path")
if [[ $socket_status == live ]]; then
  "$voice_launcher" shutdown --socket "$socket_path" >/dev/null || die \
    "The live voice daemon refused a controlled shutdown"
  for _ in {1..30}; do
    socket_status=$(python3 "$manifest_helper" socket-state \
      --runtime-root "$runtime_root" --path "$socket_path")
    [[ $socket_status != live ]] && break
    sleep 0.1
  done
  [[ $socket_status != live ]] || die \
    "The live voice daemon did not release its control socket"
fi
if [[ $socket_status == stale ]]; then
  rm -f -- "$socket_path"
fi

systemctl --user stop murmur-ime-voice.service
service_active murmur-ime-voice.service && die \
  "Refusing to remove files while the voice service is active"
managed_processes=$(python3 "$manifest_helper" voice-process-count \
  --root "$install_root")
[[ $managed_processes == 0 ]] || die \
  "A managed foreground voice daemon is still running; no files were removed"

current_engine=$(ibus engine 2>/dev/null || true)
if [[ $current_engine == murmur-voice ]]; then
  exact_engine=$saved_engine
  ibus engine "$saved_engine" >/dev/null 2>&1 || true
  current_engine=$(ibus engine 2>/dev/null || true)
  [[ $current_engine == "$saved_engine" ]] || die \
    "The recorded IBus engine could not be restored; no files were removed"
fi
valid_engine_name "$current_engine" || die \
  "The current IBus engine could not be determined; no files were removed"
exact_engine=$current_engine

systemctl --user stop murmur-ime-engine.service
service_active murmur-ime-engine.service && die \
  "Refusing to remove files while the engine service is active"
if [[ $(ibus engine 2>/dev/null || true) != "$exact_engine" ]]; then
  ibus engine "$exact_engine" >/dev/null 2>&1 || true
  if [[ $(ibus engine 2>/dev/null || true) != "$exact_engine" ]]; then
    [[ $engine_was_active == true ]] && \
      systemctl --user start murmur-ime-engine.service >/dev/null 2>&1 || true
    ibus engine "$exact_engine" >/dev/null 2>&1 || true
    [[ $(ibus engine 2>/dev/null || true) == "$exact_engine" ]] || die \
      "IBus could not preserve the active engine; no files were removed"
  fi
fi

systemctl --user disable murmur-ime-voice.service >/dev/null 2>&1 || true
systemctl --user disable murmur-ime-engine.service >/dev/null 2>&1 || true
systemctl --user stop murmur-ime-engine.service >/dev/null 2>&1 || true

data_quarantine=$(mktemp -d "$data_home/.murmur-ime.remove.XXXXXX")
unit_quarantine=$(mktemp -d "$unit_dir/.murmur-ime.remove.XXXXXX")
chmod 0700 "$data_quarantine" "$unit_quarantine"
root_move_started=true
mv -- "$install_root" "$data_quarantine/root"
engine_unit_move_started=true
mv -- "$engine_unit_path" "$unit_quarantine/engine.service"
voice_unit_move_started=true
mv -- "$voice_unit_path" "$unit_quarantine/voice.service"

systemctl --user daemon-reload
systemctl --user reset-failed \
  murmur-ime-voice.service murmur-ime-engine.service 2>/dev/null || true
if [[ $(ibus engine 2>/dev/null || true) != "$exact_engine" ]]; then
  ibus engine "$exact_engine" >/dev/null 2>&1 || true
fi
[[ $(ibus engine 2>/dev/null || true) == "$exact_engine" ]] || die \
  "IBus changed during uninstall; restoring the trusted installation"

commit_complete=true
rmdir -- "$runtime_root/murmur-ime" 2>/dev/null || true
printf '%s\n' \
  "Open Voice Input Linux user services and managed installed code were removed." \
  "The private API-key and vocabulary files were retained under the XDG config directory." \
  "No IBus daemon, Rime installation, or ~/.config/ibus/rime data was removed."
