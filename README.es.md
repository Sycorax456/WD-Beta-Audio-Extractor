# Watch Dogs 1 - Extractores de audio SPK/SBAO

[English](README.md) | **Español**

Herramientas no oficiales para extraer el audio de los archivos `.spk` / `.sbao` de *Watch Dogs*
(motor Disrupt de Ubisoft). Hay un extractor por plataforma porque el formato de audio es distinto:

| | Xbox 360 | PS4 |
|---|---|---|
| Carpeta | `xbox360/` | `ps4/` |
| Códec de audio | XMA2 | ATRAC9 |
| Orden de bytes | big-endian | little-endian (algunos `.spk` vienen comprimidos con LZMA) |
| Salida | `.xma` (RIFF con un bloque de formato XMA2) | `.wav` (con ffmpeg) o `.at9` |
| Herramientas | `wd_extractor.py` (consola)<br>`wd_extractor_gui.py` (interfaz gráfica) | `wd_ps4_extractor.py` (interfaz gráfica y consola) |
| Requisitos | Python 3.8+ (`customtkinter` para la interfaz) | Python 3.8+, ffmpeg con decodificador ATRAC9 (`customtkinter` para la interfaz) |

Cada herramienta también existe en inglés (mismo nombre con el sufijo `_en`).

## ¿Cuál necesito?

- Tus `.spk` son de una build de **Xbox 360** (audio XMA2, big-endian) -> `xbox360/`.
  Desarrollado con las builds del 21 de septiembre de 2013 y del 15 de enero de 2014 (prototipo).
- Tus `.spk` son de una build de **PS4** (audio ATRAC9, little-endian) -> `ps4/`.
- ¿No estás seguro? Corré la herramienta de PS4 sobre la carpeta: los archivos de Xbox 360 se
  reportan como `spk_big_endian`. En el sentido contrario, la herramienta de Xbox ignora en
  silencio los `.spk` de PS4 sin comprimir y marca como `no_es_spk` los comprimidos con LZMA.
  Si una corrida de Xbox no extrae nada, lo más probable es que tengas archivos de PS4.

## Instalación

```
pip install customtkinter      # solo para las interfaces gráficas
```

