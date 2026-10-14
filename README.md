# Watch Dogs 1 - SPK/SBAO audio extractors

**English** | [Español](README.es.md)

Unofficial tools to extract audio from the `.spk` / `.sbao` files of *Watch Dogs*
(Ubisoft's Disrupt engine). There is one extractor per platform because the audio
format is different:

| | Xbox 360 | PS4 |
|---|---|---|
| Folder | `xbox360/` | `ps4/` |
| Audio codec | XMA2 | ATRAC9 |
| Byte order | big-endian | little-endian (some `.spk` are LZMA-compressed) |
| Output | `.xma` (RIFF with an XMA2 format chunk) | `.wav` (through ffmpeg) or `.at9` |
| Tools | `wd_extractor_en.py` (command line)<br>`wd_extractor_gui_en.py` (GUI) | `wd_ps4_extractor_en.py` (GUI and command line) |
| Needs | Python 3.8+ (`customtkinter` for the GUI) | Python 3.8+, ffmpeg with an ATRAC9 decoder (`customtkinter` for the GUI) |

Every tool also exists in Spanish (same name without `_en`).

## Which one do I need?

- Your `.spk` files come from an **Xbox 360** build (XMA2 audio, big-endian) -> `xbox360/`.
  Developed with the September 21, 2013 and January 15, 2014 (prototype) builds.
- Your `.spk` files come from a **PS4** build (ATRAC9 audio, little-endian) -> `ps4/`.
- Not sure? Run the PS4 tool on the folder: Xbox 360 files are reported as `spk_big_endian`.
  The other way around, the Xbox tool silently ignores uncompressed PS4 `.spk` files and
  reports LZMA-compressed ones as `not_an_spk_file`. If an Xbox run extracts nothing,
  you probably have PS4 files.

## Install

```
pip install customtkinter      # only for the GUIs
```

PS4 only: install a recent [ffmpeg](https://ffmpeg.org/) and make sure it has the ATRAC9
decoder (`ffmpeg -decoders | grep atrac9`). It was tested with ffmpeg 6.1.1. Without ffmpeg
the PS4 tool still works, but it writes `.at9` files instead of `.wav`.

## Usage

Put the `.spk` files and their `.sbao` files anywhere under one input folder (subfolders are
scanned). Each streamed sample is paired with its `.sbao` by name.

**Xbox 360**

```
python xbox360/wd_extractor_gui_en.py
python xbox360/wd_extractor_en.py <input_folder> <output_folder> --report failure_report.txt
```

**PS4** (no arguments opens the GUI)

```
python ps4/wd_ps4_extractor_en.py
python ps4/wd_ps4_extractor_en.py <input_folder> <output_folder> --report failure_report_ps4.txt
        [--ffmpeg PATH_TO_FFMPEG] [--keep-at9] [--at9-only]
```

- `--keep-at9`: also keep the `.at9` files next to the WAVs.
- `--at9-only`: do not convert, only write `.at9`.
- `--ffmpeg`: use a specific ffmpeg instead of the one in `PATH`.

## Output

| Tool | File name |
|---|---|
| Xbox 360 | `SMPR_<spk>_<id>.xma` (sample), `MLTR_<spk>_<id>_L<n>.xma` (one per layer of a multi-track), `SEQR_<spk>_<id>.xma` |
| PS4 | `SMPR_<spk>_<id>.wav` (and `.at9` if requested) |

`<spk>` is the `.spk` file name and `<id>` the resource ID inside it.

The Xbox `.xma` files are standard RIFF files with an XMA2 format chunk. Convert them with
`ffmpeg -i file.xma file.wav` or open them with vgmstream.

## Failure report

Every run writes a `.txt` report: a summary grouped by reason, then one line per failure with
the `.spk`, the resource ID and the details. Common reasons:

| Reason | Meaning |
|---|---|
| `sbao_not_found` | The `.sbao` the `.spk` points to is not in the input folder (typical with incomplete dumps). |
| `spk_truncated`, `read_out_of_range` | The file is shorter than its own tables say, or it uses a layout the tool does not know. |
| `unknown_ident`, `unknown_dobbsid`, `unknown_wformattag` (Xbox) | A structure value the tool does not know yet. The report prints the value so it can be investigated. |
| `sbao_shorter_than_blockcount`, `sbao_shorter_than_stream` | The `.sbao` has less audio data than the descriptor announces. |
| `atrac9_config_not_found` (PS4) | The sample does not look like ATRAC9 (another codec ID) or its descriptor has a different layout. |
| `ps4_multitrack_unsupported` (PS4) | MultiTrack resources are not supported on PS4 yet. |
| `spk_big_endian` (PS4) | An Xbox 360 file was given to the PS4 tool. |

## Status and limitations

**Xbox 360**
- Verified with real files: streamed samples whose `.sbao` carries the XMA2 format header
  (September 2013) and headerless `.sbao` files with the format stored in the `.spk`
  (January 2014 prototype). On a 33,224-file dump of the September 2013 build, 24,998 audio files
  were extracted and 8,271 of the 8,288 failures were `.sbao` files missing from the dump.
- Implemented from the logic of the original QuickBMS scripts but not verified against real
  files: multi-layer / MultiTrack resources, direct SEQR resources and the `0x0228` `.sbao` magic.
- Unknown resource types are reported, not skipped silently.

**PS4**
- Verified with real files: LZMA-wrapped `.spk`, mono 48 kHz ATRAC9, streamed samples whose
  `.sbao` holds the whole stream, and 4-byte entry alignment inside the `.spk`.
- Implemented but not verified with real files: samples whose audio sits in a local `.spk`
  entry, and `.sbao` files holding only the continuation of a prefix stored in the `.spk`
  (accepted only when the sizes add up exactly).
- Not supported: MultiTrack resources, codecs other than ATRAC9. Multichannel ATRAC9 is untested.
- Descriptors with DobbsID `0x8F3F8A20` only point to another resource (no audio) and are
  skipped; the count appears in the report.

## How it works

- **SPK**: signature `02 4B 50 53`, entry count, entry IDs, then size-prefixed entries.
  On PS4, entries can be padded to 4 bytes and some files are LZMA-wrapped
  (`00`, LZMA properties, dictionary size, raw stream).
- **Descriptors**: a marker `0x17CE46D9` followed by a DobbsID selects the type:
  `0x93B1ECE5` SampleResource, `0xF2D3BBD4` MultiTrackResource, `0x8F3F8A20` wrapper.
- **SBAO**: a `0x1C`-byte header (`02 2A ...`) followed by the stream. In the September 2013
  Xbox builds the XMA2 format follows the header; in the January 2014 prototype the stream
  starts right away and the format is in the `.spk`.
- **PS4**: the `.spk` descriptor holds the ATRAC9 configuration word (starts with `0xFE`),
  which is validated against the sample rate, channels and stream size before an `.at9`
  (RIFF, extensible format with the ATRAC9 GUID) is built and decoded by ffmpeg.

## Repository layout

```
xbox360/   wd_extractor.py, wd_extractor_en.py, wd_extractor_gui.py, wd_extractor_gui_en.py
ps4/       wd_ps4_extractor.py, wd_ps4_extractor_en.py
README.md  README.es.md  requirements.txt
```

## Credits and disclaimer

- The Xbox 360 extractor re-implements in Python the logic of the community QuickBMS scripts
  `spk2xma.bms` and `xma_sbao.bms`.
- Unofficial fan tools, not affiliated with Ubisoft. They contain no game data: you need your own
  game files. Intended for personal research, preservation and modding.
