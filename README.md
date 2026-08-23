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
