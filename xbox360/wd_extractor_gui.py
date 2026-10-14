#!/usr/bin/env python3
"""
Watch Dogs 1 (beta) - Extractor de audio SPK/SBAO -> XMA/WAV (GUI)

Version con interfaz grafica (CustomTkinter) del extractor. Toda la logica
de parseo esta en este mismo archivo para que sea facil de distribuir /
compilar a exe (pyinstaller wd_extractor_gui.py --onefile).
"""

import os
import sys
import struct
import threading
import queue
from pathlib import Path

import customtkinter as ctk
from tkinter import filedialog, messagebox

# ============================================================================
# NUCLEO DE EXTRACCION (igual que wd_extractor.py)
# ============================================================================

class Reader:
    __slots__ = ("buf", "pos")

    def __init__(self, buf, pos=0):
        self.buf = buf
        self.pos = pos

    def seek(self, pos):
        self.pos = pos

    def skip(self, n):
        self.pos += n

    def _take(self, n):
        if self.pos + n > len(self.buf):
            raise EOFError(f"lectura fuera de rango en offset {self.pos:#x} (+{n})")
        b = self.buf[self.pos:self.pos + n]
        self.pos += n
        return b

    def u8(self):
        return self._take(1)[0]

    def u16le(self):
        return struct.unpack("<H", self._take(2))[0]

    def u16be(self):
        return struct.unpack(">H", self._take(2))[0]

    def u32le(self):
        return struct.unpack("<I", self._take(4))[0]

    def u32be(self):
        return struct.unpack(">I", self._take(4))[0]

    def raw(self, n):
        return self._take(n)


# Template RIFF/WAVE/fmt(XMA2)/data, extraido programaticamente (regex) desde
# xma_sbao.bms / spk2xma.bms y verificado contra un .sbao real (000ffeed.sbao
# -> decodifica correctamente como XMA2 24000Hz mono con ffprobe/ffmpeg).
WAV_TEMPLATE = bytes.fromhex(
    "5249464648d00c0057415645666d74203400000066010100c05d0000441b0000"
    "02001000220001000000000000722a00000800000000000000702a008001000000"
    "702a00ff049a016461746100d00c00"
)
assert len(WAV_TEMPLATE) == 80, len(WAV_TEMPLATE)
assert WAV_TEMPLATE[0x48:0x4C] == b"data"


def build_wav_xma2(fields, raw):
    hdr = bytearray(WAV_TEMPLATE)
    msize = len(raw)
    riffsize = msize + 0x48

    def put(off, val, size):
        hdr[off:off + size] = int(val).to_bytes(size, "little")

    put(0x4, riffsize, 4)
    put(0x16, fields["nChannels"], 2)
    put(0x18, fields["nSamplesPerSec"], 4)
    put(0x1C, fields["nAvgBytesPerSec"], 4)
    put(0x20, fields["nBlockAlign"], 2)
    put(0x22, fields["wBitsPerSample"], 2)
    put(0x24, fields["cbSize"], 2)
    put(0x26, fields["NumStreams"], 2)
    put(0x28, fields["ChannelMask"], 4)
    put(0x2C, fields["SamplesEncoded"], 4)
    put(0x30, fields["BytesPerBlock"], 4)
    put(0x34, fields["PlayBegin"], 4)
    put(0x38, fields["PlayLength"], 4)
    put(0x3C, fields["LoopBegin"], 4)
    put(0x40, fields["LoopLength"], 4)
    hdr[0x44] = fields["LoopCount"] & 0xFF
    hdr[0x45] = fields["EncoderVersion"] & 0xFF
    put(0x46, fields["BlockCount"], 2)
    put(0x4C, msize, 4)
    return bytes(hdr) + raw


def read_fmt_fields(r):
    f = {}
    f["nChannels"] = r.u16be()
    f["nSamplesPerSec"] = r.u32be()
    f["nAvgBytesPerSec"] = r.u32be()
    f["nBlockAlign"] = r.u16be()
    f["wBitsPerSample"] = r.u16be()
    f["cbSize"] = r.u16be()
    f["NumStreams"] = r.u16be()
    f["ChannelMask"] = r.u32be()
    f["SamplesEncoded"] = r.u32be()
    f["BytesPerBlock"] = r.u32be()
    f["PlayBegin"] = r.u32be()
    f["PlayLength"] = r.u32be()
    f["LoopBegin"] = r.u32be()
    f["LoopLength"] = r.u32be()
    f["LoopCount"] = r.u8()
    f["EncoderVersion"] = r.u8()
    f["BlockCount"] = r.u16be()
    return f


