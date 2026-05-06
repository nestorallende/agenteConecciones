"""
agent/nodes/__init__.py
────────────────────────
Nodos del grafo Langraph del agente conversacional.

Cada nodo es una función async que recibe AgentState y retorna
un dict con las claves del estado que modifica.

Nodos del grafo:
  router          → Decide la siguiente acción basándose en la query
  retrieval       → Busca en el índice de Etapa 4 (Azure AI Search)
  context_builder → Formatea los chunks para el LLM
  tool_executor   → Invoca tools del MCP Server (Etapa 4)
  generate        → Llama al LLM y genera la respuesta final
  error_handler   → Maneja errores y retorna respuesta de fallback

Todos los nodos registran trazas en node_traces para telemetría.
"""

import time
from typing import Any

from src.agent.state import AgentState
from src.core.logging_config import get_logger
from src.core.telemetry import TelemetryClient, NodeTrace

logger = get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Fábrica de nodos (recibe dependencias y retorna funciones async)
# ─────────────────────────────────────────────────────────────────────────────

def make_router_node(config: dict, telemetry: TelemetryClient):
    """
    Nodo: ROUTER
    Analiza la query y decide la siguiente acción del agente:
      - "retrieve": la query necesita contexto del índice
      - "tool":     la query requiere una tool del MCP Server
      - "generate": responder directamente (saludos, clarificaciones, etc.)
      - "end":      la sesión debe terminar (farewell, etc.)
    """
    max_iterations = config.get("agent", {}).get("max_iterations", 10)

    async def router_node(state: AgentState) -> dict:
        start = time.perf_counter()
        session_id = state["session_id"]
        query      = state["query"]
        iteration  = state["iteration_count"] + 1

        logger.info(
            "[node:router] Evaluando acción siguiente",
            extra={
                "session_id": session_id,
                "query_len":  len(query),
                "iteration":  iteration,
                "component":  "router_node",
            },
        )

        # Guardia: evitar loops infinitos
        if iteration >= max_iterations:
            logger.warning(
                "[node:router] Máximo de iteraciones alcanzado — forzando end",
                extra={"session_id": session_id, "iteration": iteration, "component": "router_node"},
            )
            latency = (time.perf_counter() - start) * 1000
            return {
                "next_action":    "end",
                "iteration_count": iteration,
                "should_end":     True,
                "current_node":   "router",
                "node_traces":    [_trace("router", session_id, latency, True)],
            }

        # Lógica de routing por palabras clave (extender con LLM classifier si se necesita)
        q_lower = query.lower().strip()

        # Detectar cierre de sesión
        farewell_keywords = {"adiós", "bye", "chao", "hasta luego", "salir", "exit", "quit"}
        if any(kw in q_lower for kw in farewell_keywords):
            next_action = "end"

        # Detectar queries de búsqueda de información (mayoría de los casos)
        elif len(q_lower) > 10:
            next_action = "retrieve"

        # Preguntas muy cortas o saludos → responder directamente
        else:
            next_action = "generate"

        latency = (time.perf_counter() - start) * 1000
        logger.info(
            f"[node:router] Acción decidida: {next_action}",
            extra={
                "session_id":  session_id,
                "next_action": next_action,
                "latency_ms":  round(latency, 2),
                "component":   "router_node",
            },
        )

        telemetry.track_node(NodeTrace(
            session_id  = session_id,
            node_name   = "router",
            latency_ms  = round(latency, 2),
            success     = True,
        ))

        return {
            "next_action":    next_action,
            "iteration_count": iteration,
            "current_node":   "router",
            "node_traces":    [_trace("router", session_id, latency, True)],
        }

    return router_node


