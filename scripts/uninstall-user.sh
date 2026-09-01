#!/usr/bin/env bash
set -eEuo pipefail
unset PYTHONHOME PYTHONPATH

usage() {
  cat <<'EOF'
Usage: scripts/uninstall-user.sh

Remove the managed Open Voice Input Linux user installation. Private API-key,
vocabulary, correction, interaction, output-style, microphone-priority, and
data-collection settings are retained. Any dataset in a user-selected external
directory is never removed.
EOF
}

if (($#)); then
  case "$1" in
    --help|-h)
      if (($# != 1)); then
        printf '%s\n' "--help does not accept additional arguments" >&2
        exit 2
      fi
      usage
      exit 0
      ;;
    *)
      printf 'Unknown option: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
fi

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
desktop_entry_path="$data_home/applications/io.github.SidUParis.OpenVoiceInputLinux.Settings.desktop"
settings_icon_path="$data_home/icons/hicolor/scalable/apps/io.github.SidUParis.OpenVoiceInputLinux.Settings.svg"
voice_launcher="$install_root/murmur-voice-daemon"
state_path="$install_root/previous-ibus-engine"
runtime_root=${XDG_RUNTIME_DIR:-}

data_quarantine=""
data_quarantine_identity=""
unit_quarantine=""
unit_quarantine_identity=""
desktop_quarantine=""
desktop_quarantine_identity=""
icon_quarantine=""
icon_quarantine_identity=""
data_lock_fd=""
config_lock_fd=""
root_move_started=false
engine_unit_move_started=false
voice_unit_move_started=false
desktop_entry_move_started=false
settings_icon_move_started=false
quarantine_verified=false
transaction_started=false
commit_complete=false
rollback_failed=false
engine_was_active=false
voice_was_active=false
engine_was_enabled=false
voice_was_enabled=false
exact_engine=""
manifest_version=0

remove_private_tree() {
  local path=${1:-}
  local identity=${2:-}
  local cleanup_dir=""
  local isolated=""
  [[ -n $path ]] || return 0
  if [[ ! -e $path && ! -L $path ]]; then
    return 0
  fi
  if [[ -z $identity || ! -d $path || -L $path ]]; then
    printf '%s\n' \
      "Cleanup refused a changed or non-directory path retained at: $path" >&2
    return 1
  fi

  # Isolate only the exact quarantine inode created by this transaction before
  # recursive deletion.  Unknown same-name replacements remain untouched.
  if ! cleanup_dir=$(mktemp -d "$(dirname -- "$path")/.murmur-ime.cleanup.XXXXXX"); then
    printf '%s\n' "Cleanup material was retained at: $path" >&2
    return 1
  fi
  chmod 0700 "$cleanup_dir" || {
    printf '%s\n' \
      "Cleanup material was retained at: $path" \
      "Cleanup isolation directory was retained at: $cleanup_dir" >&2
    return 1
  }
  isolated="$cleanup_dir/tree"
  if ! python3 -I "$manifest_helper" quarantine-committed \
    --source "$path" --quarantine "$isolated" --identity "$identity"; then
    printf '%s\n' \
      "Cleanup refused a changed path retained at: $path" \
      "Cleanup isolation material was retained at: $cleanup_dir" >&2
    return 1
  fi
  if ! rm -rf --one-file-system -- "$isolated"; then
    printf '%s\n' "Cleanup material was retained at: $cleanup_dir" >&2
    return 1
  fi
  if [[ -e $isolated || -L $isolated ]] || ! rmdir -- "$cleanup_dir"; then
    printf '%s\n' "Cleanup material was retained at: $cleanup_dir" >&2
    return 1
  fi
}

private_tree_identity() {
  local path=$1
  [[ -d $path && ! -L $path ]] || return 1
  stat -c '%d:%i' -- "$path"
}

report_retained_path() {
  local path=${1:-}
  if [[ -n $path && (-e $path || -L $path) ]]; then
    printf '%s\n' "Retained recovery material at: $path" >&2
  fi
}

move_no_clobber_with_flag() {
  local source=$1
  local destination=$2
  local flag_name=$3
  local status
  # Keep the quarantine move and its rollback marker indivisible with respect
  # to the script's signal traps.
  trap '' INT TERM
  if python3 -I "$manifest_helper" move-no-clobber \
    --source "$source" --destination "$destination" >/dev/null; then
    printf -v "$flag_name" '%s' true
    status=0
  else
    status=$?
  fi
  trap 'exit 130' INT
  trap 'exit 143' TERM
  return "$status"
}

restore_ibus_state() {
  valid_engine_name "$exact_engine" || return 0
  ibus engine "$exact_engine" >/dev/null 2>&1 || return 1
  [[ $(ibus engine 2>/dev/null || true) == "$exact_engine" ]]
}

keep_services_stopped() {
  systemctl --user stop murmur-ime-voice.service >/dev/null 2>&1 || true
  systemctl --user stop murmur-ime-engine.service >/dev/null 2>&1 || true
  systemctl --user disable murmur-ime-voice.service >/dev/null 2>&1 || true
  systemctl --user disable murmur-ime-engine.service >/dev/null 2>&1 || true
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
  restore_ibus_state || failed=true
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
  if [[ $desktop_entry_move_started == true && -f $desktop_quarantine/settings.desktop ]]; then
    python3 -I "$manifest_helper" move-no-clobber \
      --source "$desktop_quarantine/settings.desktop" \
      --destination "$desktop_entry_path" >/dev/null || failed=true
  fi
  if [[ $settings_icon_move_started == true && -f $icon_quarantine/icon.svg ]]; then
    python3 -I "$manifest_helper" move-no-clobber \
      --source "$icon_quarantine/icon.svg" \
      --destination "$settings_icon_path" >/dev/null || failed=true
  fi
  if [[ $engine_unit_move_started == true && -f $unit_quarantine/engine.service ]]; then
    python3 -I "$manifest_helper" move-no-clobber \
      --source "$unit_quarantine/engine.service" \
      --destination "$engine_unit_path" >/dev/null || failed=true
  fi
  if [[ $voice_unit_move_started == true && -f $unit_quarantine/voice.service ]]; then
    python3 -I "$manifest_helper" move-no-clobber \
      --source "$unit_quarantine/voice.service" \
      --destination "$voice_unit_path" >/dev/null || failed=true
  fi
  # Republish the launcher only after every other owned path has been restored,
  # so a manual invocation cannot enter midway through rollback.
  if [[ $failed == false \
    && $root_move_started == true && -d $data_quarantine/root ]]; then
    python3 -I "$manifest_helper" move-no-clobber \
      --source "$data_quarantine/root" \
      --destination "$install_root" >/dev/null || failed=true
  fi
  if [[ $quarantine_verified != true \
    && ($root_move_started == true \
      || $engine_unit_move_started == true \
      || $voice_unit_move_started == true \
      || $desktop_entry_move_started == true \
      || $settings_icon_move_started == true) ]]; then
    failed=true
  fi
  if [[ $failed == false ]]; then
    systemctl --user daemon-reload >/dev/null 2>&1 || failed=true
  fi
  if [[ $failed == false ]]; then
    restore_service_state || failed=true
  fi
  if [[ $failed == true ]]; then
    keep_services_stopped
    restore_ibus_state || true
  fi
  [[ $failed == false ]]
}

finalize() {
  local status=$?
  local cleanup_failed=false
  trap - EXIT ERR INT TERM
  set +e
  if [[ $transaction_started == true && $commit_complete == false ]]; then
    rollback_uninstall || rollback_failed=true
  fi
  if [[ $rollback_failed == true ]]; then
    printf '%s\n' \
      "Automatic uninstall rollback was incomplete; retain all remaining files." >&2
    report_retained_path "$data_quarantine"
    report_retained_path "$unit_quarantine"
    report_retained_path "$desktop_quarantine"
    report_retained_path "$icon_quarantine"
    status=1
  else
    remove_private_tree "$data_quarantine" "$data_quarantine_identity" || cleanup_failed=true
    remove_private_tree "$unit_quarantine" "$unit_quarantine_identity" || cleanup_failed=true
    remove_private_tree "$desktop_quarantine" "$desktop_quarantine_identity" || cleanup_failed=true
    remove_private_tree "$icon_quarantine" "$icon_quarantine_identity" || cleanup_failed=true
    if [[ $cleanup_failed == true ]]; then
      if [[ $commit_complete == true ]]; then
        printf '%s\n' \
          "Uninstall committed, but cleanup was incomplete; retained locations are listed above." >&2
      else
        printf '%s\n' \
          "Uninstall failed and cleanup was incomplete; retained locations are listed above." >&2
      fi
      status=1
    fi
  fi
  exit "$status"
}
trap finalize EXIT
trap 'failure_status=$?; exit "$failure_status"' ERR
trap 'exit 130' INT
trap 'exit 143' TERM

[[ -f $manifest_helper && ! -L $manifest_helper ]] || die \
  "The installation manifest helper is unavailable" 2
if [[ ! -e $data_home && ! -L $data_home ]]; then
  if [[ -e $engine_unit_path || -L $engine_unit_path \
    || -e $voice_unit_path || -L $voice_unit_path ]]; then
    die "Refusing to remove a partial or unowned installation" 2
  fi
  printf '%s\n' "No trusted Open Voice Input Linux user installation was found."
  exit 0
fi
if [[ ! -e $config_home && ! -L $config_home ]]; then
  if [[ -e $install_root || -L $install_root ]]; then
    die "Refusing to remove a partial or unowned installation" 2
  fi
  printf '%s\n' "No trusted Open Voice Input Linux user installation was found."
  exit 0
fi
python3 -I "$manifest_helper" secure-dir \
  --path "$data_home" --kind "XDG data"
python3 -I "$manifest_helper" secure-dir \
  --path "$config_home" --kind "XDG config"
python3 -I "$manifest_helper" require-disjoint \
  --left "$data_home" --right "$config_home"
command -v flock >/dev/null 2>&1 || die \
  "The flock utility is required for safe uninstall" 2
exec {data_lock_fd}<"$data_home" || die \
  "The XDG data directory could not be opened for locking" 2
exec {config_lock_fd}<"$config_home" || die \
  "The XDG config directory could not be opened for locking" 2
data_lock_identity=$(python3 -I "$manifest_helper" secure-dir-fd \
  --path "$data_home" --fd "$data_lock_fd")
config_lock_identity=$(python3 -I "$manifest_helper" secure-dir-fd \
  --path "$config_home" --fd "$config_lock_fd")
[[ $data_lock_identity != "$config_lock_identity" ]] || die \
  "XDG data and config lock directories must be distinct" 2
if [[ $data_lock_identity < $config_lock_identity ]]; then
  first_lock_fd=$data_lock_fd
  second_lock_fd=$config_lock_fd
else
  first_lock_fd=$config_lock_fd
  second_lock_fd=$data_lock_fd
fi
flock -n "$first_lock_fd" || die \
  "Another Open Voice Input install or uninstall is already in progress" 2
flock -n "$second_lock_fd" || die \
  "Another Open Voice Input install or uninstall is already in progress" 2
python3 -I "$manifest_helper" secure-dir-fd \
  --path "$data_home" --fd "$data_lock_fd" >/dev/null
python3 -I "$manifest_helper" secure-dir-fd \
  --path "$config_home" --fd "$config_lock_fd" >/dev/null

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

python3 -I "$manifest_helper" secure-dir \
  --path "$unit_dir" --kind "systemd user unit"
python3 -I "$manifest_helper" require-disjoint \
  --left "$install_root" --right "$config_home"
python3 -I "$manifest_helper" require-disjoint \
  --left "$data_home" --right "$unit_dir"
python3 -I "$manifest_helper" require-disjoint \
  --left "$install_root" --right "$unit_dir"
if ! manifest_identity=$(python3 -I "$manifest_helper" verify \
  --root "$install_root" \
  --engine-unit "$engine_unit_path" \
  --voice-unit "$voice_unit_path" \
  --desktop-entry "$desktop_entry_path" \
  --settings-icon "$settings_icon_path" \
  --print-version); then
  die "Refusing to remove files without a trusted ownership manifest" 2
fi
read -r install_id manifest_version extra_identity <<<"$manifest_identity"
[[ -n $install_id && -z ${extra_identity:-} ]] || die \
  "The trusted ownership manifest returned an invalid identity" 2
[[ $manifest_version == 1 || $manifest_version == 2 ]] || die \
  "The trusted ownership manifest has an unsupported version" 2

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
socket_status=$(python3 -I "$manifest_helper" socket-state \
  --runtime-root "$runtime_root" --path "$socket_path")
if [[ $socket_status == live ]]; then
  "$voice_launcher" shutdown --socket "$socket_path" >/dev/null || die \
    "The live voice daemon refused a controlled shutdown"
  for _ in {1..30}; do
    socket_status=$(python3 -I "$manifest_helper" socket-state \
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
managed_processes=$(python3 -I "$manifest_helper" voice-process-count \
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
data_quarantine_identity=$(private_tree_identity "$data_quarantine") || die \
  "The data quarantine identity could not be recorded"
unit_quarantine=$(mktemp -d "$unit_dir/.murmur-ime.remove.XXXXXX")
unit_quarantine_identity=$(private_tree_identity "$unit_quarantine") || die \
  "The unit quarantine identity could not be recorded"
chmod 0700 "$data_quarantine" "$unit_quarantine"
if [[ $manifest_version == 2 ]]; then
  desktop_quarantine=$(mktemp -d "$data_home/applications/.murmur-ime.remove.XXXXXX")
  desktop_quarantine_identity=$(private_tree_identity "$desktop_quarantine") || die \
    "The desktop quarantine identity could not be recorded"
  icon_quarantine=$(mktemp -d "$data_home/icons/hicolor/scalable/apps/.murmur-ime.remove.XXXXXX")
  icon_quarantine_identity=$(private_tree_identity "$icon_quarantine") || die \
    "The icon quarantine identity could not be recorded"
  chmod 0700 "$desktop_quarantine" "$icon_quarantine"
fi
move_no_clobber_with_flag \
  "$install_root" "$data_quarantine/root" root_move_started
# The fixed launcher path is now absent, so no ordinary invocation can enter
# after this check.  Match the argv path retained by a process that raced the
# pre-move count, but validate its interpreter through the trusted quarantine.
managed_processes=$(python3 -I "$manifest_helper" voice-process-count \
  --root "$data_quarantine/root" --argv-root "$install_root")
if [[ $managed_processes != 0 ]]; then
  if [[ $manifest_version == 2 ]]; then
    python3 -I "$manifest_helper" verify \
      --root "$data_quarantine/root" \
      --engine-unit "$engine_unit_path" \
      --voice-unit "$voice_unit_path" \
      --desktop-entry "$desktop_entry_path" \
      --settings-icon "$settings_icon_path" \
      --staged >/dev/null
  else
    python3 -I "$manifest_helper" verify \
      --root "$data_quarantine/root" \
      --engine-unit "$engine_unit_path" \
      --voice-unit "$voice_unit_path" \
      --staged >/dev/null
  fi
  quarantine_verified=true
  die "A managed foreground voice daemon raced with uninstall; the old tree was restored"
fi
move_no_clobber_with_flag \
  "$engine_unit_path" "$unit_quarantine/engine.service" engine_unit_move_started
move_no_clobber_with_flag \
  "$voice_unit_path" "$unit_quarantine/voice.service" voice_unit_move_started
if [[ $manifest_version == 2 ]]; then
  move_no_clobber_with_flag \
    "$desktop_entry_path" "$desktop_quarantine/settings.desktop" \
    desktop_entry_move_started
  move_no_clobber_with_flag \
    "$settings_icon_path" "$icon_quarantine/icon.svg" \
    settings_icon_move_started
  python3 -I "$manifest_helper" verify \
    --root "$data_quarantine/root" \
    --engine-unit "$unit_quarantine/engine.service" \
    --voice-unit "$unit_quarantine/voice.service" \
    --desktop-entry "$desktop_quarantine/settings.desktop" \
    --settings-icon "$icon_quarantine/icon.svg" \
    --staged >/dev/null
else
  python3 -I "$manifest_helper" verify \
    --root "$data_quarantine/root" \
    --engine-unit "$unit_quarantine/engine.service" \
    --voice-unit "$unit_quarantine/voice.service" \
    --staged >/dev/null
fi
quarantine_verified=true

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
rmdir -- "$runtime_root/murmur-ime-private" 2>/dev/null || true
removed_summary="Open Voice Input Linux user services and managed installed code were removed."
if [[ $manifest_version == 2 ]]; then
  removed_summary="Open Voice Input Linux user services, desktop entry, icon, and managed installed code were removed."
fi
printf '%s\n' \
  "$removed_summary" \
  "The private API-key, vocabulary, correction, interaction, output-style, microphone-priority, and data-collection settings were retained under the XDG config directory." \
  "Any local training dataset in a user-selected directory was retained and was not inspected or removed." \
  "No IBus daemon, Rime installation, or ~/.config/ibus/rime data was removed."
