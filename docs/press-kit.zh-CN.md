# Open Voice Input Linux 宣传资料包

这份资料给维护者、测试者和媒体作者提供一致的产品事实。发布前请按实际
Release 和验证矩阵更新版本号、下载链接与支持范围，不要把路线图写成已经
交付的功能。

## 基本信息

- 名称：Open Voice Input Linux
- 仓库：<https://github.com/SidUParis/openVoiceInput_linux>
- 许可证：GPL-3.0-only
- 当前重点：Ubuntu 24.04 x86_64、IBus、中文为主的中英术语混说
- 当前 ASR：用户自备火山引擎 Key，音频在用户主动听写时发送给火山引擎
- 本地模型：研究与路线图阶段，当前公开版本不能称为完全离线

## 标题与一句话

中文标题建议：

> 我做了一个真正长在 IBus 里的 Linux 语音输入法：不用剪贴板，文字直接
> 出现在光标处

中文一句话：

> Open Voice Input Linux 是一个面向 Linux/IBus 的原生语音输入项目：按下
> 快捷键即可在当前光标实时显示识别文字，并能从严格的局部修改中学习个人
> 术语。

英文标题建议：

> I built an IBus-native voice input method for Linux—live text at the caret,
> no clipboard hacks

英文一句话：

> Open Voice Input Linux streams speech directly into the active IBus input
> context, learns bounded corrections, and lets users opt in to keep their own
> training dataset.

## 30 秒介绍

很多 Linux 语音输入工具最终仍然依赖剪贴板、模拟按键或一个独立转写窗口。
Open Voice Input Linux 让 IBus 引擎拥有当前输入上下文：流式识别草稿直接成为
光标处的 preedit，最终结果只提交一次。项目还提供个人词表、受约束的自适应
纠错、每句麦克风优先级和默认关闭的 WAV/JSON 数据采集。当前 alpha 面向
Ubuntu 24.04 和用户自己的火山引擎账户；本地 ASR 与更广泛发行版支持仍在
路线图中。

## 三个核心区别

1. **IBus 原生**：识别文字在当前光标处更新，不经过剪贴板或 `Ctrl+V`。
2. **从明确修改中学习**：只在同一焦点、有限时间和严格单一替换成立时生成
   个人纠错，不做全局键盘监听。
3. **数据由用户决定**：数据采集默认关闭；启用后，WAV 和未审核的供应商
   final 只写到用户选择的本地或已挂载目录。

## 演示脚本

### 12–20 秒首屏循环

1. 浏览器或编辑器的空输入框；
2. 按 Right Alt；
3. 中文和英文术语作为 preedit 在光标处逐步出现；
4. final 原位提交；
5. 用户把一处术语改正确；
6. 下一次听写显示正确术语；
7. 结束卡：`IBus 原生 · 不用剪贴板 · 数据由你选择`。

### 55 秒介绍视频

- 0–12 秒：光标处实时 preedit；
- 12–24 秒：局部纠错和下一次术语命中；
- 24–34 秒：`DJI > 耳麦 > 电脑内置` 的下一句动态回退；
- 34–45 秒：默认关闭的数据采集以及 WAV/JSON 的字段角色；
- 45–55 秒：当前 Ubuntu/火山边界、GPLv3、GitHub 地址和本地 ASR 路线图。

演示只能使用合成、虚构、非敏感文本。不要显示真实 Key、真实录音、私人
词表、数据集路径、用户名、通知或浏览器账户。

## 可以公开陈述的事实

- partial 使用 IBus preedit，authoritative final 使用一次 IBus commit；
- 主路径不依赖剪贴板或模拟粘贴；
- 密码、PIN、private、失焦和陈旧会话有明确拒绝边界；
- 每次新听写重新评估已保存的麦克风类别顺序，一句话中不切换输入源；
- 可选数据采集默认关闭，供应商 final 标为未审核伪标签；
- Release 使用签名 tag、校验和、SBOM 和精确 commit 验证。

## 暂时不能公开陈述

- “完全离线”“永久免费识别”或“无需第三方账户”；
- “支持所有 Linux 发行版、Wayland 应用或远程桌面”；
- “已经训练出个人 Whisper/Qwen ASR 模型”；
- “所有用户修改都会自动成为 gold transcript”；
- “不会把音频发出电脑”；当前火山识别路径会在主动听写时上传音频；
- “修改反馈已经完整写回训练 JSON”，除非对应版本的端到端验证已经完成；
- 未经真实物理矩阵验证的麦克风无缝切换或绝对不中断保证。

## 建议的社区版本

### V2EX / Linux.do

先讲个人痛点和真实使用流程，再解释 IBus 与剪贴板方案的差别。给出短 GIF、
一个安装入口、明确环境和已知限制，最后邀请不同桌面/应用的兼容性测试。
不要把帖子写成“几天用 AI 做完，求 star”。

### Bilibili

封面只保留“Linux 原生语音输入”与“光标处实时出字”。视频前 10 秒先展示
结果，再讲 IBus、自适应纠错、麦克风和数据收集；安装与安全细节放章节和
简介链接。

### Reddit / Show HN

英文内容重点是 `IBus-native`、`no clipboard hacks` 和可直接体验。Show HN
应等到安装不需要多步手工配置，并最好已经提供无需注册的本地后端。不同
社区需要重新写开头和技术深度，不能同一天复制同一段宣传文案。

## 发布前核对

- [ ] README 首屏动画与当前版本能力一致；
- [ ] `.deb` 在干净 Ubuntu 24.04 上完成安装、升级、卸载验证；
- [ ] Release 下载链接、SHA256、tag 和 source commit 对应；
- [ ] 设置页和演示没有真实 Key、录音、文本或路径；
- [ ] 当前云端费用、隐私和支持矩阵写清；
- [ ] 至少一个 GTK、Qt、Chromium/Electron 和终端应用完成实际验证；
- [ ] 发布帖邀请测试与 issue，不要求或交换 star；
- [ ] 社交预览图已经在 GitHub Settings 手工上传；
- [ ] 对外联系方式能够及时接收安全和普通问题。
