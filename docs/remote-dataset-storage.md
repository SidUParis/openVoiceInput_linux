# Remote dataset storage

## 中文速览

训练数据采集默认关闭。设置页只接受一个**已经存在的绝对文件系统路径**：
它可以是本机目录，也可以是操作系统已经挂载好的 SSHFS 等远程目录。应用
不会接收 `ssh://`、`sftp://`、Google Drive URL，也不会替用户登录或挂载。

- Orange 可以先用 SSHFS 挂载，再在设置页选择本机挂载点。不要把本文中的
  示例主机名、用户名和路径原样照抄，也不要把私人主机、IP 或密钥写进仓库。
- 远程挂载断线时没有本地备用 spool；正常听写继续，但该条尚未发布的数据
  可能丢失。计划卸载前应先关闭采集，重连时必须把同一个远程目录挂回同一个
  本机路径。
- Google Drive 不适合作为采集器的直接写入挂载点。先在本机或 Orange 完整
  发布记录，再用 rclone 在后台执行 `copy`。Google OAuth 必须由用户在浏览器
  中明确授权；Open Voice Input Linux 不读取或保存该 OAuth token。
- `provider_final` 始终是 `teacher-unreviewed` 未审核伪标签；远程存储或备份
  不会把它变成 gold label。
- schema-v4 `delivery` 是实际交付到冻结目标但仍
  `machine-derived-unreviewed` 的文本及可重放删除审计；它与原始
  `provider_final`、仍为空的人工标签分开，并由 `target` 标明 `caret` 或
  `clipboard`；备份不会改变这些语义。

下面的命令使用公开占位符，不包含任何真实 Orange 地址或凭据。

## What the application accepts

Collection remains off until the user selects a destination, enables retention
of WAV, raw recognition, and actual delivery, and saves the choice. The
destination must be an existing absolute path with usable POSIX ownership,
permission, file-sync, and same-filesystem directory-rename behaviour. It can
be either:

1. a normal local folder; or
2. a remote filesystem which the operating system has already exposed at a
   local mount path, such as an SSHFS mount.

The application does not parse a remote URI, establish SSH, mount a filesystem,
or upload to a cloud API. There is no fallback local spool. Select the
**parent** folder; the application creates or reopens
`openvoiceinput-dataset-v1` below it. Do not select the dataset child itself.

The selected filesystem must report the current local user as owner and permit
private `0700` directories and `0600` files. Saving an enabled destination
initializes and validates the marked dataset. A backend that cannot provide
these semantics is rejected; passing initialization does not guarantee that a
network mount will remain connected.

## Option A: mount Orange or another SSH host with SSHFS

SSHFS uses the host's normal SSH/SFTP access. Verify the host key through a
trusted channel before accepting a first or changed fingerprint, and use the
SSH account/key that you already control. The repository and application must
never contain that host's address, private key, password, or mount-specific
credential.

Install SSHFS using the operating system package manager. On Ubuntu:

```bash
sudo apt install sshfs
```

Create a private destination on the remote computer and a local mount point.
Replace every placeholder below with your own values:

```bash
ssh your-user@orange-host \
  'mkdir -p /absolute/remote/path/openvoiceinput && chmod 700 /absolute/remote/path/openvoiceinput'
mkdir -p "$HOME/mnt/openvoice-orange"
chmod 700 "$HOME/mnt/openvoice-orange"
```

Mount it for the current desktop user:

```bash
sshfs \
  your-user@orange-host:/absolute/remote/path/openvoiceinput \
  "$HOME/mnt/openvoice-orange" \
  -o reconnect,ServerAliveInterval=15,ServerAliveCountMax=3,idmap=user,umask=0077
```

The SSHFS `umask=0077` option restricts the **local view**, but it does not by
itself guarantee the modes stored on the remote server. The remote SFTP
server's creation policy can still produce broader `0755` directories or
`0644` files. Do not treat the mount option as proof of remote privacy.

With an OpenSSH server, SSHFS can optionally start the server-side SFTP helper
with a private umask:

```bash
sshfs \
  your-user@orange-host:/absolute/remote/path/openvoiceinput \
  "$HOME/mnt/openvoice-orange" \
  -o reconnect,ServerAliveInterval=15,ServerAliveCountMax=3,idmap=user,umask=0077 \
  -o "sftp_server=/usr/lib/openssh/sftp-server -u 077"
```

The helper path is distribution-specific. Confirm that exact executable path
on the remote host before using this option; a different SFTP implementation
may require a different server-side configuration.

Confirm that the path is really mounted before enabling or re-saving
collection:

```bash
mountpoint -q "$HOME/mnt/openvoice-orange"
findmnt -T "$HOME/mnt/openvoice-orange"
stat -c 'owner=%u mode=%a type=%F' "$HOME/mnt/openvoice-orange"
```

Then open the Open Voice Input Linux settings window, choose
`$HOME/mnt/openvoice-orange`, enable collection, and save. The literal `$HOME`
text is only explanatory; the folder chooser stores the expanded absolute
path.

After the application creates `openvoiceinput-dataset-v1`, verify modes on the
**remote host itself**, not only through the masked local SSHFS view:

```bash
ssh your-user@orange-host \
  'stat -c "mode=%a owner=%U:%G path=%n" \
    /absolute/remote/path/openvoiceinput/openvoiceinput-dataset-v1 \
    /absolute/remote/path/openvoiceinput/openvoiceinput-dataset-v1/dataset.json \
    /absolute/remote/path/openvoiceinput/openvoiceinput-dataset-v1/.pending \
    /absolute/remote/path/openvoiceinput/openvoiceinput-dataset-v1/utterances \
    /absolute/remote/path/openvoiceinput/openvoiceinput-dataset-v1/usage'
```

