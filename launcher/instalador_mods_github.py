"""Interfaz de RepoMine; compilar con build.ps1 o ejecutar con Python 3.11+."""
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import urllib.request
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from urllib.error import HTTPError

from repomine_core import (
    atomic_json, download_json, download_verified,
    inspect_pack, sha256_file, synchronize, validate_manifest, PackLock, SSL_CONTEXT,
)

APP_NAME = "RepoMine · Servidor Gael"
APP_VERSION = "1.1.0"
MANIFEST_URL = "https://raw.githubusercontent.com/Qmigo745/RepoMine/main/manifest.json"
LATEST_RELEASE_URL = "https://api.github.com/repos/Qmigo745/RepoMine/releases/latest"
UPDATE_ASSET_NAME = "InstaladorModsMinecraft.exe"
SETTINGS_PATH = Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / "RepoMine" / "installer_settings.json"


def default_minecraft_dir():
    return Path(os.getenv("APPDATA", str(Path.home()))) / ".minecraft"


def open_folder(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        os.startfile(str(path))
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])


def version_tuple(version):
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", str(version))
    if not match:
        raise ValueError(f"Versión de instalador no válida: {version}")
    return tuple(map(int, match.groups()))


def find_update_asset(release):
    return next((a for a in release.get("assets", []) if a.get("name") == UPDATE_ASSET_NAME), None)


def release_url(asset):
    url = str(asset.get("browser_download_url", ""))
    if not url.startswith("https://github.com/Qmigo745/RepoMine/releases/download/"):
        raise ValueError("La actualización no pertenece a este repositorio.")
    return url


def update_digest(release, asset):
    digest = str(asset.get("digest", ""))
    if re.fullmatch(r"sha256:[a-fA-F0-9]{64}", digest):
        return digest[7:].lower()
    checksum = next((a for a in release.get("assets", []) if a.get("name") == "SHA256SUMS.txt"), None)
    if not checksum:
        raise ValueError("La actualización no incluye SHA-256 ni SHA256SUMS.txt. Vuelve a publicar ambos archivos con build.ps1.")
    request = urllib.request.Request(release_url(checksum), headers={"User-Agent": "RepoMine"})
    with urllib.request.urlopen(request, timeout=30, context=SSL_CONTEXT) as response:
        text = response.read(65537)
    if len(text) > 65536:
        raise ValueError("Archivo de checksums demasiado grande.")
    matches = re.findall(r"(?m)^([a-fA-F0-9]{64})  InstaladorModsMinecraft\.exe\r?$", text.decode("utf-8-sig"))
    if len(matches) != 1:
        raise ValueError("SHA256SUMS.txt no contiene un checksum único del instalador.")
    return matches[0].lower()


def schedule_executable_replacement(new_executable):
    current = Path(sys.executable).resolve()
    backup = current.with_suffix(".previous.exe")
    script_path = Path(tempfile.gettempdir()) / f"repomine-update-{os.getpid()}.ps1"
    def quote(value):
        return str(value).replace("'", "''")
    script = f"""$ErrorActionPreference = 'Stop'
Wait-Process -Id {os.getpid()} -ErrorAction SilentlyContinue
for ($attempt = 0; $attempt -lt 20; $attempt++) {{
    try {{
        Copy-Item -LiteralPath '{quote(current)}' -Destination '{quote(backup)}' -Force
        Move-Item -LiteralPath '{quote(new_executable)}' -Destination '{quote(current)}' -Force
        Start-Process -FilePath '{quote(current)}' -WindowStyle Hidden
        Remove-Item -LiteralPath $MyInvocation.MyCommand.Path -Force
        exit 0
    }} catch {{ Start-Sleep -Milliseconds 500 }}
}}
"""
    script_path.write_text(script, encoding="utf-8-sig")
    subprocess.Popen(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
                      "-File", str(script_path)], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


