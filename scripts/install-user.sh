#!/usr/bin/env bash
set -eEuo pipefail
unset PYTHONHOME PYTHONPATH
umask 077

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
desktop_renderer="$script_dir/render_desktop_entry.py"
bundle_verifier="$script_dir/verify_preview_bundle.py"
data_home=${XDG_DATA_HOME:-"$HOME/.local/share"}
config_home=${XDG_CONFIG_HOME:-"$HOME/.config"}
case "$data_home:$config_home" in
  /*:/*) ;;
  *) die "XDG_DATA_HOME and XDG_CONFIG_HOME must be absolute" 2 ;;
esac

if [[ -z $wheelhouse && $allow_network == false ]]; then
  wheelhouse="$repo_dir/wheelhouse"
fi
wheel_files=()
if [[ $allow_network == false ]]; then
  [[ -d $wheelhouse ]] || die \
    "No offline wheelhouse found at: $wheelhouse
Provide --wheelhouse DIR, or explicitly opt into developer downloads with --allow-network." 2
  wheelhouse=$(CDPATH= cd -- "$wheelhouse" && pwd)
  [[ -f $bundle_verifier && ! -L $bundle_verifier ]] || die \
    "The offline bundle verifier is unavailable" 2
  PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 PYTHONPATH= PYTHONHOME= \
    python3 -I "$bundle_verifier" \
    --bundle-root "$repo_dir" \
    --check-install-wheelhouse "$wheelhouse"
  mapfile -d '' -t wheel_files < <(
    find "$wheelhouse" -maxdepth 1 -type f -name '*.whl' -print0 | sort -z
  )
  ((${#wheel_files[@]} >= 1)) || die "The verified wheelhouse is empty" 2
else
  printf '%s\n' \
    "Developer mode: pip may download or reuse unlocked voice dependencies from the host or configured package indexes." >&2
fi

install_root="$data_home/murmur-ime"
unit_dir="$config_home/systemd/user"
engine_unit_path="$unit_dir/murmur-ime-engine.service"
voice_unit_path="$unit_dir/murmur-ime-voice.service"
applications_dir="$data_home/applications"
icon_dir="$data_home/icons/hicolor/scalable/apps"
desktop_entry_path="$applications_dir/io.github.SidUParis.OpenVoiceInputLinux.Settings.desktop"
settings_icon_path="$icon_dir/io.github.SidUParis.OpenVoiceInputLinux.Settings.svg"
voice_config="$config_home/murmur-ime/voice.json"
voice_vocabulary="$config_home/murmur-ime/vocabulary.json"
voice_corrections="$config_home/murmur-ime/corrections.json"
voice_launcher="$install_root/murmur-voice-daemon"
runtime_root=${XDG_RUNTIME_DIR:-}

stage_root=""
stage_root_identity=""
unit_stage_dir=""
unit_stage_identity=""
desktop_stage_dir=""
desktop_stage_identity=""
icon_stage_dir=""
icon_stage_identity=""
data_rollback_dir=""
data_rollback_identity=""
unit_rollback_dir=""
unit_rollback_identity=""
desktop_rollback_dir=""
desktop_rollback_identity=""
icon_rollback_dir=""
icon_rollback_identity=""
data_lock_fd=""
config_lock_fd=""
root_old_quarantined=false
engine_unit_old_quarantined=false
voice_unit_old_quarantined=false
root_new_committed=false
engine_unit_new_committed=false
voice_unit_new_committed=false
root_new_identity=""
engine_unit_new_identity=""
voice_unit_new_identity=""
desktop_entry_old_quarantined=false
settings_icon_old_quarantined=false
desktop_entry_new_committed=false
settings_icon_new_committed=false
desktop_entry_new_identity=""
settings_icon_new_identity=""
old_quarantine_verified=false
transaction_started=false
commit_complete=false
rollback_failed=false
engine_was_active=false
voice_was_active=false
engine_was_enabled=false
voice_was_enabled=false
exact_engine=""
existing_manifest_version=0
desktop_assets_were_managed=false

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

  # Atomically move only the inode created by this transaction into a fresh
  # private container.  A same-name symlink, file, or directory that arrived
  # later is restored/retained by quarantine-committed and is never deleted.
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
  local identity_name=${4:-}
  local identity=""
  local status
  # Do not dispatch a pending signal between the atomic rename and recording
  # that rollback owns the destination.
  trap '' INT TERM
  if identity=$(python3 -I "$manifest_helper" move-no-clobber \
    --source "$source" --destination "$destination"); then
    printf -v "$flag_name" '%s' true
    if [[ -n $identity_name ]]; then
      printf -v "$identity_name" '%s' "$identity"
    fi
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

rollback_install() {
  local failed=false
  printf '%s\n' "Installation failed; restoring the previous managed runtime." >&2
  systemctl --user stop murmur-ime-voice.service >/dev/null 2>&1 || true
  systemctl --user stop murmur-ime-engine.service >/dev/null 2>&1 || true

  # Remove a published replacement launcher before touching any other path.
  # If a process raced the failed commit, keep both quarantines and all
  # services stopped; deleting its tree or publishing the old tree over the
  # fixed path would make recovery ambiguous.
  if [[ $root_new_committed == true ]]; then
    if python3 -I "$manifest_helper" quarantine-committed \
      --source "$install_root" \
      --quarantine "$data_rollback_dir/new-root" \
      --identity "$root_new_identity"; then
      if ! managed_processes=$(python3 -I "$manifest_helper" voice-process-count \
        --root "$data_rollback_dir/new-root" --argv-root "$install_root"); then
        keep_services_stopped
        restore_ibus_state || true
        return 1
      fi
      if [[ $managed_processes != 0 ]]; then
        printf '%s\n' \
          "A replacement voice daemon is still running; retained both runtime quarantines." >&2
        keep_services_stopped
        restore_ibus_state || true
        return 1
      fi
    else
      failed=true
    fi
  fi

  if [[ $voice_unit_old_quarantined == true \
    && -f $unit_rollback_dir/voice.service ]]; then
    if [[ $voice_unit_new_committed == true ]]; then
      python3 -I "$manifest_helper" quarantine-committed \
        --source "$voice_unit_path" \
        --quarantine "$unit_rollback_dir/new-voice.service" \
        --identity "$voice_unit_new_identity" || failed=true
    fi
    python3 -I "$manifest_helper" move-no-clobber \
      --source "$unit_rollback_dir/voice.service" \
      --destination "$voice_unit_path" >/dev/null || failed=true
  elif [[ $voice_unit_new_committed == true ]]; then
    python3 -I "$manifest_helper" quarantine-committed \
      --source "$voice_unit_path" \
      --quarantine "$unit_rollback_dir/new-voice.service" \
      --identity "$voice_unit_new_identity" || failed=true
  fi
  if [[ $settings_icon_old_quarantined == true \
    && -f $icon_rollback_dir/icon.svg ]]; then
    if [[ $settings_icon_new_committed == true ]]; then
      python3 -I "$manifest_helper" quarantine-committed \
        --source "$settings_icon_path" \
        --quarantine "$icon_rollback_dir/new-icon.svg" \
        --identity "$settings_icon_new_identity" || failed=true
    fi
    python3 -I "$manifest_helper" move-no-clobber \
      --source "$icon_rollback_dir/icon.svg" \
      --destination "$settings_icon_path" >/dev/null || failed=true
  elif [[ $settings_icon_new_committed == true ]]; then
    python3 -I "$manifest_helper" quarantine-committed \
      --source "$settings_icon_path" \
      --quarantine "$icon_rollback_dir/new-icon.svg" \
      --identity "$settings_icon_new_identity" || failed=true
  fi
  if [[ $desktop_entry_old_quarantined == true \
    && -f $desktop_rollback_dir/settings.desktop ]]; then
    if [[ $desktop_entry_new_committed == true ]]; then
      python3 -I "$manifest_helper" quarantine-committed \
        --source "$desktop_entry_path" \
        --quarantine "$desktop_rollback_dir/new-settings.desktop" \
        --identity "$desktop_entry_new_identity" || failed=true
    fi
    python3 -I "$manifest_helper" move-no-clobber \
      --source "$desktop_rollback_dir/settings.desktop" \
      --destination "$desktop_entry_path" >/dev/null || failed=true
  elif [[ $desktop_entry_new_committed == true ]]; then
    python3 -I "$manifest_helper" quarantine-committed \
      --source "$desktop_entry_path" \
      --quarantine "$desktop_rollback_dir/new-settings.desktop" \
      --identity "$desktop_entry_new_identity" || failed=true
  fi
  if [[ $engine_unit_old_quarantined == true \
    && -f $unit_rollback_dir/engine.service ]]; then
    if [[ $engine_unit_new_committed == true ]]; then
      python3 -I "$manifest_helper" quarantine-committed \
        --source "$engine_unit_path" \
        --quarantine "$unit_rollback_dir/new-engine.service" \
        --identity "$engine_unit_new_identity" || failed=true
    fi
    python3 -I "$manifest_helper" move-no-clobber \
      --source "$unit_rollback_dir/engine.service" \
      --destination "$engine_unit_path" >/dev/null || failed=true
  elif [[ $engine_unit_new_committed == true ]]; then
    python3 -I "$manifest_helper" quarantine-committed \
      --source "$engine_unit_path" \
      --quarantine "$unit_rollback_dir/new-engine.service" \
      --identity "$engine_unit_new_identity" || failed=true
  fi
  # Restore the old launcher path last, after the replacement has been safely
  # isolated and every other owned path has been rolled back.
  if [[ $failed == false \
    && $root_old_quarantined == true && -d $data_rollback_dir/root ]]; then
    python3 -I "$manifest_helper" move-no-clobber \
      --source "$data_rollback_dir/root" \
      --destination "$install_root" >/dev/null || failed=true
  fi
  if [[ $old_quarantine_verified != true \
    && ($root_old_quarantined == true \
      || $engine_unit_old_quarantined == true \
      || $voice_unit_old_quarantined == true \
      || $desktop_entry_old_quarantined == true \
      || $settings_icon_old_quarantined == true) ]]; then
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
    rollback_install || rollback_failed=true
  fi
  if [[ $rollback_failed == true ]]; then
    printf '%s\n' \
      "Automatic rollback was incomplete; do not remove the retained installation files." >&2
    report_retained_path "$stage_root"
    report_retained_path "$unit_stage_dir"
    report_retained_path "$desktop_stage_dir"
    report_retained_path "$icon_stage_dir"
    report_retained_path "$data_rollback_dir"
    report_retained_path "$unit_rollback_dir"
    report_retained_path "$desktop_rollback_dir"
    report_retained_path "$icon_rollback_dir"
    status=1
  else
    remove_private_tree "$stage_root" "$stage_root_identity" || cleanup_failed=true
    remove_private_tree "$unit_stage_dir" "$unit_stage_identity" || cleanup_failed=true
    remove_private_tree "$desktop_stage_dir" "$desktop_stage_identity" || cleanup_failed=true
    remove_private_tree "$icon_stage_dir" "$icon_stage_identity" || cleanup_failed=true
    remove_private_tree "$data_rollback_dir" "$data_rollback_identity" || cleanup_failed=true
    remove_private_tree "$unit_rollback_dir" "$unit_rollback_identity" || cleanup_failed=true
    remove_private_tree "$desktop_rollback_dir" "$desktop_rollback_identity" || cleanup_failed=true
    remove_private_tree "$icon_rollback_dir" "$icon_rollback_identity" || cleanup_failed=true
    if [[ $cleanup_failed == true ]]; then
      if [[ $commit_complete == true ]]; then
        printf '%s\n' \
          "Installation committed, but cleanup was incomplete; retained locations are listed above." >&2
      else
        printf '%s\n' \
          "Installation failed and cleanup was incomplete; retained locations are listed above." >&2
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

python3 -I -c \
  "import gi; gi.require_version('IBus', '1.0'); from gi.repository import IBus"
PYTHONPATH= PYTHONHOME= python3 -I -m venv --help >/dev/null
[[ -f $manifest_helper && ! -L $manifest_helper ]] || die \
  "The installation manifest helper is unavailable" 2
[[ -f $desktop_renderer && ! -L $desktop_renderer ]] || die \
  "The desktop-entry renderer is unavailable" 2
systemctl --user show-environment >/dev/null || die \
  "A working systemd user manager is required" 2
python3 -I "$manifest_helper" secure-dir \
  --path "$data_home" --kind "XDG data" --create
python3 -I "$manifest_helper" secure-dir \
  --path "$config_home" --kind "XDG config" --create
python3 -I "$manifest_helper" require-disjoint \
  --left "$data_home" --right "$config_home"
command -v flock >/dev/null 2>&1 || die \
  "The flock utility is required for safe installation" 2
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

preflight_core_present=false
if [[ -e $install_root || -L $install_root \
  || -e $engine_unit_path || -L $engine_unit_path \
  || -e $voice_unit_path || -L $voice_unit_path ]]; then
  preflight_core_present=true
fi
if [[ $preflight_core_present == false \
  && (-e $desktop_entry_path || -L $desktop_entry_path \
    || -e $settings_icon_path || -L $settings_icon_path) ]]; then
  die "Refusing to replace same-name desktop assets without a trusted ownership manifest" 2
fi

python3 -I "$manifest_helper" secure-dir \
  --path "$unit_dir" --kind "systemd user unit" --create
python3 -I "$manifest_helper" require-disjoint \
  --left "$install_root" --right "$config_home"
python3 -I "$manifest_helper" require-disjoint \
  --left "$data_home" --right "$unit_dir"
python3 -I "$manifest_helper" require-disjoint \
  --left "$install_root" --right "$unit_dir"

root_present=false
engine_unit_present=false
voice_unit_present=false
desktop_entry_present=false
settings_icon_present=false
[[ -e $install_root || -L $install_root ]] && root_present=true
[[ -e $engine_unit_path || -L $engine_unit_path ]] && engine_unit_present=true
[[ -e $voice_unit_path || -L $voice_unit_path ]] && voice_unit_present=true
[[ -e $desktop_entry_path || -L $desktop_entry_path ]] && desktop_entry_present=true
[[ -e $settings_icon_path || -L $settings_icon_path ]] && settings_icon_present=true

existing_install=false
install_id=""
if [[ $root_present == true || $engine_unit_present == true || $voice_unit_present == true ]]; then
  if [[ $root_present != true || $engine_unit_present != true || $voice_unit_present != true ]]; then
    die "Refusing to replace a partial or unowned installation" 2
  fi
  if ! manifest_identity=$(python3 -I "$manifest_helper" verify \
    --root "$install_root" \
    --engine-unit "$engine_unit_path" \
    --voice-unit "$voice_unit_path" \
    --desktop-entry "$desktop_entry_path" \
    --settings-icon "$settings_icon_path" \
    --print-version); then
    die "Refusing to replace files without a trusted ownership manifest" 2
  fi
  read -r install_id existing_manifest_version extra_identity <<<"$manifest_identity"
  [[ -n $install_id && -z ${extra_identity:-} ]] || die \
    "The trusted ownership manifest returned an invalid identity" 2
  if [[ $existing_manifest_version == 1 ]]; then
    if [[ $desktop_entry_present == true || $settings_icon_present == true ]]; then
      die "Refusing to replace same-name desktop assets not owned by the v1 installation" 2
    fi
  elif [[ $existing_manifest_version == 2 ]]; then
    desktop_assets_were_managed=true
  else
    die "The trusted ownership manifest has an unsupported version" 2
  fi
  existing_install=true
else
  if [[ $desktop_entry_present == true || $settings_icon_present == true ]]; then
    die "Refusing to replace same-name desktop assets without a trusted ownership manifest" 2
  fi
  install_id=$(python3 -I "$manifest_helper" new-id)
fi
python3 -I "$manifest_helper" secure-dir \
  --path "$applications_dir" --kind "desktop entry" --create
python3 -I "$manifest_helper" secure-dir \
  --path "$data_home/icons" --kind "icon theme" --create
python3 -I "$manifest_helper" secure-dir \
  --path "$data_home/icons/hicolor" --kind "hicolor icon theme" --create
python3 -I "$manifest_helper" secure-dir \
  --path "$data_home/icons/hicolor/scalable" --kind "scalable icon theme" --create
python3 -I "$manifest_helper" secure-dir \
  --path "$icon_dir" --kind "application icon" --create
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
# to run. Every staged asset is a sibling of its final destination, so later
# renames stay on one filesystem.
stage_root=$(mktemp -d "$data_home/.murmur-ime.stage.XXXXXX")
stage_root_identity=$(private_tree_identity "$stage_root") || die \
  "The installation staging directory identity could not be recorded"
unit_stage_dir=$(mktemp -d "$unit_dir/.murmur-ime.stage.XXXXXX")
unit_stage_identity=$(private_tree_identity "$unit_stage_dir") || die \
  "The unit staging directory identity could not be recorded"
desktop_stage_dir=$(mktemp -d "$applications_dir/.murmur-ime.stage.XXXXXX")
desktop_stage_identity=$(private_tree_identity "$desktop_stage_dir") || die \
  "The desktop staging directory identity could not be recorded"
icon_stage_dir=$(mktemp -d "$icon_dir/.murmur-ime.stage.XXXXXX")
icon_stage_identity=$(private_tree_identity "$icon_stage_dir") || die \
  "The icon staging directory identity could not be recorded"
chmod 0700 "$stage_root" "$unit_stage_dir" "$desktop_stage_dir" "$icon_stage_dir"
stage_package="$stage_root/murmur_ime_engine"
stage_venv="$stage_root/voice-venv"
stage_marker="$stage_venv/.murmur-ime-managed"
stage_wheelhouse="$stage_root/install-wheelhouse"
stage_wheelhouse_identity=""
stage_engine_unit="$unit_stage_dir/murmur-ime-engine.service"
stage_voice_unit="$unit_stage_dir/murmur-ime-voice.service"
stage_desktop_entry="$desktop_stage_dir/io.github.SidUParis.OpenVoiceInputLinux.Settings.desktop"
stage_settings_icon="$icon_stage_dir/io.github.SidUParis.OpenVoiceInputLinux.Settings.svg"

if [[ $allow_network == false ]]; then
  install -d -m 0700 "$stage_wheelhouse"
  stage_wheelhouse_identity=$(private_tree_identity "$stage_wheelhouse") || die \
    "The staged wheelhouse identity could not be recorded"
  staged_wheel_files=()
  for wheel in "${wheel_files[@]}"; do
    staged_wheel="$stage_wheelhouse/$(basename -- "$wheel")"
    install -m 0400 -- "$wheel" "$staged_wheel"
    staged_wheel_files+=("$staged_wheel")
  done
  PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 PYTHONPATH= PYTHONHOME= \
    python3 -I "$bundle_verifier" \
    --bundle-root "$repo_dir" \
    --check-install-wheelhouse "$stage_wheelhouse"
  wheel_files=("${staged_wheel_files[@]}")
fi
install -d -m 0755 "$stage_package"
install -m 0755 \
  "$repo_dir/engine/murmur-ime-engine" \
  "$stage_root/murmur-ime-engine"
for source in "$repo_dir"/engine/murmur_ime_engine/*.py; do
  install -m 0644 "$source" "$stage_package/$(basename -- "$source")"
done
PYTHONPATH= PYTHONHOME= python3 -I -m venv \
  --system-site-packages "$stage_venv"
if [[ $allow_network == true ]]; then
  PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 "$stage_venv/bin/python" -m pip install \
    --disable-pip-version-check --no-input --upgrade "$repo_dir/voice"
else
  PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 PYTHONPATH= PYTHONHOME= \
    "$stage_venv/bin/python" -I -m pip --isolated install \
    --disable-pip-version-check --no-input --no-index --no-cache-dir \
    --ignore-installed --no-deps --find-links "$stage_wheelhouse" \
    "${wheel_files[@]}"
  PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 PYTHONPATH= PYTHONHOME= \
    "$stage_venv/bin/python" -I "$bundle_verifier" \
    --bundle-root "$repo_dir" \
    --check-install-wheelhouse "$stage_wheelhouse" \
    --check-installed-venv "$stage_venv"
  remove_private_tree "$stage_wheelhouse" "$stage_wheelhouse_identity"
  stage_wheelhouse=""
fi
printf '%s\n' "$install_id" >"$stage_marker"
chmod 0600 "$stage_marker"
PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 PYTHONPATH= PYTHONHOME= \
  "$stage_venv/bin/python" -I -c \
  "import gi, sounddevice, websockets, murmur_voice; gi.require_version('Gio', '2.0'); gi.require_version('Gtk', '4.0'); from gi.repository import Gio, Gtk"
install -m 0755 "$repo_dir/packaging/murmur-voice-daemon" \
  "$stage_root/murmur-voice-daemon"
install -m 0755 "$repo_dir/packaging/open-voice-input-settings" \
  "$stage_root/open-voice-input-settings"

python3 -I "$desktop_renderer" \
  --template "$repo_dir/packaging/desktop/io.github.SidUParis.OpenVoiceInputLinux.Settings.desktop.in" \
  --output "$stage_desktop_entry" \
  --set "SETTINGS_EXEC=$install_root/open-voice-input-settings"
install -m 0644 \
  "$repo_dir/packaging/icons/io.github.SidUParis.OpenVoiceInputLinux.Settings.svg" \
  "$stage_settings_icon"
chmod 0644 "$stage_desktop_entry"
if command -v desktop-file-validate >/dev/null 2>&1; then
  desktop-file-validate "$stage_desktop_entry"
fi

python3 -I "$script_dir/render_systemd_units.py" \
  --template "$repo_dir/packaging/systemd/murmur-ime-engine.service.in" \
  --output "$stage_engine_unit" \
  --set "ENGINE_EXEC=$install_root/murmur-ime-engine"
python3 -I "$script_dir/render_systemd_units.py" \
  --template "$repo_dir/packaging/systemd/murmur-ime-voice.service.in" \
  --output "$stage_voice_unit" \
  --set "VOICE_EXEC=$install_root/murmur-voice-daemon" \
  --set "VOICE_CONFIG=$voice_config" \
  --set "VOICE_VOCABULARY=$voice_vocabulary" \
  --set "VOICE_CORRECTIONS=$voice_corrections"
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
if [[ $existing_install == true ]]; then
  [[ $runtime_root == /* ]] || die \
    "XDG_RUNTIME_DIR is required and must be absolute for safe upgrade" 2
fi
transaction_started=true
if [[ $existing_install == true ]]; then
  # A manually launched installed daemon is not represented by systemd state.
  # Probe only the fixed private socket, use the fixed shutdown argv, and then
  # count exact managed Python argv so custom-socket daemons cannot survive the
  # installation-tree replacement.
  runtime_dir="$runtime_root/murmur-ime"
  socket_path="$runtime_dir/voice.sock"
  socket_status=$(python3 -I "$manifest_helper" socket-state \
    --runtime-root "$runtime_root" --path "$socket_path")
  if [[ $socket_status == live ]]; then
    "$voice_launcher" shutdown --socket "$socket_path" >/dev/null || die \
      "The live voice daemon refused a controlled shutdown before upgrade"
    for _ in {1..30}; do
      socket_status=$(python3 -I "$manifest_helper" socket-state \
        --runtime-root "$runtime_root" --path "$socket_path")
      [[ $socket_status != live ]] && break
      sleep 0.1
    done
    [[ $socket_status != live ]] || die \
      "The live voice daemon did not release its control socket before upgrade"
  fi
  if [[ $socket_status == stale ]]; then
    rm -f -- "$socket_path"
  fi
fi
if [[ $voice_was_active == true || $existing_install == true ]]; then
  systemctl --user stop murmur-ime-voice.service
fi
service_active murmur-ime-voice.service && die \
  "The voice service did not stop before replacement"
if [[ $existing_install == true ]]; then
  managed_processes=$(python3 -I "$manifest_helper" voice-process-count \
    --root "$install_root")
  [[ $managed_processes == 0 ]] || die \
    "A managed foreground voice daemon is still running; no files were replaced"
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
python3 -I "$manifest_helper" create \
  --root "$stage_root" \
  --engine-unit "$stage_engine_unit" \
  --voice-unit "$stage_voice_unit" \
  --desktop-entry "$stage_desktop_entry" \
  --settings-icon "$stage_settings_icon" \
  --output "$stage_root/install-manifest.json" \
  --install-id "$install_id"
python3 -I "$manifest_helper" verify \
  --root "$stage_root" \
  --engine-unit "$stage_engine_unit" \
  --voice-unit "$stage_voice_unit" \
  --desktop-entry "$stage_desktop_entry" \
  --settings-icon "$stage_settings_icon" \
  --staged >/dev/null

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

# Close the last practical launch race before the old tree is quarantined.  We
# never signal an argv-only match; a nonzero count is a hard, non-destructive
# stop that leaves rollback responsible for restoring service state.
if [[ $existing_install == true ]]; then
  managed_processes=$(python3 -I "$manifest_helper" voice-process-count \
    --root "$install_root")
  [[ $managed_processes == 0 ]] || die \
    "A managed foreground voice daemon started before replacement; no files were replaced"
fi

data_rollback_dir=$(mktemp -d "$data_home/.murmur-ime.rollback.XXXXXX")
data_rollback_identity=$(private_tree_identity "$data_rollback_dir") || die \
  "The data rollback directory identity could not be recorded"
unit_rollback_dir=$(mktemp -d "$unit_dir/.murmur-ime.rollback.XXXXXX")
unit_rollback_identity=$(private_tree_identity "$unit_rollback_dir") || die \
  "The unit rollback directory identity could not be recorded"
desktop_rollback_dir=$(mktemp -d "$applications_dir/.murmur-ime.rollback.XXXXXX")
desktop_rollback_identity=$(private_tree_identity "$desktop_rollback_dir") || die \
  "The desktop rollback directory identity could not be recorded"
icon_rollback_dir=$(mktemp -d "$icon_dir/.murmur-ime.rollback.XXXXXX")
icon_rollback_identity=$(private_tree_identity "$icon_rollback_dir") || die \
  "The icon rollback directory identity could not be recorded"
chmod 0700 \
  "$data_rollback_dir" "$unit_rollback_dir" \
  "$desktop_rollback_dir" "$icon_rollback_dir"
if [[ $existing_install == true ]]; then
  move_no_clobber_with_flag \
    "$install_root" "$data_rollback_dir/root" root_old_quarantined
  # Removing the fixed launcher path closes the remaining count-to-rename
  # window.  Count once more against the trusted quarantined interpreter while
  # matching the argv path that already-running processes retain.
  managed_processes=$(python3 -I "$manifest_helper" voice-process-count \
    --root "$data_rollback_dir/root" --argv-root "$install_root")
  if [[ $managed_processes != 0 ]]; then
    if [[ $desktop_assets_were_managed == true ]]; then
      python3 -I "$manifest_helper" verify \
        --root "$data_rollback_dir/root" \
        --engine-unit "$engine_unit_path" \
        --voice-unit "$voice_unit_path" \
        --desktop-entry "$desktop_entry_path" \
        --settings-icon "$settings_icon_path" \
        --staged >/dev/null
    else
      python3 -I "$manifest_helper" verify \
        --root "$data_rollback_dir/root" \
        --engine-unit "$engine_unit_path" \
        --voice-unit "$voice_unit_path" \
        --staged >/dev/null
    fi
    old_quarantine_verified=true
    die "A managed foreground voice daemon raced with upgrade; the old tree was restored"
  fi
  move_no_clobber_with_flag \
    "$engine_unit_path" "$unit_rollback_dir/engine.service" \
    engine_unit_old_quarantined
  move_no_clobber_with_flag \
    "$voice_unit_path" "$unit_rollback_dir/voice.service" \
    voice_unit_old_quarantined
  if [[ $desktop_assets_were_managed == true ]]; then
    move_no_clobber_with_flag \
      "$desktop_entry_path" "$desktop_rollback_dir/settings.desktop" \
      desktop_entry_old_quarantined
    move_no_clobber_with_flag \
      "$settings_icon_path" "$icon_rollback_dir/icon.svg" \
      settings_icon_old_quarantined
    python3 -I "$manifest_helper" verify \
      --root "$data_rollback_dir/root" \
      --engine-unit "$unit_rollback_dir/engine.service" \
      --voice-unit "$unit_rollback_dir/voice.service" \
      --desktop-entry "$desktop_rollback_dir/settings.desktop" \
      --settings-icon "$icon_rollback_dir/icon.svg" \
      --staged >/dev/null
  else
    python3 -I "$manifest_helper" verify \
      --root "$data_rollback_dir/root" \
      --engine-unit "$unit_rollback_dir/engine.service" \
      --voice-unit "$unit_rollback_dir/voice.service" \
      --staged >/dev/null
  fi
  old_quarantine_verified=true
fi

# Revalidate both the replacement and the unclaimed destinations at the final
# commit boundary.  The no-clobber publications below remain atomic if a path
# appears after this check.
python3 -I "$manifest_helper" verify \
  --root "$stage_root" \
  --engine-unit "$stage_engine_unit" \
  --voice-unit "$stage_voice_unit" \
  --desktop-entry "$stage_desktop_entry" \
  --settings-icon "$stage_settings_icon" \
  --staged >/dev/null
python3 -I "$manifest_helper" require-absent \
  --path "$install_root" \
  --path "$engine_unit_path" \
  --path "$voice_unit_path" \
  --path "$desktop_entry_path" \
  --path "$settings_icon_path"

move_no_clobber_with_flag \
  "$stage_root" "$install_root" root_new_committed root_new_identity
stage_root=""
move_no_clobber_with_flag \
  "$stage_engine_unit" "$engine_unit_path" \
  engine_unit_new_committed engine_unit_new_identity
move_no_clobber_with_flag \
  "$stage_voice_unit" "$voice_unit_path" \
  voice_unit_new_committed voice_unit_new_identity
move_no_clobber_with_flag \
  "$stage_desktop_entry" "$desktop_entry_path" \
  desktop_entry_new_committed desktop_entry_new_identity
move_no_clobber_with_flag \
  "$stage_settings_icon" "$settings_icon_path" \
  settings_icon_new_committed settings_icon_new_identity

# Do not load or enable the replacement until its manifest validates at the
# actual fixed paths reached by launchers and the desktop shell.
python3 -I "$manifest_helper" verify \
  --root "$install_root" \
  --engine-unit "$engine_unit_path" \
  --voice-unit "$voice_unit_path" \
  --desktop-entry "$desktop_entry_path" \
  --settings-icon "$settings_icon_path" >/dev/null

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
    "$install_root/voice-venv/bin/python" -I -c \
    "from murmur_voice.config import load_config; import sys; load_config(sys.argv[1])" \
    "$voice_config" >/dev/null 2>&1; then
  voice_config_ready=true
fi
voice_vocabulary_ready=false
if PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 \
  "$install_root/voice-venv/bin/python" -I -c \
  "from murmur_voice.config import load_vocabulary; import sys; load_vocabulary(sys.argv[1])" \
  "$voice_vocabulary" >/dev/null 2>&1; then
  voice_vocabulary_ready=true
fi
voice_corrections_ready=false
if PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 \
  "$install_root/voice-venv/bin/python" -I -c \
  "from murmur_voice.config import load_corrections; import sys; load_corrections(sys.argv[1])" \
  "$voice_corrections" >/dev/null 2>&1; then
  voice_corrections_ready=true
fi
if [[ $voice_config_ready == true && $voice_vocabulary_ready == true && $voice_corrections_ready == true ]]; then
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
if [[ $voice_corrections_ready == false ]]; then
  printf '%s\n' \
    "Replace or clear the invalid private recognition corrections with the settings window:" \
    "  $install_root/open-voice-input-settings"
fi
if [[ $voice_config_ready == false || $voice_vocabulary_ready == false || $voice_corrections_ready == false ]]; then
  printf '%s\n' \
    "Then enable and start the idle service with:" \
    "  systemctl --user enable --now murmur-ime-voice.service"
fi
printf '%s\n' \
  "Open the native settings window with: $install_root/open-voice-input-settings" \
  "Bind a desktop shortcut to: $install_root/murmur-voice-daemon toggle" \
  "Inspect service state with: systemctl --user status murmur-ime-voice.service"
