"""Logging Setup für Dockkeep.

Richtet Job-spezifische Logger mit täglich rotierenden Log-Dateien ein.
Log-Struktur: /logs/<job-name>/<datum>.log
"""

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta
from pathlib import Path

LOG_FORMAT = "%(asctime)s [%(levelname)s] [%(dk_logger_name)s]%(dk_task_context)s %(message)s"
# Time only: the date is already encoded in the per-day log filename
# (<job>/<YYYY-MM-DD>.log), so repeating it on every line is redundant.
LOG_DATE_FORMAT = "%H:%M:%S"
LOG_BASE_DIR = Path("/logs")
JOB_LOGGER_NAMESPACE = "dockkeep.jobs"
_JOB_LOGGER_PREFIX = f"{JOB_LOGGER_NAMESPACE}."
_LOG_TASK_CONTEXT: ContextVar[str | None] = ContextVar("dk_log_task_context", default=None)

# Verzeichnisname für den System-Logger unter LOG_BASE_DIR. Erfüllt das
# Job-Namensschema, damit GUI-Log-Viewer und Retention ihn wie ein Job-Verzeichnis
# behandeln. Der führende Unterstrich kennzeichnet das reservierte Verzeichnis.
SYSTEM_LOG_NAME = "_system"


def _log_base_dir_from_env() -> Path:
    return Path(os.environ.get("DK_LOG_DIR", "/logs"))


def log_base_dir() -> Path:
    return _log_base_dir_from_env()


def job_logger_name(job_name: str) -> str:
    return f"{JOB_LOGGER_NAMESPACE}.{job_name}"


@contextmanager
def log_task_context(target: str) -> Iterator[None]:
    """Attach a compact task target to job log lines in the current context.

    Nested contexts combine outer and inner targets (``outer › inner``), so
    workflow-internal steps stay findable under both the workflow target and
    their own step target.
    """
    current = _LOG_TASK_CONTEXT.get()
    token = _LOG_TASK_CONTEXT.set(f"{current} › {target}" if current else target)
    try:
        yield
    finally:
        _LOG_TASK_CONTEXT.reset(token)


class _BlankLineFormatter(logging.Formatter):
    """Formatter, der eine leere Nachricht auf den reinen Zeitstempel reduziert.

    Ermöglicht visuelle Trennung aufeinanderfolgender Runs/Steps via
    ``logger.info("")`` ohne Level-/Logger-Präfix. Der Zeitstempel bleibt
    erhalten, damit auch Trennzeilen zeitlich einordenbar bleiben und
    zeilenbasierte Log-Parser (grep, Tail-Tools, externe Shipper) nicht an
    einer komplett leeren Zeile stolpern.
    """

    def format(self, record: logging.LogRecord) -> str:
        if record.getMessage() == "":
            return self.formatTime(record, self.datefmt)
        record.dk_logger_name = _display_logger_name(record.name)
        task = _LOG_TASK_CONTEXT.get()
        record.dk_task_context = f" [{task}]" if task and _is_job_logger_name(record.name) else ""
        return super().format(record)


def log_blank_line(logger: logging.Logger) -> None:
    """Schreibt eine reine Zeitstempel-Trennzeile in die Logs des Loggers.

    Args:
        logger: Logger, dessen Handler ``_BlankLineFormatter`` verwenden
            (alle über dieses Modul eingerichteten Logger erfüllen das).
    """
    logger.info("")


def _is_job_logger_name(name: str) -> bool:
    return name == JOB_LOGGER_NAMESPACE or name.startswith(_JOB_LOGGER_PREFIX)


def _display_logger_name(name: str) -> str:
    """Shorten a job logger name for display: component name only if present.

    ``dockkeep.jobs.<job>`` (the job-root logger) displays as ``<job>``;
    ``dockkeep.jobs.<job>.<Component>`` displays as just ``<Component>``,
    since the job is already identified by the log file/viewer context.
    """
    if name.startswith(_JOB_LOGGER_PREFIX):
        remainder = name.removeprefix(_JOB_LOGGER_PREFIX)
        _job, _sep, component = remainder.partition(".")
        return component or remainder
    return name


