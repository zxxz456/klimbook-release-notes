"""
cli.py
==================================================

Descripcion:
-----------
Punto de entrada de linea de comandos basado en Typer para klimbook-release.
Conecta todos los componentes del pipeline (git_reader → classifier →
generator → formatter → validator) y los expone como comandos para el
usuario.

Proposito del modulo:
--------------------
- Parsear argumentos y opciones del CLI
- Orquestar las 5 fases del pipeline de release notes
- Persistir los artefactos generados en disco (un archivo por plataforma
  mas bundle.json)
- Imprimir progreso, resultados de validacion y metricas del pipeline

Contenido del modulo:
--------------------
1. generate - Ejecuta el pipeline completo para un rango de tags y escribe
              los outputs
2. tags - Lista los tags de Git disponibles y sugiere un comando generate
3. show_config - Imprime la configuracion cargada
4. show_changelog - Inspecciona las entradas del detailed changelog que se
                    inyectarian al generator, sin correr el pipeline

Fases del pipeline (generate):
-----------------------------
[1/6] Leer commits entre dos tags (git_reader)
[2/6] Leer detailed changelog del README destino (changelog, sin LLM)
[3/6] Clasificar commits con Haiku (classifier)
[4/6] Generar notas en markdown con Sonnet (generator + prior_context)
[5/6] Formatear por plataforma en paralelo con Sonnet (formatter)
[6/6] Validar el bundle sin LLM (validator)

Estructura de salida:
--------------------
./releases/<version_to>/
    github.md
    playstore_en.txt
    playstore_es.txt
    appstore.txt
    kofi.txt
    bundle.json   (ReleaseBundle completo + metricas)

Metadata:
----------
* Autor: zxxz6 (Bryan Violante Arriaga)
* Version: 1.0.0
* Licencia: MIT

Historial:
------------
Autor       Fecha           Descripcion
zxxz6       13/04/2026      Agregado comando `changelog` para inspeccionar
                            las entradas extraidas sin correr el pipeline
                            (flags --source, --count, --sections, --raw)
zxxz6       13/04/2026      Agregado paso [2/6] que lee el detailed
                            changelog del README (regex, sin LLM) y lo
                            inyecta como prior_context al generator. Pasos
                            renumerados de 5 a 6.
zxxz6       13/04/2026      --config ahora es opcional (default None); el
                            loader aplica la cadena de busqueda. Help
                            actualizado para explicar el orden.
zxxz6       13/04/2026      Agregado flag --estimate (real, sin llamadas a
                            la API), flags --use-cache/--no-cache, y
                            resumen de cache stats al finalizar
zxxz6       03/04/2026      Creacion
"""

import typer
from pathlib import Path
from typing import Optional
from datetime import datetime
import json
import logging
import sys

from .config import load_config
from .git_reader import read_commits, commits_to_text, list_tags, get_latest_tags
from .classifier import classify_commits, classify_to_summary
from .generator import generate_notes
from .formatter import format_all_sync
from .validator import validate_bundle, print_validation_result
from .models import ReleaseBundle, PipelineMetrics
from .utils import print_metrics_summary
from .cache import configure_cache, get_cache
from .estimate import estimate_pipeline, print_estimate
from .changelog import read_changelog, to_context_block

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s -- %(message)s",
    datefmt="%H:%M:%S",
)

app = typer.Typer(
    name="klimbook-release",
    help="CLI tool para generar release notes automaticas de Klimbook.",
    add_completion=False,
)


# =====================================================================
# comando generate
# =====================================================================

