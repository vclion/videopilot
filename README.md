# 视频下载助手

一个轻量的 Web 视频下载工具：粘贴链接，选择格式，即可下载视频或提取 MP3。

## 技术方案

- 后端：FastAPI
- 下载引擎：yt-dlp
- 前端：原生 HTML / CSS / JavaScript
- 存储：当前版本不依赖数据库，任务状态保存在内存，文件保存到 `downloads/`

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

### 2. 启动

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

> 说明：B 站大部分视频的 AI 字幕仅登录态可见，未配置 SESSDATA 时，仅能处理创作者上传的公开字幕；抖音无公开字幕，暂不支持 AI 总结。


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
- 会员身份使用浏览器本地 `member_token` 标识，无需注册账号。
- 数据保存在本地 `membership.db`（SQLite）。
- 当前为模拟支付：点击“开通会员” → 选择支付宝或微信 → 打开模拟支付页 → 点击“模拟支付成功”即开通。

### 配置

```bash
MEMBERSHIP_PLAN_DAYS=30
MEMBERSHIP_PRICE_CENTS=990
MEMBERSHIP_PLAN_NAME=30天会员
```

真实支付宝/微信支付接入时，需补充商户证书、密钥与回调地址；目前代码已预留订单与回调幂等结构。


## 用户登录与会员绑定

- 密码使用 bcrypt 哈希存储。
- 登录后签发 JWT Token（携带用户 ID 与过期时间），接口通过依赖注入自动校验登录态。
- 会员购买绑定到登录账号；注册采用邮箱 + 密码。
- 初始管理员账号：`admin` / `wcldsg666`（邮箱 `admin@example.com`）。

配置：

```bash
JWT_SECRET=请改成随机长字符串
JWT_EXPIRES_MINUTES=10080
ADMIN_EMAIL=admin@example.com
ADMIN_PASSWORD=wcldsg666
```
