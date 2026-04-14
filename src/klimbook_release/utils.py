"""
utils.py
==================================================

Descripcion:
-----------
Utilidades compartidas del pipeline: singletons del cliente de la API de
Anthropic, un wrapper alrededor de messages.create que registra metricas,
un decorator generico de retry y helpers de consola para imprimir
resultados y totales.

Proposito del modulo:
--------------------
- Proveer un cliente Anthropic y uno AsyncAnthropic para todo el proceso
- Centralizar la logica de llamada al LLM (construccion de messages,
  prefill, conteo de costo)
- Ofrecer un decorator reutilizable de retry que baja la temperatura en
  cada intento
- Imprimir de forma bonita el output por paso y los resumenes de metricas
  del pipeline

Contenido del modulo:
--------------------
1. get_sync_client - Singleton del cliente Anthropic sincrono
2. get_async_client - Singleton del cliente AsyncAnthropic
3. call_llm - Wrapper de messages.create: construye messages, mide tiempo,
              calcula costo via Config.calculate_cost, registra
              StepMetrics
4. retry_on_error - Decorator que reintenta en JSONDecodeError/ValueError
                    recorriendo una lista de temperaturas
5. print_step_result - Helper de consola para previews truncados
6. print_metrics_summary - Totales del pipeline + desglose por paso

Metadata:
----------
* Autor: zxxz6 (Bryan Violante Arriaga)
* Version: 1.0.0
* Licencia: MIT

Historial:
------------
Autor       Fecha           Descripcion
zxxz6       13/04/2026      Integrado ResponseCache en call_llm (los cache
                            hits saltan la API y registran costo $0)
zxxz6       03/04/2026      Creacion
"""

from anthropic import Anthropic, AsyncAnthropic
from .models import StepMetrics, PipelineMetrics
from .config import Config
from .cache import CachedResponse, get_cache
import time
import logging
import json
import functools

logger = logging.getLogger("utils")


# =====================================================================
# API Clients
# =====================================================================
#
# Creamos los clients una sola vez y los reutilizamos.
# El sync client se usa para el CLI (que corre con asyncio.run).
# El async client se usa internamente para paralelismo.
#

_sync_client: Anthropic | None = None
_async_client: AsyncAnthropic | None = None


def get_sync_client() -> Anthropic:
    """Retorna el client sincrono de Anthropic (singleton)."""
    global _sync_client
    if _sync_client is None:
        _sync_client = Anthropic()
    return _sync_client


def get_async_client() -> AsyncAnthropic:
    """Retorna el client asincrono de Anthropic (singleton)."""
    global _async_client
    if _async_client is None:
        _async_client = AsyncAnthropic()
    return _async_client


# =====================================================================
# LLM Call Helper
# =====================================================================

