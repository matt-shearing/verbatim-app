# Plan: move Verbatim's AI to the local 27B, and catch up with VoxType 1.0

Written 2026-09-17 after measuring `qwen3.6:27b` on this machine's own
Ollama against a real archived transcript. Nothing here is implemented yet.

## Where things stand

- The recorder still works the way `docs/LOCAL-AI.md` describes: VoxType
  meeting mode captures and transcribes, Verbatim sends the transcript to
  `gemma4:12b` on the LAN GPU node through the SSH tunnel on 11435.
- No meeting has been recorded since VoxType was upgraded from 0.7.5 to
  **1.0.1** on 2026-09-05. The 1.0.x release notes say nothing about meeting
  mode. The string `transcribe_timed is not supported in streaming mode` is
  still in the 1.0.1 binary, so the streaming conflict that
  `enter_meeting_mode()` works around is still there. **Meeting mode under
  1.0.1 is unverified.** Run the loopback test below before relying on it.
- This machine's Ollama now has `qwen3.6:27b` (Q4_K_M, 17 GB, 262k context,
  thinking + tools) on a 24 GB GPU. It was not there when the GPU-node
  decision was made.

## What the 27B measured

Real 48-minute call, three people, `ml` diarization, 194 chunks. Prompt
`verbatim/prompts/analysis.md` unchanged, `think: false`, temperature 0.2.

| Pass | Prompt tokens | Output tokens | Prefill | Generation | Wall time |
|---|---|---|---|---|---|
| Analysis | 15,130 | 1,740 | 821 tok/s | 27.9 tok/s | 102 s (incl. 21 s load) |
| Identify, no roster | 19,938 | 2,802 | 632 tok/s | 24.8 tok/s | 145 s |
| Identify, roster | 19,938 | 3,045 | | | 152 s |

Fit on the GPU (`ollama ps` reports 100% GPU in every case):

| `num_ctx` | Model + cache | Enough for |
|---|---|---|
| 24,576 | 17 GB | ~75 min of talk |
| 65,536 | 18 GB | ~3 hours |
| 131,072 | 21 GB | ~6 hours |

The KV cache is small because the architecture is hybrid attention, so long
meetings do not need chunking. Loading the model does evict the desktop's
own GPU buffers (about 10 GB of them), which is fine but visible.

Quality against the cached `gemma4:12b` analysis of the same meeting:

- The 27B named all three participants and produced a much fuller, better
  structured analysis. gemma4 left one participant as `Speaker_02`, put raw
  labels in the action-item table, and signed the email `[Your Name]`.
- Both models guessed the **wrong person as the recorder**, so the follow-up
  email is written from the other side of the table. In `ml` mode the
  transcript has no `You` label, and the prompt still says "You is the
  recorder", so the model has nothing to go on.
- The 27B turned one ASR mishearing into a confident fake product name. A
  glossary in the prompt is the fix; the same trick already works for the
  dictation polisher.
- Speaker identification without a roster swapped the two main speakers.
  With the roster, and the prompt's rule that "the first name is the
  recorder", it anchored the dominant acoustic cluster to the recorder,
  which was wrong. With the recorder described but **not** tied to a label,
  it got the majority mapping right for both main speakers, but then split
  acoustic clusters line by line and made them noisier than the diarizer had
  left them.

Conclusion: the model is good enough to replace gemma4 today. The prompts are
the weak part, and the identify pass is solving the wrong problem in `ml`
mode.

## The update

1. **Point Verbatim at this machine's Ollama.** Defaults become
   `VERBATIM_LOCAL_URL=http://127.0.0.1:11434` and
   `VERBATIM_LOCAL_MODEL=qwen3.6:27b`. Keep the tunnel unit and the GPU-node
   path as the documented fallback for laptops.
2. **Always send `num_ctx`.** `_llm_local()` sends no `num_ctx`, so it has
   been relying on `OLLAMA_CONTEXT_LENGTH=24576` set on the GPU node. This
   machine's Ollama does not set it, so the default (4,096) would silently
   truncate every transcript. Size it from the prompt length (about 1.3
   tokens per word, plus 4k for the answer), round up, cap at 131,072.
   Also send `keep_alive` of about 10 minutes so analysis and identify
   share one model load instead of paying 21 s twice.
3. **A recorder profile.** `~/.config/verbatim/profile.md`: the recorder's
   name and role, the company, regular colleagues, and a glossary of
   mishearings in the `Term | misheard, variants` form the polisher uses.
   Inject it into both prompts. This fixes the wrong-side email and the
   invented product name. The `--who` roster stays per meeting.
4. **Name clusters, do not re-split them.** In `ml` mode ask the model for
   one name per `SPEAKER_nn` label with a one-line reason, and only fall back
   to the line-by-line run splitting when diarization was `simple` (one
   merged `Remote`) or the model flags a cluster as mixed. Say explicitly in
   the prompt that no label marks the recorder.
5. **Re-transcribe from the archive.** `verbatim retranscribe <id>` runs
   `moss-diarize` (joint transcription and diarization, one model) on the
   archived Opus and swaps the transcript in. This is the repair path for
   meetings whose live diarization was poor, and the recovery path if the
   live transcription ever fails again.
6. **Verify 1.0.1 and keep the mode switch.** Run the loopback test. Keep
   `enter_meeting_mode()` unless the test shows the daemon now handles timed
   segments while streaming. Check whether `retain_audio` does anything yet;
   Verbatim's own ffmpeg archive stays either way.
7. **Refresh `docs/LOCAL-AI.md`** for the new default and the fallback.

## Loopback test (run before the next real meeting)

Do this when no call is in progress. It restarts the dictation daemon twice
and records the default sink and mic for about two minutes.

```sh
espeak-ng -v en-us -s 160 -w /tmp/a.wav "$(printf 'Alpha speaking. %.0s' $(seq 40))"
espeak-ng -v en-gb -s 160 -w /tmp/b.wav "$(printf 'Bravo replying. %.0s' $(seq 40))"
verbatim start "loopback test" --ml      # blocks until recording
paplay /tmp/a.wav; paplay /tmp/b.wav      # plays into the default sink, captured as the far end
sleep 45                                  # let the 30 s chunker close
verbatim stop --no-ai --no-open
verbatim show latest | head -40           # expect timestamped lines, two speakers
verbatim delete latest
```

A `⚠ RECORDING FAILED` at stop, or an empty transcript, means meeting mode
is broken under 1.0.1 and the mode switch needs revisiting. Confirm
`[parakeet] streaming = true` is back in `config.toml` afterwards.

## Notes for whoever implements this

- Restart `verbatim.service` and the tray after any code change; both import
  `core` once and keep running the old module.
- The autocommit timer sweeps and pushes every 20 minutes. Commit with a real
  message before it does.
- This machine runs close to its memory ceiling. The model lives in VRAM, so
  loading it did not move system RAM in the test, but check `free -h` before
  adding anything else that is resident.
