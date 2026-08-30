# Hero 演示资产

`hero-demo.gif`、`hero-demo-poster.png` 和 `social-preview.png` 是由代码确定性
绘制的合成交互概念演示。它们没有录音、真实转写、API key、用户名、本地路径、
真实截图或下载素材；也不冒充产品实录。

动画根据当前产品流程重建：用户通过自定义快捷键按一次开始、再按一次完成；
文字通过 IBus 实时预编辑显示在当前光标；每轮只提交一份最终文字；限定时间内的
同输入框严格替换会用于下一次请求。它不声称所有 Linux 应用都会以完全相同的
样式渲染预编辑文字。

画面采用 GNOME 风格的中性标题栏，不绑定某个桌面主题，也不展示维护者个人使用
的固定快捷键。社交卡以中文为主要信息层级，英文只保留项目名称和必要技术词。

在仓库根目录运行以下命令即可重新生成三个文件：

```bash
python3 scripts/generate_hero_demo.py
```

生成依赖 Pillow 和 Noto Sans CJK。脚本没有网络访问（no network access），
不读取录音（no recording）、随机数、当前时间或主机数据；在 Pillow 和字体版本
一致时，重复运行会得到逐字节相同的文件。

资产用途：

- `hero-demo.gif`：960 x 540 的 README 循环动画，共 156 帧，平均 12 fps；
- `hero-demo-poster.png`：直接取自动画最后一帧的静态后备图；
- `social-preview.png`：1200 x 600 的 GitHub 仓库社交预览图。

后续修改动画时必须保留持续可见的 `合成交互演示` 标记，也不能把合成句子替换为
用户真实录制或收集的文字。
