"""
tests/stress/locustfile.py
───────────────────────────
Pruebas de estrés del agente conversacional usando Locust.

Configuración en config_agent.yaml → stress_testing

Ejecutar:
    locust -f tests/stress/locustfile.py \
           --host http://localhost:8000 \
           --users 50 --spawn-rate 5 \
           --run-time 5m --headless \
           --html reports/stress_report.html

Métricas objetivo (config → stress_testing.thresholds):
  - p95 latencia < 3000ms
  - tasa de error < 1%
"""

import random
import uuid

from locust import HttpUser, task, between, events
from src.core.logging_config import get_logger

logger = get_logger(__name__)

# Queries de prueba representativas del dominio
SAMPLE_QUERIES = [
    "¿Cuál es el procedimiento para solicitar vacaciones?",
    "¿Cómo puedo reportar un incidente de seguridad?",
    "Explícame la política de gastos de viaje",
    "¿Cuáles son los beneficios del plan de salud?",
    "¿Qué documentos necesito para la onboarding?",
    "Resumen de la política de trabajo remoto",
    "¿Cómo se calcula el bono anual?",
    "Procedimiento de aprobación de compras",
    "hola",
    "¿Cuál es el horario de atención de TI?",
]


class AgentUser(HttpUser):
    """Simula un usuario conversando con el agente."""
    wait_time = between(1, 3)  # segundos entre requests

    def on_start(self):
        """Crea una nueva sesión para este usuario virtual."""
        self.session_id = str(uuid.uuid4())
        self.token      = "test-token-placeholder"  # REEMPLAZAR con token real en CI

    @task(7)
    def chat_with_context(self):
        """Prueba principal: consulta que requiere retrieval."""
        query = random.choice(SAMPLE_QUERIES[:-1])  # excluye "hola"
        self._send_chat(query)

    @task(3)
    def chat_simple(self):
        """Prueba secundaria: consulta simple sin contexto."""
        self._send_chat("hola")

    def _send_chat(self, query: str):
        payload = {
            "query":      query,
            "session_id": self.session_id,
        }
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type":  "application/json",
            "X-Request-ID":  str(uuid.uuid4()),
        }
        with self.client.post(
            "/api/v1/chat",
            json     = payload,
            headers  = headers,
            name     = "/api/v1/chat",
            catch_response = True,
        ) as resp:
            if resp.status_code == 200:
                data = resp.json()
                if not data.get("response"):
                    resp.failure("Respuesta vacía del agente")
                else:
                    resp.success()
            elif resp.status_code == 401:
                resp.failure("No autenticado — revisar token de prueba")
            elif resp.status_code == 500:
                resp.failure(f"Error interno: {resp.text[:200]}")
            else:
                resp.failure(f"Status inesperado: {resp.status_code}")

    @task(1)
    def health_check(self):
        """Health check periódico durante la prueba."""
        self.client.get("/health", name="/health")


# ─────────────────────────────────────────────
# Eventos de reporte
# ─────────────────────────────────────────────
@events.test_stop.add_listener
def on_test_stop(environment, **kwargs):
    """Loguea resumen al finalizar la prueba."""
    stats = environment.stats.total
    logger.info(
        "Prueba de estrés finalizada",
        extra={
            "total_requests":   stats.num_requests,
            "failures":         stats.num_failures,
            "error_rate_pct":   round(stats.fail_ratio * 100, 2),
            "p50_ms":           stats.get_response_time_percentile(0.5),
            "p95_ms":           stats.get_response_time_percentile(0.95),
            "p99_ms":           stats.get_response_time_percentile(0.99),
            "avg_rps":          round(stats.current_rps, 2),
            "component":        "locust",
        },
    )
