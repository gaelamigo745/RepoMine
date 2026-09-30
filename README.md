# RepoMine — Servidor Gael

Herramientas para publicar y preparar el pack de Minecraft **1.21.1 / NeoForge 21.1.233**, con acceso al servidor `tecserver.playit.plus`.

## Para jugar

Descarga `InstaladorModsMinecraft.exe` desde las Releases del repositorio y ejecútalo en Windows. Selecciona la carpeta de Minecraft que utilizarás y pulsa **Preparar Minecraft y mods**. Cierra Minecraft y el launcher antes de preparar sus archivos. Para posteriores actualizaciones que no cambien Minecraft ni NeoForge, puedes usar **Instalar / reparar mods**.

La preparación descarga Minecraft, su Java compatible y NeoForge, sincroniza los mods y paquetes de recursos, crea un perfil RepoMine para el launcher oficial y agrega el servidor a la lista multijugador. La primera preparación necesita Internet y puede tardar varios minutos. Los paquetes de recursos se descargan; su activación sigue siendo una elección del jugador.

El arranque directo requiere configurar una aplicación Microsoft propia autorizada para las APIs de Minecraft y que cada jugador inicie sesión. Sin esa configuración, puedes preparar el juego y seleccionar el perfil RepoMine en el launcher oficial. El instalador no extrae contraseñas ni tokens del launcher. Los launchers alternativos pueden utilizar los archivos preparados, pero no tienen integración de inicio automático.

El perfil se escribe en la carpeta seleccionada. El launcher oficial normalmente consulta `%APPDATA%\.minecraft`: si eliges otra carpeta, no descubrirá automáticamente ese perfil ni sus versiones. Para el flujo con el launcher oficial, prepara su carpeta habitual; una carpeta personalizada puede utilizarse con el arranque directo de RepoMine, una vez configurado e iniciado sesión.

## Configurar el inicio directo

