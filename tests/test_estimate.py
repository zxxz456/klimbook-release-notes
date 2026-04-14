"""
test_estimate.py
==================================================

Descripcion:
-----------
Tests para el estimador de tokens + costo. Verifica la heuristica, los
conteos por paso, la aritmetica de costo y el estimate del pipeline
completo para una lista representativa de commits.

Proposito del modulo:
--------------------
- Verificar que estimate_tokens maneja strings vacios y escala con la
  longitud
- Verificar que estimate_pipeline produce un paso por plataforma
  habilitada + 2 centrales
- Verificar que los totales respetan el pricing del Config (Haiku para
  classify, Sonnet para el resto)
- Verificar que no ocurre ninguna llamada a la API durante la estimacion

Contenido del modulo:
--------------------
1. test_estimate_tokens_empty
2. test_estimate_tokens_scales
3. test_estimate_pipeline_shape
4. test_estimate_pipeline_totals_positive
5. test_estimate_pipeline_classifier_uses_haiku_pricing
6. test_estimate_pipeline_makes_no_api_calls

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

from klimbook_release.estimate import (
    CHARS_PER_TOKEN, estimate_tokens, estimate_pipeline,
)


def test_estimate_tokens_empty():
    """Texto vacio tiene cero tokens."""
    assert estimate_tokens("") == 0
    assert estimate_tokens(None) == 0  # type: ignore[arg-type]


def test_estimate_tokens_scales():
    """El conteo de tokens escala aproximadamente con length del texto / CHARS_PER_TOKEN."""
    text = "x" * 380  # exactamente 100 tokens a 3.8 chars/token
    n = estimate_tokens(text)
    assert 95 <= n <= 105


def test_estimate_pipeline_shape(sample_commits, base_config):
    """Un paso para classify, uno para generate, uno por cada plataforma habilitada."""
    est = estimate_pipeline(sample_commits, base_config, version="v2.9.0")

    # base_config habilita 2 plataformas -> 2 classify+generate + 2 format = 4 pasos
    assert est.commit_count == len(sample_commits)
    assert est.platform_count == 2
    assert len(est.steps) == 4

    step_names = [s.step_name for s in est.steps]
    assert step_names[0] == "classify"
    assert step_names[1] == "generate"
    assert "format:github" in step_names
    assert "format:playstore_en" in step_names


def test_estimate_pipeline_totals_positive(sample_commits, base_config):
    """Los totales agregados son estrictamente positivos y consistentes."""
    est = estimate_pipeline(sample_commits, base_config, version="v2.9.0")

    assert est.total_input_tokens > 0
    assert est.total_output_tokens > 0
    assert est.total_tokens == est.total_input_tokens + est.total_output_tokens
    assert est.total_cost_usd > 0
    # La suma de costos por paso coincide con la propiedad agregada
    assert abs(est.total_cost_usd - sum(s.cost_usd for s in est.steps)) < 1e-9


def test_estimate_pipeline_classifier_uses_haiku_pricing(sample_commits, base_config):
    """El paso de classify corre en Haiku (mas barato) mientras que los demas usan Sonnet."""
    est = estimate_pipeline(sample_commits, base_config, version="v2.9.0")
    classify_step = next(s for s in est.steps if s.step_name == "classify")
    generate_step = next(s for s in est.steps if s.step_name == "generate")

    assert classify_step.model == base_config.classifier_model
    assert generate_step.model == base_config.generator_model
    # Haiku input es 0.80/M, Sonnet es 3.00/M. Para conteos de tokens similares
    # el paso de classifier es mas barato por token de input.
    haiku_rate = classify_step.cost_usd / max(1, classify_step.input_tokens)
    sonnet_rate = generate_step.cost_usd / max(1, generate_step.input_tokens)
    assert haiku_rate < sonnet_rate


def test_estimate_pipeline_makes_no_api_calls(
    sample_commits, base_config, mock_anthropic,
):
    """La ruta de estimacion NUNCA debe llegar al cliente de Anthropic."""
    estimate_pipeline(sample_commits, base_config, version="v2.9.0")
    assert len(mock_anthropic.calls) == 0
