---
kind: logging_system
name: 基于 Loguru 的结构化日志系统
category: logging_system
scope:
    - '**'
source_files:
    - opc/layer6_observability/opc_logger.py
    - opc/engine.py
    - opc/core/config.py
    - pyproject.toml
---

## 1. 使用的框架与工具
- 统一采用 **loguru**（>=0.7.0，见 pyproject.toml）作为唯一日志后端，全仓通过 `from loguru import logger` 获取全局单例 logger。
- 未使用 Python 标准库 logging、structlog、sentry 等其它方案；仅在配置加载异常时回退到 `logging.getLogger(__name__).error(...)` 做兜底。

## 2. 核心文件与入口
- 日志初始化：`opc/layer6_observability/opc_logger.py` 中的 `setup_logging(log_dir, level)` 是唯一的集中配置点。
- 启动注入：`opc/engine.py::Engine.initialize()` 在引擎初始化早期调用 `setup_logging(self.opc_home / "logs", self.config.system.log_level)`，确保后续所有模块的 logger 输出均受控。
- 级别来源：`opc/core/config.py::SystemConfig.log_level`（默认 `"INFO"`），CLI 可通过调试模式覆盖为 `"DEBUG"`。

## 3. 架构与约定
- **双 Sink 策略**：
  - 控制台 Sink：输出到 stderr，启用彩色，格式为 `<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan> - {message}`，级别由 `config.system.log_level` 控制。
  - 文件 Sink：当日志目录存在时，按日期创建子目录 `logs/YYYY-MM-DD/opc.log`，固定 DEBUG 级别，格式包含 `{name}:{function}:{line}`，并启用 `rotation="50 MB"` 与 `retention="30 days"` 自动轮转与清理。
- **全局单例**：各模块直接 `from loguru import logger` 使用同一实例，无需显式传递 logger 对象，降低耦合。
- **结构化字段**：当前代码以字符串模板拼接为主（如 `logger.warning("{} runtime error: {}", self.name, self._last_error)`），尚未广泛采用 loguru 的 `extra={...}` 结构化字段能力。
- **第三方 SDK 降噪**：部分 channel 会显式关闭其内部日志（如 mochat 的 `logger=False`、`engineio_logger=False`），避免重复输出。

## 4. 开发者应遵循的规则
- **不要自行 `logging.basicConfig` 或新建 logger**：统一通过 `from loguru import logger` 使用全局实例，由 engine 初始化阶段完成 sink 配置。
- **日志级别选择**：
  - 正常流程信息用 `info`；
  - 可恢复异常/降级路径用 `warning`；
  - 需要堆栈跟踪的用 `exception`；
  - 仅开发/排障用的细节用 `debug`。
- **格式化风格**：优先使用 loguru 的 `logger.info("msg with {}", arg)` 占位语法，避免 f-string 提前求值。
- **结构化扩展**：如需下游分析，建议使用 `logger.bind(session_id=..., work_item_id=...)` 附加上下文键，而非拼接到消息文本中。
- **敏感信息**：避免在日志中打印 token、密码、API key 等敏感内容；`AutonomyConfig.sensitive_keywords` 已定义一批敏感词清单，可在未来接入脱敏中间件。
- **外部依赖日志**：对集成第三方 SDK（飞书、mochat 等）时，参考现有做法显式关闭其自带日志，防止重复输出。