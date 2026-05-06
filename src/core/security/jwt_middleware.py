"""
security/jwt_middleware.py
──────────────────────────
Middleware FastAPI para validación de tokens JWT emitidos por Entra ID.

Valida en cada request:
  - Firma del token (clave pública JWKS de Entra ID)
  - Issuer (https://login.microsoftonline.com/{tenant_id}/v2.0)
  - Audience (client_id del App Registration del agente)
  - Expiración

Adjunta el payload del token a request.state.token_claims
para que los endpoints accedan a user_id, roles, etc.

Rutas públicas (health, docs) se omiten de la validación.
"""

import time
from typing import Any, Optional

import httpx
from fastapi import Request, HTTPException, status
from fastapi.responses import JSONResponse
from jose import JWTError, jwt
from jose.exceptions import ExpiredSignatureError, JWTClaimsError
from starlette.middleware.base import BaseHTTPMiddleware

from src.core.logging_config import get_logger, set_log_context

logger = get_logger(__name__)

# URL pública de las claves JWKS de Entra ID (no cambia por tenant)
_JWKS_URL_TEMPLATE = (
    "https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys"
)

# Caché en memoria de las claves JWKS (se renuevan cada hora)
_jwks_cache: dict[str, Any] = {}
_jwks_cache_ts: float = 0.0
_JWKS_CACHE_TTL = 3600  # 1 hora


class JWTMiddleware(BaseHTTPMiddleware):
    """
    Middleware de autenticación JWT para el agente conversacional.

    Configuración esperada en config["security"]:
        tenant_id: str
        client_id: str
        audience:  str  (normalmente igual a client_id)
        public_endpoints: list[str]
    """

    def __init__(self, app, config: dict):
        super().__init__(app)
        sec              = config.get("security", {})
        self._tenant_id  = sec.get("tenant_id", "")
        self._client_id  = sec.get("client_id", "")
        self._audience   = sec.get("audience", "") or self._client_id
        self._issuer     = (
            sec.get("issuer")
            or f"https://login.microsoftonline.com/{self._tenant_id}/v2.0"
        )
        self._public     = set(sec.get("public_endpoints", ["/health", "/docs", "/openapi.json"]))

        logger.info(
            "JWTMiddleware inicializado",
            extra={
                "tenant_id":        self._tenant_id,
                "audience":         self._audience,
                "issuer":           self._issuer,
                "public_endpoints": list(self._public),
                "component":        "JWTMiddleware",
            },
        )

    # ─────────────────────────────────────────────
    # Punto de entrada del middleware
    # ─────────────────────────────────────────────
    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Rutas públicas: pasar sin validar
        if any(path.startswith(pub) for pub in self._public):
            return await call_next(request)

        # Extraer token del header Authorization: Bearer <token>
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            logger.warning(
                "Request sin token Bearer",
                extra={
                    "path":      path,
                    "method":    request.method,
                    "component": "JWTMiddleware",
                },
            )
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": "Token de autenticación requerido"},
                headers={"WWW-Authenticate": "Bearer"},
            )

        token = auth_header[len("Bearer "):]

        try:
            claims = await self._validate_token(token)
        except ExpiredSignatureError:
            logger.warning(
                "Token expirado",
                extra={"path": path, "component": "JWTMiddleware"},
            )
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": "Token expirado"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        except JWTClaimsError as exc:
            logger.warning(
                f"Claims inválidos: {exc}",
                extra={"path": path, "component": "JWTMiddleware"},
            )
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": f"Token inválido: {exc}"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        except JWTError as exc:
            logger.error(
                "Error al validar token JWT",
                exc_info=exc,
                extra={"path": path, "component": "JWTMiddleware"},
            )
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": "Token inválido"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        except Exception as exc:
            logger.error(
                "Error inesperado en validación JWT",
                exc_info=exc,
                extra={"path": path, "component": "JWTMiddleware"},
            )
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={"detail": "Error interno de autenticación"},
            )

        # Adjuntar claims al estado de la request
        request.state.token_claims = claims
        user_id    = claims.get("oid") or claims.get("sub", "anonymous")
        request_id = request.headers.get("X-Request-ID", "")

        # Inyectar en contexto de logging para todos los logs de esta request
        set_log_context(
            user_id    = user_id,
            request_id = request_id,
            path       = path,
        )

        logger.info(
            "Request autenticada",
            extra={
                "user_id":    user_id,
                "path":       path,
                "method":     request.method,
                "component":  "JWTMiddleware",
            },
        )

        response = await call_next(request)
        return response

    # ─────────────────────────────────────────────
    # Validación del token
    # ─────────────────────────────────────────────
    async def _validate_token(self, token: str) -> dict:
        """
        Valida la firma, issuer, audience y expiración del token.

        Returns:
            Dict con los claims del token.

        Raises:
            JWTError, ExpiredSignatureError, JWTClaimsError
        """
        jwks = await self._get_jwks()

        # jose.jwt.decode valida firma, issuer, audience y expiración
        claims = jwt.decode(
            token,
            jwks,
            algorithms=["RS256"],
            audience=self._audience,
            issuer=self._issuer,
            options={"verify_at_hash": False},
        )
        return claims

    async def _get_jwks(self) -> dict:
        """
        Obtiene las claves JWKS de Entra ID con caché en memoria.
        Se renueva automáticamente cada hora.
        """
        global _jwks_cache, _jwks_cache_ts

        now = time.time()
        if _jwks_cache and (now - _jwks_cache_ts) < _JWKS_CACHE_TTL:
            return _jwks_cache

        jwks_url = _JWKS_URL_TEMPLATE.format(tenant_id=self._tenant_id)

        logger.info(
            "Actualizando claves JWKS desde Entra ID",
            extra={"jwks_url": jwks_url, "component": "JWTMiddleware"},
        )

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(jwks_url)
            resp.raise_for_status()
            _jwks_cache    = resp.json()
            _jwks_cache_ts = now

        return _jwks_cache


# ─────────────────────────────────────────────
# Dependencia FastAPI para obtener claims
# ─────────────────────────────────────────────
def get_current_user(request: Request) -> dict:
    """
    FastAPI dependency que retorna los claims del token validado.

    Uso en un endpoint:
        @router.post("/chat")
        async def chat(body: ChatRequest, user: dict = Depends(get_current_user)):
            user_id = user.get("oid")
    """
    claims = getattr(request.state, "token_claims", None)
    if claims is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No autenticado",
        )
    return claims
