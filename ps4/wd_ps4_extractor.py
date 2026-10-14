#!/usr/bin/env python3
"""
Watch Dogs 1 (build PS4) - Extractor de audio SPK/SBAO (ATRAC9) -> WAV

Que hace:
  - Recorre una carpeta buscando .spk y empareja cada sample "streameado" con su
    .sbao por nombre (dataRef en hex, 8 digitos).
  - Descomprime los .spk envueltos en LZMA (no empiezan con la firma SPK).
  - Lee el formato little-endian de PS4 y arma un .at9 (RIFF ATRAC9) en memoria.
  - Convierte el .at9 a WAV usando ffmpeg (necesita un ffmpeg reciente con
    decoder ATRAC9). Sin ffmpeg, deja los .at9.
  - Escribe un reporte .txt con todo lo que no pudo resolver y el motivo.

Uso:
  python wd_ps4_extractor.py                       -> abre la interfaz grafica
  python wd_ps4_extractor.py <entrada> <salida> [--report r.txt] [--ffmpeg ruta]
                                                   [--keep-at9] [--at9-only]
Para las builds de Xbox 360 / prototipo (XMA, big-endian) usar wd_extractor.py.
"""

import os
import sys
import struct
import lzma
import shutil
import argparse
import subprocess
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# ============================================================================
# CONSTANTES
# ============================================================================

SPK_MAGIC = b"\x02KPS"                       # 02 4b 50 53
DOBBS_TYPE_MARKER = 0x17CE46D9              # marca de "descriptor" tras unk6
DOBBS_SAMPLE = 0x93B1ECE5                   # SampleResourceDescriptor
DOBBS_MULTITRACK = 0xF2D3BBD4               # MultiTrackResourceDescriptor
DOBBS_WRAPPER = 0x8F3F8A20                  # envoltorio que apunta a otro recurso (sin audio)

SBAO_DATA_OFFSET = 0x1C                     # cabecera del .sbao antes del stream crudo
AT9_GUID = bytes.fromhex("D242E147BA368D4D88FC61654F8C836C")
AT9_SAMPLE_RATES = [11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000,
                    44100, 48000, 64000, 88200, 96000, 128000, 176400, 192000]
AT9_FRAME_LOG2 = [6, 6, 7, 7, 7, 8, 8, 8, 6, 6, 7, 7, 7, 8, 8, 8]
AT9_CHANNELS_BY_CFG = {0: 1, 1: 2, 2: 2, 3: 6, 4: 7, 5: 8}
CHANNEL_MASKS = {1: 0x4, 2: 0x3, 6: 0x3F, 8: 0x63F}

# ============================================================================
# LECTURA BINARIA
# ============================================================================

class Reader:
    __slots__ = ("buf", "pos", "e")

    def __init__(self, buf, pos=0, endian="<"):
        self.buf = buf
        self.pos = pos
        self.e = endian

    def skip(self, n):
        self.pos += n

    def seek(self, pos):
        self.pos = pos

    def raw(self, n):
        if n < 0 or self.pos + n > len(self.buf):
            raise EOFError(f"lectura fuera de rango en offset {self.pos:#x} (+{n})")
        b = self.buf[self.pos:self.pos + n]
        self.pos += n
        return b

    def u32(self):
        return struct.unpack(self.e + "I", self.raw(4))[0]


class SpkError(Exception):
    def __init__(self, reason, detail=""):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


def _fail(spk, entry_id, tipo, motivo, detalle="", sbao=None):
    d = {"spk": spk, "id": entry_id, "tipo": tipo, "motivo": motivo, "detalle": detalle}
    if sbao:
        d["sbao"] = sbao
    return d

# ============================================================================
# SPK: carga (LZMA opcional) y tabla de entradas
# ============================================================================

