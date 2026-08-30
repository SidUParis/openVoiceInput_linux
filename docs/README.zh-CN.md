# Open Voice Input Linux：中文快速上手

Open Voice Input Linux 是一个面向 Linux/IBus 的轻量语音输入技术预览。
录音时，火山引擎返回的累计识别草稿会直接显示在当前应用的光标位置；
二遍识别完成后，最终文本只提交一次。主路径不弹出转写黑框，也不使用
剪贴板或模拟 `Ctrl+V`。

## 当前能力与边界

- 支持 Ubuntu 24.04 x86_64、IBus，以及实现了标准 IBus preedit 的应用；
- 使用火山引擎 `bigmodel_async`、二遍识别、DDC、ITN、标点和智能分句；
- 可配置个人词表，以及明确的“误识别写法 → 正确写法”纠错对；
- authoritative final 提交后最多保留 5 秒纠错观察：在支持 IBus
  surrounding text 的同一输入框中，仅有原提交区间内的一处严格
  replacement 可能被记为下次听写的自适应纠错；
- 密码、PIN、隐私字段、失去焦点、取消和过期会话不会提交文字；
- 普通键盘输入与网络、麦克风进程隔离，语音服务故障不应阻塞键盘；
- 单次听写最长 600 秒，停止后最多等待最终结果 20 秒；
- 每次听写前重新选择输入：能证明大疆 Mic Mini 2 发射器在线时，只为本次
  录音流选择大疆；能证明离线时避开仍注册但静音的接收器；无法证明时保持
  系统原有行为；
- 可选的本地数据采集默认关闭。用户选择已有的本地或挂载目录后，只有已被
  当前 IBus 上下文接受的 authoritative final 才会与对应 WAV 一起发布；
- 当前版本是过渡实现：听写及随后最多 5 秒的观察期内临时选择
  `murmur-voice`，之后恢复原来的 IBus 引擎。观察期内普通直接键入
  仍可由应用处理，但原 Rime／其他 IBus 引擎暂不可用；再次按听写
  toggle 可提前结束观察。真正把 librime／雾凇键盘输入与语音合成
  到同一 IBus 引擎仍在开发中。

## 准备火山引擎

用户需要在自己的火山引擎项目中开通对应的大模型流式语音识别服务，
费用、配额和数据处理规则均属于该用户自己的账户。程序不内置共享 Key。

设置完成后，本软件必需的远端配置只有该用户自己的 API Key。不要把 Key
放进 issue、截图、日志、命令参数或 Git；一旦泄露，应立即在火山控制台
吊销并轮换。

## 安装 Ubuntu 系统依赖

离线预览包包含完整 Python wheelhouse，但不复制 Ubuntu 系统组件。软件
自身采用无 root 的当前用户安装；如果系统尚未安装 IBus、GI、GTK4、
PortAudio、`libusb`、`python3-venv` 或提供 `flock` 的 `util-linux`，通过 APT 补齐
这些系统组件仍需要管理员权限：

