"""
test_changelog.py
==================================================

Descripcion:
-----------
Tests para el parser regex de changelog.py. Cubren extraccion de
secciones, filtrado por seccion, lectura de archivo y manejo de casos
borde (archivo inexistente, sin entradas, dividers entre secciones).

Proposito del modulo:
--------------------
- Verificar que ENTRY_HEADER captura section/version/title/date
- Verificar que parse_changelog separa cuerpos correctamente
- Verificar que get_latest_per_section respeta el orden y el count
- Verificar que read_changelog degrada graceful en errores I/O
- Verificar que to_context_block produce markdown valido

Contenido del modulo:
--------------------
1. test_parse_single_entry
2. test_parse_multiple_sections
3. test_parse_handles_section_dividers
4. test_parse_returns_empty_for_no_matches
5. test_get_latest_per_section_default
6. test_get_latest_per_section_count_two
7. test_read_changelog_missing_file
8. test_read_changelog_end_to_end (tmp_path)
9. test_to_context_block_empty
10. test_to_context_block_renders_entries

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

from klimbook_release.changelog import (
    ChangelogEntry, ENTRY_HEADER, parse_changelog,
    get_latest_per_section, read_changelog, to_context_block,
)


# Extracto de README de muestra con las 3 secciones y multiples versiones por seccion
SAMPLE_README = """# Klimbook

Some intro text.

### Detailed Changelog

### Backend

#### Backend `v2.10.0` — Traditional Climbing Support (April, 06 2026)

- New `is_trad` field on Block model (`shared/models.py`).
- Schema updates in `BlockCreate`, `BlockUpdate`, `BlockListResponse`.
  - Includes `is_trad: bool | None = None`.
- Alembic migration `u1v2w3x4y5z6`.

#### Backend `v2.9.0` — Onboarding Tutorial Support (April, 03 2026)

- New `first_login` preference field added to defaults.
- Schema updates in PreferencesResponse and PreferencesUpdateRequest.

#### Backend `v2.8.1` — Bug Fixes (March, 31 2026)

- Bug fix — `wall_id` not set on new blocks.

---
### Frontend

#### Frontend `v2.10.0` — Desktop Layout Overhaul (April, 08 2026)

- Self-rendered Home header on desktop.
- DesktopDrawer simplified (removed FAB integration).

#### Frontend `v2.9.0` — Trad Climbing & Topo Markers (April, 07 2026)

- Trad chip support on BlockPrevCard.

---
### Mobile

#### Mobile `v2.10.0` — Unified Action Menus (April, 10 2026)

- Removed dividers from all action menus.
- Trad chip on BlockCard.

#### Mobile `v2.9.0` — Onboarding Tutorial System (April, 03 2026)

