# 远程桌面输入

本机 IBus 的一次文本提交属于本机 Linux 桌面会话里的应用与输入上下文。
Remmina 的 RDP 画布接收的是远端桌面事件，不是一个可由本机 IBus 获取的文本框，
因此默认的光标模式不能把 preedit 或 final 直接提交到远端光标。

## 推荐：显式复制终稿，再手动粘贴

如果麦克风、Open Voice Input Linux 和在线 ASR 都运行在本机，而目标输入框位于
Remmina 远端桌面：

1. 在 Remmina 连接配置中保持剪贴板同步开启；
2. 打开 **Open Voice Input Linux → 远程桌面**；
3. 选择 **同步剪贴板**，点击 **保存终稿交付位置**；
4. 正常开始和结束听写；
5. 看到“上一条终稿已复制；剪贴板可能已被其他应用覆盖”后，先确认远端目标
   输入框，再由你按 `Ctrl+V`。这个状态只证明写入当时成功，不承诺当前剪贴板
   仍未被其他应用改写。

缺少 `output-target.json` 时仍然是“当前光标”，不会静默启用剪贴板。剪贴板模式
只复制已经确认的 authoritative final：

- 不复制实时 partial；
- 不自动发送 `Ctrl+V`，也不模拟逐字按键；
- 不尝试猜测远端窗口或焦点；
- 复制失败时不回退到远端自动输入；
- 没有可信的远端 surrounding text，因此这条听写不会启动自动纠错学习；
- 显式的“复核最近识别”仍以原始 provider final 为准。

本机需要安装一个与当前图形会话匹配的剪贴板命令：

```bash
# X11
sudo apt install xclip

# 原生 Wayland
sudo apt install wl-clipboard
```

正式 `.deb` 声明 `xclip | wl-clipboard`。源码安装不会因为这个可选远程模式缺少
工具而阻止默认光标模式；只有用户明确选择剪贴板并开始下一条听写时，运行时
preflight 才会检查工具。不可用时会在开麦和联系识别服务之前失败关闭，并显示
`clipboard-unavailable`。

启用后，空闲状态会持续显示 `clipboard-armed`，提醒下一条终稿走剪贴板；它不
表示剪贴板当前含有任何听写内容。一次成功写入显示历史事实
`clipboard-ready`，一次失败显示 `clipboard-copy-failed`。

### 重要隐私边界

RDP 剪贴板同步会让这段终稿同时进入本机剪贴板和远端会话剪贴板。两边同一用户
会话中的应用、剪贴板历史工具或远程主机管理员策略都可能看到它。不要用该模式
输入密码、PIN、API Key、一次性验证码、恢复码或其他秘密。粘贴完成后，如内容
敏感，应按照所用桌面和剪贴板管理器的能力主动清除；本项目不会以定时覆盖方式
假装能够从所有剪贴板历史中撤回内容。

## 恢复默认光标模式

回到 **远程桌面** 页，选择 **当前光标（默认）** 并保存。下一条听写会重新使用
IBus 的聚焦、private-purpose、focus token、preedit 与 final-once 边界；正在进行的
听写保持开始时冻结的交付位置。

## 另一条高级路径：在远端会话运行语音输入

如果希望远端拥有 inline partial、private-field 拒绝和同一输入框纠错观察，需要把
麦克风重定向到 RDP，并在那个远端图形会话里运行 IBus engine 与 voice daemon：

1. 在 Remmina RDP profile 中把 **Redirect local microphone** 设置为 `sys:pulse`；
2. Snap 版 Remmina 还需要连接 `audio-record` 接口；
3. xrdp 服务器需要匹配的 PulseAudio/PipeWire xrdp source 模块；
4. 在远端会话中确认 `pactl list short sources` 可见 RDP source；
5. 在同一个远端 D-Bus/IBus 图形会话运行本项目。

当前 preview 明确以 Ubuntu 24.04 的 IBus focus-ID API 为目标。多会话部署与远端
音频模块安装没有自动化，因此这条路径仍属于高级手动配置。

参考：

- [Remmina microphone setting](https://gitlab.com/Remmina/Remmina/-/issues/2420)
- [xrdp audio and microphone redirection](https://github.com/neutrinolabs/xrdp#access-to-remote-resources)
- [PulseAudio modules for xrdp](https://github.com/neutrinolabs/pulseaudio-module-xrdp)

## 尚未实现：原生远端桥接

未来可以让本机只负责采集和识别，再把带 revision 的 partial/final 发送给远端会话
内一个小型 IBus helper。该桥接必须显式启用、认证、绑定一个聚焦上下文、拒绝
password/private 输入并丢弃陈旧 revision。当前版本没有发布这个协议；剪贴板模式
也不应被描述成这种安全边界等价的桥接。
