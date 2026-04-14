"""
config.py
==================================================

Descripcion:
-----------
Carga la configuracion del pipeline desde config.yaml y la expone como un
objeto Pydantic tipado. Se carga una sola vez al inicio y se pasa a cada
componente, por lo que ningun modulo lee YAML por su cuenta.

Proposito del modulo:
--------------------
- Parsear config.yaml a modelos Pydantic validados
- Caer a defaults razonables (Klimbook, 5 plataformas, glosario) si el
  archivo falta o esta vacio
- Calcular el costo en USD de una llamada dados el modelo y los tokens

Contenido del modulo:
--------------------
1. PlatformConfig - Configuracion por plataforma (enabled, language,
                    max_chars)
2. PricingConfig - USD por millon de tokens (input/output) de un modelo
3. Config - Config de alto nivel: modelos, temperaturas, retry,
            plataformas, etc.
4. load_config - Lee el YAML y construye un Config (o defaults)
5. _default_config - Devuelve la config por defecto de Klimbook

Metodos clave de Config:
-----------------------
- get_enabled_platforms() - Subconjunto de plataformas con enabled=True
- get_pricing(model) - Pricing de un modelo (fallback a Sonnet si no
                       esta configurado)
- calculate_cost(model, in_tokens, out_tokens) - Costo en USD de una
                                                 llamada

Metadata:
----------
* Autor: zxxz6 (Bryan Violante Arriaga)
* Version: 1.0.0
* Licencia: MIT

Historial:
------------
Autor       Fecha           Descripcion
zxxz6       13/04/2026      Agregados changelog_enabled, changelog_source,
                            changelog_sections y changelog_count para la
                            integracion con changelog.read_changelog
zxxz6       13/04/2026      Agregada cadena de busqueda de config.yaml
                            (explicit > env > CWD > XDG > bundled) para
                            permitir correr el CLI desde cualquier dir
zxxz6       13/04/2026      Agregados cache_enabled y cache_dir para la
                            integracion con ResponseCache
zxxz6       03/04/2026      Creacion
"""

import yaml
import os
from importlib import resources
from pathlib import Path
from pydantic import BaseModel
import logging

logger = logging.getLogger("config")


# =====================================================================
# Modelos de Configuracion
# =====================================================================

class PlatformConfig(BaseModel):
    """Configuracion de una plataforma individual."""
    enabled: bool = True
    language: str = "en"
    max_chars: int | None = None
    description: str = ""


class PricingConfig(BaseModel):
    """Precios de un modelo (USD por millon de tokens)."""
    input: float
    output: float


class Config(BaseModel):
    """
    Configuracion completa del proyecto.
    
    Todos los valores tienen defaults razonables para que el tool
    funcione incluso sin archivo config.yaml.
    """
    # Proyecto
    project_name: str = "Klimbook"
    repo_path: str = "."
    default_branch: str = "main"

    # Modelos
    classifier_model: str = "claude-haiku-4-5-20251001"
    generator_model: str = "claude-sonnet-4-20250514"
    formatter_model: str = "claude-sonnet-4-20250514"

    # Temperaturas
    classifier_temp: float = 0.0
    generator_temp: float = 0.3
    formatter_formal_temp: float = 0.3
    formatter_casual_temp: float = 0.7

    # Retry
    max_retries: int = 3
    retry_temperatures: list[float] = [0.3, 0.1, 0.0]

    # Concurrencia
    max_parallel: int = 3

    # Cache de respuestas (disk-backed, SHA-256 keyed)
    cache_enabled: bool = False
    cache_dir: str = ".cache"

    # Detailed changelog del README destino (contexto para el generator)
    changelog_enabled: bool = True
    changelog_source: str = "README.md"
    changelog_sections: list[str] = ["Backend", "Frontend", "Mobile"]
    changelog_count: int = 1

    # Plataformas
    platforms: dict[str, PlatformConfig] = {}

    # Glosario
    glossary: list[str] = []

    # Pricing
    pricing: dict[str, PricingConfig] = {}

    def get_enabled_platforms(self) -> dict[str, PlatformConfig]:
        """Retorna solo las plataformas habilitadas."""
        return {
            name: cfg
            for name, cfg in self.platforms.items()
            if cfg.enabled
        }

    def get_pricing(self, model: str) -> PricingConfig:
        """
        Retorna el pricing de un modelo.
        Si no esta configurado, usa valores default de Sonnet.
        """
        return self.pricing.get(model, PricingConfig(input=3.0, output=15.0))

    def calculate_cost(self, model: str, input_tokens: int, output_tokens: int) -> float:
        """Calcula el costo en USD de una llamada a la API."""
        p = self.get_pricing(model)
        return (input_tokens / 1e6) * p.input + (output_tokens / 1e6) * p.output


# =====================================================================
# Cargador
# =====================================================================