class ModInstallerApp:
    def __init__(self, root):
        self.root = root
        root.title(f"{APP_NAME} · {APP_VERSION}")
        root.geometry("930x760")
        root.minsize(800, 690)
        self.events = queue.Queue()
        self.is_busy = False
        self.manifest = None
        self.session = None
        self.game_process = None
        self.activity = []
        self.settings = self.load_settings()
        self.minecraft_dir = tk.StringVar(value=self.settings.get("minecraft_dir", str(default_minecraft_dir())))
        self.status_var = tk.StringVar(value="Listo. Cierra Minecraft antes de instalar o reparar.")
        self.pack_var = tk.StringVar(value="Minecraft 1.21.1 · NeoForge 21.1.233 · tecserver.playit.plus")
        self.action_buttons = []
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.build_ui()
        root.after(80, self.process_events)
        if getattr(sys, "frozen", False):
            root.after(1500, lambda: self.run_threaded(self.check_for_updates) if not self.is_busy else None)

    def load_settings(self):
        try:
            data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def save_settings(self):
        self.settings["minecraft_dir"] = self.minecraft_dir.get().strip()
        atomic_json(SETTINGS_PATH, self.settings)

    def build_ui(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TFrame", background="#F3F6F4")
        style.configure("TLabel", background="#F3F6F4", foreground="#193426", font=("Segoe UI", 10))
        style.configure("TButton", padding=9, font=("Segoe UI", 10))
        style.configure("Title.TLabel", font=("Segoe UI", 24, "bold"))
        body = ttk.Frame(self.root, padding=24)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Juega con tus amigos", style="Title.TLabel").pack(anchor="w")
        ttk.Label(body, textvariable=self.pack_var).pack(anchor="w", pady=(4, 18))
        ttk.Label(body, text="Carpeta de juego (debe coincidir con el perfil de Minecraft)").pack(anchor="w")
        row = ttk.Frame(body)
        row.pack(fill="x", pady=(5, 12))
        self.path_entry = ttk.Entry(row, textvariable=self.minecraft_dir)
        self.path_entry.pack(side="left", fill="x", expand=True, ipady=5)
        self.folder_button = ttk.Button(row, text="Examinar…", command=self.select_minecraft_folder)
        self.folder_button.pack(side="left", padx=(8, 0))
        actions = ttk.Frame(body)
        actions.pack(fill="x")
        for column in range(2):
            actions.columnconfigure(column, weight=1)
        definitions = [
            ("Preparar Minecraft y mods", lambda: self.start_install(prepare=True)),
            ("Jugar", self.start_play),
            ("Verificar archivos", lambda: self.run_threaded(self.verify_mods)),
            ("Instalar / reparar mods", lambda: self.start_install(prepare=False)),
            ("Configuración e inicio de sesión", self.show_settings),
            ("Guardar diagnóstico", self.export_report),
        ]
        for index, (label, command) in enumerate(definitions):
            button = ttk.Button(actions, text=label, command=command)
            button.grid(row=index // 2, column=index % 2, sticky="ew", padx=(0, 5), pady=4)
            self.action_buttons.append(button)
        self.progress = ttk.Progressbar(body, maximum=100)
        self.progress.pack(fill="x", pady=(14, 6))
        ttk.Label(body, textvariable=self.status_var, wraplength=820).pack(anchor="w", pady=(0, 10))
        output_row = ttk.Frame(body)
        output_row.pack(fill="both", expand=True)
        self.output = tk.Text(output_row, height=11, bg="#18221D", fg="#E2F1E7", font=("Consolas", 9),
                              state="disabled", wrap="word", padx=10, pady=10)
        scrollbar = ttk.Scrollbar(output_row, command=self.output.yview)
        self.output.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.output.pack(fill="both", expand=True)
        footer = ttk.Frame(body)
        footer.pack(fill="x", pady=(10, 0))
        for title, suffix in (("Abrir mods", "mods"), ("Abrir respaldos", ".repomine/backups")):
            button = ttk.Button(footer, text=title, command=lambda s=suffix: open_folder(Path(self.minecraft_dir.get()) / s))
            button.pack(side="left", padx=(0, 6))
            self.action_buttons.append(button)
        button = ttk.Button(footer, text=f"Actualizar · {APP_VERSION}", command=lambda: self.run_threaded(lambda: self.check_for_updates(True)))
        button.pack(side="right")
        self.action_buttons.append(button)

    def process_events(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "log":
                    self.activity.append(value)
                    self.activity = self.activity[-3000:]
                    self.output.configure(state="normal")
                    self.output.insert("end", value + "\n")
                    self.output.see("end")
                    self.output.configure(state="disabled")
                elif kind == "status":
                    self.status_var.set(value)
                elif kind == "progress":
                    self.progress.configure(value=value)
                elif kind == "pack":
                    self.pack_var.set(value)
                elif kind == "done":
                    self.is_busy = False
                    for widget in self.action_buttons + [self.path_entry, self.folder_button]:
                        widget.configure(state="normal")
                elif kind == "error":
                    messagebox.showerror(APP_NAME, value, parent=self.root)
                elif kind == "callback":
                    value()
                    if not self.root.winfo_exists():
                        return
        except queue.Empty:
            pass
        self.root.after(80, self.process_events)

    def log(self, message):
        self.events.put(("log", f"[{datetime.now():%H:%M:%S}] {message}"))

    def set_status(self, text):
        self.events.put(("status", text))

    def run_threaded(self, action):
        if self.is_busy:
            return
        try:
            if not self.minecraft_dir.get().strip():
                raise ValueError("Selecciona la carpeta de Minecraft.")
            self.operation_root = Path(self.minecraft_dir.get()).expanduser().resolve()
            if self.operation_root == Path(self.operation_root.anchor):
                raise ValueError("Selecciona la carpeta de Minecraft, no la raíz del disco.")
            self.save_settings()
        except (OSError, ValueError) as error:
            messagebox.showerror(APP_NAME, str(error))
            return
        self.is_busy = True
        for widget in self.action_buttons + [self.path_entry, self.folder_button]:
            widget.configure(state="disabled")
        self.progress.configure(value=0)
        def worker():
            try:
                action()
            except Exception as error:
                self.log(f"{type(error).__name__}: {error}")
                self.set_status("No se completó la operación. Puedes guardar un diagnóstico.")
                self.events.put(("error", str(error)))
            finally:
                self.events.put(("done", None))
        threading.Thread(target=worker, daemon=True).start()

    def select_minecraft_folder(self):
        path = filedialog.askdirectory(title="Carpeta de juego del perfil de Minecraft")
        if path:
            self.minecraft_dir.set(path)

    def load_manifest(self):
        self.set_status("Consultando el modpack…")
        self.manifest = validate_manifest(download_json(MANIFEST_URL + "?v=" + str(datetime.now().timestamp())))
        self.events.put(("pack", f"{self.manifest['pack_name']} · Minecraft {self.manifest['minecraft_version']} · "
                         f"{self.manifest['loader']} {self.manifest['loader_version']}"))
        self.log(f"Catálogo: {len(self.manifest['mods'])} mods, {len(self.manifest.get('resourcepacks', []))} paquetes de recursos.")
        return self.manifest

    def verify_mods(self):
        report = inspect_pack(self.operation_root, self.load_manifest())
        for label, key in (("Faltante", "missing"), ("Diferente", "changed"), ("Adicional conservado", "extras")):
            for relative in report[key]:
                self.log(f"{label}: {relative}")
        message = f"Verificación: {len(report['missing'])} faltantes, {len(report['changed'])} diferentes, {len(report['extras'])} adicionales."
        self.log(message)
        self.set_status(message)
        return report

    def start_install(self, prepare=False):
        if self.game_process is not None and self.game_process.poll() is None:
            messagebox.showinfo(APP_NAME, "Cierra el juego iniciado por RepoMine antes de actualizar.")
            return
        if not messagebox.askokcancel(APP_NAME, "Cierra Minecraft antes de continuar. Se conservarán respaldos de los archivos reemplazados.", parent=self.root):
            return
        self.run_threaded(lambda: self.install_pack(prepare))

    def install_pack(self, prepare=False):
        manifest = self.load_manifest()
        with PackLock(self.operation_root):
            if prepare:
                from repomine_runtime import prepare_game
                prepare_game(self.operation_root, manifest, self.log, self.set_status, lock=False)
            self.set_status("Descargando y verificando archivos…")
            report = synchronize(self.operation_root, manifest, self.log,
                                 lambda done, total: self.events.put(("progress", done * 100 / max(1, total))), lock=False)
            from repomine_runtime import configure_server
            configure_server(self.operation_root, manifest, lock=False)
        self.events.put(("progress", 100))
        self.set_status("Pack preparado. Revisa los archivos adicionales indicados en Actividad." if report["extras"] else "Pack preparado correctamente.")
        return report

    def start_play(self):
        if self.game_process is not None and self.game_process.poll() is None:
            messagebox.showinfo(APP_NAME, "Minecraft ya está abierto.")
            return
        if self.settings.get("launcher", "official") != "official":
            messagebox.showinfo(APP_NAME, "El arranque directo está disponible únicamente para el launcher oficial. Puedes instalar/reparar y abrir tu launcher habitual.")
            return
        if not self.session:
            messagebox.showinfo(APP_NAME, "Para iniciar directamente necesitas iniciar sesión en Microsoft desde Configuración. La sesión del launcher oficial no se comparte con RepoMine.")
            return
        def play():
            from repomine_runtime import launch_game
            report = self.install_pack(prepare=True)
            if report["extras"]:
                raise RuntimeError("Hay mods adicionales. Revisa la lista antes de iniciar el juego; RepoMine no los borra automáticamente.")
            self.game_process = launch_game(self.operation_root, self.manifest, dict(self.settings, session=self.session), self.log)
            self.set_status("Minecraft iniciado.")
        self.run_threaded(play)

    def show_settings(self):
        window = tk.Toplevel(self.root)
        window.title("Configuración de RepoMine")
        window.transient(self.root)
        window.grab_set()
        frame = ttk.Frame(window, padding=20)
        frame.pack(fill="both", expand=True)
        launcher = tk.StringVar(value=self.settings.get("launcher", "official"))
        ttk.Label(frame, text="Launcher que utilizas").pack(anchor="w")
        ttk.Radiobutton(frame, text="Oficial de Minecraft", variable=launcher, value="official").pack(anchor="w")
        ttk.Radiobutton(frame, text="Otro (solo instalar y reparar)", variable=launcher, value="other").pack(anchor="w")
        ttk.Label(frame, text="Aplicación Microsoft de RepoMine (configuración del administrador)").pack(anchor="w", pady=(12, 4))
        client_id = tk.StringVar(value=self.settings.get("client_id", ""))
        ttk.Entry(frame, textvariable=client_id, width=60).pack(fill="x")
        redirect_uri = tk.StringVar(value=self.settings.get("redirect_uri", "http://localhost:53682/"))
        ttk.Label(frame, text="URI de retorno registrada").pack(anchor="w", pady=(8, 4))
        ttk.Entry(frame, textvariable=redirect_uri, width=60).pack(fill="x")
        ttk.Label(frame, text="El administrador debe registrar y autorizar esta aplicación antes del primer inicio de sesión.\nLas contraseñas se introducen únicamente en la página de Microsoft.\nLa sesión se conserva solo mientras RepoMine esté abierto.", wraplength=510).pack(anchor="w", pady=12)
        def save():
            self.settings.update(launcher=launcher.get(), client_id=client_id.get().strip(), redirect_uri=redirect_uri.get().strip())
            self.save_settings()
            window.destroy()
        def login():
            save()
            self.begin_login()
        ttk.Button(frame, text="Guardar", command=save).pack(side="left")
        ttk.Button(frame, text="Iniciar sesión con Microsoft", command=login).pack(side="left", padx=8)
        ttk.Button(frame, text="Cerrar sesión", command=lambda: self.logout(window)).pack(side="left")

    def logout(self, window):
        self.session = None
        self.set_status("Sesión cerrada.")
        window.destroy()

    def begin_login(self):
        from repomine_runtime import login_microsoft
        def login():
            self.session = login_microsoft(self.settings, self.log, self.set_status)
            self.set_status("Sesión Microsoft iniciada. Ya puedes usar Jugar.")
        self.run_threaded(login)

    def export_report(self):
        filename = filedialog.asksaveasfilename(defaultextension=".txt", initialfile="diagnostico-repomine.txt", filetypes=[("Texto", "*.txt")])
        if not filename:
            return
        root = str(Path(self.minecraft_dir.get()).expanduser())
        text = "\n".join([f"RepoMine {APP_VERSION}", f"Plataforma: {sys.platform}", "Carpeta: <Minecraft>", *self.activity])
        text = text.replace(root, "<Minecraft>").replace(str(Path.home()), "<Usuario>")
        try:
            Path(filename).write_text(text, encoding="utf-8")
            self.status_var.set("Diagnóstico guardado.")
        except OSError as error:
            messagebox.showerror(APP_NAME, str(error))

    def check_for_updates(self, show_result=False):
        if not getattr(sys, "frozen", False) or os.name != "nt":
            self.set_status("Ejecutas el código Python. Actualízalo y vuelve a compilar con build.ps1.")
            return
        self.set_status("Buscando actualización del instalador…")
        try:
            release = download_json(LATEST_RELEASE_URL)
        except HTTPError as error:
            if error.code == 404:
                self.set_status("Todavía no hay versiones publicadas.")
                return
            if show_result:
                raise
            self.set_status("No se pudo consultar la actualización; puedes continuar.")
            return
        except (OSError, ValueError):
            if show_result:
                raise
            self.set_status("No se pudo consultar la actualización; puedes continuar.")
            return
        if version_tuple(release.get("tag_name")) <= version_tuple(APP_VERSION):
            self.set_status("El instalador está actualizado.")
            return
        asset = find_update_asset(release)
        if not asset:
            raise ValueError(f"La nueva versión no contiene {UPDATE_ASSET_NAME}.")
        digest = update_digest(release, asset)
        url = release_url(asset)
        update_path = Path(sys.executable).with_suffix(".update.exe")
        download_verified(url, update_path, digest, expected_size=asset.get("size"))
        if update_path.stat().st_size < 1024 * 1024 or sha256_file(update_path) == sha256_file(Path(sys.executable)):
            raise ValueError("La actualización no es un ejecutable nuevo válido.")
        def offer():
            if messagebox.askyesno(APP_NAME, f"Versión {release['tag_name']} descargada y verificada. ¿Reiniciar para actualizar?"):
                schedule_executable_replacement(update_path)
                self.root.destroy()
        self.events.put(("callback", offer))

    def close(self):
        if self.is_busy:
            messagebox.showinfo(APP_NAME, "Espera a que termine la operación antes de cerrar.")
            return
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    ModInstallerApp(root)
    root.mainloop()
