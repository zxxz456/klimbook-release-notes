"""
test_classifier_mock.py
==================================================

Descripcion:
-----------
Tests de integracion del paso classifier usando un cliente Anthropic
mockeado. Cubre el happy path (JSON valido), recuperacion via retry ante
JSON invalido, saltar entries individuales que fallan Pydantic, y el
agrupador classify_to_summary.

Proposito del modulo:
--------------------
- Verificar que classify_commits parsea el JSON y devuelve
  list[CommitEntry]
- Verificar que el retry loop se recupera cuando la primera respuesta es
  JSON invalido
- Verificar que items invalidos dentro de un array valido se saltan, no
  son fatales
- Verificar que classify_to_summary agrupa por tipo en el orden esperado

Contenido del modulo:
--------------------
1. test_classify_happy_path
2. test_classify_retries_on_invalid_json
3. test_classify_skips_invalid_entries
4. test_classify_empty_commits
5. test_classify_to_summary_grouping

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

from klimbook_release.classifier import classify_commits, classify_to_summary
from klimbook_release.models import CommitEntry, PipelineMetrics


def test_classify_happy_path(mock_anthropic, sample_commits, base_config):
    """Una respuesta JSON array valida produce un CommitEntry por commit."""
    # call_llm antepone el prefill "[" al texto devuelto. El mock
    # devuelve SOLO lo que el modelo generaria DESPUES del prefill.
    mock_anthropic.queue_response(
        '{"type": "feature", "description": "add OAuth login", '
        '"affected_service": "auth", "breaking": false},'
        '{"type": "fix", "description": "fix grade conversion", '
        '"affected_service": "blocks", "breaking": false},'
        '{"type": "docs", "description": "update README", '
        '"affected_service": "general", "breaking": false}]'
    )

    entries = classify_commits(sample_commits, base_config)

    assert len(entries) == 3
    assert entries[0].type == "feature"
    assert entries[0].affected_service == "auth"
    assert entries[1].type == "fix"
    assert entries[2].type == "docs"
    # Se hizo exactamente una llamada a la API (sin retries)
    assert len(mock_anthropic.calls) == 1


def test_classify_retries_on_invalid_json(mock_anthropic, sample_commits, base_config):
    """Si la primera respuesta es JSON invalido, el classifier reintenta."""
    # Primer intento: JSON roto (falta cierre, basura extra)
    mock_anthropic.queue_response("not-a-valid-json-array <<<")
    # Segundo intento: JSON valido
    mock_anthropic.queue_response(
        '{"type": "fix", "description": "retry worked", '
        '"affected_service": "general", "breaking": false}]'
    )

    metrics = PipelineMetrics()
    entries = classify_commits(sample_commits, base_config, metrics)

    assert len(entries) == 1
    assert entries[0].description == "retry worked"
    # Dos llamadas en total (la primera fallo, la segunda exitosa)
    assert len(mock_anthropic.calls) == 2


def test_classify_skips_invalid_entries(mock_anthropic, sample_commits, base_config):
    """Items con type invalido se saltan; los validos pasan."""
    mock_anthropic.queue_response(
        '{"type": "banana", "description": "invalid type here", '
        '"affected_service": "x", "breaking": false},'
        '{"type": "fix", "description": "valid one", '
        '"affected_service": "y", "breaking": false}]'
    )

    entries = classify_commits(sample_commits, base_config)

    # Solo sobrevive la entry valida; la "banana" se descarta
    assert len(entries) == 1
    assert entries[0].type == "fix"
    assert entries[0].description == "valid one"


def test_classify_empty_commits(mock_anthropic, base_config):
    """Input vacio devuelve output vacio sin tocar la API."""
    entries = classify_commits([], base_config)
    assert entries == []
    assert len(mock_anthropic.calls) == 0


def test_classify_to_summary_grouping():
    """classify_to_summary agrupa las entries por tipo en el orden documentado."""
    entries = [
        CommitEntry(type="docs", description="readme", affected_service="general"),
        CommitEntry(type="feature", description="login", affected_service="auth"),
        CommitEntry(type="fix", description="typo", affected_service="general",
                    breaking=True),
        CommitEntry(type="feature", description="signup", affected_service="auth"),
    ]
    summary = classify_to_summary(entries)

    # Features van antes que fixes, que van antes que docs
    assert summary.index("FEATURE") < summary.index("FIX")
    assert summary.index("FIX") < summary.index("DOCS")
    # Los conteos por grupo se renderizan
    assert "FEATURE (2)" in summary
    assert "FIX (1)" in summary
    # El scope de servicio aparece cuando != "general"
    assert "(auth)" in summary
    # El flag breaking se renderiza
    assert "[BREAKING]" in summary
