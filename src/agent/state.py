"""
agent/state.py
──────────────
Definición del estado del grafo Langraph del agente conversacional.

El estado es el objeto que fluye entre todos los nodos del grafo.
Cada nodo recibe el estado, lo modifica y lo retorna.
Langraph gestiona la inmutabilidad y el merge automático.

Estructura del estado:
  - Datos de entrada: query, session_id, user_id
  - Historial: messages (OpenAI format)
  - Resultados intermedios: retrieved_chunks, context
  - Control de flujo: current_node, iteration_count, should_end
  - Observabilidad: node_traces (para telemetría)
"""

from typing import Annotated, Any, Optional
from typing_extensions import TypedDict
import operator


class AgentState(TypedDict):
    """
    Estado del agente conversacional en el grafo Langraph.

    Todos los campos son opcionales para permitir actualizaciones parciales.
    Los campos con Annotated[list, operator.add] se acumulan (append)
    en lugar de reemplazarse.
    """

    # ── Entrada de la request ────────────────────────────────────────────────
    # Query del usuario en el turno actual
    query:      str

    # ID único de la sesión conversacional
    session_id: str

    # OID del usuario autenticado (Entra ID)
    user_id:    str

    # ── Historial conversacional ─────────────────────────────────────────────
    # Mensajes en formato OpenAI [{"role": "user"|"assistant"|"system", "content": "..."}]
    # operator.add: los nodos agregan mensajes sin reemplazar el historial completo
    messages: Annotated[list[dict], operator.add]

    # ── Resultados intermedios ────────────────────────────────────────────────
    # Chunks recuperados de Azure AI Search (output del nodo retrieval)
    retrieved_chunks: list[dict]

    # Contexto formateado para el LLM (output del nodo context_builder)
    context: str

    # Respuesta final del agente (output del nodo generate)
    response: str

    # Resultado de herramientas MCP (output del nodo tool_executor)
    tool_results: Annotated[list[dict], operator.add]

    # ── Control de flujo ─────────────────────────────────────────────────────
    # Nombre del nodo actual en ejecución
    current_node: str

    # Contador de iteraciones del grafo (evita loops infinitos)
    iteration_count: int

    # Flag para terminar el grafo
    should_end: bool

    # Siguiente acción decidida por el router: "retrieve" | "tool" | "generate" | "end"
    next_action: str

    # ── Metadatos de observabilidad ───────────────────────────────────────────
    # Trazas de cada nodo para telemetría (acumuladas con operator.add)
    node_traces: Annotated[list[dict], operator.add]

    # Tokens consumidos en este turno
    total_tokens: int

    # Errores no fatales ocurridos durante el turno
    errors: Annotated[list[str], operator.add]


def initial_state(
    query:      str,
    session_id: str,
    user_id:    str = "",
    history:    Optional[list[dict]] = None,
) -> AgentState:
    """
    Crea el estado inicial para un nuevo turno conversacional.

    Args:
        query:      Pregunta o mensaje del usuario.
        session_id: ID de la sesión.
        user_id:    OID del usuario autenticado.
        history:    Historial previo de la sesión (desde Cosmos DB).

    Returns:
        AgentState con valores iniciales.
    """
    system_message = {
        "role": "system",
        "content": (
            "Eres un asistente inteligente con acceso a una base de conocimientos empresarial. "
            "Respondes de forma precisa, clara y citando las fuentes cuando corresponda. "
            "Si no encuentras información relevante, lo indicas explícitamente."
        ),
    }

    # Historial previo + mensaje del usuario actual
    prior = history or []
    current_user_message = {"role": "user", "content": query}

    return AgentState(
        query            = query,
        session_id       = session_id,
        user_id          = user_id,
        messages         = [system_message] + prior + [current_user_message],
        retrieved_chunks = [],
        context          = "",
        response         = "",
        tool_results     = [],
        current_node     = "router",
        iteration_count  = 0,
        should_end       = False,
        next_action      = "retrieve",
        node_traces      = [],
        total_tokens     = 0,
        errors           = [],
    )
