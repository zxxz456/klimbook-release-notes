"""
prompts.py
==================================================

Descripcion:
-----------
Prompts centralizados para cada paso LLM del pipeline. Tenerlos todos en
un solo archivo hace la iteracion rapida y versiona cada cambio
limpiamente via 'git diff prompts.py'.

Proposito del modulo:
--------------------
- Proveer templates system + task para classifier, generator, formatter
- Exponer prompts del formatter por plataforma (GitHub, Play Store,
  App Store, Ko-fi)
- Proveer sufijos de instruccion de retry que se agregan cuando un paso
  falla
- Inyectar instruccion de glosario para outputs en espanol (terminos de
  escalada)

Contenido del modulo:
--------------------
1. CLASSIFIER_SYSTEM / CLASSIFIER_TASK - Prompts de clasificacion de
                                         commits
2. GENERATOR_SYSTEM / GENERATOR_TASK - Prompts maestros de release notes
                                       en markdown
3. EXAMPLE_GITHUB / EXAMPLE_PLAYSTORE_EN / EXAMPLE_PLAYSTORE_ES /
   EXAMPLE_APPSTORE / EXAMPLE_KOFI - Releases reales anteriores usados
   como ejemplo few-shot en los formatters
4. FORMATTER_GITHUB - Template markdown completo + link "Full Changelog"
5. FORMATTER_PLAYSTORE - Template <=500 chars, bullets simples,
                         user-facing
6. FORMATTER_APPSTORE - Template <=4000 chars, profesional y limpio
7. FORMATTER_KOFI - Template de post personal casual con firma zxxz6
8. GLOSSARY_INSTRUCTION / NO_GLOSSARY - Inyectado para outputs en espanol
9. RETRY_JSON_INVALID / RETRY_TOO_LONG / RETRY_VALIDATION_FAILED -
   Sufijos que se agregan a los prompts en retry para decirle a Claude
   que corregir

Metadata:
----------
* Autor: zxxz6 (Bryan Violante Arriaga)
* Version: 1.0.0
* Licencia: MIT

Historial:
------------
Autor       Fecha           Descripcion
zxxz6       13/04/2026      Agregadas constantes EXAMPLE_GITHUB,
                            EXAMPLE_PLAYSTORE_EN/ES, EXAMPLE_APPSTORE y
                            EXAMPLE_KOFI (ejemplos reales v2.10.0) y
                            placeholder {example} en los 4 FORMATTER_*
                            (few-shot prompting)
zxxz6       07/04/2026      Agregado placeholder {prior_context} en
                            GENERATOR_TASK para inyectar el detailed
                            changelog del README como contexto historico
zxxz6       03/04/2026      Creacion
"""


# =====================================================================
# PROMPTS DEL CLASIFICADOR
# =====================================================================
#
# El clasificador usa Haiku con temp=0 porque:
# - Es una tarea de clasificacion (no necesita creatividad)
# - Haiku es suficiente para entender commit messages
# - temp=0 produce resultados consistentes (mismo input = mismo output)
#

CLASSIFIER_SYSTEM = """\
You are a senior DevOps engineer who classifies Git commits by type.

Categories:
- feature: new functionality, new screens, new endpoints, new components
- fix: bug fixes, corrections, error handling fixes
- refactor: code restructuring without changing behavior, renaming, moving files
- docs: documentation changes, README updates, comment changes
- chore: version bumps, dependency updates, config changes, maintenance
- test: adding or modifying tests
- ci: CI/CD pipeline changes, GitHub Actions, Docker changes

Rules:
- If the commit follows Conventional Commits (feat:, fix:, refactor:, etc.), use that prefix as the category
- If not, infer the category from the message content
- For description: extract the informative part, remove hashes, issue references, and filler words
- For affected_service: look for scope patterns like feat(auth), fix(db). If none found, use "general"
- Set breaking=false unless the message explicitly says "BREAKING CHANGE" or "breaking"

IMPORTANT: Respond with a valid JSON array ONLY. No explanations, no markdown, just the JSON."""


