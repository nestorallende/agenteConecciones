"""
logging_config.py
─────────────────
Configuración centralizada de logging para el agente conversacional.

Genera logs en formato JSON estructurado compatible con:
  - Azure Application Insights (via opencensus / azure-monitor-opentelemetry)
  - Azure Log Analytics
  - Cualquier sink que consuma JSON logs (Datadog, Elastic, etc.)

Patrón idéntico al core de Etapa 4, extendido con campos de agente:
  session_id, user_id, node, tool, latency_ms, tokens_used

USO:
    from src.core.logging_config import get_logger
    logger = get_logger(__name__)
    logger.info("Mensaje", extra={"session_id": "abc", "node": "retrieval"})
"""

import json
import logging
import sys
import time
import traceback
from typing import Any, Optional


# ─────────────────────────────────────────────
# Formatter JSON estructurado
# ─────────────────────────────────────────────
class JsonFormatter(logging.Formatter):
    """
    Convierte cada LogRecord en una línea JSON con campos fijos + extras.
    Application Insights los indexa como propiedades custom del trace.
    """

    # Campos base que siempre aparecen en el log
    BASE_FIELDS = {
        "timestamp", "level", "logger", "message",
        "module", "function", "line", "process", "thread",
    }

    def format(self, record: logging.LogRecord) -> str:
        # Construir payload base
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level":     record.levelname,
            "logger":    record.name,
            "message":   record.getMessage(),
            "module":    record.module,
            "function":  record.funcName,
            "line":      record.lineno,
            "process":   record.process,
            "thread":    record.thread,
        }

        # Adjuntar excepción si existe
        if record.exc_info:
            payload["exception"] = {
                "type":       str(record.exc_info[0].__name__) if record.exc_info[0] else None,
                "message":    str(record.exc_info[1]) if record.exc_info[1] else None,
                "stacktrace": traceback.format_exception(*record.exc_info),
            }

        # Adjuntar campos extra provistos por el caller
        # (session_id, user_id, node, tool, latency_ms, tokens_used, etc.)
        for key, value in record.__dict__.items():
            if key not in self.BASE_FIELDS and not key.startswith("_") and key not in {
                "args", "created", "exc_info", "exc_text", "filename",
                "funcName", "levelname", "levelno", "lineno", "message",
                "module", "msecs", "msg", "name", "pathname", "process",
                "processName", "relativeCreated", "stack_info", "taskName",
                "thread", "threadName",
            }:
                payload[key] = value

        return json.dumps(payload, ensure_ascii=False, default=str)


# ─────────────────────────────────────────────
# Contexto de operación (thread-local)
# ─────────────────────────────────────────────
import contextvars

_log_context: contextvars.ContextVar[dict] = contextvars.ContextVar(
    "log_context", default={}
)


def set_log_context(**kwargs: Any) -> None:
    """
    Establece campos que se adjuntarán automáticamente a todos los logs
    en el contexto actual (coroutine/request).

    Uso típico al inicio de una request FastAPI:
        set_log_context(session_id="abc", user_id="u123", request_id="r456")
    """
    current = _log_context.get({})
    _log_context.set({**current, **kwargs})


def clear_log_context() -> None:
    """Limpia el contexto de log al finalizar la request."""
    _log_context.set({})


def get_log_context() -> dict:
    """Retorna el contexto de log actual."""
    return _log_context.get({})


# ─────────────────────────────────────────────
# Filter que inyecta contexto en cada record
# ─────────────────────────────────────────────
class ContextFilter(logging.Filter):
    """Inyecta los campos del contexto actual en cada LogRecord."""

    def filter(self, record: logging.LogRecord) -> bool:
        ctx = _log_context.get({})
        for key, value in ctx.items():
            if not hasattr(record, key):
                setattr(record, key, value)
        return True


# ─────────────────────────────────────────────
# Setup del root logger (llamar una sola vez)
# ─────────────────────────────────────────────
_initialized = False


def setup_logging(level: str = "INFO") -> None:
    """
    Configura el root logger con JsonFormatter.
    Debe llamarse una sola vez al arrancar la aplicación (main.py).

    Args:
        level: Nivel de log (DEBUG, INFO, WARNING, ERROR, CRITICAL)
    """
    global _initialized
    if _initialized:
        return

    numeric_level = getattr(logging, level.upper(), logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(ContextFilter())

    root = logging.getLogger()
    root.setLevel(numeric_level)
    root.handlers.clear()
    root.addHandler(handler)

    # Silenciar loggers ruidosos de librerías externas
    for noisy in ("azure.core.pipeline", "urllib3", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _initialized = True


def get_logger(name: str) -> logging.Logger:
    """
    Retorna un logger configurado con el nombre del módulo.

    Args:
        name: Normalmente __name__ del módulo que llama.

    Returns:
        logging.Logger listo para usar.

    Ejemplo:
        logger = get_logger(__name__)
        logger.info("Iniciando nodo", extra={"node": "retrieval", "session_id": "x"})
    """
    return logging.getLogger(name)


# ─────────────────────────────────────────────
# Timer de latencia (context manager)
# ─────────────────────────────────────────────
class LatencyTracker:
    """
    Context manager para medir y loguear latencia de operaciones.

    Uso:
        with LatencyTracker(logger, "azure_search_query", session_id="abc") as t:
            results = search_client.search(query)
        # Al salir logea: {"operation": "azure_search_query", "latency_ms": 142.3}
    """

    def __init__(self, logger: logging.Logger, operation: str, **extra: Any):
        self.logger    = logger
        self.operation = operation
        self.extra     = extra
        self._start: Optional[float] = None

    def __enter__(self) -> "LatencyTracker":
        self._start = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        elapsed_ms = (time.perf_counter() - self._start) * 1000
        level      = logging.ERROR if exc_type else logging.INFO
        self.logger.log(
            level,
            f"[latency] {self.operation}",
            extra={
                "operation":  self.operation,
                "latency_ms": round(elapsed_ms, 2),
                "success":    exc_type is None,
                **self.extra,
            },
        )
        return False  # no suprimir excepción
