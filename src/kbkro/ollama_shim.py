"""
ollama_shim.py
==================================================

Descripcion:
-----------
Adaptadores que hacen que la API de Ollama se vea como la de Anthropic.
Exponen la misma interfaz de superficie que `anthropic.Anthropic` y
`anthropic.AsyncAnthropic` — `client.messages.create(...)` devolviendo un
objeto con `.content[0].text` y `.usage.input_tokens` /
`.usage.output_tokens` — asi el pipeline de klimbook_release funciona sin
cambios al sustituir los singletons.

Proposito del modulo:
--------------------
- Permitir que utils.call_llm y formatter._format_single_platform usen
  Ollama sin re-implementar retry, cache, metricas ni truncacion
- Mapear `messages` estilo Anthropic a un payload /api/chat de Ollama
- Mapear `prompt_eval_count` / `eval_count` de la respuesta a input/output
  tokens para que las metricas del pipeline sigan sumando correctamente

Contenido del modulo:
--------------------
1. _Usage / _ContentBlock / _Response - Duck types que imitan al SDK
2. _SyncMessages / _AsyncMessages      - Namespaces con .create()
3. OllamaSyncShim                      - Sustituto de anthropic.Anthropic
4. OllamaAsyncShim                     - Sustituto de AsyncAnthropic

Detalles de la API /api/chat:
-----------------------------
- Endpoint: POST {host}/api/chat
- Body: {"model": ..., "messages": [...], "stream": false, "options": {
         "temperature": float, "num_predict": int (== max_tokens)}}
- Respuesta: {"message": {"role": "assistant", "content": "..."},
             "prompt_eval_count": int, "eval_count": int, ...}
- Prefill: el ultimo mensaje con role="assistant" se usa como prefijo; la
  respuesta continua desde ahi (mismo patron que Claude)

Metadata:
----------
* Autor: zxxz6 (Bryan Violante Arriaga)
* Version: 0.1.0
* Licencia: MIT

Historial:
------------
Autor       Fecha           Descripcion
zxxz6       18/04/2026      Creacion
"""

from dataclasses import dataclass
import logging
from typing import Any

import httpx

logger = logging.getLogger("kbkro.ollama")


# =====================================================================
# Duck types de respuesta (imitan anthropic.types.Message)
# =====================================================================

@dataclass
class _Usage:
    """Espejo de anthropic.types.Usage (solo los campos que leemos)."""
    input_tokens: int
    output_tokens: int


@dataclass
class _ContentBlock:
    """Bloque de content con .text (como anthropic.types.TextBlock)."""
    text: str
    type: str = "text"


@dataclass
class _Response:
    """Mensaje completo con .content[0].text y .usage.*"""
    content: list[_ContentBlock]
    usage: _Usage


# =====================================================================
# Helpers de (de)serializacion
# =====================================================================

def _build_payload(
    model: str,
    system: str,
    messages: list[dict[str, str]],
    temperature: float,
    max_tokens: int,
) -> dict[str, Any]:
    """Arma el JSON body para /api/chat a partir de la forma Anthropic."""
    ollama_messages: list[dict[str, str]] = []
    if system:
        ollama_messages.append({"role": "system", "content": system})
    for m in messages:
        # Los mensajes de Anthropic tienen la misma shape que Ollama:
        # {"role": "user"|"assistant", "content": str}
        ollama_messages.append(
            {"role": m["role"], "content": m["content"]}
        )

    return {
        "model": model,
        "messages": ollama_messages,
        "stream": False,
        # Desactivar modo thinking/reasoning explicitamente. Modelos como
        # gemma4/qwen3 pueden gastar todo num_predict en <think>...</think>
        # interno y dejar message.content vacio. El pipeline necesita JSON
        # y markdown directo, asi que forzamos salida sin razonamiento.
        # Para modelos que no soportan thinking, Ollama ignora este flag.
        "think": False,
        "options": {
            "temperature": float(temperature),
            "num_predict": int(max_tokens),
        },
    }


def _extract_prefill(messages: list[dict[str, str]]) -> str:
    """
    Si el ultimo mensaje es role=assistant lo tratamos como prefill
    (convencion de Claude). Devuelve su content, o "" si no hay prefill.
    """
    if messages and messages[-1].get("role") == "assistant":
        return messages[-1].get("content", "") or ""
    return ""


