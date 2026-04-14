"""
estimate.py
==================================================

Descripcion:
-----------
Estimador de tokens y costo para el pipeline completo SIN llamar a la API
de Claude. Usa una heuristica de caracteres a tokens (~ 3.8 chars/token
para Claude con mezcla de prosa en ingles, JSON y markdown) mas
heuristicas de output por paso calibradas contra corridas historicas.

Proposito del modulo:
--------------------
- Construir los mismos prompts que el pipeline real construiria
- Contar tokens de input para classifier + generator + formatter ×N
- Aplicar estimaciones heuristicas de tokens de output por paso
- Multiplicar por el pricing del Config y devolver un desglose de costo
  por paso

Contenido del modulo:
--------------------
1. CHARS_PER_TOKEN - Constante de calibracion para la heuristica
2. estimate_tokens - len(text) / CHARS_PER_TOKEN, redondeado hacia arriba
3. estimate_pipeline - Construye un PipelineEstimate sobre todas las
                       plataformas habilitadas
4. PipelineEstimate - Dataclass con desglose por paso y totales
5. print_estimate - Pretty-printer usado por el CLI

Precision:
---------
La heuristica tiende a sobreestimar (4 chars/token es el limite optimista;
usamos 3.8 por seguridad). Esperar ±20% contra el uso real. Suficiente
para responder "este release va a costar 10x lo normal?" antes de gastar
tokens.

Metadata:
----------
* Autor: zxxz6 (Bryan Violante Arriaga)
* Version: 1.0.0
* Licencia: MIT

Historial:
------------
Autor       Fecha           Descripcion
zxxz6       13/04/2026      _formatter_prompt_for incluye los ejemplos
                            few-shot (EXAMPLE_*) al construir el prompt
                            del estimator, reflejando los tokens reales
zxxz6       13/04/2026      Lee el detailed changelog del README y lo pasa
                            al generator estimator via prior_context, asi
                            la estimacion incluye los tokens reales que
                            consumira la nueva fase
zxxz6       13/04/2026      Creacion
"""

from dataclasses import dataclass, field
from math import ceil

from pathlib import Path

from .config import Config, PlatformConfig
from .changelog import read_changelog, to_context_block
from .git_reader import commits_to_text
from .models import RawCommit
from .prompts import (
    CLASSIFIER_SYSTEM, CLASSIFIER_TASK,
    GENERATOR_SYSTEM, GENERATOR_TASK,
    FORMATTER_GITHUB, FORMATTER_PLAYSTORE, FORMATTER_APPSTORE,
    FORMATTER_KOFI, GLOSSARY_INSTRUCTION, NO_GLOSSARY,
    EXAMPLE_GITHUB, EXAMPLE_PLAYSTORE_EN, EXAMPLE_PLAYSTORE_ES,
    EXAMPLE_APPSTORE, EXAMPLE_KOFI,
)

# La tokenizacion de Claude es ~3.5-4 chars/token para una mezcla de
# ingles + markdown + JSON. 3.8 es un punto medio calibrado contra
# corridas reales.
CHARS_PER_TOKEN = 3.8


def estimate_tokens(text: str) -> int:
    """Devuelve un conteo heuristico de tokens para un bloque de texto."""
    if not text:
        return 0
    return ceil(len(text) / CHARS_PER_TOKEN)


@dataclass
class StepEstimate:
    """Tokens de input/output y costo en USD estimados para un paso del pipeline."""
    step_name: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float


@dataclass
class PipelineEstimate:
    """Estimacion del pipeline completo: desglose por paso + totales."""
    steps: list[StepEstimate] = field(default_factory=list)
    commit_count: int = 0
    platform_count: int = 0

    @property
    def total_input_tokens(self) -> int:
        """Suma de tokens de input sobre todos los pasos."""
        return sum(s.input_tokens for s in self.steps)

    @property
    def total_output_tokens(self) -> int:
        """Suma de tokens de output sobre todos los pasos."""
        return sum(s.output_tokens for s in self.steps)

    @property
    def total_tokens(self) -> int:
        """Gran total de tokens (input + output)."""
        return self.total_input_tokens + self.total_output_tokens

    @property
    def total_cost_usd(self) -> float:
        """Suma de los costos por paso en USD."""
        return sum(s.cost_usd for s in self.steps)