def make_retrieval_node(search_client: Any, config: dict, telemetry: TelemetryClient):
    """
    Nodo: RETRIEVAL
    Busca en el índice de Azure AI Search (construido en Etapa 4)
    y guarda los chunks recuperados en el estado.
    """
    retrieval_cfg = config.get("agent", {}).get("retrieval", {})
    top_k         = retrieval_cfg.get("top_k", 5)
    min_score     = retrieval_cfg.get("min_score_threshold", 0.7)
    search_type   = retrieval_cfg.get("search_type", "hybrid")

    async def retrieval_node(state: AgentState) -> dict:
        start      = time.perf_counter()
        session_id = state["session_id"]
        query      = state["query"]

        logger.info(
            "[node:retrieval] Iniciando búsqueda",
            extra={
                "session_id":  session_id,
                "query_len":   len(query),
                "top_k":       top_k,
                "search_type": search_type,
                "component":   "retrieval_node",
            },
        )

        try:
            chunks = await search_client.search(
                query       = query,
                session_id  = session_id,
                top_k       = top_k,
                search_type = search_type,
            )
            # Filtrar por score mínimo
            filtered = [c for c in chunks if c.get("score", 0) >= min_score]

            latency = (time.perf_counter() - start) * 1000
            logger.info(
                "[node:retrieval] Búsqueda completada",
                extra={
                    "session_id":     session_id,
                    "total_chunks":   len(chunks),
                    "filtered_chunks": len(filtered),
                    "min_score":      min_score,
                    "latency_ms":     round(latency, 2),
                    "component":      "retrieval_node",
                },
            )

            telemetry.track_node(NodeTrace(
                session_id   = session_id,
                node_name    = "retrieval",
                output_chars = sum(len(c.get("content", "")) for c in filtered),
                latency_ms   = round(latency, 2),
                success      = True,
            ))

            return {
                "retrieved_chunks": filtered,
                "current_node":     "retrieval",
                "node_traces":      [_trace("retrieval", session_id, latency, True)],
            }

        except Exception as exc:
            latency = (time.perf_counter() - start) * 1000
            logger.error(
                "[node:retrieval] Error en búsqueda",
                exc_info=exc,
                extra={"session_id": session_id, "component": "retrieval_node"},
            )
            telemetry.track_node(NodeTrace(
                session_id = session_id,
                node_name  = "retrieval",
                latency_ms = round(latency, 2),
                success    = False,
                error      = str(exc),
            ))
            return {
                "retrieved_chunks": [],
                "current_node":     "retrieval",
                "errors":           [f"retrieval_error: {exc}"],
                "node_traces":      [_trace("retrieval", session_id, latency, False, str(exc))],
            }

    return retrieval_node


def make_context_builder_node(search_client: Any, config: dict, telemetry: TelemetryClient):
    """
    Nodo: CONTEXT_BUILDER
    Formatea los chunks recuperados en un bloque de contexto
    listo para insertar en el prompt del LLM.
    """
    retrieval_cfg = config.get("agent", {}).get("retrieval", {})
    min_score     = retrieval_cfg.get("min_score_threshold", 0.7)

    async def context_builder_node(state: AgentState) -> dict:
        start      = time.perf_counter()
        session_id = state["session_id"]
        chunks     = state.get("retrieved_chunks", [])

        logger.info(
            "[node:context_builder] Construyendo contexto",
            extra={
                "session_id":  session_id,
                "chunks_count": len(chunks),
                "component":   "context_builder_node",
            },
        )

        context = search_client.format_context_for_llm(chunks, min_score=min_score)

        latency = (time.perf_counter() - start) * 1000
        logger.info(
            "[node:context_builder] Contexto construido",
            extra={
                "session_id":   session_id,
                "context_len":  len(context),
                "latency_ms":   round(latency, 2),
                "component":    "context_builder_node",
            },
        )

        telemetry.track_node(NodeTrace(
            session_id   = session_id,
            node_name    = "context_builder",
            output_chars = len(context),
            latency_ms   = round(latency, 2),
            success      = True,
        ))

        return {
            "context":      context,
            "current_node": "context_builder",
            "node_traces":  [_trace("context_builder", session_id, latency, True)],
        }

    return context_builder_node