Solo PS4: instalá un [ffmpeg](https://ffmpeg.org/) reciente y verificá que tenga el decodificador
ATRAC9 (`ffmpeg -decoders | grep atrac9`). Se probó con ffmpeg 6.1.1. Sin ffmpeg la herramienta de
PS4 funciona igual, pero guarda `.at9` en lugar de `.wav`.

## Uso

Poné los `.spk` y sus `.sbao` en cualquier lugar dentro de una misma carpeta de entrada (se recorren
las subcarpetas). Cada sample con streaming se empareja con su `.sbao` por nombre.

**Xbox 360**

```
python xbox360/wd_extractor_gui.py
python xbox360/wd_extractor.py <carpeta_entrada> <carpeta_salida> --report reporte_fallos.txt
```

**PS4** (sin argumentos abre la interfaz gráfica)

```
python ps4/wd_ps4_extractor.py
python ps4/wd_ps4_extractor.py <carpeta_entrada> <carpeta_salida> --report reporte_fallos_ps4.txt
        [--ffmpeg RUTA_A_FFMPEG] [--keep-at9] [--at9-only]
```

- `--keep-at9`: conserva también los `.at9` junto a los WAV.
- `--at9-only`: no convierte, solo escribe `.at9`.
- `--ffmpeg`: usa un ffmpeg específico en lugar del que está en el `PATH`.

## Salida

| Herramienta | Nombre de archivo |
|---|---|
| Xbox 360 | `SMPR_<spk>_<id>.xma` (sample), `MLTR_<spk>_<id>_L<n>.xma` (uno por capa de un multi-track), `SEQR_<spk>_<id>.xma` |
| PS4 | `SMPR_<spk>_<id>.wav` (y `.at9` si se pide) |

`<spk>` es el nombre del `.spk` y `<id>` el ID del recurso dentro de él.

Los `.xma` de Xbox son archivos RIFF estándar con un bloque de formato XMA2. Convertilos con
`ffmpeg -i archivo.xma archivo.wav` o abrilos con vgmstream.

## Reporte de fallos

Cada corrida escribe un reporte `.txt`: un resumen agrupado por motivo y después una línea por
fallo, con el `.spk`, el ID del recurso y los detalles. Motivos frecuentes:

| Motivo | Significado |
|---|---|
| `sbao_no_encontrado` | El `.sbao` al que apunta el `.spk` no está en la carpeta de entrada (típico en volcados incompletos). |
| `spk_truncado`, `lectura_fuera_de_rango` | El archivo es más corto de lo que dicen sus propias tablas, o usa un layout que la herramienta no conoce. |
| `ident_desconocido`, `dobbsid_desconocido`, `wformattag_desconocido` (Xbox) | Un valor de la estructura que la herramienta todavía no conoce. El reporte imprime el valor para poder investigarlo. |
| `sbao_mas_corto_que_blockcount`, `sbao_mas_corto_que_stream` | El `.sbao` tiene menos datos de audio de los que anuncia el descriptor. |
| `atrac9_config_no_encontrada` (PS4) | El sample no parece ATRAC9 (otro código de códec) o su descriptor tiene otro layout. |
| `multitrack_ps4_no_soportado` (PS4) | Los recursos MultiTrack todavía no están soportados en PS4. |
| `spk_big_endian` (PS4) | Se le pasó un archivo de Xbox 360 a la herramienta de PS4. |

## Estado y limitaciones

**Xbox 360**
- Verificado con archivos reales: samples con streaming cuyo `.sbao` trae el encabezado de formato
  XMA2 (septiembre de 2013) y `.sbao` sin encabezado con el formato guardado en el `.spk` (prototipo
  de enero de 2014). Sobre un volcado de 33.224 archivos de la build de septiembre de 2013 se
  extrajeron 24.998 audios y 8.271 de los 8.288 fallos fueron `.sbao` ausentes del volcado.
- Implementado a partir de la lógica de los scripts originales de QuickBMS pero sin verificar con
  archivos reales: recursos multi-layer / MultiTrack, recursos SEQR directos y el magic `0x0228`
  del `.sbao`.
- Los tipos de recurso desconocidos se reportan, no se saltean en silencio.

**PS4**
- Verificado con archivos reales: `.spk` envueltos en LZMA, ATRAC9 mono a 48 kHz, samples con
  streaming cuyo `.sbao` contiene el stream completo, y alineación a 4 bytes de las entradas del `.spk`.
- Implementado pero sin verificar con archivos reales: samples cuyo audio está en una entrada local
  del `.spk`, y `.sbao` que contienen solo la continuación de un prefijo guardado en el `.spk`
  (se aceptan únicamente cuando los tamaños suman exactamente).
- No soportado: recursos MultiTrack y códecs distintos de ATRAC9. ATRAC9 multicanal sin probar.
- Los descriptores con DobbsID `0x8F3F8A20` solo apuntan a otro recurso (no tienen audio) y se
  omiten; la cantidad aparece en el reporte.

## Cómo funciona

- **SPK**: firma `02 4B 50 53`, cantidad de entradas, IDs de las entradas y después las entradas con
  su tamaño por delante. En PS4 las entradas pueden venir rellenadas a 4 bytes y algunos archivos están
  envueltos en LZMA (`00`, propiedades LZMA, tamaño de diccionario, stream crudo).
- **Descriptores**: una marca `0x17CE46D9` seguida de un DobbsID define el tipo:
  `0x93B1ECE5` SampleResource, `0xF2D3BBD4` MultiTrackResource, `0x8F3F8A20` envoltorio.
- **SBAO**: un encabezado de `0x1C` bytes (`02 2A ...`) seguido del stream. En las builds de Xbox de
  septiembre de 2013 el formato XMA2 va después del encabezado; en el prototipo de enero de 2014 el
  stream empieza enseguida y el formato está en el `.spk`.
- **PS4**: el descriptor del `.spk` contiene la palabra de configuración ATRAC9 (empieza con `0xFE`),
  que se valida contra la frecuencia de muestreo, los canales y el tamaño del stream antes de armar un
  `.at9` (RIFF, formato extensible con el GUID de ATRAC9) que decodifica ffmpeg.

## Estructura del repositorio

```
xbox360/   wd_extractor.py, wd_extractor_en.py, wd_extractor_gui.py, wd_extractor_gui_en.py
ps4/       wd_ps4_extractor.py, wd_ps4_extractor_en.py
README.md  README.es.md  requirements.txt
```

## Créditos y aviso

- El extractor de Xbox 360 reimplementa en Python la lógica de los scripts comunitarios de QuickBMS
  `spk2xma.bms` y `xma_sbao.bms`.
- Herramientas no oficiales hechas por fans, sin relación con Ubisoft. No incluyen datos del juego:
  necesitás tus propios archivos. Pensadas para investigación personal, preservación y modding.
