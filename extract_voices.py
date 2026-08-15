#!/usr/bin/env python3
"""
Extrait les voix Simple Voice Chat d'un replay Flashback, une piste WAV par joueur.

Les paquets voix sont stockes par Flashback comme une action dediee
(flashback:action/simple_voice_chat_sound_optional) contenant l'UUID de
l'emetteur et le PCM *deja decode* (short[] samples, 48 kHz mono, brut,
sans attenuation par la distance).

Format d'un chunk .flashback (FriendlyByteBuf, big-endian) :
    int32   magic
    varint  nombre d'actions
    n x     identifier (varint longueur + utf8)
    int32   taille du snapshot
    bytes   snapshot
    puis, en boucle jusqu'a la fin du fichier :
        varint  index de l'action
        int32   taille du payload
        bytes   payload

Payload voix :
    16 bytes  UUID
    varint    nombre de samples
    n x int16 samples PCM big-endian
    int8      type (0=static, 1=locational, 2=entity) + extras

Usage :
    python extract_voices.py
    python extract_voices.py --replay input/2026-08-15T17_04_28 --out output
    python extract_voices.py --chunks 1        # test rapide sur le 1er chunk
"""

import argparse
import hashlib
import json
import mmap
import os
import struct
import sys
import time
import uuid as uuidlib
from array import array
from collections import Counter
from pathlib import Path

SAMPLE_RATE = 48000
TICKS_PER_SECOND = 20
SAMPLES_PER_TICK = SAMPLE_RATE // TICKS_PER_SECOND  # 2400

VOICE_ACTION = "flashback:action/simple_voice_chat_sound_optional"
TICK_ACTION = "flashback:action/next_tick"

NAME_CHARS = set(b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")


# ---------------------------------------------------------------- lecture bas niveau

def read_varint(mm, pos):
    val = 0
    shift = 0
    while True:
        b = mm[pos]
        pos += 1
        val |= (b & 0x7F) << shift
        if not b & 0x80:
            return val, pos
        shift += 7


def read_header(mm):
    """Renvoie (noms d'actions, offset debut snapshot, offset debut actions)."""
    magic = struct.unpack_from(">i", mm, 0)[0]
    pos = 4
    count, pos = read_varint(mm, pos)
    names = []
    for _ in range(count):
        n, pos = read_varint(mm, pos)
        names.append(mm[pos:pos + n].decode("utf-8"))
        pos += n
    snapshot_size = struct.unpack_from(">i", mm, pos)[0]
    pos += 4
    if snapshot_size < 0:
        raise RuntimeError(f"taille de snapshot invalide: {snapshot_size}")
    return magic, names, pos, pos + snapshot_size


# ---------------------------------------------------------------- pistes audio

class Track:
    """Une piste WAV 48 kHz mono 16 bits, ecrite en acces direct (seek).

    Les silences ne sont jamais ecrits : on se contente de sauter a la bonne
    position dans le fichier, le systeme de fichiers comble avec des zeros.
    """

    def __init__(self, path, tolerance_samples):
        self.path = path
        self.f = open(path, "wb")
        self.f.write(b"\0" * 44)  # placeholder d'en-tete WAV
        self.tol = tolerance_samples
        self.cursor = None      # index du prochain sample contigu
        self.buf = []
        self.buflen = 0
        self.end = 0            # dernier sample ecrit
        self.speech = 0         # total de samples de parole
        self.segments = []
        self.seg_start = None

    def write(self, tick, pcm_le, nsamples):
        target = tick * SAMPLES_PER_TICK
        if self.cursor is None or target > self.cursor + self.tol:
            self._flush()
            if self.seg_start is not None:
                self.segments.append((self.seg_start, self.cursor))
            self.cursor = target
            self.seg_start = target
        self.buf.append(pcm_le)
        self.buflen += nsamples
        self.cursor += nsamples
        self.speech += nsamples
        if self.buflen >= 1_000_000:
            self._flush()

    def _flush(self):
        if not self.buf:
            return
        start = self.cursor - self.buflen
        self.f.seek(44 + start * 2)
        self.f.write(b"".join(self.buf))
        self.buf = []
        self.buflen = 0
        if self.cursor > self.end:
            self.end = self.cursor

    def close(self, total_samples):
        self._flush()
        if self.seg_start is not None:
            self.segments.append((self.seg_start, self.cursor))
            self.seg_start = None
        total = max(total_samples, self.end)
        self.f.truncate(44 + total * 2)
        self.f.seek(0)
        self.f.write(wav_header(total))
        self.f.close()
        return total


def wav_header(total_samples):
    data_size = total_samples * 2
    byte_rate = SAMPLE_RATE * 2
    return (b"RIFF" + struct.pack("<I", 36 + data_size) + b"WAVEfmt " +
            struct.pack("<IHHIIHH", 16, 1, 1, SAMPLE_RATE, byte_rate, 2, 16) +
            b"data" + struct.pack("<I", data_size))


# ---------------------------------------------------------------- noms de joueurs

def offline_uuid(name):
    """UUID hors-ligne Minecraft : md5('OfflinePlayer:<pseudo>'), version 3."""
    digest = bytearray(hashlib.md5(f"OfflinePlayer:{name}".encode("utf-8")).digest())
    digest[6] = (digest[6] & 0x0F) | 0x30
    digest[8] = (digest[8] & 0x3F) | 0x80
    return uuidlib.UUID(bytes=bytes(digest))


def find_names(mm, start, end, uuids, max_hits=50000):
    """Cherche 'UUID + string' dans les paquets (PlayerInfoUpdate) pour nommer les pistes."""
    found = {}
    region_end = end
    for u in uuids:
        needle = u.bytes
        counts = Counter()
        idx = mm.find(needle, start, region_end)
        hits = 0
        while idx != -1 and hits < max_hits:
            hits += 1
            p = idx + 16
            if p < region_end:
                n = mm[p]
                if 3 <= n <= 16 and p + 1 + n <= region_end:
                    cand = mm[p + 1:p + 1 + n]
                    if all(c in NAME_CHARS for c in cand):
                        counts[cand.decode("ascii")] += 1
            idx = mm.find(needle, idx + 1, region_end)
        for name, _ in counts.most_common(4):
            if offline_uuid(name) == u:      # verification cryptographique
                found[u] = (name, True)
                break
        else:
            if counts:
                found[u] = (counts.most_common(1)[0][0], False)
    return found


def safe(name):
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in name)


