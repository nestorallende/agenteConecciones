# Catálogo de Logs — Agente Conversacional (Etapa 5)

Documento de referencia para monitoreo en **Azure Application Insights**.  
Todos los logs se emiten en formato **JSON estructurado** (una línea por evento) con los campos base más los campos contextuales que se detallan por sección.

---

## Campos base — presentes en TODOS los logs

| Campo | Tipo | Descripción |
|---|---|---|
| `timestamp` | string ISO 8601 | Fecha y hora del evento |
| `level` | string | `INFO` / `WARNING` / `ERROR` / `DEBUG` |
| `logger` | string | Nombre del módulo Python que emite el log |
| `message` | string | Descripción del evento |
| `module` | string | Nombre del archivo `.py` |
| `function` | string | Función donde ocurre el evento |
| `line` | int | Número de línea en el código |
| `process` | int | PID del proceso |
| `thread` | int | ID del thread |

---

## Campos de contexto por request — inyectados automáticamente

Estos campos se establecen al inicio de cada request HTTP con `set_log_context()` y aparecen en **todos los logs** generados durante esa request, sin necesidad de pasarlos manualmente.

| Campo | Tipo | Origen | Descripción |
|---|---|---|---|
| `session_id` | string UUID | Generado o recibido en `POST /chat` | ID único de la sesión conversacional del usuario |
| `user_id` | string | Claim `oid` del token JWT de Entra ID | OID del usuario autenticado en Azure Active Directory |
| `request_id` | string UUID | Header `X-Request-ID` o generado | ID único del request HTTP para correlación |
| `path` | string | URL del endpoint | Ruta HTTP que se está procesando |

---

## 1. Startup y Ciclo de Vida de la Aplicación

**Archivo:** `src/api/main.py` — función `lifespan()`  
**Cuándo se emiten:** al arrancar y apagar el Container App.

| Mensaje | Nivel | Campos adicionales | Descripción |
|---|---|---|---|
| `Iniciando agente conversacional` | INFO | `config_path`, `environment` | Primer log al arrancar la app |
| `Configuración cargada` | INFO | `environment` | config_agent.yaml resuelto correctamente |
| `LLMClient inicializado` | INFO | `deployment_name` | Azure OpenAI listo |
| `AzureSearchClient inicializado` | INFO | `index_name`, `emb_deployment` | Conexión a AI Search establecida |
| `CosmosMemoryClient inicializado` | INFO | `database`, `container` | Cosmos DB listo, DB/container creados o reutilizados |
| `MCPClient inicializado` | INFO | `tools_count`, `server_url` | MCP Server conectado |
| `A2AClient inicializado` | INFO | `registered_agents`, `caller_id` | Protocol A2A listo con agentes registrados |
| `Grafo Langraph compilado` | INFO | — | Grafo de nodos listo para recibir requests |
| `Agente conversacional iniciado correctamente` | INFO | `environment` | Startup completado — app lista |
| `Error crítico durante startup — la app no pudo iniciar` | ERROR | `exc_info` (stacktrace) | Fallo en algún componente durante el inicio |
| `Apagando agente conversacional` | INFO | — | Señal de shutdown recibida |
| `CosmosMemoryClient cerrado` | INFO | — | Conexión a Cosmos cerrada limpiamente |
| `Agente apagado correctamente` | INFO | — | Shutdown completado |
| `JWTMiddleware registrado` | INFO | — | Middleware de autenticación montado |
| `No se pudo cargar config para JWTMiddleware en create_app` | WARNING | `exc_info` | Config no disponible al montar middleware (no fatal) |

---

## 2. Autenticación JWT — Middleware EntraID

**Archivo:** `src/core/security/jwt_middleware.py`  
**Cuándo se emiten:** en cada request HTTP entrante.

| Mensaje | Nivel | Campos adicionales | Descripción |
|---|---|---|---|
| `JWTMiddleware inicializado` | INFO | `tenant_id`, `audience`, `issuer`, `public_endpoints` | Configuración de seguridad cargada al arrancar |
| `Request sin token Bearer` | WARNING | `path`, `method` | Request sin header `Authorization: Bearer` |
| `Token expirado` | WARNING | `path` | El token JWT está vencido (`exp` en el pasado) |
| `Claims inválidos: {detalle}` | WARNING | `path` | Audience, issuer u otro claim no coincide |
| `Error al validar token JWT` | ERROR | `path`, `exc_info` | Error de firma o formato del token |
| `Error inesperado en validación JWT` | ERROR | `path`, `exc_info` | Error no controlado durante la validación |
| `Request autenticada` | INFO | `user_id`, `path`, `method` | Token válido — request permitida |
| `Actualizando claves JWKS desde Entra ID` | INFO | `jwks_url` | Renovación de claves públicas (cada hora) |

