"""
search/azure_search.py
──────────────────────
Cliente de búsqueda semántica sobre el índice construido en Etapa 4.

Operaciones:
  - Búsqueda híbrida (vectorial + keyword) — recomendada
  - Búsqueda vectorial pura
  - Búsqueda por keyword (BM25)

El cliente genera el embedding de la query en tiempo real usando el mismo
modelo que Etapa 4 (text-embedding-3-large o el configurado).

Todos los resultados se loguean con score, latencia y número de chunks
para monitoreo de calidad de recuperación en Application Insights.
"""

from typing import Any, Optional

from src.core.logging_config import get_logger, LatencyTracker
from src.core.telemetry import TelemetryClient, RetrievalTrace

logger = get_logger(__name__)


class AzureSearchClient:
    """
    Cliente de recuperación sobre Azure AI Search.

    Consume el índice vectorial creado por s04_indexing en Etapa 4.
    Solo lectura — el agente no modifica el índice.

    Args:
        config:    Dict de configuración completo (config_agent.yaml resuelto).
        telemetry: Instancia de TelemetryClient para métricas de retrieval.
    """

    def __init__(self, config: dict, telemetry: Optional[TelemetryClient] = None):
        vs_cfg = config.get("vector_stores", {}).get("vectordb_text", {})
        emb_cfg = config.get("embeddings", {}).get("embeddings_large", {})

        self._endpoint       = vs_cfg.get("endpoint", "")
        self._api_key        = vs_cfg.get("api_key", "")
        self._index_name     = vs_cfg.get("index_name", "")
        self._top_k          = vs_cfg.get("top_k", 5)
        self._hybrid         = vs_cfg.get("hybrid_search", True)
        self._semantic_config = vs_cfg.get("semantic_configuration_name", "default")
        self._vector_dims    = vs_cfg.get("vector_dimensions", 3072)

        # Config del modelo de embeddings para generar vector de la query
        self._emb_endpoint   = emb_cfg.get("endpoint", "")
        self._emb_api_key    = emb_cfg.get("api_key", "")
        self._emb_api_version = emb_cfg.get("api_version", "")
        self._emb_deployment = emb_cfg.get("deployment_name", "")

        self._telemetry      = telemetry
        self._search_client  = None
        self._emb_client     = None

        logger.info(
            "AzureSearchClient configurado",
            extra={
                "index_name":    self._index_name,
                "top_k":         self._top_k,
                "hybrid_search": self._hybrid,
                "component":     "AzureSearchClient",
            },
        )

    # ─────────────────────────────────────────────
    # Inicialización
    # ─────────────────────────────────────────────
    def initialize(self) -> None:
        """
        Inicializa los clientes de Azure Search y Azure OpenAI Embeddings.
        Llamar una vez al arrancar la aplicación.
        """
        from azure.search.documents import SearchClient
        from azure.core.credentials import AzureKeyCredential
        from openai import AzureOpenAI

        self._search_client = SearchClient(
            endpoint=self._endpoint,
            index_name=self._index_name,
            credential=AzureKeyCredential(self._api_key),
        )

        self._emb_client = AzureOpenAI(
            azure_endpoint=self._emb_endpoint,
            api_key=self._emb_api_key,
            api_version=self._emb_api_version,
        )

        logger.info(
            "AzureSearchClient inicializado",
            extra={
                "endpoint":      self._endpoint,
                "index_name":    self._index_name,
                "emb_deployment": self._emb_deployment,
                "component":     "AzureSearchClient",
            },
        )

    # ─────────────────────────────────────────────
    # Búsqueda principal
    # ─────────────────────────────────────────────
    async def search(
        self,
        query:      str,
        session_id: str = "",
        top_k:      Optional[int] = None,
        search_type: Optional[str] = None,
        filters:    Optional[str] = None,
    ) -> list[dict]:
        """
        Realiza búsqueda en el índice de Etapa 4.

        Args:
            query:       Texto de búsqueda del usuario.
            session_id:  ID de sesión para trazabilidad.
            top_k:       Número de resultados (None = usa config).
            search_type: "hybrid" | "vector" | "keyword" (None = usa config).
            filters:     Filtro OData para Azure Search (ej: "type_source eq 'pdf'").

        Returns:
            Lista de chunks recuperados con score y metadata:
            [{"content": "...", "score": 0.92, "source": "doc.pdf", "type": "pdf", ...}]
        """
        if self._search_client is None:
            logger.error(
                "AzureSearchClient no inicializado — llama a initialize() primero",
                extra={"session_id": session_id, "component": "AzureSearchClient"},
            )
            return []

        k = top_k or self._top_k
        stype = search_type or ("hybrid" if self._hybrid else "keyword")

        logger.info(
            f"[search] query recibida",
            extra={
                "session_id":  session_id,
                "query_len":   len(query),
                "top_k":       k,
                "search_type": stype,
                "component":   "AzureSearchClient",
            },
        )

        with LatencyTracker(
            logger, "azure_search_query",
            session_id=session_id, search_type=stype, component="AzureSearchClient"
        ) as timer:
            if stype == "hybrid":
                results = await self._hybrid_search(query, k, filters)
            elif stype == "vector":
                results = await self._vector_search(query, k, filters)
            else:
                results = self._keyword_search(query, k, filters)

        top_score = results[0]["score"] if results else 0.0

        if self._telemetry:
            self._telemetry.track_retrieval(RetrievalTrace(
                session_id    = session_id,
                query         = query,
                results_count = len(results),
                top_score     = top_score,
                latency_ms    = timer._start and round(
                    (__import__("time").perf_counter() - timer._start) * 1000, 2
                ) or 0.0,
                search_type   = stype,
            ))

        logger.info(
            f"[search] {len(results)} chunks recuperados",
            extra={
                "session_id":     session_id,
                "results_count":  len(results),
                "top_score":      top_score,
                "component":      "AzureSearchClient",
            },
        )
        return results

    # ─────────────────────────────────────────────
    # Estrategias de búsqueda
    # ─────────────────────────────────────────────
    async def _hybrid_search(
        self, query: str, k: int, filters: Optional[str]
    ) -> list[dict]:
        """Búsqueda híbrida: vectorial + keyword + semántica."""
        from azure.search.documents.models import VectorizedQuery

        vector = await self._embed(query)
        vector_query = VectorizedQuery(
            vector=vector,
            k_nearest_neighbors=k,
            fields="content_vector",
        )

        results = self._search_client.search(
            search_text=query,
            vector_queries=[vector_query],
            filter=filters,
            top=k,
            query_type="semantic",
            semantic_configuration_name=self._semantic_config,
            select=["content", "source", "type_source", "keywords", "chunk_id"],
        )
        return self._parse_results(results)

    async def _vector_search(
        self, query: str, k: int, filters: Optional[str]
    ) -> list[dict]:
        """Búsqueda vectorial pura."""
        from azure.search.documents.models import VectorizedQuery

        vector = await self._embed(query)
        vector_query = VectorizedQuery(
            vector=vector,
            k_nearest_neighbors=k,
            fields="content_vector",
        )
        results = self._search_client.search(
            search_text=None,
            vector_queries=[vector_query],
            filter=filters,
            top=k,
            select=["content", "source", "type_source", "keywords", "chunk_id"],
        )
        return self._parse_results(results)

    def _keyword_search(
        self, query: str, k: int, filters: Optional[str]
    ) -> list[dict]:
        """Búsqueda BM25 por keyword."""
        results = self._search_client.search(
            search_text=query,
            filter=filters,
            top=k,
            select=["content", "source", "type_source", "keywords", "chunk_id"],
        )
        return self._parse_results(results)

    # ─────────────────────────────────────────────
    # Embedding de la query
    # ─────────────────────────────────────────────
    async def _embed(self, text: str) -> list[float]:
        """Genera el vector embedding de la query para búsqueda vectorial."""
        with LatencyTracker(logger, "embed_query", component="AzureSearchClient"):
            response = self._emb_client.embeddings.create(
                input=[text],
                model=self._emb_deployment,
            )
        return response.data[0].embedding

    # ─────────────────────────────────────────────
    # Parseo de resultados
    # ─────────────────────────────────────────────
    def _parse_results(self, results: Any) -> list[dict]:
        """Convierte resultados de Azure Search al formato interno del agente."""
        parsed = []
        for r in results:
            parsed.append({
                "content":   r.get("content", ""),
                "score":     r.get("@search.score", 0.0),
                "source":    r.get("source", ""),
                "type":      r.get("type_source", ""),
                "keywords":  r.get("keywords", []),
                "chunk_id":  r.get("chunk_id", ""),
            })
        return parsed

    # ─────────────────────────────────────────────
    # Formateo para LLM
    # ─────────────────────────────────────────────
    def format_context_for_llm(
        self,
        chunks:     list[dict],
        min_score:  float = 0.0,
    ) -> str:
        """
        Convierte chunks recuperados en un bloque de contexto para el LLM.

        Args:
            chunks:    Lista de chunks de _parse_results().
            min_score: Score mínimo para incluir el chunk (filtro de calidad).

        Returns:
            String de contexto para insertar en el prompt del LLM.
        """
        filtered = [c for c in chunks if c["score"] >= min_score]
        if not filtered:
            return "No se encontró información relevante en la base de conocimientos."

        parts = []
        for i, chunk in enumerate(filtered, 1):
            source = chunk["source"] or "Fuente desconocida"
            parts.append(
                f"[{i}] Fuente: {source}\n"
                f"Relevancia: {chunk['score']:.2f}\n"
                f"{chunk['content']}"
            )

        return "\n\n---\n\n".join(parts)
