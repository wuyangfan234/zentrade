"""全局配置 — 从环境变量 / .env 读取。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# ── 运行环境检测 ──────────────────────────────────────────
# PyInstaller 打包后: __file__ 指向临时解压目录 _MEIPASS, 不能作为路径基准。
# 此时:
#   - 只读资源 (tiers.yaml / 前端 dist) 放在 _MEIPASS 内
#   - 可写用户数据 (data_dir) 放在可执行文件旁的用户目录
# 非 frozen 模式 (开发/Docker): 保持原有 __file__ 推导, 行为完全不变。
_IS_FROZEN = getattr(sys, "frozen", False)


def _user_data_root() -> Path:
    """桌面版用户数据根目录。

    定位策略 (按优先级):
      1. 环境变量 DATA_DIR (pydantic-settings 自动注入到 settings.data_dir, 不在此处理)
      2. 打包桌面版: exe 同级的 data/ 子目录 (<安装目录>/data/)
         —— 与程序同处一个总目录 (用户选择的安装目录), 视觉直观, 便于备份/迁移。
      3. 非 frozen (开发模式): 项目根 data/

    为什么不用 platformdirs 默认 (%LOCALAPPDATA%) 作为主路径:
      - 落在 C 盘系统目录, 用户不易察觉, 占系统盘空间
      - 用户期望「数据跟随程序」(便于备份/迁移)
    为什么放 {app}/data (exe 旁的 data/) 而非 {app} 外的兄弟目录:
      - 用户体验: 用户选了安装目录, 自然期望「程序和数据都在这」, 单一总目录更直观。
      - 数据安全: Inno Setup 覆盖安装(升级)时只往 {app} 写新程序文件, 不会清空
        目录里不在安装清单上的运行时文件 (data/ 即此类), 故覆盖安装不丢数据。
    """
    # 打包桌面版: exe 同级的 data/ 子目录 (与程序同一总目录, 覆盖安装不丢数据)
    if _IS_FROZEN:
        exe_dir = Path(sys.executable).resolve().parent
        return exe_dir / "data"

    # 开发模式: 项目根 data/
    return _PROJECT_ROOT / "data"


def _resource_root() -> Path:
    """只读资源根目录。

    frozen: PyInstaller 解压目录 (_MEIPASS)
    非 frozen: 项目根目录 (源码树)
    """
    if _IS_FROZEN:
        # sys._MEIPASS 是 PyInstaller 注入的解压根
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parent.parent.parent


def _project_root() -> Path:
    """项目根目录 (非 frozen 用)。"""
    return Path(__file__).resolve().parent.parent.parent


_PROJECT_ROOT = _project_root()
_RESOURCE_ROOT = _resource_root()
_ENV_FILE = Path(
    os.environ.get(
        "TICKFLOW_ENV_FILE",
        str(_RESOURCE_ROOT / ".env") if not _IS_FROZEN else ".env",
    )
)


# 访问免 Key 公开端点 (Nasdaq 日历、CBOE、FRED 等) 时使用的 User-Agent: 如实标识本项目,
# 不伪装具体浏览器。保留 "Mozilla/5.0" 前缀是因为部分 CDN 只放行该前缀的客户端。
PUBLIC_HTTP_USER_AGENT = "Mozilla/5.0 (ZenTrade self-hosted)"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # AI
    ai_provider: str = "openai_compat"
    ai_base_url: str = ""   # 未配置; 新部署在设置页选预设或填写 (0.42.0 去掉上游赞助商默认地址)
    ai_api_key: str = ""
    ai_model: str = ""
    ai_codex_command: str = "codex"
    ai_codex_reasoning_effort: str = ""
    # AI 请求的 User-Agent。默认如实标识本项目; 个别网关要求特定 UA 时可在 AI 设置页修改。
    ai_user_agent: str = "ZenTrade (self-hosted; +https://github.com/wuyangfan234/zentrade)"
    # AI 输出上限 (max_tokens) 与输入上下文窗口上限 (约 token)。
    # 任务级 max_tokens 会被钳制到 ai_max_output_tokens; 输入估算超出上下文窗口时给出明确报错。
    # 钳制仅封顶不放大: 现有任务最多请求 4500, 上调默认值只影响用户自行调高任务上限的场景。
    # 上下文 128000 对齐当前主流模型底线 (GPT/Claude/GLM/DeepSeek/Kimi 均 ≥128k); 可在 AI 设置里调整。
    ai_max_output_tokens: int = 16384
    ai_context_window: int = 128000

    # Server
    host: str = "0.0.0.0"
    port: int = 3018
    log_level: str = "INFO"
    # 跨域来源白名单 (逗号分隔)。默认空 = 只允许同源 (前端由本服务托管, 或开发时经 Vite 代理, 都是同源)。
    # 只有前端与 API 分开部署在不同域名时才需要配置, 例如 "https://zentrade.example.com"。
    cors_allow_origins: str = ""
    # 登录 Cookie 的 Secure 标志。auto = 按请求协议判断 (HTTPS 或可信反代转发的 X-Forwarded-Proto=https 时开启);
    # true = 总是开启 (仅 HTTPS 部署); false = 总是关闭 (纯 HTTP 内网)。
    cookie_secure: str = "auto"
    # 新闻翻译兜底: AI 打标失败时用 Google 翻译网页端点 (非官方、无 SLA) 只翻译标题。默认关闭。
    news_google_translate_fallback: bool = False
    backtest_range_guard: bool = False
    backtest_matrix_disk_cache_enabled: bool = True
    backtest_matrix_cache_max_mb: int = 512
    backtest_matrix_cache_prewarm: bool = True
    backtest_matrix_cache_prewarm_years: int = 5

    # polars collect 并发闸 — polars 共享执行器在多线程并发 collect 下存在死锁
    # (上游 #24448/#25754 同族), 限流并发是社区验证的缓解手段。background 限额
    # 保证预热/增量等后台计算不占满闸位饿死页面读请求。
    polars_collect_permits: int = 4
    polars_collect_background_permits: int = 2

    # 后端自愈看门狗 — 探测 collect 闸与全局写锁, 连续失败即退出交由
    # supervisor 拉起 (见 app/watchdog.py)。误伤防护靠保守阈值。
    watchdog_enabled: bool = True
    watchdog_interval_s: float = 30.0
    watchdog_probe_timeout_s: float = 15.0
    watchdog_failure_threshold: int = 2

    # 策略批量执行 (run_all / 策略页全量跑) 的并发 worker 上限。实测 2026-09-07:
    # polars eager 操作内部已多线程并行, 外层再并发 4 worker 属超订, 41 策略
    # 299.6s 慢于串行 — 默认 1 (串行)。保留开关供配合 POLARS_MAX_THREADS 调优实验。
    strategy_run_all_workers: int = 1

    # run_all 渐进式返回: HTTP 同步等待时限 (秒)。策略按历史耗时升序执行,
    # 到点后已算完的随响应返回, 未算完的转后台继续算并逐个写入策略缓存,
    # 前端轮询 cached-summary 点亮卡片。0 = 关闭 (整段阻塞, 旧行为)。
    strategy_run_all_first_return_s: float = 15.0

    # Auth — 首次启动时预置访问密码(明文, 仅用于初始化, 详见 services/auth.bootstrap_from_env)
    # 公网服务器部署时免去 SSH 端口转发设密码的麻烦。写入 auth.json(哈希)后即不再读取。
    auth_password: str = ""

    # Data — frozen: exe 同级 data/ 子目录; 非 frozen: 项目根 data/
    # (均可被环境变量 DATA_DIR 覆盖, pydantic-settings 自动注入)
    data_dir: Path = _user_data_root()

    # tiers.yaml 路径 — frozen: 资源目录内; 非 frozen: 项目根目录
    tiers_yaml: Path = _RESOURCE_ROOT / "tiers.yaml" if _IS_FROZEN else _PROJECT_ROOT / "tiers.yaml"

    # 静态文件(前端 dist) — frozen: 资源目录的 static/; 非 frozen: frontend/dist
    static_dir: Path = _RESOURCE_ROOT / "static" if _IS_FROZEN else (_PROJECT_ROOT / "frontend" / "dist")

    @model_validator(mode="after")
    def _resolve_paths(self) -> Settings:
        """确保 data_dir 是绝对路径（环境变量传入的相对路径基于项目根目录解析）。"""
        if not self.data_dir.is_absolute():
            # 相对路径基于项目根目录解析，而非 CWD
            self.data_dir = (_PROJECT_ROOT / self.data_dir).resolve()
        if self.backtest_matrix_cache_max_mb <= 0:
            raise ValueError("backtest_matrix_cache_max_mb must be positive")
        if self.backtest_matrix_cache_prewarm_years <= 0:
            raise ValueError("backtest_matrix_cache_prewarm_years must be positive")
        if self.ai_max_output_tokens <= 0:
            raise ValueError("ai_max_output_tokens must be positive")
        if self.ai_context_window <= 0:
            raise ValueError("ai_context_window must be positive")
        if self.polars_collect_permits < 2:
            raise ValueError("polars_collect_permits must be >= 2")
        if not 1 <= self.polars_collect_background_permits < self.polars_collect_permits:
            raise ValueError(
                "polars_collect_background_permits must be in [1, polars_collect_permits)"
            )
        if self.watchdog_interval_s <= 0 or self.watchdog_probe_timeout_s <= 0:
            raise ValueError("watchdog intervals must be positive")
        if self.watchdog_failure_threshold < 1:
            raise ValueError("watchdog_failure_threshold must be >= 1")
        if self.strategy_run_all_workers < 1:
            raise ValueError("strategy_run_all_workers must be >= 1")
        if self.strategy_run_all_first_return_s < 0:
            raise ValueError("strategy_run_all_first_return_s must be >= 0")
        return self

settings = Settings()
