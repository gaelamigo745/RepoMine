import hashlib
import json
import os
import queue
import re
import subprocess
import sys
import threading
import webbrowser
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from launcher.repomine_core import mod_ids_from_jar, validate_manifest, atomic_json, safe_relative

# ================= CONFIGURACIÓN =================

REPO_OWNER = "gaelamigo745"
REPO_NAME = "RepoMine"
BRANCH = "main"

MINECRAFT_VERSION = "1.21.1"
LOADER = "NeoForge"
LOADER_VERSION = "21.1.233"
PACK_NAME = "Servidor Gael"

MODS_FOLDER = "mods"
REPO_MODS_FOLDER = "mods"
RESOURCEPACKS_FOLDER = "Resourcepacks"
REPO_RESOURCEPACKS_FOLDER = "Resourcepacks"
OUTPUT_FILE = "manifest.json"

# =================================================

PROJECT_DIR = Path(__file__).resolve().parent
MANIFEST_PATH = PROJECT_DIR / OUTPUT_FILE
MODS_PATH = PROJECT_DIR / MODS_FOLDER
RESOURCEPACKS_PATH = PROJECT_DIR / RESOURCEPACKS_FOLDER
SETTINGS_PATH = Path(os.getenv("LOCALAPPDATA", str(PROJECT_DIR))) / "RepoMine" / "manager_config.json"
GITHUB_MAX_FILE_SIZE = 100 * 1024 * 1024
GITHUB_WARNING_FILE_SIZE = 50 * 1024 * 1024
PACK_CONFIG_PATH = PROJECT_DIR / "pack_config.json"
SHARED_CONFIG_PATH = PROJECT_DIR / "shared-config"


def mod_directories():
    return {"mods": MODS_PATH, "modsCliente": PROJECT_DIR / "modsCliente",
            "modsServer": PROJECT_DIR / "modsServer"}


def local_mod_files():
    return sorted((p for directory in mod_directories().values() for p in directory.glob("*.jar")),
                  key=lambda p: p.name.lower())


def manifest_mod_names(manifest):
    return {entry["file"] for section in mod_directories()
            for entry in manifest.get(section, [])}


def load_pack_config(config=None):
    data = {}
    if PACK_CONFIG_PATH.exists():
        data = json.loads(PACK_CONFIG_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("pack_config.json debe contener un objeto.")
    return {**data, **(config or {})}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fallback_mod_id(filename: str) -> str:
    """Crea un ID estable desde el nombre cuando el JAR no trae metadatos."""
    stem = Path(filename).stem.lower()
    version = re.search(r"[-_.]v?\d", stem)
    return stem[:version.start()] if version else stem


def mod_id_from_jar(path: Path) -> str:
    """Obtiene el ID oficial del mod desde los metadatos incluidos en su JAR."""
    ids = mod_ids_from_jar(path)
    if not ids:
        return fallback_mod_id(path.name)
    normalized = re.sub(r"[^a-z0-9]", "", fallback_mod_id(path.name))
    return next((value for value in ids if re.sub(r"[^a-z0-9]", "", value) == normalized), ids[0])


def raw_github_url(folder: str, filename: str, owner=REPO_OWNER, repo=REPO_NAME, branch=BRANCH) -> str:
    safe_path = "/".join(quote(part, safe="") for part in (folder + "/" + filename).split("/"))
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{safe_path}"


def format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} GB"


def run_git(*args: str, check: bool = True, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=PROJECT_DIR,
        text=True,
        capture_output=True,
        check=check,
        timeout=timeout,
    )


