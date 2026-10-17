# Running Butler on Nebius (Nebius x NVIDIA hackathon)

Rule summary (re-verify on the official Devpost rules page before you submit): the project must run on **Nebius Token Factory
or Nebius AI Cloud** and use **at least one NVIDIA open-source model** (e.g. a Nemotron model), in a track such as Personal AI.
Public repo + OSI license (this repo: MIT), README explaining the NVIDIA/Nebius use and setup, <=3 min public YouTube demo,
feedback on the Nebius/NVIDIA tools you used. A third-party recap lists the deadline as Oct 30, 2026 10:00 PDT: confirm.

## Minimum qualifying path (what Butler supports today)
1. Create a Token Factory API key at Nebius and pick an NVIDIA Nemotron model id from the Token Factory catalogue.
2. In Telegram: `/addkey nebius` -> enter that model id and key on the secure page. `/models` shows `[NVIDIA]` next to it.
3. `/modelorder default nebius` so Nebius is tried first, then use Butler normally (chat, `/do`, `/task`).
4. `python -m butler.cli hackathon` prints evidence from the call ledger: successful Token Factory calls per NVIDIA model.
   Put that output (and the model id) in your README / submission.

## Optional: host the services on Nebius AI Cloud (not required by the rules; untested here)
The rules mention Serverless Endpoints / Jobs / DevPods as qualifying compute. The compose file is the reference layout:
`web` (HTTPS endpoint), `bot`, `worker` (+`beat`) from the same image, with Postgres, Redis and FalkorDB as managed or
sidecar services. Wire the same environment variables from `.env.example`.
