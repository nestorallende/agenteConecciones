"""
agent/graph.py
──────────────
Construcción del grafo Langraph del agente conversacional.

Grafo de decisión:
                    ┌─────────────────────────────┐
                    │            START             │
                    └──────────────┬──────────────┘
                                   ▼
                              [ router ]
                          ┌────────┼────────┐
                          ▼        ▼        ▼
                      retrieve   tool    generate
                          │        │        │
                          ▼        │        │
                   [context_builder]        │
                          │        │        │
                          └────────┴────────┘
                                   ▼
                              [ generate ]
                                   ▼
                                  END

Condicionales:
  - router → "retrieve"      → retrieval → context_builder → generate → END
  - router → "tool"          → tool_executor → generate → END
  - router → "generate"      → generate → END
  - router → "end"           → END
  - any node error           → error_handler → END
"""

from langgraph.graph import StateGraph, END

from src.agent.state import AgentState
from src.agent.nodes import (
    make_router_node,
    make_retrieval_node,
    make_context_builder_node,
    make_tool_executor_node,
    make_generate_node,
)
from src.core.logging_config import get_logger
from src.core.telemetry import TelemetryClient

logger = get_logger(__name__)


def build_agent_graph(
    config:        dict,
    llm_client:    object,
    search_client: object,
    mcp_client:    object,
    telemetry:     TelemetryClient,
) -> StateGraph:
    """
    Construye y compila el grafo Langraph del agente.

    Args:
        config:        Configuración completa resuelta.
        llm_client:    Instancia de LLMClient.
        search_client: Instancia de AzureSearchClient.
        mcp_client:    Instancia de MCPClient.
        telemetry:     Instancia de TelemetryClient.

    Returns:
        CompiledGraph listo para invocar con .ainvoke(state).
    """
    logger.info(
        "Construyendo grafo Langraph del agente",
        extra={"component": "graph_builder"},
    )

    # ── Crear nodos ──────────────────────────────────────────────────────────
    router_node          = make_router_node(config, telemetry)
    retrieval_node       = make_retrieval_node(search_client, config, telemetry)
    context_builder_node = make_context_builder_node(search_client, config, telemetry)
    tool_executor_node   = make_tool_executor_node(mcp_client, config, telemetry)
    generate_node        = make_generate_node(llm_client, config, telemetry)

    # ── Construir grafo ──────────────────────────────────────────────────────
    graph = StateGraph(AgentState)

    # Registrar nodos
    graph.add_node("router",          router_node)
    graph.add_node("retrieval",       retrieval_node)
    graph.add_node("context_builder", context_builder_node)
    graph.add_node("tool_executor",   tool_executor_node)
    graph.add_node("generate",        generate_node)

    # Nodo de inicio
    graph.set_entry_point("router")

    # ── Aristas condicionales desde router ──────────────────────────────────
    graph.add_conditional_edges(
        "router",
        _route_decision,
        {
            "retrieve": "retrieval",
            "tool":     "tool_executor",
            "generate": "generate",
            "end":      END,
        },
    )

    # ── Aristas fijas ────────────────────────────────────────────────────────
    graph.add_edge("retrieval",       "context_builder")
    graph.add_edge("context_builder", "generate")
    graph.add_edge("tool_executor",   "generate")
    graph.add_edge("generate",        END)

    # ── Compilar ─────────────────────────────────────────────────────────────
    compiled = graph.compile()

    logger.info(
        "Grafo Langraph compilado exitosamente",
        extra={"component": "graph_builder"},
    )
    return compiled


# ─────────────────────────────────────────────
# Función de routing para el condicional
# ─────────────────────────────────────────────

def _route_decision(state: AgentState) -> str:
    """
    Retorna la clave de la arista a seguir desde el router.
    Langraph usa este valor para seleccionar el nodo siguiente.
    """
    next_action = state.get("next_action", "retrieve")
    logger.debug(
        f"[graph] Routing → {next_action}",
        extra={
            "session_id":  state.get("session_id"),
            "next_action": next_action,
            "component":   "graph_router",
        },
    )
    return next_action
