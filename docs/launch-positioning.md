# Open Voice Input Linux：公开定位与演示规范

这份文档约束项目主页、发布说明、演示动画和后续宣传稿的共同叙事。它不是
产品路线图；实际能力与限制仍以代码、测试、`CHANGELOG.md` 和 release 为准。

## 核心定位

中文主句：

> Linux 原生自适应语音输入：按下快捷键，说话，文字直接出现在当前光标。

英文副句：

> IBus-native voice typing for Linux. Live text at the caret, without
> clipboard paste or simulated keystrokes.

当前首先服务 Ubuntu／IBus 上的中文用户，特别是经常混用英文专有词的开发者、
研究人员和重度文字工作者。英文文案用于让国际 Linux 与开源社区理解项目，
但不能在没有真实测试前宣称已经解决任意语言、任意发行版或任意应用。

## 信息架构

陌生访问者应当按下面的顺序理解项目：

1. **结果**：语音草稿与最终文本就在当前光标；
2. **证明**：12 秒内看完、与真实 IBus 行为语义一致的交互概念动画；
3. **行动**：下载 `.deb`，打开设置，绑定快捷键；
4. **差异**：原生光标、自适应纠错、用户控制的数据；
5. **代价与边界**：当前使用火山在线 ASR、自备 Key、本地 ASR 尚未完成；
6. **兼容性**：Ubuntu 24.04 x86_64／IBus 是当前打包目标；
7. **信任证明**：安全模型、checksum、签名、SBOM、可复现 archive；
8. **内部设计**：D-Bus、0.x ABI、librime 合并计划等深层文档。

主页不应再次把工程内部状态放在结果之前。签名、SBOM 与威胁模型继续保留，
但作为选择项目之后的信任层，而不是第一次阅读的门槛。

## 采用的高星项目信息层级

这里参考的是呈现顺序，而不是复制文案或视觉：