def setup_job_logger(
    job_name: str,
    log_level: str = "debug",
    log_base_dir: Path | None = None,
) -> logging.Logger:
    resolved_base_dir = log_base_dir if log_base_dir is not None else _log_base_dir_from_env()
    numeric_level = _parse_log_level(log_level)
    logger = logging.getLogger(job_logger_name(job_name))
    logger.setLevel(numeric_level)

    formatter = _BlankLineFormatter(fmt=LOG_FORMAT, datefmt=LOG_DATE_FORMAT)

    # Logger können in Langläufern bereits existieren. In diesem Fall keine
    # Handler duplizieren, aber das Level aus der aktuellen Config übernehmen.
    # Falls sich das Datum geändert hat, FileHandler gegen einen aktuellen austauschen.
    if logger.handlers:
        today_log_file = str(
            (resolved_base_dir / job_name / f"{datetime.now().strftime('%Y-%m-%d')}.log").resolve()
        )
        for handler in list(logger.handlers):
            handler.setLevel(numeric_level)
            if isinstance(handler, logging.FileHandler) and handler.baseFilename != today_log_file:
                handler.close()
                logger.removeHandler(handler)
                log_dir = resolved_base_dir / job_name
                log_dir.mkdir(parents=True, exist_ok=True)
                new_file_handler = logging.FileHandler(today_log_file, encoding="utf-8")
                new_file_handler.setLevel(numeric_level)
                new_file_handler.setFormatter(formatter)
                logger.addHandler(new_file_handler)
        logger.propagate = False
        return logger

    console_handler = logging.StreamHandler()
    console_handler.setLevel(numeric_level)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    log_dir = resolved_base_dir / job_name
    if log_dir.resolve().parent != resolved_base_dir.resolve():
        raise ValueError(f"Invalid job name '{job_name}': would escape the log directory")
    log_dir.mkdir(parents=True, exist_ok=True)

    log_file = log_dir / f"{datetime.now().strftime('%Y-%m-%d')}.log"
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(numeric_level)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    logger.propagate = False

    return logger


def setup_root_logger(log_level: str = "debug") -> logging.Logger:
    """Richtet den Root-Logger für System-Level-Logging ein.

    Wird für Meldungen verwendet die keinem spezifischen Job zugeordnet sind,
    z.B. Scheduler-Events oder Config-Loading.

    Args:
        log_level: Log-Level als String (debug, info, warning, error, critical)

    Returns:
        Konfigurierter Root-Logger.

    Example:
        >>> logger = setup_root_logger("info")
        >>> logger.info("Scheduler started")
    """
    numeric_level = _parse_log_level(log_level)
    logger = logging.getLogger("dockkeep")
    logger.setLevel(numeric_level)

    if logger.handlers:
        for handler in logger.handlers:
            handler.setLevel(numeric_level)
        logger.propagate = False
        return logger

    formatter = _BlankLineFormatter(fmt=LOG_FORMAT, datefmt=LOG_DATE_FORMAT)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(numeric_level)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    logger.propagate = False

    return logger


class _DailyFileHandler(logging.FileHandler):
    """FileHandler der nach ``<dir>/<datum>.log`` schreibt und tagesweise rolliert.

    Behält dasselbe ``YYYY-MM-DD.log``-Namensschema wie Job-Logs, damit der
    GUI-Log-Viewer und die Retention das System-Log-Verzeichnis exakt wie ein
    Job-Verzeichnis behandeln. Da der System-Logger – anders als Job-Logger –
    nicht pro Lauf neu eingerichtet wird, prüft ``emit()`` bei jedem Record das
    Datum und öffnet bei Tageswechsel die neue Datei.
    """

    def __init__(self, log_dir: Path) -> None:
        """Initialisiert den Handler mit der heutigen Logdatei.

        Args:
            log_dir: Verzeichnis, in dem die ``<datum>.log``-Dateien liegen.
        """
        self._log_dir = log_dir
        self._current_date = datetime.now().strftime("%Y-%m-%d")
        super().__init__(self._path_for(self._current_date), encoding="utf-8", delay=True)

    def _path_for(self, day: str) -> str:
        return str(self._log_dir / f"{day}.log")

    def emit(self, record: logging.LogRecord) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        if today != self._current_date:
            self._current_date = today
            if self.stream is not None:
                self.stream.flush()
                self.stream.close()
                self.stream = None
            self.baseFilename = os.path.abspath(self._path_for(today))
        super().emit(record)


def _ensure_root_console_handler(
    root: logging.Logger, formatter: logging.Formatter, level: int
) -> None:
    for handler in root.handlers:
        if getattr(handler, "_dk_console", False):
            handler.setLevel(level)
            handler.setFormatter(formatter)
            return
    console = logging.StreamHandler()
    setattr(console, "_dk_console", True)
    console.setLevel(level)
    console.setFormatter(formatter)
    root.addHandler(console)


def _ensure_system_file_handler(
    root: logging.Logger, log_base_dir: Path, formatter: logging.Formatter, level: int
) -> None:
    """Stellt genau einen markierten System-FileHandler am Root-Logger sicher.

    Best-effort: Lässt sich das Verzeichnis nicht anlegen, bleibt es bei
    Konsolenausgabe. Zeigt ein vorhandener Handler auf ein anderes Verzeichnis
    (z. B. in Tests), wird er ersetzt.
    """
    log_dir = log_base_dir / SYSTEM_LOG_NAME
    target = str(log_dir.resolve())
    for handler in list(root.handlers):
        system_dir = getattr(handler, "_dk_system_dir", None)
        if system_dir is not None:
            if system_dir == target:
                handler.setLevel(level)
                handler.setFormatter(formatter)
                return
            handler.close()
            root.removeHandler(handler)
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = _DailyFileHandler(log_dir)
    except OSError:
        logging.getLogger("dockkeep").warning(
            "Could not set up system log file in %s; logging to console only", log_dir
        )
        return
    setattr(handler, "_dk_system_dir", target)
    handler.setLevel(level)
    handler.setFormatter(formatter)
    root.addHandler(handler)


