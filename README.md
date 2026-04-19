# Klimbook Release Notes LLM Pipeline

CLI tool que automatiza la generación de **release notes multi-plataforma** para [Klimbook](https://github.com/zxxz456/klimbook). 
Lee los commits de Git entre dos tags (al igual que los versioning notes en el repo), los clasifica con Claude Haiku, redacta notas en markdown con Claude Sonnet y adapta el resultado en paralelo a GitHub, Play Store (en/es), App Store y Ko-fi.

Ofrece **dos CLIs**:
- **`klimbook-release`** — pipeline contra la API de Anthropic (Claude Haiku + Sonnet). Producción.
- **`kbkro`** (*klimbook release ollama*) — mismo pipeline 100% local, contra un servidor Ollama (`gemma4:26b`, `llama3.3:70b`, `qwen2.5:14b`, etc.). Sin API key, sin costo. Ver [kbkro — Variante con Ollama](#kbkro--variante-con-ollama-local-gratis).

> **Estado actual (v0.1.0):** pipeline end-to-end funcional. Todos los componentes (`git_reader`, `classifier`, `generator`, `formatter`, `validator`) están implementados y conectados desde el CLI. Ejecutar `klimbook-release generate --from vX --to vY` produce los 5 artefactos en disco más un `bundle.json` con la metadata completa. La variante `kbkro` reutiliza los mismos componentes via shims Anthropic→Ollama — cero duplicación.

---

## Problema

Generar release notes para 5 plataformas (GitHub, Play Store en/es, App Store, Ko-fi) toma aprox. 40 minutos por release y es propenso a errores: copiar-pegar entre stores, traducir a mano, recortar texto al límite de cada plataforma, mantener tono casual en Ko-fi pero formal en App Store, etc.

## Solución

Un CLI que ejecuta el flujo completo en ~30 segundos:

```bash
klimbook-release generate --from v2.8.0 --to v2.9.0
```

Un solo comando produce los 5 artefactos listos para pegar en cada plataforma, validados contra los límites oficiales de cada store y traducidos respetando un glosario de términos de escalada que **no** deben traducirse (boulder, redpoint, beta, crux…).

---

## Arquitectura

```
                ┌──────────────────────────────────────┐
                │   CLI (Typer)                         │
                │   klimbook-release generate ...       │
                └──────────────────┬────────────────────┘
                                   │
                ┌──────────────────▼────────────────────┐
                │   [1/6] Git Reader (GitPython)        │
                │   tag_from..tag_to → list[RawCommit]  │
                └──────────────────┬────────────────────┘
                                   │
                ┌──────────────────▼────────────────────┐
                │   [2/6] Changelog Reader (regex)      │
                │   README.md → list[ChangelogEntry]    │
                │   latest N por sección (Backend/      │
                │   Frontend/Mobile). Sin LLM.          │
                └──────────────────┬────────────────────┘
                                   │
                ┌──────────────────▼────────────────────┐
                │   [3/6] Classifier (Haiku, T=0)       │
                │   commits → list[CommitEntry]         │
                │   {type, description, service, break} │
                │   Retry: JSON inválido / Pydantic     │
                └──────────────────┬────────────────────┘
                                   │
                ┌──────────────────▼────────────────────┐
                │   [4/6] Generator (Sonnet, T=0.3)     │
                │   commits + changelog → ReleaseNotes  │
                │   markdown completo con secciones     │
                │   prior_context = changelog del [2/6] │
                │   Retry: estructura inválida / vacío  │
                └──────────────────┬────────────────────┘
                                   │
                ┌──────────────────▼────────────────────┐
                │   [5/6] Formatter (Sonnet, paralelo)  │
                │   asyncio.gather + Semaphore(N)       │
                │   Retry + smart_truncate si excede    │
                └──┬─────────┬─────────┬─────────┬──────┘
                   │         │         │         │
              ┌────▼──┐ ┌────▼──┐ ┌────▼──┐ ┌───▼────┐
              │GitHub │ │ Play  │ │ App   │ │ Ko-fi  │
              │  MD   │ │ en/es │ │ Store │ │ casual │
              └────┬──┘ └───┬───┘ └───┬───┘ └────┬───┘
                   │        │         │          │
                ┌──▼────────▼─────────▼──────────▼──────┐
                │  [6/6] Validator (Pydantic + regex)   │
                │  límites + estructura + reglas store  │
                │  → ValidationResult (errors+warnings) │
                └──────────────────┬────────────────────┘
                                   │
                ┌──────────────────▼────────────────────┐
                │  ReleaseBundle → ./releases/vX.Y.Z/   │
                │  github.md, playstore_en.txt, ...     │
                │  bundle.json (metadata + métricas)    │
                └───────────────────────────────────────┘
```

**Modelo de datos del pipeline:**

```
RawCommit     ─►  CommitEntry  ─►  ReleaseNotes  ─►  PlatformOutput  ─►  ReleaseBundle
(git_reader)      (classifier)     (generator)       (formatter ×N)      (output final)
                                        ▲
ChangelogEntry  ────────────────────────┘
(changelog reader, inyectado como prior_context)
```

Cada paso recibe un modelo Pydantic validado y produce el siguiente. Las métricas (tokens, costo, tiempo) se acumulan en `PipelineMetrics` a lo largo de todo el flujo y se imprimen al final.

---

## Stack

| Capa | Tecnología | Uso |
|---|---|---|
| LLM | **Anthropic Claude API** | Haiku 4.5 para clasificación (rápido, barato, T=0). Sonnet 4 para generación y formateo (calidad de redacción) |
| Git | **GitPython 3.1+** | Lee commits entre tags sin shell-out (evita inyección) |
| Validación | **Pydantic 2** | Modelos tipados para todo el pipeline + validators custom |
| CLI | **Typer 0.12+** | Comandos, opciones, help auto-generado |
| Config | **PyYAML 6** | Configuración declarativa en `config.yaml` |
| Concurrencia | **`asyncio.gather` + `Semaphore`** | Formateo paralelo de las N plataformas con límite de rate |
| Tests | **pytest 8 + pytest-asyncio** | Tests unitarios sin necesidad de API key |

**Modelos por defecto:**
- Classifier: `claude-haiku-4-5-20251001` — temperatura `0.0`
- Generator: `claude-sonnet-4-20250514` — temperatura `0.3`
- Formatter: `claude-sonnet-4-20250514` — temperatura `0.3` (formal) / `0.7` (casual, Ko-fi)

---

## Estructura del proyecto

```
klimbook_release_notes_agent/
├── pyproject.toml        # Build config + dependencies + entry point CLI
├── config.yaml           # Configuración del pipeline (modelos, plataformas, glosario)
├── README.md             # Este archivo
├── src/                       # Código fuente
│   ├── klimbook_release/      # Paquete principal (pipeline contra Anthropic)
│   │   ├── __init__.py        # Versión del paquete (0.1.0)
│   │   ├── cli.py             # Entry point Typer: generate, tags, config (~310 líneas)
│   │   ├── config.py          # Loader YAML → Config (Pydantic) + cálculo de costos (~210 líneas)
│   │   ├── git_reader.py      # Lee commits entre tags con GitPython (~197 líneas)
│   │   ├── models.py          # Modelos Pydantic compartidos (~233 líneas)
│   │   ├── prompts.py         # Todos los prompts del pipeline (~248 líneas)
│   │   ├── utils.py           # API client singleton, call_llm, retry, métricas (~248 líneas)
│   │   ├── classifier.py      # Clasifica commits con Haiku + retry + agrupador (~196 líneas)
│   │   ├── generator.py       # Genera markdown con Sonnet + validación de estructura (~183 líneas)
│   │   ├── formatter.py       # Formatea por plataforma en paralelo + smart truncate (~409 líneas)
│   │   ├── validator.py       # Valida bundle (sin LLM) → ValidationResult (~334 líneas)
│   │   ├── cache.py           # ResponseCache SHA-256 en disco (~175 líneas)
│   │   ├── changelog.py       # Parser regex del README del proyecto destino (~205 líneas)
│   │   └── estimate.py        # Estimador de tokens y costo sin API (~215 líneas)
│   └── kbkro/                 # Paquete Ollama (reutiliza klimbook_release via shims)
│       ├── __init__.py        # Versión del paquete (0.1.0)
│       ├── cli.py             # Entry point Typer: generate, tags, config con --model/--host (~280 líneas)
│       └── ollama_shim.py     # Sync/Async shims duck-typed como anthropic.{Anthropic,AsyncAnthropic} (~240 líneas)
└── tests/
    ├── __init__.py
    ├── conftest.py              # Fixtures: MockAnthropic (sync + async), sample_commits, base_config
    ├── test_models.py           # ~12 tests de modelos y config
    ├── test_classifier_mock.py  # Integración: happy path, retry, skip de entries inválidos
    ├── test_generator_mock.py   # Integración: happy path, retry, empty, category counts
    ├── test_formatter_mock.py   # Integración async: paralelo, truncate, retry overshoot
    ├── test_cache.py            # Cache: key SHA-256, enable/disable, round-trip, hit en call_llm
    ├── test_changelog.py        # Parser regex: parse, filtrado por sección, edge cases
    └── test_estimate.py         # Estimador: heurística, shape, no-api-calls
```

> **Nota:** Dos paquetes se instalan desde `src/`: `klimbook_release` (CLI `klimbook-release`, contra Anthropic) y `kbkro` (CLI `kbkro`, contra Ollama local). Ambos entry points se declaran en `pyproject.toml`.

---

## Flujo de ejecución end-to-end

Trace completo desde que el usuario invoca el tool hasta que los archivos quedan en disco. Comando de ejemplo:

```bash
klimbook-release generate --from v2.9.0 --to v2.10.0
```

### Fase 0 — Shell → ejecutable

El shell encuentra `klimbook-release` en `$PATH`. Es un **script autogenerado por pip** al instalar el paquete, gracias al entry point declarado en [pyproject.toml](pyproject.toml):

```toml
[project.scripts]
klimbook-release = "klimbook_release.cli:app"
```

El script equivale a:

```python
from klimbook_release.cli import app
import sys; sys.exit(app())
```

### Fase 1 — Carga de módulos e inicialización

Al importar `klimbook_release.cli`, Python carga en cascada todos los módulos del paquete:

```
cli.py  ─►  config.py  ─►  git_reader.py  ─►  classifier.py
        ─►  generator.py  ─►  formatter.py  ─►  validator.py
        ─►  models.py  ─►  utils.py  ─►  cache.py  ─►  estimate.py
```

Al cargarse, cada módulo ejecuta su top level:
- [utils.py](src/klimbook_release/utils.py) inicializa `_sync_client = None` y `_async_client = None` (singletons vacíos)
- [cache.py](src/klimbook_release/cache.py) inicializa `_cache = None`
- [cli.py](src/klimbook_release/cli.py) crea el objeto `app = typer.Typer(...)`; cada `@app.command()` registra una función como subcomando

### Fase 2 — Typer parsea argumentos

`app()` lee `sys.argv`, identifica el subcomando `generate` ([cli.py:93](src/klimbook_release/cli.py#L93)) y mapea los flags a parámetros de la función. Llama `generate(version_from="v2.9.0", version_to="v2.10.0", ...)`.

### Fase 3 — `generate()` orquesta el pipeline

#### 3.1 — Cargar configuración

```python
config = load_config(config_path)  # config_path = None
```

[config.py:_resolve_config_path](src/klimbook_release/config.py) recorre la cadena de búsqueda en orden:

1. `--config` explícito (None aquí, se salta)
2. `$KLIMBOOK_RELEASE_CONFIG`
3. `./config.yaml` (CWD)
4. `~/.config/klimbook-release/config.yaml` (XDG)
5. `default_config.yaml` empacado (siempre existe)

Encuentra el primero, hace `yaml.safe_load`, y construye un `Config` Pydantic tipado.

#### 3.2 — Configurar cache y métricas

```python
metrics = PipelineMetrics()
configure_cache(cache_dir=config.cache_dir, enabled=cache_enabled)
```

Por defecto cache deshabilitado → `get`/`set` son no-op.

#### 3.3 — `[1/5]` Leer commits (`git_reader.py`)

`read_commits(".", "v2.9.0", "v2.10.0")` abre el repo con GitPython, valida que ambos tags existan, itera `repo.iter_commits("v2.9.0..v2.10.0")` y retorna `list[RawCommit]`.

#### 3.4 — Guardias `--dry-run` y `--estimate`

Si alguno está activo, el flujo termina antes de tocar la API. `--estimate` llama a [estimate.py:estimate_pipeline](src/klimbook_release/estimate.py) que reconstruye los prompts reales (incluyendo el changelog del README) y aplica la heurística `chars/3.8`.

#### 3.5 — `[2/6]` Leer detailed changelog del README (`changelog.py`, sin LLM)

```python
changelog_entries = read_changelog(
    Path(config.repo_path) / config.changelog_source,
    sections=tuple(config.changelog_sections),
    count=config.changelog_count,
)
changelog_context = to_context_block(changelog_entries)
```

[changelog.py:read_changelog](src/klimbook_release/changelog.py):

1. Lee el README del proyecto destino (default `{repo_path}/README.md`)
2. Aplica el regex `ENTRY_HEADER` que captura `#### Backend|Frontend|Mobile `vX.Y.Z` — Title (date)`
3. Para cada match extrae el cuerpo markdown completo hasta el siguiente `####` o `---`
4. `get_latest_per_section` filtra las últimas N entradas (default 1) por cada sección configurada
5. `to_context_block` arma un bloque markdown con las entradas seleccionadas y un encabezado que le explica al LLM que es la fuente canónica

**Degrada graceful:** si el README no existe, no tiene la sección, o `config.changelog_enabled = False`, retorna string vacío y el pipeline continúa sin contexto histórico (solo con commits).

**Configurable** vía la sección `changelog:` en `config.yaml`:
```yaml
changelog:
  enabled: true
  source: "README.md"
  sections: ["Backend", "Frontend", "Mobile"]
  count: 1
```

#### 3.6 — `[3/6]` Clasificar con Haiku (`classifier.py`)

```python
classified = classify_commits(commits, config, metrics)
```

[classifier.py:classify_commits](src/klimbook_release/classifier.py):

1. `commits_to_text(commits)` → texto plano `"hash mensaje"` por línea
2. `CLASSIFIER_TASK.format(commits=...)` ([prompts.py](src/klimbook_release/prompts.py))
3. **Retry loop** (3 intentos, temperaturas `[0.3, 0.1, 0.0]`)
4. Llama [utils.call_llm](src/klimbook_release/utils.py) con `prefill="["` para forzar JSON array puro
5. Dentro de `call_llm`:
   - `cache.get(...)` → miss
   - `get_sync_client()` → crea `Anthropic()` la primera vez (lee `$ANTHROPIC_API_KEY`)
   - `client.messages.create(model="claude-haiku-4-5-...", system=..., messages=..., temperature=0.3)`
   - Construye `StepMetrics` y lo agrega a `metrics`
6. `json.loads(raw_text)` → lista de dicts
7. Por cada dict, `CommitEntry(**item)` valida con Pydantic; si uno falla lo salta (no aborta el batch)
8. Retorna `list[CommitEntry]`

#### 3.7 — `[4/6]` Generar markdown con Sonnet (`generator.py`)

```python
notes = generate_notes(classified, version_to, config, metrics,
                       prior_context=changelog_context)
```

1. `classify_to_summary(entries)` agrupa por tipo en orden `feature → fix → refactor → docs → ...`
2. Construye `system` y `prompt` con [prompts.py:GENERATOR_*](src/klimbook_release/prompts.py); si `prior_context` no está vacío, se inserta vía el placeholder `{prior_context}` justo antes de los commits clasificados
3. Retry loop, `call_llm` con Sonnet (T=0.3, max_tokens=4000)
4. Valida ≥100 chars + al menos un `#`
5. Retorna `ReleaseNotes(version, date, markdown, commit_count, categories)`

#### 3.8 — `[5/6]` Formatear en paralelo (`formatter.py`)

```python
outputs = format_all_sync(notes, config, metrics, version_from, version_to)
```

Wrapper sincrono de `asyncio.run(format_all_platforms(...))`.

[formatter.py:format_all_platforms](src/klimbook_release/formatter.py):

1. `config.get_enabled_platforms()` → 5 plataformas
2. `asyncio.Semaphore(config.max_parallel=3)` — limita concurrencia
3. Por cada plataforma crea una coroutine `_format_single_platform(...)`
4. `await asyncio.gather(*tasks, return_exceptions=True)` — corre las 5 en paralelo (fallos no cancelan al resto)

Dentro de cada `_format_single_platform`:

- `_build_formatter_prompt(platform, ...)` selecciona el template (`FORMATTER_GITHUB` / `FORMATTER_PLAYSTORE` / `FORMATTER_APPSTORE` / `FORMATTER_KOFI`); inyecta `GLOSSARY_INSTRUCTION` solo si `language == "es"`
- Temperatura `0.7` para Ko-fi (casual), `0.3` para el resto (formal)
- Retry loop:
  - `cache.get(...)` → miss
  - `async with semaphore:` ← si ya hay 3 requests en vuelo, espera
  - `await aclient.messages.create(...)` — `AsyncAnthropic` se crea en el primer acceso
  - Si excede `max_chars`:
    - Exceso <20% → `_smart_truncate` (corta en último `.` o `\n`)
    - Exceso ≥20% → retry con sufijo `RETRY_TOO_LONG`
- Retorna `PlatformOutput(platform, content, language, max_chars)`

#### 3.9 — `[6/6]` Validar (`validator.py`)

```python
bundle = ReleaseBundle(version, date, commit_count, outputs, metrics=metrics.summary())
validation = validate_bundle(bundle, config)
```

[validator.py:validate_bundle](src/klimbook_release/validator.py) — **sin LLM**:

- `_check_missing_platforms` — error si una plataforma habilitada no tiene output
- Por cada output: vacío, `[ERROR]`, `within_limit`, longitud mínima
- `_validate_github` / `_validate_playstore` / `_validate_kofi` — warnings específicos
- `_check_metadata` — version, date, commit_count > 0

Retorna `ValidationResult(is_valid, issues, summary)`.

#### 3.10 — Escribir a disco

Crea `./releases/v2.10.0/` (pide confirmación si ya existe) y escribe un archivo por plataforma + `bundle.json` con el `ReleaseBundle.model_dump()` completo.

#### 3.11 — Resumen final

[utils.py:print_metrics_summary](src/klimbook_release/utils.py) imprime tokens totales, costo USD y tiempo, con desglose por step. Si el cache estaba activo, también `cache.stats()`.

### Diagrama compacto del flujo

```
shell: klimbook-release generate --from v2.9.0 --to v2.10.0
  │
  ▼
~/envs/agent/bin/klimbook-release  (script autogenerado por pip)
  │ from klimbook_release.cli import app; app()
  ▼
cli.py: Typer parsea args → generate(...)
  │
  ├─► load_config(None)
  │       └─► _resolve_config_path → default_config.yaml empacado
  │       └─► yaml.safe_load → Config(...)  [Pydantic]
  │
  ├─► configure_cache(enabled=False)
  │
  ├─► [1/6] read_commits  ──►  GitPython → list[RawCommit]
  │
  ├─► [2/6] read_changelog  ──►  regex sobre {repo_path}/README.md (sin LLM)
  │                          ──►  to_context_block → markdown para el generator
  │
  ├─► [3/6] classify_commits  ──►  call_llm(Haiku, T=0, prefill="[")
  │                            ──►  json.loads → list[CommitEntry]
  │
  ├─► [4/6] generate_notes    ──►  classify_to_summary + call_llm(Sonnet, T=0.3)
  │                            ──►  prompt incluye {prior_context} (changelog)
  │                            ──►  ReleaseNotes(markdown, categories, ...)
  │
  ├─► [5/6] format_all_sync   ──►  asyncio.run(format_all_platforms(...))
  │                                  │
  │                                  ├─ Semaphore(3)
  │                                  └─ asyncio.gather × 5 plataformas
  │                                       │
  │                                       └─ await aclient.messages.create()
  │                                          + retry/truncate si excede chars
  │
  ├─► [6/6] validate_bundle   ──►  ValidationResult (sin LLM)
  │
  ├─► escribe ./releases/v2.10.0/{*.md, *.txt, bundle.json}
  │
  └─► print_metrics_summary  ──►  tabla de tokens/costo/tiempo
```

### Puntos clave "detrás del telón"

| Dónde | Qué pasa |
|---|---|
| Singletons | `Anthropic()` y `AsyncAnthropic()` se crean **lazy**, la primera vez que se llaman. Leen `$ANTHROPIC_API_KEY` automáticamente |
| Paralelismo | Las 5 plataformas corren concurrentemente pero limitadas a 3 a la vez por `Semaphore` (respeta rate limits del tier API) |
| Métricas | Cada `call_llm` agrega un `StepMetrics` al `PipelineMetrics`. El formatter (async, no usa `call_llm`) registra su propio `StepMetrics` inline por cada coroutine — incluye retries, cache hits y intentos fallidos con un sufijo en `step_name` (`format:github (retry 1)`, `format:kofi [cache]`) |
| Cache | Si está habilitado, `call_llm` y el formatter hacen `cache.get()` antes de la API y devuelven directo si hay hit (con `cost_usd=0.0`) |
| Cadena de búsqueda de config | El tool funciona desde cualquier CWD porque `default_config.yaml` viaja empacado como package-data |
| Fallos en paralelo | `asyncio.gather(return_exceptions=True)` → si una plataforma revienta, las demás siguen. El output queda como `[ERROR] ...` y el validator lo detecta |

---

## Módulos en detalle

### `cli.py` — Entry point (Typer)

Define la app `klimbook-release` con tres comandos. Logging configurado a nivel `INFO` con timestamp `%H:%M:%S`. El comando `generate` orquesta el pipeline completo en 5 fases:

```
[1/5] Leyendo commits...           (git_reader)
[2/5] Clasificando commits...      (classifier + Haiku)
[3/5] Generando release notes...   (generator + Sonnet)
[4/5] Formateando para N plat...   (formatter + Sonnet, paralelo)
[5/5] Validando...                 (validator, sin LLM)
```

Cada plataforma se guarda en su propio archivo (`github.md` para GitHub, `<platform>.txt` para el resto) más un `bundle.json` con la metadata completa (versión, fecha, commit_count, outputs serializados, métricas). Si el directorio del release ya existe, pide confirmación antes de sobrescribir. Si la validación falla, los archivos se guardan igualmente para inspección manual y el CLI muestra los errores antes de salir.

| Comando | Descripción |
|---|---|
| `generate --from <tag> --to <tag>` | Pipeline completo. Opciones: `--platforms` (filtro CSV — deshabilita todas las no listadas), `--output` (default `./releases`), `--config` (default `config.yaml`), `--dry-run` (lista commits sin gastar tokens) |
| `tags [--repo .] [--last 20]` | Lista los últimos N tags del repo y sugiere el comando `generate` con los dos más recientes |
| `config [--config config.yaml]` | Imprime configuración: modelos con su temperatura, retries, paralelismo, plataformas habilitadas (con límites), tamaño y muestra del glosario |

### `config.py` — Carga de configuración

| Modelo | Campos clave |
|---|---|
| `PlatformConfig` | `enabled` (bool), `language` (en/es), `max_chars` (int \| None), `description` (str) |
| `PricingConfig` | `input` y `output` (USD por millón de tokens) |
| `Config` | Proyecto, modelos por paso, temperaturas, retry, concurrencia, dict de plataformas, glosario, dict de pricing |

**Funciones:**
- `load_config(path)` — Lee YAML, parsea cada sección, retorna `Config` validado. Si el archivo no existe o está vacío, retorna `_default_config()` (Klimbook con 5 plataformas).
- `Config.get_enabled_platforms()` — Filtra plataformas con `enabled=True`.
- `Config.get_pricing(model)` — Retorna pricing del modelo (default Sonnet si no está configurado).
- `Config.calculate_cost(model, in_tokens, out_tokens)` — Calcula costo en USD: `(in/1e6)*p.input + (out/1e6)*p.output`.

### `git_reader.py` — Lectura de commits (sin LLM)

GitPython como wrapper de Git para evitar `subprocess.run(["git", ...])` (sin riesgo de shell injection).

| Función | Descripción |
|---|---|
| `read_commits(repo_path, version_from, version_to)` | Abre el repo, valida que ambos tags existan (si no, error con los últimos 15 tags disponibles), itera `version_from..version_to` (sintaxis Git: commits que están en `to` pero no en `from`), y retorna `list[RawCommit]` con hash corto/largo, primera línea del mensaje, autor, fecha ISO 8601, y `files_changed`. |
| `commits_to_text(commits)` | Convierte la lista a texto plano `"<hash> <message>"` por línea (formato `git log --oneline`), para alimentar al clasificador. |
| `list_tags(repo_path)` | Retorna todos los tags ordenados; usado por el comando `tags` y como sugerencia. |
| `get_latest_tags(repo_path, n=2)` | Retorna los N tags más recientes; útil como default cuando el usuario no especifica. |

**Manejo de errores:** `InvalidGitRepositoryError`, `GitCommandNotFound`, repo `bare`, tag inexistente — todos producen mensajes accionables al usuario.

### `classifier.py` — Clasificación con Haiku

| Función | Descripción |
|---|---|
| `classify_commits(commits, config, metrics)` | Convierte commits a texto, los envía a Haiku con `prefill="["` para forzar JSON array, parsea la respuesta, valida cada item con `CommitEntry`. Si un item individual falla validación lo salta (no aborta el batch); si ningún item pasa, lanza `ValueError`. Retorna `list[CommitEntry]` |
| `classify_to_summary(entries)` | Agrupa los entries por tipo (orden: feature → fix → refactor → docs → chore → test → ci) y devuelve markdown estructurado: `### TYPE (N)` + bullets con `(service)` y `[BREAKING]` cuando aplica. Es lo que recibe el generator |

**Retry loop:**
- `max_retries` (default 3) intentos
- `temperatures` configurable (default `[0.3, 0.1, 0.0]`); si la lista es más corta que `max_retries`, completa con `0.0`
- En retries, agrega `RETRY_JSON_INVALID` al prompt para que Claude sepa qué corregir
- Captura `JSONDecodeError`, `ValueError`, `ValidationError` por separado y marca el `StepMetrics` como `success=False`
- Si todos fallan, lanza `RuntimeError` con el último error

**Logging:** muestra distribución por tipo al final (`{"feature": 3, "fix": 5, ...}`).

### `generator.py` — Redacción de notas con Sonnet

| Función | Descripción |
|---|---|
| `generate_notes(entries, version, config, metrics, date)` | Recibe `list[CommitEntry]`, agrupa con `classify_to_summary()`, sustituye `{project_name}`, `{version}`, `{date}`, `{changes}` en `GENERATOR_TASK`, llama a Sonnet (T=0.3), valida que el markdown tenga ≥100 chars y al menos un `#`. Retorna `ReleaseNotes` con `commit_count` y dict de `categories` (conteo por tipo) |
| `_today()` | Helper: fecha actual en formato `"April 13, 2026"` |

**Casos especiales:**
- Lista vacía de entries → retorna `ReleaseNotes` con markdown placeholder `"No changes in this release."` (no llama a la API)
- Si la fecha no se pasa, usa `datetime.now().strftime("%B %d, %Y")`

**Retry loop:** misma estrategia que classifier, pero usa `RETRY_VALIDATION_FAILED` en lugar de `RETRY_JSON_INVALID`.

### `formatter.py` — Formateo paralelo con AsyncAnthropic

Componente más complejo del pipeline. Usa `AsyncAnthropic` + `asyncio.gather` + `asyncio.Semaphore(max_parallel)` para formatear las N plataformas simultáneamente respetando rate limits.

| Función | Descripción |
|---|---|
| `format_all_platforms(notes, config, metrics, version_from, version_to)` | **Coroutine principal.** Crea una task `_format_single_platform` por plataforma habilitada, las ejecuta con `asyncio.gather(*, return_exceptions=True)` (un fallo no cancela las demás), agrupa resultados en `dict[str, PlatformOutput]`. Si una plataforma falla con excepción, su output queda como `[ERROR] {mensaje}` |
| `format_all_sync(...)` | Wrapper síncrono con `asyncio.run()` para uso desde el CLI |
| `_format_single_platform(...)` | Formatea UNA plataforma con retry. Selecciona temperatura: `formatter_casual_temp` para Ko-fi, `formatter_formal_temp` para el resto. En cada retry baja la temperatura `0.15` por intento |
| `_build_formatter_prompt(platform, ...)` | Selecciona el template correcto (`FORMATTER_GITHUB`/`PLAYSTORE`/`APPSTORE`/`KOFI`) según la plataforma, sustituye variables (`max_chars`, `language`, `version_from/to`). Inyecta `GLOSSARY_INSTRUCTION` solo si `language == "es"`. Si la plataforma es desconocida, usa un prompt genérico |
| `_smart_truncate(text, max_chars)` | Trunca texto en el último `.` o `\n` que quepa. Si el corte dejaría < 50% del contenido, retorna `None` (mejor hacer retry completo que devolver algo cortado a la mitad) |

**Estrategia de límites de chars:**
1. Si Sonnet devuelve contenido dentro del límite → OK.
2. Si excede pero por **< 20%** → intenta `_smart_truncate` (ahorra tokens vs. nuevo retry).
3. Si excede por **≥ 20%** → retry completo agregando `RETRY_TOO_LONG` con `actual_chars` y `max_chars` para que Claude sepa cuánto recortar.

**Concurrencia:** `Semaphore(config.max_parallel)` envuelve cada llamada a la API. Con `max_parallel=3` y 5 plataformas, las primeras 3 corren simultáneas y las otras 2 esperan en cola.

**Cliente:** singleton `_get_async_client()` retorna `AsyncAnthropic()` (separado del sync client de `utils.py`).

### `validator.py` — Validación sin LLM

Pydantic + regex. **No usa Claude.** Verifica que el bundle final cumpla las reglas de cada plataforma antes de guardar/publicar.

| Modelo / Función | Descripción |
|---|---|
| `ValidationIssue` (dataclass) | `platform`, `severity` (`"error"` o `"warning"`), `message` |
| `ValidationResult` (dataclass) | `is_valid` (bool: `True` si no hay errors), `issues` (lista), `summary` (dict por plataforma con chars/limit/within_limit/errors/warnings). Properties `errors` y `warnings` filtran por severidad |
| `validate_bundle(bundle, config)` | Orquesta todas las validaciones y devuelve `ValidationResult`. Loguea OK/FALLO con conteo |
| `_check_missing_platforms(bundle, config)` | Error si una plataforma habilitada en config no aparece en `bundle.outputs` |
| `_validate_platform_output(name, output, config)` | Por cada plataforma: (a) error si vacío, (b) error si empieza con `[ERROR]` (formatter falló), (c) error si excede `max_chars`, (d) warning si < 20 chars (probablemente incompleto), (e) llama al validator específico |
| `_validate_github(output)` | Warning si no contiene `#` (sin headers) o si no contiene líneas con `-` (sin listas) |
| `_validate_playstore(name, output)` | Warning si empieza con `#` o contiene `**`/`[` (Play Store no renderiza markdown complejo) |
| `_validate_kofi(output)` | Warning si no contiene `"zxxz6"` (falta firma) o si empieza con `#` (Ko-fi es texto plano) |
| `_check_metadata(bundle)` | Error si falta `version` o `date`. Warning si `commit_count == 0` |
| `print_validation_result(result)` | Imprime resultado legible: `PASSED`/`FAILED`, errores, warnings, summary por plataforma |

Errors bloquean (`is_valid=False` y el CLI lo reporta), warnings son informativos.

### `models.py` — Modelos Pydantic compartidos

Definen el contrato entre todos los pasos del pipeline.

| Modelo | Producido por | Consumido por |
|---|---|---|
| `RawCommit` | `git_reader` | `classifier` |
| `CommitEntry` | `classifier` | `generator` |
| `ReleaseNotes` | `generator` | `formatter` |
| `PlatformOutput` | `formatter` | `validator` / `ReleaseBundle` |
| `ReleaseBundle` | pipeline (output final) | CLI → archivos en disco |
| `StepMetrics`, `PipelineMetrics` | cada paso vía `utils.call_llm` | `utils.print_metrics_summary` |

**Validators clave:**
- `CommitEntry.type` — Restringido a `Literal["feature", "fix", "refactor", "docs", "chore", "test", "ci"]`. Cualquier otro tipo lanza `ValidationError`.
- `CommitEntry.description` — No puede estar vacío ni ser solo whitespace.
- `ReleaseNotes.markdown` — Mínimo 50 caracteres.
- `PlatformOutput.content` — No vacío; expone `char_count` y `within_limit` como `computed_field`.

**Constante `PLATFORM_LIMITS`:**

| Plataforma | Límite | Origen del límite |
|---|---|---|
| `github` | sin límite | GitHub Releases |
| `playstore_en` | 500 chars | Google Play "What's New" (límite duro) |
| `playstore_es` | 500 chars | Google Play "What's New" (límite duro) |
| `appstore` | 4000 chars | Apple App Store "What's New" |
| `kofi` | 2000 chars | Convención propia (Ko-fi no tiene límite estricto) |

**`ReleaseBundle`** expone:
- `get_output(platform)` — Retorna el `PlatformOutput` de una plataforma específica.
- `all_within_limits()` — `True` si todos los outputs respetan su límite.
- `validation_summary()` — Dict `{platform: {chars, limit, ok}}`.

**`PipelineMetrics`** acumula `StepMetrics` y expone `summary()` con totales (tokens, costo USD redondeado a 6 decimales, segundos).

### `prompts.py` — Prompts centralizados

Todos los prompts viven aquí para iterar rápido y versionarlos con Git (`git diff prompts.py` muestra exactamente qué cambió).

| Constante | Uso |
|---|---|
| `CLASSIFIER_SYSTEM` + `CLASSIFIER_TASK` | Define las 7 categorías y reglas (Conventional Commits prefix → categoría, scope `feat(auth)` → `affected_service`, marca `breaking` solo si dice "BREAKING CHANGE"). Exige JSON array puro como respuesta. |
| `GENERATOR_SYSTEM` + `GENERATOR_TASK` | Tono de technical writer para climbers. Estructura fija: summary → What's New (Features / Bug Fixes / Improvements / Other). Solo incluye secciones con cambios. Items de 1-2 oraciones. Menciona servicios entre paréntesis. |
| `FORMATTER_GITHUB` | Mantiene markdown completo + agrega "Full Changelog" con URL `compare/{from}...{to}` |
| `FORMATTER_PLAYSTORE` | Reglas estrictas: ≤500 chars, bullets cortos, lenguaje user-facing, sin markdown, sin jerga técnica. Respeta `language` y `glossary_instruction` |
| `FORMATTER_APPSTORE` | ≤4000 chars, lenguaje limpio, párrafos cortos, profesional pero accesible |
| `FORMATTER_KOFI` | Voz en primera persona ("eres zxxz6, solo developer de Klimbook, climber en Puebla, México"). Tono casual, agradecido. Estructura: saludo → trabajo de la semana → highlights → agradecimiento → call-to-action → firma `— zxxz6` |
| `GLOSSARY_INSTRUCTION` | Inyectada en prompts en español: "no traduzcas estos términos: {terms}" |
| `RETRY_JSON_INVALID`, `RETRY_TOO_LONG`, `RETRY_VALIDATION_FAILED` | Sufijos que se agregan al prompt cuando un paso falla, indicando exactamente qué corregir |
| `EXAMPLE_GITHUB`, `EXAMPLE_PLAYSTORE_EN`, `EXAMPLE_PLAYSTORE_ES`, `EXAMPLE_APPSTORE`, `EXAMPLE_KOFI` | Releases reales anteriores (v2.10.0) usados como **few-shot prompting**. Se inyectan en cada `FORMATTER_*` vía el placeholder `{example}`. El LLM los trata como referencia de **estilo** (tono, estructura, longitud, giros lexicos) — no de contenido. El prompt lo dice explícito para evitar copy-paste |

**Few-shot y playstore bilingüe:** `formatter._build_formatter_prompt` selecciona `EXAMPLE_PLAYSTORE_ES` cuando `platform_config.language == "es"` y `EXAMPLE_PLAYSTORE_EN` en caso contrario. Así el modelo ve un ejemplo en el idioma que debe producir.

**Trade-off de tokens:** añadir los ejemplos cuesta ~1,500 input tokens distribuidos en los 5 formatters (~$0.005 por release completo, ~5% del costo total). A cambio se gana consistencia de voz y estructura respecto a releases anteriores.

### `utils.py` — Cliente API + retry + métricas

**Singletons de cliente:**
- `get_sync_client()` → `Anthropic` (uso desde el CLI / `call_llm`)
- `get_async_client()` → `AsyncAnthropic` (uso interno; `formatter.py` mantiene su propio singleton equivalente)

Ambos leen `ANTHROPIC_API_KEY` del entorno automáticamente.

**`call_llm(model, system, prompt, temperature, max_tokens, prefill, config, step_name, metrics)`:**
1. Construye `messages` (con `prefill` opcional como turno de assistant para forzar formato — usado por `classifier` con `prefill="["`).
2. Llama a `client.messages.create(...)` y mide tiempo.
3. Extrae texto, calcula costo vía `config.calculate_cost(...)`.
4. Crea `StepMetrics` y lo agrega al `PipelineMetrics` global si se pasó.
5. Retorna `(texto, StepMetrics)` y loguea tokens/costo/tiempo.

**`@retry_on_error(max_retries=3, temperatures=[0.3, 0.1, 0.0], retry_prompt_extra="")`:**
Decorator que reintenta una función bajando la temperatura en cada intento. Captura `JSONDecodeError`, `ValueError` y `Exception`. Si todos los intentos fallan, propaga la última excepción. (En la práctica, `classifier`/`generator`/`formatter` implementan su propio retry loop más expresivo; este decorator queda como utilidad genérica.)

**Helpers de output:** `print_step_result(name, content, max_preview)`, `print_metrics_summary(metrics)` con desglose por step (tokens, costo, tiempo, retries, status OK/FAIL).

### `changelog.py` — Parser regex del README destino (sin LLM)

Lee el README del proyecto (Klimbook) y extrae la sección "Detailed Changelog" para inyectarla como contexto al generator. **No usa LLM** — regex puro sobre los encabezados markdown estándar.

| Función / objeto | Descripción |
|---|---|
| `ENTRY_HEADER` | Regex que captura `#### Backend\|Frontend\|Mobile `vX.Y.Z` — Title (date)` con grupos nombrados (acepta em dash `—` o guión `-`) |
| `ChangelogEntry` | Dataclass: `section`, `version`, `title`, `date`, `body` (markdown crudo del cuerpo). Property `heading` reconstruye el encabezado original; `to_markdown()` devuelve heading + body |
| `parse_changelog(text)` | Aplica `ENTRY_HEADER`, separa los cuerpos cortando en el siguiente `####` o `---`, retorna `list[ChangelogEntry]` |
| `get_latest_per_section(entries, sections, count)` | Filtra las primeras N entries por cada sección configurada, en el orden `sections` |
| `read_changelog(path, sections, count)` | Combina lectura de archivo + parse + filter. Degrada graceful: archivo inexistente o sin entradas → `[]` con warning |
| `to_context_block(entries)` | Renderiza la lista a markdown con un encabezado que le explica al LLM que es la fuente canónica del release |

**Configurable** vía la sección `changelog:` del YAML (`enabled`, `source`, `sections`, `count`).

**Convención asumida:** el README está ordenado de más reciente a más antiguo. "Latest" = la primera entrada de cada sección.

### `cache.py` — Cache de respuestas en disco

Cache SHA-256-keyed para iterar prompts sin volver a gastar tokens. Transparente: cuando está habilitado, `call_llm` (sync) y `_format_single_platform` (async) consultan el cache **antes** de llamar a la API; si hay hit devuelven el texto guardado con `cost_usd=0.0`.

| Clase / función | Descripción |
|---|---|
| `CachedResponse` | Dataclass: `text`, `input_tokens`, `output_tokens`, `model` |
| `ResponseCache(cache_dir, enabled)` | Cache backed por JSON en disco. Si `enabled=False` todas las operaciones son no-op |
| `ResponseCache._key(...)` | SHA-256 truncado a 24 hex sobre `(model, system, prompt, temperature, max_tokens, prefill)`. Orden estable garantizado por `json.dumps(sort_keys=True)` |
| `ResponseCache.get(...)` / `.set(...)` | Round-trip sobre `{key}.json`; incrementa `.hits`/`.misses` |
| `ResponseCache.clear()` | Borra todo el directorio. Retorna cuántos archivos eliminó |
| `ResponseCache.stats()` | Dict con `enabled`, `dir`, `entries`, `hits`, `misses`, `bytes` |
| `configure_cache(cache_dir, enabled)` | Reconfigura el singleton global. La llama el CLI al inicio |
| `get_cache()` | Devuelve el singleton (deshabilitado por default) |

**Política:** sin TTL. Si cambiaste un prompt o quieres respuestas frescas, corre con `--no-cache` o borra `.cache/` manualmente.

### `estimate.py` — Estimador de tokens y costo (sin API)

Cuenta tokens y calcula costo usando una heurística de chars/token sin tocar Anthropic. Pensado para responder "¿este release va a costar $0.10 o $10?" antes de gastar nada.

| Función / objeto | Descripción |
|---|---|
| `CHARS_PER_TOKEN = 3.8` | Calibración para Claude con mezcla de inglés + JSON + markdown |
| `estimate_tokens(text)` | `ceil(len(text) / 3.8)`. Devuelve 0 para texto vacío |
| `StepEstimate` | Dataclass por paso: `step_name`, `model`, `input_tokens`, `output_tokens`, `cost_usd` |
| `PipelineEstimate` | Agregador con `.steps`, `.commit_count`, `.platform_count`, y propiedades `total_*` |
| `estimate_pipeline(commits, config, version)` | Reconstruye los prompts exactos de classifier/generator/formatter, estima tokens y multiplica por `Config.pricing`. **No llama a la API** |
| `print_estimate(est)` | Pretty-printer con totales y desglose por step |

**Heurística de output por paso:**
- Classifier: `55 tokens × len(commits)` (JSON array pequeño)
- Generator: `250 + 35 × len(commits)`, cap a `max_tokens=4000`
- Formatter: `max_chars × 0.85 / 3.8` si hay límite; si no, `1500 tokens` (GitHub)

**Precisión:** ±20% vs. uso real. Suficiente para detectar órdenes de magnitud.

---

## Configuración (`config.yaml`)

El archivo raíz `config.yaml` controla el comportamiento del pipeline sin tocar código. Secciones:

| Sección | Contenido |
|---|---|
| `project` | `name`, `repo_path`, `default_branch` |
| `models` | Modelo por paso (`classifier`, `generator`, `formatter`) |
| `temperatures` | Temperatura por paso (`classifier`, `generator`, `formatter_formal`, `formatter_casual`) |
| `retry` | `max_retries: 3`, `temperatures: [0.3, 0.1, 0.0]` |
| `concurrency` | `max_parallel: 3` (respeta el rate limit de tu tier API) |
| `cache` | `enabled: false`, `dir: ".cache"`. Al habilitarse, respuestas idénticas se reutilizan |
| `changelog` | `enabled: true`, `source: "README.md"`, `sections: [Backend, Frontend, Mobile]`, `count: 1`. Inyecta el detailed changelog del README destino al generator como contexto |
| `platforms` | Por plataforma: `enabled`, `language`, `max_chars`, `description` |
| `glossary` | Lista de ~26 términos de escalada (boulder, redpoint, beta, crux, dyno, anchor, quickdraw, top-rope, etc.) que NO se traducen al español |
| `pricing` | Pricing por modelo (USD/millón de tokens) para calcular costo estimado |

**Plataformas habilitadas por defecto:** `github`, `playstore_en`, `playstore_es`, `appstore`, `kofi`.

Si `config.yaml` no existe, `load_config()` retorna `_default_config()` con las mismas 5 plataformas y un glosario reducido (10 términos) para que el tool funcione out-of-the-box.

---

## Instalación

Requiere **Python ≥ 3.11**.

```bash
# Clonar e instalar en modo desarrollo
git clone https://github.com/zxxz456/klimbook-release
cd klimbook-release
pip install -e ".[dev]"

# Configurar API key de Anthropic
export ANTHROPIC_API_KEY="sk-ant-..."

# Verificar instalación
klimbook-release --help
klimbook-release config
```

---

## Uso

```bash
# Generar release notes para todas las plataformas habilitadas
klimbook-release generate --from v2.8.0 --to v2.9.0

# Solo plataformas específicas (las demás se deshabilitan en runtime)
klimbook-release generate --from v2.8.0 --to v2.9.0 --platforms github,kofi

# Custom output dir y config
klimbook-release generate --from v2.8.0 --to v2.9.0 \
    --output ./my-releases \
    --config ./prod.yaml

# Dry run: muestra los commits sin gastar tokens
klimbook-release generate --from v2.8.0 --to v2.9.0 --dry-run

# Estimate: calcula tokens y costo previstos sin llamar a la API
klimbook-release generate --from v2.8.0 --to v2.9.0 --estimate

# Habilitar cache (reutiliza respuestas idénticas de corridas previas)
klimbook-release generate --from v2.8.0 --to v2.9.0 --use-cache

# Forzar llamadas frescas ignorando cualquier cache configurado
klimbook-release generate --from v2.8.0 --to v2.9.0 --no-cache

# Listar tags disponibles (sugiere comando con los 2 más recientes)
klimbook-release tags
klimbook-release tags --last 50

# Ver configuración cargada
klimbook-release config

# Inspeccionar el detailed changelog que se inyectaría al generator
# (sin correr el pipeline, sin gastar tokens)
klimbook-release changelog
klimbook-release changelog --sections Backend --count 2
klimbook-release changelog --source /otro/README.md --raw
```

**Output esperado en disco:**

```
./releases/v2.9.0/
├── github.md            # Markdown completo con "Full Changelog" link
├── playstore_en.txt     # ≤500 chars, bullets, user-facing
├── playstore_es.txt     # ≤500 chars, traducido (glosario respetado)
├── appstore.txt         # ≤4000 chars, formato Apple
├── kofi.txt             # Post casual con firma "— zxxz6"
└── bundle.json          # Bundle completo (metadata + outputs + métricas)
```

**Output esperado en consola** (resumen):

```
============================================================
  Klimbook Release Notes Generator
  v2.8.0 -> v2.9.0
  Plataformas: github, playstore_en, playstore_es, appstore, kofi
============================================================

[1/5] Leyendo commits...
  27 commits encontrados

[2/5] Clasificando commits con claude-haiku-4-5-20251001...
  27 commits clasificados
    feature: 8
    fix: 12
    refactor: 4
    chore: 3

[3/5] Generando release notes con claude-sonnet-4-20250514...
  Markdown generado: 1842 chars

[4/5] Formateando para 5 plataformas...
  github:       3104 chars [OK]
  playstore_en: 487/500 chars [OK]
  playstore_es: 492/500 chars [OK]
  appstore:     2156/4000 chars [OK]
  kofi:         1378/2000 chars [OK]

[5/5] Validando...
============================================================
  Validation: PASSED
============================================================
  ...
============================================================
  Pipeline Metrics
============================================================
  Steps:          7
  Input tokens:   10,234
  Output tokens:  5,128
  Total tokens:   15,362
  Cost:           $0.103450
  Time:           28.34s
============================================================

  Release notes guardadas en: ./releases/v2.9.0/
  Listas para publicar.
```

---

## kbkro — Variante con Ollama (local, gratis)

`kbkro` (*klimbook release ollama*) es un segundo CLI que corre el **mismo pipeline** pero contra un servidor Ollama local en vez de la API de Anthropic. Sin API key, sin costo monetario, sin enviar el código a terceros.

**Requisitos:**
- [Ollama](https://ollama.com/) corriendo localmente (`ollama serve`)
- Al menos un modelo instalado: `ollama pull gemma4:26b` (o el que prefieras: `llama3.3:70b`, `qwen2.5:14b`, `gemma4:31b`, etc.)

### Cómo funciona (arquitectura del shim)

`kbkro` **no duplica** el pipeline. El trabajo pesado lo hace `klimbook_release` tal cual — solo se sustituyen los clientes de Anthropic con *shims* que tienen la misma interfaz pero hablan HTTP con Ollama. Así, `classifier`, `generator`, `formatter`, cache, retry, métricas y validación se reutilizan sin modificar.

```
┌─────────────────────────────────────────────────────────────────┐
│  kbkro generate --from v2.8.0 --to v2.9.0 --model gemma4:26b    │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ▼
       ┌─────────────────────────────────────────────┐
       │  kbkro/cli.py — _install_ollama()           │
       │  1. Crea OllamaSyncShim + OllamaAsyncShim   │
       │  2. Monkey-patch:                           │
       │       utils._sync_client  = sync_shim       │
       │       utils._async_client = async_shim     │
       │       formatter._async_client = async_shim  │
       │  3. Fuerza los 3 models = --model           │
       │  4. pricing[model] = (0, 0)   ← $0 USD      │
       └──────────────────┬──────────────────────────┘
                          │ el pipeline de klimbook_release
                          │ sigue sin saber que habla con Ollama
                          ▼
       ┌─────────────────────────────────────────────┐
       │  [1-2/5] git_reader + changelog (sin LLM)   │
       └──────────────────┬──────────────────────────┘
                          ▼
       ┌─────────────────────────────────────────────┐
       │  [3/5] classifier.classify_commits()        │
       │  call_llm(..., prefill="[", max_tokens=16k) │
       │  → cache lookup                             │
       │  → sync_shim.messages.create(...)           │
       │    (duck-typed como Anthropic)              │
       └──────────────────┬──────────────────────────┘
                          ▼
       ┌─────────────────────────────────────────────┐
       │  OllamaSyncShim._SyncMessages.create()      │
       │  1. _build_payload:                         │
       │     {model, messages, stream:false,         │
       │      think:false,  ← desactiva reasoning    │
       │      options:{temperature, num_predict}}    │
       │  2. POST http://localhost:11434/api/chat    │
       │  3. _parse(response, prefill):              │
       │     - strip del prefill si Ollama lo repite │
       │     - log de thinking separado (si hubo)    │
       │     - warn si done_reason != "stop"         │
       │  4. Devuelve _Response duck-typed           │
       │     (.content[0].text + .usage.*)           │
       └──────────────────┬──────────────────────────┘
                          ▼
       ┌─────────────────────────────────────────────┐
       │  call_llm (post-provider):                  │
       │    text = prefill + response.content[0].text│
       │    cache.set(...)                           │
       │    metrics.add(StepMetrics($0.00))          │
       └──────────────────┬──────────────────────────┘
                          ▼
       ┌─────────────────────────────────────────────┐
       │  [4/5] generator → mismo camino via shim    │
       │  [5/5] formatter × N en paralelo (async     │
       │        shim con httpx.AsyncClient)          │
       │  Retry + smart_truncate tal cual            │
       └──────────────────┬──────────────────────────┘
                          ▼
       ┌─────────────────────────────────────────────┐
       │  releases/v2.9.0/{github.md,.txt, ...}     │
       │  Cost: $0.000000                            │
       └─────────────────────────────────────────────┘
```

### Reglas de compatibilidad del shim

Para que Ollama se vea exactamente como Claude por fuera, el shim compensa varias diferencias:

| Claude (Anthropic) | Ollama | Shim compensa |
|---|---|---|
| Prefill: cuando el último `message` es `role="assistant"`, Claude **continúa** desde ese texto sin repetirlo | Algunos modelos (gemma4, qwen3) **repiten** el prefill al inicio de `message.content` | `_parse()` detecta el prefill y lo strip-ea si aparece duplicado |
| `response.usage.input_tokens` / `output_tokens` | `prompt_eval_count` / `eval_count` | Wrapper los renombra |
| `response.content[0].text` | `message.content` | `_Response` + `_ContentBlock` dataclasses |
| Modelo responde directo al prompt | Modelos reasoning (gemma4, qwen3) gastan num_predict en `message.thinking` | Pasa `"think": false` en el payload + log separado si aparece thinking |
| `response.stop_reason == "end_turn"` | `done_reason: "stop"` normalmente; `"length"` si se truncó | Warn explícito si `done_reason != "stop"` para alertar de truncación |

### Instalación

```bash
# Clonar e instalar en modo desarrollo (instala ambos CLIs)
git clone https://github.com/zxxz456/klimbook-release.git
cd klimbook-release
pip install -e .

# Verificar que los dos comandos están disponibles
klimbook-release --help
kbkro --help

# Arrancar Ollama y bajar un modelo
ollama serve &
ollama pull gemma4:26b
```

### Uso

```bash
# Modelo por defecto: gemma4:26b, host localhost:11434
kbkro generate --from v2.8.0 --to v2.9.0

# Especificar modelo
kbkro generate --from v2.8.0 --to v2.9.0 --model gemma4:31b
kbkro generate --from v2.8.0 --to v2.9.0 --model llama3.3:70b
kbkro generate --from v2.8.0 --to v2.9.0 --model qwen2.5:14b-instruct

# Host remoto de Ollama (otra máquina de la red)
kbkro generate --from v2.8.0 --to v2.9.0 --host http://192.168.1.50:11434

# Solo algunas plataformas (útil para iterar rápido con modelos locales lentos)
kbkro generate --from v2.8.0 --to v2.9.0 --platforms github

# Dry run (lista commits sin llamar a Ollama)
kbkro generate --from v2.8.0 --to v2.9.0 --dry-run

# Verbose: vuelca respuestas completas de Ollama + separación content/thinking
kbkro generate --from v2.8.0 --to v2.9.0 --verbose

# Output custom
kbkro generate --from v2.8.0 --to v2.9.0 -o ~/Documents/notes

# Cache (igual que klimbook-release)
kbkro generate --from v2.8.0 --to v2.9.0 --use-cache

# Inspeccionar config tal como quedaría
kbkro config --model gemma4:26b

# Listar tags del repo
kbkro tags
```

### Output esperado

Idéntico a `klimbook-release` en estructura, pero con `Cost: $0.000000` y modelo local en cada paso:

```
============================================================
  Klimbook Release Notes (Ollama)
  Model:       gemma4:26b
  Host:        http://localhost:11434
  v2.8.0 -> v2.9.0
  Plataformas: github, playstore_en, playstore_es, appstore, kofi
============================================================

[1/5] Leyendo commits...
  14 commits encontrados

[2/5] Leyendo detailed changelog del README...
  • Backend v2.11.0 — Notification Deep-Linking + New Reward Notifications
  • Frontend v2.11.0 — Notification Deep-Linking
  • Mobile v2.11.0 — Notification Deep-Linking

[3/5] Clasificando commits con gemma4:26b...
  14 commits clasificados
    docs: 6 | feature: 3 | fix: 2 | refactor: 1 | test: 1 | ...

[4/5] Generando notas y formateando plataformas...
  Markdown generado: 2134 chars
  github:       3102 chars [OK]
  playstore_en: 481/500 chars [OK]
  playstore_es: 495/500 chars [OK]
  appstore:     2201/4000 chars [OK]
  kofi:         1402/2000 chars [OK]

[5/5] Validando y guardando...
============================================================
  Validation: PASSED
============================================================
  Cost:          $0.000000
  Time:          312.48s
```

### Notas prácticas

- **`num_predict` alto ≠ costo**: a diferencia de Claude, en Ollama pagas en tiempo de cómputo, no por token. Los caps de `classifier` y `generator` están en **16000** para dar margen a modelos verbosos sin riesgo de JSON truncado.
- **Concurrencia**: formatear 5 plataformas en paralelo con `gemma4:26b` hace sufrir a tu GPU. Si se cuelga, bájale: en `config.yaml` → `concurrency.max_parallel: 1`.
- **Elegir modelo**: modelos reasoning tipo `gemma4`/`qwen3` son más verbosos. Para un pipeline más rápido prueba `llama3.2:3b` o `qwen2.5:14b` (siguen JSON mejor). Para calidad de redacción, `gemma4:31b` o `llama3.3:70b`.
- **Streaming no**: el shim usa `stream: false` porque el pipeline necesita el texto completo antes de validar JSON/markdown/longitud.
- **Costo reportado**: el pipeline sigue sumando tokens en `PipelineMetrics`, pero `pricing[model] = (0, 0)` hace que el costo final siempre dé `$0.000000`.

---

## Testing

```bash
# Todos los tests (no requieren API key ni internet)
pytest -v

# Un solo archivo
pytest tests/test_models.py -v

# Un solo test
pytest tests/test_models.py::TestCommitEntry::test_valid_entry -v

# Con output de print() visible
pytest -v -s
```

**Cobertura actual:**

| Archivo | Alcance |
|---|---|
| `test_models.py` | Pydantic models + Config: tipos, defaults, validators, `calculate_cost`, `PipelineMetrics` |
| `test_classifier_mock.py` | Classifier con `MockAnthropic`: happy path, retry en JSON inválido, skip de entries con `type` inválido, `classify_to_summary` |
| `test_generator_mock.py` | Generator con `MockAnthropic`: happy path, retry en markdown corto, empty entries (sin API call), category counts |
| `test_formatter_mock.py` | Formatter con `MockAsyncAnthropic`: paralelo sobre 2 plataformas, `_smart_truncate` en overshoot <20%, retry en overshoot ≥20%, `format_all_sync` |
| `test_cache.py` | `ResponseCache`: disabled=no-op, round-trip, key determinista, temperatura cambia key, `clear`, `call_llm` hace cache hit |
| `test_changelog.py` | Parser regex: parse simple/multi-sección, dividers, `get_latest_per_section`, `read_changelog` end-to-end con tmp_path, `to_context_block`, edge cases |
| `test_estimate.py` | Estimador: `estimate_tokens` escala lineal, shape del `PipelineEstimate`, Haiku < Sonnet por token, **cero API calls** |

**Fixtures en `conftest.py`:**
- `mock_anthropic` — Instala `MockAnthropic` (sync) y `MockAsyncAnthropic` (async) que comparten estado. Sustituye los singletons en `utils` y `formatter` vía `monkeypatch`.
- `sample_commits` — 3 `RawCommit` representativos (feat/fix/docs).
- `base_config` — `Config` mínimo con 2 plataformas (github, playstore_en), glosario corto, pricing Haiku + Sonnet.
- `tmp_cache` — `ResponseCache` apuntando a `tmp_path`.

**`pytest-asyncio` en modo `auto`** (configurado en `pyproject.toml` bajo `[tool.pytest.ini_options]`), así no hace falta decorar cada test async con `@pytest.mark.asyncio`.

---

## Costo

Estimación por release típico (~20-30 commits):

| Paso | Modelo | Tokens in | Tokens out | Costo aprox. |
|---|---|---|---|---|
| Classify | Haiku 4.5 | ~1,500 | ~800 | $0.005 |
| Generate | Sonnet 4 | ~1,200 | ~1,500 | $0.026 |
| Format ×5 | Sonnet 4 | ~7,500 | ~3,000 | $0.068 |
| **Total** | | ~10,200 | ~5,300 | **~$0.10** |

Pricing (USD por millón de tokens) en `config.yaml` y usado por `Config.calculate_cost()`. Actualízalo cuando Anthropic ajuste precios. El resumen de métricas se imprime al final de cada `generate` con el costo real exacto.

---

## Estrategia de retries (resumen)

Cada paso del pipeline maneja retries de forma diferente según qué puede salir mal:

| Paso | Falla típica | Estrategia |
|---|---|---|
| `classifier` | JSON inválido o item que no pasa Pydantic | Hasta `max_retries` intentos bajando temp. Inyecta `RETRY_JSON_INVALID`. Items individuales malos se saltan; solo aborta si **ningún** item pasa |
| `generator` | Markdown vacío, < 100 chars, o sin headers | Hasta `max_retries` intentos. Inyecta `RETRY_VALIDATION_FAILED` con el error específico |
| `formatter` | Excede `max_chars` de la plataforma | (a) Si exceso < 20% → `_smart_truncate` (corta en último `.`/`\n`). (b) Si exceso ≥ 20% → retry con `RETRY_TOO_LONG` indicando `actual_chars` y `max_chars`. Si todos los intentos fallan, devuelve `[ERROR] ...` (no aborta el resto del pipeline) |
| `validator` | N/A — sin LLM | No reintenta; reporta issues como `error` (bloquea) o `warning` (no bloquea) |

`asyncio.gather(*, return_exceptions=True)` en el formatter garantiza que si una plataforma revienta, las demás siguen.

---

## Convenciones

- **Lenguaje del código:** docstrings y comentarios principalmente en español; mensajes al usuario en CLI también en español. Los prompts a Claude están en inglés (mejor performance del modelo).
- **Validación primero:** todo objeto que cruza un límite del pipeline es un modelo Pydantic. `ValidationError` se captura en el retry loop.
- **Prompts versionados:** todos en `prompts.py`. Cualquier cambio queda en `git diff` para auditoría.
- **Sin shell-out a Git:** se usa GitPython para evitar inyección y para tener errores tipados (`InvalidGitRepositoryError`, etc.).
- **Singletons de API client:** `get_sync_client()` / `get_async_client()` (en `utils.py`) y `_get_async_client()` (en `formatter.py`) evitan recrear el cliente en cada llamada.
- **Métricas siempre activas:** cada llamada a `call_llm()` registra tokens, costo y tiempo en `PipelineMetrics` para mostrar resumen al final. El formatter, al usar el async client directo, registra sus propias métricas inline.
- **Outputs guardados aunque falle la validación:** si el `validator` reporta errores, los archivos se guardan igualmente para inspección manual; el CLI muestra los errores y termina con código de salida no-cero solo cuando aplica.

---

## Roadmap

| Status | Entregable |
|---|---|
| ✅ Done | CLI base (`generate`, `tags`, `config`), modelos, config loader, git_reader |
| ✅ Done | `classifier` (Haiku + retry + parsing JSON con prefill `[`) |
| ✅ Done | `generator` (Sonnet + validación de estructura markdown) |
| ✅ Done | `formatter` paralelo (asyncio.gather + Semaphore) con `smart_truncate` |
| ✅ Done | `validator` con `ValidationResult`, reglas por plataforma, `print_validation_result` |
| ✅ Done | Pipeline end-to-end conectado en `cli.py generate` con guardado de archivos + bundle.json |
| ✅ Done | Tests de integración con mocks de Claude API (`pytest-asyncio` + `MockAnthropic` / `MockAsyncAnthropic`) |
| ✅ Done | Cache de respuestas SHA-256 en disco (`cache.py`, `--use-cache`/`--no-cache`) |
| ✅ Done | Flag `--estimate` real (cuenta tokens con heurística chars/token sin llamar a la API) |
| ✅ Done | Stage `[2/6]` que parsea el "Detailed Changelog" del README destino con regex (sin LLM) y lo inyecta como contexto al generator |

---

## Variables de entorno

| Variable | Requerido | Descripción |
|---|---|---|
| `ANTHROPIC_API_KEY` | Sí para `klimbook-release` (excepto `--dry-run`, `--estimate`, `tags`, `config`, `changelog`). **No** para `kbkro`. | API key de Anthropic. La lee `Anthropic()` / `AsyncAnthropic()` automáticamente. |

`kbkro` en cambio no necesita ninguna variable — solo un servidor Ollama accesible (default `http://localhost:11434`, override con `--host`).

---

## Autor

**zxxz6** 

## Licencia

MIT
