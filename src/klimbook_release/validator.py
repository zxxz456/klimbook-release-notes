"""
validator.py
==================================================

Descripcion:
-----------
Valida el ReleaseBundle final contra reglas especificas por plataforma sin
llamar a ningun LLM. Pydantic + regex puros. Produce un ValidationResult
con errores (bloquean) y warnings (informativos) por plataforma.

Proposito del modulo:
--------------------
- Confirmar que cada plataforma habilitada tiene un output en el bundle
- Verificar que cada output es no vacio, no es un error del formatter, y
  esta dentro del limite de chars
- Ejecutar chequeos de sanity por plataforma (markdown de GitHub, texto
  plano de Play Store, firma de Ko-fi, etc.)
- Confirmar que el metadata del bundle (version, fecha, commit_count)
  esta completo
- Imprimir un resumen legible del resultado de validacion

Contenido del modulo:
--------------------
1. ValidationIssue - Dataclass: platform, severity, message
2. ValidationResult - Dataclass con is_valid, issues, resumen por
                      plataforma; expone properties .errors y .warnings
3. validate_bundle - Orquestador que corre todos los chequeos
4. _check_missing_platforms - Error si una plataforma habilitada no tiene
                              output
5. _validate_platform_output - Chequeos por output (vacio, [ERROR],
                               limite, minimo)
6. _validate_github - Especifico de GitHub (headers, marcadores de lista)
7. _validate_playstore - Especifico de Play Store (sin markdown)
8. _validate_kofi - Especifico de Ko-fi (firma, texto plano)
9. _check_metadata - Completitud del metadata del bundle
10. print_validation_result - Printer de resumen en consola

Severidad:
---------
- error: Bloquea el release (is_valid = False)
- warning: Informativo, no bloquea

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

from .models import ReleaseBundle, PlatformOutput, ReleaseNotes, PLATFORM_LIMITS
from .config import Config

from dataclasses import dataclass
import logging
import re

logger = logging.getLogger("validator")


# =====================================================================
# Validation Result
# =====================================================================

@dataclass
class ValidationIssue:
    """Un problema encontrado durante la validacion."""
    platform: str       # plataforma afectada ("github", "playstore_en", etc.)
    severity: str       # "error" (bloquea), "warning" (no bloquea)
    message: str        # descripcion del problema


@dataclass
class ValidationResult:
    """Resultado completo de la validacion."""
    is_valid: bool                      # True si no hay errores (warnings OK)
    issues: list[ValidationIssue]       # lista de problemas encontrados
    summary: dict[str, dict]            # resumen por plataforma

    @property
    def errors(self) -> list[ValidationIssue]:
        """Solo los issues de severidad 'error'."""
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[ValidationIssue]:
        """Solo los issues de severidad 'warning'."""
        return [i for i in self.issues if i.severity == "warning"]


# =====================================================================
# Validators
# =====================================================================

def validate_bundle(bundle: ReleaseBundle, config: Config) -> ValidationResult:
    """
    Valida el bundle completo del release.
    
    Ejecuta todas las validaciones y retorna un resultado
    con la lista de issues encontrados.
    
    Un bundle es valido si no tiene issues de severidad "error".
    Los warnings son informativos pero no bloquean.
    
    Args:
        bundle: El bundle a validar
        config: Configuracion del proyecto
        
    Returns:
        ValidationResult con is_valid, issues, y summary
    """
    issues = []

    logger.info(f"[Validator] Validando bundle {bundle.version}")

    # 1. Verificar que no falten plataformas habilitadas
    issues.extend(_check_missing_platforms(bundle, config))

    # 2. Validar cada output individual
    for name, output in bundle.outputs.items():
        issues.extend(_validate_platform_output(name, output, config))

    # 3. Verificar metadata del bundle
    issues.extend(_check_metadata(bundle))

    # Construir resumen por plataforma
    summary = {}
    for name, output in bundle.outputs.items():
        platform_issues = [i for i in issues if i.platform == name]
        summary[name] = {
            "chars": output.char_count,
            "limit": output.max_chars,
            "within_limit": output.within_limit,
            "errors": len([i for i in platform_issues if i.severity == "error"]),
            "warnings": len([i for i in platform_issues if i.severity == "warning"]),
        }

    # El bundle es valido si no hay errores (warnings estan OK)
    errors = [i for i in issues if i.severity == "error"]
    is_valid = len(errors) == 0

    result = ValidationResult(
        is_valid=is_valid,
        issues=issues,
        summary=summary,
    )

    if is_valid:
        logger.info(
            f"[Validator] OK | {len(bundle.outputs)} plataformas | "
            f"{len(result.warnings)} warnings"
        )
    else:
        logger.warning(
            f"[Validator] FALLO | {len(errors)} errores | "
            f"{len(result.warnings)} warnings"
        )

    return result


def _check_missing_platforms(
    bundle: ReleaseBundle, config: Config
) -> list[ValidationIssue]:
    """Verifica que todas las plataformas habilitadas tengan output."""
    issues = []
    enabled = config.get_enabled_platforms()

    for name in enabled:
        if name not in bundle.outputs:
            issues.append(ValidationIssue(
                platform=name,
                severity="error",
                message=f"Plataforma '{name}' habilitada pero sin output en el bundle.",
            ))

    return issues


def _validate_platform_output(
    name: str, output: PlatformOutput, config: Config
) -> list[ValidationIssue]:
    """Valida el output de una plataforma individual."""
    issues = []

    # -- Verificar contenido vacio --
    if not output.content.strip():
        issues.append(ValidationIssue(
            platform=name,
            severity="error",
            message="Contenido vacio.",
        ))
        return issues  # no tiene sentido validar mas si esta vacio

    # -- Verificar contenido de error --
    # Si el formatter fallo, el contenido empieza con "[ERROR]"
    if output.content.startswith("[ERROR]"):
        issues.append(ValidationIssue(
            platform=name,
            severity="error",
            message=f"El formatter fallo: {output.content[:200]}",
        ))
        return issues

    # -- Verificar longitud maxima --
    if not output.within_limit:
        issues.append(ValidationIssue(
            platform=name,
            severity="error",
            message=(
                f"Contenido excede el limite: {output.char_count} chars "
                f"(maximo: {output.max_chars}). "
                f"Exceso: {output.char_count - output.max_chars} chars."
            ),
        ))

    # -- Verificar longitud minima --
    # Un output muy corto probablemente esta incompleto
    min_chars = 20
    if output.char_count < min_chars:
        issues.append(ValidationIssue(
            platform=name,
            severity="warning",
            message=f"Contenido muy corto: {output.char_count} chars (minimo sugerido: {min_chars}).",
        ))

    # -- Validaciones especificas por plataforma --
    if name == "github":
        issues.extend(_validate_github(output))
    elif name.startswith("playstore"):
        issues.extend(_validate_playstore(name, output))
    elif name == "kofi":
        issues.extend(_validate_kofi(output))

    return issues


def _validate_github(output: PlatformOutput) -> list[ValidationIssue]:
    """Validaciones especificas para GitHub release notes."""
    issues = []
    content = output.content

    # Debe tener al menos un header markdown
    if "#" not in content:
        issues.append(ValidationIssue(
            platform="github",
            severity="warning",
            message="No se encontraron headers markdown (#).",
        ))

    # Debe tener al menos una lista (lineas que empiezan con -)
    lines_with_dash = [l for l in content.split("\n") if l.strip().startswith("-")]
    if not lines_with_dash:
        issues.append(ValidationIssue(
            platform="github",
            severity="warning",
            message="No se encontraron listas (lineas con '-').",
        ))

    return issues


def _validate_playstore(name: str, output: PlatformOutput) -> list[ValidationIssue]:
    """Validaciones especificas para Google Play Store."""
    issues = []
    content = output.content

    # Play Store no soporta markdown headers
    if content.startswith("#"):
        issues.append(ValidationIssue(
            platform=name,
            severity="warning",
            message="Contiene headers markdown (#). Play Store no los renderiza.",
        ))

    # Verificar que no tenga formato markdown complejo
    # (bold, links, etc. que Play Store no soporta)
    if "**" in content or "[" in content:
        issues.append(ValidationIssue(
            platform=name,
            severity="warning",
            message="Contiene formato markdown (bold/links) que Play Store no soporta.",
        ))

    return issues


def _validate_kofi(output: PlatformOutput) -> list[ValidationIssue]:
    """Validaciones especificas para Ko-fi posts."""
    issues = []
    content = output.content

    # Debe terminar con la firma
    if "zxxz6" not in content.lower():
        issues.append(ValidationIssue(
            platform="kofi",
            severity="warning",
            message="No contiene la firma '— zxxz6'.",
        ))

    # No debe tener markdown headers (es texto plano)
    if content.startswith("#"):
        issues.append(ValidationIssue(
            platform="kofi",
            severity="warning",
            message="Contiene headers markdown (#). Ko-fi deberia ser texto plano.",
        ))

    return issues


def _check_metadata(bundle: ReleaseBundle) -> list[ValidationIssue]:
    """Verifica que el metadata del bundle este completo."""
    issues = []

    if not bundle.version:
        issues.append(ValidationIssue(
            platform="bundle",
            severity="error",
            message="Falta la version del release.",
        ))

    if not bundle.date:
        issues.append(ValidationIssue(
            platform="bundle",
            severity="error",
            message="Falta la fecha del release.",
        ))

    if bundle.commit_count == 0:
        issues.append(ValidationIssue(
            platform="bundle",
            severity="warning",
            message="El release tiene 0 commits.",
        ))

    return issues


# =====================================================================
# Print Helpers
# =====================================================================

def print_validation_result(result: ValidationResult):
    """Imprime el resultado de la validacion de forma legible."""
    status = "PASSED" if result.is_valid else "FAILED"

    print(f"\n{'='*60}")
    print(f"  Validation: {status}")
    print(f"{'='*60}")

    if result.errors:
        print(f"\n  Errors ({len(result.errors)}):")
        for issue in result.errors:
            print(f"    [ERROR] {issue.platform}: {issue.message}")

    if result.warnings:
        print(f"\n  Warnings ({len(result.warnings)}):")
        for issue in result.warnings:
            print(f"    [WARN]  {issue.platform}: {issue.message}")

    print(f"\n  Platform summary:")
    for name, info in result.summary.items():
        limit_str = f"/{info['limit']}" if info['limit'] else ""
        status_str = "OK" if info['errors'] == 0 else "FAIL"
        print(f"    [{status_str}] {name}: {info['chars']}{limit_str} chars")
