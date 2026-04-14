"""
test_generator_mock.py
==================================================

Descripcion:
-----------
Tests de integracion del paso generator usando un cliente Anthropic
mockeado. Cubre el happy path, retry cuando el output es muy corto o no
tiene headers, el short-circuit con entries vacias, y el conteo de
categorias/commits.

Proposito del modulo:
--------------------
- Verificar que generate_notes produce un ReleaseNotes desde una lista
  de CommitEntry
- Verificar que los conteos de categorias y commit_count se llenan
- Verificar retry ante markdown corto/invalido
- Verificar que entries vacias devuelven un placeholder sin llamadas a
  la API

Contenido del modulo:
--------------------
1. test_generate_happy_path
2. test_generate_retries_on_short_markdown
3. test_generate_empty_entries_skips_api
4. test_generate_categories_counted

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

from klimbook_release.generator import generate_notes
from klimbook_release.models import CommitEntry, PipelineMetrics


VALID_MARKDOWN = """# Klimbook — Release Notes v2.9.0
**April 13, 2026**

Small release with a few improvements.

---

## What's New

### Features
- Added Google OAuth login (auth)

### Bug Fixes
- Fixed grade conversion for V10 (blocks)

---

More text to reach the minimum length requirement.
"""


def _sample_entries() -> list[CommitEntry]:
    """Conjunto pequeno de entries clasificadas usado por varios tests."""
    return [
        CommitEntry(type="feature", description="add OAuth", affected_service="auth"),
        CommitEntry(type="fix", description="grade conv", affected_service="blocks"),
        CommitEntry(type="docs", description="readme", affected_service="general"),
    ]


def test_generate_happy_path(mock_anthropic, base_config):
    """Una respuesta markdown valida produce un ReleaseNotes con metadata."""
    mock_anthropic.queue_response(VALID_MARKDOWN)
    entries = _sample_entries()

    notes = generate_notes(entries, version="v2.9.0", config=base_config)

    assert notes.version == "v2.9.0"
    assert notes.commit_count == 3
    assert "# Klimbook" in notes.markdown
    assert notes.categories == {"feature": 1, "fix": 1, "docs": 1}
    assert len(mock_anthropic.calls) == 1


def test_generate_retries_on_short_markdown(mock_anthropic, base_config):
    """Un output muy corto dispara un retry; el segundo intento tiene exito."""
    # Primer intento: muy corto (<100 chars) Y sin header '#'
    mock_anthropic.queue_response("tiny")
    # Segundo intento: valido
    mock_anthropic.queue_response(VALID_MARKDOWN)

    metrics = PipelineMetrics()
    notes = generate_notes(
        _sample_entries(), version="v2.9.0", config=base_config, metrics=metrics,
    )

    assert "# Klimbook" in notes.markdown
    assert len(mock_anthropic.calls) == 2


def test_generate_empty_entries_skips_api(mock_anthropic, base_config):
    """Sin entries -> notas placeholder, sin llamadas a la API."""
    notes = generate_notes([], version="v2.9.0", config=base_config)

    assert notes.commit_count == 0
    assert notes.categories == {}
    assert "No changes" in notes.markdown
    assert len(mock_anthropic.calls) == 0


def test_generate_categories_counted(mock_anthropic, base_config):
    """Los conteos de categorias reflejan exactamente las entries de entrada."""
    mock_anthropic.queue_response(VALID_MARKDOWN)
    entries = [
        CommitEntry(type="feature", description="a", affected_service="x"),
        CommitEntry(type="feature", description="b", affected_service="x"),
        CommitEntry(type="fix", description="c", affected_service="x"),
    ]

    notes = generate_notes(entries, version="v2.9.0", config=base_config)

    assert notes.categories == {"feature": 2, "fix": 1}
    assert notes.commit_count == 3