---

## 3. Endpoint Chat — Router Principal

**Archivo:** `src/api/routers/chat.py`  
**Cuándo se emiten:** en cada llamada a `POST /api/v1/chat`.

| Mensaje | Nivel | Campos adicionales | Descripción |
|---|---|---|---|
| `[chat] Request recibida` | INFO | `session_id`, `user_id`, `query_len` | Inicio del procesamiento de una query |
| `[chat] Historial recuperado` | INFO | `session_id`, `history_turns` | Mensajes previos obtenidos de Cosmos DB |
| `[chat] Errores durante ejecución del grafo` | WARNING | `session_id`, `errors` | El grafo completó pero hubo errores no fatales |
| `[chat] Respuesta generada exitosamente` | INFO | `session_id`, `response_len`, `sources_count`, `latency_ms`, `tokens` | Ciclo completo exitoso |
| `[chat] Error no controlado en endpoint /chat` | ERROR | `session_id`, `exc_info` | Excepción no manejada — HTTP 500 |
| `[chat] Solicitud de limpieza de sesión` | INFO | `session_id`, `user_id` | `DELETE /api/v1/sessions/{id}` recibido |

---

## 4. Health Check

**Archivo:** `src/api/routers/health.py`  
**Cuándo se emite:** en cada llamada a `GET /health`.

| Mensaje | Nivel | Campos adicionales | Descripción |
|---|---|---|---|
| `[health] Health check` | INFO | `status` (ok/degraded), `components` (dict por componente), `environment` | Estado de todos los componentes del agente |

---

## 5. Grafo Langraph — Nodos

**Archivo:** `src/agent/nodes/__init__.py`  
**Cuándo se emiten:** durante la ejecución del grafo en cada turno conversacional.

### Nodo: router
| Mensaje | Nivel | Campos adicionales | Descripción |
|---|---|---|---|
| `[node:router] Evaluando acción siguiente` | INFO | `session_id`, `query_len`, `iteration` | Inicio de la decisión de routing |
| `[node:router] Máximo de iteraciones alcanzado — forzando end` | WARNING | `session_id`, `iteration` | Se alcanzó el límite de iteraciones configurado |
| `[node:router] Acción decidida: {acción}` | INFO | `session_id`, `next_action`, `latency_ms` | Resultado de la decisión: retrieve / tool / generate / end |

### Nodo: retrieval
| Mensaje | Nivel | Campos adicionales | Descripción |
|---|---|---|---|
| `[node:retrieval] Iniciando búsqueda` | INFO | `session_id`, `query_len`, `top_k`, `search_type` | Comienza la búsqueda en AI Search |
| `[node:retrieval] Búsqueda completada` | INFO | `session_id`, `total_chunks`, `filtered_chunks`, `min_score`, `latency_ms` | Chunks recuperados y filtrados por score |
| `[node:retrieval] Error en búsqueda` | ERROR | `session_id`, `exc_info` | Fallo en la llamada a Azure AI Search |

### Nodo: context_builder
| Mensaje | Nivel | Campos adicionales | Descripción |
|---|---|---|---|
| `[node:context_builder] Construyendo contexto` | INFO | `session_id`, `chunks_count` | Inicio del formateo de chunks para el LLM |
| `[node:context_builder] Contexto construido` | INFO | `session_id`, `context_len`, `latency_ms` | Bloque de contexto listo para el LLM |

### Nodo: tool_executor
| Mensaje | Nivel | Campos adicionales | Descripción |
|---|---|---|---|
| `[node:tool_executor] Sin tools MCP disponibles — redirigiendo a retrieval` | INFO | `session_id` | No hay tools configuradas, flujo alternativo |

### Nodo: generate
| Mensaje | Nivel | Campos adicionales | Descripción |
|---|---|---|---|
| `[node:generate] Generando respuesta` | INFO | `session_id`, `context_len`, `messages_count` | Inicio de la llamada al LLM |
| `[node:generate] Respuesta generada` | INFO | `session_id`, `response_len`, `latency_ms` | Respuesta del LLM recibida |
| `[node:generate] Error al generar respuesta` | ERROR | `session_id`, `exc_info` | Fallo en la llamada al LLM |