def make_tool_executor_node(mcp_client: Any, config: dict, telemetry: TelemetryClient):
    """
    Nodo: TOOL_EXECUTOR
    Invoca las tools disponibles en el MCP Server de Etapa 4.
    Por ahora redirige a retrieval si no hay tools configuradas.
    """

    async def tool_executor_node(state: AgentState) -> dict:
        start      = time.perf_counter()
        session_id = state["session_id"]

        if not mcp_client.is_configured or not mcp_client.available_tools:
            logger.info(
                "[node:tool_executor] Sin tools MCP disponibles — redirigiendo a retrieval",
                extra={"session_id": session_id, "component": "tool_executor_node"},
            )
            return {
                "next_action":  "retrieve",
                "current_node": "tool_executor",
            }

        # TODO: implementar selección de tool según query cuando se definan las tools
        # Por ahora pasa a generate con los tool_results vacíos
        latency = (time.perf_counter() - start) * 1000
        telemetry.track_node(NodeTrace(
            session_id = session_id,
            node_name  = "tool_executor",
            latency_ms = round(latency, 2),
            success    = True,
        ))

        return {
            "tool_results": [],
            "current_node": "tool_executor",
            "node_traces":  [_trace("tool_executor", session_id, latency, True)],
        }

    return tool_executor_node


def make_generate_node(llm_client: Any, config: dict, telemetry: TelemetryClient):
    """
    Nodo: GENERATE
    Llama al LLM con el contexto recuperado y el historial
    para generar la respuesta final del agente.
    """

    async def generate_node(state: AgentState) -> dict:
        start      = time.perf_counter()
        session_id = state["session_id"]
        query      = state["query"]
        context    = state.get("context", "")
        messages   = list(state.get("messages", []))

        logger.info(
            "[node:generate] Generando respuesta",
            extra={
                "session_id":     session_id,
                "context_len":    len(context),
                "messages_count": len(messages),
                "component":      "generate_node",
            },
        )

        # Construir prompt con contexto si hay chunks recuperados
        if context and context != "No se encontró información relevante en la base de conocimientos.":
            # Insertar contexto antes del último mensaje del usuario
            context_message = {
                "role": "system",
                "content": (
                    "Usa el siguiente contexto de la base de conocimientos para responder. "
                    "Cita las fuentes cuando sea relevante.\n\n"
                    f"CONTEXTO:\n{context}"
                ),
            }
            # Insertar el contexto justo antes del último user message
            final_messages = messages[:-1] + [context_message] + messages[-1:]
        else:
            final_messages = messages

        try:
            response = await llm_client.complete(
                messages   = final_messages,
                session_id = session_id,
                node_name  = "generate",
            )

            latency = (time.perf_counter() - start) * 1000
            logger.info(
                "[node:generate] Respuesta generada",
                extra={
                    "session_id":  session_id,
                    "response_len": len(response),
                    "latency_ms":  round(latency, 2),
                    "component":   "generate_node",
                },
            )

            telemetry.track_node(NodeTrace(
                session_id   = session_id,
                node_name    = "generate",
                input_chars  = sum(len(m.get("content", "")) for m in final_messages),
                output_chars = len(response),
                latency_ms   = round(latency, 2),
                success      = True,
            ))

            return {
                "response":     response,
                "should_end":   True,
                "current_node": "generate",
                "messages":     [{"role": "assistant", "content": response}],
                "node_traces":  [_trace("generate", session_id, latency, True)],
            }

        except Exception as exc:
            latency = (time.perf_counter() - start) * 1000
            logger.error(
                "[node:generate] Error al generar respuesta",
                exc_info=exc,
                extra={"session_id": session_id, "component": "generate_node"},
            )
            telemetry.track_node(NodeTrace(
                session_id = session_id,
                node_name  = "generate",
                latency_ms = round(latency, 2),
                success    = False,
                error      = str(exc),
            ))

            fallback = "Lo siento, ocurrió un error al procesar tu consulta. Por favor intenta de nuevo."
            return {
                "response":     fallback,
                "should_end":   True,
                "current_node": "generate",
                "messages":     [{"role": "assistant", "content": fallback}],
                "errors":       [f"generate_error: {exc}"],
                "node_traces":  [_trace("generate", session_id, latency, False, str(exc))],
            }

    return generate_node


# ─────────────────────────────────────────────────────────────────────────────
# Helper interno
# ─────────────────────────────────────────────────────────────────────────────

def _trace(
    node_name:  str,
    session_id: str,
    latency_ms: float,
    success:    bool,
    error:      str = "",
) -> dict:
    """Crea un dict de traza de nodo para node_traces del estado."""
    return {
        "node":       node_name,
        "session_id": session_id,
        "latency_ms": round(latency_ms, 2),
        "success":    success,
        "error":      error,
    }
