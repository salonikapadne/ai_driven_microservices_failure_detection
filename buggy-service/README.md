# buggy-service (code-heal demo)

Flask app with an intentional bug (`EXPECTED_MAGIC != 42`) so logs emit `[code_heal]` until `/health` succeeds.

- **Seed (immutable in image):** `seed/app.py`
- **Live (runtime):** `live/app.py` — copied from seed on every container start (`entrypoint.sh`), then executed.

The AI engine mounts `buggy-service/live` on the host into the `ai-engine` container at `CODE_HEAL_ROOT` (default `/buggy-live`) so `fix_code` can rewrite `app.py` and restart the service.

**Reset the demo:** `docker compose restart buggy-service` (or bring the stack down and up). The entrypoint copies seed → live again, restoring the buggy baseline. Do not commit edited files under `live/` — only `seed/` is source of truth for git.
