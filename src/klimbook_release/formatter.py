"""
formatter.py
==================================================

Descripcion:
-----------
Adapta el ReleaseNotes maestro a outputs por plataforma en paralelo usando
AsyncAnthropic + asyncio.gather + asyncio.Semaphore. Cada plataforma
destino tiene su propio template de prompt, limite de caracteres, tono y
reglas de idioma.

Proposito del modulo:
--------------------
- Ejecutar N formatters de plataforma en paralelo (acotado por
  max_parallel)
- Hacer cumplir los limites de caracteres por plataforma con smart
  truncation + retry
- Inyectar instrucciones de glosario para outputs en espanol (terminos
  de escalada)
- Devolver un dict de PlatformOutput indexado por nombre de plataforma

Contenido del modulo:
--------------------
1. format_all_platforms - Entry point async principal (asyncio.gather
                          sobre todas)
2. format_all_sync - Wrapper sincrono que envuelve asyncio.run para uso
                     desde el CLI
3. _format_single_platform - Coroutine por plataforma con retry loop
4. _build_formatter_prompt - Selecciona el template correcto por
                             plataforma
5. _smart_truncate - Truncado sensible al limite en el ultimo "." o
                     salto de linea
6. _get_async_client - Singleton del cliente AsyncAnthropic

Reglas de plataforma:
--------------------
- GitHub: markdown completo, sin limite de caracteres, agrega link
  "Full Changelog"
- Play Store en/es: <=500 chars, bullets simples, user-facing, sin markdown
- App Store: <=4000 chars, limpio y profesional
- Ko-fi: <=2000 chars, casual, personal, tono agradecido, firmado "— zxxz6"

Manejo de exceso de longitud:
----------------------------
- Exceso < 20% del limite: _smart_truncate en el ultimo punto/salto
- Exceso >= 20% del limite: retry con instruccion RETRY_TOO_LONG
- El retry loop baja la temperatura 0.15 por intento

Metadata:
----------
* Autor: zxxz6 (Bryan Violante Arriaga)
* Version: 1.0.0
* Licencia: MIT

Historial:
------------
Autor       Fecha           Descripcion
zxxz6       14/04/2026      Fix: _format_single_platform ahora acepta
                            y llena `metrics` con un StepMetrics por
                            llamada (incluye intentos fallidos, cache
                            hits y retries). Antes los 5 formatters
                            eran invisibles al PipelineMetrics global.
zxxz6       13/04/2026      _build_formatter_prompt inyecta ejemplos
                            few-shot (EXAMPLE_*) en el placeholder
                            {example}; para playstore se selecciona el
                            ejemplo ES o EN segun platform_config.language
zxxz6       13/04/2026      Integrado ResponseCache en el path async
                            (chequeo antes de cada messages.create,
                            store despues)
zxxz6       03/04/2026      Creacion
"""

from .models import ReleaseNotes, PlatformOutput, PipelineMetrics, StepMetrics
from .config import Config, PlatformConfig
from .cache import CachedResponse, get_cache
from .prompts import (
    FORMATTER_GITHUB, FORMATTER_PLAYSTORE, FORMATTER_APPSTORE,
    FORMATTER_KOFI, GLOSSARY_INSTRUCTION, NO_GLOSSARY,
    RETRY_TOO_LONG,
    EXAMPLE_GITHUB, EXAMPLE_PLAYSTORE_EN, EXAMPLE_PLAYSTORE_ES,
    EXAMPLE_APPSTORE, EXAMPLE_KOFI,
)

from anthropic import AsyncAnthropic
import asyncio
import time
import json
import logging

logger = logging.getLogger("formatter")

# Cliente async para las llamadas paralelas.
# Se usa el async client porque asyncio.gather necesita coroutines.
_async_client: AsyncAnthropic | None = None


def _get_async_client() -> AsyncAnthropic:
    """Singleton del cliente async."""
    global _async_client
    if _async_client is None:
        _async_client = AsyncAnthropic()
    return _async_client


# =====================================================================
# Prompt Builder
# =====================================================================
#
# Cada plataforma tiene un template de prompt diferente.
# Esta funcion selecciona el template correcto y sustituye
# las variables (notes, max_chars, language, glossary, version tags).
#