# ---------------------------------------------------------------- passe principale

def main():
    ap = argparse.ArgumentParser(description="Separe les voix Simple Voice Chat d'un replay Flashback")
    ap.add_argument("--replay", help="dossier du replay (contenant metadata.json)")
    ap.add_argument("--out", default="output", help="dossier de sortie")
    ap.add_argument("--chunks", type=int, default=0, help="ne traiter que les N premiers chunks (test)")
    ap.add_argument("--tolerance-ms", type=int, default=100,
                    help="ecart max avant d'inserer un silence de resynchronisation")
    args = ap.parse_args()

    root = Path(__file__).parent
    if args.replay:
        replay = Path(args.replay)
        if not replay.is_absolute():
            replay = root / replay
    else:
        candidates = sorted(p for p in (root / "input").iterdir() if (p / "metadata.json").exists())
        if not candidates:
            sys.exit("aucun replay trouve dans input/")
        replay = candidates[0]

    meta = json.loads((replay / "metadata.json").read_text(encoding="utf-8"))
    chunk_names = sorted(meta["chunks"], key=lambda n: int(n[1:].split(".")[0]))
    if args.chunks:
        chunk_names = chunk_names[:args.chunks]

    out_dir = Path(args.out)
    if not out_dir.is_absolute():
        out_dir = root / out_dir
    out_dir = out_dir / safe(meta.get("name") or replay.name)
    out_dir.mkdir(parents=True, exist_ok=True)

    total_ticks = sum(meta["chunks"][c]["duration"] for c in chunk_names)
    tolerance = args.tolerance_ms * SAMPLE_RATE // 1000

    print(f"replay      : {meta.get('name')} ({meta.get('version_string')})")
    print(f"chunks      : {len(chunk_names)}")
    print(f"duree       : {total_ticks} ticks = {total_ticks / TICKS_PER_SECOND / 60:.1f} min")
    print(f"sortie      : {out_dir}")
    print()

    tracks = {}
    tick_offset = 0
    t0 = time.time()
    total_voice_actions = 0
    uuid_cache = {}

    for ci, chunk_name in enumerate(chunk_names):
        path = replay / chunk_name
        with open(path, "rb") as fh:
            mm = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
            try:
                magic, names, snap_start, actions_start = read_header(mm)
                if VOICE_ACTION not in names:
                    print(f"  {chunk_name}: aucune action voix, ignore")
                    tick_offset += meta["chunks"][chunk_name]["duration"]
                    continue
                voice_id = names.index(VOICE_ACTION)
                tick_id = names.index(TICK_ACTION)

                pos = actions_start
                size_total = len(mm)
                tick = tick_offset
                nvoice = 0

                while pos < size_total:
                    b = mm[pos]
                    pos += 1
                    if b & 0x80:
                        extra, pos = read_varint(mm, pos)
                        aid = (b & 0x7F) | (extra << 7)
                    else:
                        aid = b
                    size = struct.unpack_from(">i", mm, pos)[0]
                    pos += 4
                    if aid == tick_id:
                        tick += 1
                        pos += size
                        continue
                    if aid != voice_id:
                        pos += size
                        continue

                    start = pos
                    raw = mm[pos:pos + 16]
                    u = uuid_cache.get(raw)
                    if u is None:
                        u = uuidlib.UUID(bytes=raw)
                        uuid_cache[raw] = u
                    p = start + 16
                    nsamples, p = read_varint(mm, p)
                    if nsamples:
                        pcm = array("h")
                        pcm.frombytes(mm[p:p + nsamples * 2])
                        pcm.byteswap()  # big-endian (Java) -> little-endian (WAV)
                        track = tracks.get(u)
                        if track is None:
                            track = Track(out_dir / f"{u}.wav", tolerance)
                            tracks[u] = track
                        track.write(tick, pcm.tobytes(), nsamples)
                        nvoice += 1
                    pos = start + size

                expected = meta["chunks"][chunk_name]["duration"]
                counted = tick - tick_offset
                warn = "" if counted == expected else f"  [!] {counted} ticks lus, {expected} attendus"
                total_voice_actions += nvoice
                elapsed = time.time() - t0
                print(f"  [{ci + 1:2d}/{len(chunk_names)}] {chunk_name:16s} "
                      f"{nvoice:7d} paquets voix  {len(tracks)} voix  {elapsed:6.1f}s{warn}")

                # noms de joueurs : on cherche dans le snapshot du premier chunk
                if ci == 0:
                    resolved = find_names(mm, snap_start, actions_start, list(tracks))
                    uuid_cache["__names__"] = resolved
            finally:
                mm.close()
        tick_offset += meta["chunks"][chunk_name]["duration"]

    if not tracks:
        sys.exit("aucun paquet voix trouve")

    # completer la resolution des noms sur les joueurs apparus plus tard
    names_map = uuid_cache.get("__names__", {})
    missing = [u for u in tracks if u not in names_map]
    if missing:
        with open(replay / chunk_names[0], "rb") as fh:
            mm = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
            try:
                names_map.update(find_names(mm, 0, len(mm), missing))
            finally:
                mm.close()

    print("\nfinalisation des pistes...")
    report = []
    for u, track in sorted(tracks.items(), key=lambda kv: -kv[1].speech):
        total = track.close(total_ticks * SAMPLES_PER_TICK)
        name, verified = names_map.get(u, (None, False))
        label = safe(name) if name else str(u)
        final = out_dir / f"{label}.wav"
        if final != track.path:
            if final.exists():
                final.unlink()
            os.rename(track.path, final)
        report.append({
            "uuid": str(u),
            "name": name,
            "name_verified": verified,
            "file": final.name,
            "speech_seconds": round(track.speech / SAMPLE_RATE, 2),
            "duration_seconds": round(total / SAMPLE_RATE, 2),
            "segments": [[round(a / SAMPLE_RATE, 2), round(b / SAMPLE_RATE, 2)] for a, b in track.segments],
        })

    (out_dir / "voices.json").write_text(
        json.dumps({
            "replay": meta.get("name"),
            "sample_rate": SAMPLE_RATE,
            "total_seconds": round(total_ticks / TICKS_PER_SECOND, 2),
            "speakers": report,
        }, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\n{len(report)} pistes, {total_voice_actions} paquets voix, {time.time() - t0:.1f}s\n")
    print(f"{'piste':24s} {'parole':>10s}  {'segments':>9s}  uuid")
    for r in report:
        mark = "" if r["name_verified"] else ("  (nom non verifie)" if r["name"] else "")
        print(f"{r['file']:24s} {r['speech_seconds'] / 60:9.1f}m  "
              f"{len(r['segments']):9d}  {r['uuid']}{mark}")
    print(f"\ntoutes les pistes font {report[0]['duration_seconds'] / 60:.1f} min "
          f"et demarrent a t=0 du replay -> alignement direct au montage.")


if __name__ == "__main__":
    main()