def lzma_unwrap(raw):
    """Formato visto en PS4: [1 byte 00][props LZMA][dict_size LE x4][stream crudo]."""
    if len(raw) < 8 or raw[0] != 0x00:
        raise SpkError("spk_formato_desconocido", f"primeros bytes: {raw[:16].hex()}")
    props = raw[1]
    if props >= 9 * 5 * 5:
        raise SpkError("spk_formato_desconocido", f"primeros bytes: {raw[:16].hex()}")
    lc, rest = props % 9, props // 9
    lp, pb = rest % 5, rest // 5
    dict_size = struct.unpack_from("<I", raw, 2)[0]
    if not (4096 <= dict_size <= (1 << 30)):
        raise SpkError("spk_formato_desconocido", f"primeros bytes: {raw[:16].hex()}")
    filters = [{"id": lzma.FILTER_LZMA1, "dict_size": dict_size, "lc": lc, "lp": lp, "pb": pb}]
    try:
        dec = lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=filters)
        out = dec.decompress(raw[6:])
    except (lzma.LZMAError, ValueError) as e:
        raise SpkError("lzma_fallo", str(e))
    if out[:4] != SPK_MAGIC:
        raise SpkError("lzma_sin_firma_spk", f"salida: {out[:16].hex()}")
    return out


