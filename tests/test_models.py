"""
test_models.py
==================================================

Descripcion:
-----------
Tests unitarios para los modelos Pydantic y el loader de config. Corre
offline — sin API key, sin red. Verifica reglas de validacion, defaults,
campos computados y calculo de costo.

Proposito del modulo:
--------------------
- Ejercitar validators de CommitEntry, PlatformOutput, ReleaseBundle
- Confirmar que PipelineMetrics acumula correctamente los totales por
  paso
- Confirmar defaults del Config y calculate_cost

Contenido del modulo:
--------------------
1. TestCommitEntry - Entrada valida, defaults, type literal invalido,
                     descripcion vacia, los 7 tipos validos
2. TestPlatformOutput - Dentro/fuera del limite, sin limite, contenido
                        vacio
3. TestReleaseBundle - all_within_limits, uno excede, validation_summary
4. TestPipelineMetrics - Acumulacion en add_step, totales de summary
5. TestConfig - Config por defecto, get_enabled_platforms, calculate_cost

Ejecutar:
---------
    pytest tests/test_models.py -v

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

import pytest
from klimbook_release.models import (
    RawCommit, CommitEntry, ReleaseNotes,
    PlatformOutput, ReleaseBundle, PLATFORM_LIMITS,
    StepMetrics, PipelineMetrics,
)
from klimbook_release.config import load_config, Config
from pydantic import ValidationError


# =====================================================================
# CommitEntry Tests
# =====================================================================

class TestCommitEntry:
    """Tests for CommitEntry field validators and type Literal."""

    def test_valid_entry(self):
        """Un commit valido debe pasar sin errores."""
        entry = CommitEntry(
            type="feature",
            description="add login endpoint",
            affected_service="auth",
            breaking=False,
        )
        assert entry.type == "feature"
        assert entry.description == "add login endpoint"
        assert entry.affected_service == "auth"
        assert entry.breaking is False

    def test_defaults(self):
        """Los defaults deben aplicarse correctamente."""
        entry = CommitEntry(type="fix", description="fix bug")
        assert entry.affected_service == "general"
        assert entry.breaking is False

    def test_invalid_type(self):
        """Un type que no esta en el Literal debe fallar."""
        with pytest.raises(ValidationError):
            CommitEntry(type="banana", description="invalid type")

    def test_empty_description(self):
        """Una description vacia debe fallar."""
        with pytest.raises(ValidationError):
            CommitEntry(type="feature", description="")

    def test_whitespace_description(self):
        """Una description con solo espacios debe fallar."""
        with pytest.raises(ValidationError):
            CommitEntry(type="feature", description="   ")

    def test_all_types(self):
        """Todos los tipos validos deben aceptarse."""
        valid_types = ["feature", "fix", "refactor", "docs", "chore", "test", "ci"]
        for t in valid_types:
            entry = CommitEntry(type=t, description=f"test {t}")
            assert entry.type == t


# =====================================================================
# PlatformOutput Tests
# =====================================================================

class TestPlatformOutput:
    """Tests for PlatformOutput char_count and within_limit computed fields."""

    def test_within_limit(self):
        """Content dentro del limite debe ser valido."""
        output = PlatformOutput(
            platform="playstore_en",
            content="Short update.",
            max_chars=500,
        )
        assert output.within_limit is True
        assert output.char_count == len("Short update.")

    def test_exceeds_limit(self):
        """Content que excede el limite debe marcarse como fuera de limite."""
        long_content = "x" * 600
        output = PlatformOutput(
            platform="playstore_en",
            content=long_content,
            max_chars=500,
        )
        assert output.within_limit is False
        assert output.char_count == 600

    def test_no_limit(self):
        """Sin limite de chars, siempre esta dentro del limite."""
        output = PlatformOutput(
            platform="github",
            content="x" * 10000,
            max_chars=None,
        )
        assert output.within_limit is True

    def test_empty_content(self):
        """Content vacio debe fallar."""
        with pytest.raises(ValidationError):
            PlatformOutput(platform="github", content="")


# =====================================================================
# ReleaseBundle Tests
# =====================================================================

class TestReleaseBundle:
    """Tests for ReleaseBundle aggregate helpers over multiple outputs."""

    def test_all_within_limits(self):
        """Bundle donde todos los outputs estan dentro de limites."""
        bundle = ReleaseBundle(
            version="v2.9.0",
            date="2026-04-03",
            commit_count=15,
            outputs={
                "github": PlatformOutput(
                    platform="github", content="Full notes here", max_chars=None
                ),
                "playstore_en": PlatformOutput(
                    platform="playstore_en", content="Short.", max_chars=500
                ),
            },
        )
        assert bundle.all_within_limits() is True

    def test_one_exceeds_limit(self):
        """Bundle donde un output excede su limite."""
        bundle = ReleaseBundle(
            version="v2.9.0",
            date="2026-04-03",
            commit_count=15,
            outputs={
                "github": PlatformOutput(
                    platform="github", content="OK", max_chars=None
                ),
                "playstore_en": PlatformOutput(
                    platform="playstore_en", content="x" * 600, max_chars=500
                ),
            },
        )
        assert bundle.all_within_limits() is False

    def test_validation_summary(self):
        """El resumen de validacion debe incluir chars, limit, y ok."""
        bundle = ReleaseBundle(
            version="v2.9.0",
            date="2026-04-03",
            commit_count=5,
            outputs={
                "kofi": PlatformOutput(
                    platform="kofi", content="Post here", max_chars=2000
                ),
            },
        )
        summary = bundle.validation_summary()
        assert "kofi" in summary
        assert summary["kofi"]["ok"] is True
        assert summary["kofi"]["limit"] == 2000


# =====================================================================
# PipelineMetrics Tests
# =====================================================================

class TestPipelineMetrics:
    """Tests for PipelineMetrics step accumulation and summary totals."""

    def test_add_steps(self):
        """Agregar steps debe acumular correctamente."""
        metrics = PipelineMetrics()

        metrics.add_step(StepMetrics(
            step_name="classify",
            model="haiku",
            input_tokens=500,
            output_tokens=200,
            cost_usd=0.001,
            elapsed_seconds=1.5,
        ))

        metrics.add_step(StepMetrics(
            step_name="generate",
            model="sonnet",
            input_tokens=300,
            output_tokens=800,
            cost_usd=0.013,
            elapsed_seconds=3.2,
        ))

        assert metrics.total_input_tokens == 800
        assert metrics.total_output_tokens == 1000
        assert len(metrics.steps) == 2

    def test_summary(self):
        """El resumen debe contener todos los campos."""
        metrics = PipelineMetrics()
        metrics.add_step(StepMetrics(
            step_name="test",
            model="haiku",
            input_tokens=100,
            output_tokens=50,
            cost_usd=0.0005,
            elapsed_seconds=0.8,
        ))

        s = metrics.summary()
        assert "total_steps" in s
        assert "total_tokens" in s
        assert "total_cost_usd" in s
        assert s["total_tokens"] == 150


# =====================================================================
# Config Tests
# =====================================================================

class TestConfig:
    """Tests for Config defaults and cost calculation."""

    def test_default_config(self):
        """La config por defecto debe tener valores razonables."""
        # Cargar sin archivo (usa defaults)
        config = load_config("nonexistent.yaml")
        assert config.project_name == "Klimbook"
        assert len(config.platforms) > 0
        assert len(config.glossary) > 0
        assert config.max_retries == 3

    def test_get_enabled_platforms(self):
        """Solo retorna plataformas habilitadas."""
        config = load_config("nonexistent.yaml")
        enabled = config.get_enabled_platforms()
        # Todos deben estar habilitados por defecto
        assert len(enabled) == len(config.platforms)

    def test_calculate_cost(self):
        """El calculo de costo debe ser correcto."""
        config = load_config("nonexistent.yaml")
        # 1M tokens input con Sonnet = $3.00
        cost = config.calculate_cost("claude-sonnet-4-20250514", 1_000_000, 0)
        assert abs(cost - 3.0) < 0.01