CLASSIFIER_TASK = """\
Classify each of these Git commits:

<commits>
{commits}
</commits>

Return a JSON array where each element has this exact structure:
{{"type": "feature|fix|refactor|docs|chore|test|ci", "description": "clear description", "affected_service": "service_name", "breaking": false}}"""


# =====================================================================
# PROMPTS DEL GENERADOR
# =====================================================================
#
# El generador usa Sonnet con temp=0.3 porque:
# - Necesita calidad de escritura para las notas
# - temp=0.3 da algo de variacion sin ser impredecible
# - Sonnet es mejor que Haiku en redaccion y organizacion
#

GENERATOR_SYSTEM = """\
You are a technical writer for {project_name}, a social network for rock climbers.
You write clear, professional, and well-organized release notes in markdown.
Your audience includes both technical users (developers, contributors) and 
casual users (climbers who use the app)."""


GENERATOR_TASK = """\
Generate release notes for {project_name} {version} ({date}).
{prior_context}
Here are the classified changes for this release (extracted from Git commits):

<changes>
{changes}
</changes>

Write the release notes in this markdown format:

# {project_name} — Release Notes {version}
**{date}**

A brief summary paragraph (2-3 sentences) describing the overall theme of this release.

---

## What's New

### Features
- Brief, clear description of each feature

### Bug Fixes
- Brief, clear description of each fix

### Improvements
- Brief description of refactors and improvements

### Other Changes
- Docs, tests, chores, CI changes

---

Rules:
- Only include sections that have changes (if no fixes, skip "Bug Fixes")
- Each item should be 1-2 sentences max
- Use user-friendly language when possible
- Mention affected services in parentheses: "Fixed login timeout (auth)"
- If there are breaking changes, add a "Breaking Changes" section at the top with a warning
- Output ONLY the markdown, no preamble or explanation"""


# =====================================================================
# EJEMPLOS DEL FORMATEADOR (few-shot)
# =====================================================================
#
# Ejemplos reales de releases anteriores de Klimbook. Se inyectan en
# los prompts FORMATTER_* como referencia de estilo. El LLM los usa
# para igualar tono, estructura y nivel de detalle — no para copiar
# contenido literal (el prompt lo dice explicitamente).
#
# Estan hardcoded (no en config.yaml) porque son contratos de voz/estilo
# con la marca del producto. Si cambian, es un cambio de prompt y debe
# quedar en el git diff.
#

EXAMPLE_GITHUB = """\
# Klimbook Mobile — Release Notes v2.10.0
**April 11, 2026**

We are pleased to announce the release of Klimbook Mobile version 2.10.0. This release introduces unified action menus, traditional climbing support, Kilter-style topo markers, shareable card templates with customization, and menu-based navigation.

---

## Release Notes

### Unified Action Menu Design

All action menus across the app received a visual overhaul for a cleaner, more consistent look.

* **Standardized Styling:** Removed dividers and unified styling, behavior, and placement across `UserMenu`, `BlockCardMenu`, `ProjectCardMenu`, `WallActionMenu`, `LanguageActionMenu`, `PostOptionsMenu`, `VenueBookActionMenu`, `ProfileActionMenu`, and `RewardScreenActionMenu`.
* **Consistent Icon Coloring:** Menus now use consistent primary icon coloring and simplified item rendering.

### Traditional Climbing Support

* **Trad Chip:** `BlockCard` displays a "Trad" chip on blocks where `is_trad=True`, making it easy to spot traditional routes at a glance.
* **Trad Toggle:** `BlockRegisterModal` includes a toggle switch for setting the trad flag on outdoor route types (`Oruta`).
* **Service Updated:** `bookService` passes `is_trad` in create/update payloads.

### Kilter-Style Topo Markers

New hand and feet marker types for spray wall hold annotations.

* **Hand Holds (Purple, `#E040FB`):** Mark specific hand hold positions on topo images. Unlimited placement with own undo/clear behaviors.
* **Feet Holds (Cyan, `#00BCD4`):** Mark intended foot placements. Same behavior as hand hold markers.
* **WebView-Based Renderer:** `TopoMarkerModal` migrated from custom Canvas to a `WebView`-based renderer for improved marker precision and interaction.

### Menu Hub Navigation

Tabbed layouts replaced with menu hub screens for a cleaner navigation flow.

* **Book Menu:** Participants see Ascensions, Projects, Stats. Venues see Blocks, Rewards, Reviews.
* **Bookmarks Menu:** Split into Spots (bookmarked venues) and Blocks (bookmarked blocks).

---

## Installation

### iOS
Download the attached `.ipa` file to install the application on registered devices.

### Android
Download the attached `.apk` file to install the application on registered devices.

## What's Changed
* Features/sprint 11 by @zxxz456 in https://github.com/zxxz456/klimbook-mobile/pull/1

**Full Changelog**: https://github.com/zxxz456/klimbook-mobile/compare/v2.9.0...v2.10.0"""