def _walk_entries(buf, align):
    r = Reader(buf, 4, "<")
    count = r.u32()
    if count <= 0 or count > 100000:
        raise SpkError("count_invalido", f"count={count}")
    ids = [r.u32() for _ in range(count)]
    entries, end = [], r.pos
    for k in range(count):
        size = r.u32()
        off = r.pos
        entries.append({"id": ids[k], "offset": off, "size": size})
        end = off + size
        r.seek((end + align - 1) // align * align if align > 1 else end)
    return entries, end


def read_spk_entries(buf):
    """Devuelve (entradas, fin_de_datos). La cabecera del SPK siempre es little-endian.
    Las entradas pueden venir alineadas a 4 bytes (1 a 3 bytes de relleno tras un dato de
    tamano impar): se prueba sin alineacion y alineando a 4, y se elige la variante que
    termina justo al final del archivo."""
    valid, problems = [], []
    for align in (1, 4):
        try:
            entries, end = _walk_entries(buf, align)
        except EOFError as ex:
            problems.append(f"alineado a {align}: tabla fuera de los datos ({ex})")
            continue
        if end > len(buf):
            problems.append(f"alineado a {align}: esperado {end} bytes, hay {len(buf)}")
            continue
        valid.append((align, entries, end))
    exact = [v for v in valid if len(buf) - v[2] <= 3]
    chosen = (exact or valid or [None])[0]
    if chosen is None:
        raise SpkError("spk_truncado", " ; ".join(problems))
    return chosen[1], chosen[2]


def load_spk(raw):
    compressed = raw[:4] != SPK_MAGIC
    buf = lzma_unwrap(raw) if compressed else raw
    try:
        entries, end = read_spk_entries(buf)
    except SpkError as ex:
        extra = f"comprimido={compressed} tam_archivo={len(raw)} tam_datos={len(buf)}"
        ex.detail = f"{ex.detail} | {extra}" if ex.detail else extra
        raise
    return buf[:end], entries, compressed


def detect_endian(buf, entries):
    """Mira el campo 'type' de cada entrada en ambos ordenes de bytes."""
    for e in entries:
        off = e["offset"] + 28
        if off + 4 > len(buf):
            continue
        chunk = buf[off:off + 4]
        for end in ("<", ">"):
            t = struct.unpack(end + "I", chunk)[0]
            if t == DOBBS_TYPE_MARKER or (t >> 16) == 0x0166:
                return end
    return None

# ============================================================================
# ATRAC9
# ============================================================================

def parse_at9_config(cfg):
    if len(cfg) != 4 or cfg[0] != 0xFE:
        return None
    v = int.from_bytes(cfg[1:4], "big")
    sr_idx = (v >> 20) & 0xF
    ch_cfg = (v >> 17) & 0x7
    valid_bit = (v >> 16) & 1
    frame_bytes = ((v >> 5) & 0x7FF) + 1
    sf_idx = (v >> 3) & 0x3
    return {
        "sr_idx": sr_idx, "rate": AT9_SAMPLE_RATES[sr_idx], "ch_cfg": ch_cfg,
        "valid_bit": valid_bit, "frame_bytes": frame_bytes, "sf_idx": sf_idx,
        "superframe": frame_bytes << sf_idx,
        "samples_per_block": (1 << AT9_FRAME_LOG2[sr_idx]) << sf_idx,
    }


def find_at9_config(buf, start, end, rate, channels, stream_size):
    """Busca la palabra de configuracion (empieza con 0xFE) dentro del descriptor y la
    valida contra los datos del propio descriptor (frecuencia, canales, tamano)."""
    for p in range(start, max(start, end - 3)):
        if buf[p] != 0xFE:
            continue
        c = parse_at9_config(buf[p:p + 4])
        if c is None or c["valid_bit"] != 0:
            continue
        if c["rate"] != rate:
            continue
        if AT9_CHANNELS_BY_CFG.get(c["ch_cfg"]) != channels:
            continue
        if stream_size and stream_size % c["superframe"] != 0:
            continue
        return buf[p:p + 4], p, c
    return None


def build_at9(cfg, channels, data, version=2):
    c = parse_at9_config(cfg)
    avg = c["superframe"] * c["rate"] // c["samples_per_block"]
    fmt = struct.pack("<HHIIHHHHI", 0xFFFE, channels, c["rate"], avg, c["superframe"],
                      0, 34, c["samples_per_block"], CHANNEL_MASKS.get(channels, 0))
    fmt += AT9_GUID + struct.pack("<I", version) + cfg + struct.pack("<I", 0)
    body = (b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt +
            b"data" + struct.pack("<I", len(data)) + data)
    return b"RIFF" + struct.pack("<I", len(body)) + body

# ============================================================================
# DESCRIPTOR DE SAMPLE (little-endian, PS4)
# ============================================================================

def _blob(buf, e):
    return buf[e["offset"]:e["offset"] + e["size"]]


def _resumen_entradas(others, limit=6):
    txt = ",".join(f"{x['id']:08x}:{x['size']}" for x in others[:limit])
    return txt + (",..." if len(others) > limit else "")


def parse_sample(r, entry, entries, buf, sbao_index, basename, items, fails):
    entry_id = f"{entry['id']:08x}"
    entry_end = entry["offset"] + entry["size"]
    e = r.e
    others = [x for x in entries if x is not entry]

    count_emitter = r.u32()
    r.skip(count_emitter * 4)
    block40 = r.raw(40)
    block16 = r.raw(16)
    bstream = r.u32()

    stream_size = struct.unpack_from(e + "I", block40, 12)[0]
    codec, channels, rate = struct.unpack_from(e + "III", block40, 28)

    if bstream != 0:
        r.skip(16)
        data_ref = r.u32()
        r.u32()
    else:
        data_ref = r.u32()

    strlen = r.u32()
    mod = strlen % 4
    r.skip((strlen + (4 - mod)) if mod else strlen)
    r.skip(r.u32() * 4)
    r.skip(36)
    region_start = r.pos

    tipo = "SampleResourceDescriptor (streamed)" if bstream else "SampleResourceDescriptor (local)"
    found = find_at9_config(buf, region_start, entry_end, rate, channels, stream_size)
    if found is None:
        fails.append(_fail(basename, entry_id, tipo, "atrac9_config_no_encontrada",
                            f"codec={codec} canales={channels} rate={rate} stream={stream_size}"))
        return
    cfg, cfg_pos, cinfo = found

    # datos inline que algunos descriptores traen tras la config (tag 0x0f)
    inline = None
    try:
        rr = Reader(buf, cfg_pos + 4, e)
        if rr.u32() == 0x0F and rr.u32() == 1:
            size = rr.u32()
            rr.u32()
            if 0 < size <= entry_end - rr.pos:
                inline = rr.raw(size)
    except EOFError:
        inline = None
    n_inline = len(inline) if inline else 0

    data, sbao_name, avail = None, None, None

    # 1) el descriptor trae el stream completo
    if inline is not None and len(inline) == stream_size:
        data = inline

    # 2) entrada local referenciada por dataRef: cabecera de 0x1C bytes + stream (como un .sbao)
    local = next((x for x in others if x["id"] == data_ref), None)
    if data is None and local is not None:
        blob = _blob(buf, local)
        if blob[:2] == b"\x02\x2a" and len(blob) - SBAO_DATA_OFFSET >= stream_size:
            data = blob[SBAO_DATA_OFFSET:SBAO_DATA_OFFSET + stream_size]

    # 3) .sbao externo (completo, o solo la continuacion de un prefijo guardado en el .spk)
    if data is None and bstream != 0:
        sbao_path = sbao_index.get(f"{data_ref:08x}.sbao")
        if sbao_path is None:
            fails.append(_fail(basename, entry_id, tipo, "sbao_no_encontrado",
                                f"dataRef={data_ref:08x}.sbao"))
            return
        sbao_name = sbao_path.name
        try:
            sbuf = sbao_path.read_bytes()
        except OSError as ex:
            fails.append(_fail(basename, entry_id, tipo, "sbao_ilegible", str(ex), sbao_name))
            return
        if sbuf[:2] != b"\x02\x2a":
            fails.append(_fail(basename, entry_id, tipo, "magic_sbao_desconocido",
                                f"primeros bytes: {sbuf[:8].hex()}", sbao_name))
            return
        sbao_data = sbuf[SBAO_DATA_OFFSET:]
        avail = len(sbao_data)
        if avail >= stream_size:
            data = sbao_data[:stream_size]
        else:
            # el .sbao trae solo el resto: probar prefijos guardados localmente, exigiendo
            # que prefijo + .sbao den exactamente el tamano del stream
            prefixes = ([inline] if inline else []) + [
                _blob(buf, x)[SBAO_DATA_OFFSET:] for x in others if x["size"] > SBAO_DATA_OFFSET]
            for pre in prefixes:
                if len(pre) + avail == stream_size:
                    data = pre + sbao_data
                    break

    if data is None:
        if avail is not None:
            fails.append(_fail(basename, entry_id, tipo, "sbao_mas_corto_que_stream",
                                f"esperado={stream_size} disponible={avail} inline={n_inline} "
                                f"entradas_locales={_resumen_entradas(others)}", sbao_name))
        else:
            extra = ""
            if local is not None:
                extra = f" local_tam={local['size']} local_inicio={_blob(buf, local)[:8].hex()}"
            fails.append(_fail(basename, entry_id, tipo, "datos_locales_no_encontrados",
                                f"stream={stream_size} inline={n_inline} dataRef={data_ref:08x} "
                                f"entrada_local={'si' if local else 'no'}{extra} "
                                f"entradas={_resumen_entradas(others)}"))
        return

    items.append((f"SMPR_{basename}_{entry_id}", build_at9(cfg, channels, data)))


def process_spk(path, sbao_index):
    """Devuelve (items, fails, wrappers). items = [(nombre_base, bytes_at9)]."""
    items, fails, wrappers = [], [], 0
    basename = path.stem
    try:
        raw = path.read_bytes()
        buf, entries, _compressed = load_spk(raw)
    except SpkError as ex:
        return items, [_fail(basename, "-", "SPK", ex.reason, ex.detail)], wrappers
    except OSError as ex:
        return items, [_fail(basename, "-", "SPK", "archivo_ilegible", str(ex))], wrappers

    endian = detect_endian(buf, entries)
    if endian is None:
        return items, fails, wrappers           # solo eventos/contenedores, sin audio
    if endian == ">":
        fails.append(_fail(basename, "-", "SPK", "spk_big_endian",
                            "build Xbox 360/prototipo: usar wd_extractor.py"))
        return items, fails, wrappers

    for entry in entries:
        try:
            r = Reader(buf, entry["offset"], endian)
            r.skip(24)
            if r.u32() == 0:
                continue
            t = r.u32()
            if (t >> 16) == 0x0166:
                fails.append(_fail(basename, f"{entry['id']:08x}", "SEQR (XMA directo)",
                                    "xma_en_spk_little_endian", "no soportado en este script"))
                continue
            if t != DOBBS_TYPE_MARKER:
                continue                         # evento / contenedor
            r.skip(44)
            dobbs = r.u32()
            if dobbs == DOBBS_SAMPLE:
                parse_sample(r, entry, entries, buf, sbao_index, basename, items, fails)
            elif dobbs == DOBBS_MULTITRACK:
                fails.append(_fail(basename, f"{entry['id']:08x}",
                                    "MultiTrackResourceDescriptor", "multitrack_ps4_no_soportado",
                                    "sin muestra para estudiar el formato"))
            elif dobbs == DOBBS_WRAPPER:
                wrappers += 1
            else:
                fails.append(_fail(basename, f"{entry['id']:08x}", "descriptor",
                                    "dobbsid_desconocido", f"DobbsID={dobbs:#010x}"))
        except EOFError as ex:
            fails.append(_fail(basename, f"{entry['id']:08x}", "-",
                                "lectura_fuera_de_rango", str(ex)))
        except Exception as ex:
            fails.append(_fail(basename, f"{entry['id']:08x}", "-",
                                "excepcion_no_controlada", repr(ex)))
    return items, fails, wrappers

# ============================================================================
# FFMPEG
# ============================================================================

def _popen_flags():
    return {"creationflags": 0x08000000} if os.name == "nt" else {}


def resolve_ffmpeg(user_path=None):
    """Devuelve (ruta, None) si ffmpeg sirve para ATRAC9, o (None, mensaje)."""
    cand = user_path.strip() if user_path and user_path.strip() else shutil.which("ffmpeg")
    if not cand:
        return None, "no se encontro ffmpeg (ni en el PATH ni en la ruta indicada)"
    try:
        r = subprocess.run([cand, "-hide_banner", "-decoders"], capture_output=True,
                           text=True, timeout=30, **_popen_flags())
    except (OSError, subprocess.SubprocessError) as ex:
        return None, f"no se pudo ejecutar ffmpeg ({ex})"
    if "atrac9" not in r.stdout.lower():
        return None, "ese ffmpeg no trae decoder ATRAC9 (hace falta una version reciente)"
    return cand, None


def at9_to_wav(ffmpeg, at9_bytes, wav_path):
    r = subprocess.run([ffmpeg, "-y", "-v", "error", "-i", "pipe:0", "-c:a", "pcm_s16le",
                        str(wav_path)], input=at9_bytes, capture_output=True, **_popen_flags())
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode("utf-8", "ignore").strip()[:300] or "ffmpeg fallo")

