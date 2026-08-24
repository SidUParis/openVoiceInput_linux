# Open Voice Input Linux：中文快速上手

Open Voice Input Linux 是一个面向 Linux/IBus 的轻量语音输入技术预览。
录音时，火山引擎返回的累计识别草稿会直接显示在当前应用的光标位置；
二遍识别完成后，最终文本只提交一次。主路径不弹出转写黑框，也不使用
剪贴板或模拟 `Ctrl+V`。

## 当前能力与边界

- 支持 Ubuntu 24.04 x86_64、IBus，以及实现了标准 IBus preedit 的应用；
- 使用火山引擎 `bigmodel_async`、二遍识别、DDC、ITN、标点和智能分句；
- 可配置个人词表，以及明确的“误识别写法 → 正确写法”纠错对；
- 密码、PIN、隐私字段、失去焦点、取消和过期会话不会提交文字；
- 普通键盘输入与网络、麦克风进程隔离，语音服务故障不应阻塞键盘；
- 单次听写最长 600 秒，停止后最多等待最终结果 20 秒；
- 当前版本是过渡实现：听写期间临时选择 `murmur-voice`，结束或取消后
  恢复原来的 IBus 引擎。真正把 librime／雾凇键盘输入与语音合成到同一
  IBus 引擎仍在开发中。

## 准备火山引擎

用户需要在自己的火山引擎项目中开通对应的大模型流式语音识别服务，
费用、配额和数据处理规则均属于该用户自己的账户。程序不内置共享 Key。

设置完成后，本软件必需的远端配置只有该用户自己的 API Key。不要把 Key
放进 issue、截图、日志、命令参数或 Git；一旦泄露，应立即在火山控制台
吊销并轮换。

## 安装 Ubuntu 系统依赖

离线预览包包含完整 Python wheelhouse，但不复制 Ubuntu 系统组件。软件
自身采用无 root 的当前用户安装；如果系统尚未安装 IBus、GI、GTK4、
PortAudio、`python3-venv` 或提供 `flock` 的 `util-linux`，通过 APT 补齐
这些系统组件仍需要管理员权限：

```bash
sudo apt-get update
sudo apt-get install --yes \
  ibus gir1.2-ibus-1.0 gir1.2-gtk-4.0 \
  libportaudio2 python3-gi python3-venv util-linux
```

## 安装经过 CI 校验的离线预览

只从本仓库 `main` 分支 push 产生的 Actions artifact 安装，并先确认
artifact 名中的完整 commit SHA 属于受信任的 `main` 历史。Pull request
仍会构建和验证，但不会上传可安装 artifact；校验和不能单独证明发布者身份。

从对应 GitHub Actions 运行下载 `.tar.gz` 和 `.tar.gz.sha256` 两个文件，
然后执行：

```bash
sha256sum --check openVoiceInput_linux-preview-*.tar.gz.sha256
tar -xzf openVoiceInput_linux-preview-*.tar.gz
cd openVoiceInput_linux-preview-*/
sha256sum --check SHA256SUMS
./scripts/install-user.sh
```

安装器只写入当前用户的 XDG 数据、配置和 systemd user 目录，不写入
`~/.config/ibus/rime`。它会记录并恢复安装前的精确 IBus 引擎。默认使用
包内 wheelhouse 和 `pip --no-index`；只有开发者明确传入
`--allow-network` 时才允许在线解析 Python 依赖。

## 设置 Key、词表和纠错

打开原生 GTK4 设置窗口：

```bash
~/.local/share/murmur-ime/open-voice-input-settings
```

完成受管安装后，也可以直接从桌面应用菜单打开 **Open Voice Input Linux
设置**。

![未配置 API Key 的 Open Voice Input Linux 设置窗口](assets/settings-window.png)

_截图使用空临时配置；当前 0.x 设置界面为英文，页面可继续下滚到纠错与
服务控制。_

设置窗口不会预填或显示已经保存的 Key。保存 Key、词表或纠错不会联网，
也不会自动重启正在运行的服务。完成设置后，点击
**Enable and start service**；要更换设置，先停止听写并手动停启服务。

个人词表适合人名、项目名、地名和专业术语。显式纠错只适合模型反复把
同一短语识别成同一种错误的情况。两者都会随每次新的语音请求发送给
火山引擎，但不会从剪贴板、输入历史、文档、Rime 词库或既往转写中自动
学习。客户端不会在识别完成后再做一次本地全文替换。

火山官方资料：

- [大模型流式识别能力](https://www.volcengine.com/docs/6561/1354871?lang=zh)
- [请求级热词和 correct_words 示例](https://www.volcengine.com/docs/6561/1395846?lang=zh)
- [托管热词表](https://www.volcengine.com/docs/6561/155739?lang=zh)

## 开始、停止和取消听写

当前独立预览版还没有内置全局快捷键，也没有独立的可见录音指示器；正在
运行的旧兼容应用有悬浮按钮，但不属于新守护进程。使用独立版时，需要在
GNOME/KDE 的键盘设置中自行把快捷键绑定到：

```bash
~/.local/share/murmur-ime/murmur-voice-daemon toggle
```

也可以分别调用：

```bash
~/.local/share/murmur-ime/murmur-voice-daemon start
~/.local/share/murmur-ime/murmur-voice-daemon stop
~/.local/share/murmur-ime/murmur-voice-daemon cancel
~/.local/share/murmur-ime/murmur-voice-daemon status
```

`stop` 会正常结束音频并等待火山引擎的二遍最终结果；`cancel` 会清除
本地 preedit 且不提交，但无法撤回已经上传到远端的音频。

## 隐私和 Key 清除

只有用户主动开始听写后，16 kHz 单声道 PCM 才会发送到火山引擎。
设置页明确显示远端上传、个人账户计费和取消不可撤回已上传音频的边界。

要删除本地 Key，先点击 **Disable and stop**，再完成两步
**Clear saved key** 确认。这个操作只删除经过权限和所有者校验的本地
私有文件，不会替用户吊销火山控制台中的凭据。

## 卸载

```bash
./scripts/uninstall-user.sh
```

当前预览尚未把卸载脚本复制进受管安装目录，因此请保留解压后的预览目录，
并从该目录运行上面的命令。

卸载器会验证安装归属、停止项目服务、恢复原 IBus 引擎，并只移除项目
管理的文件。出于防止误删凭据的考虑，私有 Key、词表和纠错文件会保留；
可在卸载前先通过设置页清除 Key。卸载不会删除 IBus、Rime、雾凇配置或
用户词库。

## 项目状态

当前目标是可审计的公开技术预览，不是已完成的发行版。`main` 已由四项
required checks 保护；公开仓库前仍需完成干净图形 Ubuntu 虚拟机的真实
麦克风／IBus 验收、轮换所有预发布 Key，并准备经过验证的私密联系渠道。
公开转换时还必须立即启用并回读 GitHub private vulnerability reporting。
完整状态见 [open-source readiness 清单](open-source-readiness.md)。
