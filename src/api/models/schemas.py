"""
api/models/schemas.py
──────────────────────
Modelos Pydantic para request y response de la API del agente.
"""

from typing import Any, Optional
from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    """Request para el endpoint POST /api/v1/chat."""
    query:      str  = Field(..., min_length=1, max_length=4000, description="Mensaje del usuario")
    session_id: Optional[str] = Field(None, description="ID de sesión. Si es None se crea uno nuevo.")


class SourceReference(BaseModel):
    """Referencia a un chunk fuente usado en la respuesta."""
    source: str   = Field(..., description="Nombre del documento fuente")
    score:  float = Field(..., description="Score de relevancia del chunk")
    type:   str   = Field("", description="Tipo de documento: pdf, docx, etc.")


class ChatResponse(BaseModel):
    """Response del endpoint POST /api/v1/chat."""
    session_id: str              = Field(..., description="ID de la sesión")
    response:   str              = Field(..., description="Respuesta del agente")
    sources:    list[SourceReference] = Field(default_factory=list, description="Fuentes utilizadas")
    tokens_used: int             = Field(0,  description="Tokens consumidos en el turno")
    latency_ms:  float           = Field(0.0, description="Latencia total del turno en ms")


class HealthResponse(BaseModel):
    """Response del endpoint GET /health."""
    status:      str  = Field(..., description="ok | degraded | down")
    environment: str  = Field("", description="Ambiente de despliegue")
    components:  dict = Field(default_factory=dict, description="Estado por componente")