def call_llm(
    model: str,
    system: str,
    prompt: str,
    temperature: float,
    max_tokens: int = 2000,
    prefill: str = "",
    config: Config | None = None,
    step_name: str = "",
    metrics: PipelineMetrics | None = None,
) -> tuple[str, StepMetrics]:
    """
    Wrapper para llamadas a Claude con tracking de metricas.
    
    Centraliza la logica de:
    - Construir messages (con prefill opcional)
    - Llamar a la API
    - Registrar tokens y costo
    - Retornar el texto + metricas
    
    Args:
        model: ID del modelo (ej: "claude-haiku-4-5-20251001")
        system: System prompt
        prompt: User prompt
        temperature: Temperature para la generacion
        max_tokens: Maximo de tokens a generar
        prefill: Texto para poner en el turno de assistant (fuerza formato)
        config: Configuracion (para calcular costos)
        step_name: Nombre del paso (para logging)
        metrics: PipelineMetrics donde acumular las metricas
        
    Returns:
        Tupla (texto_respuesta, StepMetrics)
    """
    client = get_sync_client()
    cache = get_cache()

    # ---- Cache lookup ----
    # Mismos (model, system, prompt, temperature, max_tokens, prefill) -> misma
    # respuesta. Cuando esta habilitado saltamos la API por completo y
    # retornamos los tokens/texto guardados; el costo se registra como 0 para
    # que las metricas reflejen que no hubo gasto real.
    cached = cache.get(model, system, prompt, temperature, max_tokens, prefill)
    if cached is not None:
        text = cached.text
        inp = cached.input_tokens
        out = cached.output_tokens
        elapsed = 0.0
        step_metrics = StepMetrics(
            step_name=step_name or "unknown",
            model=model,
            input_tokens=inp,
            output_tokens=out,
            cost_usd=0.0,
            elapsed_seconds=0.0,
        )
        if metrics:
            metrics.add_step(step_metrics)
        logger.info(
            f"  [{step_name}] CACHE HIT | tokens: in={inp} out={out} | $0.00 | 0.00s"
        )
        return text, step_metrics

    # Construir messages
    messages = [{"role": "user", "content": prompt}]
    if prefill:
        messages.append({"role": "assistant", "content": prefill})

    # Llamar a la API
    start = time.time()
    response = client.messages.create(
        model=model,
        system=system,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    elapsed = time.time() - start

    # Extraer texto (concatenando prefill si lo hubo)
    text = response.content[0].text
    if prefill:
        text = prefill + text

    # Calcular costo
    inp = response.usage.input_tokens
    out = response.usage.output_tokens

    # Persistir en cache para la proxima iteracion (no-op si esta deshabilitado)
    cache.set(
        model, system, prompt, temperature, max_tokens,
        CachedResponse(text=text, input_tokens=inp, output_tokens=out, model=model),
        prefill,
    )
    if config:
        cost = config.calculate_cost(model, inp, out)
    else:
        # Default: pricing de Sonnet
        cost = (inp / 1e6) * 3.0 + (out / 1e6) * 15.0

    # Crear metricas del paso
    step_metrics = StepMetrics(
        step_name=step_name or "unknown",
        model=model,
        input_tokens=inp,
        output_tokens=out,
        cost_usd=cost,
        elapsed_seconds=round(elapsed, 2),
    )

    # Registrar en el acumulador global si existe
    if metrics:
        metrics.add_step(step_metrics)

    # Log
    logger.info(
        f"  [{step_name}] tokens: in={inp} out={out} | "
        f"${cost:.6f} | {elapsed:.2f}s | model={model}"
    )

    return text, step_metrics


# =====================================================================
# Retry Decorator
# =====================================================================

def retry_on_error(
    max_retries: int = 3,
    temperatures: list[float] | None = None,
    retry_prompt_extra: str = "",
):
    """
    Decorator que reintenta una funcion si falla con ValidationError
    o JSONDecodeError, bajando la temperature en cada intento.
    
    Uso:
        @retry_on_error(max_retries=3)
        def step_classify(commits, temperature=0.3):
            ...
    
    Args:
        max_retries: Numero maximo de intentos
        temperatures: Lista de temperatures por intento
        retry_prompt_extra: Texto adicional para agregar al prompt en retries
    """
    if temperatures is None:
        temperatures = [0.3, 0.1, 0.0]

    while len(temperatures) < max_retries:
        temperatures.append(0.0)

    def decorator(func):
        """Envuelve la funcion objetivo con logica de retry + bajada de temperature."""
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            """Invoca la funcion envuelta, reintentando en errores de JSON/validacion."""
            last_error = None

            for attempt in range(max_retries):
                temp = temperatures[attempt]
                try:
                    logger.info(
                        f"  [{func.__name__}] Intento {attempt + 1}/{max_retries} "
                        f"(temp={temp})"
                    )
                    kwargs["temperature"] = temp
                    kwargs["retry_count"] = attempt

                    result = func(*args, **kwargs)
                    logger.info(f"  [{func.__name__}] OK en intento {attempt + 1}")
                    return result

                except (json.JSONDecodeError, ValueError, Exception) as e:
                    last_error = e
                    logger.warning(
                        f"  [{func.__name__}] Intento {attempt + 1} fallo: "
                        f"{type(e).__name__}: {str(e)[:200]}"
                    )

                    if attempt == max_retries - 1:
                        logger.error(f"  [{func.__name__}] Todos los intentos fallaron")
                        raise

            raise last_error

        return wrapper
    return decorator


# =====================================================================
# Output Helpers
# =====================================================================

def print_step_result(step_name: str, content: str, max_preview: int = 300):
    """Imprime el resultado de un paso del pipeline."""
    print(f"\n  --- {step_name} ---")
    if len(content) > max_preview:
        print(f"  {content[:max_preview]}...")
        print(f"  ({len(content)} chars total)")
    else:
        print(f"  {content}")


def print_metrics_summary(metrics: PipelineMetrics):
    """Imprime un resumen de metricas del pipeline."""
    s = metrics.summary()
    print(f"\n{'='*60}")
    print(f"  Pipeline Metrics")
    print(f"{'='*60}")
    print(f"  Steps:          {s['total_steps']}")
    print(f"  Input tokens:   {s['total_input_tokens']:,}")
    print(f"  Output tokens:  {s['total_output_tokens']:,}")
    print(f"  Total tokens:   {s['total_tokens']:,}")
    print(f"  Cost:           ${s['total_cost_usd']:.6f}")
    print(f"  Time:           {s['total_elapsed_seconds']:.2f}s")
    print(f"{'='*60}")

    if metrics.steps:
        print(f"\n  Step breakdown:")
        for step in metrics.steps:
            status = "OK" if step.success else "FAIL"
            retry_info = f" (retry {step.retries})" if step.retries > 0 else ""
            print(
                f"    [{status}] {step.step_name}: "
                f"in={step.input_tokens} out={step.output_tokens} "
                f"${step.cost_usd:.6f} {step.elapsed_seconds:.2f}s"
                f"{retry_info}"
            )