# ============================================================================
# PROCESO COMPLETO + REPORTE
# ============================================================================

def build_sbao_index(root):
    return {p.name.lower(): p for p in root.rglob("*.sbao")}


def write_report(report_path, spk_count, total_ok, all_fails, wrappers, mode_note):
    report_path = Path(report_path)
    with report_path.open("w", encoding="utf-8") as f:
        f.write(f"Reporte de extraccion PS4 - {spk_count} archivos .spk procesados\n")
        f.write(f"Audios extraidos correctamente: {total_ok}\n")
        f.write(f"Entradas que fallaron:          {len(all_fails)}\n")
        f.write(f"Descriptores sin audio omitidos (DobbsID {DOBBS_WRAPPER:#010x}): {wrappers}\n")
        if mode_note:
            f.write(f"Nota: {mode_note}\n")
        f.write("=" * 70 + "\n\n")
        by_reason = {}
        for fl in all_fails:
            by_reason.setdefault(fl["motivo"], []).append(fl)
        f.write("RESUMEN POR MOTIVO\n" + "-" * 70 + "\n")
        for reason, lst in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
            f.write(f"  {reason}: {len(lst)} casos\n")
        f.write("\n" + "=" * 70 + "\n\nDETALLE\n" + "-" * 70 + "\n")
        for fl in all_fails:
            f.write(f"[{fl['spk']}] id={fl['id']}  tipo={fl['tipo']}  motivo={fl['motivo']}"
                    + (f"  sbao={fl['sbao']}" if "sbao" in fl else "")
                    + (f"  detalle={fl['detalle']}" if fl.get("detalle") else "") + "\n")
    return report_path


