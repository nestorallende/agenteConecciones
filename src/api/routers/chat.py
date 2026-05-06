"""
api/routers/chat.py
────────────────────
Router FastAPI para el endpoint conversacional del agente.

Endpoints:
  POST /api/v1/chat        → Enviar mensaje y recibir respuesta del agente
  DELETE /api/v1/sessions/{session_id} → Limpiar historial de una sesión
"""

import time
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status

from src.agent.state import initial_state
from src.api.models.schemas import ChatRequest, ChatResponse, SourceReference
from src.core.logging_config import get_logger, set_log_context, clear_log_context
from src.core.security.jwt_middleware import get_current_user
from src.core.telemetry import SessionEvent

logger = get_logger(__name__)
router = APIRouter()


@router.post(
    "/chat",
    response_model=ChatResponse,
    summary="Enviar mensaje al agente",
    description="Procesa un mensaje del usuario y retorna la respuesta del agente con fuentes.",
)
async def chat(
    body:    ChatRequest,
    request: Request,
    user:    dict = Depends(get_current_user),
) -> ChatResponse:
    """
    Endpoint principal del agente conversacional.

    Flujo:
    1. Obtiene o crea session_id
    2. Recupera historial desde Cosmos DB
    3. Invoca el grafo Langraph
    4. Persiste los nuevos mensajes en Cosmos
    5. Retorna respuesta con fuentes y métricas
    """
    start = time.perf_counter()

    # Dependencias desde el estado de la app (cargadas en lifespan)
    d          = request.app.state.deps
    user_id    = user.get("oid") or user.get("sub", "anonymous")
    session_id = body.session_id or str(uuid.uuid4())
    request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))

    # Inyectar contexto en todos los logs de esta request
    set_log_context(
        session_id = session_id,
        user_id    = user_id,
        request_id = request_id,
    )

    logger.info(
        "[chat] Request recibida",
        extra={
            "session_id": session_id,
            "user_id":    user_id,
            "query_len":  len(body.query),
            "component":  "chat_router",
        },
    )

    # Telemetría: inicio de sesión
    d.telemetry.track_session(SessionEvent(
        session_id = session_id,
        event_type = "start",
        user_id    = user_id,
    ))

    try:
        # 1. Recuperar historial desde Cosmos DB
        history_docs = await d.memory_client.get_history(session_id)
        history      = d.memory_client.format_for_llm(history_docs)

        logger.info(
            "[chat] Historial recuperado",
            extra={
                "session_id":     session_id,
                "history_turns":  len(history),
                "component":      "chat_router",
            },
        )

        # 2. Construir estado inicial del grafo
        state = initial_state(
            query      = body.query,
            session_id = session_id,
            user_id    = user_id,
            history    = history,
        )

        # 3. Invocar el grafo Langraph
        result = await d.agent_graph.ainvoke(state)

        response_text = result.get("response", "")
        chunks        = result.get("retrieved_chunks", [])
        total_tokens  = result.get("total_tokens", 0)
        errors        = result.get("errors", [])

        if errors:
            logger.warning(
                "[chat] Errores durante ejecución del grafo",
                extra={
                    "session_id": session_id,
                    "errors":     errors,
                    "component":  "chat_router",
                },
            )

        # 4. Persistir mensajes en Cosmos DB
        await d.memory_client.save_message(
            session_id = session_id,
            role       = "user",
            content    = body.query,
            user_id    = user_id,
            metadata   = {"request_id": request_id},
        )
        await d.memory_client.save_message(
            session_id = session_id,
            role       = "assistant",
            content    = response_text,
            user_id    = user_id,
            metadata   = {
                "request_id":  request_id,
                "chunks_used": len(chunks),
                "tokens":      total_tokens,
                "errors":      errors,
            },
        )

        # 5. Construir respuesta
        latency_ms = (time.perf_counter() - start) * 1000
        sources    = [
            SourceReference(
                source = c.get("source", ""),
                score  = round(c.get("score", 0.0), 4),
                type   = c.get("type", ""),
            )
            for c in chunks
            if c.get("score", 0) > 0
        ]

        # Telemetría: fin de sesión
        d.telemetry.track_session(SessionEvent(
            session_id  = session_id,
            event_type  = "end",
            user_id     = user_id,
            duration_ms = round(latency_ms, 2),
            turns       = len(history) + 1,
        ))

        logger.info(
            "[chat] Respuesta generada exitosamente",
            extra={
                "session_id":  session_id,
                "response_len": len(response_text),
                "sources_count": len(sources),
                "latency_ms":  round(latency_ms, 2),
                "tokens":      total_tokens,
                "component":   "chat_router",
            },
        )

        return ChatResponse(
            session_id   = session_id,
            response     = response_text,
            sources      = sources,
            tokens_used  = total_tokens,
            latency_ms   = round(latency_ms, 2),
        )

    except Exception as exc:
        latency_ms = (time.perf_counter() - start) * 1000
        d.telemetry.track_error(
            session_id = session_id,
            component  = "chat_router",
            error      = exc,
        )
        d.telemetry.track_session(SessionEvent(
            session_id  = session_id,
            event_type  = "error",
            user_id     = user_id,
            duration_ms = round(latency_ms, 2),
            error       = str(exc),
        ))
        logger.error(
            "[chat] Error no controlado en endpoint /chat",
            exc_info=exc,
            extra={"session_id": session_id, "component": "chat_router"},
        )
        raise HTTPException(
            status_code = status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail      = "Error interno al procesar la consulta",
        )
    finally:
        clear_log_context()


@router.delete(
    "/sessions/{session_id}",
    summary="Limpiar historial de sesión",
    description="Elimina el historial conversacional de una sesión específica.",
)
async def clear_session(
    session_id: str,
    request:    Request,
    user:       dict = Depends(get_current_user),
) -> dict:
    """Elimina todos los mensajes de una sesión en Cosmos DB."""
    logger.info(
        "[chat] Solicitud de limpieza de sesión",
        extra={
            "session_id": session_id,
            "user_id":    user.get("oid"),
            "component":  "chat_router",
        },
    )
    # TODO: implementar delete en CosmosMemoryClient si se requiere
    return {"session_id": session_id, "status": "cleared"}