def _build_formatter_prompt(
    platform: str,
    notes_md: str,
    platform_config: PlatformConfig,
    config: Config,
    version_from: str = "",
    version_to: str = "",
) -> tuple[str, str]:
    """
    Construye el system prompt y user prompt para una plataforma.
    
    Args:
        platform: Nombre de la plataforma (ej: "playstore_es")
        notes_md: Release notes en markdown (del generator)
        platform_config: Config de esta plataforma
        config: Config global
        version_from: Tag anterior (para link de GitHub)
        version_to: Tag actual
        
    Returns:
        Tupla (system_prompt, user_prompt)
    """
    # System prompt generico para todos los formatters
    system = (
        f"You are a content formatter for {config.project_name}, "
        f"a social network for rock climbers."
    )

    # Instruccion de glosario (solo para plataformas en espanol)
    glossary_instruction = NO_GLOSSARY
    if platform_config.language == "es" and config.glossary:
        glossary_instruction = GLOSSARY_INSTRUCTION.format(
            terms=", ".join(config.glossary)
        )

    # Seleccionar el template correcto segun la plataforma.
    # Cada formatter recibe un ejemplo real (few-shot) que el LLM
    # usa como referencia de estilo — no de contenido.
    if platform == "github":
        user = FORMATTER_GITHUB.format(
            example=EXAMPLE_GITHUB,
            notes=notes_md,
            version_from=version_from,
            version_to=version_to,
        )

    elif platform.startswith("playstore"):
        # Usar el ejemplo en el idioma correcto para que tono y giros
        # lexicos calcen con el output esperado.
        example = (
            EXAMPLE_PLAYSTORE_ES
            if platform_config.language == "es"
            else EXAMPLE_PLAYSTORE_EN
        )
        user = FORMATTER_PLAYSTORE.format(
            example=example,
            notes=notes_md,
            max_chars=platform_config.max_chars or 500,
            language=platform_config.language,
            glossary_instruction=glossary_instruction,
        )

    elif platform == "appstore":
        user = FORMATTER_APPSTORE.format(
            example=EXAMPLE_APPSTORE,
            notes=notes_md,
            max_chars=platform_config.max_chars or 4000,
        )

    elif platform == "kofi":
        user = FORMATTER_KOFI.format(
            example=EXAMPLE_KOFI,
            notes=notes_md,
            max_chars=platform_config.max_chars or 2000,
        )

    else:
        # Plataforma desconocida: usar un prompt generico
        user = (
            f"Format these release notes for {platform}.\n"
            f"Language: {platform_config.language}\n"
            f"Max chars: {platform_config.max_chars or 'no limit'}\n\n"
            f"<notes>{notes_md}</notes>"
        )

    return system, user


# =====================================================================
# Single Platform Formatter
# =====================================================================

