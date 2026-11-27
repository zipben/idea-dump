# Idea Dump

A self-hosted capture inbox you message over **Signal**. A small local LLM decides whether each
message is an **idea** (filed into a category, numbered `#12`) or a short-term **to-do** (`t3`),
saves it, and replies. Correct it in plain words ("wrong, put it in Gifts", "that's a to-do",
"done t3") and it learns from your corrections. A password-protected **website** shows ideas and
to-dos side by side, with link preview images and editing.

```
phone (Signal) ──> idea-signal-api  (signal-cli-rest-api, 127.0.0.1:8099)
                        │ websocket
                        ▼
                   idea-bot  (Python, host network) ──> Ollama (user service, 127.0.0.1:11434, NVIDIA GPU)
                        │
                        ▼
                   data/ideas.db (SQLite) <── idea-web (Python stdlib, :9001)
```

| Piece | What | Where |
|---|---|---|
| `ollama/` | Ollama as a **systemd user service**, pinned to the NVIDIA GPU (Vulkan backend removed so it can't touch an iGPU used by Plex) | `~/.local/ollama` |
| `bot/` | Signal ↔ LLM bot, link previews (`og:image`, YouTube via oEmbed) | container `idea-bot` |
| `web/` | Viewer/editor, stdlib only, plain HTML forms, no JS | container `idea-web`, port 9001 |
| signal | `bbernhard/signal-cli-rest-api:0.102`, json-rpc mode | container `idea-signal-api` |

All containers run with `oom_score_adj: 900` plus memory/CPU caps, so on a shared server
the OOM killer (or earlyoom) picks them before anything important.

## Requirements
- Linux, Docker with Compose v2, an NVIDIA GPU with ~3GB free VRAM and the NVIDIA driver
  (CPU-only works too, just slower; remove `num_gpu` in `bot/llm.py`)
- `zstd`, `curl`, `sqlite3` (only for backups)
- A **second phone number** for the bot that can receive one SMS or call (Google Voice works)

## Deploy from scratch
```bash
git clone <this repo> ~/idea-dump && cd ~/idea-dump

# 1. Model (no sudo needed except linger)
sudo loginctl enable-linger $USER          # once: user services start at boot
./ollama/install-ollama.sh                 # downloads Ollama v0.40.17 + qwen3:4b-instruct (~4GB)

# 2. Config
cp .env.example .env && $EDITOR .env       # BOT_NUMBER, ALLOWED_SENDERS

# 3. Signal registration (one time)
docker compose up -d idea-signal-api
./register.sh request                      # needs a captcha link, see below
./register.sh verify 123456                # code texted to BOT_NUMBER
./register.sh finish 12345678              # registration-lock PIN + profile name; keep the PIN

# 4. Everything else
docker compose up -d --build
./set-password.sh                          # website password
```
Then message the bot's number on Signal: `help`. Open `http://<server>:9001`.

**Captcha:** open <https://signalcaptchas.org/registration/generate.html>, solve it, and copy the
`signalcaptcha://…` link from the "Open Signal" button (right-click → copy link). If the browser
won't give you the link, launch Chrome with `--remote-debugging-port=9333`, solve the captcha there, and
read it from the page with `hcaptcha.getResponse()` over the DevTools protocol. The link is
`signalcaptcha://signal-hcaptcha.<sitekey>.registration.<token>`.

**Hidden phone number:** if the bot ignores you, Signal is hiding your number from it. The
bot logs `ignoring message from unlisted sender … uuid=<UUID>`. Add that UUID to `ALLOWED_SENDERS`
and run `docker compose up -d idea-bot`.

## Using it
Send `help` to the bot for the full list. Highlights: `todo`, `todo: <task>`, `idea: <text>`,
`describe <category>: <what belongs there>` (also editable on the category page),
`done t3`, `undone t3`, `cats`, `ls <category>`, `find <words>`, `mv [#N] <category>`,
`rm #N` / `rm t3`, `undo`. Natural language works for corrections and check-offs.

## Operations
```bash
docker compose logs -f idea-bot
systemctl --user status ollama
./set-password.sh                                       # change web password (signs everyone out)
docker compose restart idea-web                         # clears a login lockout
sqlite3 data/ideas.db ".backup backups/ideas-$(date +%F).db"
```
Tests (throwaway DB, real model):
```bash
docker compose run --rm --no-deps -e DB_PATH=/tmp/t.db -e BOT_NUMBER=x idea-bot python test_flow.py
docker compose run --rm --no-deps -e DB_PATH=/tmp/t.db -e BOT_NUMBER=x idea-bot python test_todo.py
```

## Including it in another compose stack
Add this to the parent compose file (Compose ≥ 2.20):
```yaml
include:
  - /path/to/idea-dump/docker-compose.yml
```
Paths resolve relative to this repo, so `data/` etc. stay here.

## What's not in git (runtime state, back these up)
| Path | Contents |
|---|---|
| `.env` | bot number, your number/UUID |
| `data/` | `ideas.db` (ideas, to-dos, corrections), `thumbs/` |
| `signal/` | Signal account keys for the bot number. **Losing this means re-registering.** |
| `signal-pin.txt` | registration-lock PIN |
| `web-auth/` | website password hash + session key |

## Data model (`data/ideas.db`)
`categories`, `items` (ideas; `url`, `url_title`, `thumb`), `corrections` (category fixes; fed
back to the model), `todos`, `kind_corrections` (idea↔to-do fixes; fed back to the model).