Como administrador, registra una aplicación que acepte cuentas Microsoft personales y copia su **Application (client) ID**, siguiendo la [guía oficial de Microsoft](https://learn.microsoft.com/en-us/entra/identity-platform/quickstart-register-app). Configúrala como aplicación pública de escritorio, con redirección `http://localhost:53682/`; consulta las [reglas oficiales de redirección](https://learn.microsoft.com/en-us/entra/identity-platform/reply-url). No se necesita ni se debe distribuir un secreto de cliente.

El registro en Microsoft Entra por sí solo no garantiza acceso a Minecraft Services: la aplicación necesita la autorización correspondiente para las APIs de Minecraft. Hasta contar con ella y probar una cuenta que tenga Minecraft Java, el inicio directo permanece pendiente de validación real. No uses identificadores de aplicaciones de otros launchers.

En **Configuración e inicio de sesión**, selecciona el launcher oficial, introduce el ID de tu aplicación y su URI de redirección registrada, guarda e inicia sesión. El navegador recibe el inicio de sesión de Microsoft; RepoMine espera la respuesta local durante dos minutos y valida PKCE y el estado de la solicitud. Después puedes pulsar **Jugar**, que sincroniza el pack y abre el proceso del juego sin abrir el launcher.

La sesión se conserva únicamente en memoria mientras el instalador está abierto; al cerrarlo debes volver a iniciar sesión. La carpeta elegida, el tipo de launcher, el ID público y la redirección se guardan en `%LOCALAPPDATA%\RepoMine\installer_settings.json`. No se guardan allí tokens ni contraseñas.

Por ahora cada instalación debe introducir el ID público y la redirección en Configuración. El administrador puede compartir esos dos valores; todavía no se incluyen automáticamente como valores predeterminados del EXE ni del manifiesto.

## Qué hace cada archivo

| Archivo | Responsabilidad |
| --- | --- |
| `generar_manifest.py` | Administrar el pack, generar su catálogo y publicar archivos mediante Git. |
| `instalador_mods_github.py` | Interfaz para verificar, preparar y actualizar el pack. |
| `repomine_core.py` | Validación, SHA-256, descargas y seguimiento de archivos administrados. |
| `repomine_runtime.py` | Preparación del juego, Java y NeoForge; perfil, servidor y autenticación. |
| `manifest.json` | Versiones, direcciones de descarga y hashes del pack. |
| `pack_config.json` | Valores del servidor, versiones y políticas de configuración usados para generar el catálogo. |
| `mods/` y `Resourcepacks/` | Archivos distribuidos a los jugadores. |
| `shared-config/` | Configuraciones iniciales opcionales, instaladas en `config/`. |
| `build.ps1` y `InstaladorModsMinecraft.spec` | Compilación del ejecutable de Windows. |
| `.github/workflows/installer.yml` | Compilación y publicación opcionales en GitHub Actions. |

`mods.zip` es un archivo personal para transportar mods: se ignora en Git y no forma parte de la distribución. Puedes seguir utilizándolo.

## Actualizaciones y archivos personales

El manifiesto se valida antes de instalar. Las descargas usan HTTPS, hasta cuatro trabajadores, reintentos y SHA-256 obligatorio. Primero se descargan y verifican todos los archivos pendientes; después se aplican los cambios. Una descarga fallida no reemplaza archivos del juego.

En la carpeta seleccionada, `.repomine/state.json` registra los archivos que administra el instalador. Cuando retiras un archivo del pack, una actualización posterior lo retira del jugador solamente si estaba registrado y conserva el hash esperado. Los archivos adicionales o modificados por el jugador se conservan y se reportan. Las instalaciones antiguas sin registro pueden conservar versiones sobrantes: revisa los archivos adicionales que indique el instalador si Minecraft informa de mods duplicados.

**Jugar** comprueba también los mods adicionales y detiene el arranque si encuentra alguno. Revisa la lista y mueve los JAR que no pertenezcan al pack a una carpeta de respaldo fuera de `mods/` antes de intentarlo de nuevo.

Los reemplazos y retiros se respaldan en `.repomine/backups/`. Si falla la aplicación de cambios, se intenta restaurar lo aplicado. No borres el registro de estado para intentar reparar una instalación: se perdería la información necesaria para retirar archivos antiguos de forma selectiva.

Las configuraciones compartidas usan por defecto la política `default`: se copian si faltan y se conservan si ya existen. La política `managed` del manifiesto permite sincronizar una configuración obligatoria. No distribuyas controles, preferencias gráficas ni datos personales como ajustes obligatorios. El servidor y los perfiles del launcher tienen sus propias copias de seguridad antes de modificarse.

## Desarrollo y administración

Utiliza Python 3.11 o posterior; la compilación automatizada utiliza Python 3.12. El instalador depende de `certifi`, `minecraft-launcher-lib` y `nbtlib`. PyInstaller se instala únicamente para compilar. Las dependencias directas tienen versiones fijadas en los archivos `requirements`; sus dependencias transitivas y las herramientas del entorno no están bloqueadas, por lo que no se garantiza un EXE idéntico byte por byte entre compilaciones.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe generar_manifest.py
.\.venv\Scripts\python.exe instalador_mods_github.py
```

Para actualizar el pack, coloca los JAR en `mods/`, los ZIP de recursos en `Resourcepacks/` y las configuraciones compartidas opcionales en `shared-config/`. Genera y revisa el manifiesto antes de publicarlo:

```powershell
.\.venv\Scripts\python.exe generar_manifest.py --generate
git diff -- manifest.json
```

La interfaz del administrador permite publicar. La alternativa por consola `--publish-all` genera, crea un commit y hace push del pack; úsala cuando quieras publicar los cambios. Requiere Git y acceso de escritura a `Qmigo745/RepoMine`. `--upload-manifest` y `--upload-mods` publican partes del pack por separado; evita dejar el manifiesto apuntando a archivos todavía no publicados.

Los JAR pueden declarar varios IDs: el generador registra sus IDs declarados sin confundirlos con dependencias. La retirada de versiones en los clientes se basa en el registro de archivos, no en deducir identidades a partir de sus nombres.

## Compilar manualmente

La compilación manual sigue disponible:

```powershell
.\build.ps1
```

El script crea `.venv-build/`, instala las dependencias, ejecuta las pruebas y construye:

- `dist/InstaladorModsMinecraft.exe`
- `dist/SHA256SUMS.txt`

Sube ambos archivos a la misma GitHub Release. Conserva esos nombres. El actualizador verifica el EXE mediante el campo SHA-256 `digest` que devuelve la API de GitHub para el archivo de la Release. Si GitHub no lo proporciona, obtiene `SHA256SUMS.txt` de esa misma Release y exige una única entrada válida con el nombre exacto `InstaladorModsMinecraft.exe`; sin ningún hash verificable, detiene la actualización. El checksum también sirve para comprobar descargas manuales. La versión del tag debe ser `v` seguida del valor de `APP_VERSION` en `instalador_mods_github.py`, por ejemplo `v1.1.0`. Incrementa esa constante al publicar una nueva versión del instalador; actualizar solamente los mods no necesita recompilarlo.

Si PowerShell bloquea scripts por la política de tu equipo, puedes ejecutar los mismos pasos manualmente:

```powershell
python -m venv .venv-build
.\.venv-build\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv-build\Scripts\python.exe -m unittest discover -s tests -v
.\.venv-build\Scripts\python.exe -m PyInstaller --noconfirm --clean InstaladorModsMinecraft.spec
```

En ese caso, genera también `SHA256SUMS.txt` con una línea `<sha256>  InstaladorModsMinecraft.exe`, en UTF-8, a partir del hash del EXE final. El ejecutable no tiene firma de código; la verificación SHA-256 comprueba integridad, pero no sustituye una firma del editor.

## Automatización opcional

En GitHub, **Actions → Compilar instalador → Run workflow** verifica y construye un artefacto descargable sin publicar una Release. Las pull requests que cambian código o herramientas también se verifican.

Al subir un tag `v*`, el workflow exige que coincida con `APP_VERSION`, ejecuta las pruebas, compila en Windows y publica una Release con el EXE y su checksum. Crear los archivos del workflow localmente no publica nada. No subas un tag si deseas conservar una publicación exclusivamente manual.

## Verificación y límites

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Antes de distribuir una versión, prueba el EXE en Windows con una carpeta de prueba y comprueba una instalación inicial, una actualización y el arranque con una cuenta válida. Las pruebas automatizadas no sustituyen una partida real ni verifican que todos los mods sean compatibles entre sí.

Los mods siguen descargándose desde GitHub; cada actualización de JAR aumenta el historial del repositorio. El ZIP personal no se usa como descarga del pack. Si el repositorio crece demasiado, puede migrarse la distribución a archivos de Releases o a una plataforma de modpacks sin cambiar el principio de verificación mediante hashes.