async def _format_single_platform(
    platform: str,
    notes_md: str,
    platform_config: PlatformConfig,
    config: Config,
    semaphore: asyncio.Semaphore,
    version_from: str = "",
    version_to: str = "",
    metrics: PipelineMetrics | None = None,
) -> PlatformOutput:
    """
    Formatea release notes para UNA plataforma.

    Incluye retry si el output excede el limite de caracteres.
    En cada retry, agrega instrucciones mas estrictas sobre la longitud.
    Cada llamada exitosa a la API (o cache hit) se registra en
    `metrics` si se pasa, para que PipelineMetrics refleje el costo
    total real de la corrida.
    
    Args:
        platform: Nombre de la plataforma
        notes_md: Markdown de las release notes
        platform_config: Config de esta plataforma
        config: Config global
        semaphore: Semaphore para limitar concurrencia
        version_from/to: Tags para el link de GitHub
        
    Returns:
        PlatformOutput validado
    """
    aclient = _get_async_client()
    system, user = _build_formatter_prompt(
        platform, notes_md, platform_config, config,
        version_from, version_to,
    )

    # Determinar temperature segun plataforma.
    # Ko-fi es casual (mas creativo), el resto es formal (mas consistente).
    if platform == "kofi":
        base_temp = config.formatter_casual_temp
    else:
        base_temp = config.formatter_formal_temp

    max_chars = platform_config.max_chars
    last_error = None

    for attempt in range(config.max_retries):
        # En retries, bajar la temperature
        temp = base_temp if attempt == 0 else max(0.0, base_temp - (attempt * 0.15))
        step_name = f"format:{platform} (attempt {attempt + 1})"

        # En retries por longitud excedida, agregar instruccion extra
        current_user = user
        if attempt > 0 and last_error and "chars" in str(last_error):
            current_user = user + "\n" + RETRY_TOO_LONG.format(
                actual_chars=str(last_error).split("=")[-1] if "=" in str(last_error) else "?",
                max_chars=max_chars or "?",
            )

        logger.info(
            f"  [{platform}] Intento {attempt + 1}/{config.max_retries} "
            f"(temp={temp})"
        )

        try:
            # Lookup al cache primero (no hay llamada API si hit)
            cache = get_cache()
            cached = cache.get(
                config.formatter_model, system, current_user,
                temp, 2000,
            )
            is_cache_hit = cached is not None
            if is_cache_hit:
                content = cached.text.strip()
                inp = cached.input_tokens
                out = cached.output_tokens
                cost = 0.0
                elapsed = 0.0
                logger.info(
                    f"  [{platform}] CACHE HIT | {len(content)} chars | "
                    f"tokens: in={inp} out={out}"
                )
            else:
                # Usar el semaphore para limitar concurrencia.
                # Si ya hay max_parallel requests en vuelo,
                # esta coroutine espera hasta que una termine.
                async with semaphore:
                    start = time.time()
                    response = await aclient.messages.create(
                        model=config.formatter_model,
                        system=system,
                        messages=[{"role": "user", "content": current_user}],
                        temperature=temp,
                        max_tokens=2000,
                    )
                    elapsed = time.time() - start

                content = response.content[0].text.strip()

                # Registrar metricas
                inp = response.usage.input_tokens
                out = response.usage.output_tokens
                cost = config.calculate_cost(config.formatter_model, inp, out)

                # Persistir respuesta cruda antes de truncado/validacion
                cache.set(
                    config.formatter_model, system, current_user,
                    temp, 2000,
                    CachedResponse(
                        text=content, input_tokens=inp, output_tokens=out,
                        model=config.formatter_model,
                    ),
                )

            logger.info(
                f"  [{platform}] {len(content)} chars | "
                f"tokens: in={inp} out={out} | "
                f"${cost:.6f} | {elapsed:.2f}s"
            )

            # Registrar StepMetrics en el agregador global.
            # Se registra aunque el output exceda el limite y requiera
            # retry — asi los tokens "desperdiciados" tambien cuentan
            # en el total reportado al final de la corrida.
            if metrics is not None:
                step_label = f"format:{platform}"
                if attempt > 0:
                    step_label += f" (retry {attempt})"
                if is_cache_hit:
                    step_label += " [cache]"
                metrics.add_step(StepMetrics(
                    step_name=step_label,
                    model=config.formatter_model,
                    input_tokens=inp,
                    output_tokens=out,
                    cost_usd=cost,
                    elapsed_seconds=round(elapsed, 2),
                    retries=attempt,
                ))

            # Verificar limite de caracteres.
            # Si excede, marcamos el error y reintentamos.
            if max_chars and len(content) > max_chars:
                last_error = ValueError(
                    f"{platform}: {len(content)} chars excede limite "
                    f"de {max_chars}. Diferencia={len(content) - max_chars}"
                )
                logger.warning(f"  [{platform}] Excede limite: {last_error}")

                # Si esta cerca del limite (< 20% exceso), intentar truncar
                # en vez de re-llamar a la API (ahorra tokens)
                excess_pct = (len(content) - max_chars) / max_chars
                if excess_pct < 0.2:
                    # Truncar en el ultimo punto o salto de linea
                    truncated = _smart_truncate(content, max_chars)
                    if truncated:
                        logger.info(f"  [{platform}] Truncado: {len(truncated)} chars")
                        content = truncated
                    else:
                        continue  # No se pudo truncar, retry completo
                else:
                    continue  # Muy largo, retry completo

            # Construir el PlatformOutput
            output = PlatformOutput(
                platform=platform,
                content=content,
                language=platform_config.language,
                max_chars=max_chars,
            )

            return output

        except Exception as e:
            last_error = e
            logger.warning(f"  [{platform}] Error: {type(e).__name__}: {e}")

    # Si todos los intentos fallaron, retornar con el ultimo contenido
    # que tengamos (mejor algo que nada)
    logger.error(f"  [{platform}] Todos los intentos fallaron: {last_error}")
    return PlatformOutput(
        platform=platform,
        content=f"[ERROR] No se pudo generar contenido para {platform}: {last_error}",
        language=platform_config.language,
        max_chars=max_chars,
    )


