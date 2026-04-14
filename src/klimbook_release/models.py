"""
models.py
==================================================

Descripcion:
-----------
Modelos Pydantic compartidos por todo el pipeline. Cada paso consume un
modelo y produce el siguiente, por lo que estas definiciones son el
contrato que mantiene a git_reader, classifier, generator, formatter y
validator sincronizados.

Proposito del modulo:
--------------------
- Definir el intercambio de datos tipado para cada etapa del pipeline
- Validar contenido (no vacio, longitud minima, literals permitidos)
- Exponer campos computados para consumidores downstream (char_count,
  within_limit)
- Acumular metricas de ejecucion (tokens, costo, tiempo) a lo largo de
  los pasos

Flujo del pipeline:
------------------
RawCommit  ─►  CommitEntry  ─►  ReleaseNotes  ─►  PlatformOutput  ─►  ReleaseBundle
(git_reader)   (classifier)     (generator)        (formatter ×N)      (output)

Contenido del modulo:
--------------------
1. RawCommit - Datos crudos del commit desde GitPython (hash, mensaje,
               autor, fecha)
2. CommitEntry - Commit clasificado (type Literal, descripcion, servicio)
3. ReleaseNotes - Markdown maestro + metadata (version, fecha, categorias)
4. PlatformOutput - Contenido por plataforma con char_count/within_limit
5. ReleaseBundle - Contenedor final: outputs + metricas; helpers de
                   resumen
6. PLATFORM_LIMITS - Limites canonicos de chars por plataforma
7. StepMetrics - Metricas por llamada LLM (tokens, costo, tiempo, exito)
8. PipelineMetrics - Agregador sobre todos los pasos

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

from pydantic import BaseModel, field_validator, computed_field
from typing import Literal
from datetime import datetime


# =====================================================================
# Modelos de Commits
# =====================================================================

class RawCommit(BaseModel):
    """
    Un commit tal como lo lee Git, antes de clasificar.
    
    Este modelo lo produce git_reader.py y lo consume classifier.py.
    No tiene informacion semantica (no sabe si es feature o fix),
    solo los datos crudos del commit.
    """
    hash: str               # hash corto (7 chars)
    hash_full: str          # hash completo (40 chars)
    message: str            # primera linea del mensaje
    author: str             # nombre del autor
    date: str               # fecha ISO 8601
    files_changed: int = 0  # archivos modificados


class CommitEntry(BaseModel):
    """
    Un commit clasificado por el LLM.
    
    Este modelo lo produce classifier.py y lo consume generator.py.
    Agrega informacion semantica: tipo, servicio afectado, breaking.
    """
    type: Literal["feature", "fix", "refactor", "docs", "chore", "test", "ci"]
    description: str
    affected_service: str = "general"
    breaking: bool = False

    @field_validator("description")
    @classmethod
    def description_not_empty(cls, v):
        """Rechaza descripciones vacias o solo espacios; recorta espacios."""
        if not v.strip():
            raise ValueError("Description no puede estar vacia")
        return v.strip()

    @field_validator("type")
    @classmethod
    def type_not_empty(cls, v):
        """Rechaza valores de type vacios; pasa a minusculas y recorta antes de guardar."""
        if not v.strip():
            raise ValueError("Type no puede estar vacio")
        return v.strip().lower()


# =====================================================================
# Modelos de Release Notes
# =====================================================================

class ReleaseNotes(BaseModel):
    """
    Release notes generadas por el LLM en formato markdown.
    
    Este modelo lo produce generator.py y lo consume formatter.py.
    Es el formato intermedio antes de adaptar a cada plataforma.
    """
    version: str
    date: str
    markdown: str
    commit_count: int
    categories: dict[str, int]  # {"feature": 3, "fix": 5, ...}

    @field_validator("markdown")
    @classmethod
    def markdown_not_empty(cls, v):
        """Requiere markdown no vacio de al menos 50 caracteres."""
        if not v.strip():
            raise ValueError("Markdown no puede estar vacio")
        if len(v) < 50:
            raise ValueError(f"Markdown muy corto: {len(v)} chars (minimo 50)")
        return v


# =====================================================================
# Modelos de Output por Plataforma
# =====================================================================

class PlatformOutput(BaseModel):
    """
    Output formateado para una plataforma especifica.
    
    Este modelo lo produce formatter.py y se incluye en ReleaseBundle.
    Cada plataforma tiene su propio PlatformOutput con contenido
    adaptado a sus reglas (longitud, formato, idioma, tono).
    """
    platform: str
    content: str
    language: str = "en"
    max_chars: int | None = None  # limite de la plataforma (None = sin limite)

    @computed_field
    @property
    def char_count(self) -> int:
        """Cantidad de caracteres del contenido."""
        return len(self.content)

    @computed_field
    @property
    def within_limit(self) -> bool:
        """True si el contenido esta dentro del limite de caracteres."""
        if self.max_chars is None:
            return True
        return len(self.content) <= self.max_chars

    @field_validator("content")
    @classmethod
    def content_not_empty(cls, v):
        """Rechaza contenido de plataforma vacio o solo espacios."""
        if not v.strip():
            raise ValueError("Content no puede estar vacio")
        return v


# =====================================================================
# Release Bundle (output final del pipeline)
# =====================================================================

class ReleaseBundle(BaseModel):
    """
    Bundle completo de un release con todos los formatos.
    
    Este es el OUTPUT FINAL del pipeline. Contiene:
    - Metadata del release (version, fecha, commits)
    - Output para cada plataforma
    - Metricas de ejecucion (tokens, costo, tiempo)
    
    El CLI lo usa para mostrar resultados y guardar archivos.
    """
    version: str
    date: str
    commit_count: int
    outputs: dict[str, PlatformOutput]  # key = nombre de plataforma
    metrics: dict = {}                   # tokens, costo, tiempo

    def get_output(self, platform: str) -> PlatformOutput | None:
        """Retorna el output de una plataforma especifica."""
        return self.outputs.get(platform)

    def all_within_limits(self) -> bool:
        """True si TODOS los outputs estan dentro de sus limites."""
        return all(o.within_limit for o in self.outputs.values())

    def validation_summary(self) -> dict:
        """Resumen de validacion de todos los outputs."""
        summary = {}
        for name, output in self.outputs.items():
            summary[name] = {
                "chars": output.char_count,
                "limit": output.max_chars,
                "ok": output.within_limit,
            }
        return summary


# =====================================================================
# Limites por Plataforma
# =====================================================================

# Limites de caracteres por plataforma.
# None significa sin limite.
# Estos valores vienen de las guias de cada store:
# - Google Play "What's New": 500 caracteres
# - App Store "What's New": 4000 caracteres
# - Ko-fi: no tiene limite estricto pero 2000 es razonable
PLATFORM_LIMITS = {
    "github": None,
    "playstore_en": 500,
    "playstore_es": 500,
    "appstore": 4000,
    "kofi": 2000,
}


# =====================================================================
# Metricas del Pipeline
# =====================================================================

class StepMetrics(BaseModel):
    """Metricas de un paso individual del pipeline."""
    step_name: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    elapsed_seconds: float
    retries: int = 0
    success: bool = True
    error: str = ""


class PipelineMetrics(BaseModel):
    """Metricas acumuladas de todo el pipeline."""
    steps: list[StepMetrics] = []
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0
    total_elapsed_seconds: float = 0.0

    def add_step(self, step: StepMetrics):
        """Agrega las metricas de un paso."""
        self.steps.append(step)
        self.total_input_tokens += step.input_tokens
        self.total_output_tokens += step.output_tokens
        self.total_cost_usd += step.cost_usd
        self.total_elapsed_seconds += step.elapsed_seconds

    def summary(self) -> dict:
        """Retorna un resumen serializable."""
        return {
            "total_steps": len(self.steps),
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_tokens": self.total_input_tokens + self.total_output_tokens,
            "total_cost_usd": round(self.total_cost_usd, 6),
            "total_elapsed_seconds": round(self.total_elapsed_seconds, 2),
        }