class SbaoError(Exception):
    def __init__(self, reason, **info):
        super().__init__(reason)
        self.reason = reason
        self.info = info


def decode_headerless_sbao(buf, data_offset, spk_fields):
    """.sbao sin cabecera de formato (build prototipo ene-2014): tras los 0x1C
    bytes iniciales vienen directo los paquetes XMA2 crudos, y el formato XMA2
    (WAVEFORMATEX + extras) vive en el descriptor del .spk."""
    data_size = spk_fields["BlockCount"] * 0x800
    available = len(buf) - data_offset
    if data_size <= 0 or available < data_size:
        raise SbaoError("sbao_mas_corto_que_blockcount",
                         blockcount=spk_fields["BlockCount"], disponible=available, esperado=data_size)
    raw = buf[data_offset:data_offset + data_size]
    return [("", build_wav_xma2(spk_fields, raw))]


def decode_sbao(buf, spk_fields=None):
    if len(buf) < 4:
        raise SbaoError("archivo sbao demasiado chico")

    magic = struct.unpack(">H", buf[0:2])[0]
    if magic == 0x022A:
        data_offset = 0x1C
    elif magic == 0x0228:
        data_offset = struct.unpack(">I", buf[4:8])[0]
    else:
        raise SbaoError("magic_sbao_desconocido", magic=hex(magic))

    r = Reader(buf, data_offset)
    ident = r.u16be()

    if ident != 0x10:
        if ident != 0x166:
            if spk_fields is not None:
                return decode_headerless_sbao(buf, data_offset, spk_fields)
            raise SbaoError("ident_desconocido", ident=hex(ident))
        fields = read_fmt_fields(r)
        msize_file = len(buf)
        data_size = fields["BlockCount"] * 0x800
        data_offset2 = msize_file - data_size
        if data_offset2 < 0 or data_offset2 + data_size > msize_file:
            raise SbaoError("blockcount_fuera_de_rango",
                             blockcount=fields["BlockCount"], filesize=msize_file)
        raw = buf[data_offset2:data_offset2 + data_size]
        return [("", build_wav_xma2(fields, raw))]

    return decode_multilayer(buf, base=data_offset, region_end=len(buf))


def decode_multilayer(buf, base, region_end):
    pos = base + 0x8
    r = Reader(buf, pos)
    layers = r.u32be()
    if layers <= 0 or layers > 64:
        raise SbaoError("layers_invalido", layers=layers)

    # el bms hace "math dataOffset += 0x8; goto dataOffset" partiendo de
    # dataOffset=base+8 (ya usado para leer LAYERS), o sea que el siguiente
    # campo arranca en base+16: desde la posicion actual (base+12, justo
    # despues de leer LAYERS) son solo 4 bytes de salto, no 8.
    r.seek(base + 0x10)
    r.u32be()
    r.u32be()
    r.u32be()

    size_layer = [r.u32be() for _ in range(layers)]
    r.skip(0x40)
    r.skip(8)

    size_info = [r.u32be() for _ in range(layers)]

    layer_fmt = []
    for i in range(layers):
        wfmt = r.u16be()
        if wfmt != 0x166:
            raise SbaoError("wformattag_desconocido", wformattag=hex(wfmt), layer=i)
        fields = {"wFormatTag": wfmt}
        fields.update(read_fmt_fields(r))
        layer_fmt.append(fields)

    r.u32be()
    r.u32be()

    total_seek = 0
    for _ in range(layers):
        total_seek += r.u32be()
    r.skip(total_seek)

    offset_firstblock = r.pos

    results = []
    for i in range(layers):
        mem = bytearray()
        next_offset = offset_firstblock
        guard = 0
        while True:
            guard += 1
            if guard > 200000:
                raise SbaoError("loop_infinito_multilayer", layer=i)
            cur = next_offset
            cur += 4
            cur += 4
            block_sizes = []
            for _j in range(layers):
                v = struct.unpack(">I", buf[cur:cur + 4])[0]
                block_sizes.append(v)
                cur += 4
            start_offset = cur
            next_offset = start_offset + sum(block_sizes)
            size_to_skip = sum(block_sizes[:i])
            layer_block_offset = start_offset + size_to_skip
            nblocksize = block_sizes[i]
            mem += buf[layer_block_offset:layer_block_offset + nblocksize]
            if not (next_offset < region_end):
                break
        raw = bytes(mem)
        fields = layer_fmt[i]
        wav = build_wav_xma2(fields, raw)
        results.append((f"_L{i + 1}", wav))
    return results


