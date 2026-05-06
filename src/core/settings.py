"""
settings.py
───────────
Carga y resolución de configuración para el agente conversacional.

Patrón idéntico al core de Etapa 4:
  - Lee config_agent.yaml
  - Resuelve referencias ${KV-nombre} desde Azure Key Vault
  - Expone funciones load_config() y get_secret()

El objeto Config resultante es inmutable durante el ciclo de vida
de la aplicación y se pasa como dependencia a todos los componentes.
"""

import os
import re
from pathlib import Path
from typing import Any, Optional

import yaml

from src.core.logging_config import get_logger

logger = get_logger(__name__)

# Patrón de placeholder Key Vault: ${KV-nombre-del-secreto}
_KV_PATTERN = re.compile(r"\$\{(KV-[^}]+)\}")

# Patrón de placeholder DevOps / env var: ${AZDEVOPS-*} o ${VAR}
_ENV_PATTERN = re.compile(r"\$\{([^}]+)\}")


class SecretHelper:
    """
    Resuelve secretos desde Azure Key Vault.
    Cachea en memoria para evitar llamadas repetidas durante la misma ejecución.
    """

    def __init__(self, vault_url: Optional[str] = None):
        self._vault_url  = vault_url or os.environ.get("AZURE_KEY_VAULT_URL", "")
        self._cache: dict[str, str] = {}
        self._client: Any = None

        if self._vault_url:
            self._init_client()
        else:
            logger.warning(
                "AZURE_KEY_VAULT_URL no configurada — "
                "los placeholders ${KV-*} NO se resolverán",
                extra={"component": "SecretHelper"},
            )

    def _init_client(self) -> None:
        try:
            from azure.identity import DefaultAzureCredential
            from azure.keyvault.secrets import SecretClient

            credential    = DefaultAzureCredential()
            self._client  = SecretClient(
                vault_url=self._vault_url,
                credential=credential,
            )
            logger.info(
                "SecretHelper conectado a Key Vault",
                extra={"vault_url": self._vault_url, "component": "SecretHelper"},
            )
        except Exception as exc:
            logger.error(
                "No se pudo conectar al Key Vault",
                exc_info=exc,
                extra={"vault_url": self._vault_url, "component": "SecretHelper"},
            )
            self._client = None

    def get(self, secret_name: str) -> str:
        """
        Obtiene un secreto por nombre desde Key Vault.
        El nombre se normaliza: KV-Mi-Secret → mi-secret (lowercase, sin prefijo KV-)

        Args:
            secret_name: Nombre del placeholder sin ${}. Ejemplo: "KV-CosmosDB-Key"

        Returns:
            Valor del secreto o cadena vacía si no se puede resolver.
        """
        if secret_name in self._cache:
            return self._cache[secret_name]

        # Normalizar nombre: KV-CosmosDB-Key → cosmosdb-key
        kv_name = secret_name.replace("KV-", "", 1).lower()

        if self._client is None:
            logger.warning(
                f"Key Vault no disponible — secreto '{kv_name}' no resuelto",
                extra={"secret": kv_name, "component": "SecretHelper"},
            )
            return ""

        try:
            value = self._client.get_secret(kv_name).value
            self._cache[secret_name] = value
            logger.debug(
                f"Secreto '{kv_name}' resuelto desde Key Vault",
                extra={"secret": kv_name, "component": "SecretHelper"},
            )
            return value
        except Exception as exc:
            logger.error(
                f"Error al obtener secreto '{kv_name}' desde Key Vault",
                exc_info=exc,
                extra={"secret": kv_name, "component": "SecretHelper"},
            )
            return ""


def _resolve_value(value: Any, helper: SecretHelper) -> Any:
    """
    Resuelve placeholders ${KV-*} y ${VAR} en un valor.
    Recorre recursivamente dicts y listas.
    """
    if isinstance(value, str):
        # Primero intentar Key Vault
        kv_matches = _KV_PATTERN.findall(value)
        for kv_key in kv_matches:
            resolved = helper.get(kv_key)
            value    = value.replace(f"${{{kv_key}}}", resolved)

        # Luego variables de entorno
        env_matches = _ENV_PATTERN.findall(value)
        for env_key in env_matches:
            env_val = os.environ.get(env_key, "")
            value   = value.replace(f"${{{env_key}}}", env_val)

        return value

    if isinstance(value, dict):
        return {k: _resolve_value(v, helper) for k, v in value.items()}

    if isinstance(value, list):
        return [_resolve_value(item, helper) for item in value]

    return value


def load_config(config_path: str) -> dict:
    """
    Carga y resuelve config_agent.yaml.

    1. Lee el YAML desde disco.
    2. Inicializa SecretHelper con Key Vault.
    3. Recorre todo el árbol resolviendo ${KV-*} y ${VAR}.
    4. Retorna el dict de configuración completamente resuelto.

    Args:
        config_path: Ruta al archivo config_agent.yaml.

    Returns:
        Dict con toda la configuración resuelta.

    Raises:
        FileNotFoundError: Si el archivo no existe.
        yaml.YAMLError: Si el YAML es inválido.
    """
    path = Path(config_path)

    if not path.exists():
        logger.error(
            f"Archivo de config no encontrado: {config_path}",
            extra={"config_path": config_path, "component": "settings"},
        )
        raise FileNotFoundError(f"Config no encontrado: {config_path}")

    logger.info(
        f"Cargando configuración desde {config_path}",
        extra={"config_path": config_path, "component": "settings"},
    )

    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    # Inicializar helper con vault_url desde el yaml o desde env var
    vault_url = (
        raw.get("secrets", {}).get("vault_url")
        or os.environ.get("AZURE_KEY_VAULT_URL", "")
    )
    helper = SecretHelper(vault_url=vault_url)

    # Resolver todo el árbol
    resolved = _resolve_value(raw, helper)

    logger.info(
        "Configuración cargada y resuelta correctamente",
        extra={
            "config_path": config_path,
            "environment": resolved.get("global_settings", {}).get("environment"),
            "component":   "settings",
        },
    )
    return resolved


def get_secret(name: str) -> str:
    """
    Obtiene un secreto puntual desde Key Vault sin necesidad de cargar todo el config.
    Útil para obtener tokens en runtime (refresh de credenciales, etc.)

    Args:
        name: Nombre del secreto en formato "KV-nombre-del-secreto".

    Returns:
        Valor del secreto.
    """
    vault_url = os.environ.get("AZURE_KEY_VAULT_URL", "")
    helper    = SecretHelper(vault_url=vault_url)
    return helper.get(name)