---

## 6. LLM Client — Azure OpenAI

**Archivo:** `src/core/llm/llm_client.py`  
**Cuándo se emiten:** en cada llamada de chat completion.

| Mensaje | Nivel | Campos adicionales | Descripción |
|---|---|---|---|
| `LLMClient configurado` | INFO | `llm_key`, `deployment_name` | Cliente configurado al iniciar |
| `LLMClient inicializado` | INFO | `deployment` | Conexión a Azure OpenAI establecida |
| `LLMClient no inicializado — llama a initialize() primero` | ERROR | `session_id` | Se intentó usar el cliente sin inicializar |
| `[llm] Llamando a {deployment}` | INFO | `session_id`, `node`, `messages_count`, `temperature` | Inicio de la llamada al LLM |
| `[llm] Respuesta recibida` | INFO | `session_id`, `node`, `prompt_tokens`, `completion_tokens`, `total_tokens`, `latency_ms`, `response_len` | Respuesta del modelo recibida correctamente |
| `[llm] Error en llamada al LLM` | ERROR | `session_id`, `node`, `exc_info` | Fallo en la API de Azure OpenAI |

---

## 7. Azure AI Search — Búsqueda Semántica

**Archivo:** `src/core/search/azure_search.py`  
**Cuándo se emiten:** en cada búsqueda durante el nodo retrieval.

| Mensaje | Nivel | Campos adicionales | Descripción |
|---|---|---|---|
| `AzureSearchClient configurado` | INFO | `index_name`, `top_k`, `hybrid_search` | Configuración cargada al iniciar |
| `AzureSearchClient inicializado` | INFO | `endpoint`, `index_name`, `emb_deployment` | Conexión establecida |
| `AzureSearchClient no inicializado — llama a initialize() primero` | ERROR | `session_id` | Cliente usado sin inicializar |
| `[search] query recibida` | INFO | `session_id`, `query_len`, `top_k`, `search_type` | Inicio de la búsqueda |
| `[search] {n} chunks recuperados` | INFO | `session_id`, `results_count`, `top_score` | Resultados obtenidos del índice |
| `[latency] azure_search_query` | INFO | `session_id`, `operation`, `latency_ms`, `success` | Latencia de la llamada a AI Search |
| `[latency] embed_query` | INFO | `operation`, `latency_ms`, `success` | Latencia del embedding de la query |

---

## 8. Cosmos DB — Memoria Conversacional

**Archivo:** `src/core/memory/cosmos_memory.py`  
**Cuándo se emiten:** al inicializar, guardar y leer historial.

| Mensaje | Nivel | Campos adicionales | Descripción |
|---|---|---|---|
| `CosmosMemoryClient configurado` | INFO | `endpoint`, `database`, `container`, `partition_key`, `default_ttl` | Parámetros de Cosmos cargados |
| `Inicializando conexión a Cosmos DB` | INFO | `endpoint` | Intento de conexión al servicio |
| `Database '{nombre}' creado` | INFO | `database` | Base de datos nueva creada en Cosmos |
| `Database '{nombre}' ya existe — reutilizando` | INFO | `database` | Base de datos existente reutilizada |
| `Container '{nombre}' creado` | INFO | `container`, `partition_key`, `ttl` | Container nuevo creado |
| `Container '{nombre}' ya existe — reutilizando` | INFO | `container` | Container existente reutilizado |
| `CosmosMemoryClient no inicializado` | ERROR | `session_id` | Se intentó usar el cliente sin inicializar |
| `Mensaje guardado en Cosmos` | INFO | `doc_id`, `session_id`, `role` | Mensaje persistido en Cosmos |
| `[latency] cosmos_save_message` | INFO | `session_id`, `role`, `latency_ms`, `success` | Latencia de escritura en Cosmos |
| `Historial recuperado desde Cosmos` | INFO | `session_id`, `messages_count` | Mensajes del historial obtenidos |
| `[latency] cosmos_get_history` | INFO | `session_id`, `latency_ms`, `success` | Latencia de lectura del historial |
| `Conexión a Cosmos DB cerrada` | INFO | — | Cierre limpio al apagar la app |

---

## 9. MCP Client — Conexión al MCP Server

**Archivo:** `src/core/mcp/mcp_client.py`  
**Cuándo se emiten:** al inicializar y al invocar tools del MCP Server.

