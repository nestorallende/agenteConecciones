"""
telemetry.py
─────────────
Integración con Azure Application Insights y Azure AI Foundry
para telemetría del agente conversacional.

Registra:
  - Trazas de cada nodo del grafo Langraph
  - Métricas de calidad: relevance, groundedness, latency, tokens
  - Eventos de sesión: inicio, fin, error
  - Contadores de MCP tool calls

Patrón: cada componente del agente llama a TelemetryClient.track_*()
El cliente encapsula el envío para no contaminar lógica de negocio.
"""

import time
from dataclasses import dataclass, field
from typing import Any, Optional

from src.core.logging_config import get_logger, LatencyTracker

logger = get_logger(__name__)


# ─────────────────────────────────────────────
# Dataclasses de eventos
# ─────────────────────────────────────────────
@dataclass
class NodeTrace:
    """Traza de ejecución de un nodo Langraph."""
    session_id:  str
    node_name:   str
    input_chars: int       = 0
    output_chars: int      = 0
    latency_ms:  float     = 0.0
    tokens_used: int       = 0
    success:     bool      = True
    error:       Optional[str] = None
    extra:       dict      = field(default_factory=dict)


@dataclass
class RetrievalTrace:
    """Traza de búsqueda en Azure AI Search."""
    session_id:   str
    query:        str
    results_count: int     = 0
    top_score:    float    = 0.0
    latency_ms:   float    = 0.0
    search_type:  str      = "hybrid"
    extra:        dict     = field(default_factory=dict)


@dataclass
class LLMTrace:
    """Traza de llamada al LLM."""
    session_id:      str
    node_name:       str
    model:           str
    prompt_tokens:   int   = 0
    completion_tokens: int = 0
    total_tokens:    int   = 0
    latency_ms:      float = 0.0
    success:         bool  = True
    error:           Optional[str] = None


@dataclass
class SessionEvent:
    """Evento de ciclo de vida de sesión."""
    session_id:  str
    event_type:  str       # "start" | "end" | "error"
    user_id:     Optional[str] = None
    duration_ms: float     = 0.0
    turns:       int       = 0
    error:       Optional[str] = None


@dataclass
class MCPToolTrace:
    """Traza de llamada a una tool del MCP Server."""
    session_id:  str
    tool_name:   str
    latency_ms:  float     = 0.0
    success:     bool      = True
    error:       Optional[str] = None
    extra:       dict      = field(default_factory=dict)


