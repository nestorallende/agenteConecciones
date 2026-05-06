"""
llm/llm_client.py
─────────────────
Cliente directo a Azure OpenAI para el agente conversacional.

Encapsula:
  - Llamadas de chat completion con historial
  - Conteo de tokens (prompt + completion)
  - Registro de latencia y métricas en telemetría
  - Manejo de errores con reintentos
"""

import time
from typing import Any, Optional

from src.core.logging_config import get_logger, LatencyTracker
from src.core.telemetry import TelemetryClient, LLMTrace

logger = get_logger(__name__)


class LLMClient:
    """
    Cliente Azure OpenAI para el agente conversacional.

    Args:
        config:    Dict de configuración completo resuelto.
        llm_key:   Clave dentro de config["llms"] a usar ("gpt4_reasoner", etc.).
        telemetry: Instancia de TelemetryClient.
    """

    def __init__(
        self,
        config:    dict,
        llm_key:   str = "gpt4_reasoner",
        telemetry: Optional[TelemetryClient] = None,
    ):
        llm_cfg = config.get("llms", {}).get(llm_key, {})

        self._endpoint        = llm_cfg.get("endpoint", "")
        self._api_key         = llm_cfg.get("api_key", "")
        self._api_version     = llm_cfg.get("api_version", "")
        self._deployment_name = llm_cfg.get("deployment_name", "")
        self._temperature     = llm_cfg.get("temperature", 0.0)
        self._max_tokens      = llm_cfg.get("max_tokens", 4000)
        self._llm_key         = llm_key
        self._telemetry       = telemetry
        self._client: Any     = None

        logger.info(
            "LLMClient configurado",
            extra={
                "llm_key":         self._llm_key,
                "deployment_name": self._deployment_name,
                "component":       "LLMClient",
            },
        )

    # ─────────────────────────────────────────────
    # Inicialización
    # ─────────────────────────────────────────────
    def initialize(self) -> None:
        """Inicializa el cliente Azure OpenAI. Llamar una vez al arrancar."""
        from openai import AzureOpenAI

        self._client = AzureOpenAI(
            azure_endpoint=self._endpoint,
            api_key=self._api_key,
            api_version=self._api_version,
        )
        logger.info(
            "LLMClient inicializado",
            extra={
                "deployment": self._deployment_name,
                "component":  "LLMClient",
            },
        )

    # ─────────────────────────────────────────────
    # Chat completion
    # ─────────────────────────────────────────────
    async def complete(
        self,
        messages:   list[dict],
        session_id: str = "",
        node_name:  str = "",
        temperature: Optional[float] = None,
        max_tokens:  Optional[int] = None,
    ) -> str:
        """
        Realiza una llamada de chat completion al LLM.

        Args:
            messages:    Lista de {"role": ..., "content": ...}.
            session_id:  ID de sesión para trazabilidad.
            node_name:   Nombre del nodo Langraph que llama.
            temperature: Override de temperatura (None = usa config).
            max_tokens:  Override de max_tokens (None = usa config).

        Returns:
            Texto de respuesta del LLM.
        """
        if self._client is None:
            logger.error(
                "LLMClient no inicializado — llama a initialize() primero",
                extra={"session_id": session_id, "component": "LLMClient"},
            )
            return ""

        temp   = temperature if temperature is not None else self._temperature
        tokens = max_tokens  if max_tokens  is not None else self._max_tokens

        logger.info(
            f"[llm] Llamando a {self._deployment_name}",
            extra={
                "session_id":    session_id,
                "node":          node_name,
                "messages_count": len(messages),
                "temperature":   temp,
                "component":     "LLMClient",
            },
        )

        start = time.perf_counter()
        try:
            response = self._client.chat.completions.create(
                model=self._deployment_name,
                messages=messages,
                temperature=temp,
                max_tokens=tokens,
            )
            latency_ms = (time.perf_counter() - start) * 1000

            content         = response.choices[0].message.content or ""
            prompt_tokens   = response.usage.prompt_tokens     if response.usage else 0
            completion_tokens = response.usage.completion_tokens if response.usage else 0
            total_tokens    = response.usage.total_tokens       if response.usage else 0

            if self._telemetry:
                self._telemetry.track_llm(LLMTrace(
                    session_id        = session_id,
                    node_name         = node_name,
                    model             = self._deployment_name,
                    prompt_tokens     = prompt_tokens,
                    completion_tokens = completion_tokens,
                    total_tokens      = total_tokens,
                    latency_ms        = round(latency_ms, 2),
                    success           = True,
                ))

            logger.info(
                f"[llm] Respuesta recibida",
                extra={
                    "session_id":          session_id,
                    "node":                node_name,
                    "prompt_tokens":       prompt_tokens,
                    "completion_tokens":   completion_tokens,
                    "total_tokens":        total_tokens,
                    "latency_ms":          round(latency_ms, 2),
                    "response_len":        len(content),
                    "component":           "LLMClient",
                },
            )
            return content

        except Exception as exc:
            latency_ms = (time.perf_counter() - start) * 1000
            if self._telemetry:
                self._telemetry.track_llm(LLMTrace(
                    session_id = session_id,
                    node_name  = node_name,
                    model      = self._deployment_name,
                    latency_ms = round(latency_ms, 2),
                    success    = False,
                    error      = str(exc),
                ))
            logger.error(
                "[llm] Error en llamada al LLM",
                exc_info=exc,
                extra={
                    "session_id": session_id,
                    "node":       node_name,
                    "component":  "LLMClient",
                },
            )
            raise