class SpkError(Exception):
    def __init__(self, reason, **info):
        super().__init__(reason)
        self.reason = reason
        self.info = info


def read_spk_entries(buf):
    r = Reader(buf, 0)
    magic = r.u32le()
    if magic != 1397771010:
        raise SpkError("no_es_spk")
    count = r.u32le()
    if count <= 0 or count > 100000:
        raise SpkError("count_invalido", count=count)

    ids = [r.u32le() for _ in range(count)]

    entries = []
    for k in range(count):
        size = r.u32le()
        local_offset = r.pos
        entries.append({"id": ids[k], "offset": local_offset, "size": size})
        r.seek(local_offset + size)
    return entries


def find_sbao(sbao_index, data_ref):
    name = f"{data_ref:08x}.sbao"
    return sbao_index.get(name.lower())


def parse_sample_descriptor(r, entry_id, entries, buf, sbao_index, results, fails, basename):
    count_emitter = r.u32be() * 4
    r.skip(count_emitter)
    r.skip(40)
    r.skip(16)
    bstream = r.u32be()

    if bstream != 0:
        r.skip(16)
        data_ref = r.u32be()
        r.u32be()
        # el formato XMA2 tambien viaja en el descriptor del .spk (tras el nombre,
        # los markers y 36 bytes de relleno); en los .sbao sin cabecera propia
        # (prototipo ene-2014) es la unica fuente de ese formato.
        spk_fields = None
        try:
            strlen = r.u32be()
            mod = strlen % 4
            r.skip(strlen + (4 - mod) if mod else strlen)
            r.skip(r.u32be() * 4)
            r.skip(36)
            if r.u16be() == 0x166:
                spk_fields = read_fmt_fields(r)
        except EOFError:
            spk_fields = None
        sbao_path = find_sbao(sbao_index, data_ref)
        if sbao_path is None:
            fails.append({
                "spk": basename, "id": f"{entry_id:08x}",
                "tipo": "SampleResourceDescriptor (streamed)",
                "motivo": "sbao_no_encontrado",
                "detalle": f"dataRef={data_ref:08x}.sbao"
            })
            return
        try:
            sbuf = sbao_path.read_bytes()
            decoded = decode_sbao(sbuf, spk_fields)
        except SbaoError as e:
            fails.append({
                "spk": basename, "id": f"{entry_id:08x}",
                "tipo": "SampleResourceDescriptor (streamed)",
                "motivo": e.reason, "detalle": str(e.info),
                "sbao": sbao_path.name,
            })
            return
        except EOFError as e:
            fails.append({
                "spk": basename, "id": f"{entry_id:08x}",
                "tipo": "SampleResourceDescriptor (streamed)",
                "motivo": "lectura_fuera_de_rango", "detalle": str(e),
                "sbao": sbao_path.name,
            })
            return
        for suffix, wav in decoded:
            results.append((f"SMPR_{basename}_{entry_id:08x}{suffix}.xma", wav))
        return

    data_ref = r.u32be()
    strlen = r.u32be()
    mod = strlen % 4
    if mod != 0:
        r.skip(strlen + (4 - mod))
    else:
        r.skip(strlen)
    count_marker = r.u32be() * 4
    r.skip(count_marker)
    r.skip(36)

    target = next((e for e in entries if e["id"] == data_ref), None)
    if target is None:
        fails.append({
            "spk": basename, "id": f"{entry_id:08x}",
            "tipo": "SampleResourceDescriptor (local)",
            "motivo": "dataRef_local_no_encontrado",
            "detalle": f"dataRef={data_ref:08x}"
        })
        return
    try:
        rr = Reader(buf, target["offset"])
        rr.skip(2)
        fields = read_fmt_fields(rr)
        data_offset = target["offset"] + 0x1C
        data_size = target["size"] - 0x1C
        if data_size <= 0 or data_offset + data_size > len(buf):
            raise SpkError("rango_invalido", data_offset=data_offset, data_size=data_size)
        raw = buf[data_offset:data_offset + data_size]
        wav = build_wav_xma2(fields, raw)
        results.append((f"SMPR_{basename}_{entry_id:08x}_{data_ref:08x}.xma", wav))
    except (SpkError, EOFError) as e:
        fails.append({
            "spk": basename, "id": f"{entry_id:08x}",
            "tipo": "SampleResourceDescriptor (local)",
            "motivo": getattr(e, "reason", "error_local"), "detalle": str(e)
        })


