"""
changelog.py
==================================================

Descripcion:
-----------
Lee y parsea las "Detailed Changelog" del README del proyecto destino para
inyectar contexto historico al generator. Sin LLM — regex puro sobre los
encabezados markdown estandar de Klimbook.

Proposito del modulo:
--------------------
- Localizar entradas tipo "#### Backend `vX.Y.Z` — Title (date)" en el
  README y extraer su cuerpo markdown completo
- Filtrar las ultimas N entradas por seccion (Backend, Frontend, Mobile)
  configurables
- Renderizar el resultado como bloque markdown listo para inyectar en
  el prompt del generator
- Degradar de forma graceful: si el README no existe o no tiene la
  seccion "Detailed Changelog", el pipeline continua sin contexto
  historico

Contenido del modulo:
--------------------
1. ENTRY_HEADER - Regex que captura section, version, title y date
2. ChangelogEntry - Dataclass con section, version, title, date, body
3. parse_changelog - Parsea el texto del README y devuelve list[Entry]
4. get_latest_per_section - Filtra las ultimas N por seccion
5. read_changelog - Lee archivo + parse + filter en un solo paso
6. to_context_block - Renderiza la lista a markdown para el prompt

Formato esperado:
----------------
    #### Backend `v2.10.0` — Traditional Climbing Support (April, 06 2026)

    - Bullet 1
    - Bullet 2
      - Nested bullet

    #### Backend `v2.9.0` — Onboarding Tutorial Support (April, 03 2026)
    ...

El em dash (—, U+2014) es parte del formato.

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
import logging
import re

logger = logging.getLogger("changelog")


# Captura: "#### Backend `v2.10.0` — Title (date)"
# Soporta em dash (—, U+2014) y guion comun (-) por si acaso.
ENTRY_HEADER = re.compile(
    r"^####\s+"
    r"(?P<section>Backend|Frontend|Mobile)\s+"
    r"`(?P<version>v[\d.]+(?:[a-zA-Z0-9.-]*)?)`\s+"
    r"[—-]\s+"
    r"(?P<title>.+?)\s+"
    r"\((?P<date>[^)]+)\)\s*$",
    re.MULTILINE,
)


# Patrones que marcan el final del cuerpo de una entrada (ademas del
# proximo "####"). Si aparecen, recortamos el body antes.
BODY_TERMINATORS = re.compile(
    r"\n---\s*\n",
    re.MULTILINE,
)


@dataclass
class ChangelogEntry:
    """
    Una entrada individual del changelog detallado.

    `body` conserva el markdown original sin el encabezado, listo para
    re-renderizar via to_markdown() o concatenar tal cual.
    """
    section: str
    version: str
    title: str
    date: str
    body: str = ""

    @property
    def heading(self) -> str:
        """Reconstruye el encabezado original."""
        return f"#### {self.section} `{self.version}` — {self.title} ({self.date})"

    def to_markdown(self) -> str:
        """Devuelve heading + body como markdown."""
        if self.body:
            return f"{self.heading}\n\n{self.body}"
        return self.heading


def parse_changelog(text: str) -> list[ChangelogEntry]:
    """
    Parsea todas las entradas Backend/Frontend/Mobile presentes en el
    texto. Las devuelve en el orden en que aparecen.

    Args:
        text: Contenido completo del README (u otro markdown).

    Returns:
        Lista de ChangelogEntry. Vacia si no se encuentra ninguna.
    """
    matches = list(ENTRY_HEADER.finditer(text))
    if not matches:
        return []

    entries: list[ChangelogEntry] = []
    for i, m in enumerate(matches):
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[body_start:body_end]

        # Recortar en el primer "---" que aparezca (separador de seccion
        # mayor: Backend / Frontend / Mobile).
        terminator = BODY_TERMINATORS.search(body)
        if terminator:
            body = body[: terminator.start()]

        body = body.strip()

        entries.append(
            ChangelogEntry(
                section=m.group("section"),
                version=m.group("version"),
                title=m.group("title").strip(),
                date=m.group("date").strip(),
                body=body,
            )
        )

    return entries


def get_latest_per_section(
    entries: list[ChangelogEntry],
    sections: tuple[str, ...] = ("Backend", "Frontend", "Mobile"),
    count: int = 1,
) -> list[ChangelogEntry]:
    """
    Devuelve las ultimas `count` entradas por seccion, en el orden:
    primero todas las de la primera seccion, luego la segunda, etc.

    "Ultimas" = las que aparecen primero en el README, asumiendo que el
    README esta ordenado de mas reciente a mas antiguo (convencion de
    Klimbook).

    Args:
        entries: Lista completa parseada por parse_changelog.
        sections: Secciones a incluir y su orden.
        count: Cuantas entradas tomar por seccion (>=1).

    Returns:
        Subset de entries con como maximo len(sections) * count items.
    """
    if count < 1:
        return []

    result: list[ChangelogEntry] = []
    for section in sections:
        section_entries = [e for e in entries if e.section == section]
        result.extend(section_entries[:count])
    return result


def read_changelog(
    readme_path: str | Path,
    sections: tuple[str, ...] = ("Backend", "Frontend", "Mobile"),
    count: int = 1,
) -> list[ChangelogEntry]:
    """
    Lee el README en disco, parsea y devuelve las ultimas N por seccion.

    Degrada graceful: si el archivo no existe o no contiene entradas,
    devuelve [] y loguea un warning. El pipeline puede continuar.

    Args:
        readme_path: Path al README del proyecto destino.
        sections: Secciones del changelog a buscar.
        count: Ultimas N entradas por seccion.

    Returns:
        list[ChangelogEntry] (posiblemente vacia).
    """
    path = Path(readme_path)
    if not path.is_file():
        logger.warning(f"[Changelog] README no encontrado: {path}")
        return []

    text = path.read_text(encoding="utf-8")
    all_entries = parse_changelog(text)

    if not all_entries:
        logger.warning(
            f"[Changelog] No se encontraron entradas Backend/Frontend/Mobile "
            f"en {path}"
        )
        return []

    selected = get_latest_per_section(all_entries, sections, count)
    logger.info(
        f"[Changelog] {len(selected)}/{len(all_entries)} entradas seleccionadas "
        f"(latest {count} por seccion: {', '.join(sections)})"
    )
    return selected


def to_context_block(entries: list[ChangelogEntry]) -> str:
    """
    Renderiza una lista de entries como bloque markdown listo para
    inyectar en el prompt del generator.

    Args:
        entries: Las entries seleccionadas.

    Returns:
        String markdown. Vacio si no hay entries.
    """
    if not entries:
        return ""

    parts = ["## Detailed changelog from project README\n"]
    parts.append(
        "Use these notes as the canonical source of truth for what shipped "
        "in this release. Mention features and fixes by name when relevant.\n"
    )
    for entry in entries:
        parts.append(entry.to_markdown())
    return "\n\n".join(parts)
