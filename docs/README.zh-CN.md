# Open Voice Input Linux：中文安装与使用指南

**按下快捷键，说话，识别文字默认直接出现在当前光标；远程桌面也可显式选择
只复制终稿，再由用户手动粘贴。**

[返回中文主页](../README.md) · [English](../README.en.md)

![按下快捷键后，语音文字直接显示在当前光标](assets/hero-demo.gif)

_这是使用合成文字制作的交互概念动画，用来说明已经实现的 IBus 光标内
preedit/commit 流程；它不是实际录屏，也没有调用麦克风或网络。当前 alpha
的真实听写需要用户自己所选在线 ASR 服务的账户。_

## 快速安装

当前 `.deb` 面向 **Ubuntu 24.04 x86_64 + IBus**。从本仓库对应的已签名
[Releases 页面](https://github.com/SidUParis/openVoiceInput_linux/releases)
下载 `.deb`，然后在下载目录执行：

```bash
sudo apt install ./open-voice-input-linux_*_amd64.deb
```

安装完成后，从应用菜单打开 **Open Voice Input Linux**，或运行：

```bash
open-voice-input-settings
```

![未配置 API Key 的 Open Voice Input Linux 设置窗口](assets/settings-window.png)

_截图由当前 `main` 分支使用空临时配置渲染，不包含已保存的 Key 或用户数据。_

选择在线识别服务并填入该服务的 API Key，检查麦克风优先级，然后点击
**启用并启动**。保存 Key、词表、纠错、麦克风顺序或采集目录
不会立即联网，也不会打断正在进行的听写；服务会在下一次听写前重新加载。

### 配置听写快捷键

当前 alpha 还不会自动注册系统级快捷键。请在 GNOME/KDE 的键盘快捷键设置
中，选择一个方便且不冲突的组合键并绑定到：

```bash
murmur-voice-daemon toggle
```

第一次调用开始录音，第二次调用停止录音并等待所选服务的最终结果。也可以
分别运行：

```bash
murmur-voice-daemon start
murmur-voice-daemon stop
murmur-voice-daemon cancel
murmur-voice-daemon status
```

设置页可以选择“点按切换”或“按住说话”。按住说话要求按键集成在 key-down
调用 `murmur-voice-daemon press`，在 key-up 调用
`murmur-voice-daemon release`；物理按键由用户决定，项目不硬编码 Right Alt。
普通 GNOME/KDE 快捷键只保证 activation，适合 `toggle`。通用 Wayland
快捷键没有可靠的全局 key-up，因此只有能够分别提供两个边沿事件的桌面、
键盘或辅助工具才能使用按住说话。本项目不会为此读取全部 `/dev/input`。

`stop` 会结束音频并等待 authoritative final；`cancel` 会清除本地 preedit、
阻止文字提交，但无法撤回已经发送给供应商的音频。

## 三个核心特点

### 1. 真正进入当前输入框

支持流式 partial 的服务会把累计识别草稿通过 IBus preedit 显示在光标处；
authoritative final 原位提交且只提交一次。OpenAI 批量后端在停录后返回 final，
不伪装成实时 partial。正常路径不读取剪贴板，不发送
`Ctrl+V`，也不模拟逐字键盘输入。

最终文本方式默认是“忠实转写”。用户也可选择“清爽表达”：partial 仍显示原始
识别，只有 authoritative final 才运行本机、有界、确定性的删除规则。它不调用
LLM、不为清理增加网络请求，也不替换术语、数字或大小写；任何失败或不安全结果
都回退为原始 final。若清理改变文本，本条自动学习观察会跳过；复核仍以原始
provider final 为唯一纠错来源，delivery 仅只读展示。

Remmina 等 RDP 画布不能接收本机 IBus commit。**远程桌面**页因此提供一项
默认关闭的“同步剪贴板”：只复制 authoritative final，不复制 partial、不自动
粘贴、不模拟按键。成功状态只证明上一条写入当时成功，其他应用可能随后覆盖；
用户确认远端光标后再手动 `Ctrl+V`。两端
会话和剪贴板历史都可能读取内容，禁止用于密码、Key、验证码等秘密；远端没有
可信 surrounding text，所以本条不会自动学习。详见
[远程桌面说明](remote-desktop.md)。

### 2. 从修改中学习，但不把润色当规则

最终文本提交后，输入法可以在同一输入框中保留最长 5 秒的有界观察窗口。
单一高置信术语或拼写替换可以启用；多处独立替换会拆成待确认候选，冲突项
会隔离。设置页显示最近结果与原因。无法暴露可信 IBus surrounding text 的
Chrome/Electron 应用可运行 `open-voice-input-settings --review-last`；设置页从独立
的主机私有 socket 载入内存中最近一条识别原文，原文只读，用户只能明确编辑
“实际说法（逐字）”后提交。结果限存十分钟、新结果覆盖，退出守护进程即清空。

复核提交仍由守护进程完成：它核对 utterance ID 仍对应当前未过期结果，更新
自适应账本，并在数据留存启用时把有界纠错排入同一 utterance 的 append-only
feedback sidecar。成功后结果单次消费；重复、过期或已被新结果覆盖的提交会拒绝。
“已进入写入队列”不等于已经最终落盘，界面会单独显示这一状态。

它不会监听全局键盘、AT-SPI、剪贴板、Rime 输入历史或其他应用内容；失去
焦点、超时和整句润色不会被静默提升为全局规则，完整 surrounding text 也不会
写入纠错账本。去口头词、改写或润色不能作为 `spoken_verbatim` 提交。

### 3. 可选保留属于用户的数据

本地数据采集默认关闭。用户明确启用并选择已有的本地或操作系统已挂载目录
后，一次 authoritative final 成功交付到该条冻结的目标（当前光标或显式
剪贴板）后，可以保存为精确的 16 kHz 单声道 WAV
和一个版本化 JSON。

当前 alpha 保存的是**未经人工审核的供应商结果**。不可变 `record.json` 不会被
后续修改覆盖；捕获成功的短纠错会另存为数据集根目录下 append-only 的
`feedback/<utterance_id>/<event_id>.json`。`spoken_verbatim` 与
`preferred_output` 仍保持未填写，直到以后实现听音审核流程。因此这些记录是
珍贵的候选数据，但不是已经确认的 gold label。

每条不可变听写目录仍严格保持 `audio.wav` + `record.json` 两文件契约；数据集
根目录另有不含转写正文的 `usage/<utterance_id>.json` 索引。首页在后台只读
这个索引来显示今日与累计统计，不读取或展示转写标签。采集关闭时不扫描旧
目录；远程挂载断线时显示“存储不可用”，不会把未知状态写成 0。

## 准备在线识别服务

默认后端是火山引擎 BigModel ASR 2.0，使用
`bigmodel_async`、二遍识别、DDC、ITN、标点和智能分句。用户需要在自己
所选服务的账户中开通对应语音能力，并承担该账户的费用和配额；地域处理、
服务端留存与账户政策也遵循所选服务的条款和配置。

alpha.5 还接入了 Qwen 实时 ASR 与 OpenAI 停录后批量转写，两者尚未用真实
用户 Key 验收；MiniMax 仍为计划项。可在设置页选择已接入服务，也可参考
[后端说明](provider-backends.md)使用交互式 CLI 配置。

项目不内置共享 Key，当前也**没有本地或完全离线 ASR**。不要把 API Key
放进 issue、截图、日志、命令参数或 Git；一旦泄露，应立即在所选服务的
账户控制台吊销并轮换。

当前语音路径负责忠实转写，不是生成式写作。供应商侧 DDC、标点、分句和
ITN 可以整理转写文本，但应用不会把一句简短指令扩写成邮件，也不会主动
补充用户没有说出的内容。

火山官方资料：

- [大模型流式识别能力](https://www.volcengine.com/docs/6561/1354871?lang=zh)
- [请求级热词和 correct_words 示例](https://www.volcengine.com/docs/6561/1395846?lang=zh)
- [托管热词表](https://www.volcengine.com/docs/6561/155739?lang=zh)

## 当前支持范围

| 项目 | 当前 alpha 状态 |
| --- | --- |
| 操作系统 | Ubuntu 24.04 x86_64 是打包与 CI 目标；干净机器上的真实麦克风、供应商和多应用验收仍在扩大 |
| 桌面输入 | 面向实现标准 IBus preedit 的应用；X11 与 Wayland 应用矩阵仍在完善 |
| 中文键盘输入 | 听写时临时切换到 `murmur-voice`，结束后恢复原来的精确 IBus 引擎；librime／雾凇与语音永久合并尚未完成 |
| ASR | 火山引擎为默认且已实机使用；Qwen 实时与 OpenAI 批量后端已接入但尚未用真实 Key 验收；MiniMax 仍为计划项 |
| 本地 ASR | 尚未实现 |
| 快捷键与指示器 | 目前需要用户自己绑定快捷键；旧兼容控制器不属于本仓库守护进程包 |
| 隐私输入框 | 密码、PIN、private、fake 或不支持 preedit 的上下文拒绝开始语音 |
| 远程桌面 | IBus preedit 不能穿过 RDP 画布；可显式复制终稿并由用户手动粘贴，无 remote partial、自动粘贴或 surrounding-text 学习；详见[远程桌面说明](remote-desktop.md) |

当前是社区测试用的公开 alpha，不是稳定发行版。请先查看
[CHANGELOG](../CHANGELOG.md)、[ROADMAP](../ROADMAP.md)和
[真实兼容性验证矩阵](compatibility-matrix.md)。

## 临时 IBus 切换边界

当前实现会在一次听写期间执行：

1. 记住当前 IBus 引擎；
2. 临时切换到 `murmur-voice`；
3. 在当前焦点中显示 partial 并提交一个 final；
4. 最多观察 5 秒，明确单项可启用，多处修改保留为候选；
5. 恢复之前的精确 IBus 引擎。

在听写和这段观察窗口中，原来的 Rime/其他 IBus 引擎暂不可用；普通直接
键入仍可能由应用处理。再次触发 `toggle`、取消、失败、焦点变化或超时会
提前结束观察。真正连续的键盘与语音输入仍需要计划中的 librime-capable
合并引擎。

## 词表与自适应纠错

个人词表适合人名、项目名、服务器名、地名和专业术语。手动纠错适合模型
反复把同一个短语识别成同一种错误的情况。`vocabulary.json` 与
`corrections.json` 都是明确配置：只有用户点击保存时才会建立；文件不存在
等价于没有明确条目，不代表自动学习失效。自动学习只写独立的私有
`adaptive-corrections.json`，不会把 API 输出、整句文本或所有标准写法自动
复制进个人词表。

自适应观察只接受原提交区间内的有界 replacement，每侧最多 64 个 Unicode
字符；一处高置信替换可直接启用，多处替换会拆成等待确认的候选。

例如把 `bench mark` 改为 `benchmark` 时，提取器只会形成这个具体短语的
候选，而不是草率学习更宽的替换。个人词表和系统可选的英／法 Hunspell
词典只会在唯一匹配时进一步规范化；有歧义时保留用户实际修改。

学习结果保存在私有 `adaptive-corrections.json`，只包含有界纠错对、状态和
support 计数，不保存 surrounding 全文、独立转写记录、音频或整篇文档。
手动规则优先；冲突、重叠和循环规则会被抑制，传给所选服务的手动与自适应
provider view 合计仍不超过 50 对。这是确定性的纠错记忆，不是本地神经
网络、自回归模型或已经完成的模型训练。

设置页分别显示明确词汇数、明确纠错数、自适应状态和下一次请求实际编译的
纠错上下文数。确认候选后，程序会重新读取刚写入的账本并再次编译；只有该
规则确实进入 provider view 才会提示“下一次生效”。明确规则覆盖、冲突、
循环、级联、重叠或容量限制都会显示具体原因。

当前 alpha 会在非空 authoritative final 后默认开始这段事件驱动观察，设置
页暂时没有关闭开关。再次触发 toggle、焦点变化、取消或超时都会结束观察；
不支持可信 surrounding text 的应用会立即恢复原来的 IBus 引擎，不再空等五秒；
用户仍可通过上述“纠正上一条”入口明确生成纠错对。

## 动态麦克风选择

每次新听写都会重新读取用户保存的完整优先顺序，并枚举当前输入设备。用户
可以在设置窗口按自己的设备与工作方式自由重排；首选设备不可用时，守护
进程会继续尝试后续候选，而不是把维护者的设备偏好强加给所有人。

同一类输入中会依次尝试用户明确保存的设备、该类当前系统默认或唯一候选；
仍有歧义就跳到下一类，而不是猜测。设备断开后可以自动降级，重新连接后
下一次听写会再次按用户顺序选择。

大疆 USB 接收器在发射器关机后仍可能注册输入 source，因此守护进程会做
有界 link-state 判断。能证明离线的接收器会被排除；未知状态不会被提升到
已知可用设备之前。选择仅作用于新建的应用录音流，不改变播放 sink，也不
请求系统级默认 source 变更。一次听写开始后不会中途换麦，下一次听写才
重新选择。

单次听写最长 600 秒，停止后最多等待最终结果 20 秒。守护进程不会主动解除
静音或调整麦克风音量；无法唯一、安全地选择时会返回
`microphone-unavailable`。

## 可选数据采集与远程目录

若要保留候选数据，在设置页勾选 **在所选目录保留 WAV、原始识别与实际交付
结果**，选择一个已经存在的绝对目录，再点击 **保存数据留存设置**。
目录可以
位于本地磁盘，也可以是操作系统已经挂载的 SSHFS 等 POSIX 文件系统；不能
直接填写 SSH 地址或 Google Drive URL。

软件本身不会登录或挂载远程主机，也不会直接上传 Google Drive：

- 远程主机可以先通过 SSHFS 挂载，再把挂载目录选为采集位置；
- Google Drive 应在一条记录完整发布后，再通过 `rclone copy` 异步备份；
- 不要把 `rclone mount` 当作实时采集目录。

数据直接写向所选目录，没有备用本地 spool。挂载点卡住或断开时，正常听写
仍会继续，但一条尚未发布的 staged record 可能丢失；已经发布的记录保留。
服务退出时后台 writer 最多等待 10 秒，并受 systemd 总停止预算约束。关闭
采集会阻止尚未发布的排队记录继续发布，不会删除已经发布的数据。

软件不做应用层静态加密；实际访问权限和静态保护由用户所选文件系统决定。
新 `record.json` 使用 schema v4：原始 `provider_final`、机器生成且未经复核的
`delivery`、仍为空的两个人工标签彼此独立，并由 `delivery.target` 标明 `caret`
或 `clipboard`；旧 v1/v2/v3 不会改写。usage v2
明确按实际交付文本统计字符，同时仍兼容旧 v1 摘要。
完整 SSHFS、断线恢复、权限验证和 rclone 操作见
[远程数据集存储指南](remote-dataset-storage.md)，标签边界见
[个人 ASR 数据计划](personal-asr-data-plan.md)。

## 隐私与安全

只有用户主动开始听写后，16 kHz 单声道 PCM 才会发送到用户选择的在线识别
服务。取消不能撤回已经上传的音频。本地 WAV/JSON 是另一项独立 opt-in，不会取消云端上传，
也不会由本软件再次上传到其他服务。

设置页不会预填已经保存的 Key。要删除本地 Key，先点击
**停用并停止（取消当前听写）**，再完成两步 **清除已保存的 Key…** 确认；这只删除本地
私有文件，不会替用户吊销所选服务账户中的凭据。

完整边界请阅读[隐私说明](privacy.md)、[安全说明](security.md)和
[威胁模型](threat-model.md)。

## 高级：验证型离线预览包

`.deb` 是普通用户的主要试用路径。需要核对精确 commit、完整 Python
wheelhouse、SHA-256 清单和 CycloneDX SBOM 时，请使用 CI 构建的预览
archive，并按[离线预览包说明](offline-preview.md)完成两级校验。

它仍需要 Ubuntu 已有的 IBus、GI、GTK4、PortAudio、`libusb`、
`python3-venv` 和 `util-linux` 系统组件；离线 archive 不会复制这些系统库。
从联网源码 checkout 安装则必须显式接受开发依赖解析：

```bash
./scripts/install-user.sh --allow-network
```

维护者可以在干净的 Ubuntu 24.04 x86_64 checkout 中，从一个精确 commit 和
准备好的离线 wheelhouse 构建 `.deb`；当前执行的 builder 必须与该 revision
中的字节一致：

```bash
./scripts/build-deb.sh \
  --ref EXACT_COMMIT_SHA \
  --wheelhouse /absolute/path/to/wheelhouse \
  --output-dir dist
sudo apt install ./dist/open-voice-input-linux_*_amd64.deb
```

包安装的运行时位于 `/usr/lib/open-voice-input-linux`，公开命令位于
`/usr/bin`，systemd user unit 位于 `/usr/lib/systemd/user`。安装本身不会
擅自启用录音；用户仍需要在设置页明确启用服务。

## 不用 Key 的确定性演示

开发者可以在不使用麦克风、API Key，也不修改当前桌面 IBus 引擎的情况下，
验证真实的光标内 preedit/commit 路径：

```bash
python3 -I scripts/run_isolated_preedit_smoke.py
```

脚本会在私有 Xvfb、D-Bus 和 IBus 中发送固定的合成中文 partial/final，并
输出截图目录。它不能证明真实麦克风质量、供应商准确率、systemd 用户会话或
所有应用兼容性。

## 卸载与保留数据

升级时让 `apt install` 指向新 `.deb`。卸载 Debian 包使用：

```bash
sudo apt remove open-voice-input-linux
```

`remove`（以及 `purge`）不会删除用户私有 Key、词表、纠错、终稿交付位置、
采集选择或外部 dataset。源码或验证型 preview 安装可以从原解压目录执行：

```bash
./scripts/uninstall-user.sh
```

卸载器会验证安装归属、停止项目服务、恢复之前的 IBus 引擎，并只移除项目
管理的文件。为避免误删凭据或珍贵语音数据，私有 Key、词表、纠错、麦克风
优先级、终稿交付位置、采集配置和用户所选目录内的数据不会被自动删除。

## 进一步阅读

- [中文项目主页](../README.md)
- [English README](../README.en.md)
- [架构](architecture.md)
- [原型实现与运行边界](python-preedit-prototype.md)
- [识别准确率与纠错设计](recognition-accuracy.md)
- [用户服务、升级和故障排查](user-service.md)
- [发布流程](release-process.md)
- [宣传定位与演示分镜](launch-positioning.md)
- [产品与发布设计](product-launch-plan.zh-CN.md)
- [宣传资料包](press-kit.zh-CN.md)
- [真实兼容性验证矩阵](compatibility-matrix.md)

Open Voice Input Linux 的新原创代码采用 GPL-3.0-only。项目是独立社区作品，
不隶属于 Rime、火山引擎、字节跳动或豆包。