def parse_multitrack_descriptor(r, entry_id, entries, buf, sbao_index, results, fails, basename):
    count_emitter = r.u32be() * 4
    r.skip(count_emitter)
    r.skip(0x24)
    bstream = r.u32be()

    if bstream != 0:
        r.skip(16)
        data_ref = r.u32be()
        r.u32be()
    else:
        data_ref = r.u32be()

    strlen = r.u32be()
    mod = strlen % 4
    if mod != 0:
        r.skip(strlen + (4 - mod))
    else:
        r.skip(strlen)
    count_marker = r.u32be() * 4
    r.skip(count_marker)
    r.skip(8)
    tracks = r.u32be() * 52
    r.skip(tracks)
    raw_size = r.u32be()

    if raw_size > 0:
        r.skip(0x0C)
        data_start = r.pos
        end_offset = data_start + raw_size
        ident = r.u16be()
        if ident != 0x10:
            fails.append({
                "spk": basename, "id": f"{entry_id:08x}",
                "tipo": "MultiTrackResourceDescriptor (local)",
                "motivo": "ident_desconocido", "detalle": f"IDENT={ident:#x}"
            })
            return
        try:
            decoded = decode_multilayer(buf, base=data_start, region_end=end_offset)
        except (SbaoError, EOFError) as e:
            fails.append({
                "spk": basename, "id": f"{entry_id:08x}",
                "tipo": "MultiTrackResourceDescriptor (local)",
                "motivo": getattr(e, "reason", "error_multilayer"), "detalle": str(e)
            })
            return
        for suffix, wav in decoded:
            results.append((f"MLTR_{basename}_{entry_id:08x}{suffix}.xma", wav))
        return

    if data_ref is None:
        fails.append({
            "spk": basename, "id": f"{entry_id:08x}",
            "tipo": "MultiTrackResourceDescriptor",
            "motivo": "sin_dataRef_ni_raw", "detalle": ""
        })
        return
    sbao_path = find_sbao(sbao_index, data_ref)
    if sbao_path is None:
        fails.append({
            "spk": basename, "id": f"{entry_id:08x}",
            "tipo": "MultiTrackResourceDescriptor (streamed)",
            "motivo": "sbao_no_encontrado", "detalle": f"dataRef={data_ref:08x}.sbao"
        })
        return
    try:
        sbuf = sbao_path.read_bytes()
        decoded = decode_sbao(sbuf)
    except (SbaoError, EOFError) as e:
        fails.append({
            "spk": basename, "id": f"{entry_id:08x}",
            "tipo": "MultiTrackResourceDescriptor (streamed)",
            "motivo": getattr(e, "reason", "error_sbao"), "detalle": str(e),
            "sbao": sbao_path.name,
        })
        return
    for suffix, wav in decoded:
        results.append((f"MLTR_{basename}_{entry_id:08x}{suffix}.xma", wav))