The four directories should be `0700` and `dataset.json` should be `0600`,
owned by the intended remote account. Repeat the remote-side check on a sample
published utterance before relying on the dataset: its directory should be
`0700` and `audio.wav`/`record.json` should be `0600`. The dataset-level
`usage/` directory should be `0700`, and its `<utterance_id>.json` summaries
should be `0600`. If the remote modes are
broader, disable collection and correct the server-side SFTP/umask policy first.

If saving reports a permission or initialization error, do not weaken the
dataset permissions. Check the remote directory ownership and SSHFS mapping.
If that filesystem cannot expose the required semantics, collect into a local
folder and copy complete records afterward.

### Planned disconnect or unmount

First disable collection in settings and let any accepted record finish. Then:

```bash
fusermount3 -u "$HOME/mnt/openvoice-orange"
```

On systems that provide only the older helper, use `fusermount -u` instead.
Never enable or re-save collection merely because the unmounted mount-point
directory still exists: that could initialize a different local dataset under
the empty mount point.

### Unexpected disconnect

SSHFS normally blocks an in-flight filesystem operation until the connection
returns or fails. `ServerAliveInterval` bounds detection of a dead SSH
connection, and `reconnect` attempts a new connection, but neither option is a
durability guarantee. The [SSHFS documentation](https://github.com/libfuse/sshfs/blob/master/sshfs.rst)
explicitly warns that interrupted in-flight reads or writes can fail or lose
data.

Open Voice Input Linux keeps ordinary dictation independent of the optional
writer. A disconnected or stalled mount can therefore cause
`data-collection-failed`, fill the bounded writer queue, or lose the current
unpublished staged record without stopping accepted text from reaching the
focused application. There is no automatic local replay. A record is usable
only after its directory appears under `utterances/`; `.pending` is not a
published sample.

Reconnect the **same remote directory at the same local mount path** and verify
it with `mountpoint`/`findmnt`. The existing `dataset.json` identity must match
the saved collection setting. Repeated identity or permission errors should be
handled by disabling collection and inspecting the mount, not by deleting or
replacing the marker.

## Option B: back up complete records to Google Drive with rclone

Do not choose a Google Drive browser URL, a GVfs Drive location, or an
`rclone mount` as the live collection destination. Google Drive is a cloud
object API and its mounted views do not reliably promise the ownership,
`fsync`, and atomic publication semantics required by this collector. The
supported workflow is:

1. collect into a normal local folder or a compatible Orange/SSHFS folder;
2. let records publish under `utterances/`; and
3. copy the completed dataset to Drive asynchronously.

Install rclone from a trusted distribution or follow its
[official installation instructions](https://rclone.org/install/). Configure a
Drive remote interactively:

```bash
rclone config
```

Create a remote such as `gdrive`, select the Google Drive backend, and complete
the browser OAuth consent. The current
[rclone Google Drive guide](https://rclone.org/drive/) explains the OAuth flow
and current Google client-ID requirements. Authorization must be completed by
the Google account owner; Open Voice Input Linux cannot silently create,
approve, or recover that OAuth grant. Treat rclone's configuration and refresh
token as private data and never commit them to this repository.

Test the configured remote without exposing its token:

```bash
rclone lsd gdrive:
```

Assuming the selected collection parent is `$HOME/openvoiceinput-storage`, a
non-destructive backup is:

```bash
DATASET_ROOT="$HOME/openvoiceinput-storage/openvoiceinput-dataset-v1"
rclone copy \
  "$DATASET_ROOT" \
  gdrive:OpenVoiceInput/openvoiceinput-dataset-v1 \
  --checksum \
  --exclude '/.pending/**'
```

`rclone copy` skips unchanged files and does not delete extra destination
files. This is safer for personal recordings than `rclone sync`, whose
destination-deletion behaviour is not needed here. See the official
[`rclone copy` reference](https://rclone.org/commands/rclone_copy/).

To start the same copy as a transient background user service:

```bash
DATASET_ROOT="$HOME/openvoiceinput-storage/openvoiceinput-dataset-v1"
RCLONE_BIN="$(command -v rclone)"
systemd-run --user \
  --unit=openvoiceinput-gdrive-backup \
  --collect \
  --property=Nice=10 \
  "$RCLONE_BIN" copy \
  "$DATASET_ROOT" \
  gdrive:OpenVoiceInput/openvoiceinput-dataset-v1 \
  --checksum \
  --exclude '/.pending/**'
```

Inspect its result without opening the OAuth configuration:

```bash
systemctl --user status openvoiceinput-gdrive-backup.service
journalctl --user-unit openvoiceinput-gdrive-backup.service --no-pager
```

The same rclone workflow can run on Orange after a record has been completely
published there. A headless Orange setup still requires an explicit OAuth
authorization flow described by rclone; copying an OAuth token between
machines is a separate user-controlled credential decision, not an application
feature.

After a backup, an optional one-way comparison is:

```bash
rclone check \
  "$DATASET_ROOT" \
  gdrive:OpenVoiceInput/openvoiceinput-dataset-v1 \
  --one-way \
  --exclude '/.pending/**'
```

Rerunning `copy` is the recovery path after a network or quota failure. Backup
success changes only storage redundancy: `provider_final` and machine-derived
`delivery` remain unreviewed in their separate roles, and
`spoken_verbatim`/`preferred_output` remain unreviewed until the separate human
review workflow exists.
