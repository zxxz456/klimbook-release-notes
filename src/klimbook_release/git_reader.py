"""
git_reader.py
==================================================

Descripcion:
-----------
Lee commits entre dos tags de Git usando GitPython. Sin LLM de por medio
— interaccion pura en Python con el repositorio local. Usa GitPython (no
subprocess) para evitar shell injection y obtener errores tipados.

Proposito del modulo:
--------------------
- Abrir un repositorio Git de forma segura (errores tipados para repos
  invalidos/bare/inexistentes)
- Validar que ambos tags existen; si no, reportar las coincidencias mas
  cercanas
- Iterar commits en el rango "version_from..version_to"
- Proveer helpers para listar tags y renderizar commits en texto plano

Contenido del modulo:
--------------------
1. read_commits - Devuelve list[RawCommit] entre dos tags
2. commits_to_text - Renderiza commits en formato "git log --oneline"
3. list_tags - Todos los tags del repositorio, ordenados
4. get_latest_tags - Los N tags mas recientes (default del CLI)

Manejo de errores:
-----------------
Envuelve InvalidGitRepositoryError, GitCommandNotFound, repos bare y tags
faltantes en RuntimeError/ValueError con mensajes accionables
(ej. muestra los ultimos 15 tags disponibles cuando no encuentra un tag).

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

from git import Repo, InvalidGitRepositoryError, GitCommandNotFound
from pathlib import Path
import logging

from .models import RawCommit

logger = logging.getLogger("git-reader")


def read_commits(
    repo_path: str,
    version_from: str,
    version_to: str,
) -> list[RawCommit]:
    """
    Lee commits entre dos tags de Git.
    
    Usa la sintaxis Git "v2.8.0..v2.9.0" que significa:
    "commits que estan en v2.9.0 pero NO en v2.8.0"
    Es decir, los commits nuevos de ese release.
    
    Args:
        repo_path: Ruta al repositorio Git (puede ser "." para el actual)
        version_from: Tag de inicio (ej: "v2.8.0")
        version_to: Tag de fin (ej: "v2.9.0")
        
    Returns:
        Lista de RawCommit ordenados del mas reciente al mas antiguo
        
    Raises:
        RuntimeError: Si no se puede abrir el repo
        ValueError: Si un tag no existe
    """
    repo_path = Path(repo_path).resolve()
    logger.info(f"[Git] Abriendo repo: {repo_path}")

    # Intentar abrir el repositorio
    try:
        repo = Repo(repo_path)
    except InvalidGitRepositoryError:
        raise RuntimeError(
            f"'{repo_path}' no es un repositorio Git valido. "
            f"Verifica la ruta o ejecuta 'git init'."
        )
    except GitCommandNotFound:
        raise RuntimeError(
            "Git no esta instalado o no esta en el PATH. "
            "Instala Git: https://git-scm.com/downloads"
        )
    except Exception as e:
        raise RuntimeError(f"Error abriendo el repo: {type(e).__name__}: {e}")

    if repo.bare:
        raise RuntimeError(
            "El repo esta en modo 'bare' (sin working tree). "
            "Usa un repo normal, no bare."
        )

    # Verificar que los tags existen.
    # Si el usuario escribe mal un tag (ej: "v2.80" en vez de "v2.8.0"),
    # es mejor dar un error claro con los tags disponibles.
    tag_names = [t.name for t in repo.tags]

    if not tag_names:
        raise ValueError(
            "No se encontraron tags en el repositorio. "
            "Crea tags con: git tag v1.0.0"
        )

    for tag in [version_from, version_to]:
        if tag not in tag_names:
            # Mostrar los ultimos 15 tags para ayudar al usuario
            recent_tags = sorted(tag_names)[-15:]
            raise ValueError(
                f"Tag '{tag}' no encontrado.\n"
                f"Tags disponibles (ultimos 15): {', '.join(recent_tags)}"
            )

    # Leer commits en el rango
    commit_range = f"{version_from}..{version_to}"
    logger.info(f"[Git] Leyendo commits: {commit_range}")

    commits = []
    for commit in repo.iter_commits(commit_range):
        # commit.stats.total retorna un dict con:
        # {"insertions": N, "deletions": N, "lines": N, "files": N}
        # "files" es la cantidad de archivos modificados en ese commit.
        try:
            files_changed = commit.stats.total.get("files", 0)
        except Exception:
            # Algunos commits (merges, empty) pueden no tener stats
            files_changed = 0

        # Solo tomar la primera linea del mensaje.
        # Los mensajes multi-linea tienen el titulo en la primera linea
        # y la descripcion detallada despues.
        first_line = commit.message.strip().split("\n")[0].strip()

        commits.append(RawCommit(
            hash=commit.hexsha[:7],
            hash_full=commit.hexsha,
            message=first_line,
            author=commit.author.name,
            date=commit.committed_datetime.isoformat(),
            files_changed=files_changed,
        ))

    logger.info(f"[Git] {len(commits)} commits encontrados entre {version_from} y {version_to}")

    if not commits:
        logger.warning(
            f"[Git] No se encontraron commits entre {version_from} y {version_to}. "
            f"Verifica que los tags sean correctos y que haya commits entre ellos."
        )

    return commits


def commits_to_text(commits: list[RawCommit]) -> str:
    """
    Convierte la lista de commits a texto plano para el clasificador.
    
    El formato es el mismo que 'git log --oneline':
    cada linea tiene "hash mensaje"
    
    Este texto es lo que el clasificador LLM recibe como input.
    
    Args:
        commits: Lista de RawCommit
        
    Returns:
        String con un commit por linea
    """
    if not commits:
        return ""
    return "\n".join(f"{c.hash} {c.message}" for c in commits)


def list_tags(repo_path: str = ".") -> list[str]:
    """
    Lista todos los tags del repositorio, ordenados.
    
    Util para el CLI: mostrar tags disponibles cuando el usuario
    no sabe que versiones existen.
    
    Args:
        repo_path: Ruta al repositorio Git
        
    Returns:
        Lista de nombres de tags ordenados
    """
    try:
        repo = Repo(Path(repo_path).resolve())
        return sorted([t.name for t in repo.tags])
    except Exception as e:
        logger.error(f"[Git] Error listando tags: {e}")
        return []


def get_latest_tags(repo_path: str = ".", n: int = 2) -> list[str]:
    """
    Retorna los N tags mas recientes.
    
    Util como default para el CLI: si el usuario no especifica tags,
    usar los dos mas recientes automaticamente.
    
    Args:
        repo_path: Ruta al repositorio Git
        n: Cantidad de tags a retornar
        
    Returns:
        Lista de los N tags mas recientes
    """
    tags = list_tags(repo_path)
    if len(tags) < n:
        return tags
    return tags[-n:]