EXAMPLE_PLAYSTORE_EN = """\
New shareable card templates! Customize your session stats card with 3 design styles, background colors, and text colors before sharing to Instagram. Traditional climbing support for outdoor routes with trad chip. New hand and feet topo markers for spray wall annotations. Cleaner menu navigation throughout the app.

Bug fixes and performance improvements."""


EXAMPLE_PLAYSTORE_ES = """\
Nuevas plantillas de tarjetas para compartir! Personaliza tu tarjeta de estadisticas con 3 estilos de diseno, colores de fondo y colores de texto antes de compartir en Instagram. Soporte para escalada tradicional en rutas outdoor con chip trad. Nuevos marcadores de topo para manos y pies en spray walls. Navegacion por menus mas limpia en toda la app.

Correccion de errores y mejoras de rendimiento."""


EXAMPLE_APPSTORE = """\
New shareable card templates! Customize your session stats card with 3 design styles, background colors, and text colors before sharing to Instagram. Traditional climbing support for outdoor routes with trad chip. New hand and feet topo markers for spray wall annotations. Cleaner menu navigation throughout the app.
Bug fixes and performance improvements."""


EXAMPLE_KOFI = """\
Weekly Update (v2.10.0)

Hey everyone!

Packed week. A lot of visual and functional changes landed across web and mobile.

The biggest one on mobile is shareable card templates. When you share your session stats to Instagram Stories, you can now pick from three card styles — simple, gradient, and minimal — and customize the background and text colors before sharing. It is a small thing but it makes the cards feel way more personal.

I also added traditional climbing support. If you are setting outdoor routes, you can now flag them as trad. A small orange "Trad" chip shows up on those blocks so you can spot them instantly.

For spray wall fans — there are now Kilter-style hand and feet topo markers. Purple for hands, cyan for feet. You can place as many as you want on your topo images to map out holds the way you would on a Kilter Board.

On the navigation side, both web and mobile moved away from tabbed layouts. Book, Bookmarks, and Visited Book sections now use clean menu hub screens instead of tabs. Participants see Ascensions, Projects, and Stats. Venues see Blocks, Rewards, and Reviews.

All action menus across the app got a visual cleanup too — removed dividers, added contextual icon colors, and unified the styling everywhere.

That is it for this one. See you next week!

— zxxz6"""


# =====================================================================
# PROMPTS DEL FORMATEADOR
# =====================================================================
#
# Cada plataforma tiene su propio prompt porque las reglas son
# muy diferentes entre ellas. GitHub permite markdown completo,
# Play Store tiene limite de 500 chars, Ko-fi es casual.
# Cada prompt incluye un ejemplo real (EXAMPLE_*) como referencia de
# estilo — few-shot prompting para mejorar calidad y consistencia.
#