def parse_seqr(r, entry, buf, results, fails, basename):
    try:
        m_offset = r.pos - 4
        r.seek(m_offset)
        r.u16be()
        fields = read_fmt_fields(r)
        data_offset = entry["offset"] + entry["size"]
        data_size_total = entry["size"]
        blockcount = data_size_total // 2048
        data_size = blockcount * 0x800
        data_offset -= data_size
        if data_offset < entry["offset"] or data_offset + data_size > len(buf):
            raise SpkError("rango_invalido")
        raw = buf[data_offset:data_offset + data_size]
        fields["BlockCount"] = blockcount
        wav = build_wav_xma2(fields, raw)
        results.append((f"SEQR_{basename}_{entry['id']:08x}.xma", wav))
    except (SpkError, EOFError) as e:
        fails.append({
            "spk": basename, "id": f"{entry['id']:08x}",
            "tipo": "SEQR (directo)",
            "motivo": getattr(e, "reason", "error_seqr"), "detalle": str(e)
        })


def process_spk(path, sbao_index, results, fails):
    basename = path.stem
    buf = path.read_bytes()
    try:
        entries = read_spk_entries(buf)
    except SpkError as e:
        fails.append({"spk": basename, "id": "-", "tipo": "SPK",
                       "motivo": e.reason, "detalle": str(e.info)})
        return

    for entry in entries:
        try:
            r = Reader(buf, entry["offset"])
            r.u32le()
            r.skip(20)
            unk6 = r.u32be()
            if unk6 == 0:
                continue

            type_ = r.u32be()
            m_type = type_ >> 16

            if type_ == 0x17CE46D9:
                r.skip(44)
                dobbs_id = r.u32be()
                if dobbs_id == 0x93B1ECE5:
                    parse_sample_descriptor(r, entry["id"], entries, buf, sbao_index,
                                             results, fails, basename)
                elif dobbs_id == 0xF2D3BBD4:
                    parse_multitrack_descriptor(r, entry["id"], entries, buf, sbao_index,
                                                 results, fails, basename)
                else:
                    fails.append({
                        "spk": basename, "id": f"{entry['id']:08x}",
                        "tipo": "descriptor", "motivo": "dobbsid_desconocido",
                        "detalle": f"DobbsID={dobbs_id:#010x}"
                    })
            elif m_type == 0x0166:
                parse_seqr(r, entry, buf, results, fails, basename)
        except EOFError as e:
            fails.append({
                "spk": basename, "id": f"{entry['id']:08x}",
                "tipo": "-", "motivo": "lectura_fuera_de_rango", "detalle": str(e)
            })


def build_sbao_index(root):
    idx = {}
    for p in root.rglob("*.sbao"):
        idx[p.name.lower()] = p
    return idx


def write_report(report_path, spk_count, total_ok, total_fail, all_fails):
    report_path = Path(report_path)
    with report_path.open("w", encoding="utf-8") as f:
        f.write(f"Reporte de extraccion - {spk_count} archivos .spk procesados\n")
        f.write(f"Audios extraidos correctamente: {total_ok}\n")
        f.write(f"Audios/entradas que fallaron:   {total_fail}\n")
        f.write("=" * 70 + "\n\n")

        by_reason = {}
        for fail in all_fails:
            by_reason.setdefault(fail["motivo"], []).append(fail)

        f.write("RESUMEN POR MOTIVO\n")
        f.write("-" * 70 + "\n")
        for reason, items in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
            f.write(f"  {reason}: {len(items)} casos\n")
        f.write("\n" + "=" * 70 + "\n\n")

        f.write("DETALLE\n")
        f.write("-" * 70 + "\n")
        for fail in all_fails:
            f.write(
                f"[{fail['spk']}] id={fail['id']}  tipo={fail['tipo']}  "
                f"motivo={fail['motivo']}"
                + (f"  sbao={fail['sbao']}" if "sbao" in fail else "")
                + (f"  detalle={fail['detalle']}" if fail.get("detalle") else "")
                + "\n"
            )
    return report_path