def load_manifest() -> dict:
    if not MANIFEST_PATH.exists():
        return {}
    try:
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def load_manager_settings() -> dict:
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_manager_settings(settings: dict) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = SETTINGS_PATH.with_suffix(".tmp")
    temporary_path.write_text(
        json.dumps(settings, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary_path, SETTINGS_PATH)


def git_changes(paths=None) -> list[str]:
    args = ["status", "--short", "--untracked-files=all"]
    if paths:
        args.extend(["--", *paths])
    try:
        result = run_git(*args)
    except (FileNotFoundError, subprocess.CalledProcessError):
        return []
    return [line for line in result.stdout.splitlines() if line.strip()]


def branch_status() -> str:
    try:
        result = run_git("status", "--short", "--branch", "--untracked-files=no")
    except (FileNotFoundError, subprocess.CalledProcessError):
        return "Git no disponible"
    first_line = result.stdout.splitlines()[0] if result.stdout else ""
    if "ahead" in first_line and "behind" in first_line:
        return "Ramas divergentes"
    if "ahead" in first_line:
        return "Commits por subir"
    if "behind" in first_line:
        return "Cambios por descargar"
    return "Sincronizado"


def git_preflight(branch=BRANCH, owner=REPO_OWNER, repo=REPO_NAME) -> dict:
    """Comprueba que un push vaya a la rama y repositorio esperados."""
    report = {
        "ready": False,
        "error": "",
        "current_branch": "",
        "remote": "",
        "ahead": 0,
        "behind": 0,
        "outgoing": [],
    }
    try:
        report["current_branch"] = run_git("branch", "--show-current").stdout.strip()
        report["remote"] = run_git("remote", "get-url", "origin").stdout.strip()
        upstream = run_git(
            "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"
        ).stdout.strip()

        if report["current_branch"] != branch:
            report["error"] = (
                f"La rama activa es '{report['current_branch'] or 'HEAD separado'}', "
                f"pero la publicación está configurada para '{branch}'."
            )
            return report
        if upstream != f"origin/{branch}":
            report["error"] = (
                f"La rama sigue a '{upstream}', no a 'origin/{branch}'."
            )
            return report

        normalized_remote = report["remote"].lower().replace(":", "/").rstrip("/")
        if normalized_remote.endswith(".git"):
            normalized_remote = normalized_remote[:-4]
        expected = f"github.com/{owner}/{repo}".lower()
        if not normalized_remote.endswith(expected):
            report["error"] = (
                f"El remoto origin ({report['remote']}) no coincide con "
                f"{owner}/{repo}."
            )
            return report

        counts = run_git(
            "rev-list", "--left-right", "--count", "HEAD...@{u}"
        ).stdout.split()
        report["ahead"], report["behind"] = map(int, counts[:2])
        if report["behind"]:
            report["error"] = (
                "La rama local está detrás de GitHub. Actualízala antes de publicar "
                "para evitar sobrescribir o mezclar cambios inesperados."
            )
            return report

        outgoing = run_git(
            "log", "--format=%h  %s", "@{u}..HEAD"
        ).stdout.splitlines()
        report["outgoing"] = [line for line in outgoing if line.strip()]
        report["ready"] = True
    except FileNotFoundError:
        report["error"] = "No se encontró Git en el equipo."
    except subprocess.TimeoutExpired:
        report["error"] = "Git tardó demasiado en responder."
    except (subprocess.CalledProcessError, ValueError, IndexError) as error:
        detail = getattr(error, "stderr", "") or getattr(error, "stdout", "") or str(error)
        report["error"] = f"No se pudo validar el repositorio: {str(detail).strip()}"
    return report


def generate_manifest(log=print, progress=None, config=None) -> bool:
    """Crea manifest.json a partir de todos los .jar locales."""
    try:
        config = load_pack_config(config)
    except (OSError, ValueError) as error:
        log(f"Configuración inválida: {error}")
        return False
    if not MODS_PATH.exists():
        log(f"No existe la carpeta: {MODS_PATH}")
        return False

    jar_files = [(section, jar) for section, directory in mod_directories().items()
                 for jar in sorted(directory.glob("*.jar"), key=lambda p: p.name.lower())]
    if not jar_files:
        log("No se encontraron archivos .jar dentro de mods/.")
        return False

    owner = config.get("repo_owner", REPO_OWNER)
    repo = config.get("repo_name", REPO_NAME)
    branch = config.get("branch", BRANCH)
    log(f"Analizando {len(jar_files)} mods...")

    mod_groups = {section: [] for section in mod_directories()}
    declared_ids = {}
    total = len(jar_files)
    for index, (section, jar) in enumerate(jar_files, start=1):
        if jar.is_symlink():
            log(f"Error: no se permiten enlaces simbólicos: {jar.name}")
            return False
        before = jar.stat()
        if before.st_size > GITHUB_MAX_FILE_SIZE:
            log(
                f"Error: {jar.name} pesa {format_size(before.st_size)} y supera "
                "el límite de 100 MB de GitHub."
            )
            return False
        if before.st_size > GITHUB_WARNING_FILE_SIZE:
            log(f"Aviso: {jar.name} es grande ({format_size(before.st_size)}).")
        if not zipfile.is_zipfile(jar):
            log(f"Error: {jar.name} no es un archivo JAR/ZIP válido.")
            return False
        try:
            ids = mod_ids_from_jar(jar)
            mod_id = mod_id_from_jar(jar)
        except (ValueError, KeyError, OSError) as error:
            log(f"Metadatos inválidos en {jar.name}: {error}")
            return False
        for declared in ids or [mod_id]:
            if declared in declared_ids:
                log(f"ID duplicado {declared}: {declared_ids[declared]} y {jar.name}")
                return False
            declared_ids[declared] = jar.name
        if not mod_id:
            log(f"Error: no se pudo obtener el ID interno de {jar.name}.")
            return False
        if progress:
            progress(index - 1, total, f"Calculando SHA-256: {jar.name}")
        file_hash = sha256_file(jar)
        after = jar.stat()
        if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
            log(f"Error: {jar.name} cambió mientras se generaba el manifiesto.")
            return False
        mod_groups[section].append({
            "id": mod_id,
            "ids": ids or [mod_id],
            "size": before.st_size,
            "file": jar.name,
            "url": raw_github_url(section, jar.name, owner, repo, branch),
            "sha256": file_hash,
        })
        log(f"✓ [{index}/{total}] {jar.name}")

    resourcepacks = []
    if RESOURCEPACKS_PATH.exists():
        pack_files = sorted(RESOURCEPACKS_PATH.glob("*.zip"), key=lambda path: path.name.lower())
        log(f"Analizando {len(pack_files)} paquetes de recursos...")
        for index, pack in enumerate(pack_files, start=1):
            if pack.is_symlink():
                log(f"Error: no se permiten enlaces simbólicos: {pack.name}")
                return False
            before = pack.stat()
            if before.st_size > GITHUB_MAX_FILE_SIZE:
                log(f"Error: {pack.name} supera el límite de 100 MB de GitHub.")
                return False
            if not zipfile.is_zipfile(pack):
                log(f"Error: {pack.name} no es un paquete ZIP válido.")
                return False
            if progress:
                progress(index - 1, len(pack_files), f"Calculando SHA-256: {pack.name}")
            file_hash = sha256_file(pack)
            after = pack.stat()
            if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
                log(f"Error: {pack.name} cambió mientras se generaba el manifiesto.")
                return False
            resourcepacks.append({
                "size": before.st_size,
                "file": pack.name,
                "url": raw_github_url(REPO_RESOURCEPACKS_FOLDER, pack.name, owner, repo, branch),
                "sha256": file_hash,
            })
            log(f"✓ [recurso {index}/{len(pack_files)}] {pack.name}")

    configs = []
    try:
        policies = config.get("config_policies", {})
        if not isinstance(policies, dict):
            raise ValueError("config_policies debe ser un objeto.")
        for path in sorted(SHARED_CONFIG_PATH.rglob("*")):
            if path.is_symlink() or not path.resolve().is_relative_to(SHARED_CONFIG_PATH.resolve()):
                raise ValueError(f"No se permiten enlaces en configuraciones: {path}")
            if not path.is_file() or any(part.startswith(".") for part in path.relative_to(SHARED_CONFIG_PATH).parts):
                continue
            name = safe_relative(path.relative_to(SHARED_CONFIG_PATH).as_posix(), nested=True)
            before = path.stat()
            if before.st_size > GITHUB_MAX_FILE_SIZE:
                raise ValueError(f"Configuración demasiado grande: {name}")
            digest = sha256_file(path)
            after = path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError(f"La configuración cambió durante la lectura: {name}")
            configs.append({"file": name, "size": before.st_size, "sha256": digest,
                "url": raw_github_url("shared-config", name, owner, repo, branch),
                "policy": policies.get(name, "default")})
    except (OSError, ValueError) as error:
        log(f"Error en configuraciones: {error}")
        return False
    manifest = {
        "schema_version": 2,
        "pack_name": config.get("pack_name", PACK_NAME),
        "minecraft_version": config.get("minecraft_version", MINECRAFT_VERSION),
        "loader": config.get("loader", LOADER),
        "loader_version": config.get("loader_version", LOADER_VERSION),
        **mod_groups,
        "resourcepacks": resourcepacks,
        "configs": configs,
        "server": {"name": config.get("server_name", PACK_NAME),
                   "address": config.get("server_address", "tecserver.playit.plus")},
    }
    try:
        validate_manifest(manifest)
    except ValueError as error:
        log(f"Manifiesto inválido: {error}")
        return False
    temporary_path = MANIFEST_PATH.with_suffix(".json.tmp")
    temporary_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary_path, MANIFEST_PATH)
    if progress:
        progress(total, total, "Manifiesto terminado")
    log(f"Manifiesto generado correctamente con {total} mods y {len(resourcepacks)} paquetes de recursos.")
    log(f"Guardado en: {MANIFEST_PATH}")
    return True