def _smart_truncate(text: str, max_chars: int) -> str | None:
    """
    Trunca texto inteligentemente en el ultimo punto o salto de linea
    que quepa dentro del limite.
    
    Retorna None si no puede truncar de forma limpia (ej: si el primer
    parrafo ya excede el limite).
    """
    if len(text) <= max_chars:
        return text

    # Buscar el ultimo punto dentro del limite
    truncated = text[:max_chars]
    last_period = truncated.rfind(".")
    last_newline = truncated.rfind("\n")

    # Usar el corte mas cercano al final
    cut_at = max(last_period, last_newline)

    if cut_at < max_chars * 0.5:
        # Si el corte dejaria menos de la mitad del contenido,
        # no truncar (mejor hacer retry completo)
        return None

    return text[:cut_at + 1].strip()


# =====================================================================
# Parallel Formatter (entry point)
# =====================================================================

async def format_all_platforms(
    notes: ReleaseNotes,
    config: Config,
    metrics: PipelineMetrics | None = None,
    version_from: str = "",
    version_to: str = "",
) -> dict[str, PlatformOutput]:
    """
    Formatea release notes para TODAS las plataformas en paralelo.
    
    Usa asyncio.gather para lanzar todas las tareas simultaneamente.
    Un Semaphore limita la concurrencia al valor de config.max_parallel
    para respetar rate limits de la API.
    
    Args:
        notes: Release notes generadas por generator
        config: Configuracion del proyecto
        metrics: Acumulador de metricas (opcional)
        version_from/to: Tags para el link de GitHub
        
    Returns:
        Dict con platform_name -> PlatformOutput
    """
    enabled = config.get_enabled_platforms()

    if not enabled:
        logger.warning("[Formatter] No hay plataformas habilitadas")
        return {}

    logger.info(
        f"[Formatter] Formateando para {len(enabled)} plataformas en paralelo "
        f"(max {config.max_parallel} concurrentes)"
    )

    # Semaphore limita cuantas requests van en paralelo.
    # Si max_parallel=3 y hay 5 plataformas, las primeras 3
    # se ejecutan inmediatamente y las otras 2 esperan.
    semaphore = asyncio.Semaphore(config.max_parallel)
    total_start = time.time()

    # Crear una tarea async por cada plataforma
    tasks = []
    platform_names = []
    for name, pcfg in enabled.items():
        task = _format_single_platform(
            platform=name,
            notes_md=notes.markdown,
            platform_config=pcfg,
            config=config,
            semaphore=semaphore,
            version_from=version_from,
            version_to=version_to,
            metrics=metrics,
        )
        tasks.append(task)
        platform_names.append(name)

    # Ejecutar todas en paralelo.
    # return_exceptions=True hace que si una tarea falla,
    # no cancele las demas. El error se retorna como resultado.
    results = await asyncio.gather(*tasks, return_exceptions=True)

    total_elapsed = time.time() - total_start

    # Procesar resultados
    outputs = {}
    successes = 0
    failures = 0

    for name, result in zip(platform_names, results):
        if isinstance(result, Exception):
            failures += 1
            logger.error(f"  [{name}] Fallo: {result}")
            outputs[name] = PlatformOutput(
                platform=name,
                content=f"[ERROR] {result}",
                language=enabled[name].language,
                max_chars=enabled[name].max_chars,
            )
        else:
            successes += 1
            outputs[name] = result

    logger.info(
        f"[Formatter] Completado en {total_elapsed:.2f}s | "
        f"{successes} OK, {failures} fallaron"
    )

    return outputs


def format_all_sync(
    notes: ReleaseNotes,
    config: Config,
    metrics: PipelineMetrics | None = None,
    version_from: str = "",
    version_to: str = "",
) -> dict[str, PlatformOutput]:
    """
    Wrapper sincrono de format_all_platforms.
    
    Usa asyncio.run() para ejecutar el formatter async
    desde contextos sincronos (como el CLI).
    """
    return asyncio.run(
        format_all_platforms(notes, config, metrics, version_from, version_to)
    )
