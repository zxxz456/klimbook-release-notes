"""
cache.py
==================================================

Descripcion:
-----------
Cache en disco para respuestas de la API de Claude. Permite iterar sobre
prompts sin volver a gastar tokens: la misma tupla (model, system, prompt,
temperature, max_tokens, prefill) devuelve una respuesta cacheada en vez
de llamar a la API. El almacenamiento es un archivo JSON por key dentro
de un directorio de cache.

Proposito del modulo:
--------------------
- Key SHA-256 deterministica sobre la forma exacta del input de
  messages.create
- get/set transparente usado por utils.call_llm y las llamadas async del
  formatter
- Seguro cuando esta deshabilitado (todas las operaciones son no-op)
- Sin TTL: las entradas viejas se limpian borrando el directorio de cache

Contenido del modulo:
--------------------
1. CachedResponse - Contenedor tipado para una respuesta cacheada
2. ResponseCache - Store clave/valor en disco indexado por la firma de la
                   llamada

Metadata:
----------
* Autor: zxxz6 (Bryan Violante Arriaga)
* Version: 1.0.0
* Licencia: MIT

Historial:
------------
Autor       Fecha           Descripcion
zxxz6       13/04/2026      Creacion
"""

from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any
import hashlib
import json
import logging

logger = logging.getLogger("cache")


@dataclass
class CachedResponse:
    """Respuesta cacheada de la API con los campos que usamos aguas abajo."""
    text: str
    input_tokens: int
    output_tokens: int
    model: str


class ResponseCache:
    """
    Cache en disco indexado por SHA-256 para respuestas de la API de Claude.

    La key se construye a partir de la tupla que determina completamente
    la respuesta a temperatura 0:
    (model, system, prompt, temperature, max_tokens, prefill).
    Cuando esta deshabilitado, get() siempre devuelve None y set() es no-op.
    """

    def __init__(self, cache_dir: str | Path = ".cache", enabled: bool = False):
        """
        Inicializa el cache.

        Args:
            cache_dir: Directorio donde se escriben los archivos JSON cacheados.
            enabled: Si es False, todas las operaciones son no-op.
        """
        self.cache_dir = Path(cache_dir)
        self.enabled = enabled
        self.hits = 0
        self.misses = 0
        if self.enabled:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _key(
        self,
        model: str,
        system: str,
        prompt: str,
        temperature: float,
        max_tokens: int,
        prefill: str = "",
    ) -> str:
        """Calcula la key SHA-256 deterministica para una firma de llamada."""
        payload = json.dumps(
            {
                "model": model,
                "system": system,
                "prompt": prompt,
                "temperature": round(float(temperature), 4),
                "max_tokens": int(max_tokens),
                "prefill": prefill,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]

    def _path(self, key: str) -> Path:
        """Devuelve el path en disco para una key de cache dada."""
        return self.cache_dir / f"{key}.json"

    def get(
        self,
        model: str,
        system: str,
        prompt: str,
        temperature: float,
        max_tokens: int,
        prefill: str = "",
    ) -> CachedResponse | None:
        """Devuelve la respuesta cacheada para esta llamada, o None si no existe."""
        if not self.enabled:
            return None
        key = self._key(model, system, prompt, temperature, max_tokens, prefill)
        path = self._path(key)
        if not path.exists():
            self.misses += 1
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self.hits += 1
            logger.info(f"[Cache] HIT  {key[:8]}... ({path.name})")
            return CachedResponse(**data)
        except (json.JSONDecodeError, TypeError, KeyError) as e:
            logger.warning(f"[Cache] Entrada {key[:8]} esta corrupta: {e}. Ignorando.")
            self.misses += 1
            return None

    def set(
        self,
        model: str,
        system: str,
        prompt: str,
        temperature: float,
        max_tokens: int,
        response: CachedResponse,
        prefill: str = "",
    ) -> None:
        """Persiste una respuesta bajo la key de cache calculada."""
        if not self.enabled:
            return
        key = self._key(model, system, prompt, temperature, max_tokens, prefill)
        path = self._path(key)
        try:
            path.write_text(
                json.dumps(asdict(response), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            logger.info(f"[Cache] STORE {key[:8]}... ({path.name})")
        except OSError as e:
            logger.warning(f"[Cache] Fallo escribiendo {path}: {e}")

    def clear(self) -> int:
        """Borra todas las entradas cacheadas. Devuelve el numero de archivos eliminados."""
        if not self.cache_dir.exists():
            return 0
        count = 0
        for path in self.cache_dir.glob("*.json"):
            path.unlink()
            count += 1
        logger.info(f"[Cache] Borradas {count} entradas de {self.cache_dir}")
        return count

    def stats(self) -> dict[str, Any]:
        """Devuelve los contadores de hit/miss y bytes totales en disco."""
        entries = list(self.cache_dir.glob("*.json")) if self.cache_dir.exists() else []
        return {
            "enabled": self.enabled,
            "dir": str(self.cache_dir),
            "entries": len(entries),
            "hits": self.hits,
            "misses": self.misses,
            "bytes": sum(p.stat().st_size for p in entries),
        }


# Singleton a nivel de proceso. Los modulos que necesiten cache deben llamar a get_cache().
_cache: ResponseCache | None = None


def get_cache() -> ResponseCache:
    """Devuelve el singleton ResponseCache del proceso (deshabilitado por defecto)."""
    global _cache
    if _cache is None:
        _cache = ResponseCache(enabled=False)
    return _cache


def configure_cache(cache_dir: str | Path = ".cache", enabled: bool = False) -> ResponseCache:
    """(Re)configura el cache del proceso. Lo llama el CLI al iniciar."""
    global _cache
    _cache = ResponseCache(cache_dir=cache_dir, enabled=enabled)
    return _cache