def publish_changes(
    paths: list[str],
    message: str,
    log=print,
    progress=None,
    branch=BRANCH,
    owner=REPO_OWNER,
    repo=REPO_NAME,
) -> bool:
    """Hace commit y push únicamente de las rutas indicadas."""
    if not paths:
        log("No hay archivos publicables.")
        return True
    try:
        if progress:
            progress(0, 4, "Comprobando GitHub")
        run_git("rev-parse", "--is-inside-work-tree")
        log(f"Actualizando la referencia de origin/{branch}...")
        run_git("fetch", "origin", branch, timeout=180)
        preflight = git_preflight(branch, owner, repo)
        if not preflight["ready"]:
            log(f"Error de validación: {preflight['error']}")
            return False

        if progress:
            progress(1, 4, "Preparando archivos")
        run_git("add", "--", *paths)

        staged = run_git("diff", "--cached", "--quiet", "--", *paths, check=False)
        if staged.returncode == 0:
            log("No hay cambios nuevos para crear otro commit.")
        if staged.returncode != 1:
            if staged.returncode != 0:
                raise subprocess.CalledProcessError(
                    staged.returncode, staged.args, staged.stdout, staged.stderr
                )
        else:
            if progress:
                progress(2, 4, "Creando commit")
            log(f"Creando commit: {message}")
            run_git("commit", "-m", message, "--", *paths)

        after_commit = git_preflight(branch, owner, repo)
        if not after_commit["ready"]:
            log(f"Error de validación: {after_commit['error']}")
            return False
        if not after_commit["outgoing"]:
            log("No hay cambios ni commits pendientes para subir.")
            if progress:
                progress(4, 4, "Sin cambios")
            return True

        log(f"Se publicarán {len(after_commit['outgoing'])} commit(s) salientes:")
        for commit in after_commit["outgoing"]:
            log(f"  • {commit}")

        if progress:
            progress(3, 4, f"Subiendo a origin/{branch}")
        log(f"Subiendo cambios a origin/{branch}...")
        run_git("push", "origin", f"HEAD:refs/heads/{branch}", timeout=300)
    except FileNotFoundError:
        log("Error: no se encontró Git. Instálalo y vuelve a intentarlo.")
        return False
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or str(error)).strip()
        log(f"Error al publicar: {detail}")
        return False
    except subprocess.TimeoutExpired:
        log("Error: la operación de Git superó el tiempo de espera.")
        return False

    if progress:
        progress(4, 4, "Publicación completada")
    log("Cambios publicados correctamente en GitHub.")
    return True


def upload_manifest(log=print, progress=None, message="Actualiza manifest", config=None) -> bool:
    if not MANIFEST_PATH.exists():
        log("Primero genera el manifest.json.")
        return False
    if not verify_manifest_assets(log):
        return False
    config = load_pack_config(config)
    return publish_changes(
        [OUTPUT_FILE, "pack_config.json"], message, log, progress,
        config.get("branch", BRANCH),
        config.get("repo_owner", REPO_OWNER),
        config.get("repo_name", REPO_NAME),
    )


def upload_mods(log=print, progress=None, message="Actualiza mods", config=None) -> bool:
    config = load_pack_config(config)
    return publish_changes(
        publication_asset_paths(), message, log, progress,
        config.get("branch", BRANCH),
        config.get("repo_owner", REPO_OWNER),
        config.get("repo_name", REPO_NAME),
    )


def generate_and_upload(log=print, progress=None, message="Actualiza manifest y mods", config=None) -> bool:
    config = load_pack_config(config)
    if not generate_manifest(log, progress, config):
        return False
    config = config or {}
    return publish_changes(
        [OUTPUT_FILE, "pack_config.json", *publication_asset_paths()],
        message,
        log,
        progress,
        config.get("branch", BRANCH),
        config.get("repo_owner", REPO_OWNER),
        config.get("repo_name", REPO_NAME),
    )


def verify_manifest_assets(log=print):
    """Evita publicar un manifiesto generado antes del último cambio local."""
    try:
        manifest = validate_manifest(json.loads(MANIFEST_PATH.read_text(encoding="utf-8")))
        for section, directory, pattern in (*((name, directory, "*.jar") for name, directory in mod_directories().items()),
                ("resourcepacks", RESOURCEPACKS_PATH, "*.zip"),
                ("configs", SHARED_CONFIG_PATH, "**/*")):
            expected = {entry["file"] for entry in manifest.get(section, [])}
            actual = {p.relative_to(directory).as_posix() for p in directory.glob(pattern) if p.is_file() and not any(part.startswith(".") for part in p.relative_to(directory).parts)}
            if expected != actual:
                raise ValueError(f"Cambió el contenido de {directory.name}; genera de nuevo el manifiesto.")
            for entry in manifest.get(section, []):
                path = directory / entry["file"]
                if sha256_file(path) != entry["sha256"]:
                    raise ValueError(f"Cambió {entry['file']}; genera de nuevo el manifiesto.")
        return True
    except (OSError, ValueError) as error:
        log(f"No se puede publicar: {error}")
        return False


def publication_asset_paths():
    """Selección explícita: nunca incluye el ZIP de transporte ni archivos .disable."""
    paths = []
    for folder, pattern in (*((folder, "*.jar") for folder in mod_directories()), (REPO_RESOURCEPACKS_FOLDER, "*.zip")):
        selector = f":(glob){folder}/{pattern}"
        if list((PROJECT_DIR / folder).glob(pattern)) or run_git("ls-files", "--", selector).stdout.strip():
            paths.append(selector)
    if SHARED_CONFIG_PATH.exists() or run_git("ls-files", "--", "shared-config").stdout.strip():
        paths.append("shared-config")
    return paths


