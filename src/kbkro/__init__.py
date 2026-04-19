"""
kbkro (klimbook release - ollama)
==================================================

Descripcion:
-----------
Variante del pipeline de klimbook_release que corre contra Ollama local
en vez de la API de Anthropic. Reutiliza toda la logica del paquete
klimbook_release (classifier, generator, formatter, cache, retry,
metricas, validacion) y solo sustituye los clientes de Anthropic por
adaptadores que hablan /api/chat de Ollama.

Proposito del paquete:
---------------------
- Exponer un comando `kbkro` (klimbook release ollama) independiente
- Parchar los singletons de Anthropic con OllamaSyncShim/AsyncShim antes
  de correr el pipeline
- Normalizar los 3 roles (classifier, generator, formatter) al mismo
  modelo local pasado por --model (ej. gemma4:26b, llama3.2:3b, etc.)
- Dejar costo en 0 USD (hardware local)

Uso basico:
-----------
    kbkro generate --from v2.10.0 --to v2.11.0 --model gemma4:26b

Contenido del paquete:
---------------------
1. ollama_shim - OllamaSyncShim / OllamaAsyncShim duck-typed como los
                 clientes de Anthropic
2. cli         - Typer app con el comando `generate`

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

__version__ = "0.1.0"
