"""
mcp/mcp_client.py
─────────────────
Cliente MCP que conecta el agente conversacional al MCP Server desplegado en Etapa 4.

Soporta 2 modos de autenticación (configurable en config_agent.yaml):
  - "managed_identity": obtiene token vía Managed Identity del Container App
  - "token":            usa API Key estática desde Key Vault

Las tools disponibles se cargan dinámicamente desde el servidor al inicializar,
lo que permite agregar nuevas tools en Etapa 4 sin modificar el agente.

Todos los eventos se loguean con latencia para monitoreo en Application Insights.
"""

import time
from typing import Any, Optional

import httpx

from src.core.logging_config import get_logger, LatencyTracker
from src.core.telemetry import TelemetryClient, MCPToolTrace

logger = get_logger(__name__)


class MCPClient:
    """
    Cliente HTTP para el MCP Server de Etapa 4.

    Descubre y expone las tools del servidor automáticamente.
    Registra telemetría de cada llamada a tool.

    Args:
        config:    Dict de configuración completo (config_agent.yaml resuelto).
        telemetry: Instancia de TelemetryClient para registro de métricas.
    """

    def __init__(self, config: dict, telemetry: Optional[TelemetryClient] = None):
        mcp_cfg = config.get("mcp_client", {})

        self._server_url  = mcp_cfg.get("server_url", "").rstrip("/")
        self._auth_type   = mcp_cfg.get("auth_type", "managed_identity")
        self._timeout     = mcp_cfg.get("timeout_seconds", 30)
        self._max_retries = mcp_cfg.get("max_retries", 3)
        self._telemetry   = telemetry

        # Credenciales según tipo de auth
        self._mi_client_id = mcp_cfg.get("managed_identity_client_id", "")
        self._mi_resource  = mcp_cfg.get("managed_identity_resource", "")
        self._api_key      = mcp_cfg.get("api_key", "")

        # Tools descubiertas del servidor (se cargan en initialize())
        self._tools: list[dict] = mcp_cfg.get("tools", [])

        # Cache de token para Managed Identity
        self._token: Optional[str]  = None
        self._token_expiry: float   = 0.0

        logger.info(
            "MCPClient configurado",
            extra={
                "server_url": self._server_url,
                "auth_type":  self._auth_type,
                "tools_predefined": len(self._tools),
                "component":  "MCPClient",
            },
        )

    # ─────────────────────────────────────────────
    # Inicialización
    # ─────────────────────────────────────────────
    async def initialize(self) -> None:
        """
        Inicializa el cliente y descubre las tools disponibles en el MCP Server.
        Si el servidor no está disponible, continúa con las tools predefinidas en config.
        """
        if not self._server_url:
            logger.warning(
                "MCP Server URL no configurada — MCPClient deshabilitado",
                extra={"component": "MCPClient"},
            )
            return

        try:
            with LatencyTracker(logger, "mcp_discover_tools", component="MCPClient"):
                discovered = await self._discover_tools()
            if discovered:
                self._tools = discovered
                logger.info(
                    f"Tools MCP descubiertas: {[t.get('name') for t in self._tools]}",
                    extra={"tools_count": len(self._tools), "component": "MCPClient"},
                )
        except Exception as exc:
            logger.warning(
                "No se pudieron descubrir tools del MCP Server — usando predefinidas",
                exc_info=exc,
                extra={
                    "server_url":      self._server_url,
                    "predefined_tools": len(self._tools),
                    "component":       "MCPClient",
                },
            )

    async def _discover_tools(self) -> list[dict]:
        """
        Llama al endpoint estándar MCP /tools/list para obtener las tools disponibles.
        Si el servidor no implementa este endpoint, retorna lista vacía.
        """
        headers = await self._get_auth_headers()
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(
                f"{self._server_url}/tools/list",
                headers=headers,
            )
            if resp.status_code == 200:
                data = resp.json()
                return data.get("tools", [])
            # Algunos MCP servers exponen en /mcp/tools
            resp2 = await client.get(
                f"{self._server_url}/mcp/tools",
                headers=headers,
            )
            if resp2.status_code == 200:
                data2 = resp2.json()
                return data2.get("tools", [])
        return []

    # ─────────────────────────────────────────────
    # Llamada a tools
    # ─────────────────────────────────────────────
    async def call_tool(
        self,
        tool_name:  str,
        arguments:  dict,
        session_id: str = "",
    ) -> Any:
        """
        Llama a una tool del MCP Server con reintentos automáticos.

        Args:
            tool_name:  Nombre de la tool a invocar.
            arguments:  Argumentos de la tool (dict).
            session_id: ID de sesión para trazabilidad.

        Returns:
            Resultado de la tool (cualquier tipo JSON serializable).

        Raises:
            MCPToolError: Si el servidor retorna error o no está disponible.
        """
        start = time.perf_counter()
        last_error: Optional[Exception] = None

        for attempt in range(1, self._max_retries + 1):
            try:
                result = await self._invoke_tool(tool_name, arguments)
                latency_ms = (time.perf_counter() - start) * 1000

                if self._telemetry:
                    self._telemetry.track_mcp_tool(MCPToolTrace(
                        session_id = session_id,
                        tool_name  = tool_name,
                        latency_ms = round(latency_ms, 2),
                        success    = True,
                        extra      = {"attempt": attempt},
                    ))

                logger.info(
                    f"[mcp] tool '{tool_name}' ejecutada exitosamente",
                    extra={
                        "session_id": session_id,
                        "tool_name":  tool_name,
                        "latency_ms": round(latency_ms, 2),
                        "attempt":    attempt,
                        "component":  "MCPClient",
                    },
                )
                return result

            except Exception as exc:
                last_error = exc
                logger.warning(
                    f"[mcp] Intento {attempt}/{self._max_retries} fallido para tool '{tool_name}'",
                    exc_info=exc,
                    extra={
                        "session_id": session_id,
                        "tool_name":  tool_name,
                        "attempt":    attempt,
                        "component":  "MCPClient",
                    },
                )

        # Todos los intentos fallaron
        latency_ms = (time.perf_counter() - start) * 1000
        if self._telemetry:
            self._telemetry.track_mcp_tool(MCPToolTrace(
                session_id = session_id,
                tool_name  = tool_name,
                latency_ms = round(latency_ms, 2),
                success    = False,
                error      = str(last_error),
            ))

        raise MCPToolError(
            f"Tool '{tool_name}' falló después de {self._max_retries} intentos: {last_error}"
        )

    async def _invoke_tool(self, tool_name: str, arguments: dict) -> Any:
        """Realiza la llamada HTTP al MCP Server."""
        headers = await self._get_auth_headers()
        payload = {
            "jsonrpc": "2.0",
            "id":      1,
            "method":  "tools/call",
            "params":  {
                "name":      tool_name,
                "arguments": arguments,
            },
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                f"{self._server_url}/mcp",
                json=payload,
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()

            if "error" in data:
                raise MCPToolError(
                    f"MCP error {data['error'].get('code')}: {data['error'].get('message')}"
                )
            return data.get("result")

    # ─────────────────────────────────────────────
    # Autenticación
    # ─────────────────────────────────────────────
    async def _get_auth_headers(self) -> dict:
        """Retorna los headers de autenticación según auth_type configurado."""
        if self._auth_type == "managed_identity":
            token = await self._get_managed_identity_token()
            return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        else:
            # auth_type == "token"
            return {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}

    async def _get_managed_identity_token(self) -> str:
        """
        Obtiene token de acceso vía Azure Managed Identity.
        Cachea el token hasta 5 minutos antes de su expiración.
        """
        now = time.time()
        if self._token and now < self._token_expiry - 300:
            return self._token

        logger.debug(
            "Renovando token de Managed Identity para MCP Server",
            extra={"resource": self._mi_resource, "component": "MCPClient"},
        )

        from azure.identity.aio import ManagedIdentityCredential

        async with ManagedIdentityCredential(
            client_id=self._mi_client_id if self._mi_client_id else None
        ) as credential:
            token_obj = await credential.get_token(
                f"{self._mi_resource}/.default"
            )
            self._token        = token_obj.token
            self._token_expiry = token_obj.expires_on

        return self._token

    # ─────────────────────────────────────────────
    # Propiedades
    # ─────────────────────────────────────────────
    @property
    def available_tools(self) -> list[dict]:
        """Lista de tools disponibles en el MCP Server."""
        return self._tools

    @property
    def is_configured(self) -> bool:
        """True si el MCP Server está configurado y tiene tools disponibles."""
        return bool(self._server_url)


# ─────────────────────────────────────────────
# Excepción específica de MCP
# ─────────────────────────────────────────────
class MCPToolError(Exception):
    """Error al invocar una tool del MCP Server."""
    pass