```bash
sudo apt-get update
sudo apt-get install --yes \
  ibus gir1.2-ibus-1.0 gir1.2-gtk-4.0 \
  libportaudio2 libusb-1.0-0 pulseaudio-utils python3-gi python3-venv util-linux
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

## 设置 Key、词表、纠错和可选本地采集

打开原生 GTK4 设置窗口：

```bash
~/.local/share/murmur-ime/open-voice-input-settings
```

完成受管安装后，也可以直接从桌面应用菜单打开 **Open Voice Input Linux
设置**。

![未配置 API Key 的 Open Voice Input Linux 设置窗口](assets/settings-window.png)

_截图使用空临时配置；当前 0.x 设置界面为英文，页面可继续下滚到纠错、
麦克风选择、可选本地采集与服务控制。_

设置窗口不会预填或显示已经保存的 Key。保存 Key、词表、纠错或本地采集
选项不会联网，也不会打断正在进行的听写；空闲后的下一次听写会重新加载，
无需重启服务。首次完成设置后，点击 **Enable and start service**。

若要保留数据，勾选 **Keep local WAV + unreviewed provider final**，选择一个
已有文件夹，再点击 **Save local collection setting**。保存立即作用于下一次
听写，不需要重启服务。不要勾选时，缺失或不可用的采集目录不会阻止普通
听写。

## 不用 Key 的轻量级实时光标验证

无需下载虚拟机、无需麦克风、无需 Key，也不改变当前桌面的 IBus 引擎：

```bash
python3 -I scripts/run_isolated_preedit_smoke.py
```

脚本会建立临时 HOME、私有 Xvfb、D-Bus 和 IBus，发送固定的合成中文
partial/final。partial 阶段文字必须出现在光标处而已提交值保持为空；final
随后只提交一次。脚本会打印 mode-0700 结果目录，其中保留 partial/final
截图与私有日志。它能验证真实 IBus/GTK preedit 路径，但不能替代干净系统、
真实 systemd 用户会话、麦克风、火山账户或多应用兼容性验收。

个人词表适合人名、项目名、地名和专业术语。显式纠错只适合模型反复把
同一短语识别成同一种错误的情况。新的自适应纠错内存只观察 authoritative
final 后的同一输入框：如果用户在 5 秒内只替换原提交区间内的一处文字，
则可保存一个每侧最长 64 个 Unicode 字符的“错误 → 正确”对。纯插入、纯删除、
多处修改、整句润色、失焦、超时或不支持 IBus surrounding text 都不学习。
提取器按 token 处理中英文边界：把 `奔驰 mark` 中的 `奔驰` 改为 `bench`
可借助未改动的 `mark` 得到更具体的短语规则，绝不会学习过宽的
`奔驰 → bench`。个人词表和系统可选的英/法 Hunspell 词典可把唯一匹配的
`bench mark` 规范为 `benchmark`；有歧义时保留用户实际修改。这是事件驱动的
确定性逻辑，不会常驻运行本地神经网络模型。

学习结果保存在私有 `adaptive-corrections.json`，只包含有界的纠错对、状态和
support 计数，不保存 surrounding 全文、独立转写记录、音频或文档上下文。
它不读剪贴板、AT-SPI、
全局键盘事件、输入历史或 Rime 词库，也不在已提交文本上再做本地全文
替换。手动纠错优先；冲突、重叠和循环的学习规则会被抑制，每次发给
火山引擎的手动+自适应 provider view 仍最多 50 对。这是纠错内存，不是
本地模型训练或“自回归模型”。

本 alpha 在非空 authoritative final 后默认开启该 5 秒观察；它由 IBus
surrounding-text 事件驱动，不轮询也不监听全局键盘。目前设置窗口尚无关闭
开关。不支持或不能可信锚定 surrounding text 的应用只会跳过学习。

本地数据采集已经实现，但默认关闭。勾选后必须选择一个已存在的绝对路径
（可以是本地目录或已经挂载的文件系统）；软件会在其中初始化
`openvoiceinput-dataset-v1`。只有 authoritative provider final 已被当前
IBus 上下文接受时，才会在后台发布该次听写的精确 16 kHz、单声道、signed
16-bit WAV 和 `record.json`。`provider_final` 明确标记为未审核伪标签；
`spoken_verbatim`（实际说了什么）与 `preferred_output`（希望最终输入什么）
保持 `null`，不能把当前记录宣传成 gold label 或可直接蒸馏的数据。

采集使用有界内存并在后台写盘；写盘失败不会阻塞正常听写。这个功能不会
把本地数据上传云端或 Orange，不会训练／微调模型，也不做应用层静态加密；
实际可见性和静态保护由用户所选文件系统决定。关闭后，尚未发布的排队记录
不能再发布，已经发布的数据会保留。训练和 Orange 传输仍只是后续计划。
数据会直接写向所选目录，没有备用本地 spool；服务退出时后台 writer 只在
systemd 的 30 秒总停止预算内等待最多 10 秒。若挂载点卡住或消失，隐藏的
staging 可能被保留或清理，该条尚未发布的记录可能丢失，已发布记录不受影响。
详见[个人 ASR 数据计划](personal-asr-data-plan.md)。

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

IBus preedit 只属于当前桌面会话，不能作为普通按键穿过 Remmina/RDP
画布。远端麦克风重定向、在远端会话内运行本项目，以及不提供实时光标
草稿的显式剪贴板备用方案，见[远程桌面说明](remote-desktop.md)。

每次 `start` 或空闲状态下的 `toggle` 都会重新检查输入设备。蓝牙设备断开
后，如果系统默认 source 已失效或变成以 `.monitor` 结尾的扬声器监听源，
守护进程会重新枚举真实输入，并把选定的物理 source 只绑定到自己的
PortAudio `pulse` 录音流，不会直接调用 `set-default-source`。若声卡只剩
output-only profile，则只在存在唯一、安全且保留当前输出的 input+output
profile 时自动恢复；激活 profile 仍可能触发系统音频策略重新计算全局默认
source。它不会解除静音或改变音量。无法唯一、安全地选择时会返回
`microphone-unavailable`；请在系统声音设置中选择或解除静音后直接再次
听写，不需要重启服务。

大疆 Mic Mini 2 的 USB 接收器在发射器关机后仍会注册一个输入 source，
所以仅看设备列表可能选到静音接收器。守护进程在每次听写前做一次有界的
link-state 检查：能证明发射器在线时选择大疆；能证明离线时优先使用当前
非大疆默认输入，否则只接受唯一、明确的内置／非大疆回退；link state
未知（例如接收器忙、不可访问或缺少 `libusb`）时保留原有系统默认选择逻辑。
这只决定新建的应用录音流，不改变播放 sink，也不请求修改系统默认 source。
一次听写开始后不会在中途实时换麦；收起或重新打开发射器后，下一次听写才
会重新判断。

## 隐私和 Key 清除

只有用户主动开始听写后，16 kHz 单声道 PCM 才会发送到火山引擎。
设置页明确显示远端上传、个人账户计费和取消不可撤回已上传音频的边界。
可选本地采集是另一项明确 opt-in：它不会取消火山上传，也不会把生成的数据
再次上传到其他服务。

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
管理的文件。出于防止误删凭据或珍贵数据的考虑，私有 Key、词表、纠错和
`data-collection.json` 会保留；用户所选目录中的
`openvoiceinput-dataset-v1` 也不会被删除。可在卸载前先通过设置页清除 Key。
卸载不会删除 IBus、Rime、雾凇配置或用户词库。

## 项目状态

当前是面向社区测试与反馈的公开 early technical preview，不是稳定或受支持
的发行版。`main` 已由四项 required checks 保护；GitHub private vulnerability
reporting、Secret Scanning 和 Push Protection 已启用。干净图形 Ubuntu 环境的
真实麦克风／provider／多应用验收仍未完成，会作为 alpha 的已知验证缺口明确
披露；所有开发 Key 的轮换和签名 tag 是发布前门槛，发布后必须立即验证
immutable 状态。
完整状态见 [open-source readiness 清单](open-source-readiness.md)。