FORMATTER_GITHUB = """\
Format these release notes for a GitHub Release page.

Use the example below as your style reference — match its tone, structure,
section ordering, and level of detail. Do NOT copy its content literally;
adapt the structure to the actual changes in <notes>.

<example>
{example}
</example>

<notes>
{notes}
</notes>

Requirements:
- Keep full markdown formatting with headers, lists, bold
- Add a "Full Changelog" link at the bottom:
  https://github.com/zxxz456/klimbook/compare/{version_from}...{version_to}
- Keep all technical details
- Output ONLY the formatted markdown"""


FORMATTER_PLAYSTORE = """\
Convert these release notes to Google Play "What's New" format.

Use the example below as your style reference — match its tone, sentence
shape, and level of detail. Do NOT copy its content literally.

<example>
{example}
</example>

<notes>
{notes}
</notes>

STRICT RULES:
- Maximum {max_chars} characters total (this is a HARD limit from Google)
- User-facing language ONLY (no technical jargon, no service names)
- No markdown headers or formatting (plain text only)
- Focus on what the user will NOTICE when using the app
- Language: {language}
{glossary_instruction}
- Count your characters carefully. If in doubt, be shorter.

Output ONLY the formatted text, nothing else."""


FORMATTER_APPSTORE = """\
Convert these release notes to Apple App Store "What's New" format.

Use the example below as your style reference — match its tone and
structure. Do NOT copy its content literally.

<example>
{example}
</example>

<notes>
{notes}
</notes>

RULES:
- Maximum {max_chars} characters
- Clean, user-friendly language
- Short paragraphs or bullet points
- No markdown formatting
- Focus on user benefits, not technical details
- Professional but approachable tone

Output ONLY the formatted text."""


FORMATTER_KOFI = """\
Write a Ko-fi post announcing this release to your supporters.

Use the example below as your voice reference — match its tone, pacing,
opening/closing style, and level of personal detail. Do NOT copy its
content literally; write a fresh post about the changes in <notes>.

<example>
{example}
</example>

<notes>
{notes}
</notes>

RULES:
- You are zxxz6, developer of Klimbook, a rock climber based in Puebla, Mexico
- Tone: casual, enthusiastic, grateful, personal
- Maximum {max_chars} characters
- Plain text, NO markdown headers (but emojis are ok, use sparingly)
- Structure: greeting, what you worked on this week, brief highlight of changes,
  thank supporters, sign off
- End with "— zxxz6"
- Include a brief call-to-action (like "if you enjoy Klimbook, sharing it helps a lot")
- Be genuine, not corporate

Output ONLY the post text."""


# =====================================================================
# INSTRUCCION DE GLOSARIO
# =====================================================================
#
# Esta instruccion se inyecta en los prompts de plataformas
# en espanol para que el traductor no traduzca terminos tecnicos
# de escalada.
#

GLOSSARY_INSTRUCTION = """\
IMPORTANT: Do NOT translate these climbing/technical terms — keep them in English:
{terms}"""


NO_GLOSSARY = ""  # Para plataformas en ingles que no necesitan glosario


# =====================================================================
# INSTRUCCIONES DE RETRY
# =====================================================================
#
# Instrucciones extra que se agregan al prompt cuando un paso falla
# y se reintenta. Le dicen a Claude que fue lo que salio mal
# para que lo corrija.
#

RETRY_JSON_INVALID = """\

IMPORTANT: Your previous response was not valid JSON. 
This time, respond with ONLY a valid JSON array. 
No text before or after. No markdown code blocks. Just the raw JSON."""


RETRY_TOO_LONG = """\

IMPORTANT: Your previous response was {actual_chars} characters but the 
limit is {max_chars}. This time, be MORE CONCISE. Cut descriptions shorter. 
Remove less important items if needed. Stay under {max_chars} characters."""


RETRY_VALIDATION_FAILED = """\

IMPORTANT: Your previous response failed validation: {error}
Please fix this issue and try again."""
