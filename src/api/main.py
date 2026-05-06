"""
api/main.py
───────────
Punto de entrada de la API FastAPI del agente conversacional.

Responsabilidades:
  - Inicializar todos los componentes al arrancar (lifespan)
  - Registrar middleware de autenticación JWT
  - Montar routers de endpoints
  - Exponer health check sin autenticación

Todos los eventos de startup/shutdown se loguean para
monitoreo en Application Insights.
"""

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.core.logging_config import setup_logging, get_logger
from src.core.settings import load_config
from src.core.telemetry import TelemetryClient
from src.core.security.jwt_middleware import JWTMiddleware
from src.core.memory.cosmos_memory import CosmosMemoryClient
from src.core.mcp.mcp_client import MCPClient
from src.core.search.azure_search import AzureSearchClient
from src.core.llm.llm_client import LLMClient
from src.agent.graph import build_agent_graph
from src.api.routers import chat, health

logger = get_logger(__name__)

# ─────────────────────────────────────────────
# Contenedor global de dependencias
# ─────────────────────────────────────────────
# Se llena durante el lifespan y se expone como estado de la app
class AppDependencies:
    config:        dict         = {}
    telemetry:     TelemetryClient  = None
    llm_client:    LLMClient        = None
    search_client: AzureSearchClient = None
    mcp_client:    MCPClient        = None
    memory_client: CosmosMemoryClient = None
    agent_graph:   object           = None


deps = AppDependencies()


# ─────────────────────────────────────────────
# Lifespan: startup y shutdown
# ─────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Ciclo de vida de la aplicación.
    Inicializa todos los clientes al arrancar y los cierra al terminar.
    """
    # ── STARTUP ──────────────────────────────────────────────────────────────
    config_path = os.environ.get("CONFIG_PATH", "config/config_agent.yaml")
    log_level   = os.environ.get("LOG_LEVEL", "INFO")

    # Configurar logging lo primero
    setup_logging(level=log_level)

    logger.info(
        "Iniciando agente conversacional",
        extra={"config_path": config_path, "component": "lifespan"},
    )

    try:
        # 1. Cargar configuración y resolver Key Vault
        deps.config = load_config(config_path)
        logger.info("Configuración cargada", extra={"component": "lifespan"})

        # 2. Telemetría (debe ir temprano para capturar errores de init)
        deps.telemetry = TelemetryClient(deps.config)

        # 3. LLM Client
        llm_key         = deps.config.get("agent", {}).get("llm_key", "gpt4_reasoner")
        deps.llm_client = LLMClient(deps.config, llm_key=llm_key, telemetry=deps.telemetry)
        deps.llm_client.initialize()
        logger.info("LLMClient inicializado", extra={"component": "lifespan"})

        # 4. Azure AI Search
        deps.search_client = AzureSearchClient(deps.config, telemetry=deps.telemetry)
        deps.search_client.initialize()
        logger.info("AzureSearchClient inicializado", extra={"component": "lifespan"})

        # 5. Cosmos DB Memory
        deps.memory_client = CosmosMemoryClient(deps.config)
        await deps.memory_client.initialize()
        logger.info("CosmosMemoryClient inicializado", extra={"component": "lifespan"})

        # 6. MCP Client
        deps.mcp_client = MCPClient(deps.config, telemetry=deps.telemetry)
        await deps.mcp_client.initialize()
        logger.info("MCPClient inicializado", extra={"component": "lifespan"})

        # 7. Grafo Langraph
        deps.agent_graph = build_agent_graph(
            config        = deps.config,
            llm_client    = deps.llm_client,
            search_client = deps.search_client,
            mcp_client    = deps.mcp_client,
            telemetry     = deps.telemetry,
        )
        logger.info("Grafo Langraph compilado", extra={"component": "lifespan"})

        # Exponer deps en el estado de la app para que los routers accedan
        app.state.deps = deps

        logger.info(
            "Agente conversacional iniciado correctamente",
            extra={
                "environment": deps.config.get("global_settings", {}).get("environment"),
                "component":   "lifespan",
            },
        )

    except Exception as exc:
        logger.error(
            "Error crítico durante startup — la app no pudo iniciar",
            exc_info=exc,
            extra={"component": "lifespan"},
        )
        raise

    # ── HANDOFF AL RUNTIME ───────────────────────────────────────────────────
    yield

    # ── SHUTDOWN ─────────────────────────────────────────────────────────────
    logger.info("Apagando agente conversacional", extra={"component": "lifespan"})

    if deps.memory_client:
        await deps.memory_client.close()
        logger.info("CosmosMemoryClient cerrado", extra={"component": "lifespan"})

    logger.info("Agente apagado correctamente", extra={"component": "lifespan"})


# ─────────────────────────────────────────────
# Crear aplicación FastAPI
# ─────────────────────────────────────────────
def create_app() -> FastAPI:
    """
    Factory de la aplicación FastAPI.
    Registra middleware, routers y configura la app.
    """
    config_path = os.environ.get("CONFIG_PATH", "config/config_agent.yaml")

    app = FastAPI(
        title       = "Agente Conversacional — Framework IA",
        description = "API del agente conversacional con RAG sobre Azure",
        version     = "1.0.0",
        lifespan    = lifespan,
        # Deshabilitar docs en producción si se requiere
        docs_url    = "/docs",
        redoc_url   = "/redoc",
    )

    # ── CORS ─────────────────────────────────────────────────────────────────
    app.add_middleware(
        CORSMiddleware,
        allow_origins     = ["*"],   # EDITAR: restringir en producción
        allow_credentials = True,
        allow_methods     = ["*"],
        allow_headers     = ["*"],
    )

    # ── JWT Middleware ────────────────────────────────────────────────────────
    # Se registra DESPUÉS de CORS para que las preflight requests no requieran auth
    # El config se carga mínimamente aquí solo para leer security settings
    # Los valores reales se usan desde deps.config en el lifespan
    try:
        raw_config = load_config(config_path)
        app.add_middleware(JWTMiddleware, config=raw_config)
        logger.info("JWTMiddleware registrado", extra={"component": "create_app"})
    except Exception as exc:
        logger.warning(
            "No se pudo cargar config para JWTMiddleware en create_app — "
            "se cargará en lifespan",
            exc_info=exc,
            extra={"component": "create_app"},
        )

    # ── Routers ───────────────────────────────────────────────────────────────
    app.include_router(health.router, tags=["health"])
    app.include_router(chat.router,   prefix="/api/v1", tags=["chat"])

    return app


# ─────────────────────────────────────────────
# Entrypoint
# ─────────────────────────────────────────────
app = create_app()
