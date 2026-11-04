# 视频下载助手

一个轻量的 Web 视频下载工具：粘贴链接，选择格式，即可下载视频或提取 MP3。

## 技术方案

- 后端：FastAPI
- 下载引擎：yt-dlp
- 前端：原生 HTML / CSS / JavaScript
- 存储：下载任务保存在内存；账号、会员、问答记录与字幕缓存使用 SQLite，文件保存到 `downloads/` 和 `data/`

## 项目结构

```text
app/                 后端应用、模型接口与业务模块
static/              前端页面、脚本和样式
tests/               自动化测试
tools/               独立验证工具及示例
docs/                功能与配置说明
data/db/             SQLite 数据库（账号、会员、聊天及缓存）
data/transcripts/    提取的字幕与转写文件
data/video_cache/    问答使用的视频及切片缓存
downloads/           用户下载的视频和音频
run.py               服务启动入口
.env                 本地配置与密钥，不提交到 Git
.env.example         配置模板
```

运行时自动创建 `data/` 和 `downloads/`，这些目录中的数据库、字幕、缓存和下载文件不提交到 Git。已有部署若仍在根目录保留旧数据库，会优先读取旧文件。

## 本地运行

### 1. 安装依赖

建议使用 Python 3.10+。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

另外需要系统安装 FFmpeg，用于视频合并和 MP3 提取：

```bash
sudo apt update
sudo apt install -y ffmpeg
```

### 2. 配置认证

首次运行先复制配置模板；已有 `.env` 时保留原文件并补齐配置：

```bash
cp -n .env.example .env
chmod 600 .env
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

把命令生成的随机字符串填入 `.env` 的 `JWT_SECRET`（至少 32 位）。设置 `ADMIN_USERNAME`、`ADMIN_EMAIL` 和独立的 `ADMIN_PASSWORD`（至少 12 位且不超过 72 个 UTF-8 字节）。可再次运行随机字符串命令生成管理员密码。仓库不提供默认管理员密码或默认 JWT 密钥。

`.env` 在业务模块导入前加载；已设置的系统环境变量优先。认证配置不完整或仍为密钥占位值时，服务拒绝启动。修改配置后需要重启服务；更换 JWT 密钥后，原登录会话需要重新登录。

DeepSeek、Qwen 和 B 站登录态按所需功能填写，具体见下方说明。

### 3. 启动

```bash
python run.py
```

浏览器打开 <http://127.0.0.1:8000>。

## 接口

- `POST /api/parse`：解析链接
- `POST /api/download`：创建下载任务
- `GET /api/task/{task_id}`：查询任务状态
- `GET /api/file/{task_id}`：下载文件
- `GET /api/history`：最近任务
- `GET /api/health`：健康检查

## 说明

请仅下载你有权保存的公开内容，并遵守相关平台条款与当地法律法规。
## AI 视频总结（二期）

解析视频后可在页面下方生成 AI 总结：先获取视频字幕（B 站平台字幕 API 优先，其他平台用 yt-dlp 提取），再调用 DeepSeek 生成结构化总结（一句话摘要、核心观点、章节要点、金句、行动建议），支持导出 Markdown。

### 配置

在项目根目录创建 `.env`（可参考 `.env.example`）：

```bash
DEEPSEEK_API_KEY=你的DeepSeek密钥
DEEPSEEK_MODEL=deepseek-v4-flash
DEEPSEEK_BASE_URL=https://api.deepseek.com
# B 站 AI 字幕需要登录态，建议填写（登录 bilibili.com 后从浏览器 Cookie 中复制 SESSDATA）
BILIBILI_SESSDATA=
```

### 新增接口

- `POST /api/ai/summarize`：创建 AI 总结任务
- `GET /api/ai/task/{task_id}`：查询总结任务状态与结果
- `GET /api/ai/export/{task_id}?fmt=md|txt`：导出总结
- `POST /api/subtitles`：单独创建字幕提取任务，前端可查看完整字幕（不调用大模型）
- `GET /api/subtitles/task/{task_id}`：查询字幕提取任务状态与结果
- `GET /api/subtitles/file/{filename}`：打开已保存的语音转写字幕文件

> 说明：B 站大部分视频的 AI 字幕仅登录态可见。未配置 SESSDATA 时会先尝试公开字幕；平台字幕不可用时，可尝试下文的本地语音转写，实际可用性取决于平台访问和音频下载。


## SEO（搜索引擎优化）

在 `.env` 中配置公开域名后，服务会自动生成并返回：

- `/robots.txt`
- `/sitemap.xml`
- 首页 `canonical`、Open Graph、Twitter Card 和 `WebApplication` 结构化数据

配置示例：

```bash
SITE_URL=https://your-domain.com
```

`SITE_URL` 末尾不要带斜杠。未配置时默认使用 `https://example.com`，上线前请替换为真实域名。


## 会员与支付（当前为模拟支付）

- 非会员无法使用 AI 视频总结，视频下载与解析保持免费。
- 前端通过注册或登录账号绑定会员；接口保留基于浏览器本地 `member_token` 的兼容方式。
- 数据保存在本地 `data/db/membership.db`（SQLite）。
- 当前为模拟支付：点击“开通会员” → 选择支付宝或微信 → 打开模拟支付页 → 点击“模拟支付成功”即开通。

