"""Minecraft preparation and explicit Microsoft authentication (no launcher tokens)."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from contextlib import contextmanager, nullcontext
from threading import RLock

_NETWORK_LOCK = RLock()


@contextmanager
def _bounded_network():
    """Pinned library 8.0 does not supply Requests timeouts itself."""
    import requests
    import certifi
    with _NETWORK_LOCK:
        original = requests.sessions.Session.request

        def request(session, method, url, **kwargs):
            kwargs.setdefault("timeout", (15, 90))
            kwargs.setdefault("verify", certifi.where())
            return original(session, method, url, **kwargs)

        requests.sessions.Session.request = request
        try:
            yield
        finally:
            requests.sessions.Session.request = original


class DirectLaunchUnavailable(RuntimeError):
    pass


def _backup(path: Path):
    if path.exists():
        destination = path.with_name(f"{path.name}.repomine-{time.time_ns()}.bak")
        shutil.copy2(path, destination)
        return destination
    return None


def _write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        _backup(path)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _versions(manifest):
    minecraft = manifest.get("minecraft_version", "1.21.1")
    loader = manifest.get("loader_version", "21.1.233")
    if str(manifest.get("loader", "NeoForge")).lower() != "neoforge":
        raise ValueError("La preparación automática actualmente requiere NeoForge.")
    if not all(isinstance(v, str) and re.fullmatch(r"[0-9]+(?:\.[0-9]+)+(?:-beta)?", v) for v in (minecraft, loader)):
        raise ValueError("Versión de Minecraft o NeoForge inválida.")
    return minecraft, loader


def configure_profile(root, manifest, version_id, java_path):
    """Update only our stable profile; fail rather than replace malformed files."""
    root = Path(root).resolve()
    files = [root / "launcher_profiles.json"]
    store = root / "launcher_profiles_microsoft_store.json"
    if store.exists():
        files.append(store)
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    for path in files:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"profiles": {}, "version": 3}
        if not isinstance(data, dict) or not isinstance(data.get("profiles", {}), dict):
            raise ValueError(f"El archivo {path.name} no contiene perfiles válidos; se conservó intacto.")
        profiles = data.setdefault("profiles", {})
        old = profiles.get("repomine", {})
        if not isinstance(old, dict):
            raise ValueError("El perfil RepoMine existente no es válido.")
        profile = dict(old)
        profile.update(name=manifest.get("pack_name", "Servidor Gael") + " · RepoMine",
                       type="custom", lastVersionId=version_id, gameDir=str(root),
                       javaDir=str(java_path), icon="Grass")
        profile.setdefault("lastUsed", now)
        profile.setdefault("created", now)
        profile.setdefault("javaArgs", "-Xmx4G -Xms1G")
        if old != profile:
            profiles["repomine"] = profile
            _write_json(path, data)
    return profile["name"]


def configure_server(root, manifest, *, lock=True):
    from repomine_core import PackLock
    with PackLock(root) if lock else nullcontext():
        return _configure_server(root, manifest)


def _configure_server(root, manifest):
    import nbtlib
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    server = manifest.get("server") or {}
    if isinstance(server, str):
        server = {"address": server}
    address = server.get("address", "tecserver.playit.plus")
    name = server.get("name", manifest.get("pack_name", "Servidor Gael"))
    if not isinstance(address, str) or not address.strip() or any(c.isspace() for c in address):
        raise ValueError("La dirección del servidor no es válida.")
    path = root / "servers.dat"
    data = nbtlib.load(path, gzipped=False) if path.exists() else nbtlib.File({"servers": nbtlib.List[nbtlib.Compound]()})
    servers = data.get("servers")
    if not isinstance(servers, nbtlib.List) or any(not isinstance(item, nbtlib.Compound) for item in servers):
        raise ValueError("servers.dat no contiene una lista válida; se conservó intacto.")
    if any(str(item.get("ip", "")).lower() == address.lower() for item in servers):
        return False
    servers.append(nbtlib.Compound({"name": nbtlib.String(name), "ip": nbtlib.String(address)}))
    fd, temporary = tempfile.mkstemp(prefix="servers-", suffix=".tmp", dir=root)
    os.close(fd)
    try:
        data.save(temporary, gzipped=False)
        _backup(path)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return True


def _java(root, minecraft):
    from minecraft_launcher_lib import runtime
    information = runtime.get_version_runtime_information(minecraft, str(root))
    if not information:
        raise RuntimeError("Minecraft no indicó qué versión de Java necesita.")
    java = runtime.get_executable_path(information["name"], str(root))
    if not java or not Path(java).is_file():
        raise RuntimeError("No se encontró Java. Ejecuta Preparar Minecraft de nuevo.")
    return java


def prepare_game(root, manifest, log=lambda text: None, status=lambda text: None, *, lock=True):
    from repomine_core import PackLock
    with PackLock(root) if lock else nullcontext():
        with _bounded_network():
            return _prepare_game(root, manifest, log, status)


def _prepare_game(root, manifest, log, status):
    from minecraft_launcher_lib import install, mod_loader
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    minecraft, loader_version = _versions(manifest)
    # The upstream NeoForge installer also writes launcher profiles.
    for filename in ("launcher_profiles.json", "launcher_profiles_microsoft_store.json"):
        path = root / filename
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("profiles", {}), dict):
                raise ValueError(f"{filename} no contiene perfiles válidos; se conservó intacto.")
            _backup(path)
    callback = {"setStatus": status}
    log(f"Preparando Minecraft {minecraft} y su Java oficial. La primera descarga puede tardar varios minutos.")
    install.install_minecraft_version(minecraft, str(root), callback=callback)
    java = _java(root, minecraft)
    log(f"Instalando NeoForge {loader_version}.")
    loader = mod_loader.get_mod_loader("neoforge")
    # The pinned dependency runs the downloaded Java installer. Scope a proxy to
    # its own module so other subprocess callers never inherit this timeout.
    from minecraft_launcher_lib.mod_loader import _neoforge
    from types import SimpleNamespace
    original_subprocess = _neoforge.subprocess

    def run_installer(*args, **kwargs):
        kwargs.setdefault("timeout", 1200)
        return subprocess.run(*args, **kwargs)

    _neoforge.subprocess = SimpleNamespace(run=run_installer, PIPE=subprocess.PIPE)
    try:
        version_id = loader.install(minecraft, str(root), loader_version=loader_version, callback=callback, java=java)
    finally:
        _neoforge.subprocess = original_subprocess
    profile = configure_profile(root, manifest, version_id, java)
    configure_server(root, manifest, lock=False)
    log(f"Perfil listo: {profile}. Servidor agregado a Multijugador.")
    return {"version_id": version_id, "java_path": java, "profile_name": profile}


def begin_login(client_id, redirect_uri):
    from minecraft_launcher_lib import microsoft_account
    if not client_id or not redirect_uri:
        raise DirectLaunchUnavailable("Configura el ID de aplicación Microsoft y su URI de redirección para iniciar sesión.")
    return microsoft_account.get_secure_login_data(client_id, redirect_uri)


def complete_login(client_id, redirect_uri, response_url, state, verifier):
    from minecraft_launcher_lib import microsoft_account
    from urllib.parse import urlsplit
    expected, actual = urlsplit(redirect_uri), urlsplit(response_url)
    if (expected.scheme, expected.netloc, expected.path) != (actual.scheme, actual.netloc, actual.path):
        raise ValueError("La dirección de respuesta no corresponde a la redirección configurada.")
    try:
        code = microsoft_account.parse_auth_code_url(response_url, state)
        # Public desktop application: no client secret is embedded or requested.
        with _bounded_network():
            session = microsoft_account.complete_login(client_id, None, redirect_uri, code, verifier)
        return {key: session[key] for key in ("id", "name", "access_token")}
    except Exception:
        # OAuth exceptions can include codes, tokens or response bodies. Never
        # expose them through GUI reports, including chained tracebacks.
        raise RuntimeError("No se pudo iniciar sesión. Comprueba que la cuenta tenga Minecraft Java y que la aplicación Microsoft esté autorizada; después inténtalo de nuevo.") from None


def login_microsoft(settings, log=lambda text: None, status=lambda text: None):
    """Receive a PKCE callback only on loopback; never persist or log credentials."""
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import urlsplit, parse_qs
    import webbrowser

    client_id = settings.get("client_id") or settings.get("microsoft_client_id")
    redirect_uri = settings.get("redirect_uri", "http://localhost:53682/")
    redirect = urlsplit(redirect_uri)
    if (redirect.scheme != "http" or redirect.hostname not in ("localhost", "127.0.0.1")
            or not redirect.port or redirect.query or redirect.fragment or redirect.username):
        raise ValueError("Usa una redirección local registrada, por ejemplo http://localhost:53682/.")
    url, state, verifier = begin_login(client_id, redirect_uri)
    received = {}

    class CallbackHandler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass  # The request includes a one-use authorization code.

        def do_GET(self):
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query)
            if parsed.path != (redirect.path or "/") or query.get("state") != [state]:
                self.send_error(400, "Invalid login response")
                return
            received["url"] = redirect_uri.split("?", 1)[0] + "?" + parsed.query
            body = b"Login received. You can close this window and return to RepoMine."
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    try:
        server = HTTPServer(("127.0.0.1", redirect.port), CallbackHandler)
    except OSError as exc:
        raise RuntimeError("No se pudo abrir la respuesta de inicio de sesión. Cierra otro instalador que esté iniciando sesión.") from exc
    with server:
        server.timeout = 1
        status("Completa el inicio de sesión Microsoft en tu navegador (máximo 2 minutos).")
        log("Abriendo Microsoft para iniciar sesión. La sesión solo se conserva mientras esté abierto el instalador.")
        if not webbrowser.open(url):
            raise RuntimeError("No se pudo abrir el navegador predeterminado.")
        deadline = time.monotonic() + 120
        while not received and time.monotonic() < deadline:
            server.handle_request()
        if not received:
            raise TimeoutError("El inicio de sesión expiró. Inténtalo de nuevo.")
    return complete_login(client_id, redirect_uri, received["url"], state, verifier)


def launch_game(root, manifest, settings, log=lambda text: None):
    """Start licensed Java Edition using the user's explicit, in-memory login."""
    if settings.get("launcher", "official") != "official":
        raise DirectLaunchUnavailable("El inicio directo está habilitado únicamente para usuarios del launcher oficial.")
    session = settings.get("session")
    if not isinstance(session, dict) or not all(session.get(k) for k in ("id", "name", "access_token")):
        raise DirectLaunchUnavailable("Inicia sesión con Microsoft para jugar directamente. El instalador no reutiliza la sesión del launcher oficial. También puedes seleccionar el perfil RepoMine en ese launcher.")
    from minecraft_launcher_lib import command, mod_loader
    root = Path(root).resolve()
    minecraft, loader_version = _versions(manifest)
    version = mod_loader.get_mod_loader("neoforge").get_installed_version(minecraft, loader_version)
    if not (root / "versions" / version / f"{version}.json").is_file():
        raise RuntimeError("Primero ejecuta Preparar Minecraft para instalar NeoForge.")
    ram = int(settings.get("ram_mb", 4096))
    if ram < 2048 or ram > 65536:
        raise ValueError("La memoria debe estar entre 2048 y 65536 MB.")
    options = {"username": session["name"], "uuid": session["id"], "token": session["access_token"],
               "executablePath": _java(root, minecraft), "gameDirectory": str(root),
               "jvmArguments": [f"-Xmx{ram}M", "-Xms1G"], "launcherName": "RepoMine", "launcherVersion": "1.1.0"}
    arguments = command.get_minecraft_command(version, str(root), options)
    # Never log arguments: they contain the access token. Minecraft owns latest.log.
    process = subprocess.Popen(arguments, cwd=root, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    log("Minecraft iniciado. Encontrarás el servidor en Multijugador.")
    return process
