"""
cli.py
==================================================

Descripcion:
-----------
CLI del paquete kbkro — misma funcionalidad que `klimbook-release generate`
pero usando Ollama local en vez de la API de Anthropic.

Proposito del modulo:
--------------------
- Exponer el comando `kbkro generate` con las mismas opciones que el
  original, mas --model y --host para apuntar a Ollama
- Parchar los singletons de cliente de klimbook_release.utils y
  klimbook_release.formatter con OllamaSyncShim/AsyncShim antes de correr
  el pipeline
- Normalizar classifier_model/generator_model/formatter_model al mismo
  modelo local y poner pricing en 0 para que las metricas reporten $0

Contenido del modulo:
--------------------
1. _install_ollama - Instala los shims y normaliza la Config
2. generate - Ejecuta el pipeline completo contra Ollama
3. tags / show_config - Delegan a klimbook_release (no necesitan patch)

Metadata:
----------
* Autor: zxxz6 (Bryan Violante Arriaga)
* Version: 0.1.0
* Licencia: MIT

Historial:
------------
Autor       Fecha           Descripcion
zxxz6       18/04/2026      Creacion
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

import typer

from klimbook_release import utils as utils_mod
from klimbook_release import formatter as formatter_mod
from klimbook_release.cache import configure_cache, get_cache
from klimbook_release.changelog import read_changelog, to_context_block
from klimbook_release.classifier import classify_commits
from klimbook_release.config import Config, PricingConfig, load_config
from klimbook_release.formatter import format_all_sync
from klimbook_release.generator import generate_notes
from klimbook_release.git_reader import (
    commits_to_text, list_tags, read_commits,
)
from klimbook_release.models import PipelineMetrics, ReleaseBundle
from klimbook_release.utils import print_metrics_summary
from klimbook_release.validator import print_validation_result, validate_bundle

from .ollama_shim import OllamaAsyncShim, OllamaSyncShim


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s -- %(message)s",
    datefmt="%H:%M:%S",
)

app = typer.Typer(
    name="kbkro",
    help="Klimbook Release Ollama: mismo pipeline que klimbook-release, "
         "pero corriendo contra un servidor Ollama local (sin costo, sin "
         "API key de Anthropic).",
    add_completion=False,
)


# =====================================================================
# Instalacion de los shims
# =====================================================================

def _install_ollama(host: str, model: str, config: Config) -> None:
    """
    Reemplaza los clientes Anthropic de klimbook_release con shims Ollama
    y normaliza Config para que todos los pasos usen el mismo modelo local
    con pricing $0.

    Este parche:
    - Setea los singletons `_sync_client`/`_async_client` en utils y
      formatter.
    - Sobrescribe los getters (`get_sync_client`, `_get_async_client`)
      para que cualquier codepath futuro reciba los mismos shims.
    - Fuerza classifier_model = generator_model = formatter_model = model.
    - Registra PricingConfig(0, 0) para ese modelo asi calculate_cost
      devuelve 0.
    """
    sync_shim = OllamaSyncShim(host=host)
    async_shim = OllamaAsyncShim(host=host)

    # Singletons a nivel de modulo
    utils_mod._sync_client = sync_shim
    utils_mod._async_client = async_shim
    formatter_mod._async_client = async_shim

    # Getters — cubren paths que no pasen por el singleton directamente
    utils_mod.get_sync_client = lambda: sync_shim
    utils_mod.get_async_client = lambda: async_shim
    formatter_mod._get_async_client = lambda: async_shim

    # Forzar el modelo y neutralizar pricing
    config.classifier_model = model
    config.generator_model = model
    config.formatter_model = model
    config.pricing[model] = PricingConfig(input=0.0, output=0.0)


# =====================================================================
# comando generate
# =====================================================================

@app.command()
def generate(
    version_from: str = typer.Option(
        ..., "--from", "-f", help="Tag de inicio (ej: v2.10.0)",
    ),
    version_to: str = typer.Option(
        ..., "--to", "-t", help="Tag de fin (ej: v2.11.0)",
    ),
    model: str = typer.Option(
        "gemma4:26b", "--model", "-m",
        help="Modelo de Ollama a usar en los 3 pasos del pipeline. "
             "Ej: gemma4:26b, gemma4:31b, llama3.3:70b, qwen2.5:14b.",
    ),
    host: str = typer.Option(
        "http://localhost:11434", "--host",
        help="URL base del servidor Ollama.",
    ),
    platforms: Optional[str] = typer.Option(
        None, "--platforms", "-p",
        help="Plataformas separadas por coma (default: todas las "
             "habilitadas). Ej: github,playstore_en,kofi",
    ),
    output_dir: Path = typer.Option(
        Path("./releases"), "--output", "-o", help="Directorio de salida",
    ),
    config_path: Optional[str] = typer.Option(
        None, "--config", "-c",
        help="Ruta al archivo de configuracion. Si se omite, busca en: "
             "$KLIMBOOK_RELEASE_CONFIG, ./config.yaml, "
             "~/.config/klimbook-release/config.yaml, y el default empacado.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="Lista commits sin llamar al modelo",
    ),
    no_cache: bool = typer.Option(
        False, "--no-cache",
        help="Deshabilita el cache de respuestas",
    ),
    use_cache: bool = typer.Option(
        False, "--use-cache",
        help="Habilita el cache de respuestas (override de config.yaml)",
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v",
        help="Log DEBUG: volcado completo de cada respuesta de Ollama y "
             "headers de request. Util para diagnosticar JSON invalido o "
             "outputs truncados.",
    ),
) -> None:
    """
    Genera release notes para todas las plataformas configuradas usando
    Ollama local.
    """
    if verbose:
        # Subir todos los loggers (root + los del pipeline) a DEBUG
        logging.getLogger().setLevel(logging.DEBUG)
        for name in (
            "kbkro.ollama", "classifier", "generator", "formatter",
            "utils", "cache",
        ):
            logging.getLogger(name).setLevel(logging.DEBUG)

    config = load_config(config_path)
    _install_ollama(host, model, config)

    metrics = PipelineMetrics()

    # ---- Cache (misma semantica que klimbook-release) ----
    cache_enabled = config.cache_enabled
    if use_cache:
        cache_enabled = True
    if no_cache:
        cache_enabled = False
    configure_cache(cache_dir=config.cache_dir, enabled=cache_enabled)

    # ---- Filtrar plataformas ----
    if platforms:
        requested = [p.strip() for p in platforms.split(",")]
        for name in config.platforms:
            config.platforms[name].enabled = name in requested

    enabled = config.get_enabled_platforms()
    typer.echo(f"\n{'='*60}")
    typer.echo(f"  {config.project_name} Release Notes (Ollama)")
    typer.echo(f"  Model:       {model}")
    typer.echo(f"  Host:        {host}")
    typer.echo(f"  {version_from} -> {version_to}")
    typer.echo(f"  Plataformas: {', '.join(enabled.keys())}")
    typer.echo(f"{'='*60}")

    # ---- [1/5] Commits ----
    typer.echo(f"\n[1/5] Leyendo commits...")
    try:
        commits = read_commits(config.repo_path, version_from, version_to)
    except (RuntimeError, ValueError) as e:
        typer.echo(f"  Error: {e}", err=True)
        raise typer.Exit(1)
    typer.echo(f"  {len(commits)} commits encontrados")
    if not commits:
        typer.echo("  No hay commits entre estos tags.")
        raise typer.Exit(1)
    if dry_run:
        typer.echo(f"\n  [DRY RUN] Commits:")
        typer.echo(commits_to_text(commits))
        typer.echo("\n  No se llamo a Ollama.")
        raise typer.Exit(0)

    # ---- [2/5] Changelog (contexto para el generator) ----
    changelog_context = ""
    if config.changelog_enabled:
        typer.echo(f"\n[2/5] Leyendo detailed changelog del README...")
        readme_path = Path(config.repo_path) / config.changelog_source
        entries = read_changelog(
            readme_path,
            sections=tuple(config.changelog_sections),
            count=config.changelog_count,
        )
        if entries:
            for e in entries:
                typer.echo(f"  • {e.section} {e.version} — {e.title}")
            changelog_context = to_context_block(entries)
        else:
            typer.echo(
                f"  (sin entradas en {readme_path}; el generator usa solo "
                f"commits)"
            )
    else:
        typer.echo(f"\n[2/5] Detailed changelog deshabilitado en config")

    # ---- [3/5] Classify ----
    typer.echo(f"\n[3/5] Clasificando commits con {model}...")
    try:
        classified = classify_commits(commits, config, metrics)
    except RuntimeError as e:
        typer.echo(f"  Error en clasificacion: {e}", err=True)
        raise typer.Exit(1)
    typer.echo(f"  {len(classified)} commits clasificados")
    type_counts: dict[str, int] = {}
    for entry in classified:
        type_counts[entry.type] = type_counts.get(entry.type, 0) + 1
    for t, count in sorted(type_counts.items()):
        typer.echo(f"    {t}: {count}")

    # ---- [4/5] Generator + Formatter ----
    typer.echo(f"\n[4/5] Generando notas y formateando plataformas...")
    try:
        notes = generate_notes(
            entries=classified, version=version_to, config=config,
            metrics=metrics, prior_context=changelog_context,
        )
    except RuntimeError as e:
        typer.echo(f"  Error en generacion: {e}", err=True)
        raise typer.Exit(1)
    typer.echo(f"  Markdown generado: {len(notes.markdown)} chars")

    outputs = format_all_sync(
        notes=notes, config=config, metrics=metrics,
        version_from=version_from, version_to=version_to,
    )
    for name, output in outputs.items():
        limit = f"/{output.max_chars}" if output.max_chars else ""
        status = "OK" if output.within_limit else "EXCEDE LIMITE"
        typer.echo(f"  {name}: {output.char_count}{limit} chars [{status}]")

    # ---- [5/5] Validate + save ----
    typer.echo(f"\n[5/5] Validando y guardando...")
    bundle = ReleaseBundle(
        version=version_to,
        date=datetime.now().strftime("%B %d, %Y"),
        commit_count=len(commits),
        outputs=outputs,
        metrics=metrics.summary(),
    )
    validation = validate_bundle(bundle, config)
    print_validation_result(validation)

    output_dir.mkdir(parents=True, exist_ok=True)
    release_dir = output_dir / version_to
    if release_dir.exists():
        overwrite = typer.confirm(
            f"\n  El directorio {release_dir} ya existe. Sobreescribir?"
        )
        if not overwrite:
            typer.echo("  Cancelado.")
            raise typer.Exit(0)
    release_dir.mkdir(parents=True, exist_ok=True)

    for name, output in bundle.outputs.items():
        ext = "md" if name == "github" else "txt"
        fpath = release_dir / f"{name}.{ext}"
        fpath.write_text(output.content, encoding="utf-8")
        typer.echo(f"  Guardado: {fpath}")

    bundle_path = release_dir / "bundle.json"
    bundle_path.write_text(
        json.dumps(bundle.model_dump(), indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    typer.echo(f"  Guardado: {bundle_path}")

    print_metrics_summary(metrics)

    cache_stats = get_cache().stats()
    if cache_stats["enabled"]:
        typer.echo(
            f"\n  Cache: {cache_stats['hits']} hits / "
            f"{cache_stats['misses']} misses | "
            f"{cache_stats['entries']} entries ({cache_stats['bytes']} bytes) "
            f"in {cache_stats['dir']}"
        )

    typer.echo(f"\n  Release notes guardadas en: {release_dir}/")
    if validation.is_valid:
        typer.echo("  Listas para publicar.")
    else:
        typer.echo("  Revisa los errores de validacion antes de publicar.")


# =====================================================================
# comando tags (delegacion simple a git)
# =====================================================================

@app.command()
def tags(
    repo_path: str = typer.Option(".", "--repo", "-r", help="Ruta al repo Git"),
    last: int = typer.Option(20, "--last", "-n", help="Ultimos N tags"),
) -> None:
    """Lista los tags disponibles en el repositorio."""
    all_tags = list_tags(repo_path)
    if not all_tags:
        typer.echo("No se encontraron tags.")
        raise typer.Exit(1)
    shown = all_tags[-last:] if len(all_tags) > last else all_tags
    typer.echo(f"\nTags ({len(shown)} de {len(all_tags)}):")
    for tag in shown:
        typer.echo(f"  {tag}")
    if len(all_tags) >= 2:
        latest = all_tags[-2:]
        typer.echo(
            f"\nSugerencia:\n  kbkro generate "
            f"--from {latest[0]} --to {latest[1]} --model gemma4:26b"
        )


# =====================================================================
# comando config
# =====================================================================

@app.command("config")
def show_config(
    config_path: Optional[str] = typer.Option(
        None, "--config", "-c",
        help="Ruta explicita. Si se omite, usa la cadena de busqueda.",
    ),
    model: str = typer.Option(
        "gemma4:26b", "--model", "-m",
        help="Muestra la config como quedaria con este modelo aplicado.",
    ),
    host: str = typer.Option(
        "http://localhost:11434", "--host",
        help="Host de Ollama a mostrar.",
    ),
) -> None:
    """Muestra la configuracion como quedaria al correr `kbkro generate`."""
    cfg = load_config(config_path)
    # Aplicamos el mismo normalizado para mostrar el estado real
    cfg.classifier_model = model
    cfg.generator_model = model
    cfg.formatter_model = model

    typer.echo(f"\nkbkro config:")
    typer.echo(f"  Proyecto:     {cfg.project_name}")
    typer.echo(f"  Repo:         {cfg.repo_path}")
    typer.echo(f"  Ollama host:  {host}")
    typer.echo(f"  Modelo:       {model} (classifier/generator/formatter)")
    typer.echo(f"  Max retries:  {cfg.max_retries}")
    typer.echo(f"  Max parallel: {cfg.max_parallel}")

    typer.echo(f"\n  Plataformas:")
    for name, pcfg in cfg.platforms.items():
        status = "ON" if pcfg.enabled else "OFF"
        limit = f"max {pcfg.max_chars}" if pcfg.max_chars else "sin limite"
        typer.echo(f"    [{status}] {name}: {pcfg.language}, {limit}")

    typer.echo(f"\n  Glosario: {len(cfg.glossary)} terminos")
    if cfg.glossary:
        typer.echo(f"    {', '.join(cfg.glossary[:10])}...")


# =====================================================================
# Punto de entrada
# =====================================================================

if __name__ == "__main__":
    app()