def setup_system_logger(
    log_level: str = "debug",
    log_base_dir: Path | None = None,
) -> logging.Logger:
    """Richtet den prozessweiten System-Logger (Datei + Konsole) ein.

    Installiert idempotent einen Konsolen-Handler und einen tagesrotierenden
    File-Handler unter ``<log_base_dir>/_system/<datum>.log`` am Root-Logger.
    Dadurch landet jeder Record, der **nicht** über einen Job-Logger läuft
    (Scheduler, Config-Loading, Locking, Services, GUI, Drittbibliotheken), in
    einer persistenten Datei und ist über den GUI-Log-Viewer lesbar. Die
    Retention teilt sich denselben Mechanismus wie Job-Logs
    (:func:`cleanup_all_old_logs`).

    Der benannte ``dockkeep``-Logger wird über die Root-Handler
    geführt, damit bestehende Scheduler-/CLI-Aufrufstellen unverändert
    funktionieren. Die File-Handler-Einrichtung ist best-effort: Ist das
    Verzeichnis nicht beschreibbar, wird nur auf die Konsole geloggt.

    Args:
        log_level: Log-Level als String (debug, info, warning, error, critical).
        log_base_dir: Basis-Verzeichnis für Log-Dateien (Standard: /logs).

    Returns:
        Den ``dockkeep``-Logger, der zu den Root-Handlern propagiert.
    """
    resolved_base_dir = log_base_dir if log_base_dir is not None else _log_base_dir_from_env()
    numeric_level = _parse_log_level(log_level)
    formatter = _BlankLineFormatter(fmt=LOG_FORMAT, datefmt=LOG_DATE_FORMAT)
    root = logging.getLogger()
    root.setLevel(numeric_level)

    _ensure_root_console_handler(root, formatter, numeric_level)
    _ensure_system_file_handler(root, resolved_base_dir, formatter, numeric_level)

    # Den benannten Logger über die gemeinsamen Root-Handler führen, damit es
    # keine doppelte Konsolenausgabe gibt.
    named = logging.getLogger("dockkeep")
    named.setLevel(numeric_level)
    for handler in list(named.handlers):
        handler.close()
        named.removeHandler(handler)
    named.propagate = True
    return named


def cleanup_old_logs(
    job_name: str, retention_days: int | None, log_base_dir: Path | None = None
) -> int:
    if retention_days is None:
        return 0
    resolved_base_dir = log_base_dir if log_base_dir is not None else _log_base_dir_from_env()
    log_dir = resolved_base_dir / job_name
    if not log_dir.exists():
        return 0

    cutoff_date = datetime.now() - timedelta(days=retention_days)
    deleted = 0

    for log_file in log_dir.glob("*.log"):
        try:
            file_date = datetime.strptime(log_file.stem, "%Y-%m-%d")
            if file_date < cutoff_date:
                log_file.unlink()
                deleted += 1
        except ValueError:
            continue

    return deleted


def cleanup_all_old_logs(retention_days: int | None, log_base_dir: Path | None = None) -> int:
    """Löscht alte Log-Dateien für alle Jobs.

    Iteriert über alle Job-Verzeichnisse unter log_base_dir und entfernt
    Dateien die älter als retention_days sind.

    Args:
        retention_days: Anzahl der Tage die Logs aufbewahrt werden; None = keine Bereinigung
        log_base_dir: Basis-Verzeichnis für Log-Dateien (Standard: /logs)

    Returns:
        Gesamtanzahl der gelöschten Log-Dateien.

    Example:
        >>> deleted = cleanup_all_old_logs(retention_days=30)
        >>> print(f"Total deleted: {deleted}")
    """
    if retention_days is None:
        return 0
    resolved_base_dir = log_base_dir if log_base_dir is not None else _log_base_dir_from_env()
    if not resolved_base_dir.exists():
        return 0

    total_deleted = 0
    for job_dir in resolved_base_dir.iterdir():
        if job_dir.is_dir():
            total_deleted += cleanup_old_logs(job_dir.name, retention_days, resolved_base_dir)

    return total_deleted


def _parse_log_level(log_level: str) -> int:
    """Konvertiert Log-Level-String zu Python-Logging-Konstante.

    Args:
        log_level: Log-Level als String (debug, info, warning, error, critical)

    Returns:
        Numerischer Log-Level.

    Raises:
        ValueError: Wenn log_level kein gültiger Level ist.
    """
    numeric_level = getattr(logging, log_level.upper(), None)
    if not isinstance(numeric_level, int):
        raise ValueError(f"Invalid log level: {log_level!r}")
    return numeric_level
