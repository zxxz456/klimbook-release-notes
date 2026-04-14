"""
test_formatter_mock.py
==================================================

Descripcion:
-----------
Tests de integracion del paso formatter usando un cliente AsyncAnthropic
mockeado. Cubre el formateo paralelo en multiples plataformas, smart
truncation cuando el output excede ligeramente el limite, retry ante
exceso grande, y degradacion graceful cuando todos los retries fallan.

Proposito del modulo:
--------------------
- Verificar que format_all_platforms devuelve un PlatformOutput por cada
  plataforma habilitada
- Verificar que _smart_truncate maneja excesos pequenos sin volver a
  llamar la API
- Verificar que un exceso grande dispara un retry
- Verificar que _smart_truncate devuelve None cuando el corte perderia
  mas del 50%
- Verificar que format_all_sync envuelve asyncio.run correctamente

Contenido del modulo:
--------------------
1. test_format_all_platforms_happy_path
2. test_format_sync_wrapper
3. test_format_truncates_small_overshoot
4. test_format_retries_large_overshoot
5. test_smart_truncate_returns_none_for_huge_overshoot

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

from klimbook_release.formatter import (
    format_all_platforms, format_all_sync, _smart_truncate,
)
from klimbook_release.models import ReleaseNotes


def _sample_notes() -> ReleaseNotes:
    """Un ReleaseNotes minimo valido para tests del formatter."""
    md = (
        "# Klimbook v2.9.0\n\n"
        "This release ships several improvements. The team worked hard "
        "on authentication, grade conversion, and documentation.\n\n"
        "## Features\n- Google OAuth login\n\n## Fixes\n- Grade conversion"
    )
    return ReleaseNotes(
        version="v2.9.0",
        date="April 13, 2026",
        markdown=md,
        commit_count=3,
        categories={"feature": 1, "fix": 1, "docs": 1},
    )


async def test_format_all_platforms_happy_path(mock_anthropic, base_config):
    """Un output por cada plataforma habilitada, cada uno dentro de los limites."""
    mock_anthropic.set_default("* Cool new feature\n* Critical fix shipped")
    notes = _sample_notes()

    outputs = await format_all_platforms(notes, base_config)

    # base_config habilita github + playstore_en
    assert set(outputs.keys()) == {"github", "playstore_en"}
    for name, out in outputs.items():
        assert out.platform == name
        assert out.within_limit
        assert not out.content.startswith("[ERROR]")
    # Al menos una llamada a la API por plataforma
    assert len(mock_anthropic.calls) >= 2


def test_format_sync_wrapper(mock_anthropic, base_config):
    """format_all_sync lleva la coroutine async hasta su finalizacion."""
    mock_anthropic.set_default("* Short update")
    notes = _sample_notes()

    outputs = format_all_sync(notes, base_config)

    assert "github" in outputs
    assert "playstore_en" in outputs


async def test_format_truncates_small_overshoot(mock_anthropic, base_config):
    """Un overshoot <20% se truncate en el ultimo punto, sin llamada de retry."""
    # El limite de playstore_en es 500 chars. Devolver 540 chars con puntos dentro.
    text = "* ".join(["Sentence " + "x" * 20 + "." for _ in range(30)])
    assert 500 < len(text) <= 600  # dentro del rango de smart-truncate
    mock_anthropic.set_default(text)
    notes = _sample_notes()

    outputs = await format_all_platforms(notes, base_config)

    ps = outputs["playstore_en"]
    assert ps.char_count <= 500
    assert ps.within_limit


async def test_format_retries_large_overshoot(mock_anthropic, base_config):
    """Un overshoot >=20% omite el truncate y hace retry con una respuesta mas corta."""
    over = "x" * 700  # 40% sobre el limite de 500, sin puntuacion donde truncate
    short = "* Short and sweet update."
    # Encolar una vez por plataforma: primero enorme, luego corto. Solo playstore
    # tiene un limite; github no tiene limite y aceptara el string de 700 chars
    # en la primera llamada. Asi que solo playstore dispara el camino de retry.
    # Con 2 plataformas × retry_temperatures de longitud 2, necesitamos respuestas
    # para cubrir: github (1), playstore (2).
    mock_anthropic.queue_response(short)       # github: cualquiera
    mock_anthropic.queue_response(over)        # playstore intento 1
    mock_anthropic.queue_response(short)       # playstore intento 2 (retry)
    notes = _sample_notes()

    outputs = await format_all_platforms(notes, base_config)

    ps = outputs["playstore_en"]
    assert ps.within_limit
    # github acepto la primera respuesta; playstore tomo 2 llamadas
    # Total: 1 + 2 = 3 llamadas a la API
    assert len(mock_anthropic.calls) == 3


def test_smart_truncate_returns_none_for_huge_overshoot():
    """Si el punto de corte mas cercano es <50% del limite, devuelve None."""
    # 1000 chars, el primer "." cae en indice 5 (muy antes de la mitad del limite)
    text = "tiny." + ("y" * 1000)
    assert _smart_truncate(text, max_chars=500) is None


def test_smart_truncate_cuts_at_last_period():
    """Caso normal: cortar en el ultimo punto dentro del limite de chars."""
    text = "First sentence. Second sentence. Third sentence." + ("x" * 100)
    result = _smart_truncate(text, max_chars=50)
    assert result is not None
    assert result.endswith(".")
    assert len(result) <= 50