def run_extraction(input_dir, output_dir, report_path,
                    log_cb=print, progress_cb=None, should_stop=None):
    in_dir = Path(input_dir)
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    log_cb("Indexando archivos .sbao...")
    sbao_index = build_sbao_index(in_dir)
    log_cb(f"  {len(sbao_index)} .sbao encontrados")

    spk_files = sorted(in_dir.rglob("*.spk"))
    log_cb(f"Procesando {len(spk_files)} archivos .spk...")

    total_ok = 0
    total_fail = 0
    all_fails = []

    for i, spk_path in enumerate(spk_files, 1):
        if should_stop and should_stop():
            log_cb("Cancelado por el usuario.")
            break
        results = []
        fails = []
        try:
            process_spk(spk_path, sbao_index, results, fails)
        except Exception as e:
            fails.append({"spk": spk_path.stem, "id": "-", "tipo": "SPK",
                           "motivo": "excepcion_no_controlada", "detalle": repr(e)})

        for fname, wav in results:
            (out_dir / fname).write_bytes(wav)
        total_ok += len(results)
        total_fail += len(fails)
        all_fails.extend(fails)

        if progress_cb:
            progress_cb(i, len(spk_files), total_ok, total_fail)

    report_path = write_report(report_path, len(spk_files), total_ok, total_fail, all_fails)
    log_cb(f"Listo. Extraidos: {total_ok}  Fallos: {total_fail}")
    log_cb(f"Reporte: {report_path.resolve()}")
    return {
        "spk_count": len(spk_files),
        "total_ok": total_ok,
        "total_fail": total_fail,
        "report_path": report_path,
    }


# ============================================================================
# GUI
# ============================================================================

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


