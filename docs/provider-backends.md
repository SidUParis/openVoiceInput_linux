# 语音识别后端

守护进程通过一个很小的 `ASRClient` 协议隔离麦克风、IBus、数据留存和云服务。
选择后端不会改变下面这些边界：只有用户开始听写后才采集音频；API Key 只保存在
`0600` 的私有配置；可选 WAV 留存仍由另一项默认关闭的同意开关控制；运行中不会
静默改投另一个后端。

| 后端 | 状态 | 交互 | 术语提示 | 说明 |
|---|---|---|---|---|
| 火山引擎 BigModel ASR 2.0 | 可用、默认 | 实时 | 热词与明确纠错 | 保留原有配置格式和行为 |
| 阿里云千问 Qwen Audio 3.0 ASR | 代码已接入，未用真实 Key 验收 | 实时 | 请求级即时热词 | 默认提示中文、英文、法文 |
| OpenAI Transcribe | 代码已接入，未用真实 Key 验收 | 停录后批量 | prompt | 在内存中有界保存 PCM，停录后上传 WAV |
| MiniMax | 计划项 | — | — | 暂未找到可独立验证的官方语音转文字接口，因此没有伪造适配器 |

## 配置

旧的 `{"api_key":"..."}` 仍被解释为火山引擎，不需要迁移。原生设置页的
“云端识别”页面可选择三个已接入后端；更换后端时必须同时输入该服务的新 Key，
已经保存的 Key 永远不会回填到窗口。也可以用交互式命令写入 version 2 私有配置：

```bash
murmur-voice-daemon configure --provider qwen
murmur-voice-daemon configure --provider openai
```

Key 通过终端无回显地输入两次，不应放在命令参数、日志、issue 或 Git 中。每次
新听写都会重新加载一份不可变配置快照；进行中的一句不会中途换后端。

## 已审核的协议边界

- Qwen 使用官方 duplex WebSocket 顺序：`run-task`、等待 `task-started`、发送
  16 kHz 单声道 PCM、`finish-task`、等待 `task-finished`。结果按
  `sentence_id` 组合，`task_id` 不匹配、未知事件和畸形 JSON 会失败关闭。
- OpenAI 仅调用官方 `/v1/audio/transcriptions`。它没有假装提供实时 partial；
  PCM 上限为十分钟，网络请求在后台线程运行，全进程最多一个上传任务，取消后
  迟到结果不会提交，也不会因快速重试无限堆积音频线程。
- MiniMax 只有 capability descriptor，设置页不能把它保存为可运行后端。

官方协议资料：

- [阿里云实时语音识别 WebSocket API](https://help.aliyun.com/en/model-studio/fun-asr-realtime-websocket-api)
- [阿里云客户端事件](https://help.aliyun.com/en/model-studio/fun-asr-client-events)
- [阿里云服务端事件](https://help.aliyun.com/en/model-studio/fun-asr-server-events)
- [OpenAI Audio Transcriptions API](https://platform.openai.com/docs/api-reference/audio/createTranscription)

当前自动测试全部使用假 WebSocket/HTTP 响应，不消耗真实配额。加入一个新后端前，
必须先固定官方 endpoint、认证方式、音频边界、超时、取消语义与 fake-transport
测试；不能只在 UI 中显示一个尚不存在的选项。