| Mensaje | Nivel | Campos adicionales | Descripción |
|---|---|---|---|
| `MCPClient configurado` | INFO | `server_url`, `auth_type`, `tools_predefined` | Parámetros cargados |
| `MCP Server URL no configurada — MCPClient deshabilitado` | WARNING | — | `mcp_client.server_url` vacío en config |
| `Tools MCP descubiertas: [lista]` | INFO | `tools_count` | Tools del servidor detectadas en startup |
| `No se pudieron descubrir tools del MCP Server — usando predefinidas` | WARNING | `server_url`, `predefined_tools`, `exc_info` | Error al descubrir tools, continúa con config |
| `[mcp] tool '{nombre}' ejecutada exitosamente` | INFO | `session_id`, `tool_name`, `latency_ms`, `attempt` | Invocación a tool completada |
| `[mcp] Intento {n}/{max} fallido para tool '{nombre}'` | WARNING | `session_id`, `tool_name`, `attempt`, `exc_info` | Fallo en un intento, se reintentará |
| `Renovando token de Managed Identity para MCP Server` | DEBUG | `resource` | Refresh del token de autenticación |

---

## 10. Agent-to-Agent (A2A)

**Archivo:** `src/core/a2a/a2a_client.py`  
**Cuándo se emiten:** al invocar o recibir tareas de otros agentes.

| Mensaje | Nivel | Campos adicionales | Descripción |
|---|---|---|---|
| `A2AClient inicializado` | INFO | `caller_id`, `agents_count`, `registered` | Lista de agentes destino registrados |
| `[a2a] Invocando tarea '{task}' en agente '{id}'` | INFO | `session_id`, `agent_id`, `task` | Inicio de llamada a otro agente |
| `[a2a] Tarea '{task}' completada en agente '{id}'` | INFO | `session_id`, `agent_id`, `task`, `success`, `latency_ms`, `attempt` | Respuesta recibida del agente destino |
| `[a2a] Intento {n}/{max} fallido para '{task}' en '{id}'` | WARNING | `session_id`, `agent_id`, `attempt`, `wait_s`, `exc_info` | Fallo con reintento pendiente |
| `[a2a] Agente '{id}' no respondió después de {n} intentos` | ERROR | `session_id`, `agent_id`, `task`, `latency_ms` | Todos los reintentos agotados |
| `[a2a] Token renovado para agente '{id}'` | DEBUG | `agent_id` | Refresh de token de Managed Identity |
| `[a2a] Agente '{id}' registrado dinámicamente` | INFO | `agent_id`, `url` | Nuevo agente registrado en runtime |
| `[a2a] Tarea recibida: '{task}' desde '{caller}'` | INFO | `task`, `caller_id`, `session_id` | Este agente recibió una tarea A2A entrante |
| `[a2a] Tarea '{task}' completada` | INFO | `task`, `latency_ms` | Tarea entrante procesada y respondida |

---

## 11. Telemetría — TelemetryClient

**Archivo:** `src/core/telemetry.py`  
**Cuándo se emiten:** disparados por cada componente vía `telemetry.track_*()`.

Estos son los **event_type** que aparecen como campo en el log para filtrar en Application Insights:

| `event_type` | Nivel | Campos de datos | Qué mide |
|---|---|---|---|
| `node_execution` | INFO / ERROR | `session_id`, `node`, `input_chars`, `output_chars`, `latency_ms`, `tokens_used`, `success`, `error` | Ejecución de cada nodo Langraph |
| `retrieval` | INFO | `session_id`, `query_length`, `results_count`, `top_score`, `latency_ms`, `search_type` | Calidad y velocidad de búsqueda |
| `llm_call` | INFO / ERROR | `session_id`, `node`, `model`, `prompt_tokens`, `completion_tokens`, `total_tokens`, `latency_ms`, `success`, `error` | Consumo de tokens y latencia del LLM |
| `session_lifecycle` | INFO / ERROR | `session_id`, `user_id`, `lifecycle` (start/end/error), `duration_ms`, `turns`, `error` | Duración y resultado de cada sesión |
| `mcp_tool_call` | INFO / ERROR | `session_id`, `tool_name`, `latency_ms`, `success`, `error` | Uso y rendimiento de tools MCP |
| `unhandled_error` | ERROR | `session_id`, `component`, `error_type`, `error_msg`, `exc_info` | Errores no controlados en cualquier componente |
| `latency` | INFO / ERROR | `operation`, `latency_ms`, `success` + campos extra del caller | Latencia de cualquier operación con `LatencyTracker` |