class PathRow(ctk.CTkFrame):
    """Fila reutilizable: label + entry + boton buscar. Se estira con la ventana."""

    def __init__(self, master, label_text, mode="dir", default_name=None, **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        self.mode = mode
        self.default_name = default_name

        self.grid_columnconfigure(1, weight=1)

        self.label = ctk.CTkLabel(self, text=label_text, width=170, anchor="w")
        self.label.grid(row=0, column=0, padx=(0, 8), pady=4, sticky="w")

        self.entry = ctk.CTkEntry(self)
        self.entry.grid(row=0, column=1, padx=(0, 8), pady=4, sticky="ew")

        self.button = ctk.CTkButton(self, text="Buscar...", width=90, command=self._browse)
        self.button.grid(row=0, column=2, pady=4, sticky="e")

    def _browse(self):
        if self.mode == "dir":
            path = filedialog.askdirectory(title="Elegi una carpeta")
        else:
            path = filedialog.asksaveasfilename(
                title="Guardar reporte como...",
                defaultextension=".txt",
                initialfile=self.default_name or "reporte.txt",
                filetypes=[("Texto", "*.txt"), ("Todos los archivos", "*.*")],
            )
        if path:
            self.entry.delete(0, "end")
            self.entry.insert(0, path)

    def get(self):
        return self.entry.get().strip()

    def set(self, value):
        self.entry.delete(0, "end")
        self.entry.insert(0, value)


class App(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("Watch Dogs 1 (beta) - Extractor SPK/SBAO")
        self.geometry("760x560")
        self.minsize(600, 420)

        # el layout entero se adapta al tamano de ventana
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        self.msg_queue = queue.Queue()
        self.worker_thread = None
        self._stop_requested = False

        # ---- fila de rutas ----
        paths_frame = ctk.CTkFrame(self)
        paths_frame.grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 8))
        paths_frame.grid_columnconfigure(0, weight=1)

        self.row_input = PathRow(paths_frame, "Carpeta con .spk/.sbao:")
        self.row_input.grid(row=0, column=0, sticky="ew", pady=2)

        self.row_output = PathRow(paths_frame, "Carpeta de salida (.xma):")
        self.row_output.grid(row=1, column=0, sticky="ew", pady=2)

        self.row_report = PathRow(paths_frame, "Reporte de fallos (.txt):",
                                   mode="file", default_name="reporte_fallos.txt")
        self.row_report.grid(row=2, column=0, sticky="ew", pady=2)

        # ---- controles ----
        controls_frame = ctk.CTkFrame(self, fg_color="transparent")
        controls_frame.grid(row=1, column=0, sticky="ew", padx=16, pady=4)
        controls_frame.grid_columnconfigure(2, weight=1)

        self.start_btn = ctk.CTkButton(controls_frame, text="Iniciar extraccion",
                                        command=self.on_start)
        self.start_btn.grid(row=0, column=0, padx=(0, 8))

        self.cancel_btn = ctk.CTkButton(controls_frame, text="Cancelar",
                                         fg_color="#8B2E2E", hover_color="#6E2424",
                                         command=self.on_cancel, state="disabled")
        self.cancel_btn.grid(row=0, column=1, padx=(0, 8))

        self.status_label = ctk.CTkLabel(controls_frame, text="Listo.", anchor="w")
        self.status_label.grid(row=0, column=2, sticky="ew")

        # ---- progreso ----
        progress_frame = ctk.CTkFrame(self, fg_color="transparent")
        progress_frame.grid(row=1, column=0, sticky="ew", padx=16, pady=(48, 4))
        progress_frame.grid_columnconfigure(0, weight=1)

        self.progress_bar = ctk.CTkProgressBar(progress_frame)
        self.progress_bar.set(0)
        self.progress_bar.grid(row=0, column=0, sticky="ew")

        # ---- log (con scroll vertical propio) ----
        log_frame = ctk.CTkFrame(self)
        log_frame.grid(row=2, column=0, sticky="nsew", padx=16, pady=(4, 16))
        log_frame.grid_columnconfigure(0, weight=1)
        log_frame.grid_rowconfigure(0, weight=1)

        self.log_box = ctk.CTkTextbox(log_frame, wrap="word", state="disabled")
        self.log_box.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)

        self.after(100, self._poll_queue)

    # ---- helpers de log/progreso, llamados desde el hilo de trabajo ----

    def _log(self, text):
        self.msg_queue.put(("log", text))

    def _progress(self, done, total, ok, fail):
        self.msg_queue.put(("progress", (done, total, ok, fail)))

    def _finished(self, result_or_error):
        self.msg_queue.put(("finished", result_or_error))

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
                    frac = done / total if total else 0
                    self.progress_bar.set(frac)
                    self.status_label.configure(
                        text=f"{done}/{total}  -  extraidos: {ok}  fallos: {fail}")
                elif kind == "finished":
                    self._on_finished(payload)
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _on_finished(self, result_or_error):
        self.start_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")
        if isinstance(result_or_error, Exception):
            messagebox.showerror("Error", f"Ocurrio un error:\n{result_or_error}")
            self.status_label.configure(text="Termino con error.")
            return
        r = result_or_error
        self.status_label.configure(
            text=f"Listo: {r['total_ok']} extraidos, {r['total_fail']} fallos "
                 f"de {r['spk_count']} .spk")
        messagebox.showinfo(
            "Extraccion terminada",
            f".spk procesados: {r['spk_count']}\n"
            f"Audios extraidos: {r['total_ok']}\n"
            f"Fallos: {r['total_fail']}\n\n"
            f"Reporte: {r['report_path']}"
        )

    # ---- acciones de botones ----

    def on_start(self):
        input_dir = self.row_input.get()
        output_dir = self.row_output.get()
        report_path = self.row_report.get()

        if not input_dir or not os.path.isdir(input_dir):
            messagebox.showwarning("Falta la carpeta de entrada",
                                    "Elegi una carpeta valida con .spk/.sbao.")
            return
        if not output_dir:
            messagebox.showwarning("Falta la carpeta de salida",
                                    "Elegi donde guardar los .xma extraidos.")
            return
        if not report_path:
            report_path = str(Path(output_dir) / "reporte_fallos.txt")
            self.row_report.set(report_path)

        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")
        self.progress_bar.set(0)
        self.status_label.configure(text="Iniciando...")

        self.start_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self._stop_requested = False

        self.worker_thread = threading.Thread(
            target=self._worker, args=(input_dir, output_dir, report_path), daemon=True
        )
        self.worker_thread.start()

    def on_cancel(self):
        self._stop_requested = True
        self.cancel_btn.configure(state="disabled")
        self._log("Cancelando... (termina el archivo .spk actual)")

    def _worker(self, input_dir, output_dir, report_path):
        try:
            result = run_extraction(
                input_dir, output_dir, report_path,
                log_cb=self._log,
                progress_cb=self._progress,
                should_stop=lambda: self._stop_requested,
            )
            self._finished(result)
        except Exception as e:
            self._finished(e)


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