def _resolve_config_path(explicit: str | None) -> Path | None:
    """
    Devuelve la primera ruta existente en la cadena de busqueda.

    Orden de prioridad:
    1. `explicit` (flag --config) si se pasa y existe
    2. Variable de entorno KLIMBOOK_RELEASE_CONFIG
    3. ./config.yaml (directorio actual, para overrides por-repo)
    4. ~/.config/klimbook-release/config.yaml (XDG config home)
    5. default_config.yaml empacado con el paquete

    Retorna None si ninguno existe (imposible en la practica porque el
    default empacado siempre viaja con la instalacion).
    """
    candidates: list[Path] = []

    if explicit:
        candidates.append(Path(explicit).expanduser())

    env = os.environ.get("KLIMBOOK_RELEASE_CONFIG")
    if env:
        candidates.append(Path(env).expanduser())

    candidates.append(Path.cwd() / "config.yaml")
    candidates.append(Path.home() / ".config" / "klimbook-release" / "config.yaml")

    # Default empacado (viaja como package data)
    try:
        with resources.as_file(
            resources.files("klimbook_release") / "default_config.yaml"
        ) as bundled:
            candidates.append(Path(bundled))
    except (ModuleNotFoundError, FileNotFoundError):
        pass

    for p in candidates:
        if p.is_file():
            return p
    return None


def load_config(path: str | None = None) -> Config:
    """
    Carga la configuracion desde un archivo YAML.

    La cadena de busqueda (implementada en _resolve_config_path) permite
    correr el CLI desde cualquier directorio sin pasar --config cada vez:
    explicit > env var > CWD > ~/.config > bundled default.

    Args:
        path: Ruta explicita (flag --config). Si es None o el archivo no
              existe, se usa el siguiente candidato.

    Returns:
        Objeto Config tipado y validado.
    """
    resolved = _resolve_config_path(path)

    if resolved is None:
        logger.warning("[Config] Ningun config.yaml encontrado. Usando defaults hardcoded.")
        return _default_config()

    logger.info(f"[Config] Cargando desde '{resolved}'")

    with open(resolved) as f:
        raw = yaml.safe_load(f)

    if not raw:
        logger.warning("[Config] Archivo vacio. Usando defaults.")
        return _default_config()

    # ---- Extraer secciones del YAML ----
    project = raw.get("project", {})
    models = raw.get("models", {})
    temps = raw.get("temperatures", {})
    retry = raw.get("retry", {})
    concurrency = raw.get("concurrency", {})
    cache = raw.get("cache", {})
    changelog = raw.get("changelog", {})

    # Parsear plataformas
    platforms = {}
    for name, cfg in raw.get("platforms", {}).items():
        platforms[name] = PlatformConfig(**cfg)

    # Parsear pricing
    pricing = {}
    for model_name, prices in raw.get("pricing", {}).items():
        pricing[model_name] = PricingConfig(**prices)

    config = Config(
        # Proyecto
        project_name=project.get("name", "Klimbook"),
        repo_path=project.get("repo_path", "."),
        default_branch=project.get("default_branch", "main"),

        # Modelos
        classifier_model=models.get("classifier", "claude-haiku-4-5-20251001"),
        generator_model=models.get("generator", "claude-sonnet-4-20250514"),
        formatter_model=models.get("formatter", "claude-sonnet-4-20250514"),

        # Temperaturas
        classifier_temp=temps.get("classifier", 0.0),
        generator_temp=temps.get("generator", 0.3),
        formatter_formal_temp=temps.get("formatter_formal", 0.3),
        formatter_casual_temp=temps.get("formatter_casual", 0.7),

        # Retry
        max_retries=retry.get("max_retries", 3),
        retry_temperatures=retry.get("temperatures", [0.3, 0.1, 0.0]),

        # Concurrencia
        max_parallel=concurrency.get("max_parallel", 3),

        # Cache
        cache_enabled=cache.get("enabled", False),
        cache_dir=cache.get("dir", ".cache"),

        # Changelog
        changelog_enabled=changelog.get("enabled", True),
        changelog_source=changelog.get("source", "README.md"),
        changelog_sections=changelog.get(
            "sections", ["Backend", "Frontend", "Mobile"]
        ),
        changelog_count=changelog.get("count", 1),

        # Plataformas, glosario, pricing
        platforms=platforms,
        glossary=raw.get("glossary", []),
        pricing=pricing,
    )

    enabled = config.get_enabled_platforms()
    logger.info(
        f"[Config] Cargado: {config.project_name} | "
        f"{len(enabled)} plataformas habilitadas | "
        f"{len(config.glossary)} terminos en glosario"
    )

    return config


def _default_config() -> Config:
    """Retorna configuracion por defecto para Klimbook."""
    return Config(
        platforms={
            "github": PlatformConfig(language="en", description="GitHub release markdown"),
            "playstore_en": PlatformConfig(language="en", max_chars=500, description="Play Store en"),
            "playstore_es": PlatformConfig(language="es", max_chars=500, description="Play Store es"),
            "appstore": PlatformConfig(language="en", max_chars=4000, description="App Store"),
            "kofi": PlatformConfig(language="en", max_chars=2000, description="Ko-fi post"),
        },
        glossary=[
            "boulder", "route", "crag", "pitch", "flash",
            "onsight", "redpoint", "beta", "crux", "dyno",
        ],
        pricing={
            "claude-haiku-4-5-20251001": PricingConfig(input=0.80, output=4.00),
            "claude-sonnet-4-20250514": PricingConfig(input=3.00, output=15.00),
        },
    )