- [Handy](https://github.com/cjpais/Handy) 把一句结果、演示和下载入口放在架构
  之前；本项目采用相同的低理解成本，但不使用它的“完全离线”承诺；
- [VoiceInk](https://github.com/Beingpax/VoiceInk) 先展示真实使用画面，再解释
  个人词典和高级工作流；本项目也把自适应纠错放在光标输入证明之后；
- [OpenWhispr](https://github.com/OpenWhispr/openwhispr) 用清晰的平台包和
  local/cloud 边界降低试用阻力；本项目当前只列出真正交付的 Ubuntu `.deb`
  和火山 BYOK，不伪装成跨平台或本地 ASR；
- [Voxtype](https://github.com/peteonrails/voxtype) 用 Linux-first 的可验证结果
  建立类别，而不是把 Linux 当作附带平台；本项目进一步聚焦 IBus、中文与
  光标内 preedit。

共同原则是“先看结果、再能安装、随后理解差异、最后审计内部实现”。项目的
独特内容仍然是 IBus-native、自适应纠错与用户控制的数据，而不是竞品首页的
措辞或版式。

## Hero 动画分镜

目标资产：`docs/assets/hero-demo.gif`。

推荐时长 10–12 秒、16:9、循环播放，画面只展示一个“魔法时刻”：

| 时间 | 画面 | 目的 |
| --- | --- | --- |
| 0–1.5 秒 | 一个普通 Linux 输入框，光标清晰闪烁；右下角出现 `Right Alt` 提示 | 建立起点 |
| 1.5–6 秒 | 合成 partial 在同一光标处逐步增长，使用 preedit 下划线或选区视觉 | 证明不是弹窗转写 |
| 6–8 秒 | authoritative final 原位提交，preedit 装饰消失 | 证明只提交一次 |
| 8–10 秒 | 小标签依次出现：`IBus native`、`No clipboard` | 收束差异 |
| 10–12 秒 | 回到干净输入框并自然循环 | 避免跳帧 |

动画必须满足：

- 使用合成、非敏感文字；不录入真实 API Key、录音、路径或个人词表；
- 当前生成资产是交互概念动画，caption 必须明确它不是实际录屏、没有录音或
  网络调用；如果以后换成确定性 smoke test，也必须说明它不代表真实 ASR；
- 不在这个短动画中塞入设置页、麦克风切换、训练 JSON 或架构图；
- partial 与 final 必须符合真实累计 preedit／单次 commit 行为，不制作代码
  当前做不到的效果；
- 保持文字足够大，GitHub 约 720 px 宽显示时仍可读；
- 优先保证首帧和结尾在深色、浅色 GitHub 主题下都清楚；
- GIF 应控制体积；如以后加入视频，可保留静态 poster 和无声字幕版本。

同批资产还包括：

- `docs/assets/hero-demo-poster.png`：960×540 静态 poster，可用于不支持动画的
  页面或文章封面；
- `docs/assets/social-preview.png`：1200×600 仓库社交分享图。该文件不会仅因
  进入 Git 就自动成为 GitHub Open Graph 图片，维护者仍需在仓库设置中明确
  上传，并在公开前预览深色／浅色分享卡。
- `docs/assets/hero-demo.md`：生成方式、帧数以及“无录音、无网络”的事实说明。

## 后续演示，不混入 Hero

完成真实端到端验证后，可以分别录制三个独立短片：

1. **专有词纠错**：供应商误识别 → 用户在 5 秒内替换一个 span → 下一次
   听写应用纠错。只展示 correction ledger 已经真实工作的部分；在用户编辑
   尚未回填训练 JSON 时，不把它描述成“自动生成金标”。
2. **麦克风容错**：显示用户定义顺序；首选设备不可用后，下一次听写降级到
   下一类；同时证明播放输出不变。不要声称同一句录音可以无缝热切换。
3. **个人数据候选集**：明确勾选 opt-in，展示 WAV 和 `provider_final`；画面
   必须标注 `teacher-unreviewed`，并说明审核字段当前未填写。

## 首次安装路径

普通用户的主要路径应始终保持短小：

```text
Release 下载 .deb
    → apt 安装本地文件
    → 打开 Settings
    → 保存自己的火山 Key
    → 绑定 toggle 快捷键
    → 第一次听写
```

SHA-256、SBOM、完整 wheelhouse 和无网络验证型 archive 是高级可信路径。
它们不应从仓库删除，但不再要求第一次体验的用户先理解两级清单。

## 三项公开差异

### IBus-native

允许表述：

- partial 直接显示在当前光标；
- authoritative final 原位提交一次；
- 正常路径不读剪贴板、不模拟 `Ctrl+V`。

不可提前表述：

- 已经完成永久 Rime + voice 合并；
- 所有 Linux 应用、Wayland/X11 和发行版都已经通过；
- 远程 RDP 画布可以透明接收本地 IBus preedit。

### Adaptive corrections

允许表述：

- 同一可信 IBus 输入框内、最长 5 秒、一处严格 replacement 可以成为私有
  纠错；
- 不做全局键盘监听，不保存 surrounding 全文；
- 手动规则优先，冲突和循环规则受抑制。

不可提前表述：

- 模型已经在本地持续训练；
- 任意润色、插入、删除或多处编辑都会学习；
- 用户修改已经自动更新 `preferred_output` 或 `spoken_verbatim`。

### User-controlled data

允许表述：

- 默认关闭；
- 可把 WAV 与未经审核的 provider final 写到用户选择的本地或已挂载目录；
- Orange 可通过系统 SSHFS，Drive 可用异步 rclone 备份完整记录。

不可提前表述：

- 数据默认全部是高质量 gold label；
- 软件直接登录 Orange 或 Google Drive；
- 远程断线时存在本地 fallback spool；
- 已经能用这些数据微调或蒸馏出本地模型。

## Release 页面模板

每个面向用户的 release 正文优先回答：

1. 这个版本解决了什么真实问题；
2. 一个短 GIF 或截图；
3. `.deb` 下载与安装命令；
4. 已验证环境；
5. 最重要的三项限制；
6. 升级／卸载；
7. 完整 checksum、SBOM、签名和审计链接。

一次 release 只讲一个主故事。动态麦克风、数据采集、安全 hardening 可以是
重要更新，但不应同时挤掉“光标处原生语音输入”这个稳定的项目主语。

## 后续宣传稿的事实检查

发布中文或英文宣传稿前逐项确认：

- 文中安装命令与当前 release 的真实 asset 名一致；
- 演示使用的能力已在相同 commit 和公开包中存在；
- 明确当前音频会发送到用户选择的火山账户；
- 不使用“完全离线”“永久免费 ASR”“所有 Linux”“会自动训练自己”等未实现
  承诺；
- 区分供应商 pseudo-label、用户期望文本、局部 correction 与人工确认的
  spoken verbatim；
- 给出具体 Ubuntu、桌面会话、应用和麦克风测试环境；
- 邀请测试和提交兼容性报告，不购买 star，也不组织交换 star。
