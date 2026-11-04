# 阶段 A：字幕与视频证据对比

本工具独立于网站，只处理人工选定的片段。运行此工具不会修改前端、线上 Agent、总结、会话记忆或业务数据库。现有服务无需因运行此脚本而重启。工具保留原来的“Qwen 画面观察 → DeepSeek”对比实验；网站已改为 [Qwen 直接视频问答](video-enhancement.md)，实验结果不代表当前网站的最终回答。

## 准备案例

复制 `tools/examples/video_compare.json` 并填写本地视频、带时间戳的字幕和问题。媒体路径相对于案例 JSON 所在目录。支持 ASR JSON (`segments/start/end/text`)、B 站 JSON (`body/from/to/content`)、SRT、VTT。视频与字幕必须来自同一版本、同一分 P；工具无法自动证明二者匹配。

- `windows: [0, 1]` 表示 0–10 和 10–20 秒；每个案例最多 3 块。
- 跨窗口字幕会关联到所有相交窗口；无字幕窗口保留。
- `subtitles: null` 表示没有字幕证据，不会伪造时间对齐；纯文本旧缓存需要重新提取。
- `expected_answer` 是人工复核答案，不会发送给模型。
- 10 秒是片段长度，不代表逐帧分析。默认视频输入采样为 2 FPS，快速事件仍可能遗漏。

## 离线准备（当前可使用）

在项目根目录使用现有虚拟环境执行：

```bash
.venv/bin/python -m tools.video_compare --cases tools/examples/my-cases.json --output data/video_validation/run-001
```

默认不联网、不调用模型、不需要 API Key。需要系统已安装 FFmpeg 和 ffprobe。输出目录必须不存在，避免覆盖旧实验。

输出包括静音 MP4 片段、`report.json` 和 `report.md`。切片重新编码以精确定位，不依赖关键帧复制；原始时间保留在报告中。尾片不足 2 秒时重复最后一帧补足视频模型的最短时长，补时不计入原视频时间，也不得作为新事件引用。

## 真实模型对比（等待接口配置）

确认服务平台的 OpenAI 兼容视频接口及模型名称后，在本地 `.env` 填写：

```dotenv
QWEN_VL_API_KEY=
QWEN_VL_BASE_URL=
QWEN_VL_MODEL=
```

BASE_URL 是服务提供方给出的基础地址，不包含 `/chat/completions`。不默认猜测密钥所属平台。适配器使用 `video_url` + MP4 Base64、`fps`、`enable_thinking: false` 和 JSON 输出；其他代理平台是否兼容这些参数，需要真实测试确认。

已有 DeepSeek 配置继续用于生成答案。显式添加 `--live` 才会调用模型并产生费用：

```bash
.venv/bin/python -m tools.video_compare --cases tools/examples/my-cases.json --output data/video_validation/run-002 --live
```

每个案例先生成仅字幕的 DeepSeek 回答，再逐块请求 Qwen 画面观察，最后用相同 DeepSeek 函数和同一组字幕生成增强回答。首次验证不带历史对话，排除记忆影响。视觉观察标明来源与不确定性，相对时间由程序转换为原视频时间；超出片段范围的模型时间戳会导致该案例失败。人工参考答案不会进入任何模型输入。

报告包含回答、证据、各阶段耗时和服务返回的 token 用量。未配置计费价格，不估算金额；不由另一个模型自动宣称正确率。失败时保留已经完成的阶段，不把缺少视频分析的结果伪装成增强回答。原有 DeepSeek 函数内部可能重试，返回的 usage 不能代表所有重试的累计账单。

## 人工验收

在 `report.json` 的 `review` 中记录基线正确性、增强正确性、视觉误判及备注。至少覆盖字幕可回答、仅画面可回答、OCR、跨窗口动作、画面模糊无法确认等案例。

本阶段只衡量“给对片段能否回答”，不能证明全片检索命中率。关键词/向量检索、全片视觉索引、自动下载、缓存淘汰、全局计数及前端接入属于后续阶段。

模型接口参考：[阿里云视频理解文档](https://help.aliyun.com/zh/model-studio/vision/)。

运行回归验证：

```bash
.venv/bin/python -m unittest discover -s tests -v
```
