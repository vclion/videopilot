"""视频下载、字幕、总结和问答服务。"""

from dotenv import load_dotenv

from .paths import PROJECT_ROOT

# 先加载本地配置，再导入读取环境变量的业务模块；外部环境变量优先。
load_dotenv(PROJECT_ROOT / ".env")
