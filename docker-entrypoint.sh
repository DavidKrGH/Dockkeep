#!/bin/bash
set -e

DK_CONFIG_DIR="${DK_CONFIG_DIR:-/config}"
DK_LOG_DIR="${DK_LOG_DIR:-/logs}"
DK_BACKUP_DIR="${DK_BACKUP_DIR:-/backups}"
DK_LOCK_DIR="${DK_LOCK_DIR:-/var/lock}"
DK_SCRIPTS_DIR="${DK_SCRIPTS_DIR:-/scripts}"
DK_RESTORE_DIR="${DK_RESTORE_DIR:-/restore}"
DK_APPDATA_DIR="${DK_APPDATA_DIR:-/appdata}"
export RCLONE_CONFIG="${RCLONE_CONFIG:-${DK_CONFIG_DIR}/rclone.conf}"

mkdir -p "$DK_CONFIG_DIR" "$DK_LOG_DIR" "$DK_BACKUP_DIR" "$DK_LOCK_DIR" "$DK_SCRIPTS_DIR" "$DK_RESTORE_DIR" "$DK_APPDATA_DIR"

# Seed an empty config so the GUI can start on a fresh mount.
CONFIG_FILE="${DK_CONFIG_DIR}/config.toml"
if [ ! -f "$CONFIG_FILE" ]; then
    echo "INFO: Keine Konfiguration gefunden unter $CONFIG_FILE."
    touch "$CONFIG_FILE"
    echo "INFO: Leere Konfiguration wurde unter $CONFIG_FILE angelegt."
    echo "INFO: Bitte Konfiguration über die GUI oder Datei anpassen."
fi

if [ "$#" -eq 0 ]; then
    case "${DK_MODE:-gui}" in
        gui)
            set -- dk-runtime gui
            ;;
        cli)
            set -- dk-runtime scheduler
            ;;
        *)
            echo "WARN: invalid DK_MODE '${DK_MODE}', falling back to gui." >&2
            set -- dk-runtime gui
            ;;
    esac
fi

if [ "${1:-}" = "dk" ] || [ "${1:-}" = "dk-runtime" ]; then
    ENTRYPOINT_BIN="${1:-}"
    shift
else
    ENTRYPOINT_BIN="dk"
fi

if [ -n "${PUID}" ] || [ -n "${PGID}" ]; then
    if [ -z "${PUID}" ] || [ -z "${PGID}" ]; then
        echo "ERROR: PUID and PGID must be set together; refusing to silently fall back to root." >&2
        exit 1
    fi

    groupadd -o -g "${PGID}" appuser 2>/dev/null || true
    useradd -o -u "${PUID}" -g "${PGID}" -M -s /bin/bash appuser 2>/dev/null || true

    echo "WARN: PUID/PGID non-root mode enabled (${PUID}:${PGID})." >&2
    echo "WARN: Dockkeep does not recursively chown or chmod mounted data directories." >&2
    echo "WARN: Ensure the host paths mounted as $DK_CONFIG_DIR, $DK_LOG_DIR, $DK_BACKUP_DIR, $DK_LOCK_DIR, $DK_SCRIPTS_DIR, $DK_RESTORE_DIR and $DK_APPDATA_DIR are readable and writable by ${PUID}:${PGID}." >&2
    echo "WARN: Example host command: sudo chown -R ${PUID}:${PGID} <config-dir> <logs-dir> <backups-dir> <scripts-dir> <restore-dir> <appdata-dir>" >&2
    echo "WARN: If you manage access with permissions or ACLs instead, grant this UID/GID read, write and directory traversal rights." >&2

    if [ "$ENTRYPOINT_BIN" = "dk-runtime" ]; then
        exec gosu "${PUID}:${PGID}" python -m src.runtime "$@"
    else
        exec gosu "${PUID}:${PGID}" python -m src.main "$@"
    fi
else
    if [ "$ENTRYPOINT_BIN" = "dk-runtime" ]; then
        exec python -m src.runtime "$@"
    else
        exec python -m src.main "$@"
    fi
fi
