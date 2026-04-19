"""
generator.py
==================================================

Descripcion:
-----------
Genera las release notes en markdown a partir de commits clasificados
usando Claude Sonnet. Es el segundo paso LLM y produce el contenido
largo canonico que el formatter luego adapta por plataforma.

Proposito del modulo:
--------------------
- Construir un prompt con nombre del proyecto, version, fecha y cambios
  agrupados
- Llamar a Sonnet (T=0.3) para escribir las notas en un formato
  estructurado
- Validar que el output tenga markdown real (>=100 chars, al menos un
  header)
- Devolver un modelo ReleaseNotes con commit_count y conteo de categorias
  por tipo

Contenido del modulo:
--------------------
1. generate_notes - Entry point principal; construye el prompt, llama a
                    Sonnet, valida
2. _today - Helper que devuelve la fecha de hoy en formato
            "Month DD, YYYY"

Flujo:
------
1. Si entries esta vacio, devolver un ReleaseNotes placeholder sin tocar
   la API
2. Agrupar entries por tipo via classify_to_summary() como contexto
3. Formatear GENERATOR_SYSTEM y GENERATOR_TASK con
   project_name/version/date
4. Llamar a Sonnet; si falla inyectar RETRY_VALIDATION_FAILED con el error
5. Validar longitud del markdown y presencia de header antes de devolver

Metadata:
----------
* Autor: zxxz6 (Bryan Violante Arriaga)
* Version: 1.0.0
* Licencia: MIT

Historial:
------------
Autor       Fecha           Descripcion
zxxz6       18/04/2026      Inc max_tokens a 16K para modelos de ollama
zxxz6       13/04/2026      Agregado parametro prior_context para inyectar
                            el detailed changelog del README al prompt
                            via el placeholder {prior_context}
zxxz6       03/04/2026      Creacion
"""

from .models import CommitEntry, ReleaseNotes, PipelineMetrics
from .config import Config
from .prompts import GENERATOR_SYSTEM, GENERATOR_TASK, RETRY_VALIDATION_FAILED
from .classifier import classify_to_summary
from .utils import call_llm

from datetime import datetime
import logging

logger = logging.getLogger("generator")


def generate_notes(
    entries: list[CommitEntry],
    version: str,
    config: Config,
    metrics: PipelineMetrics | None = None,
    date: str | None = None,
    prior_context: str = "",
) -> ReleaseNotes:
    """
    Genera release notes en markdown a partir de commits clasificados.

    Args:
        entries: Commits clasificados por el classifier
        version: Version del release (ej: "v2.9.0")
        config: Configuracion del proyecto
        metrics: Acumulador de metricas (opcional)
        date: Fecha del release (default: hoy). Formato: "April 3, 2026"
        prior_context: Contexto markdown adicional (ej. detailed changelog
                       del README) que se inyecta en el prompt antes de
                       los commits clasificados. Si es vacio, no se
                       agrega al prompt.

    Returns:
        ReleaseNotes con markdown, metadata, y conteo por categoria

    Raises:
        RuntimeError: Si todos los intentos fallan
    """
    if not entries:
        logger.warning("[Generator] No hay entries para generar notas")
        return ReleaseNotes(
            version=version,
            date=date or _today(),
            markdown=f"# {config.project_name} {version}\n\nNo changes in this release.",
            commit_count=0,
            categories={},
        )

    # Fecha del release
    release_date = date or _today()

    # Preparar el resumen de cambios organizado por tipo.
    # Esto es lo que Claude recibe como contexto para escribir las notas.
    changes_summary = classify_to_summary(entries)

    # Contar categorias para metadata
    categories = {}
    for entry in entries:
        categories[entry.type] = categories.get(entry.type, 0) + 1

    logger.info(
        f"[Generator] Generando notas para {config.project_name} {version} | "
        f"{len(entries)} cambios | {categories}"
    )

    # Construir prompts sustituyendo variables.
    # El system prompt tiene {project_name} que se sustituye con el nombre del proyecto.
    # El task prompt tiene {project_name}, {version}, {date}, {prior_context} y {changes}.
    system = GENERATOR_SYSTEM.format(project_name=config.project_name)

    # prior_context se envuelve en saltos de linea solo si tiene contenido,
    # asi el prompt queda limpio cuando no hay changelog previo.
    context_block = f"\n{prior_context.strip()}\n" if prior_context.strip() else ""

    prompt = GENERATOR_TASK.format(
        project_name=config.project_name,
        version=version,
        date=release_date,
        prior_context=context_block,
        changes=changes_summary,
    )

    # Retry loop
    temperatures = config.retry_temperatures.copy()
    while len(temperatures) < config.max_retries:
        temperatures.append(0.0)

    last_error = None

    for attempt in range(config.max_retries):
        temp = temperatures[attempt]
        step_name = f"generate (attempt {attempt + 1})"

        logger.info(
            f"[Generator] Intento {attempt + 1}/{config.max_retries} "
            f"(temp={temp})"
        )

        # En retries, agregar instruccion sobre el error anterior
        current_prompt = prompt
        if attempt > 0 and last_error:
            current_prompt = prompt + "\n" + RETRY_VALIDATION_FAILED.format(
                error=str(last_error)[:300]
            )

        try:
            raw_text, step_metrics = call_llm(
                model=config.generator_model,
                system=system,
                prompt=current_prompt,
                temperature=temp,
                max_tokens=16000,
                config=config,
                step_name=step_name,
                metrics=metrics,
            )

            # Validar que el output tenga estructura de markdown.
            # No validamos la estructura exacta porque Claude puede
            # variar el formato ligeramente, pero debe tener al menos
            # un header y algo de contenido.
            markdown = raw_text.strip()

            if len(markdown) < 100:
                raise ValueError(
                    f"Markdown muy corto: {len(markdown)} chars (minimo 100)"
                )

            if "#" not in markdown:
                raise ValueError(
                    "El markdown no contiene headers (#). "
                    "Se esperaba al menos un header."
                )

            # Construir el resultado
            result = ReleaseNotes(
                version=version,
                date=release_date,
                markdown=markdown,
                commit_count=len(entries),
                categories=categories,
            )

            logger.info(
                f"[Generator] OK: {len(markdown)} chars | "
                f"{len(entries)} commits -> notas generadas"
            )
            return result

        except ValueError as e:
            last_error = e
            logger.warning(f"[Generator] Validacion fallo: {e}")
            if metrics and metrics.steps:
                metrics.steps[-1].success = False
                metrics.steps[-1].error = str(e)

        except Exception as e:
            last_error = e
            logger.error(f"[Generator] Error: {type(e).__name__}: {e}")
            if metrics and metrics.steps:
                metrics.steps[-1].success = False
                metrics.steps[-1].error = str(e)

    raise RuntimeError(
        f"Generacion fallo despues de {config.max_retries} intentos. "
        f"Ultimo error: {last_error}"
    )


def _today() -> str:
    """Retorna la fecha de hoy en formato legible."""
    return datetime.now().strftime("%B %d, %Y")