def _estimate_classifier(
    commits: list[RawCommit], config: Config
) -> StepEstimate:
    """Estima el paso del classifier dados los commits y la config global."""
    commits_text = commits_to_text(commits)
    prompt = CLASSIFIER_TASK.format(commits=commits_text)
    input_tokens = estimate_tokens(CLASSIFIER_SYSTEM) + estimate_tokens(prompt)
    # Heuristica de output: cada commit se vuelve un objeto JSON pequeno
    # (~55 tokens en promedio incluyendo llaves y comas).
    output_tokens = max(20, 55 * max(1, len(commits)))
    cost = config.calculate_cost(
        config.classifier_model, input_tokens, output_tokens
    )
    return StepEstimate(
        step_name="classify",
        model=config.classifier_model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost,
    )


def _estimate_generator(
    commits: list[RawCommit], config: Config, version: str = "vX.Y.Z",
    prior_context: str = "",
) -> StepEstimate:
    """Estima el paso del generator. Usa un proxy simple de cambios agrupados."""
    # Proxy para classify_to_summary(): una linea por commit mas encabezados
    # de seccion. 45 chars por commit es un punto medio conservador.
    changes_proxy = "\n".join(f"- {c.message}" for c in commits)
    system = GENERATOR_SYSTEM.format(project_name=config.project_name)
    context_block = f"\n{prior_context.strip()}\n" if prior_context.strip() else ""
    prompt = GENERATOR_TASK.format(
        project_name=config.project_name,
        version=version,
        date="April 13, 2026",
        prior_context=context_block,
        changes=changes_proxy,
    )
    input_tokens = estimate_tokens(system) + estimate_tokens(prompt)
    # Output: markdown base (~250 tokens) + ~35 tokens por cada commit que
    # aparece en las notas. Cap en max_tokens=4000 usado por generator.py.
    output_tokens = min(4000, 250 + 35 * max(1, len(commits)))
    cost = config.calculate_cost(
        config.generator_model, input_tokens, output_tokens
    )
    return StepEstimate(
        step_name="generate",
        model=config.generator_model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost,
    )


def _formatter_prompt_for(
    platform: str, pcfg: PlatformConfig, notes_chars: int, config: Config
) -> str:
    """
    Construye el user prompt que enviaria un formatter, usando un
    placeholder para el cuerpo de las notas para contar los tokens de
    input correctos sin necesitar el markdown real.
    """
    notes_placeholder = "X" * notes_chars
    glossary = NO_GLOSSARY
    if pcfg.language == "es" and config.glossary:
        glossary = GLOSSARY_INSTRUCTION.format(terms=", ".join(config.glossary))

    if platform == "github":
        return FORMATTER_GITHUB.format(
            example=EXAMPLE_GITHUB,
            notes=notes_placeholder, version_from="vX", version_to="vY",
        )
    if platform.startswith("playstore"):
        example = EXAMPLE_PLAYSTORE_ES if pcfg.language == "es" else EXAMPLE_PLAYSTORE_EN
        return FORMATTER_PLAYSTORE.format(
            example=example,
            notes=notes_placeholder,
            max_chars=pcfg.max_chars or 500,
            language=pcfg.language,
            glossary_instruction=glossary,
        )
    if platform == "appstore":
        return FORMATTER_APPSTORE.format(
            example=EXAMPLE_APPSTORE,
            notes=notes_placeholder, max_chars=pcfg.max_chars or 4000,
        )
    if platform == "kofi":
        return FORMATTER_KOFI.format(
            example=EXAMPLE_KOFI,
            notes=notes_placeholder, max_chars=pcfg.max_chars or 2000,
        )
    return f"Format for {platform}: <notes>{notes_placeholder}</notes>"


