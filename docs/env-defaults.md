# Environment Defaults

This overview answers two slightly different questions:

- **Without environment variables or Compose defaults:** Which values does
  Dockkeep use on its own when a variable is missing from the process
  environment?
- **With the current `docker-compose.yml` and a copied, unchanged `.env` from
  `.env.example`:** Which values are set in the example deployment when optional
  values remain commented out in `.env`?

One important Compose detail: `.env` is only loaded into the container
automatically when `env_file: .env` is set. The current Compose file uses
exactly that path. The `environment` block now only sets the example defaults
for `TZ` and `DK_MODE`.

## Summary

- In the Docker entrypoint, `RCLONE_CONFIG` always falls back to
  `$DK_CONFIG_DIR/rclone.conf`. With no further settings, that is
  `/config/rclone.conf`.
- `TZ` has no Dockkeep code-level default. Without `TZ`, Python uses the local
  timezone of the container/system, which is usually UTC in standard containers.
  The example Compose file deliberately sets `Europe/Berlin`.
- `PUID` and `PGID` have no defaults. If neither is set, the container runs as
  `root`; setting only one of them is a startup error.
- Credentials and providers have no built-in secrets or defaults.
- Inline hooks are disabled unless `DK_ALLOW_INLINE_HOOKS=true` is set. An
  explicit `false` is not required.
- AppData retention is disabled without environment values. In that case, no
  automatic AppData cleanup is performed.

## Compose-Policy

In `docker-compose.yml`, `.env` is loaded into the container via `env_file`.
The `environment` block is intentionally small and contains only the defaults
that the example deployment should actively set.

| Variable | Compose decision | Rationale |
|---|---|---|
| `TZ` | set with default `Europe/Berlin` | Gives the example local cron and next-run times instead of the more common UTC |
| `DK_MODE` | set with default `gui` | Documents the Docker default mode and starts the UI plus scheduler |
| `.env` as a whole | load via `env_file: .env` | All deliberately uncommented values from `.env` are available inside the container |
| `DK_RESTIC_PASSWORD`, `DK_RESTIC_PASSWORD_SECONDARY` | document only in `.env.example` | `env_file` passes them into the container when they are uncommented in `.env` |
| `SMTP_USER`, `SMTP_PASSWORD`, `PUSHOVER_TOKEN`, `PUSHOVER_USER_KEY` | document only in `.env.example` | Provider credentials are only needed when the config references them |
| `DK_ALLOW_INLINE_HOOKS` | document only in `.env.example` | The safe default stays off; only the exact value `true` enables inline hooks |
| `DK_APPDATA_RETENTION_DAYS`, `DK_APPDATA_RETENTION_COUNT` | document only in `.env.example` | AppData retention stays opt-in |
| `PUID`, `PGID` | document only in `.env.example` | With neither value set, the container runs as root; with both set, the entrypoint uses `gosu`; setting only one is a startup error |
| `RCLONE_CONFIG` | do not set in Compose | The entrypoint automatically sets `$DK_CONFIG_DIR/rclone.conf`, so the default is `/config/rclone.conf` |
| `DK_CONFIG_DIR`, `DK_LOG_DIR`, `DK_BACKUP_DIR`, `DK_LOCK_DIR`, `DK_SCRIPTS_DIR`, `DK_RESTORE_DIR`, `DK_APPDATA_DIR` | do not set in Compose | Container paths are represented through volumes and entrypoint defaults |
| `DK_STATS_TIMEOUT`, `DK_BROWSE_TIMEOUT`, `DK_NOTIFICATION_TIMEOUT`, `DK_SIGTERM_GRACE_PERIOD` | do not set in Compose | Code defaults are sufficient; overrides belong in a local customization only when needed |

## `.env.example`-Policy

In `.env.example`, only values that should truly act as defaults for a local
example setup are enabled. Secrets and optional switches remain commented out so
a copied `.env` does not accidentally activate behavior.

| Variable | `.env.example` decision | Rationale |
|---|---|---|
| `DK_MODE` | enabled with `gui` | Matches the Docker default mode |
| `TZ` | enabled with `Europe/Berlin` | Matches the example Compose file and makes cron times locally predictable |
| `DK_RESTIC_PASSWORD`, `DK_RESTIC_PASSWORD_SECONDARY` | commented out | Passwords should be set deliberately or replaced with `password_file` |
| `SMTP_USER`, `SMTP_PASSWORD`, `PUSHOVER_TOKEN`, `PUSHOVER_USER_KEY` | commented out | Provider credentials only matter when the config references them |
| `DK_ALLOW_INLINE_HOOKS` | commented out | The safe default is off; only an intentional `true` enables inline hooks |
| `DK_APPDATA_RETENTION_DAYS`, `DK_APPDATA_RETENTION_COUNT` | commented out | AppData retention is opt-in |
| `PUID`, `PGID` | commented out | With neither value set, the container runs as root; setting only one is a startup error |

## Paths

