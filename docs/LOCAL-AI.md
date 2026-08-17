# Local AI processing (default)

Verbatim's AI passes — the Fireflies-style **analysis** and **speaker
identification** — run against a **local model on our own hardware** by
default. Transcripts never leave the network. The Claude Code CLI path still
exists but is opt-in (`--claude` or `VERBATIM_AI_ENGINE=claude`).

## Architecture

```
verbatim (this box)                        gpu-node (RTX 4080, 12GB)
┌──────────────────────┐   SSH tunnel      ┌───────────────────────────┐
│ analyze()/identify() │ ────────────────▶ │ ollama serve (user unit)  │
│ POST /v1/chat/...    │  127.0.0.1:11435  │ qwen3.5:9b / gemma4:12b   │
└──────────────────────┘  → gpu-node:11434 └───────────────────────────┘
```

- Any **OpenAI-compatible** server works (Ollama, llama.cpp server, vLLM).
- The client is stdlib-only (`urllib`), keeping Verbatim's zero-pip-deps rule.
- `gpu-node` is an `~/.ssh/config` host alias — define it on each box that
  installs the tunnel unit, pointing at your GPU machine.

## Configuration (env vars)

| Var | Default | Meaning |
|-----|---------|---------|
| `VERBATIM_AI_ENGINE` | `local` | `local` or `claude`. No silent fallback between them — if the transcript must stay local, it stays local. |
| `VERBATIM_LOCAL_URL` | `http://127.0.0.1:11435` | Base URL of the OpenAI-compatible server (the SSH tunnel to the GPU node). Point at `http://gpu-node:11434` directly if the firewall port is opened. |
| `VERBATIM_LOCAL_MODEL` | `gemma4:12b` | Model tag. `qwen3.5:9b` is also pulled as an alternative. |

## Model choice (researched + tested 2026-08)

Hardware target: the GPU node (RTX 4080 Laptop-class, **12 GB VRAM**). Candidates were
shortlisted by research (grok report, 2026-08-07), then decided by head-to-head
testing on a constructed multi-speaker transcript with known ground truth:

| Model | Analysis | Speaker attribution | Verdict |
|-------|----------|--------------------|---------|
| **`gemma4:12b`** (Q4_K_M, ~7.6 GB) | Clean, accurate, **17 s** | **Perfect** — split a merged "Remote" into the right 3 people, 11 s | **Default** |
| `qwen3.5:9b` (thinking off) | Good, some mixed-up attributions | Wrong — lumped far-end lines into the recorder, leaked "Remote" | Pulled, not default |
| `qwen3.5:9b` (thinking on) | — | 7 min / 23k tokens of rumination, no parseable JSON | Unusable here |

The client disables thinking (`think: false` via Ollama's native API) — with a
reasoning model left in thinking mode, a single pass burns minutes and
thousands of tokens.

Rejected at research stage: Phi-4 (16k context ceiling), Mistral Small 24B
(doesn't fit 12 GB), Llama 3.1 8B (superseded on instruction-following/JSON).

## The pieces

### On the GPU node — `~/.config/systemd/user/ollama.service`

User-level unit (no sudo on the GPU node), lingering enabled so it survives
logout/reboot. Key environment:

```
OLLAMA_HOST=0.0.0.0:11434     # LAN-ready if the firewall is ever opened
OLLAMA_FLASH_ATTENTION=1
OLLAMA_KV_CACHE_TYPE=q8_0     # halves KV cache → long transcripts stay on-GPU
OLLAMA_CONTEXT_LENGTH=24576   # default num_ctx for every request
OLLAMA_KEEP_ALIVE=30m
```

Manage it: `ssh gpu-node systemctl --user {status,restart} ollama`.

### On this box — `~/.config/systemd/user/verbatim-ai-tunnel.service`

Persistent `ssh -N -L 127.0.0.1:11435 → gpu-node:11434` tunnel (auto-restart).
The GPU node's firewall blocks 11434 from the LAN and there's no sudo there, so the
tunnel is the transport; it's also encrypted, which direct LAN traffic isn't.

To go direct instead (needs someone with sudo on the GPU node):

```bash
sudo firewall-cmd --add-port=11434/tcp --permanent && sudo firewall-cmd --reload
# then: export VERBATIM_LOCAL_URL=http://gpu-node:11434
```

## Troubleshooting

- **"local AI server unreachable"** — tunnel down? `systemctl --user restart
  verbatim-ai-tunnel`. GPU node down? `ssh gpu-node systemctl --user status ollama`.
- **"is model … pulled?"** — `ssh gpu-node /usr/bin/ollama pull qwen3.5:9b`.
- **Slow first request** — the model loads into VRAM on first use (~10–20 s),
  then stays warm for `OLLAMA_KEEP_ALIVE` (30 min).
- **Want the old cloud behaviour** — `verbatim ai latest --claude`, or export
  `VERBATIM_AI_ENGINE=claude`.
