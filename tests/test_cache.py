"""
test_cache.py
==================================================

Descripcion:
-----------
Tests unitarios y de integracion para ResponseCache. Cubren la derivacion
de la key, semantica enabled/disabled, round-trip de get/set, contadores
hit/miss, clear, y el efecto end-to-end del cache sobre utils.call_llm.

Proposito del modulo:
--------------------
- Verificar que la key basada en SHA-256 es deterministica entre corridas
- Verificar que el modo deshabilitado es un no-op
- Verificar round-trip de get/set en disco
- Verificar que utils.call_llm consulta el cache y salta la API en hit

Contenido del modulo:
--------------------
1. test_cache_disabled_is_noop
2. test_cache_round_trip
3. test_cache_key_deterministic
4. test_cache_different_temperature_different_key
5. test_cache_clear
6. test_call_llm_uses_cache

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

import pytest

from klimbook_release.cache import (
    CachedResponse, ResponseCache, configure_cache, get_cache,
)
from klimbook_release.utils import call_llm


def test_cache_disabled_is_noop(tmp_path):
    """Cache disabled retorna None en get e ignora set silenciosamente."""
    c = ResponseCache(cache_dir=tmp_path, enabled=False)

    c.set(
        "m", "sys", "prompt", 0.0, 100,
        CachedResponse(text="x", input_tokens=1, output_tokens=1, model="m"),
    )
    assert c.get("m", "sys", "prompt", 0.0, 100) is None
    assert list(tmp_path.glob("*.json")) == []


def test_cache_round_trip(tmp_path):
    """Guardar y releer devuelve el mismo CachedResponse."""
    c = ResponseCache(cache_dir=tmp_path, enabled=True)
    original = CachedResponse(
        text="hello world", input_tokens=42, output_tokens=7, model="m1",
    )

    c.set("m1", "sys", "prompt", 0.3, 500, original)
    fetched = c.get("m1", "sys", "prompt", 0.3, 500)

    assert fetched is not None
    assert fetched.text == "hello world"
    assert fetched.input_tokens == 42
    assert fetched.output_tokens == 7
    assert c.hits == 1
    assert c.misses == 0


def test_cache_key_deterministic(tmp_path):
    """Los mismos inputs producen la misma key sin importar el orden de llamada."""
    c = ResponseCache(cache_dir=tmp_path, enabled=True)
    k1 = c._key("m", "sys", "prompt", 0.0, 100)
    k2 = c._key("m", "sys", "prompt", 0.0, 100)
    assert k1 == k2


def test_cache_different_temperature_different_key(tmp_path):
    """La temperature es parte de la key -- distintas temps no deben colisionar."""
    c = ResponseCache(cache_dir=tmp_path, enabled=True)
    k1 = c._key("m", "sys", "prompt", 0.0, 100)
    k2 = c._key("m", "sys", "prompt", 0.5, 100)
    assert k1 != k2


def test_cache_clear(tmp_path):
    """clear() elimina todas las entradas del disco y retorna el conteo."""
    c = ResponseCache(cache_dir=tmp_path, enabled=True)
    for i in range(3):
        c.set(
            "m", "sys", f"prompt-{i}", 0.0, 100,
            CachedResponse(text=f"r{i}", input_tokens=1, output_tokens=1, model="m"),
        )
    removed = c.clear()
    assert removed == 3
    assert list(tmp_path.glob("*.json")) == []


def test_call_llm_uses_cache(tmp_path, mock_anthropic, base_config):
    """call_llm retorna el texto cacheado sin llamar a la API en hit."""
    # Habilitar el cache global para este test
    configure_cache(cache_dir=tmp_path, enabled=True)
    try:
        mock_anthropic.set_default("live response from API")

        # Primera llamada: cache miss, API invocada, resultado almacenado
        text1, m1 = call_llm(
            model="m1", system="sys", prompt="p",
            temperature=0.0, max_tokens=100,
            config=base_config, step_name="t1",
        )
        assert text1 == "live response from API"
        assert len(mock_anthropic.calls) == 1

        # Segunda llamada con los mismos inputs: cache hit, sin llamada a la API
        text2, m2 = call_llm(
            model="m1", system="sys", prompt="p",
            temperature=0.0, max_tokens=100,
            config=base_config, step_name="t2",
        )
        assert text2 == "live response from API"
        assert len(mock_anthropic.calls) == 1  # sigue siendo una
        # Las llamadas cacheadas registran costo cero para que la estimacion siga siendo honesta
        assert m2.cost_usd == 0.0
    finally:
        # Resetear el cache al estado disabled por default
        configure_cache(cache_dir=".cache", enabled=False)