@app.command()
def generate(
    version_from: str = typer.Option(
        ..., "--from", "-f",
        help="Tag de inicio (ej: v2.8.0)",
    ),
    version_to: str = typer.Option(
        ..., "--to", "-t",
        help="Tag de fin (ej: v2.9.0)",
    ),
    platforms: Optional[str] = typer.Option(
        None, "--platforms", "-p",
        help="Plataformas separadas por coma (default: todas). "
             "Ej: github,playstore_en,kofi",
    ),
    output_dir: Path = typer.Option(
        Path("./releases"), "--output", "-o",
        help="Directorio de salida",
    ),
    config_path: Optional[str] = typer.Option(
        None, "--config", "-c",
        help="Ruta al archivo de configuracion. Si se omite, busca en: "
             "$KLIMBOOK_RELEASE_CONFIG, ./config.yaml, "
             "~/.config/klimbook-release/config.yaml, y el default empacado.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="Muestra commits sin gastar tokens",
    ),
    estimate: bool = typer.Option(
        False, "--estimate",
        help="Calcula tokens y costo estimados sin llamar a la API",
    ),
    no_cache: bool = typer.Option(
        False, "--no-cache",
        help="Deshabilita el cache (fuerza llamadas frescas a la API)",
    ),
    use_cache: bool = typer.Option(
        False, "--use-cache",
        help="Habilita el cache de respuestas (override de config.yaml)",
    ),
):
    """
    Genera release notes para todas las plataformas configuradas.

    El pipeline completo:
    1. Lee commits entre los dos tags (Git)
    2. Clasifica cada commit por tipo (Haiku)
    3. Genera release notes en markdown (Sonnet)
    4. Formatea para cada plataforma en paralelo (Sonnet)
    5. Valida formato y longitud
    6. Guarda archivos
    """
    config = load_config(config_path)
    metrics = PipelineMetrics()

    # ---- Configurar cache (CLI flags tienen precedencia sobre config.yaml) ----
    cache_enabled = config.cache_enabled
    if use_cache:
        cache_enabled = True
    if no_cache:
        cache_enabled = False
    configure_cache(cache_dir=config.cache_dir, enabled=cache_enabled)

    # Si el usuario especifico plataformas, deshabilitar las demas
    if platforms:
        requested = [p.strip() for p in platforms.split(",")]
        for name in config.platforms:
            config.platforms[name].enabled = name in requested

    enabled = config.get_enabled_platforms()
    typer.echo(f"\n{'='*60}")
    typer.echo(f"  {config.project_name} Release Notes Generator")
    typer.echo(f"  {version_from} -> {version_to}")
    typer.echo(f"  Plataformas: {', '.join(enabled.keys())}")
    typer.echo(f"{'='*60}")

    # ---- Paso [1/6]: Leer commits de Git ----
    typer.echo(f"\n[1/6] Leyendo commits...")
    try:
        commits = read_commits(config.repo_path, version_from, version_to)
    except (RuntimeError, ValueError) as e:
        typer.echo(f"  Error: {e}", err=True)
        raise typer.Exit(1)

    typer.echo(f"  {len(commits)} commits encontrados")

    if not commits:
        typer.echo("  No hay commits entre estos tags.")
        raise typer.Exit(1)

    # ---- Dry run: solo mostrar commits ----
    if dry_run:
        typer.echo(f"\n  [DRY RUN] Commits:")
        text = commits_to_text(commits)
        typer.echo(text)
        typer.echo(f"\n  Plataformas: {', '.join(enabled.keys())}")
        typer.echo("  No se gastaron tokens.")
        raise typer.Exit(0)

    # ---- Estimate: calcular tokens y costo sin llamar a la API ----
    if estimate:
        est = estimate_pipeline(commits, config, version=version_to)
        print_estimate(est)
        typer.echo("\n  No se gastaron tokens (estimate mode).")
        raise typer.Exit(0)

    # ---- Paso [2/6]: Leer detailed changelog del README (sin LLM) ----
    changelog_context = ""
    if config.changelog_enabled:
        typer.echo(f"\n[2/6] Leyendo detailed changelog del README...")
        readme_path = Path(config.repo_path) / config.changelog_source
        changelog_entries = read_changelog(
            readme_path,
            sections=tuple(config.changelog_sections),
            count=config.changelog_count,
        )
        if changelog_entries:
            for entry in changelog_entries:
                typer.echo(f"  • {entry.section} {entry.version} — {entry.title}")
            changelog_context = to_context_block(changelog_entries)
        else:
            typer.echo(
                f"  (sin entradas en {readme_path}; se generan notas solo "
                f"con commits)"
            )
    else:
        typer.echo(f"\n[2/6] Detailed changelog deshabilitado en config")

    # ---- Paso [3/6]: Clasificar commits ----
    typer.echo(f"\n[3/6] Clasificando commits con {config.classifier_model}...")
    try:
        classified = classify_commits(commits, config, metrics)
    except RuntimeError as e:
        typer.echo(f"  Error en clasificacion: {e}", err=True)
        raise typer.Exit(1)

    typer.echo(f"  {len(classified)} commits clasificados")

    # Mostrar distribucion
    type_counts = {}
    for entry in classified:
        type_counts[entry.type] = type_counts.get(entry.type, 0) + 1
    for t, count in sorted(type_counts.items()):
        typer.echo(f"    {t}: {count}")

    # ---- Paso [4/6]: Generar release notes ----
    typer.echo(f"\n[4/6] Generando release notes con {config.generator_model}...")
    try:
        notes = generate_notes(
            entries=classified,
            version=version_to,
            config=config,
            metrics=metrics,
            prior_context=changelog_context,
        )
    except RuntimeError as e:
        typer.echo(f"  Error en generacion: {e}", err=True)
        raise typer.Exit(1)

    typer.echo(f"  Markdown generado: {len(notes.markdown)} chars")

    # ---- Paso [5/6]: Formatear para cada plataforma (paralelo) ----
    typer.echo(f"\n[5/6] Formateando para {len(enabled)} plataformas...")
    outputs = format_all_sync(
        notes=notes,
        config=config,
        metrics=metrics,
        version_from=version_from,
        version_to=version_to,
    )

    for name, output in outputs.items():
        limit_str = f"/{output.max_chars}" if output.max_chars else ""
        status = "OK" if output.within_limit else "EXCEDE LIMITE"
        typer.echo(f"  {name}: {output.char_count}{limit_str} chars [{status}]")

    # ---- Paso [6/6]: Validar ----
    typer.echo(f"\n[6/6] Validando...")
    bundle = ReleaseBundle(
        version=version_to,
        date=datetime.now().strftime("%B %d, %Y"),
        commit_count=len(commits),
        outputs=outputs,
        metrics=metrics.summary(),
    )

    validation = validate_bundle(bundle, config)
    print_validation_result(validation)

    if not validation.is_valid:
        typer.echo(
            "\n  El bundle tiene errores de validacion. "
            "Revisa los issues arriba.",
            err=True,
        )
        # Aun asi guardamos los archivos para inspeccion manual

    # ---- Guardar archivos ----
    output_dir.mkdir(parents=True, exist_ok=True)
    release_dir = output_dir / version_to

    # Si el directorio ya existe, preguntar si sobreescribir
    if release_dir.exists():
        overwrite = typer.confirm(
            f"\n  El directorio {release_dir} ya existe. Sobreescribir?"
        )
        if not overwrite:
            typer.echo("  Cancelado.")
            raise typer.Exit(0)

    release_dir.mkdir(parents=True, exist_ok=True)

    # Guardar cada plataforma en su propio archivo
    for name, output in bundle.outputs.items():
        # Determinar extension segun plataforma
        if name == "github":
            ext = "md"
        else:
            ext = "txt"

        filepath = release_dir / f"{name}.{ext}"
        filepath.write_text(output.content, encoding="utf-8")
        typer.echo(f"  Guardado: {filepath}")

    # Guardar el bundle completo como JSON (para referencia)
    bundle_path = release_dir / "bundle.json"
    bundle_json = bundle.model_dump()
    # Convertir a JSON serializable
    bundle_path.write_text(
        json.dumps(bundle_json, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    typer.echo(f"  Guardado: {bundle_path}")

    # ---- Resumen final ----
    print_metrics_summary(metrics)

    # Resumen de cache si estaba habilitado
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
# comando tags
# =====================================================================

@app.command()
def tags(
    repo_path: str = typer.Option(".", "--repo", "-r", help="Ruta al repo Git"),
    last: int = typer.Option(20, "--last", "-n", help="Ultimos N tags"),
):
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
            f"\nSugerencia:\n  klimbook-release generate "
            f"--from {latest[0]} --to {latest[1]}"
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
):
    """Muestra la configuracion actual."""
    cfg = load_config(config_path)

    source = config_path or "cadena de busqueda"
    typer.echo(f"\nConfiguracion ({source}):")
    typer.echo(f"  Proyecto:     {cfg.project_name}")
    typer.echo(f"  Repo:         {cfg.repo_path}")
    typer.echo(f"  Classifier:   {cfg.classifier_model} (temp={cfg.classifier_temp})")
    typer.echo(f"  Generator:    {cfg.generator_model} (temp={cfg.generator_temp})")
    typer.echo(f"  Formatter:    {cfg.formatter_model}")
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
# comando changelog
# =====================================================================

@app.command("changelog")
def show_changelog(
    config_path: Optional[str] = typer.Option(
        None, "--config", "-c",
        help="Ruta explicita al config.yaml. Si se omite, usa la cadena.",
    ),
    source: Optional[str] = typer.Option(
        None, "--source", "-s",
        help="Ruta al README a parsear (override de config.changelog_source).",
    ),
    count: Optional[int] = typer.Option(
        None, "--count", "-n",
        help="Ultimas N entradas por seccion (override de changelog_count).",
    ),
    sections: Optional[str] = typer.Option(
        None, "--sections",
        help="Secciones separadas por coma (ej: Backend,Frontend,Mobile).",
    ),
    raw: bool = typer.Option(
        False, "--raw",
        help="Imprime solo las entradas crudas (sin el encabezado de contexto "
             "que se inyectaria al generator).",
    ),
):
    """
    Inspecciona las entradas del detailed changelog que se inyectarian al
    generator, sin correr el pipeline completo ni gastar tokens.

    Util para debuggear el regex y ver exactamente que contexto recibira
    el LLM. Use `--raw` para el markdown crudo o sin flag para el bloque
    de contexto completo listo para copiar/pegar.
    """
    cfg = load_config(config_path)

    # CLI overrides tienen precedencia sobre el config
    readme_path = Path(source) if source else Path(cfg.repo_path) / cfg.changelog_source
    effective_count = count if count is not None else cfg.changelog_count
    effective_sections = (
        tuple(s.strip() for s in sections.split(",") if s.strip())
        if sections
        else tuple(cfg.changelog_sections)
    )

    typer.echo(f"\n{'='*60}")
    typer.echo(f"  Detailed Changelog Inspector")
    typer.echo(f"{'='*60}")
    typer.echo(f"  README:    {readme_path}")
    typer.echo(f"  Sections:  {', '.join(effective_sections)}")
    typer.echo(f"  Count:     {effective_count} por seccion")
    typer.echo(f"{'='*60}\n")

    if not readme_path.is_file():
        typer.echo(f"  Error: README no encontrado en {readme_path}", err=True)
        raise typer.Exit(1)

    entries = read_changelog(
        readme_path,
        sections=effective_sections,
        count=effective_count,
    )

    if not entries:
        typer.echo("  No se encontraron entradas en el README.")
        typer.echo(
            "  Verifica que el README tenga lineas tipo:\n"
            "    #### Backend `v2.10.0` — Title (date)"
        )
        raise typer.Exit(1)

    # Resumen breve primero
    typer.echo(f"  {len(entries)} entradas extraidas:\n")
    for i, entry in enumerate(entries, 1):
        typer.echo(
            f"  [{i}] {entry.section} {entry.version} — {entry.title} "
            f"({entry.date})  [{len(entry.body)} chars]"
        )

    typer.echo(f"\n{'='*60}")
    typer.echo("  Contenido extraido")
    typer.echo(f"{'='*60}\n")

    if raw:
        # Solo las entradas reconstruidas (heading + body), sin wrapper
        for entry in entries:
            typer.echo(entry.to_markdown())
            typer.echo("")
    else:
        # Bloque tal cual se inyectaria al generator
        typer.echo(to_context_block(entries))

    total_chars = sum(len(e.body) for e in entries)
    typer.echo(f"\n{'='*60}")
    typer.echo(f"  Total: {len(entries)} entradas, {total_chars} chars de body")
    typer.echo(f"{'='*60}")


# =====================================================================
# Punto de entrada
# =====================================================================

if __name__ == "__main__":
    app()
