#!/usr/bin/env python3
"""
Watch Dogs 1 (2013/2014 beta builds) - SPK/SBAO Audio Extractor (GUI)

Reimplements the logic of spk2xma.bms + xma_sbao.bms (QuickBMS scripts) in
pure Python, with automatic resolution of streamed .sbao pairs and a folder
scanner that produces a failure report for anything it can't decode yet.

Everything (parsing core + GUI) lives in this single file on purpose, so it's
easy to distribute or freeze into an .exe (e.g. `pyinstaller --onefile
wd_extractor_gui.py`).
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
# EXTRACTION CORE
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
            raise EOFError(f"read out of range at offset {self.pos:#x} (+{n})")
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


# RIFF/WAVE/fmt(XMA2)/data template, extracted programmatically (regex) from
# xma_sbao.bms / spk2xma.bms and verified against a real .sbao sample
# (decodes correctly as XMA2 24000Hz mono via ffprobe/ffmpeg).
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
    """Headerless .sbao (Jan-2014 prototype build): after the initial 0x1C
    bytes come the raw XMA2 packets directly, and the XMA2 format
    (WAVEFORMATEX + extras) lives in the .spk descriptor."""
    data_size = spk_fields["BlockCount"] * 0x800
    available = len(buf) - data_offset
    if data_size <= 0 or available < data_size:
        raise SbaoError("sbao_shorter_than_blockcount",
                         blockcount=spk_fields["BlockCount"], available=available, expected=data_size)
    raw = buf[data_offset:data_offset + data_size]
    return [("", build_wav_xma2(spk_fields, raw))]


def decode_sbao(buf, spk_fields=None):
    if len(buf) < 4:
        raise SbaoError("sbao_file_too_small")

    magic = struct.unpack(">H", buf[0:2])[0]
    if magic == 0x022A:
        data_offset = 0x1C
    elif magic == 0x0228:
        data_offset = struct.unpack(">I", buf[4:8])[0]
    else:
        raise SbaoError("unknown_sbao_magic", magic=hex(magic))

    r = Reader(buf, data_offset)
    ident = r.u16be()

    if ident != 0x10:
        if ident != 0x166:
            if spk_fields is not None:
                return decode_headerless_sbao(buf, data_offset, spk_fields)
            raise SbaoError("unknown_ident", ident=hex(ident))
        fields = read_fmt_fields(r)
        msize_file = len(buf)
        data_size = fields["BlockCount"] * 0x800
        data_offset2 = msize_file - data_size
        if data_offset2 < 0 or data_offset2 + data_size > msize_file:
            raise SbaoError("blockcount_out_of_range",
                             blockcount=fields["BlockCount"], filesize=msize_file)
        raw = buf[data_offset2:data_offset2 + data_size]
        return [("", build_wav_xma2(fields, raw))]

    return decode_multilayer(buf, base=data_offset, region_end=len(buf))


def decode_multilayer(buf, base, region_end):
    pos = base + 0x8
    r = Reader(buf, pos)
    layers = r.u32be()
    if layers <= 0 or layers > 64:
        raise SbaoError("invalid_layer_count", layers=layers)

    # the bms does "math dataOffset += 0x8; goto dataOffset" starting from
    # dataOffset=base+8 (already used to read LAYERS), so the next field
    # starts at base+16: from the current position (base+12, right after
    # reading LAYERS) that's only a 4-byte skip, not 8.
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
            raise SbaoError("unknown_wformattag", wformattag=hex(wfmt), layer=i)
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
                raise SbaoError("multilayer_infinite_loop", layer=i)
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
        raise SpkError("not_an_spk_file")
    count = r.u32le()
    if count <= 0 or count > 100000:
        raise SpkError("invalid_entry_count", count=count)

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
        # the XMA2 format also travels in the .spk descriptor (after the name,
        # the markers and 36 padding bytes); for headerless .sbao files
        # (Jan-2014 prototype) it is the only source of that format.
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
                "type": "SampleResourceDescriptor (streamed)",
                "reason": "sbao_not_found",
                "detail": f"dataRef={data_ref:08x}.sbao"
            })
            return
        try:
            sbuf = sbao_path.read_bytes()
            decoded = decode_sbao(sbuf, spk_fields)
        except SbaoError as e:
            fails.append({
                "spk": basename, "id": f"{entry_id:08x}",
                "type": "SampleResourceDescriptor (streamed)",
                "reason": e.reason, "detail": str(e.info),
                "sbao": sbao_path.name,
            })
            return
        except EOFError as e:
            fails.append({
                "spk": basename, "id": f"{entry_id:08x}",
                "type": "SampleResourceDescriptor (streamed)",
                "reason": "read_out_of_range", "detail": str(e),
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
            "type": "SampleResourceDescriptor (local)",
            "reason": "local_dataref_not_found",
            "detail": f"dataRef={data_ref:08x}"
        })
        return
    try:
        rr = Reader(buf, target["offset"])
        rr.skip(2)
        fields = read_fmt_fields(rr)
        data_offset = target["offset"] + 0x1C
        data_size = target["size"] - 0x1C
        if data_size <= 0 or data_offset + data_size > len(buf):
            raise SpkError("invalid_range", data_offset=data_offset, data_size=data_size)
        raw = buf[data_offset:data_offset + data_size]
        wav = build_wav_xma2(fields, raw)
        results.append((f"SMPR_{basename}_{entry_id:08x}_{data_ref:08x}.xma", wav))
    except (SpkError, EOFError) as e:
        fails.append({
            "spk": basename, "id": f"{entry_id:08x}",
            "type": "SampleResourceDescriptor (local)",
            "reason": getattr(e, "reason", "local_error"), "detail": str(e)
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
                "type": "MultiTrackResourceDescriptor (local)",
                "reason": "unknown_ident", "detail": f"IDENT={ident:#x}"
            })
            return
        try:
            decoded = decode_multilayer(buf, base=data_start, region_end=end_offset)
        except (SbaoError, EOFError) as e:
            fails.append({
                "spk": basename, "id": f"{entry_id:08x}",
                "type": "MultiTrackResourceDescriptor (local)",
                "reason": getattr(e, "reason", "multilayer_error"), "detail": str(e)
            })
            return
        for suffix, wav in decoded:
            results.append((f"MLTR_{basename}_{entry_id:08x}{suffix}.xma", wav))
        return

    if data_ref is None:
        fails.append({
            "spk": basename, "id": f"{entry_id:08x}",
            "type": "MultiTrackResourceDescriptor",
            "reason": "no_dataref_or_raw_data", "detail": ""
        })
        return
    sbao_path = find_sbao(sbao_index, data_ref)
    if sbao_path is None:
        fails.append({
            "spk": basename, "id": f"{entry_id:08x}",
            "type": "MultiTrackResourceDescriptor (streamed)",
            "reason": "sbao_not_found", "detail": f"dataRef={data_ref:08x}.sbao"
        })
        return
    try:
        sbuf = sbao_path.read_bytes()
        decoded = decode_sbao(sbuf)
    except (SbaoError, EOFError) as e:
        fails.append({
            "spk": basename, "id": f"{entry_id:08x}",
            "type": "MultiTrackResourceDescriptor (streamed)",
            "reason": getattr(e, "reason", "sbao_error"), "detail": str(e),
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
            raise SpkError("invalid_range")
        raw = buf[data_offset:data_offset + data_size]
        fields["BlockCount"] = blockcount
        wav = build_wav_xma2(fields, raw)
        results.append((f"SEQR_{basename}_{entry['id']:08x}.xma", wav))
    except (SpkError, EOFError) as e:
        fails.append({
            "spk": basename, "id": f"{entry['id']:08x}",
            "type": "SEQR (direct)",
            "reason": getattr(e, "reason", "seqr_error"), "detail": str(e)
        })


def process_spk(path, sbao_index, results, fails):
    basename = path.stem
    buf = path.read_bytes()
    try:
        entries = read_spk_entries(buf)
    except SpkError as e:
        fails.append({"spk": basename, "id": "-", "type": "SPK",
                       "reason": e.reason, "detail": str(e.info)})
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
                        "type": "descriptor", "reason": "unknown_dobbsid",
                        "detail": f"DobbsID={dobbs_id:#010x}"
                    })
            elif m_type == 0x0166:
                parse_seqr(r, entry, buf, results, fails, basename)
        except EOFError as e:
            fails.append({
                "spk": basename, "id": f"{entry['id']:08x}",
                "type": "-", "reason": "read_out_of_range", "detail": str(e)
            })


def build_sbao_index(root):
    idx = {}
    for p in root.rglob("*.sbao"):
        idx[p.name.lower()] = p
    return idx


def write_report(report_path, spk_count, total_ok, total_fail, all_fails):
    report_path = Path(report_path)
    with report_path.open("w", encoding="utf-8") as f:
        f.write(f"Extraction report - {spk_count} .spk files processed\n")
        f.write(f"Successfully extracted audio: {total_ok}\n")
        f.write(f"Failed entries:                {total_fail}\n")
        f.write("=" * 70 + "\n\n")

        by_reason = {}
        for fail in all_fails:
            by_reason.setdefault(fail["reason"], []).append(fail)

        f.write("SUMMARY BY REASON\n")
        f.write("-" * 70 + "\n")
        for reason, items in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
            f.write(f"  {reason}: {len(items)} cases\n")
        f.write("\n" + "=" * 70 + "\n\n")

        f.write("DETAIL\n")
        f.write("-" * 70 + "\n")
        for fail in all_fails:
            f.write(
                f"[{fail['spk']}] id={fail['id']}  type={fail['type']}  "
                f"reason={fail['reason']}"
                + (f"  sbao={fail['sbao']}" if "sbao" in fail else "")
                + (f"  detail={fail['detail']}" if fail.get("detail") else "")
                + "\n"
            )
    return report_path


def run_extraction(input_dir, output_dir, report_path,
                    log_cb=print, progress_cb=None, should_stop=None):
    in_dir = Path(input_dir)
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    log_cb("Indexing .sbao files...")
    sbao_index = build_sbao_index(in_dir)
    log_cb(f"  found {len(sbao_index)} .sbao files")

    spk_files = sorted(in_dir.rglob("*.spk"))
    log_cb(f"Processing {len(spk_files)} .spk files...")

    total_ok = 0
    total_fail = 0
    all_fails = []

    for i, spk_path in enumerate(spk_files, 1):
        if should_stop and should_stop():
            log_cb("Cancelled by user.")
            break
        results = []
        fails = []
        try:
            process_spk(spk_path, sbao_index, results, fails)
        except Exception as e:
            fails.append({"spk": spk_path.stem, "id": "-", "type": "SPK",
                           "reason": "unhandled_exception", "detail": repr(e)})

        for fname, wav in results:
            (out_dir / fname).write_bytes(wav)
        total_ok += len(results)
        total_fail += len(fails)
        all_fails.extend(fails)

        if progress_cb:
            progress_cb(i, len(spk_files), total_ok, total_fail)

    report_path = write_report(report_path, len(spk_files), total_ok, total_fail, all_fails)
    log_cb(f"Done. Extracted: {total_ok}  Failed: {total_fail}")
    log_cb(f"Report: {report_path.resolve()}")
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
    """Reusable row: label + entry + browse button. Stretches with the window."""

    def __init__(self, master, label_text, mode="dir", default_name=None, **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        self.mode = mode
        self.default_name = default_name

        self.grid_columnconfigure(1, weight=1)

        self.label = ctk.CTkLabel(self, text=label_text, width=170, anchor="w")
        self.label.grid(row=0, column=0, padx=(0, 8), pady=4, sticky="w")

        self.entry = ctk.CTkEntry(self)
        self.entry.grid(row=0, column=1, padx=(0, 8), pady=4, sticky="ew")

        self.button = ctk.CTkButton(self, text="Browse...", width=90, command=self._browse)
        self.button.grid(row=0, column=2, pady=4, sticky="e")

    def _browse(self):
        if self.mode == "dir":
            path = filedialog.askdirectory(title="Choose a folder")
        else:
            path = filedialog.asksaveasfilename(
                title="Save report as...",
                defaultextension=".txt",
                initialfile=self.default_name or "report.txt",
                filetypes=[("Text", "*.txt"), ("All files", "*.*")],
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

        self.title("Watch Dogs 1 (beta) - SPK/SBAO Extractor")
        self.geometry("760x560")
        self.minsize(600, 420)

        # the whole layout scales with the window size
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        self.msg_queue = queue.Queue()
        self.worker_thread = None
        self._stop_requested = False

        # ---- path fields ----
        paths_frame = ctk.CTkFrame(self)
        paths_frame.grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 8))
        paths_frame.grid_columnconfigure(0, weight=1)

        self.row_input = PathRow(paths_frame, "Input folder (.spk/.sbao):")
        self.row_input.grid(row=0, column=0, sticky="ew", pady=2)

        self.row_output = PathRow(paths_frame, "Output folder (.xma):")
        self.row_output.grid(row=1, column=0, sticky="ew", pady=2)

        self.row_report = PathRow(paths_frame, "Failure report (.txt):",
                                   mode="file", default_name="failure_report.txt")
        self.row_report.grid(row=2, column=0, sticky="ew", pady=2)

        # ---- controls ----
        controls_frame = ctk.CTkFrame(self, fg_color="transparent")
        controls_frame.grid(row=1, column=0, sticky="ew", padx=16, pady=4)
        controls_frame.grid_columnconfigure(2, weight=1)

        self.start_btn = ctk.CTkButton(controls_frame, text="Start extraction",
                                        command=self.on_start)
        self.start_btn.grid(row=0, column=0, padx=(0, 8))

        self.cancel_btn = ctk.CTkButton(controls_frame, text="Cancel",
                                         fg_color="#8B2E2E", hover_color="#6E2424",
                                         command=self.on_cancel, state="disabled")
        self.cancel_btn.grid(row=0, column=1, padx=(0, 8))

        self.status_label = ctk.CTkLabel(controls_frame, text="Ready.", anchor="w")
        self.status_label.grid(row=0, column=2, sticky="ew")

        # ---- progress ----
        progress_frame = ctk.CTkFrame(self, fg_color="transparent")
        progress_frame.grid(row=1, column=0, sticky="ew", padx=16, pady=(48, 4))
        progress_frame.grid_columnconfigure(0, weight=1)

        self.progress_bar = ctk.CTkProgressBar(progress_frame)
        self.progress_bar.set(0)
        self.progress_bar.grid(row=0, column=0, sticky="ew")

        # ---- log (with its own vertical scrollbar) ----
        log_frame = ctk.CTkFrame(self)
        log_frame.grid(row=2, column=0, sticky="nsew", padx=16, pady=(4, 16))
        log_frame.grid_columnconfigure(0, weight=1)
        log_frame.grid_rowconfigure(0, weight=1)

        self.log_box = ctk.CTkTextbox(log_frame, wrap="word", state="disabled")
        self.log_box.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)

        self.after(100, self._poll_queue)

    # ---- log/progress helpers, called from the worker thread ----

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
                        text=f"{done}/{total}  -  extracted: {ok}  failed: {fail}")
                elif kind == "finished":
                    self._on_finished(payload)
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _on_finished(self, result_or_error):
        self.start_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")
        if isinstance(result_or_error, Exception):
            messagebox.showerror("Error", f"An error occurred:\n{result_or_error}")
            self.status_label.configure(text="Finished with an error.")
            return
        r = result_or_error
        self.status_label.configure(
            text=f"Done: {r['total_ok']} extracted, {r['total_fail']} failed "
                 f"out of {r['spk_count']} .spk")
        messagebox.showinfo(
            "Extraction finished",
            f".spk files processed: {r['spk_count']}\n"
            f"Audio extracted: {r['total_ok']}\n"
            f"Failed: {r['total_fail']}\n\n"
            f"Report: {r['report_path']}"
        )

    # ---- button actions ----

    def on_start(self):
        input_dir = self.row_input.get()
        output_dir = self.row_output.get()
        report_path = self.row_report.get()

        if not input_dir or not os.path.isdir(input_dir):
            messagebox.showwarning("Missing input folder",
                                    "Choose a valid folder with .spk/.sbao files.")
            return
        if not output_dir:
            messagebox.showwarning("Missing output folder",
                                    "Choose where to save the extracted .xma files.")
            return
        if not report_path:
            report_path = str(Path(output_dir) / "failure_report.txt")
            self.row_report.set(report_path)

        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")
        self.progress_bar.set(0)
        self.status_label.configure(text="Starting...")

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
        self._log("Cancelling... (finishing the current .spk file)")

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
