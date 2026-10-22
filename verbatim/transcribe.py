"""verbatim.transcribe — turn an archived meeting recording into a transcript.

Runs in Verbatim's own venv (~/.local/share/verbatim/venv), which adds
`onnx-asr` on top of the system onnxruntime and numpy. The rest of Verbatim is
stdlib-only and calls this as a subprocess:

    python -m verbatim.transcribe AUDIO OUT.json

Why offline: VoxType's live meeting mode shares one `[parakeet] streaming`
flag with dictation and fails silently when they disagree (it destroyed a
month of meetings in July 2026), and VoxType 1.1.1 stopped seeing its own
meeting store. Recording a plain audio file first and transcribing it
afterwards means a recording can never be lost to the transcriber: if this
step fails, the file is still there to re-run.

Input: a stereo recording with the microphone on the left channel and the
system output (the other side of the call) on the right, as written by
core.start_audio_capture(). A mono file (older recordings mixed both) is
handled too, with every voice clustered together.

Output: VoxType's transcript.json shape, so the rest of Verbatim reads it
unchanged: {"segments": [{id, start_ms, end_ms, text, source, speaker_id,
confidence, chunk_id}], "total_chunks", "engine"}.

Progress goes to stdout as JSON lines: {"progress": 0.4, "message": "..."}.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np

SR = 16000
FRAME = 320                      # 20 ms
MODELS = Path(os.environ.get("VOXTYPE_MODELS",
                             Path.home() / ".local/share/voxtype/models"))
ASR_MODEL = os.environ.get("VERBATIM_ASR_MODEL", "parakeet-tdt-0.6b-v3")
MAX_SEG_S = 25.0                 # Parakeet is happiest under ~30 s per call
MIN_SEG_S = 0.35
MERGE_GAP_S = 0.6
PAD_S = 0.15
# Cosine similarity of ECAPA embeddings: same voice ~0.3-0.55, different
# voices ~0.17 (measured on the 2026-09-30 meeting). Merge above this.
SAME_SPEAKER = float(os.environ.get("VERBATIM_SAME_SPEAKER", "0.27"))


def say(progress: float, message: str) -> None:
    print(json.dumps({"progress": round(progress, 3), "message": message}),
          flush=True)


# ── audio ───────────────────────────────────────────────────────────────────
def channels(path: str) -> int:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=channels", "-of", "csv=p=0", path],
        capture_output=True, text=True, check=True).stdout.strip()
    return int(out or 1)


def decode(path: str, channel: int | None) -> np.ndarray:
    """One channel (or a mono downmix when channel is None) as float32 16 kHz."""
    af = ["-af", f"pan=mono|c0=c{channel}"] if channel is not None else ["-ac", "1"]
    pcm = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, *af, "-ar", str(SR),
         "-f", "f32le", "-"],
        capture_output=True, check=True).stdout
    return np.frombuffer(pcm, np.float32).copy()


# ── voice activity ──────────────────────────────────────────────────────────
def speech_regions(x: np.ndarray) -> list[tuple[int, int]]:
    """Energy VAD with an adaptive floor. Returns (start, end) sample pairs."""
    n = len(x) // FRAME
    if n == 0:
        return []
    fr = x[:n * FRAME].reshape(n, FRAME)
    db = 10 * np.log10(np.mean(fr * fr, axis=1) + 1e-10)
    floor = np.percentile(db, 20)
    thr = max(floor + 9.0, -55.0)
    if np.percentile(db, 99) < thr:          # a silent channel
        return []
    on = db > thr
    regions, start = [], None
    for i, v in enumerate(on):
        if v and start is None:
            start = i
        elif not v and start is not None:
            regions.append([start, i])
            start = None
    if start is not None:
        regions.append([start, n])

    pad, gap = int(PAD_S * SR / FRAME), int(MERGE_GAP_S * SR / FRAME)
    merged: list[list[int]] = []
    for s, e in regions:
        s, e = max(0, s - pad), min(n, e + pad)
        if merged and s - merged[-1][1] <= gap:
            merged[-1][1] = e
        else:
            merged.append([s, e])

    out: list[tuple[int, int]] = []
    max_f, min_f = int(MAX_SEG_S * SR / FRAME), int(MIN_SEG_S * SR / FRAME)
    for s, e in merged:
        # Split long stretches at the quietest frame in the back half of
        # each window, so a cut lands between words rather than inside one.
        while e - s > max_f:
            lo, hi = s + max_f // 2, s + max_f
            cut = lo + int(np.argmin(db[lo:hi]))
            out.append((s, cut))
            s = cut
        if e - s >= min_f:
            out.append((s, e))
    return [(s * FRAME, e * FRAME) for s, e in out]


# ── speakers ────────────────────────────────────────────────────────────────
def embed(sess, x: np.ndarray, regions: list[tuple[int, int]]) -> list:
    embs = []
    for s, e in regions:
        if e - s < SR:                       # under 1 s says little about a voice
            embs.append(None)
            continue
        a = x[s:min(e, s + 10 * SR)][None, :].astype(np.float32)
        v = sess.run(None, {"audio": a})[0].reshape(-1)
        embs.append(v / (np.linalg.norm(v) + 1e-9))
    return embs


def cluster(embs: list) -> list[int]:
    """Average-linkage agglomerative clustering on cosine similarity.

    Short segments without an embedding inherit the previous segment's
    speaker. Clusters are numbered by first appearance.
    """
    idx = [i for i, e in enumerate(embs) if e is not None]
    if not idx:
        return [0] * len(embs)
    E = np.array([embs[i] for i in idx])
    groups: list[list[int] | None] = [[k] for k in range(len(idx))]
    # Mean pairwise similarity between clusters, kept current with the
    # Lance-Williams update so a two-hour meeting clusters in seconds.
    M = E @ E.T
    np.fill_diagonal(M, -np.inf)
    while True:
        flat = int(np.argmax(M))
        a, b = divmod(flat, len(M))
        if M[a, b] < SAME_SPEAKER:
            break
        na, nb = len(groups[a]), len(groups[b])
        row = (na * M[a] + nb * M[b]) / (na + nb)
        M[a], M[:, a] = row, row
        M[a, a] = -np.inf
        M[b], M[:, b] = -np.inf, -np.inf
        groups[a] += groups[b]
        groups[b] = None
    groups = [g for g in groups if g]
    # Fold tiny clusters (under 3 % of segments) into their nearest neighbour:
    # they are usually laughter, a cough or a line of crosstalk.
    big = [g for g in groups if len(g) >= max(2, 0.03 * len(idx))] or groups
    cent = [E[g].mean(axis=0) for g in big]
    lab = np.zeros(len(idx), dtype=int)
    for g in groups:
        c = E[g].mean(axis=0)
        lab[g] = int(np.argmax([c @ k for k in cent]))
    order: dict[int, int] = {}
    labels, last = [], 0
    pos = dict(zip(idx, lab))
    for i in range(len(embs)):
        if i in pos:
            last = order.setdefault(int(pos[i]), len(order))
        labels.append(last)
    return labels


# ── main ────────────────────────────────────────────────────────────────────
def transcribe(audio: str, out_path: str) -> dict:
    import onnx_asr
    import onnxruntime as ort

    say(0.0, "Reading audio")
    nch = channels(audio)
    tracks = ([("microphone", decode(audio, 0)), ("loopback", decode(audio, 1))]
              if nch >= 2 else [("mixed", decode(audio, None))])

    items = []                               # (source, start, end) in samples
    for source, x in tracks:
        for s, e in speech_regions(x):
            items.append((source, s, e))
    items.sort(key=lambda t: t[1])
    total = len(items)
    say(0.05, f"Found {total} stretches of speech")

    say(0.06, "Loading speech model")
    asr = onnx_asr.load_model(f"nemo-{ASR_MODEL}", str(MODELS / ASR_MODEL),
                              providers=["CPUExecutionProvider"])
    audio_of = dict(tracks)
    texts: list[str] = []
    batch = 16
    for k in range(0, total, batch):
        chunk = items[k:k + batch]
        wavs = [audio_of[src][s:e] for src, s, e in chunk]
        texts += [t.strip() for t in asr.recognize(wavs, sample_rate=SR)]
        done = min(total, k + batch)
        say(0.08 + 0.80 * done / max(total, 1), f"Transcribing {done}/{total}")
    del asr

    say(0.9, "Telling speakers apart")
    spk = ort.InferenceSession(str(MODELS / "ecapa_tdnn.onnx"),
                               providers=["CPUExecutionProvider"])
    speaker = ["You" if src == "microphone" else None for src, _, _ in items]
    # The left channel is the person recording. Everyone else arrives on the
    # right channel (or, for an old mono mix, on the only channel).
    others = [i for i, (src, _, _) in enumerate(items) if src != "microphone"]
    first = 1 if nch >= 2 else 0
    embs = embed(spk, audio_of["loopback" if nch >= 2 else "mixed"],
                 [items[i][1:] for i in others])
    for i, lab in zip(others, cluster(embs)):
        speaker[i] = f"SPEAKER_{lab + first:02d}"

    segments = []
    for (src, s, e), text, who in zip(items, texts, speaker):
        if not text:
            continue
        segments.append({
            "id": len(segments), "start_ms": s * 1000 // SR, "end_ms": e * 1000 // SR,
            "text": text, "source": "microphone" if src == "microphone" else "loopback",
            "speaker_id": who, "confidence": 1.0, "chunk_id": 0,
        })
    result = {"segments": segments, "total_chunks": 1,
              "engine": f"verbatim-offline/{ASR_MODEL}",
              "duration_ms": max(len(x) for _, x in tracks) * 1000 // SR}
    tmp = Path(out_path).with_suffix(".tmp")
    tmp.write_text(json.dumps(result, indent=1), encoding="utf-8")
    tmp.replace(out_path)
    say(1.0, f"{len(segments)} lines transcribed")
    return result


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: python -m verbatim.transcribe AUDIO OUT.json")
    transcribe(sys.argv[1], sys.argv[2])