- New TutorialOverlay component.
"""


def test_parse_single_entry():
    """Una entrada simple se parsea con todos los campos correctos."""
    text = "#### Backend `v1.0.0` — Hello World (January, 01 2026)\n\n- bullet"
    entries = parse_changelog(text)

    assert len(entries) == 1
    assert entries[0].section == "Backend"
    assert entries[0].version == "v1.0.0"
    assert entries[0].title == "Hello World"
    assert entries[0].date == "January, 01 2026"
    assert "bullet" in entries[0].body


def test_parse_multiple_sections():
    """Las 3 secciones del README de ejemplo se extraen completas."""
    entries = parse_changelog(SAMPLE_README)

    sections = [e.section for e in entries]
    assert sections.count("Backend") == 3
    assert sections.count("Frontend") == 2
    assert sections.count("Mobile") == 2

    # Verificar que el primer Backend es v2.10.0
    backend = [e for e in entries if e.section == "Backend"]
    assert backend[0].version == "v2.10.0"
    assert backend[0].title == "Traditional Climbing Support"
    assert "is_trad" in backend[0].body


def test_parse_handles_section_dividers():
    """El cuerpo de una entrada NO incluye el `---` ni `### Frontend`."""
    entries = parse_changelog(SAMPLE_README)

    last_backend = [e for e in entries if e.section == "Backend"][-1]
    # No debe contener el divider ni el header de la siguiente seccion
    assert "---" not in last_backend.body
    assert "### Frontend" not in last_backend.body


def test_parse_returns_empty_for_no_matches():
    """Texto sin entradas devuelve lista vacia."""
    assert parse_changelog("just some prose with no headers") == []
    assert parse_changelog("") == []
    # Headers de otras secciones no cuentan
    assert parse_changelog("#### Random `v1.0.0` — Title (date)") == []


def test_get_latest_per_section_default():
    """Default: 1 por seccion, en orden Backend → Frontend → Mobile."""
    entries = parse_changelog(SAMPLE_README)
    latest = get_latest_per_section(entries)

    assert len(latest) == 3
    assert latest[0].section == "Backend"
    assert latest[0].version == "v2.10.0"
    assert latest[1].section == "Frontend"
    assert latest[1].version == "v2.10.0"
    assert latest[2].section == "Mobile"
    assert latest[2].version == "v2.10.0"


def test_get_latest_per_section_count_two():
    """Count=2 devuelve las 2 mas recientes por seccion."""
    entries = parse_changelog(SAMPLE_README)
    latest = get_latest_per_section(entries, count=2)

    assert len(latest) == 6  # 2 backend + 2 frontend + 2 mobile
    backend = [e for e in latest if e.section == "Backend"]
    assert [e.version for e in backend] == ["v2.10.0", "v2.9.0"]


def test_get_latest_per_section_zero_returns_empty():
    """Count=0 (o negativo) devuelve lista vacia."""
    entries = parse_changelog(SAMPLE_README)
    assert get_latest_per_section(entries, count=0) == []


def test_get_latest_per_section_filters_unknown_sections():
    """Si pides una seccion que no existe, se omite sin error."""
    entries = parse_changelog(SAMPLE_README)
    latest = get_latest_per_section(entries, sections=("DesktopApp", "Backend"))
    assert len(latest) == 1
    assert latest[0].section == "Backend"


def test_read_changelog_missing_file(tmp_path, caplog):
    """Archivo inexistente devuelve [] sin lanzar excepcion."""
    missing = tmp_path / "no_such_file.md"
    result = read_changelog(missing)
    assert result == []


def test_read_changelog_end_to_end(tmp_path):
    """Lee desde disco y filtra correctamente."""
    readme = tmp_path / "README.md"
    readme.write_text(SAMPLE_README, encoding="utf-8")

    result = read_changelog(readme, count=1)

    assert len(result) == 3
    assert {e.section for e in result} == {"Backend", "Frontend", "Mobile"}


def test_read_changelog_no_entries_returns_empty(tmp_path):
    """README sin la seccion de changelog devuelve []."""
    readme = tmp_path / "README.md"
    readme.write_text("# Just a title\n\nNo changelog here.", encoding="utf-8")
    assert read_changelog(readme) == []


def test_to_context_block_empty():
    """Lista vacia produce string vacio."""
    assert to_context_block([]) == ""


def test_to_context_block_renders_entries():
    """El bloque markdown incluye heading, intro y bodies."""
    entries = parse_changelog(SAMPLE_README)
    latest = get_latest_per_section(entries)
    block = to_context_block(latest)

    assert "## Detailed changelog" in block
    # Cada entrada renderizada con su heading completo
    assert "#### Backend `v2.10.0`" in block
    assert "#### Frontend `v2.10.0`" in block
    assert "#### Mobile `v2.10.0`" in block
    # Y su contenido
    assert "is_trad" in block
    assert "Self-rendered Home header" in block


def test_changelog_entry_to_markdown():
    """ChangelogEntry.to_markdown reconstruye heading + body."""
    entry = ChangelogEntry(
        section="Backend",
        version="v1.0.0",
        title="Test Release",
        date="January, 01 2026",
        body="- Bullet one\n- Bullet two",
    )
    md = entry.to_markdown()
    assert "#### Backend `v1.0.0` — Test Release (January, 01 2026)" in md
    assert "- Bullet one" in md


def test_changelog_entry_to_markdown_empty_body():
    """Entry con body vacio devuelve solo el heading."""
    entry = ChangelogEntry(
        section="Frontend", version="v2.0.0", title="T", date="d",
    )
    assert entry.to_markdown() == "#### Frontend `v2.0.0` — T (d)"
