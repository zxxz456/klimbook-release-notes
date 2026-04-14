"""
conftest.py
==================================================

Descripcion:
-----------
Fixtures compartidos de pytest para la suite de tests de klimbook_release.
Provee clientes mock de Anthropic (sync y async) para que los tests de
integracion puedan ejercitar el pipeline completo sin gastar tokens ni
requerir API key.

Proposito del modulo:
--------------------
- Exponer MockAnthropic / MockAsyncAnthropic que registran llamadas y
  devuelven respuestas en cola o por defecto
- Parchar los clientes singleton en utils.py y formatter.py para que todo
  caller dentro de un test pase transparentemente por el mock
- Proveer listas de commits de ejemplo, config y directorios temporales
  de cache listos para usar

Contenido del modulo:
--------------------
1. FakeUsage / FakeContent / FakeResponse - Respuesta de Anthropic
                                            duck-typed
2. MockAnthropic - Mock sincrono con respuestas en cola
3. MockAsyncAnthropic - Mock async; comparte estado con el sync via
                        delegacion
4. mock_anthropic (fixture) - Instala los mocks via monkeypatch
5. sample_commits - Lista fija de RawCommit para tests del pipeline
6. base_config - Config minima apta para tests
7. tmp_cache (fixture) - ResponseCache temporal apuntando a tmp_path

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

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from klimbook_release import cache as cache_module
from klimbook_release import utils as utils_module
from klimbook_release import formatter as formatter_module
from klimbook_release.config import Config, PlatformConfig, PricingConfig
from klimbook_release.models import RawCommit


# =====================================================================
# Objetos fake del SDK de Anthropic (duck-typed)
# =====================================================================

@dataclass
class FakeUsage:
    """Espejo de anthropic.types.Usage con los campos que leemos."""
    input_tokens: int = 100
    output_tokens: int = 50


@dataclass
class FakeContent:
    """Espejo de un bloque de content devuelto por messages.create."""
    text: str


@dataclass
class FakeResponse:
    """Espejo de anthropic.types.Message con .content[0].text y .usage."""
    content: list[FakeContent]
    usage: FakeUsage


def _make_response(text: str, in_tokens: int = 100, out_tokens: int = 50) -> FakeResponse:
    """Construye un FakeResponse a partir de texto plano."""
    return FakeResponse(
        content=[FakeContent(text=text)],
        usage=FakeUsage(input_tokens=in_tokens, output_tokens=out_tokens),
    )


# =====================================================================
# Mock clients
# =====================================================================

class _MessagesNamespace:
    """Sustituto de client.messages que rutea create() al mock."""

    def __init__(self, client):
        self._client = client

    def create(self, **kwargs):
        """create() sincrono usado por utils.call_llm."""
        return self._client._dispatch(kwargs)


class _AsyncMessagesNamespace:
    """Sustituto de async_client.messages usado por formatter.py."""

    def __init__(self, client):
        self._client = client

    async def create(self, **kwargs):
        """create() async usado por formatter._format_single_platform."""
        return self._client._dispatch(kwargs)


class MockAnthropic:
    """Mock sincrono. Comparte cola/default con el mock async."""

    def __init__(self):
        self.calls: list[dict] = []
        self.queue: list[FakeResponse] = []
        self.default_text = "[]"
        self.messages = _MessagesNamespace(self)

    def queue_response(self, text: str, in_tokens: int = 100, out_tokens: int = 50):
        """Encola una respuesta que devolvera la proxima llamada a create()."""
        self.queue.append(_make_response(text, in_tokens, out_tokens))

    def set_default(self, text: str):
        """Fija la respuesta fallback usada cuando la cola esta vacia."""
        self.default_text = text

    def _dispatch(self, kwargs: dict) -> FakeResponse:
        """Registra la llamada y devuelve la proxima respuesta (cola o default)."""
        self.calls.append(kwargs)
        if self.queue:
            return self.queue.pop(0)
        return _make_response(self.default_text)


class MockAsyncAnthropic:
    """Mock async que delega en el mock sincrono hermano para el estado."""

    def __init__(self, sibling: MockAnthropic):
        self._sibling = sibling
        self.messages = _AsyncMessagesNamespace(self)

    @property
    def calls(self) -> list[dict]:
        """Proxy al log de llamadas del mock hermano."""
        return self._sibling.calls

    def _dispatch(self, kwargs: dict) -> FakeResponse:
        """Delega en el mock sincrono para compartir cola/defaults."""
        return self._sibling._dispatch(kwargs)


# =====================================================================
# Fixtures
# =====================================================================

@pytest.fixture
def mock_anthropic(monkeypatch):
    """
    Parcha los clientes sincrono y async de Anthropic con un mock compartido.

    Los tests interactuan con el MockAnthropic devuelto para encolar
    respuestas o inspeccionar llamadas registradas. Cualquier codigo del
    paquete que llame a get_sync_client() o _get_async_client() recibira
    el mock.
    """
    sync = MockAnthropic()
    async_mock = MockAsyncAnthropic(sync)

    # Reemplazar los singletons a nivel de modulo para que los helpers
    # devuelvan los mocks
    monkeypatch.setattr(utils_module, "_sync_client", sync)
    monkeypatch.setattr(utils_module, "_async_client", async_mock)
    monkeypatch.setattr(formatter_module, "_async_client", async_mock)

    # Parchar tambien los getters para que cualquier call path futuro
    # reciba los mismos mocks
    monkeypatch.setattr(utils_module, "get_sync_client", lambda: sync)
    monkeypatch.setattr(utils_module, "get_async_client", lambda: async_mock)
    monkeypatch.setattr(formatter_module, "_get_async_client", lambda: async_mock)

    return sync


@pytest.fixture
def tmp_cache(tmp_path, monkeypatch):
    """Configura el cache global para vivir en tmp_path (deshabilitado)."""
    cache = cache_module.configure_cache(cache_dir=tmp_path / "cache", enabled=False)
    yield cache
    # Resetear a un cache fresco deshabilitado despues del test
    cache_module.configure_cache(cache_dir=".cache", enabled=False)


@pytest.fixture
def sample_commits() -> list[RawCommit]:
    """Lista pequena y representativa de commits crudos."""
    return [
        RawCommit(
            hash="abc1234", hash_full="abc1234" + "0" * 33,
            message="feat(auth): add Google OAuth login",
            author="zxxz6", date="2026-04-10T12:00:00",
            files_changed=5,
        ),
        RawCommit(
            hash="def5678", hash_full="def5678" + "0" * 33,
            message="fix(blocks): correct grade conversion for V10",
            author="zxxz6", date="2026-04-11T09:30:00",
            files_changed=2,
        ),
        RawCommit(
            hash="aaa9999", hash_full="aaa9999" + "0" * 33,
            message="docs: update README with roadmap",
            author="zxxz6", date="2026-04-12T14:15:00",
            files_changed=1,
        ),
    ]


@pytest.fixture
def base_config() -> Config:
    """Config minima con dos plataformas, glosario recortado y pricing."""
    return Config(
        project_name="Klimbook",
        classifier_model="claude-haiku-4-5-20251001",
        generator_model="claude-sonnet-4-20250514",
        formatter_model="claude-sonnet-4-20250514",
        max_retries=2,
        retry_temperatures=[0.3, 0.0],
        max_parallel=2,
        cache_enabled=False,
        cache_dir=".cache",
        platforms={
            "github": PlatformConfig(
                enabled=True, language="en", max_chars=None,
                description="GitHub release",
            ),
            "playstore_en": PlatformConfig(
                enabled=True, language="en", max_chars=500,
                description="Play Store EN",
            ),
        },
        glossary=["boulder", "redpoint"],
        pricing={
            "claude-haiku-4-5-20251001": PricingConfig(input=0.80, output=4.00),
            "claude-sonnet-4-20250514": PricingConfig(input=3.00, output=15.00),
        },
    )