# ─────────────────────────────────────────────
# Cliente de telemetría
# ─────────────────────────────────────────────
class TelemetryClient:
    """
    Cliente centralizado de telemetría del agente.

    Envía eventos a:
    1. Logger JSON estructurado (siempre) → Application Insights via Log Analytics
    2. Azure AI Foundry métricas (cuando está habilitado en config)

    Uso:
        telemetry = TelemetryClient(config)
        telemetry.track_node(NodeTrace(session_id="x", node_name="retrieval", ...))
    """

    def __init__(self, config: dict):
        self._config  = config
        self._enabled = config.get("observability", {}).get("enabled", True)
        self._metrics_config = config.get("observability", {}).get("foundry", {})

        logger.info(
            "TelemetryClient inicializado",
            extra={
                "telemetry_enabled": self._enabled,
                "foundry_metrics":   bool(self._metrics_config.get("endpoint")),
            },
        )

    # ── Nodos del grafo ──────────────────────────────────────────────────────
    def track_node(self, trace: NodeTrace) -> None:
        """Registra la ejecución de un nodo Langraph."""
        if not self._enabled:
            return

        level = "error" if not trace.success else "info"
        getattr(logger, level)(
            f"[node] {trace.node_name}",
            extra={
                "event_type":    "node_execution",
                "session_id":    trace.session_id,
                "node":          trace.node_name,
                "input_chars":   trace.input_chars,
                "output_chars":  trace.output_chars,
                "latency_ms":    trace.latency_ms,
                "tokens_used":   trace.tokens_used,
                "success":       trace.success,
                "error":         trace.error,
                **trace.extra,
            },
        )

    # ── Búsqueda ─────────────────────────────────────────────────────────────
    def track_retrieval(self, trace: RetrievalTrace) -> None:
        """Registra una búsqueda semántica en Azure AI Search."""
        if not self._enabled:
            return

        logger.info(
            "[retrieval] Azure AI Search query",
            extra={
                "event_type":     "retrieval",
                "session_id":     trace.session_id,
                "query_length":   len(trace.query),
                "results_count":  trace.results_count,
                "top_score":      trace.top_score,
                "latency_ms":     trace.latency_ms,
                "search_type":    trace.search_type,
                **trace.extra,
            },
        )

    # ── LLM calls ────────────────────────────────────────────────────────────
    def track_llm(self, trace: LLMTrace) -> None:
        """Registra una llamada al LLM con métricas de tokens y latencia."""
        if not self._enabled:
            return

        level = "error" if not trace.success else "info"
        getattr(logger, level)(
            f"[llm] {trace.model} @ {trace.node_name}",
            extra={
                "event_type":          "llm_call",
                "session_id":          trace.session_id,
                "node":                trace.node_name,
                "model":               trace.model,
                "prompt_tokens":       trace.prompt_tokens,
                "completion_tokens":   trace.completion_tokens,
                "total_tokens":        trace.total_tokens,
                "latency_ms":          trace.latency_ms,
                "success":             trace.success,
                "error":               trace.error,
            },
        )

    # ── Sesiones ─────────────────────────────────────────────────────────────
    def track_session(self, event: SessionEvent) -> None:
        """Registra un evento de ciclo de vida de sesión."""
        if not self._enabled:
            return

        level = "error" if event.event_type == "error" else "info"
        getattr(logger, level)(
            f"[session] {event.event_type}",
            extra={
                "event_type":  "session_lifecycle",
                "session_id":  event.session_id,
                "lifecycle":   event.event_type,
                "user_id":     event.user_id,
                "duration_ms": event.duration_ms,
                "turns":       event.turns,
                "error":       event.error,
            },
        )

    # ── MCP Tools ────────────────────────────────────────────────────────────
    def track_mcp_tool(self, trace: MCPToolTrace) -> None:
        """Registra una llamada a una tool del MCP Server."""
        if not self._enabled:
            return

        level = "error" if not trace.success else "info"
        getattr(logger, level)(
            f"[mcp] tool={trace.tool_name}",
            extra={
                "event_type": "mcp_tool_call",
                "session_id": trace.session_id,
                "tool_name":  trace.tool_name,
                "latency_ms": trace.latency_ms,
                "success":    trace.success,
                "error":      trace.error,
                **trace.extra,
            },
        )

    # ── Errores generales ────────────────────────────────────────────────────
    def track_error(
        self,
        session_id: str,
        component:  str,
        error:      Exception,
        **extra:    Any,
    ) -> None:
        """
        Registra un error no controlado en cualquier componente del agente.

        Args:
            session_id: ID de la sesión donde ocurrió el error.
            component:  Nombre del componente (node, api, mcp_client, etc.).
            error:      Excepción capturada.
            **extra:    Campos adicionales de contexto.
        """
        logger.error(
            f"[error] {component}: {type(error).__name__}",
            exc_info=error,
            extra={
                "event_type": "unhandled_error",
                "session_id": session_id,
                "component":  component,
                "error_type": type(error).__name__,
                "error_msg":  str(error),
                **extra,
            },
        )

    # ── Helper: timer de latencia ────────────────────────────────────────────
    def latency(self, operation: str, **extra: Any) -> LatencyTracker:
        """
        Context manager para medir latencia de cualquier operación.

        Uso:
            with telemetry.latency("cosmos_write", session_id="x"):
                await cosmos.upsert(doc)
        """
        return LatencyTracker(logger, operation, **extra)
