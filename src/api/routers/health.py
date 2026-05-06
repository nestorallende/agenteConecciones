"""
api/routers/health.py
──────────────────────
Endpoint de health check del agente.
No requiere autenticación (ruta pública en JWT middleware).
"""

from fastapi import APIRouter, Request
from src.api.models.schemas import HealthResponse
from src.core.logging_config import get_logger

logger = get_logger(__name__)
router = APIRouter()


@router.get("/health", response_model=HealthResponse, summary="Health check")
async def health(request: Request) -> HealthResponse:
    """
    Verifica el estado de los componentes del agente.
    Usado por Container Apps para liveness y readiness probes.
    """
    d = getattr(request.app.state, "deps", None)

    components = {
        "llm_client":    "ok" if d and d.llm_client    and d.llm_client._client    else "not_ready",
        "search_client": "ok" if d and d.search_client and d.search_client._search_client else "not_ready",
        "memory_client": "ok" if d and d.memory_client and d.memory_client._container    else "not_ready",
        "mcp_client":    "ok" if d and d.mcp_client    and d.mcp_client.is_configured    else "not_configured",
        "agent_graph":   "ok" if d and d.agent_graph   else "not_ready",
    }

    all_ok   = all(v in ("ok", "not_configured") for v in components.values())
    status   = "ok" if all_ok else "degraded"
    env      = d.config.get("global_settings", {}).get("environment", "") if d else ""

    logger.info(
        "[health] Health check",
        extra={"status": status, "components": components, "component": "health_router"},
    )

    return HealthResponse(
        status      = status,
        environment = env,
        components  = components,
    )