---

## 12. Settings — Carga de Configuración y Key Vault

**Archivo:** `src/core/settings.py`  
**Cuándo se emiten:** al arrancar la app o al resolver secretos puntualmente.

| Mensaje | Nivel | Campos adicionales | Descripción |
|---|---|---|---|
| `AZURE_KEY_VAULT_URL no configurada` | WARNING | — | Variable de entorno no definida — secretos no se resolverán |
| `SecretHelper conectado a Key Vault` | INFO | `vault_url` | Conexión al Key Vault establecida |
| `No se pudo conectar al Key Vault` | ERROR | `vault_url`, `exc_info` | Error de credenciales o red al conectar |
| `Key Vault no disponible — secreto '{nombre}' no resuelto` | WARNING | `secret` | Se intentó resolver un secreto sin cliente disponible |
| `Secreto '{nombre}' resuelto desde Key Vault` | DEBUG | `secret` | Secreto obtenido y cacheado en memoria |
| `Error al obtener secreto '{nombre}' desde Key Vault` | ERROR | `secret`, `exc_info` | El secreto no existe o no tiene permisos |
| `Archivo de config no encontrado: {ruta}` | ERROR | `config_path` | El archivo config_agent.yaml no existe en la ruta indicada |
| `Cargando configuración desde {ruta}` | INFO | `config_path` | Inicio de lectura del YAML |
| `Configuración cargada y resuelta correctamente` | INFO | `config_path`, `environment` | Todos los `${KV-*}` resueltos exitosamente |

---

## 13. Evaluación de Calidad — Foundry

**Archivo:** `tests/evaluation/eval_foundry.py`  
**Cuándo se emiten:** al correr el script de evaluación offline.

| Mensaje | Nivel | Campos adicionales | Descripción |
|---|---|---|---|
| `Iniciando evaluación de calidad del agente` | INFO | `dataset_size`, `agent_url` | Inicio del proceso de evaluación |
| `Evaluando muestra {n}/{total}` | INFO | `query` (primeros 60 chars) | Progreso por cada muestra del dataset |
| `Error al consultar el agente: {error}` | ERROR | `exc_info` | El agente no respondió para una muestra |
| `Error al evaluar métrica: {error}` | ERROR | `exc_info` | El LLM juez falló al puntuar una métrica |
| `Muestra {n}: ✓ PASS / ✗ FAIL` | INFO | `relevance`, `groundedness`, `coherence`, `fluency`, `latency_ms`, `pass` | Resultado por muestra con todas las métricas |
| `Reporte guardado en {ruta}` | INFO | `output_path` | Archivo de reporte JSON generado |

---

## 14. Smoke Test — Validación de Despliegue

**Archivo:** `scripts/smoke_test.py`  
**Cuándo se emiten:** al validar un despliegue específico en Container Apps.

| Mensaje | Nivel | Campos adicionales | Descripción |
|---|---|---|---|
| `Iniciando smoke test` | INFO | `app_url`, `expected_tag`, `timeout` | Inicio de la validación del despliegue |
| `[smoke] /health intento {n}: HTTP {code}` | INFO | `attempt`, `latency_ms` | Estado de cada intento de health check |
| `[smoke] /health HTTP {code}` | WARNING | `status_code` | Health check devolvió código inesperado |
| `[smoke] /health error: {error}` | WARNING | `exc_info` | Conexión rechazada o timeout |
| `[smoke] Reintentando en 10s... ({n}s restantes)` | INFO | — | Espera entre reintentos |
| `[smoke] /chat HTTP {code} \| {latencia}ms` | INFO | `latency_ms`, `has_response` | Resultado del check del endpoint de chat |

---

## Resumen por archivo

