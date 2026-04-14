"""
classifier.py
==================================================

Descripcion:
-----------
Clasifica commits crudos de Git en categorias tipadas usando Claude Haiku.
Es el primer paso LLM del pipeline y el mas barato: Haiku con temperatura 0
da una clasificacion consistente y deterministica.

Proposito del modulo:
--------------------
- Convertir una lista de RawCommit en una lista de CommitEntry validados
- Inferir type, description, affected_service y el flag breaking-change
- Recuperarse de JSON invalido o items invalidos via retry con temperatura
  mas baja
- Proveer un helper que agrupa las entries clasificadas para el generator

Contenido del modulo:
--------------------
1. classify_commits - Entry point principal; llama a Haiku y valida el
                      output
2. classify_to_summary - Agrupa la lista de CommitEntry en secciones
                         markdown organizadas por tipo (para el prompt
                         del generator)

Flujo:
------
1. Convertir la lista de RawCommit a texto plano (formato git log --oneline)
2. Enviar a Claude con el system prompt del classifier y prefill "["
3. Parsear el JSON array; validar cada elemento como CommitEntry
4. Saltar items que fallen Pydantic individualmente; abortar solo si
   ninguno pasa
5. Si hay fallo de JSON/validacion, reintentar con temperatura mas baja
   e instruccion extra RETRY_JSON_INVALID

Metadata:
----------
* Autor: zxxz6 (Bryan Violante Arriaga)
* Version: 1.0.0
* Licencia: MIT

Historial:
------------
Autor       Fecha           Descripcion
zxxz6       03/04/2026      Creacion
"""

from .models import RawCommit, CommitEntry, PipelineMetrics
from .config import Config
from .prompts import CLASSIFIER_SYSTEM, CLASSIFIER_TASK, RETRY_JSON_INVALID
from .utils import call_llm
from .git_reader import commits_to_text

from pydantic import ValidationError
import json
import logging

logger = logging.getLogger("classifier")


def classify_commits(
    commits: list[RawCommit],
    config: Config,
    metrics: PipelineMetrics | None = None,
) -> list[CommitEntry]:
    """
    Clasifica una lista de commits crudos en categorias.
    
    Maneja el retry loop internamente: si Claude devuelve JSON
    invalido o los datos no pasan Pydantic, reintenta con
    temperature mas baja.
    
    Args:
        commits: Commits crudos de git_reader
        config: Configuracion del proyecto
        metrics: Acumulador de metricas (opcional)
        
    Returns:
        Lista de CommitEntry validados
        
    Raises:
        RuntimeError: Si todos los intentos fallan
    """
    if not commits:
        logger.warning("[Classifier] No hay commits para clasificar")
        return []

    # Convertir a texto plano (formato: "hash mensaje" por linea)
    commits_text = commits_to_text(commits)
    logger.info(f"[Classifier] Clasificando {len(commits)} commits")

    # Construir el prompt con los commits
    prompt = CLASSIFIER_TASK.format(commits=commits_text)

    # Preparar temperatures para cada intento
    temperatures = config.retry_temperatures.copy()
    while len(temperatures) < config.max_retries:
        temperatures.append(0.0)

    last_error = None

    for attempt in range(config.max_retries):
        temp = temperatures[attempt]
        step_name = f"classify (attempt {attempt + 1})"

        logger.info(
            f"[Classifier] Intento {attempt + 1}/{config.max_retries} "
            f"(temp={temp})"
        )

        # En retries, agregar instruccion extra para que Claude
        # sepa que su respuesta anterior fue invalida
        current_prompt = prompt
        if attempt > 0:
            current_prompt = prompt + "\n" + RETRY_JSON_INVALID

        try:
            # Llamar a Claude con prefill "[" para forzar JSON array.
            # call_llm concatena el prefill con la respuesta automaticamente,
            # asi que raw_text ya empieza con "[".
            raw_text, step_metrics = call_llm(
                model=config.classifier_model,
                system=CLASSIFIER_SYSTEM,
                prompt=current_prompt,
                temperature=temp,
                max_tokens=3000,
                prefill="[",
                config=config,
                step_name=step_name,
                metrics=metrics,
            )

            # Parsear JSON
            parsed = json.loads(raw_text)

            if not isinstance(parsed, list):
                raise ValueError(
                    f"Se esperaba JSON array, se recibio {type(parsed).__name__}"
                )

            # Validar cada entry con Pydantic.
            # Si un entry individual falla, lo saltamos pero no
            # abortamos todo el batch (algunos commits pueden tener
            # mensajes raros que Claude no clasifica bien).
            entries = []
            for i, item in enumerate(parsed):
                try:
                    entry = CommitEntry(**item)
                    entries.append(entry)
                except ValidationError as e:
                    logger.warning(
                        f"[Classifier] Entry {i} fallo validacion: {e}"
                    )

            if not entries:
                raise ValueError("Ningun entry paso la validacion Pydantic")

            # Log del resultado
            type_counts = {}
            for e in entries:
                type_counts[e.type] = type_counts.get(e.type, 0) + 1

            logger.info(
                f"[Classifier] OK: {len(entries)}/{len(commits)} clasificados | "
                f"{type_counts}"
            )
            return entries

        except json.JSONDecodeError as e:
            last_error = e
            logger.warning(f"[Classifier] JSON invalido: {e}")
            if metrics and metrics.steps:
                metrics.steps[-1].success = False
                metrics.steps[-1].error = str(e)

        except (ValueError, ValidationError) as e:
            last_error = e
            logger.warning(f"[Classifier] Validacion fallo: {e}")
            if metrics and metrics.steps:
                metrics.steps[-1].success = False
                metrics.steps[-1].error = str(e)

    raise RuntimeError(
        f"Clasificacion fallo despues de {config.max_retries} intentos. "
        f"Ultimo error: {last_error}"
    )


def classify_to_summary(entries: list[CommitEntry]) -> str:
    """
    Convierte entries clasificados a texto organizado por tipo.
    
    Este texto es lo que el generador de notas recibe como input.
    Organiza los cambios en secciones para que Claude pueda
    escribir las notas de forma estructurada.
    
    Args:
        entries: Lista de CommitEntry clasificados
        
    Returns:
        String con cambios organizados por categoria
    """
    by_type = {}
    for entry in entries:
        if entry.type not in by_type:
            by_type[entry.type] = []
        by_type[entry.type].append(entry)

    # Orden logico: features primero, luego fixes, luego el resto
    type_order = ["feature", "fix", "refactor", "docs", "chore", "test", "ci"]

    lines = []
    for type_name in type_order:
        if type_name not in by_type:
            continue
        group = by_type[type_name]
        lines.append(f"\n### {type_name.upper()} ({len(group)})")
        for entry in group:
            service = f" ({entry.affected_service})" if entry.affected_service != "general" else ""
            breaking = " [BREAKING]" if entry.breaking else ""
            lines.append(f"- {entry.description}{service}{breaking}")

    return "\n".join(lines)
