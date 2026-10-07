"""Descarga los cuatro JAR adicionales grandes sin generar el catálogo SHA-256."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parent
SOURCES = ROOT / "docs" / "mods-descargas-adicionales.json"

def verify(path: Path, item: dict) -> bool:
    if path.is_symlink() or not path.is_file() or path.stat().st_size != item["size"]:
        return False
    digest = hashlib.sha512()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != item["sha512"]:
        return False
    with zipfile.ZipFile(path) as jar:
        return jar.testzip() is None

def download(destination: Path, item: dict) -> None:
    name = item["file"]
    if Path(name).name != name or not name.endswith(".jar"):
        raise ValueError(f"Nombre de archivo inválido: {name}")
    target = destination / name
    if target.is_symlink():
        raise ValueError(f"No se reemplazan enlaces simbólicos: {target}")
    if verify(target, item):
        print(f"Verificado: {name}", flush=True)
        return
    for attempt in range(1, 4):
        temporary = None
        try:
            print(f"Descargando {name} ({attempt}/3)...", flush=True)
            with tempfile.NamedTemporaryFile(dir=destination, suffix=".download", delete=False) as stream:
                temporary = Path(stream.name)
                request = urllib.request.Request(item["url"], headers={"User-Agent": "RepoMine-pack-downloader/1"})
                with urllib.request.urlopen(request, timeout=60) as response:
                    total = 0
                    while chunk := response.read(1024 * 1024):
                        total += len(chunk)
                        if total > item["size"]:
                            raise ValueError("La descarga excede el tamaño publicado")
                        stream.write(chunk)
            if not verify(temporary, item):
                raise ValueError(f"Tamaño, SHA-512 o estructura JAR incorrectos: {name}")
            os.replace(temporary, target)
            print(f"Instalado: {name}", flush=True)
            return
        except Exception:
            if attempt == 3:
                raise
            time.sleep(attempt)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-only", action="store_true", help="Comprueba los archivos sin descargarlos")
    args = parser.parse_args()
    destination = ROOT / "mods"
    destination.mkdir(exist_ok=True)
    if destination.is_symlink():
        raise ValueError("La carpeta mods no puede ser un enlace simbólico")
    for item in json.loads(SOURCES.read_text(encoding="utf-8")):
        if args.verify_only:
            if not verify(destination / item["file"], item):
                raise ValueError(f"Archivo ausente o inválido: {item['file']}")
            print(f"Verificado: {item['file']}")
        else:
            download(destination, item)
    print("Cuatro JAR listos. Ahora genera y publica el manifiesto completo.")
    return 0

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1)