class ManifestManagerApp:
    BG = "#090E1A"
    SIDEBAR = "#0D1424"
    CARD = "#121B2E"
    CARD_ALT = "#172238"
    BORDER = "#24324D"
    TEXT = "#F4F7FC"
    MUTED = "#93A4BF"
    ACCENT = "#7C6DFF"
    ACCENT_HOVER = "#6B5BEF"
    GREEN = "#45D483"
    AMBER = "#FFB84D"
    RED = "#FF6B78"
    BLUE = "#4EB5FF"

    def __init__(self, root):
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.root = root
        self.events = queue.Queue()
        self.action_buttons = []
        self.nav_buttons = {}
        self.pages = {}
        self.current_page = "Resumen"
        self.busy = False

        manifest = load_manifest()
        self.config = {
            "repo_owner": REPO_OWNER,
            "repo_name": REPO_NAME,
            "branch": BRANCH,
            "pack_name": manifest.get("pack_name", PACK_NAME),
            "minecraft_version": manifest.get("minecraft_version", MINECRAFT_VERSION),
            "loader": manifest.get("loader", LOADER),
            "loader_version": manifest.get("loader_version", LOADER_VERSION),
            "server_name": manifest.get("server", {}).get("name", PACK_NAME),
            "server_address": manifest.get("server", {}).get("address", "tecserver.playit.plus"),
        }
        saved_settings = load_manager_settings()
        for key in self.config:
            if isinstance(saved_settings.get(key), str) and saved_settings[key].strip():
                self.config[key] = saved_settings[key].strip()
        self.config.update(load_pack_config())

        self.root.title("RepoMine · Administrador de mods")
        self.root.geometry("1160x760")
        self.root.minsize(960, 650)
        self.root.configure(bg=self.BG)
        self.root.protocol("WM_DELETE_WINDOW", self.close_app)

        self.configure_styles()
        self.build_layout()
        self.show_page("Resumen")
        self.refresh_all()
        self.write_log("Aplicación iniciada. El repositorio está listo para administrar.", "info")
        self.root.after(100, self.process_events)

    def configure_styles(self):
        style = self.ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(
            "Dark.Treeview",
            background=self.CARD,
            fieldbackground=self.CARD,
            foreground=self.TEXT,
            rowheight=34,
            borderwidth=0,
            font=("Segoe UI", 10),
        )
        style.map(
            "Dark.Treeview",
            background=[("selected", self.ACCENT)],
            foreground=[("selected", "#FFFFFF")],
        )
        style.configure(
            "Dark.Treeview.Heading",
            background=self.CARD_ALT,
            foreground=self.MUTED,
            relief="flat",
            borderwidth=0,
            font=("Segoe UI Semibold", 9),
            padding=(8, 10),
        )
        style.map("Dark.Treeview.Heading", background=[("active", self.BORDER)])
        style.configure(
            "Horizontal.TProgressbar",
            background=self.ACCENT,
            troughcolor=self.BORDER,
            bordercolor=self.BORDER,
            lightcolor=self.ACCENT,
            darkcolor=self.ACCENT,
            thickness=8,
        )
        style.configure(
            "Vertical.TScrollbar",
            background=self.CARD_ALT,
            troughcolor=self.CARD,
            bordercolor=self.CARD,
            arrowcolor=self.MUTED,
        )

    def build_layout(self):
        self.root.grid_rowconfigure(0, weight=1)
        self.root.grid_columnconfigure(1, weight=1)

        sidebar = self.tk.Frame(self.root, bg=self.SIDEBAR, width=225)
        sidebar.grid(row=0, column=0, sticky="nsew")
        sidebar.grid_propagate(False)

        brand = self.tk.Frame(sidebar, bg=self.SIDEBAR)
        brand.pack(fill="x", padx=22, pady=(26, 30))
        self.tk.Label(
            brand, text="◆", bg=self.SIDEBAR, fg=self.ACCENT,
            font=("Segoe UI", 22, "bold"),
        ).pack(side="left")
        brand_text = self.tk.Frame(brand, bg=self.SIDEBAR)
        brand_text.pack(side="left", padx=(10, 0))
        self.tk.Label(
            brand_text, text="RepoMine", bg=self.SIDEBAR, fg=self.TEXT,
            font=("Segoe UI", 16, "bold"), anchor="w",
        ).pack(anchor="w")
        self.tk.Label(
            brand_text, text="MOD MANAGER", bg=self.SIDEBAR, fg=self.MUTED,
            font=("Segoe UI", 7, "bold"), anchor="w",
        ).pack(anchor="w")

        for name, icon in (
            ("Resumen", "▦"),
            ("Mods", "⬡"),
            ("Actividad", "≡"),
            ("Configuración", "⚙"),
        ):
            button = self.tk.Button(
                sidebar,
                text=f"  {icon}    {name}",
                command=lambda page=name: self.show_page(page),
                bg=self.SIDEBAR,
                fg=self.MUTED,
                activebackground=self.CARD_ALT,
                activeforeground=self.TEXT,
                relief="flat",
                bd=0,
                font=("Segoe UI", 10, "bold"),
                anchor="w",
                padx=20,
                pady=13,
                cursor="hand2",
            )
            button.pack(fill="x", padx=10, pady=2)
            self.nav_buttons[name] = button

        sidebar_bottom = self.tk.Frame(sidebar, bg=self.SIDEBAR)
        sidebar_bottom.pack(side="bottom", fill="x", padx=18, pady=20)
        self.repo_state_dot = self.tk.Label(
            sidebar_bottom, text="●", bg=self.SIDEBAR, fg=self.GREEN,
            font=("Segoe UI", 11),
        )
        self.repo_state_dot.pack(side="left")
        self.repo_state_label = self.tk.Label(
            sidebar_bottom, text="Comprobando Git...", bg=self.SIDEBAR,
            fg=self.MUTED, font=("Segoe UI", 9), anchor="w",
        )
        self.repo_state_label.pack(side="left", padx=(7, 0))

        main = self.tk.Frame(self.root, bg=self.BG)
        main.grid(row=0, column=1, sticky="nsew")
        main.grid_rowconfigure(1, weight=1)
        main.grid_columnconfigure(0, weight=1)

        topbar = self.tk.Frame(main, bg=self.BG, height=82)
        topbar.grid(row=0, column=0, sticky="ew", padx=30)
        topbar.grid_propagate(False)
        self.page_title = self.tk.Label(
            topbar, text="Resumen", bg=self.BG, fg=self.TEXT,
            font=("Segoe UI", 22, "bold"),
        )
        self.page_title.pack(side="left", pady=24)

        self.top_status = self.tk.Label(
            topbar,
            text=f"  origin/{self.config['branch']}  ",
            bg=self.CARD_ALT,
            fg=self.GREEN,
            font=("Segoe UI", 9, "bold"),
            padx=10,
            pady=7,
        )
        self.top_status.pack(side="right", pady=23)

        self.page_container = self.tk.Frame(main, bg=self.BG)
        self.page_container.grid(row=1, column=0, sticky="nsew", padx=30, pady=(0, 26))
        self.page_container.grid_rowconfigure(0, weight=1)
        self.page_container.grid_columnconfigure(0, weight=1)

        self.build_dashboard()
        self.build_mods_page()
        self.build_activity_page()
        self.build_settings_page()

    def new_page(self, name):
        page = self.tk.Frame(self.page_container, bg=self.BG)
        page.grid(row=0, column=0, sticky="nsew")
        self.pages[name] = page
        return page

    def card(self, parent, **grid_options):
        frame = self.tk.Frame(
            parent, bg=self.CARD, highlightbackground=self.BORDER,
            highlightthickness=1,
        )
        if grid_options:
            frame.grid(**grid_options)
        return frame

    def build_dashboard(self):
        page = self.new_page("Resumen")
        for column in range(4):
            page.grid_columnconfigure(column, weight=1, uniform="cards")
        page.grid_rowconfigure(2, weight=1)

        self.stat_labels = {}
        cards = (
            ("mods", "MODS LOCALES", self.BLUE),
            ("manifest", "MANIFIESTO", self.GREEN),
            ("pending", "CAMBIOS PENDIENTES", self.AMBER),
            ("size", "TAMAÑO TOTAL", self.ACCENT),
        )
        for column, (key, title, color) in enumerate(cards):
            card = self.card(page, row=0, column=column, sticky="ew", padx=(0 if column == 0 else 6, 0 if column == 3 else 6))
            self.tk.Frame(card, bg=color, width=4).pack(side="left", fill="y")
            body = self.tk.Frame(card, bg=self.CARD)
            body.pack(fill="both", expand=True, padx=16, pady=14)
            self.tk.Label(
                body, text=title, bg=self.CARD, fg=self.MUTED,
                font=("Segoe UI", 8, "bold"), anchor="w",
            ).pack(anchor="w")
            value = self.tk.Label(
                body, text="—", bg=self.CARD, fg=self.TEXT,
                font=("Segoe UI", 19, "bold"), anchor="w",
            )
            value.pack(anchor="w", pady=(4, 0))
            self.stat_labels[key] = value

        actions_card = self.card(page, row=1, column=0, columnspan=4, sticky="ew", pady=(14, 14))
        actions_header = self.tk.Frame(actions_card, bg=self.CARD)
        actions_header.pack(fill="x", padx=20, pady=(17, 10))
        self.tk.Label(
            actions_header, text="Acciones rápidas", bg=self.CARD, fg=self.TEXT,
            font=("Segoe UI", 12, "bold"),
        ).pack(side="left")
        self.tk.Label(
            actions_header, text="Se solicitará confirmación antes de publicar", bg=self.CARD,
            fg=self.MUTED, font=("Segoe UI", 9),
        ).pack(side="right")

        action_row = self.tk.Frame(actions_card, bg=self.CARD)
        action_row.pack(fill="x", padx=16, pady=(0, 16))
        for column in range(4):
            action_row.grid_columnconfigure(column, weight=1, uniform="actions")

        actions = (
            ("Generar manifiesto", "Recalcula hashes y JSON", self.BLUE, self.action_generate),
            ("Subir manifiesto", "Publica solo manifest.json", self.ACCENT, self.action_upload_manifest),
            ("Subir archivos", "Publica mods y paquetes de recursos", self.AMBER, self.action_upload_mods),
            ("Publicar todo", "Genera y publica todo el contenido", self.GREEN, self.action_publish_all),
        )
        for column, (title, subtitle, color, command) in enumerate(actions):
            button = self.tk.Button(
                action_row,
                text=f"{title}\n{subtitle}",
                command=command,
                bg=self.CARD_ALT,
                fg=self.TEXT,
                activebackground=color,
                activeforeground="#FFFFFF",
                disabledforeground="#64748B",
                relief="flat",
                bd=0,
                font=("Segoe UI", 9, "bold"),
                justify="left",
                anchor="w",
                padx=15,
                pady=12,
                cursor="hand2",
                highlightbackground=color,
                highlightthickness=1,
            )
            button.grid(row=0, column=column, sticky="ew", padx=4)
            self.action_buttons.append(button)

        lower_left = self.card(page, row=2, column=0, columnspan=3, sticky="nsew", padx=(0, 7))
        lower_left.grid_rowconfigure(1, weight=1)
        lower_left.grid_columnconfigure(0, weight=1)
        changes_header = self.tk.Frame(lower_left, bg=self.CARD)
        changes_header.grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 8))
        self.tk.Label(
            changes_header, text="Cambios detectados", bg=self.CARD, fg=self.TEXT,
            font=("Segoe UI", 11, "bold"),
        ).pack(side="left")
        self.change_count_label = self.tk.Label(
            changes_header, text="0 archivos", bg=self.CARD_ALT, fg=self.MUTED,
            font=("Segoe UI", 8, "bold"), padx=9, pady=4,
        )
        self.change_count_label.pack(side="right")

        self.changes_list = self.tk.Listbox(
            lower_left, bg=self.CARD, fg=self.MUTED, selectbackground=self.CARD_ALT,
            selectforeground=self.TEXT, borderwidth=0, highlightthickness=0,
            font=("Consolas", 9), activestyle="none",
        )
        self.changes_list.grid(row=1, column=0, sticky="nsew", padx=17, pady=(0, 10))

        progress_frame = self.tk.Frame(lower_left, bg=self.CARD)
        progress_frame.grid(row=2, column=0, sticky="ew", padx=18, pady=(4, 16))
        self.progress_label = self.tk.Label(
            progress_frame, text="Listo para trabajar", bg=self.CARD, fg=self.MUTED,
            font=("Segoe UI", 9), anchor="w",
        )
        self.progress_label.pack(fill="x", pady=(0, 7))
        self.progress = self.ttk.Progressbar(progress_frame, mode="determinate", maximum=100)
        self.progress.pack(fill="x")

        lower_right = self.card(page, row=2, column=3, sticky="nsew", padx=(7, 0))
        self.tk.Label(
            lower_right, text="Publicación", bg=self.CARD, fg=self.TEXT,
            font=("Segoe UI", 11, "bold"),
        ).pack(anchor="w", padx=18, pady=(16, 5))
        self.tk.Label(
            lower_right, text="Mensaje del próximo commit", bg=self.CARD, fg=self.MUTED,
            font=("Segoe UI", 8),
        ).pack(anchor="w", padx=18, pady=(8, 5))
        self.commit_message = self.tk.StringVar(value="Actualiza manifest y mods")
        commit_entry = self.tk.Entry(
            lower_right, textvariable=self.commit_message, bg=self.CARD_ALT,
            fg=self.TEXT, insertbackground=self.TEXT, relief="flat", bd=0,
            font=("Segoe UI", 9),
        )
        commit_entry.pack(fill="x", padx=18, ipady=8)

        self.tk.Label(
            lower_right,
            text="El nuevo commit se limita a los archivos indicados. Si ya existen commits locales salientes, aparecerán en la confirmación.",
            bg=self.CARD, fg=self.MUTED, font=("Segoe UI", 8),
            justify="left", wraplength=190,
        ).pack(anchor="w", padx=18, pady=(14, 10))
        self.make_button(
            lower_right, "Abrir repositorio en GitHub", self.open_github,
            self.CARD_ALT, self.TEXT,
        ).pack(fill="x", padx=18, pady=(8, 18))

    def build_mods_page(self):
        page = self.new_page("Mods")
        page.grid_rowconfigure(1, weight=1)
        page.grid_columnconfigure(0, weight=1)

        toolbar = self.tk.Frame(page, bg=self.BG)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        self.search_var = self.tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self.populate_mods())
        search = self.tk.Entry(
            toolbar, textvariable=self.search_var, bg=self.CARD, fg=self.TEXT,
            insertbackground=self.TEXT, relief="flat", bd=0,
            font=("Segoe UI", 10),
        )
        search.pack(side="left", fill="x", expand=True, ipady=10, padx=(0, 10))
        search.insert(0, "")
        self.make_button(toolbar, "Actualizar", self.refresh_all, self.CARD_ALT, self.TEXT).pack(side="right", padx=(8, 0))
        self.make_button(toolbar, "Abrir carpeta mods", self.open_mods_folder, self.ACCENT, "#FFFFFF").pack(side="right")

        table_card = self.card(page, row=1, column=0, sticky="nsew")
        table_card.grid_rowconfigure(0, weight=1)
        table_card.grid_columnconfigure(0, weight=1)

        columns = ("status", "name", "size", "modified")
        self.mods_tree = self.ttk.Treeview(
            table_card, columns=columns, show="headings", style="Dark.Treeview",
        )
        self.mods_tree.heading("status", text="ESTADO")
        self.mods_tree.heading("name", text="ARCHIVO")
        self.mods_tree.heading("size", text="TAMAÑO")
        self.mods_tree.heading("modified", text="MODIFICADO")
        self.mods_tree.column("status", width=125, minwidth=110, anchor="w")
        self.mods_tree.column("name", width=520, minwidth=280, anchor="w")
        self.mods_tree.column("size", width=100, minwidth=85, anchor="e")
        self.mods_tree.column("modified", width=150, minwidth=120, anchor="center")
        self.mods_tree.tag_configure("ok", foreground=self.GREEN)
        self.mods_tree.tag_configure("new", foreground=self.BLUE)
        self.mods_tree.tag_configure("pending", foreground=self.AMBER)
        self.mods_tree.tag_configure("missing", foreground=self.RED)

        scrollbar = self.ttk.Scrollbar(table_card, command=self.mods_tree.yview)
        self.mods_tree.configure(yscrollcommand=scrollbar.set)
        self.mods_tree.grid(row=0, column=0, sticky="nsew", padx=(1, 0), pady=1)
        scrollbar.grid(row=0, column=1, sticky="ns", pady=1)

        self.mods_footer = self.tk.Label(
            page, text="", bg=self.BG, fg=self.MUTED,
            font=("Segoe UI", 9), anchor="w",
        )
        self.mods_footer.grid(row=2, column=0, sticky="ew", pady=(10, 0))

    def build_activity_page(self):
        page = self.new_page("Actividad")
        page.grid_rowconfigure(1, weight=1)
        page.grid_columnconfigure(0, weight=1)

        toolbar = self.tk.Frame(page, bg=self.BG)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        self.tk.Label(
            toolbar, text="Registro detallado de operaciones", bg=self.BG,
            fg=self.MUTED, font=("Segoe UI", 9),
        ).pack(side="left")
        self.make_button(toolbar, "Limpiar", self.clear_log, self.CARD_ALT, self.TEXT).pack(side="right")
        self.make_button(toolbar, "Copiar", self.copy_log, self.CARD_ALT, self.TEXT).pack(side="right", padx=(0, 8))

        log_card = self.card(page, row=1, column=0, sticky="nsew")
        log_card.grid_rowconfigure(0, weight=1)
        log_card.grid_columnconfigure(0, weight=1)
        self.log_area = self.tk.Text(
            log_card, state="disabled", wrap="word", bg=self.CARD,
            fg=self.MUTED, insertbackground=self.TEXT, relief="flat", bd=0,
            padx=18, pady=16, font=("Cascadia Mono", 9),
        )
        self.log_area.tag_configure("time", foreground="#667892")
        self.log_area.tag_configure("info", foreground=self.MUTED)
        self.log_area.tag_configure("success", foreground=self.GREEN)
        self.log_area.tag_configure("error", foreground=self.RED)
        self.log_area.tag_configure("title", foreground=self.TEXT, font=("Cascadia Mono", 9, "bold"))
        log_scroll = self.ttk.Scrollbar(log_card, command=self.log_area.yview)
        self.log_area.configure(yscrollcommand=log_scroll.set)
        self.log_area.grid(row=0, column=0, sticky="nsew")
        log_scroll.grid(row=0, column=1, sticky="ns")

    def build_settings_page(self):
        page = self.new_page("Configuración")
        page.grid_columnconfigure(0, weight=1)

        card = self.card(page, row=0, column=0, sticky="new")
        card.grid_columnconfigure(1, weight=1)
        self.tk.Label(
            card, text="Datos del modpack", bg=self.CARD, fg=self.TEXT,
            font=("Segoe UI", 13, "bold"),
        ).grid(row=0, column=0, columnspan=2, sticky="w", padx=22, pady=(20, 4))
        self.tk.Label(
            card,
            text="Estos valores se usarán la próxima vez que generes el manifiesto.",
            bg=self.CARD, fg=self.MUTED, font=("Segoe UI", 9),
        ).grid(row=1, column=0, columnspan=2, sticky="w", padx=22, pady=(0, 18))

        self.setting_vars = {}
        fields = (
            ("pack_name", "Nombre del modpack"),
            ("minecraft_version", "Versión de Minecraft"),
            ("loader", "Mod loader"),
            ("loader_version", "Versión del loader"),
            ("repo_owner", "Propietario de GitHub"),
            ("repo_name", "Repositorio"),
            ("branch", "Rama de publicación"),
            ("server_name", "Nombre del servidor"),
            ("server_address", "Dirección del servidor"),
        )
        for row, (key, label) in enumerate(fields, start=2):
            self.tk.Label(
                card, text=label, bg=self.CARD, fg=self.MUTED,
                font=("Segoe UI", 9), anchor="w",
            ).grid(row=row, column=0, sticky="w", padx=(22, 18), pady=7)
            variable = self.tk.StringVar(value=self.config[key])
            self.setting_vars[key] = variable
            entry = self.tk.Entry(
                card, textvariable=variable, bg=self.CARD_ALT, fg=self.TEXT,
                insertbackground=self.TEXT, relief="flat", bd=0,
                font=("Segoe UI", 10),
            )
            entry.grid(row=row, column=1, sticky="ew", padx=(0, 22), pady=6, ipady=8)

        buttons = self.tk.Frame(card, bg=self.CARD)
        buttons.grid(row=len(fields) + 2, column=0, columnspan=2, sticky="e", padx=22, pady=(14, 20))
        self.make_button(buttons, "Restaurar", self.restore_settings, self.CARD_ALT, self.TEXT).pack(side="left", padx=(0, 8))
        self.make_button(buttons, "Aplicar configuración", self.apply_settings, self.ACCENT, "#FFFFFF").pack(side="left")

        note = self.card(page, row=1, column=0, sticky="ew", pady=(14, 0))
        self.tk.Label(
            note, text="ⓘ", bg=self.CARD, fg=self.BLUE,
            font=("Segoe UI", 16, "bold"),
        ).pack(side="left", padx=(18, 12), pady=16)
        self.tk.Label(
            note,
            text="La configuración se conserva localmente y los datos del modpack también se escriben en manifest.json. Nunca se almacenan contraseñas ni tokens de GitHub.",
            bg=self.CARD, fg=self.MUTED, font=("Segoe UI", 9),
            justify="left", wraplength=720,
        ).pack(side="left", fill="x", expand=True, padx=(0, 18), pady=16)

    def make_button(self, parent, text, command, bg, fg):
        return self.tk.Button(
            parent, text=text, command=command, bg=bg, fg=fg,
            activebackground=self.ACCENT_HOVER, activeforeground="#FFFFFF",
            disabledforeground="#64748B", relief="flat", bd=0,
            font=("Segoe UI", 9, "bold"), padx=15, pady=9,
            cursor="hand2",
        )

    def show_page(self, name):
        self.current_page = name
        self.pages[name].tkraise()
        self.page_title.configure(text=name)
        for page_name, button in self.nav_buttons.items():
            selected = page_name == name
            button.configure(
                bg=self.CARD_ALT if selected else self.SIDEBAR,
                fg=self.TEXT if selected else self.MUTED,
            )
        if name == "Mods":
            self.populate_mods()

    def write_log(self, message, level="info"):
        self.events.put(("log", str(message), level))

    def report_progress(self, current, total, label):
        self.events.put(("progress", current, total, label))

    def process_events(self):
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == "log":
                    _, message, level = event
                    self.append_log(message, level)
                elif event[0] == "progress":
                    _, current, total, label = event
                    percent = 0 if not total else max(0, min(100, current * 100 / total))
                    self.progress.configure(value=percent)
                    self.progress_label.configure(text=label, fg=self.MUTED)
                elif event[0] == "done":
                    _, success, label = event
                    self.finish_action(success, label)
        except queue.Empty:
            pass
        if self.root.winfo_exists():
            self.root.after(100, self.process_events)

    def append_log(self, message, level):
        timestamp = datetime.now().strftime("%H:%M:%S")
        if "Error" in message or "error" in message:
            level = "error"
        elif "correctamente" in message or message.startswith("✓"):
            level = "success"
        self.log_area.configure(state="normal")
        self.log_area.insert("end", f"[{timestamp}] ", "time")
        self.log_area.insert("end", message + "\n", level)
        self.log_area.see("end")
        self.log_area.configure(state="disabled")

    def run_action(self, label, action):
        if self.busy:
            return
        self.busy = True
        self.set_buttons_state("disabled")
        self.progress.configure(value=0)
        self.progress_label.configure(text=label, fg=self.MUTED)
        self.write_log(f"── {label} ──", "title")

        def worker():
            try:
                success = bool(action())
            except Exception as error:
                self.write_log(f"Error inesperado: {error}", "error")
                success = False
            self.events.put(("done", success, label))

        threading.Thread(target=worker, daemon=True).start()

    def finish_action(self, success, label):
        from tkinter import messagebox

        self.busy = False
        self.set_buttons_state("normal")
        self.progress.configure(value=100 if success else 0)
        self.progress_label.configure(
            text=f"{label}: completado" if success else f"{label}: ocurrió un error",
            fg=self.GREEN if success else self.RED,
        )
        self.refresh_all()
        if not success:
            messagebox.showerror(
                "No se completó la operación",
                "Revisa la pestaña Actividad para ver el detalle del error.",
                parent=self.root,
            )

    def set_buttons_state(self, state):
        for button in self.action_buttons:
            button.configure(state=state)

    def action_generate(self):
        self.run_action(
            "Generar manifiesto",
            lambda: generate_manifest(self.write_log, self.report_progress, self.config.copy()),
        )

    def confirm_publish(self, title, paths, description):
        from tkinter import messagebox

        message = self.commit_message.get().strip()
        if not message:
            messagebox.showwarning(
                "Mensaje requerido", "Escribe un mensaje para el commit.", parent=self.root
            )
            return None

        preflight = git_preflight(
            self.config["branch"],
            self.config["repo_owner"],
            self.config["repo_name"],
        )
        if not preflight["ready"]:
            messagebox.showerror(
                "Publicación bloqueada",
                preflight["error"],
                parent=self.root,
            )
            return None

        changes = git_changes(paths)
        preview = "\n".join(changes[:12])
        if len(changes) > 12:
            preview += f"\n... y {len(changes) - 12} archivo(s) más"
        if not preview:
            preview = "No se detectaron cambios actualmente."

        outgoing = ""
        if preflight["outgoing"]:
            outgoing = (
                "\n\nAdemás se enviarán estos commits locales pendientes:\n"
                + "\n".join(preflight["outgoing"][:8])
            )

        accepted = messagebox.askyesno(
            title,
            f"{description}\n\nRama: {preflight['current_branch']}\n"
            f"Remoto: {preflight['remote']}\n\nCambios detectados:\n{preview}"
            f"{outgoing}\n\nMensaje del commit:\n{message}\n\n¿Deseas continuar?",
            icon="question",
            parent=self.root,
        )
        return message if accepted else None

    def action_upload_manifest(self):
        message = self.confirm_publish(
            "Subir manifiesto", [OUTPUT_FILE], "Se publicará únicamente manifest.json."
        )
        if message is None:
            return
        self.run_action(
            "Subir manifiesto",
            lambda: upload_manifest(
                self.write_log, self.report_progress, message, self.config.copy()
            ),
        )

    def action_upload_mods(self):
        message = self.confirm_publish(
            "Subir archivos", [*mod_directories(), REPO_RESOURCEPACKS_FOLDER],
            "Se publicarán los cambios de mods/, modsCliente/, modsServer/, shared-config/ y Resourcepacks/."
        )
        if message is None:
            return
        self.run_action(
            "Subir mods",
            lambda: upload_mods(
                self.write_log, self.report_progress, message, self.config.copy()
            ),
        )

    def action_publish_all(self):
        from tkinter import messagebox

        message = self.commit_message.get().strip()
        if not message:
            messagebox.showwarning(
                "Mensaje requerido", "Escribe un mensaje para el commit.", parent=self.root
            )
            return
        preflight = git_preflight(
            self.config["branch"],
            self.config["repo_owner"],
            self.config["repo_name"],
        )
        if not preflight["ready"]:
            messagebox.showerror(
                "Publicación bloqueada", preflight["error"], parent=self.root
            )
            return
        outgoing = ""
        if preflight["outgoing"]:
            outgoing = (
                "\n\nTambién se enviarán estos commits locales pendientes:\n"
                + "\n".join(preflight["outgoing"][:8])
            )
        changes = git_changes([OUTPUT_FILE, *mod_directories()])
        preview = "\n".join(changes[:12]) or "No se detectaron cambios actualmente."
        if len(changes) > 12:
            preview += f"\n... y {len(changes) - 12} archivo(s) más"
        accepted = messagebox.askyesno(
            "Generar y publicar todo",
            "Se recalculará el manifiesto completo y después se publicarán manifest.json, mods/, modsCliente/, modsServer/, shared-config/ y Resourcepacks/.\n\n"
            f"Rama: {preflight['current_branch']}\nRemoto: {preflight['remote']}"
            f"\n\nCambios actuales:\n{preview}{outgoing}"
            f"\n\nMensaje del commit:\n{message}\n\n¿Deseas continuar?",
            icon="question",
            parent=self.root,
        )
        if not accepted:
            return
        self.run_action(
            "Generar y publicar todo",
            lambda: generate_and_upload(
                self.write_log, self.report_progress, message, self.config.copy()
            ),
        )

    def refresh_all(self):
        jars = local_mod_files()
        manifest = load_manifest()
        manifest_names = manifest_mod_names(manifest)
        local_names = {jar.name for jar in jars}
        scoped_changes = git_changes([OUTPUT_FILE, *mod_directories(), REPO_RESOURCEPACKS_FOLDER])
        mods_changes = git_changes([*mod_directories(), REPO_RESOURCEPACKS_FOLDER])
        total_size = sum(jar.stat().st_size for jar in jars)

        self.stat_labels["mods"].configure(text=str(len(jars)))
        if not manifest:
            manifest_state = "No existe"
            manifest_color = self.RED
        elif local_names != manifest_names or mods_changes:
            manifest_state = "Revisar"
            manifest_color = self.AMBER
        else:
            manifest_state = "Actualizado"
            manifest_color = self.GREEN
        self.stat_labels["manifest"].configure(text=manifest_state, fg=manifest_color)
        self.stat_labels["pending"].configure(text=str(len(scoped_changes)))
        self.stat_labels["size"].configure(text=format_size(total_size))

        self.changes_list.delete(0, "end")
        if scoped_changes:
            for change in scoped_changes[:30]:
                self.changes_list.insert("end", change)
        else:
            self.changes_list.insert("end", "✓ No hay cambios pendientes en manifest.json, mods/, modsCliente/, modsServer/ ni Resourcepacks/")
        self.change_count_label.configure(text=f"{len(scoped_changes)} archivo(s)")

        state = branch_status()
        state_color = self.GREEN if state == "Sincronizado" else self.AMBER
        if state == "Git no disponible":
            state_color = self.RED
        self.repo_state_dot.configure(fg=state_color)
        self.repo_state_label.configure(text=state)
        self.top_status.configure(
            text=f"  origin/{self.config['branch']}  ", fg=state_color
        )
        self.populate_mods(jars, manifest_names, mods_changes)

    def populate_mods(self, jars=None, manifest_names=None, mods_changes=None):
        if not hasattr(self, "mods_tree"):
            return
        if jars is None:
            jars = local_mod_files()
        if manifest_names is None:
            manifest = load_manifest()
            manifest_names = manifest_mod_names(manifest)
        if mods_changes is None:
            mods_changes = git_changes(list(mod_directories()))

        changed_names = set()
        for line in mods_changes:
            raw_path = line[3:].strip().strip('"').replace("\\", "/")
            changed_names.add(raw_path.rsplit("/", 1)[-1])

        local_by_name = {jar.name: jar for jar in jars}
        all_names = sorted(set(local_by_name) | set(manifest_names), key=str.lower)
        query = self.search_var.get().strip().lower() if hasattr(self, "search_var") else ""

        self.mods_tree.delete(*self.mods_tree.get_children())
        visible = 0
        counts = {"new": 0, "pending": 0, "missing": 0, "ok": 0}
        rows = []
        for name in all_names:
            if query and query not in name.lower():
                continue
            jar = local_by_name.get(name)
            if jar is None:
                status, tag = "Falta archivo", "missing"
                size, modified = "—", "—"
            elif name not in manifest_names:
                status, tag = "Nuevo", "new"
                size = format_size(jar.stat().st_size)
                modified = datetime.fromtimestamp(jar.stat().st_mtime).strftime("%d/%m/%Y %H:%M")
            elif name in changed_names:
                status, tag = "Pendiente", "pending"
                size = format_size(jar.stat().st_size)
                modified = datetime.fromtimestamp(jar.stat().st_mtime).strftime("%d/%m/%Y %H:%M")
            else:
                status, tag = "Sincronizado", "ok"
                size = format_size(jar.stat().st_size)
                modified = datetime.fromtimestamp(jar.stat().st_mtime).strftime("%d/%m/%Y %H:%M")

            rows.append((tag, status, name, size, modified))

        # Los mods nuevos necesitan atención primero; dentro de cada grupo se ordenan por nombre.
        order = {"new": 0, "pending": 1, "ok": 2, "missing": 3}
        for tag, status, name, size, modified in sorted(
            rows, key=lambda row: (order[row[0]], row[2].lower())
        ):
            counts[tag] += 1
            visible += 1
            self.mods_tree.insert("", "end", values=(status, name, size, modified), tags=(tag,))

        self.mods_footer.configure(
            text=(
                f"Mostrando {visible} de {len(all_names)} · "
                f"{counts['ok']} sincronizados · {counts['new']} nuevos · "
                f"{counts['pending']} pendientes · {counts['missing']} faltantes"
            )
        )

    def apply_settings(self):
        from tkinter import messagebox

        values = {key: variable.get().strip() for key, variable in self.setting_vars.items()}
        missing = [key for key, value in values.items() if not value]
        if missing:
            messagebox.showwarning(
                "Datos incompletos", "Todos los campos son obligatorios.", parent=self.root
            )
            return
        try:
            branch_check = run_git(
                "check-ref-format", "--branch", values["branch"], check=False
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            messagebox.showerror(
                "Git no disponible",
                "No se pudo usar Git para validar el nombre de la rama.",
                parent=self.root,
            )
            return
        if branch_check.returncode != 0:
            messagebox.showwarning(
                "Rama no válida",
                "Escribe un nombre de rama válido para Git.",
                parent=self.root,
            )
            return
        if any(character in values["repo_owner"] for character in " /\\") or any(
            character in values["repo_name"] for character in " /\\"
        ):
            messagebox.showwarning(
                "Repositorio no válido",
                "El propietario y el repositorio no deben contener espacios ni diagonales.",
                parent=self.root,
            )
            return
        self.config.update(values)
        try:
            save_manager_settings(self.config)
            atomic_json(PACK_CONFIG_PATH, load_pack_config(self.config))
        except OSError as error:
            messagebox.showerror(
                "No se pudo guardar",
                f"La configuración se aplicó a esta sesión, pero no pudo guardarse:\n{error}",
                parent=self.root,
            )
            return
        self.top_status.configure(text=f"  origin/{self.config['branch']}  ")
        self.write_log("Configuración aplicada para las próximas operaciones.", "success")
        messagebox.showinfo(
            "Configuración aplicada",
            "Los valores quedaron guardados. Los datos del modpack se aplicarán a manifest.json al generarlo.",
            parent=self.root,
        )

    def restore_settings(self):
        manifest = load_manifest()
        defaults = {
            "repo_owner": REPO_OWNER,
            "repo_name": REPO_NAME,
            "branch": BRANCH,
            "pack_name": manifest.get("pack_name", PACK_NAME),
            "minecraft_version": manifest.get("minecraft_version", MINECRAFT_VERSION),
            "loader": manifest.get("loader", LOADER),
            "loader_version": manifest.get("loader_version", LOADER_VERSION),
        }
        for key, variable in self.setting_vars.items():
            variable.set(defaults.get(key, {"server_name": PACK_NAME, "server_address": "tecserver.playit.plus"}.get(key, "")))

    def open_mods_folder(self):
        MODS_PATH.mkdir(exist_ok=True)
        os.startfile(str(MODS_PATH))

    def open_github(self):
        webbrowser.open(f"https://github.com/{self.config['repo_owner']}/{self.config['repo_name']}")

    def clear_log(self):
        self.log_area.configure(state="normal")
        self.log_area.delete("1.0", "end")
        self.log_area.configure(state="disabled")

    def copy_log(self):
        content = self.log_area.get("1.0", "end-1c")
        self.root.clipboard_clear()
        self.root.clipboard_append(content)
        self.progress_label.configure(text="Registro copiado al portapapeles", fg=self.GREEN)

    def close_app(self):
        from tkinter import messagebox

        if self.busy:
            messagebox.showwarning(
                "Operación en curso",
                "Espera a que termine la operación antes de cerrar la aplicación.",
                parent=self.root,
            )
            return
        self.root.destroy()


def start_gui() -> None:
    try:
        import tkinter as tk
    except ImportError:
        print("Tkinter no está instalado. Ejecuta el script con --cli o instala Tkinter.")
        return

    root = tk.Tk()
    ManifestManagerApp(root)
    root.mainloop()


def print_cli_help() -> None:
    print(
        "Uso:\n"
        "  python generar_manifest.py                  Abre la interfaz gráfica\n"
        "  python generar_manifest.py --generate       Solo genera manifest.json\n"
        "  python generar_manifest.py --upload-manifest\n"
        "  python generar_manifest.py --upload-mods\n"
        "  python generar_manifest.py --cli            Genera y publica todo"
    )


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    if "--generate" in sys.argv:
        raise SystemExit(0 if generate_manifest() else 1)
    elif "--upload-manifest" in sys.argv:
        raise SystemExit(0 if upload_manifest() else 1)
    elif "--upload-mods" in sys.argv:
        raise SystemExit(0 if upload_mods() else 1)
    elif "--cli" in sys.argv or "--publish-all" in sys.argv:
        raise SystemExit(0 if generate_and_upload() else 1)
    elif "--help" in sys.argv or "-h" in sys.argv:
        print_cli_help()
    else:
        start_gui()
