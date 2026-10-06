"""Validación y sincronización del pack, independiente de la interfaz gráfica."""
import hashlib
import json
import os
import re
import shutil
import ssl
import tempfile
import time
import tomllib
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import certifi

SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
SECTIONS = {"mods": "mods", "resourcepacks": "resourcepacks", "configs": "config"}
MAX_FILE_SIZE = 1024 * 1024 * 1024


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def safe_relative(value, nested=False):
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("Nombre de archivo no válido.")
    path = PurePosixPath(value)
    parts = value.split("/")
    if path.is_absolute() or (not nested and len(parts) != 1):
        raise ValueError(f"Ruta no permitida: {value}")
    for part in parts:
        if (part in ("", ".", "..") or part.endswith((" ", "."))
                or re.search(r'[<>:"|?*\x00-\x1f]', part)
                or re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", part)):
            raise ValueError(f"Ruta no permitida: {value}")
    return value


def confined_path(root, relative):
    safe_relative(relative, nested=True)
    root = Path(root).resolve()
    path = root.joinpath(*relative.split("/"))
    # Reject symlinks/junctions that leave the chosen installation.
    if not path.resolve().is_relative_to(root):
        raise ValueError(f"La ruta sale de la instalación: {relative}")
    if path.is_symlink():
        raise ValueError(f"No se puede administrar un enlace: {relative}")
    return path


def validate_url(url):
    if not isinstance(url, str):
        raise ValueError("URL no válida.")
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Las descargas requieren una URL HTTPS sin credenciales.")
    return url


def validate_manifest(data):
    if (not isinstance(data, dict) or type(data.get("schema_version", 1)) is not int
            or data.get("schema_version", 1) not in (1, 2)):
        raise ValueError("Formato de manifiesto no compatible.")
    for key in ("pack_name", "minecraft_version", "loader", "loader_version"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise ValueError(f"Falta el campo {key} en el manifiesto.")
    if not isinstance(data.get("mods"), list) or not data["mods"]:
        raise ValueError("El manifiesto no contiene una lista de mods válida.")
    for section in SECTIONS:
        entries = data.get(section, [])
        if not isinstance(entries, list) or len(entries) > 5000:
            raise ValueError(f"Lista no válida: {section}")
        seen = set()
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError(f"Entrada no válida en {section}")
            name = safe_relative(entry.get("file"), nested=section == "configs")
            if name.casefold() in seen:
                raise ValueError(f"Archivo duplicado: {name}")
            seen.add(name.casefold())
            if section in ("mods", "resourcepacks") and not name.lower().endswith(
                    ".jar" if section == "mods" else ".zip"):
                raise ValueError(f"Extensión incorrecta: {name}")
            if not re.fullmatch(r"[a-fA-F0-9]{64}", str(entry.get("sha256", ""))):
                raise ValueError(f"SHA-256 ausente o inválido: {name}")
            validate_url(entry.get("url"))
            size = entry.get("size")
            if size is not None and (type(size) is not int or not 0 <= size <= MAX_FILE_SIZE):
                raise ValueError(f"Tamaño no válido: {name}")
            if section == "configs" and entry.get("policy", "default") not in ("default", "managed"):
                raise ValueError(f"Política de configuración no válida: {name}")
    server = data.get("server", {})
    if not isinstance(server, dict) or not isinstance(server.get("address", ""), str):
        raise ValueError("Configuración de servidor no válida.")
    if re.search(r"[\s\x00-\x1f]", server.get("address", "")):
        raise ValueError("La dirección del servidor contiene espacios.")
    return data


def mod_ids_from_jar(path):
    """Lee solo mods declarados, nunca los modId de dependencies."""
    with zipfile.ZipFile(path) as jar:
        for name in ("META-INF/neoforge.mods.toml", "META-INF/mods.toml"):
            if name in jar.namelist():
                metadata = tomllib.loads(jar.read(name).decode("utf-8-sig"))
                return list(dict.fromkeys(str(mod["modId"]).lower()
                    for mod in metadata.get("mods", []) if isinstance(mod, dict) and mod.get("modId")))
        if "fabric.mod.json" in jar.namelist():
            return [json.loads(jar.read("fabric.mod.json"))["id"].lower()]
    return []


def download_json(url):
    validate_url(url)
    request = urllib.request.Request(url, headers={"User-Agent": "RepoMine/1.1", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(request, timeout=30, context=SSL_CONTEXT) as response:
        content = response.read(8 * 1024 * 1024 + 1)
    if len(content) > 8 * 1024 * 1024:
        raise ValueError("El manifiesto es demasiado grande.")
    return json.loads(content)


def download_verified(url, destination, expected_hash, progress=None, expected_size=None):
    """Valida antes de reemplazar. Reintenta sin destruir el archivo anterior."""
    validate_url(url)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(3):
        fd, temporary = tempfile.mkstemp(dir=destination.parent, suffix=".download")
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "RepoMine/1.1"})
            digest, downloaded = hashlib.sha256(), 0
            with os.fdopen(fd, "wb") as output:
                with urllib.request.urlopen(request, timeout=60, context=SSL_CONTEXT) as response:
                    total = int(response.headers.get("Content-Length", 0))
                    for chunk in iter(lambda: response.read(512 * 1024), b""):
                        downloaded += len(chunk)
                        if downloaded > MAX_FILE_SIZE:
                            raise ValueError("La descarga supera el tamaño permitido.")
                        digest.update(chunk)
                        output.write(chunk)
                        if progress and total:
                            progress(downloaded, total)
            if expected_size is not None and downloaded != expected_size:
                raise ValueError("La descarga tiene un tamaño diferente al esperado.")
            if digest.hexdigest() != expected_hash.lower():
                raise ValueError("SHA-256 diferente: descarga dañada o pack actualizado durante la descarga.")
            os.replace(temporary, destination)
            return
        except (OSError, ValueError):
            if attempt == 2:
                raise
            time.sleep(attempt + 1)
        finally:
            Path(temporary).unlink(missing_ok=True)


def entries(manifest):
    for section, folder in SECTIONS.items():
        for entry in manifest.get(section, []):
            yield f"{folder}/{entry['file']}", entry


def inspect_pack(root, manifest):
    validate_manifest(manifest)
    missing, changed, preserved = [], [], []
    for relative, entry in entries(manifest):
        path = confined_path(root, relative)
        if not path.is_file():
            missing.append(relative)
        elif relative.startswith("config/") and entry.get("policy", "default") == "default":
            preserved.append(relative)
        elif sha256_file(path) != entry["sha256"].lower():
            changed.append(relative)
    known = {r.casefold() for r, _ in entries(manifest)}
    extras = ["mods/" + p.name for p in (Path(root) / "mods").glob("*.jar")
              if ("mods/" + p.name).casefold() not in known]
    return {"missing": missing, "changed": changed, "preserved": preserved, "extras": extras}


class PackLock:
    """Bloqueo del sistema operativo; se libera incluso si se cierra el proceso."""
    def __init__(self, root):
        self.path = confined_path(root, ".repomine/install.lock")

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("a+b")
        self.stream.write(b"0")
        self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            raise RuntimeError("Otra ventana de RepoMine está actualizando esta carpeta.") from None
        return self

    def __exit__(self, *_):
        self.stream.close()


def synchronize(root, manifest, log=print, progress=None, *, lock=True):
    validate_manifest(manifest)
    root = Path(root).resolve()
    with PackLock(root) if lock else nullcontext():
        state_path = confined_path(root, ".repomine/state.json")
        if state_path.exists():
            old = json.loads(state_path.read_text(encoding="utf-8"))
            if (not isinstance(old, dict) or type(old.get("schema_version")) is not int
                    or old.get("schema_version") != 1 or not isinstance(old.get("files"), dict)):
                raise ValueError("El registro de instalación está dañado; se conserva para diagnóstico.")
            old = old["files"]
            for relative, digest in old.items():
                safe_relative(relative, nested=True)
                if relative.split("/")[0] not in SECTIONS.values() or not re.fullmatch(r"[a-f0-9]{64}", str(digest)):
                    raise ValueError("Registro de archivos administrados no válido.")
        else:
            old = {}
        report = inspect_pack(root, manifest)
        desired = dict(entries(manifest))
        pending = report["missing"] + report["changed"]
        work_dir = confined_path(root, ".repomine/staging")
        work_dir.mkdir(parents=True, exist_ok=True)
        backup = confined_path(root, ".repomine/backups/" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
        with tempfile.TemporaryDirectory(dir=work_dir) as temporary:
            stage = Path(temporary)
            def fetch(relative):
                item = desired[relative]
                download_verified(item["url"], stage / relative, item["sha256"], expected_size=item.get("size"))
                return relative
            errors = []
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = {pool.submit(fetch, r): r for r in pending}
                for completed, future in enumerate(as_completed(futures), 1):
                    try:
                        log(f"Descargado y comprobado: {future.result()}")
                    except Exception as error:
                        errors.append(f"{futures[future]}: {error}")
                    if progress:
                        progress(completed, len(pending))
            if errors:
                raise RuntimeError("No se aplicaron cambios.\n" + "\n".join(errors))
            applied, retired = [], []
            try:
                for relative in pending:
                    destination = confined_path(root, relative)
                    if destination.exists():
                        copy = backup / relative
                        copy.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(destination, copy)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(stage / relative, destination)
                    applied.append(relative)
                for relative, digest in old.items():
                    if relative.casefold() in {r.casefold() for r in desired}:
                        continue
                    path = confined_path(root, relative)
                    if path.is_file() and sha256_file(path) == digest:
                        copy = backup / relative
                        copy.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(path, copy)
                        path.unlink()
                        retired.append(relative)
                        log(f"Retirado con respaldo: {relative}")
                    elif path.exists():
                        log(f"Se conserva un archivo modificado por el jugador: {relative}")
                result = inspect_pack(root, manifest)
                if result["missing"] or result["changed"]:
                    raise RuntimeError("La verificación final falló.")
                owned = {}
                for relative, item in desired.items():
                    if relative in report["preserved"] and relative not in old:
                        continue
                    # Defaults customized by the player retain their original ownership hash.
                    owned[relative] = old[relative] if relative in report["preserved"] else item["sha256"].lower()
                atomic_json(state_path, {"schema_version": 1, "pack_name": manifest["pack_name"], "files": owned})
            except Exception:
                for relative in reversed(applied + retired):
                    destination = confined_path(root, relative)
                    copy = backup / relative
                    if copy.exists():
                        shutil.copy2(copy, destination)
                    elif relative in applied:
                        destination.unlink(missing_ok=True)
                raise
        if result["extras"]:
            log("Archivos adicionales conservados (pueden causar conflictos): " + ", ".join(result["extras"]))
        log(f"Sincronización completa: {len(pending)} instalados/reparados, {len(retired)} retirados.")
        return result