def run_extraction(input_dir, output_dir, report_path, ffmpeg_path=None,
                   keep_at9=False, at9_only=False,
                   log_cb=print, progress_cb=None, should_stop=None):
    in_dir, out_dir = Path(input_dir), Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    ffmpeg, mode_note = None, ""
    if not at9_only:
        ffmpeg, why = resolve_ffmpeg(ffmpeg_path)
        if ffmpeg is None:
            at9_only = True
            mode_note = f"se guardaron .at9 en lugar de WAV: {why}"
            log_cb(f"AVISO: {why}. Se van a guardar solo los .at9.")
        else:
            log_cb(f"ffmpeg: {ffmpeg}")

    log_cb("Indexando archivos .sbao...")
    sbao_index = build_sbao_index(in_dir)
    log_cb(f"  {len(sbao_index)} .sbao encontrados")
    spk_files = sorted(in_dir.rglob("*.spk"))
    log_cb(f"Procesando {len(spk_files)} archivos .spk...")

    def work(spk_path):
        if should_stop and should_stop():
            return 0, [], 0, True
        try:
            items, fails, wrappers = process_spk(spk_path, sbao_index)
        except Exception as ex:
            return 0, [_fail(spk_path.stem, "-", "SPK", "excepcion_no_controlada",
                              repr(ex))], 0, False
        ok = 0
        for name, at9 in items:
            try:
                if at9_only or keep_at9:
                    (out_dir / f"{name}.at9").write_bytes(at9)
                if not at9_only:
                    at9_to_wav(ffmpeg, at9, out_dir / f"{name}.wav")
                ok += 1
            except Exception as ex:
                fails.append(_fail(spk_path.stem, name.rsplit("_", 1)[-1], "conversion",
                                    "conversion_ffmpeg_fallida", str(ex)))
        return ok, fails, wrappers, False

    total_ok, wrappers_total, done, cancelled = 0, 0, 0, False
    all_fails = []
    workers = 4 if at9_only else min(8, (os.cpu_count() or 4))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(work, p) for p in spk_files]
        for fut in as_completed(futures):
            ok, fails, wr, was_cancelled = fut.result()
            cancelled = cancelled or was_cancelled
            total_ok += ok
            wrappers_total += wr
            all_fails.extend(fails)
            done += 1
            if progress_cb:
                progress_cb(done, len(spk_files), total_ok, len(all_fails))

    if cancelled:
        log_cb("Cancelado por el usuario.")
    all_fails.sort(key=lambda d: (d["spk"], d["id"]))
    report = write_report(report_path, len(spk_files), total_ok, all_fails,
                          wrappers_total, mode_note)
    log_cb(f"Listo. Extraidos: {total_ok}  Fallos: {len(all_fails)}")
    log_cb(f"Reporte: {report.resolve()}")
    return {"spk_count": len(spk_files), "total_ok": total_ok,
            "total_fail": len(all_fails), "report_path": report, "mode_note": mode_note}