def _estimate_formatter_platform(
    platform: str,
    pcfg: PlatformConfig,
    notes_tokens: int,
    config: Config,
) -> StepEstimate:
    """Estima tokens y costo para la llamada de una plataforma del formatter."""
    # Convertir tokens estimados de las notas de vuelta a chars para el
    # placeholder, asi el conteo de input queda alineado con lo que el
    # formatter real enviaria.
    notes_chars = int(notes_tokens * CHARS_PER_TOKEN)
    system_chars = len(
        f"You are a content formatter for {config.project_name}, "
        f"a social network for rock climbers."
    )
    user_prompt = _formatter_prompt_for(platform, pcfg, notes_chars, config)
    input_tokens = ceil(system_chars / CHARS_PER_TOKEN) + estimate_tokens(user_prompt)

    # Output: para plataformas con limite de chars asumimos que lo llena
    # al ~85%. GitHub no tiene limite, asi que cap en 1500 tokens (notas
    # tipicas completas).
    if pcfg.max_chars:
        output_tokens = ceil((pcfg.max_chars * 0.85) / CHARS_PER_TOKEN)
    else:
        output_tokens = 1500

    cost = config.calculate_cost(
        config.formatter_model, input_tokens, output_tokens
    )
    return StepEstimate(
        step_name=f"format:{platform}",
        model=config.formatter_model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost,
    )


def estimate_pipeline(
    commits: list[RawCommit],
    config: Config,
    version: str = "vX.Y.Z",
) -> PipelineEstimate:
    """
    Calcula la estimacion de tokens + costo del pipeline completo para los
    commits dados.

    Args:
        commits: Commits crudos leidos de Git (normalmente de read_commits()).
        config: Configuracion del pipeline ya cargada.
        version: String de version placeholder para interpolar en los prompts.

    Returns:
        PipelineEstimate con StepEstimate por paso y properties de resumen.
    """
    enabled = config.get_enabled_platforms()
    est = PipelineEstimate(
        commit_count=len(commits),
        platform_count=len(enabled),
    )

    # Leer el changelog del README destino (sin LLM, solo para contar
    # tokens del prior_context que se inyectara al generator).
    prior_context = ""
    if config.changelog_enabled:
        readme_path = Path(config.repo_path) / config.changelog_source
        entries = read_changelog(
            readme_path,
            sections=tuple(config.changelog_sections),
            count=config.changelog_count,
        )
        prior_context = to_context_block(entries)

    classifier = _estimate_classifier(commits, config)
    generator = _estimate_generator(commits, config, version, prior_context)
    est.steps.append(classifier)
    est.steps.append(generator)

    # El input del formatter incluye el output del generator (el cuerpo de las notas).
    notes_tokens = generator.output_tokens
    for name, pcfg in enabled.items():
        est.steps.append(
            _estimate_formatter_platform(name, pcfg, notes_tokens, config)
        )

    return est


def print_estimate(est: PipelineEstimate) -> None:
    """Imprime un resumen legible de la estimacion a stdout."""
    print(f"\n{'='*60}")
    print(f"  Pipeline Cost Estimate (no API calls made)")
    print(f"{'='*60}")
    print(f"  Commits:       {est.commit_count}")
    print(f"  Platforms:     {est.platform_count}")
    print(f"  Input tokens:  {est.total_input_tokens:,}")
    print(f"  Output tokens: {est.total_output_tokens:,}")
    print(f"  Total tokens:  {est.total_tokens:,}")
    print(f"  Cost:          ${est.total_cost_usd:.4f}")
    print(f"{'='*60}")
    print(f"\n  Step breakdown:")
    for s in est.steps:
        print(
            f"    {s.step_name:24s} "
            f"in={s.input_tokens:>6,} out={s.output_tokens:>6,} "
            f"${s.cost_usd:.5f}"
        )
    print(f"\n  Note: heuristic estimate, expect ±20% vs. actual.")