def _parse(data: dict[str, Any], prefill: str = "") -> _Response:
    """Convierte la respuesta JSON de Ollama en un _Response duck-typed.

    Normaliza el comportamiento de prefill al de Claude. Claude, dado un
    ultimo message con role=assistant, CONTINUA desde ese texto sin
    repetirlo en la respuesta. Ollama es inconsistente: algunos modelos
    continuan (como Claude) y otros repiten el prefill al inicio de
    message.content. Para que el caller pueda hacer `text = prefill + text`
    en ambos casos, aqui quitamos el prefill del content si aparece.
    """
    message = data.get("message") or {}
    content = message.get("content", "") or ""
    thinking = message.get("thinking", "") or ""

    # Normalizar prefill: si Ollama lo repitio, lo removemos. Si ya venia
    # como continuacion (al estilo Claude), no hacemos nada.
    prefill_stripped = False
    if prefill and content.startswith(prefill):
        content = content[len(prefill):]
        prefill_stripped = True
    done_reason = data.get("done_reason")
    inp = int(data.get("prompt_eval_count") or 0)
    out = int(data.get("eval_count") or 0)

    # Si el modelo gasto tokens en thinking y no emitio content (modelos
    # reasoning como qwen3/gemma4 cuando think=True es el default), al menos
    # avisamos que algo paso ahi para que el usuario sepa donde se fue su
    # num_predict.
    if thinking and not content:
        logger.warning(
            "Ollama devolvio thinking (%d chars) pero content vacio. "
            "El shim pasa think=False; si sigue pasando, el modelo ignora "
            "el flag — prueba con otro modelo o actualiza Ollama.",
            len(thinking),
        )

    # done_reason != "stop" indica truncacion (por num_predict o por context
    # window). Con caps altos esto casi siempre senala un prompt demasiado
    # grande o un modelo que se va por las ramas.
    if done_reason and done_reason != "stop":
        logger.warning(
            "Ollama done_reason=%s (in=%d out=%d) — respuesta truncada, "
            "revisa num_predict o usa un modelo menos verboso.",
            done_reason, inp, out,
        )

    # Con --verbose (DEBUG): volcado completo para diagnosticar. Mostramos
    # content y thinking por separado asi queda obvio donde se fueron los
    # tokens, y tambien el JSON crudo de Ollama (done_reason, stats, etc.).
    if logger.isEnabledFor(logging.DEBUG):
        _preview = lambda s: s if len(s) < 4000 else (s[:4000] + f"... [+{len(s)-4000} chars]")
        logger.debug(
            "Ollama response (in=%d out=%d done=%s prefill_stripped=%s):\n"
            "--- content (%d chars) ---\n%s\n"
            "--- thinking (%d chars) ---\n%s\n"
            "--- raw keys: %s",
            inp, out, done_reason, prefill_stripped,
            len(content), _preview(content),
            len(thinking), _preview(thinking),
            sorted(data.keys()),
        )

    return _Response(
        content=[_ContentBlock(text=content)],
        usage=_Usage(input_tokens=inp, output_tokens=out),
    )


# =====================================================================
# Namespaces .messages.create()
# =====================================================================

class _SyncMessages:
    """Sustituto de client.messages: expone create() sync."""

    def __init__(self, parent: "OllamaSyncShim"):
        self._parent = parent

    def create(
        self,
        *,
        model: str,
        system: str = "",
        messages: list[dict[str, str]],
        temperature: float = 0.0,
        max_tokens: int = 2000,
        **_: Any,  # ignora kwargs extra que Claude soporta y Ollama no
    ) -> _Response:
        """POST a /api/chat y mapeo a _Response."""
        payload = _build_payload(model, system, messages, temperature, max_tokens)
        prefill = _extract_prefill(messages)
        resp = self._parent._http().post("/api/chat", json=payload)
        resp.raise_for_status()
        return _parse(resp.json(), prefill=prefill)


class _AsyncMessages:
    """Sustituto de async_client.messages: expone create() coroutine."""

    def __init__(self, parent: "OllamaAsyncShim"):
        self._parent = parent

    async def create(
        self,
        *,
        model: str,
        system: str = "",
        messages: list[dict[str, str]],
        temperature: float = 0.0,
        max_tokens: int = 2000,
        **_: Any,
    ) -> _Response:
        """POST async a /api/chat (usado por formatter paralelo)."""
        payload = _build_payload(model, system, messages, temperature, max_tokens)
        resp = await self._parent._http().post("/api/chat", json=payload)
        resp.raise_for_status()
        return _parse(resp.json())


# =====================================================================
# Clientes shim
# =====================================================================

class OllamaSyncShim:
    """
    Drop-in para `anthropic.Anthropic` que habla con Ollama.

    Solo implementa `.messages.create(...)` porque es todo lo que
    klimbook_release.utils.call_llm necesita. Otros endpoints (batch,
    files, etc.) no se proveen.
    """

    def __init__(self, host: str = "http://localhost:11434", timeout: float = 600.0):
        """
        Args:
            host: URL base de Ollama (p.ej. "http://localhost:11434").
            timeout: Timeout en segundos por request. Generoso por default
                     porque modelos grandes en CPU pueden tardar minutos.
        """
        self.host = host.rstrip("/")
        self.timeout = timeout
        self._client: httpx.Client | None = None
        self.messages = _SyncMessages(self)

    def _http(self) -> httpx.Client:
        """Lazy singleton del cliente httpx sincrono."""
        if self._client is None:
            self._client = httpx.Client(base_url=self.host, timeout=self.timeout)
        return self._client


class OllamaAsyncShim:
    """
    Drop-in para `anthropic.AsyncAnthropic` que habla con Ollama.

    Solo implementa `.messages.create(...)` async, suficiente para el
    formatter paralelo que corre con asyncio.gather.
    """

    def __init__(self, host: str = "http://localhost:11434", timeout: float = 600.0):
        """Mismos args que OllamaSyncShim; usa httpx.AsyncClient internamente."""
        self.host = host.rstrip("/")
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self.messages = _AsyncMessages(self)

    def _http(self) -> httpx.AsyncClient:
        """Lazy singleton del cliente httpx async."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.host, timeout=self.timeout
            )
        return self._client
