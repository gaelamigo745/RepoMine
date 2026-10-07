# Revisión de IDAS, Steam ’n’ Rails, Crafts & Additions y AeroPortals

Objetivo: Minecraft 1.21.1, NeoForge 21.1.233, Create 6.0.10, Aeronautics 1.3.0 y Sable 2.0.6. Todos los JAR nuevos se instalan en `mods/`, para cliente y servidor.

| Archivo | Versión | Fuente |
| --- | --- | --- |
| `idas-1.13.7+1.21.1-neoforge.jar` | 1.13.7+1.21.1-neoforge | https://cdn.modrinth.com/data/Z8OZShAU/versions/bpMwZSKf/idas-1.13.7%2B1.21.1-neoforge.jar |
| `createaddition-1.6.0.jar` | neoforge-1.21.1-1.6.0 | https://cdn.modrinth.com/data/kU1G12Nn/versions/qPr8V4G2/createaddition-1.6.0.jar |
| `railways-0.3.0-beta.2+neoforge-mc1.21.1.jar` | 0.3.0-beta.2+neoforge-mc1.21.1 | https://cdn.modrinth.com/data/L3Jv0QZI/versions/czVeSmZo/railways-0.3.0-beta.2%2Bneoforge-mc1.21.1.jar |
| `integrated_api-neoforge-1.21.1-1.8.2.jar` | 1.8.2 | https://cdn.modrinth.com/data/V6fKbpBN/versions/cc365ykf/integrated_api-neoforge-1.21.1-1.8.2.jar |
| `Zeta-1.1-40.jar` | 1.1-40 | https://cdn.modrinth.com/data/MVARlG2f/versions/9GjNW2Gf/Zeta-1.1-40.jar |
| `moonlight-1.21.1-3.7.1-neoforge.jar` | 1.21.1-3.7.1 | https://cdn.modrinth.com/data/twkfQtEc/versions/YfHkk7hg/moonlight-1.21.1-3.7.1-neoforge.jar |
| `Quark-4.1-486.jar` | 4.1-486 | https://cdn.modrinth.com/data/qnQsVE2z/versions/k4NCrqRq/Quark-4.1-486.jar |
| `supplementaries-1.21.1-3.8.9-neoforge.jar` | 1.21.1-3.8.9 | https://cdn.modrinth.com/data/fFEIiSDQ/versions/ZHJvgW8G/supplementaries-1.21.1-3.8.9-neoforge.jar |
| `aeroportals-1.21.1-1.3.3.jar` | 1.3.3 | https://www.curseforge.com/minecraft/mc-mods/create-aeroportals/files/8876165 |

IDAS 1.13.7 requiere Create, Quark, Supplementaries e Integrated API. Quark necesita Zeta y empaqueta Biolith 3.0.14; Supplementaries necesita Moonlight. Quark 4.1-487 exige NeoForge 21.1.252: se eligió 4.1-486, que acepta 21.1.230+. Supplementaries 3.9.9 exige 21.1.247: se eligió 3.8.9. Esta versión admite Farmer’s Delight 1.3.2 y Sodium 0.8.12 presentes en el pack. Las estructuras nuevas aparecen en terreno nuevo; no se elimina ni regenera terreno existente.

Crafts & Additions 1.6.0 es release: requiere Create >=6.0.7 y <6.1.0, NeoForge >=21.1.219 y publica mejoras de docking con Sable. Sus motores y alternadores se solapan con otros complementos eléctricos, pero no declaran incompatibilidad entre ellos.

Steam ’n’ Rails 0.3.0-beta.2 es un port NO oficial y beta. Requiere NeoForge 21.1.233+ y Create 6.0.7+. La rama 0.3.0 incluye la corrección de arranque para Create 6.0.10. No se eligió la release antigua 0.2.1. Falta verificar en juego todas las interacciones con los complementos del pack.

AeroPortals 1.3.3 transporta naves por portales del Nether y End sin Immersive Portals. Requiere Sable >=2.0.3 y <3.0. Las integraciones con otros mods dependen del soporte explícito del autor: no todos los portales están soportados. La versión incluye correcciones de pasajeros, swivel bearings y recuperación de transferencias. Probar una nave pequeña en una copia del mundo antes de transportar construcciones importantes.

FTB Library pasa de `modsServer/` a `mods/`: registra `ftblibrary:icon_item`, que necesita el cliente para conectarse. FTB Essentials continúa en `modsServer/`.

Se revisaron metadatos y dependencias de JAR internos. Los archivos de Modrinth se verificaron contra tamaño y SHA-512 publicados; AeroPortals se descargó del CDN oficial de CurseForge y se comprobó su estructura JAR. No se ejecutó Minecraft. No se recalcularon hashes SHA-256 del catálogo: `catalog_pending: true` impide instalar el catálogo desactualizado. Ejecutar `python generar_manifest.py --generate` y publicar el manifiesto actualizado antes de usar el launcher. Releases y código del launcher se conservan.

## Completar los JAR grandes

La conexión de publicación usada tiene un límite de 16 MiB por petición. IDAS, Steam ’n’ Rails, Quark y Supplementaries exceden ese límite al codificarse en Base64, por lo que esos cuatro binarios no se incluyen en este commit. Los demás JAR sí están en `mods/`. Sus fuentes oficiales, tamaños y SHA-512 están fijados en `docs/mods-descargas-adicionales.json`.

Desde la raíz, ejecuta `python descargar_mods_adicionales.py` para descargar/verificar esos cuatro JAR en `mods/`, y después `python generar_manifest.py --generate`. Publica los cuatro JAR y el manifiesto con tu herramienta habitual. El generador rechaza la generación si faltan o están dañados, evitando un catálogo parcial. El pack completo tiene 119 JAR: 98 comunes, 20 de cliente y 1 de servidor. Esta descarga no calcula ni publica los hashes SHA-256 del catálogo.