### 配置

```bash
MEMBERSHIP_PLAN_DAYS=30
MEMBERSHIP_PRICE_CENTS=990
MEMBERSHIP_PLAN_NAME=30天会员
```

模拟支付仅用于本地演示，确认接口会直接开通会员。公开部署前须禁用模拟支付或接入真实支付校验；现有订单与回调幂等结构不等于已接入支付宝或微信支付。


## 用户登录与会员绑定

- 密码使用 bcrypt 哈希存储。
- 登录后签发 JWT Token（携带用户 ID 与过期时间），接口通过依赖注入自动校验登录态。
- 会员购买绑定到登录账号；注册采用邮箱 + 密码。
- 管理员用户名由 `ADMIN_USERNAME` 配置，首次建库时使用 `ADMIN_EMAIL` 和 `ADMIN_PASSWORD` 创建；仓库不包含固定密码。

配置：

```bash
JWT_SECRET=
JWT_EXPIRES_MINUTES=10080
ADMIN_USERNAME=admin
ADMIN_EMAIL=admin@example.com
ADMIN_PASSWORD=
```

以上空值必须按前文说明填写。管理员初始化参数只在账号不存在时生效，修改 `ADMIN_PASSWORD` 不会自动重置已有账号密码。迁移已有部署时，应单独更换原默认管理员密码。

## Video Intelligence Agent（新增）

在不改变原有解析、下载、登录和会员接口的前提下，新增独立的 Agent 任务接口：

- `POST /api/agent/runs`：创建视频总结或视频问答任务
- `GET /api/agent/runs/{run_id}`：查询 Agent 状态、工具步骤和结果

请求示例：

```json
{
  "url": "https://example.com/video",
  "mode": "question",
  "question": "视频中提到的主要风险是什么？"
}
```

Agent 当前采用显式工具链，保留会员权限、话题记忆和历史记录。总结使用字幕与 DeepSeek；问答使用字幕和画面索引检索 10 秒候选视频，再由 Qwen 直接读取片段并回答。每次运行记录在独立的 `data/db/agent.db` 中。

### 本地语音转写备用链路

当 B 站或其他平台没有可读取的字幕轨道时，系统会自动尝试下载音频并使用 `faster-whisper` 本地转写，再交给 AI 总结。模型文件缓存在本机，不会提交到仓库。

语音转写完成后，正文和带时间片段的结构化结果会保存到项目下的 `data/transcripts/`，前端字幕面板会展示对应文件位置。

字幕提取结果会按规范化网址缓存到独立的 `data/db/subtitle_cache.db`。再次解析同一视频时会优先读取缓存，避免重复下载、转写和提取。

配置项：

```bash
ASR_ENABLED=true
ASR_MODEL=small
ASR_DEVICE=cpu
ASR_COMPUTE_TYPE=int8
ASR_LANGUAGE=auto
ASR_MAX_MINUTES=60
```

没有 GPU 时建议使用 `small + cpu + int8`；有 NVIDIA GPU 时可以改用更大的模型和对应的 CUDA 配置。首次使用会下载模型，耗时取决于网络和模型大小。

## 视频增强问答：阶段 A 验证工具

独立工具 `tools/video_compare.py` 准备 10 秒片段并对比“字幕 → DeepSeek”和“字幕 + 视频模型观察 → DeepSeek”。默认仅准备本地素材，不调用模型，不改变网站功能。配置示例见 `tools/examples/video_compare.json`，使用 `python -m tools.video_compare` 运行，验收说明见 [阶段 A 验证文档](docs/video-validation.md)。真实模型对比需补充服务接口配置并显式使用 `--live`。

## Qwen 视频问答

视频问答默认使用 Qwen：按 10 秒建立画面索引，由 Qwen 根据问题含义阅读字幕与画面记录、选择相关片段，再把实际 MP4、字幕和当前话题上下文直接发送给 Qwen 生成最终答案。片段选择不再使用关键词匹配。AI 总结继续使用 DeepSeek。

在 `.env` 填写 `QWEN_VL_API_KEY`、`QWEN_VL_BASE_URL`、`QWEN_VL_MODEL` 后重启即可启用问答；配置缺失时页面会明确提示。基础地址不包含 `/chat/completions`，服务需支持 OpenAI 兼容的多视频 Base64 输入和 JSON 输出。已使用 `qwen3.8-omni-flash` 完成真实视频输入与模型选择片段验证。索引、素材缓存、容量限制和验证步骤见 [视频问答使用说明](docs/video-enhancement.md)。显式设置 `VIDEO_ANALYSIS_ENABLED=false` 可恢复旧字幕问答。

## 上传 GitHub 前检查

`.env` 及其本地变体、虚拟环境、数据库及其日志、字幕、视频缓存、下载文件和 `tests/artifacts/` 已配置忽略；只提交 `.env.example` 配置模板。

```bash
git status --short
git add -A -- .gitignore .env.example README.md requirements.txt run.py app/ static/ docs/ tests/ tools/
git add -u -- '*.py'
git diff --cached --stat
git diff --cached --check
```

本次后端从根目录迁移到 `app/`，应同时提交新模块和旧模块的删除记录。`git commit -am` 不会添加尚未跟踪的新文件。提交前复核暂存清单，确认不含个人配置、凭据、数据库或视频。