| Archivo | INFO | WARNING | ERROR | DEBUG | Campos de datos clave |
|---|:---:|:---:|:---:|:---:|---|
| `api/main.py` | 11 | 1 | 1 | 0 | `environment`, `config_path`, `registered_agents` |
| `api/routers/chat.py` | 4 | 1 | 1 | 0 | `session_id`, `user_id`, `query_len`, `latency_ms`, `tokens`, `sources_count` |
| `api/routers/health.py` | 1 | 0 | 0 | 0 | `status`, `components` |
| `core/security/jwt_middleware.py` | 3 | 3 | 2 | 0 | `user_id`, `path`, `tenant_id`, `audience` |
| `core/memory/cosmos_memory.py` | 9 | 0 | 2 | 0 | `session_id`, `doc_id`, `role`, `database`, `container`, `latency_ms` |
| `core/llm/llm_client.py` | 4 | 0 | 2 | 0 | `session_id`, `node`, `prompt_tokens`, `completion_tokens`, `total_tokens`, `latency_ms`, `deployment` |
| `core/search/azure_search.py` | 4 | 0 | 1 | 0 | `session_id`, `query_len`, `results_count`, `top_score`, `latency_ms` |
| `core/mcp/mcp_client.py` | 3 | 3 | 0 | 1 | `session_id`, `tool_name`, `latency_ms`, `attempt`, `server_url` |
| `core/a2a/a2a_client.py` | 6 | 2 | 1 | 1 | `session_id`, `agent_id`, `task`, `latency_ms`, `caller_id` |
| `core/settings.py` | 3 | 2 | 3 | 1 | `vault_url`, `secret`, `config_path`, `environment` |
| `agent/nodes/__init__.py` | 9 | 1 | 2 | 0 | `session_id`, `next_action`, `iteration`, `query_len`, `results_count`, `latency_ms` |
| `core/telemetry.py` | 6 | 0 | 1 | 0 | `session_id`, `node`, `event_type`, `latency_ms`, `tokens_used`, `success` |
| `tests/evaluation/eval_foundry.py` | 4 | 0 | 2 | 0 | `relevance`, `groundedness`, `coherence`, `fluency`, `latency_ms`, `pass` |
| `scripts/smoke_test.py` | 4 | 2 | 0 | 0 | `app_url`, `attempt`, `latency_ms`, `status_code` |
| **TOTAL** | **71** | **15** | **18** | **3** | |

---

## Consultas KQL recomendadas para Application Insights

```kql
// Latencia p50 / p95 / p99 del endpoint /chat por hora
traces
| where customDimensions.event_type == "session_lifecycle"
|   and customDimensions.lifecycle == "end"
| summarize
    p50 = percentile(todouble(customDimensions.duration_ms), 50),
    p95 = percentile(todouble(customDimensions.duration_ms), 95),
    p99 = percentile(todouble(customDimensions.duration_ms), 99)
  by bin(timestamp, 1h)
| order by timestamp desc
```

```kql
// Consumo de tokens por hora (costo LLM)
traces
| where customDimensions.event_type == "llm_call"
|   and customDimensions.success == "True"
| summarize
    total_tokens  = sum(toint(customDimensions.total_tokens)),
    prompt_tokens = sum(toint(customDimensions.prompt_tokens)),
    completion_tokens = sum(toint(customDimensions.completion_tokens)),
    llamadas = count()
  by bin(timestamp, 1h)
| order by timestamp desc
```

```kql
// Calidad de búsqueda: score promedio y chunks recuperados
traces
| where customDimensions.event_type == "retrieval"
| summarize
    avg_score   = avg(todouble(customDimensions.top_score)),
    avg_chunks  = avg(toint(customDimensions.results_count)),
    queries     = count()
  by bin(timestamp, 1h)
| order by timestamp desc
```

```kql
// Errores por componente en las últimas 24 horas
traces
| where customDimensions.event_type == "unhandled_error"
|   and timestamp > ago(24h)
| summarize count() by
    tostring(customDimensions.component),
    tostring(customDimensions.error_type)
| order by count_ desc
```

```kql
// Sesiones activas por usuario en los últimos 7 días
traces
| where customDimensions.event_type == "session_lifecycle"
|   and customDimensions.lifecycle == "start"
|   and timestamp > ago(7d)
| summarize sesiones = count() by
    tostring(customDimensions.user_id),
    bin(timestamp, 1d)
| order by timestamp desc
```

```kql
// Latencia por nodo del grafo Langraph
traces
| where customDimensions.event_type == "node_execution"
| summarize
    p95_ms = percentile(todouble(customDimensions.latency_ms), 95),
    llamadas = count(),
    errores  = countif(customDimensions.success == "False")
  by tostring(customDimensions.node)
| order by p95_ms desc
```

```kql
// Uso de tools MCP
traces
| where customDimensions.event_type == "mcp_tool_call"
| summarize
    llamadas  = count(),
    errores   = countif(customDimensions.success == "False"),
    avg_ms    = avg(todouble(customDimensions.latency_ms))
  by tostring(customDimensions.tool_name)
```