| Variable | Dockkeep default when env is missing | Current Compose with copied `.env` from `.env.example` | Note |
|---|---:|---:|---|
| `DK_CONFIG_DIR` | `/config` | not set | Directory for `config.toml`; the entrypoint creates it |
| `DK_LOG_DIR` | `/logs` | not set | Base directory for job and system logs; the entrypoint creates it |
| `DK_BACKUP_DIR` | `/backups` | not set | Default backup directory; the entrypoint creates it, no `chown` |
| `DK_LOCK_DIR` | `/var/lock` | not set | Resource and scheduler locks; the entrypoint creates it |
| `DK_SCRIPTS_DIR` | `/scripts` | not set | CWD and allowed root path for script hooks; the entrypoint creates it |
| `DK_RESTORE_DIR` | `/restore` | not set | Base directory for restore targets; the entrypoint creates it |
| `DK_APPDATA_DIR` | `/appdata` | not set | SQLite AppData, run-control socket, restore registry, and stats; the entrypoint creates it |
| `RCLONE_CONFIG` | Docker entrypoint: `$DK_CONFIG_DIR/rclone.conf`; without entrypoint: `config_dir/rclone.conf` | not set, but exported by the entrypoint | With the default `DK_CONFIG_DIR`: `/config/rclone.conf` |

## Runtime

| Variable | Dockkeep default when env is missing | Current Compose with copied `.env` from `.env.example` | Note |
|---|---:|---:|---|
| `DK_MODE` | `gui` | `gui` | `gui` starts `dk-runtime gui` (managed mode with AppData persistence; user CLI limited to `dk shell` and `dk config validate`); `cli` starts the non-persistent `dk-runtime scheduler` and enables the full `dk` command set; invalid values fall back to `gui`. Maintainer note: `DK_MODE` is evaluated in two places (`docker-entrypoint.sh` and `_resolve_dk_mode()` in `src/main.py`) and must remain semantically aligned (invalid -> GUI + warning) |
| `TZ` | no Dockkeep code-level default | `Europe/Berlin` | Without Compose/env, the container/system timezone applies, usually UTC |
| `PUID` | not set | not set while commented out in `.env` | With neither value set, `gosu` is not used and the process runs as root; setting only `PUID` or only `PGID` aborts startup |
| `PGID` | not set | not set while commented out in `.env` | Evaluated only together with `PUID`; setting only one of the pair aborts startup |

## Credentials and Providers

These values are only examples that can be referenced from `config.toml` via
`*_env`. Dockkeep has no built-in default secrets for them.

| Variable | Dockkeep default when env is missing | Current Compose with copied `.env` from `.env.example` | Note |
|---|---:|---:|---|
| `DK_RESTIC_PASSWORD` | not set | not set while commented out in `.env` | Example for `password_env = "DK_RESTIC_PASSWORD"` |
| `DK_RESTIC_PASSWORD_SECONDARY` | not set | not set while commented out in `.env` | Second example for another repository/backup |
| `SMTP_USER` | not set | not set while commented out in `.env` | Example for mail `username_env` |
| `SMTP_PASSWORD` | not set | not set while commented out in `.env` | Example for mail `password_env` |
| `PUSHOVER_TOKEN` | not set | not set while commented out in `.env` | Example for Pushover `token_env` |
| `PUSHOVER_USER_KEY` | not set | not set while commented out in `.env` | Example for Pushover `user_key_env` |

A missing or empty credential value is treated as absent. For Restic, a mounted
password file can be used instead via `password_file`.

## Switches and AppData Retention

| Variable | Dockkeep default when env is missing | Current Compose with copied `.env` from `.env.example` | Note |
|---|---:|---:|---|
| `DK_ALLOW_INLINE_HOOKS` | not set; inline hooks are off | not set while commented out in `.env` | Only the exact value `true` allows inline hook commands |
| `DK_APPDATA_RETENTION_DAYS` | not set; no age-based AppData cleanup | not set while commented out in `.env` | A positive number enables age-based cleanup |
| `DK_APPDATA_RETENTION_COUNT` | not set; no count-based AppData cleanup | not set while commented out in `.env` | A positive number enables count-based cleanup |

If only one of the two AppData retention variables is set, only that one limit
applies. If both are empty or unset, run history, terminal restore records, and
removed backup artifact data are not deleted automatically through AppData
retention.

## Timeout-Overrides

These variables are intentionally not included in `.env.example` or the current
Compose file. The code uses defaults and falls back to them for empty or invalid
values.

| Variable | Dockkeep default when env is missing | Current Compose with copied `.env` from `.env.example` | Note |
|---|---:|---:|---|
| `DK_STATS_TIMEOUT` | `600` | not set | Timeout for each follow-up `restic stats`/`restic snapshots` command |
| `DK_BROWSE_TIMEOUT` | `30` | not set | Timeout for each `restic ls` command in the repository browser |
| `DK_NOTIFICATION_TIMEOUT` | `10` | not set | Shared mail/Pushover provider timeout |
| `DK_SIGTERM_GRACE_PERIOD` | `10` | not set | Seconds between SIGTERM and SIGKILL during subprocess cleanup |

## Sources

- `docker-entrypoint.sh`: Docker path defaults, `RCLONE_CONFIG`, `DK_MODE`,
  `PUID`/`PGID`
- `docker-compose.yml`: currently forwarded variables and Compose defaults
- `src/main.py`: `DK_CONFIG_DIR`, `DK_MODE`
- `src/utils/logging.py`: `DK_LOG_DIR`
- `src/core/locking.py`: `DK_LOCK_DIR`
- `src/core/workflow.py`: `DK_SCRIPTS_DIR`
- `src/services/run_history.py`: `DK_APPDATA_DIR`, AppData retention
- `src/services/run_control.py`: run-control socket under `DK_APPDATA_DIR`
- `src/services/restore.py`: `DK_RESTORE_DIR`
- `src/services/rclone.py`: `RCLONE_CONFIG`
- `src/utils/timeouts.py`: positive timeout environment overrides