# ============================================================================
# GUI (CustomTkinter) - se importa solo si se abre la interfaz
# ============================================================================

def launch_gui():
    import threading
    import queue
    import customtkinter as ctk
    from tkinter import filedialog, messagebox

    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")

    class PathRow(ctk.CTkFrame):
        def __init__(self, master, label_text, mode="dir", default_name=None,
                     placeholder=None, **kwargs):
            super().__init__(master, fg_color="transparent", **kwargs)
            self.mode, self.default_name = mode, default_name
            self.grid_columnconfigure(1, weight=1)
            ctk.CTkLabel(self, text=label_text, width=200, anchor="w").grid(
                row=0, column=0, padx=(0, 8), pady=4, sticky="w")
            self.entry = (ctk.CTkEntry(self, placeholder_text=placeholder)
                          if placeholder else ctk.CTkEntry(self))
            self.entry.grid(row=0, column=1, padx=(0, 8), pady=4, sticky="ew")
            ctk.CTkButton(self, text="Buscar...", width=90, command=self._browse).grid(
                row=0, column=2, pady=4, sticky="e")

        def _browse(self):
            if self.mode == "dir":
                path = filedialog.askdirectory(title="Elegi una carpeta")
            elif self.mode == "open":
                path = filedialog.askopenfilename(title="Elegi el ejecutable de ffmpeg")
            else:
                path = filedialog.asksaveasfilename(
                    title="Guardar reporte como...", defaultextension=".txt",
                    initialfile=self.default_name or "reporte.txt",
                    filetypes=[("Texto", "*.txt"), ("Todos los archivos", "*.*")])
            if path:
                self.set(path)

        def get(self):
            return self.entry.get().strip()

        def set(self, value):
            self.entry.delete(0, "end")
            self.entry.insert(0, value)

    class App(ctk.CTk):
        def __init__(self):
            super().__init__()
            self.title("Watch Dogs 1 (PS4) - Extractor SPK/SBAO (ATRAC9 a WAV)")
            self.geometry("820x640")
            self.minsize(640, 480)
            self.grid_columnconfigure(0, weight=1)
            self.grid_rowconfigure(3, weight=1)

            self.msg_queue = queue.Queue()
            self._stop_requested = False

            paths = ctk.CTkFrame(self)
            paths.grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 8))
            paths.grid_columnconfigure(0, weight=1)
            self.row_input = PathRow(paths, "Carpeta con .spk/.sbao:")
            self.row_input.grid(row=0, column=0, sticky="ew", pady=2)
            self.row_output = PathRow(paths, "Carpeta de salida:")
            self.row_output.grid(row=1, column=0, sticky="ew", pady=2)
            self.row_report = PathRow(paths, "Reporte de fallos (.txt):", mode="save",
                                      default_name="reporte_fallos_ps4.txt")
            self.row_report.grid(row=2, column=0, sticky="ew", pady=2)
            self.row_ffmpeg = PathRow(paths, "ffmpeg (opcional):", mode="open",
                                      placeholder="vacio = buscar ffmpeg en el PATH")
            self.row_ffmpeg.grid(row=3, column=0, sticky="ew", pady=2)

            opts = ctk.CTkFrame(self, fg_color="transparent")
            opts.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 4))
            self.keep_var = ctk.BooleanVar(value=False)
            self.at9_var = ctk.BooleanVar(value=False)
            ctk.CTkCheckBox(opts, text="Conservar tambien los .at9",
                            variable=self.keep_var).grid(row=0, column=0, padx=(0, 24))
            ctk.CTkCheckBox(opts, text="Solo .at9 (no convertir a WAV)",
                            variable=self.at9_var).grid(row=0, column=1)

            controls = ctk.CTkFrame(self, fg_color="transparent")
            controls.grid(row=2, column=0, sticky="ew", padx=16, pady=4)
            controls.grid_columnconfigure(2, weight=1)
            self.start_btn = ctk.CTkButton(controls, text="Iniciar extraccion",
                                           command=self.on_start)
            self.start_btn.grid(row=0, column=0, padx=(0, 8))
            self.cancel_btn = ctk.CTkButton(controls, text="Cancelar", fg_color="#8B2E2E",
                                            hover_color="#6E2424", command=self.on_cancel,
                                            state="disabled")
            self.cancel_btn.grid(row=0, column=1, padx=(0, 8))
            self.status_label = ctk.CTkLabel(controls, text="Listo.", anchor="w")
            self.status_label.grid(row=0, column=2, sticky="ew")
            self.progress_bar = ctk.CTkProgressBar(controls)
            self.progress_bar.set(0)
            self.progress_bar.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(8, 0))

            log_frame = ctk.CTkFrame(self)
            log_frame.grid(row=3, column=0, sticky="nsew", padx=16, pady=(4, 16))
            log_frame.grid_columnconfigure(0, weight=1)
            log_frame.grid_rowconfigure(0, weight=1)
            self.log_box = ctk.CTkTextbox(log_frame, wrap="word", state="disabled")
            self.log_box.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)

            self.after(100, self._poll_queue)

        def _log(self, text):
            self.msg_queue.put(("log", text))

        def _poll_queue(self):
            try:
                while True:
                    kind, payload = self.msg_queue.get_nowait()
                    if kind == "log":
                        self.log_box.configure(state="normal")
                        self.log_box.insert("end", payload + "\n")
                        self.log_box.see("end")
                        self.log_box.configure(state="disabled")
                    elif kind == "progress":
                        done, total, ok, fail = payload
                        self.progress_bar.set(done / total if total else 0)
                        self.status_label.configure(
                            text=f"{done}/{total}  -  extraidos: {ok}  fallos: {fail}")
                    elif kind == "finished":
                        self._on_finished(payload)
            except queue.Empty:
                pass
            self.after(100, self._poll_queue)

        def _on_finished(self, res):
            self.start_btn.configure(state="normal")
            self.cancel_btn.configure(state="disabled")
            if isinstance(res, Exception):
                messagebox.showerror("Error", f"Ocurrio un error:\n{res}")
                self.status_label.configure(text="Termino con error.")
                return
            self.status_label.configure(
                text=f"Listo: {res['total_ok']} extraidos, {res['total_fail']} fallos "
                     f"de {res['spk_count']} .spk")
            extra = f"\n\nNota: {res['mode_note']}" if res["mode_note"] else ""
            messagebox.showinfo(
                "Extraccion terminada",
                f".spk procesados: {res['spk_count']}\nAudios extraidos: {res['total_ok']}\n"
                f"Fallos: {res['total_fail']}\n\nReporte: {res['report_path']}{extra}")

        def on_start(self):
            in_dir, out_dir = self.row_input.get(), self.row_output.get()
            report = self.row_report.get()
            if not in_dir or not os.path.isdir(in_dir):
                messagebox.showwarning("Falta la carpeta de entrada",
                                       "Elegi una carpeta valida con .spk/.sbao.")
                return
            if not out_dir:
                messagebox.showwarning("Falta la carpeta de salida",
                                       "Elegi donde guardar los audios extraidos.")
                return
            if not report:
                report = str(Path(out_dir) / "reporte_fallos_ps4.txt")
                self.row_report.set(report)

            self.log_box.configure(state="normal")
            self.log_box.delete("1.0", "end")
            self.log_box.configure(state="disabled")
            self.progress_bar.set(0)
            self.status_label.configure(text="Iniciando...")
            self.start_btn.configure(state="disabled")
            self.cancel_btn.configure(state="normal")
            self._stop_requested = False

            args = (in_dir, out_dir, report, self.row_ffmpeg.get() or None,
                    bool(self.keep_var.get()), bool(self.at9_var.get()))
            threading.Thread(target=self._worker, args=args, daemon=True).start()

        def on_cancel(self):
            self._stop_requested = True
            self.cancel_btn.configure(state="disabled")
            self._log("Cancelando... (termina lo que ya esta en curso)")

        def _worker(self, in_dir, out_dir, report, ffmpeg, keep_at9, at9_only):
            try:
                res = run_extraction(
                    in_dir, out_dir, report, ffmpeg_path=ffmpeg, keep_at9=keep_at9,
                    at9_only=at9_only, log_cb=self._log,
                    progress_cb=lambda *a: self.msg_queue.put(("progress", a)),
                    should_stop=lambda: self._stop_requested)
                self.msg_queue.put(("finished", res))
            except Exception as ex:
                self.msg_queue.put(("finished", ex))

    App().mainloop()


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        launch_gui()
        return
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input_dir", help="carpeta con los .spk (y los .sbao, en cualquier subcarpeta)")
    ap.add_argument("output_dir", help="carpeta de salida")
    ap.add_argument("--report", default="reporte_fallos_ps4.txt")
    ap.add_argument("--ffmpeg", default=None, help="ruta a ffmpeg (por defecto, el del PATH)")
    ap.add_argument("--keep-at9", action="store_true", help="conservar tambien los .at9")
    ap.add_argument("--at9-only", action="store_true", help="no convertir: solo .at9")
    a = ap.parse_args(argv)
    run_extraction(a.input_dir, a.output_dir, a.report, ffmpeg_path=a.ffmpeg,
                   keep_at9=a.keep_at9, at9_only=a.at9_only)


if __name__ == "__main__":
    main()
