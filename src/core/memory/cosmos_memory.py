"""
memory/cosmos_memory.py
───────────────────────
Gestión del historial conversacional en Azure Cosmos DB (NoSQL API).

Responsabilidades:
  - Crear database y container si no existen (idempotente)
  - Guardar mensajes por sesión (session_id como partition key)
  - Recuperar historial de una sesión ordenado por timestamp
  - Limpiar sesiones antiguas (TTL o borrado explícito)

Patrón de documento en Cosmos:
{
    "id":         "<session_id>_<message_index>",   ← clave única
    "session_id": "<uuid>",                          ← partition key
    "user_id":    "<entra_oid>",
    "role":       "user" | "assistant" | "system",
    "content":    "<texto del mensaje>",
    "timestamp":  "<ISO 8601>",
    "metadata":   { "node": "...", "tokens": 0, ... }
}
"""

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from src.core.logging_config import get_logger, LatencyTracker

logger = get_logger(__name__)


class CosmosMemoryClient:
    """
    Cliente de memoria conversacional sobre Azure Cosmos DB NoSQL API.

    Crea el database y container automáticamente si no existen,
    según los parámetros del config.

    Args:
        config: Dict de configuración completo (config_agent.yaml resuelto).
    """

    def __init__(self, config: dict):
        cosmos_cfg = config.get("cosmos_db", {})

        self._endpoint       = cosmos_cfg.get("endpoint", "")
        self._api_key        = cosmos_cfg.get("api_key", "")
        self._database_name  = cosmos_cfg.get("database_name", "agent_memory_db")
        self._container_name = cosmos_cfg.get("container_name", "conversation_history")
        self._partition_key  = cosmos_cfg.get("partition_key", "/session_id")
        self._default_ttl    = cosmos_cfg.get("default_ttl", 2592000)
        self._throughput     = cosmos_cfg.get("throughput", 400)
        self._max_messages   = cosmos_cfg.get("max_messages_per_session", 50)

        self._client    = None
        self._database  = None
        self._container = None

        logger.info(
            "CosmosMemoryClient configurado",
            extra={
                "endpoint":       self._endpoint,
                "database":       self._database_name,
                "container":      self._container_name,
                "partition_key":  self._partition_key,
                "default_ttl":    self._default_ttl,
                "component":      "CosmosMemoryClient",
            },
        )

    # ─────────────────────────────────────────────
    # Inicialización (llamar una vez al arrancar)
    # ─────────────────────────────────────────────
    async def initialize(self) -> None:
        """
        Inicializa el cliente Cosmos DB y garantiza que database + container existen.
        Idempotente: si ya existen no hace nada.
        """
        from azure.cosmos.aio import CosmosClient
        from azure.cosmos import PartitionKey
        from azure.cosmos.exceptions import CosmosResourceExistsError

        logger.info(
            "Inicializando conexión a Cosmos DB",
            extra={"component": "CosmosMemoryClient", "endpoint": self._endpoint},
        )

        self._client = CosmosClient(url=self._endpoint, credential=self._api_key)

        # Crear database si no existe
        try:
            self._database = await self._client.create_database(
                id=self._database_name,
            )
            logger.info(
                f"Database '{self._database_name}' creado",
                extra={"database": self._database_name, "component": "CosmosMemoryClient"},
            )
        except CosmosResourceExistsError:
            self._database = self._client.get_database_client(self._database_name)
            logger.info(
                f"Database '{self._database_name}' ya existe — reutilizando",
                extra={"database": self._database_name, "component": "CosmosMemoryClient"},
            )

        # Crear container si no existe
        pk_field = self._partition_key.lstrip("/")
        try:
            self._container = await self._database.create_container(
                id=self._container_name,
                partition_key=PartitionKey(path=self._partition_key),
                default_ttl=self._default_ttl if self._default_ttl > 0 else None,
                offer_throughput=self._throughput,
            )
            logger.info(
                f"Container '{self._container_name}' creado",
                extra={
                    "container":     self._container_name,
                    "partition_key": self._partition_key,
                    "ttl":           self._default_ttl,
                    "component":     "CosmosMemoryClient",
                },
            )
        except CosmosResourceExistsError:
            self._container = self._database.get_container_client(self._container_name)
            logger.info(
                f"Container '{self._container_name}' ya existe — reutilizando",
                extra={"container": self._container_name, "component": "CosmosMemoryClient"},
            )

    # ─────────────────────────────────────────────
    # Escritura
    # ─────────────────────────────────────────────
    async def save_message(
        self,
        session_id: str,
        role:       str,
        content:    str,
        user_id:    Optional[str] = None,
        metadata:   Optional[dict] = None,
    ) -> str:
        """
        Guarda un mensaje en el historial de la sesión.

        Args:
            session_id: ID único de la sesión conversacional.
            role:       "user" | "assistant" | "system"
            content:    Texto del mensaje.
            user_id:    OID del usuario (Entra ID claim 'oid').
            metadata:   Campos adicionales (node, tokens, latency_ms, etc.)

        Returns:
            ID del documento creado en Cosmos.
        """
        if self._container is None:
            logger.error(
                "CosmosMemoryClient no inicializado — llama a initialize() primero",
                extra={"session_id": session_id, "component": "CosmosMemoryClient"},
            )
            return ""

        doc_id = f"{session_id}_{uuid.uuid4().hex[:8]}"
        now    = datetime.now(timezone.utc).isoformat()

        document = {
            "id":         doc_id,
            "session_id": session_id,
            "user_id":    user_id or "",
            "role":       role,
            "content":    content,
            "timestamp":  now,
            "metadata":   metadata or {},
        }

        with LatencyTracker(
            logger,
            "cosmos_save_message",
            session_id=session_id,
            role=role,
            component="CosmosMemoryClient",
        ):
            await self._container.upsert_item(document)

        logger.info(
            "Mensaje guardado en Cosmos",
            extra={
                "doc_id":     doc_id,
                "session_id": session_id,
                "role":       role,
                "component":  "CosmosMemoryClient",
            },
        )
        return doc_id

    # ─────────────────────────────────────────────
    # Lectura
    # ─────────────────────────────────────────────
    async def get_history(
        self,
        session_id: str,
        max_turns:  Optional[int] = None,
    ) -> list[dict]:
        """
        Recupera el historial de una sesión ordenado por timestamp (más reciente al final).

        Args:
            session_id: ID de la sesión.
            max_turns:  Máximo de mensajes a retornar (None = usa config).

        Returns:
            Lista de documentos ordenados cronológicamente.
        """
        if self._container is None:
            logger.error(
                "CosmosMemoryClient no inicializado",
                extra={"session_id": session_id, "component": "CosmosMemoryClient"},
            )
            return []

        limit = max_turns or self._max_messages

        query = (
            "SELECT c.role, c.content, c.timestamp, c.metadata "
            "FROM c "
            "WHERE c.session_id = @session_id "
            "ORDER BY c.timestamp ASC "
            f"OFFSET 0 LIMIT {limit}"
        )
        params = [{"name": "@session_id", "value": session_id}]

        messages = []
        with LatencyTracker(
            logger,
            "cosmos_get_history",
            session_id=session_id,
            component="CosmosMemoryClient",
        ):
            async for item in self._container.query_items(
                query=query,
                parameters=params,
                partition_key=session_id,
            ):
                messages.append(item)

        logger.info(
            "Historial recuperado desde Cosmos",
            extra={
                "session_id":   session_id,
                "messages_count": len(messages),
                "component":    "CosmosMemoryClient",
            },
        )
        return messages

    # ─────────────────────────────────────────────
    # Formateo para LLM
    # ─────────────────────────────────────────────
    def format_for_llm(self, history: list[dict]) -> list[dict]:
        """
        Convierte el historial de Cosmos al formato messages[] de OpenAI.

        Args:
            history: Lista de documentos de Cosmos.

        Returns:
            Lista de {"role": ..., "content": ...} para la API de OpenAI.
        """
        return [
            {"role": msg["role"], "content": msg["content"]}
            for msg in history
            if msg.get("role") in ("user", "assistant", "system")
        ]

    # ─────────────────────────────────────────────
    # Cierre
    # ─────────────────────────────────────────────
    async def close(self) -> None:
        """Cierra el cliente Cosmos DB de forma limpia."""
        if self._client:
            await self._client.close()
            logger.info(
                "Conexión a Cosmos DB cerrada",
                extra={"component": "CosmosMemoryClient"},
            )
