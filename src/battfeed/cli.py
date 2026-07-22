"""Command-line interface: ``battfeed sources``, ``battfeed collect``, ``battfeed import``."""

from __future__ import annotations

import argparse
import contextlib
import datetime
import inspect
import json
import logging
import os
import signal
import sys
import threading
from pathlib import Path
from typing import Any, Iterator

from .config import (
    Config,
    ConfigError,
    is_secret_key,
    load_config,
    redact_mapping,
    redact_text,
    url_userinfo_passwords,
)
from .harvester import Harvester, SourceFailure
from .importer import run_import
from .protocols import DataSource
from .registry import available_sources, create_source
from .sinks.bdf_csv import dataset_filename, BdfCsvSink
from .sinks.routing import RoutingSink

__all__ = ["main", "build_parser"]

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="battfeed",
        description="Turn live battery data sources into BDF (Battery Data Format) feeds.",
    )
    from battfeed import __version__

    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="enable INFO-level logging to stderr"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("sources", help="list available data sources")

    discover = subparsers.add_parser(
        "discover",
        help="scan for devices a source can collect from (BLE chargers, adb devices)",
    )
    discover.add_argument(
        "--source",
        default=None,
        help="limit the scan to one source (see 'battfeed sources'); required with --opt",
    )
    discover.add_argument(
        "--timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help="time budget per source scan/probe (default: 6.0)",
    )
    discover.add_argument(
        "--opt",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="discovery option for the selected source, repeatable (requires "
        "--source), e.g. --opt adb_path=C:/platform-tools/adb.exe",
    )
    discover.add_argument(
        "--json",
        action="store_true",
        help="machine-readable output: {source: [candidate, ...]}",
    )

    # Config-overridable options default to None so an explicit CLI value can be
    # told apart from an unset one; the real defaults are applied after the
    # config file is merged (see _resolve). Precedence is documented in config.py.
    collect = subparsers.add_parser("collect", help="poll a source and write a .bdf.csv file")
    collect.add_argument("--source", required=True, help="source name (see 'battfeed sources')")
    _add_config_argument(collect, "collect")
    collect.add_argument(
        "--duration",
        type=float,
        default=None,
        metavar="SECONDS",
        help="how long to collect, in seconds (default: run until Ctrl-C)",
    )
    collect.add_argument(
        "--interval",
        type=float,
        default=None,
        metavar="SECONDS",
        help="polling interval in seconds (default: 1.0)",
    )
    collect.add_argument(
        "--opt",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="source constructor option, repeatable (values parsed as JSON when "
        "possible, e.g. --opt slot=2 --opt path='\"log.csv\"' --opt "
        'column_map=\'{"V":"voltage_volt"}\'); overrides a [source.<name>] '
        "config block; see 'battfeed sources' for each source's options",
    )
    collect.add_argument(
        "--out",
        type=Path,
        default=None,
        help="output .bdf.csv path (default: generated from --institution/--cell "
        "as InstitutionCode__CellName__YYYYMMDD_XXX.bdf.csv)",
    )
    collect.add_argument(
        "--institution",
        default=None,
        help="institution code used in the generated file name (default: LOCAL)",
    )
    collect.add_argument(
        "--cell",
        default=None,
        help="cell name used in the generated file name (default: the source name)",
    )

    importer = subparsers.add_parser(
        "import",
        help="drain a batch/file-import source into per-(series, run) .bdf.csv files",
    )
    importer.add_argument("--source", required=True, help="source name (see 'battfeed sources')")
    _add_config_argument(importer, "import")
    importer.add_argument(
        "--opt",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="source constructor option, repeatable (values parsed as JSON when "
        "possible, e.g. --opt path='\"C:/logs\"'); overrides a [source.<name>] "
        "config block; see 'battfeed sources' for each source's options",
    )
    importer.add_argument(
        "--watch",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="keep polling for new files until Ctrl-C; --no-watch forces one-shot "
        "and overrides a config watch=true (default: one-shot, stop when drained)",
    )
    importer.add_argument(
        "--interval",
        type=float,
        default=None,
        metavar="SECONDS",
        help="idle interval between checks for new files (default: 5.0)",
    )
    importer.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="directory for the .bdf.csv files, one per (series, run) (default: current directory)",
    )
    importer.add_argument(
        "--institution",
        default=None,
        help="institution code used in the generated file names (default: LOCAL)",
    )
    importer.add_argument(
        "--reset-ledger",
        action="store_true",
        help="clear the source's dedupe ledger before importing, re-ingesting "
        "everything (deleting output files never resets the ledger); the source "
        "must support it (a reset_ledger() hook); cannot repair a corrupt ledger "
        "file -- delete that file manually as its error message directs",
    )
    return parser


def _add_config_argument(sub: argparse.ArgumentParser, verb: str) -> None:
    sub.add_argument(
        "--config",
        type=Path,
        default=None,
        metavar="FILE",
        help=f"TOML config file supplying source options ([source.<name>]) and run "
        f"parameters ([{verb}]). Precedence, highest first: --opt (source kwargs) / "
        f"a CLI flag (run params) > the config file > the built-in default. The "
        f'environment enters only via "${{ENV:VAR}}" expansion inside the file (the '
        f"sanctioned way to keep a secret out of it) -- there is no separate "
        f"env-over-config layer. Needs Python 3.11+ (or 'pip install tomli' on 3.10).",
    )


def _describe(cls: type) -> str:
    doc = (cls.__doc__ or "").strip()
    return doc.splitlines()[0] if doc else "(no description)"


def _options_of(cls: type) -> str:
    """Render a source's constructor options, e.g. ``slot=0, address=None``.

    A secret-named parameter (``api_key``, ``token``, ...) never shows a real
    default value: a non-empty default is masked to ``***`` so the ``battfeed
    sources`` listing can never echo a credential baked into a source.
    """
    try:
        params = inspect.signature(cls).parameters
    except (TypeError, ValueError):
        return ""
    rendered: list[str] = []
    for name, p in params.items():
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            continue
        if p.default is inspect.Parameter.empty:
            rendered.append(name)
        elif is_secret_key(name) and p.default not in (None, ""):
            rendered.append(f"{name}=***")
        else:
            rendered.append(f"{name}={p.default!r}")
    return ", ".join(rendered)


def _parse_opts(pairs: list[str]) -> dict[str, Any]:
    """Parse repeated ``--opt KEY=VALUE`` flags into constructor kwargs.

    Values are interpreted as JSON when they parse (numbers, booleans, null,
    quoted strings, objects, arrays); anything else is taken as a literal
    string, so ``--opt path=log.csv`` just works.
    """
    opts: dict[str, Any] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise SystemExit(f"error: --opt expects KEY=VALUE, got {pair!r}")
        try:
            opts[key] = json.loads(value)
        except json.JSONDecodeError:
            opts[key] = value
    return opts


def _resolve(cli_value: Any, config_value: Any, default: Any) -> Any:
    """Apply precedence: an explicit CLI flag, then the config file, then default.

    A CLI value of ``None`` means the flag was not given (config-overridable
    flags default to ``None``); a config value of ``None`` means the key was
    absent from the file.
    """
    if cli_value is not None:
        return cli_value
    if config_value is not None:
        return config_value
    return default


def _load_config(path: Path | None) -> Config | None:
    """Load ``--config`` if given (may raise :class:`ConfigError`)."""
    return None if path is None else load_config(path)


def _positive_number(value: Any, name: str) -> float:
    """Return ``value`` as a positive float, or raise ``ValueError`` actionably.

    Run parameters can arrive from a config file as any TOML scalar; a
    non-numeric ``interval = "fast"`` or a non-positive ``interval = 0`` must
    exit cleanly (rc 2) with an actionable message, never a ``TypeError``
    traceback from deep in the harvester.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        hint = (
            " (a config value must be a TOML number, not a string)"
            if isinstance(value, str)
            else ""
        )
        raise ValueError(f"--{name} must be a positive number, got {value!r}{hint}")
    if value <= 0:
        raise ValueError(f"--{name} must be positive, got {value}")
    return float(value)


def _resolve_source(args: argparse.Namespace, cfg: Config | None) -> tuple[str, dict[str, Any]]:
    """Merge a ``[source.<name>]`` config block with ``--opt`` into (type, kwargs).

    ``--source NAME`` selects the block ``[source.NAME]`` when the config
    defines one; the block's ``type`` field (defaulting to NAME) names the
    registered source, and its other keys are constructor kwargs. ``--opt``
    values override the config block key-for-key.
    """
    block = cfg.source_block(args.source) if cfg is not None else None
    if block is not None:
        source_type = block.pop("type", args.source)
        if not isinstance(source_type, str):
            raise ConfigError(
                f"[source.{args.source}] 'type' must be a string, got {type(source_type).__name__}"
            )
        kwargs = block
    else:
        source_type = args.source
        kwargs = {}
    kwargs.update(_parse_opts(args.opt))  # --opt wins over the config file
    return source_type, kwargs


#: Source type -> ``(secret kwarg, env var it falls back to)`` pairs. When the
#: kwarg is left unset the source reads the env var itself (e.g. dji's parser
#: reads ``DJI_API_KEY``), so its value never enters ``kwargs`` and the CLI's
#: scrubber would otherwise be blind to it. Registering it here arms the
#: value-scrubber with that env value. Future credentialed sources add a row.
_SOURCE_SECRET_ENV: dict[str, tuple[tuple[str, str], ...]] = {
    "dji": (("api_key", "DJI_API_KEY"),),
}


def _secret_values(kwargs: dict[str, Any]) -> list[str]:
    """The concrete secret *values* among ``kwargs`` (by secret-named key)."""
    return [v for k, v in kwargs.items() if is_secret_key(k) and isinstance(v, str) and v]


def _resolve_secret_values(source_type: str, kwargs: dict[str, Any]) -> list[str]:
    """Every concrete secret value the run must keep out of text output.

    Three sources: values under a secret-named kwarg; the env-var value a source
    reads for itself when its secret kwarg is unset (:data:`_SOURCE_SECRET_ENV`);
    and any password embedded in a ``scheme://user:pass@host`` value under *any*
    key (harvested so it is scrubbed by value, not only masked in place).
    """
    values = list(_secret_values(kwargs))
    for kwarg_name, env_var in _SOURCE_SECRET_ENV.get(source_type, ()):
        if not kwargs.get(kwarg_name):  # unset/empty -> the source reads the env var
            env_value = os.environ.get(env_var)
            if env_value:
                values.append(env_value)
    for value in kwargs.values():
        if isinstance(value, str):
            values.extend(url_userinfo_passwords(value))
    return [s for s in dict.fromkeys(values) if s]  # de-duplicated, non-empty


class _SecretLogFilter(logging.Filter):
    """Scrub known secret values (and URL userinfo) from every log record.

    Attached to every handler on every logger for the duration of a run, so a
    log line from anywhere -- including third-party source code we do not
    control and cannot edit, and its exception tracebacks -- never carries a
    resolved credential out to the console or a capture buffer.
    """

    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        self._secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - a broken record fails at emit anyway
            return True
        scrubbed = redact_text(message, self._secrets)
        if scrubbed != message:
            record.msg = scrubbed
            record.args = ()
        # Tracebacks: a secret in an exception message surfaces in the rendered
        # traceback (logger.exception / exc_info=True). Render it now, scrub it,
        # and clear exc_info so downstream formatters reuse our scrubbed text
        # instead of re-rendering the raw exception.
        if record.exc_info:
            if not record.exc_text:
                record.exc_text = logging.Formatter().formatException(record.exc_info)
            record.exc_info = None
        if record.exc_text:
            record.exc_text = redact_text(record.exc_text, self._secrets)
        if record.stack_info:
            record.stack_info = redact_text(record.stack_info, self._secrets)
        return True


def _all_log_handlers() -> list[logging.Handler]:
    """Every handler on the root logger and on every configured logger.

    Root-only coverage misses a handler a source attaches to its own
    (possibly ``propagate=False``) logger; walking ``loggerDict`` closes that.
    """
    loggers: list[logging.Logger] = [logging.getLogger()]
    for name in list(logging.Logger.manager.loggerDict):
        loggers.append(logging.getLogger(name))
    seen: set[int] = set()
    handlers: list[logging.Handler] = []
    for logger_obj in loggers:
        for handler in getattr(logger_obj, "handlers", []):
            if id(handler) not in seen:
                seen.add(id(handler))
                handlers.append(handler)
    return handlers


@contextlib.contextmanager
def _scrubbing_logs(secrets: list[str]) -> Iterator[None]:
    """Redact ``secrets`` from all log output while the block runs.

    Enumerates handlers at entry across all loggers. Nesting one instance inside
    another (around construction, then re-entered around the run) is deliberate:
    the inner enumeration also covers handlers a source added in its ``__init__``.
    A no-op when there is nothing to scrub.
    """
    if not secrets:
        yield
        return
    log_filter = _SecretLogFilter(secrets)
    handlers = _all_log_handlers()
    for handler in handlers:
        handler.addFilter(log_filter)
    try:
        yield
    finally:
        for handler in handlers:
            handler.removeFilter(log_filter)


def _cmd_sources() -> int:
    sources = available_sources()
    width = max(len(name) for name in sources)
    for name in sorted(sources):
        cls = sources[name]
        note = ""
        availability = getattr(cls, "availability", None)
        if callable(availability):
            reason = availability()
            if reason:
                note = f"  [unavailable: {reason}]"
        print(f"{name:<{width}}  {_describe(cls)}{note}")
        options = _options_of(cls)
        if options:
            print(f"{'':<{width}}    options: {options}")
    return 0


def _cmd_discover(args: argparse.Namespace) -> int:
    """Scan discovery-capable sources and print connectable candidates.

    With ``--source`` the named source must support discovery (rc 2
    otherwise); without it, every discovery-capable, available source is
    scanned and per-source failures are reported as notes rather than
    aborting the sweep. Finding nothing is rc 0 -- an empty bench is not an
    error, and scripts read the JSON, not the exit code.
    """
    sources = available_sources()
    if args.source is not None and args.source not in sources:
        print(
            f"error: unknown source {args.source!r}. Available sources: {sorted(sources)}",
            file=sys.stderr,
        )
        return 2
    opts = _parse_opts(args.opt)
    if opts and args.source is None:
        print("error: --opt with 'discover' requires --source", file=sys.stderr)
        return 2
    try:
        timeout = _positive_number(args.timeout if args.timeout is not None else 6.0, "timeout")
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    names = [args.source] if args.source is not None else sorted(sources)
    results: dict[str, list[dict[str, Any]]] = {}
    notes: dict[str, str] = {}
    for name in names:
        cls = sources[name]
        hook = getattr(cls, "discover", None)
        if not callable(hook):
            if args.source is not None:
                print(f"error: source {name!r} does not support discovery", file=sys.stderr)
                return 2
            continue
        # The availability gate applies to the sweep only: an explicit --source
        # is always attempted, because --opt can supply exactly what
        # availability() found missing (e.g. adb_path when adb is not on PATH),
        # and a real failure surfaces as its own actionable error below.
        if args.source is None:
            availability = getattr(cls, "availability", None)
            if callable(availability):
                reason = availability()
                if reason:
                    notes[name] = f"skipped: {reason}"
                    continue
        try:
            results[name] = list(hook(timeout_s=timeout, **opts))
        except TypeError as exc:
            print(f"error: bad --opt for source {name!r}: {exc}", file=sys.stderr)
            return 2
        except Exception as exc:  # per-source scan failures must not kill the sweep
            if args.source is not None:
                print(f"error: {exc}", file=sys.stderr)
                return 2
            notes[name] = f"failed: {exc}"

    if args.json:
        print(json.dumps({"candidates": results, "notes": notes}, indent=2))
        return 0
    if not results and not notes:
        print("No discovery-capable sources are installed.")
        return 0
    for name in sorted(set(results) | set(notes)):
        if name in notes:
            print(f"{name}: {notes[name]}")
            continue
        candidates = results[name]
        if not candidates:
            print(f"{name}: nothing found")
            continue
        print(f"{name}:")
        for candidate in candidates:
            described = ", ".join(
                f"{key}={value}"
                for key, value in candidate.items()
                if key not in ("option", "value", "ready") and value is not None
            )
            print(f"  {candidate['value']}" + (f"  ({described})" if described else ""))
            if candidate.get("ready", True):
                print(
                    f"      -> battfeed collect --source {name} "
                    f"--opt {candidate['option']}={candidate['value']}"
                )
    return 0


def _default_out_path(institution: str, cell: str) -> Path:
    today = datetime.date.today()
    for seq in range(1, 1000):
        candidate = Path(dataset_filename(institution, cell, today, seq))
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"All sequence numbers 001-999 are taken for {institution}/{cell} today")


def _instantiate_source(
    source_type: str, kwargs: dict[str, Any], secrets: list[str]
) -> DataSource | None:
    """Build a source for a CLI command; on failure print to stderr and return None.

    Any secret value in ``kwargs`` is scrubbed from the error text, so a bad
    option can never surface a credential (a source may reflect a kwarg it did
    not like straight into a ValueError/TypeError message).
    """

    def fail(message: str) -> None:
        print(f"error: {redact_text(message, secrets)}", file=sys.stderr)

    try:
        source: DataSource = create_source(source_type, **kwargs)
        return source
    except KeyError as exc:
        fail(str(exc.args[0]))
    except TypeError as exc:
        fail(
            f"bad options for source {source_type!r}: {exc}\n"
            "Pass constructor options with --opt KEY=VALUE or a [source.<name>] "
            "config block (see 'battfeed sources' for each source's options)."
        )
    except ImportError as exc:
        fail(str(exc))
    except ValueError as exc:
        # e.g. a torn/corrupt import ledger opened in the source constructor:
        # the message is actionable on its own; no traceback at the operator.
        fail(str(exc))
    return None


@contextlib.contextmanager
def _sigint_sets(stop: threading.Event) -> Iterator[None]:
    """Route Ctrl-C into ``stop`` so loops end cleanly and files are finalised."""
    previous = None
    try:
        previous = signal.signal(signal.SIGINT, lambda *_: stop.set())
    except ValueError:  # not the main thread; run without a SIGINT hook
        pass
    try:
        yield
    finally:
        if previous is not None:
            signal.signal(signal.SIGINT, previous)


def _close_source(source: DataSource) -> None:
    close = getattr(source, "close", None)
    if callable(close):
        close()


def _cmd_collect(args: argparse.Namespace) -> int:
    try:
        cfg = _load_config(args.config)
        source_type, kwargs = _resolve_source(args, cfg)
        run = cfg.run_params("collect") if cfg is not None else {}
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    secrets = _resolve_secret_values(source_type, kwargs)

    # Outer scrub covers any credential a source logs while being constructed
    # (on handlers that already exist).
    with _scrubbing_logs(secrets):
        source = _instantiate_source(source_type, kwargs, secrets)
        if source is None:
            return 2

        try:
            interval = _positive_number(
                _resolve(args.interval, run.get("interval"), 1.0), "interval"
            )
            duration = _resolve(args.duration, run.get("duration"), None)
            if duration is not None:
                duration = _positive_number(duration, "duration")
            institution = str(_resolve(args.institution, run.get("institution"), "LOCAL"))
            cell = str(_resolve(args.cell, run.get("cell"), None) or args.source)
            out = _resolve(args.out, run.get("out"), None)
            out_path: Path = Path(out) if out is not None else _default_out_path(institution, cell)
        except ValueError as exc:  # bad interval/duration, or empty/'__' institution/cell
            print(f"error: {exc}", file=sys.stderr)
            _close_source(source)  # usage errors must still release the source
            return 2

        harvester = Harvester()
        harvester.register(source)
        sink = BdfCsvSink(
            out_path,
            metadata=redact_mapping(
                {
                    "institution": institution,
                    "cell_name": cell,
                    "source": dict(source.metadata()),
                    "requested_duration_second": duration,
                    "requested_interval_second": interval,
                },
                secrets,
            ),
        )

        # Ctrl-C sets the stop event so the loop ends cleanly and files are finalised.
        stop = threading.Event()
        if duration is None:
            print(f"Collecting from '{args.source}' until Ctrl-C ...", file=sys.stderr)
        try:
            # Inner scrub re-enumerates handlers after construction, covering any
            # a source attached to its own logger in __init__.
            with _scrubbing_logs(secrets), _sigint_sets(stop):
                stats = harvester.collect(
                    source.name,
                    duration_s=duration,
                    interval_s=interval,
                    sink=sink,
                    stop=stop,
                )
        except SourceFailure as exc:
            print(f"error: {redact_text(str(exc), secrets)}", file=sys.stderr)
            return 1
        finally:
            sink.close()
            _close_source(source)

    interrupted = " (interrupted)" if stop.is_set() else ""
    tolerated = f", {stats.errors} tolerated error(s)" if stats.errors else ""
    print(
        f"Collected {stats.samples} sample(s) from '{stats.source}' "
        f"in {stats.duration_s:.1f} s{tolerated} -> {out_path}{interrupted}"
    )
    return 0


def _cmd_import(args: argparse.Namespace) -> int:
    try:
        cfg = _load_config(args.config)
        source_type, kwargs = _resolve_source(args, cfg)
        run = cfg.run_params("import") if cfg is not None else {}
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    secrets = _resolve_secret_values(source_type, kwargs)

    # Outer scrub covers any credential a source logs while being constructed
    # (on handlers that already exist).
    with _scrubbing_logs(secrets):
        source = _instantiate_source(source_type, kwargs, secrets)
        if source is None:
            return 2

        institution = str(_resolve(args.institution, run.get("institution"), "LOCAL"))
        out_dir = Path(_resolve(args.out_dir, run.get("out_dir"), "."))
        watch = bool(_resolve(args.watch, run.get("watch"), False))

        # Everything from here on runs under one finally so that EVERY exit --
        # including usage errors like a bad --institution or --interval -- still
        # closes the source (and the sink, once it exists).
        stop = threading.Event()
        sink: RoutingSink | None = None
        try:
            if args.reset_ledger:
                reset = getattr(source, "reset_ledger", None)
                if not callable(reset):
                    print(
                        f"error: source {args.source!r} does not support --reset-ledger "
                        "(it has no reset_ledger() hook)",
                        file=sys.stderr,
                    )
                    return 2
                reset()
                print(f"Reset the import ledger of '{args.source}'.", file=sys.stderr)

            try:
                interval = _positive_number(
                    _resolve(args.interval, run.get("interval"), 5.0), "interval"
                )
            except ValueError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2

            # Imported data is inherently multi-(series, run): one folder holds
            # many objects and many runs, so the import verb always writes through
            # a RoutingSink -- one .bdf.csv (plus sidecar) per (series_id, run_id).
            try:
                sink = RoutingSink(
                    out_dir,
                    institution=institution,
                    metadata=redact_mapping(
                        {
                            "institution": institution,
                            "source": dict(source.metadata()),
                            "imported_with": "battfeed import",
                            "watch": watch,
                            "requested_interval_second": interval,
                        },
                        secrets,
                    ),
                )
            except ValueError as exc:  # e.g. an institution containing "__"
                print(f"error: {exc}", file=sys.stderr)
                return 2

            if watch:
                print(f"Watching '{args.source}' for new files until Ctrl-C ...", file=sys.stderr)
            try:
                # Inner scrub re-enumerates handlers after construction, covering any
                # a source attached to its own logger in __init__.
                with _scrubbing_logs(secrets), _sigint_sets(stop):
                    stats = run_import(
                        source,
                        sink,
                        watch=watch,
                        interval_s=interval,
                        stop=stop,
                    )
            except SourceFailure as exc:
                print(f"error: {redact_text(str(exc), secrets)}", file=sys.stderr)
                return 1
        finally:
            if sink is not None:
                sink.close()
            _close_source(source)

        assert sink is not None  # every early exit above returns inside the try
        interrupted = " (interrupted)" if stop.is_set() else ""
        tolerated = f", {stats.errors} tolerated error(s)" if stats.errors else ""
        files = [path for paths in sink.files_by_series.values() for path in paths]
        if not files:
            print(f"Nothing to import from '{args.source}'{tolerated}{interrupted}.")
            return 0
        print(
            f"Imported {stats.samples} sample(s) from '{stats.source}' into "
            f"{len(files)} file(s) under {out_dir}{tolerated}{interrupted}:"
        )
        for path in files:
            print(f"  {path}")
        return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)
    if args.command == "sources":
        return _cmd_sources()
    if args.command == "discover":
        return _cmd_discover(args)
    if args.command == "import":
        return _cmd_import(args)
    return _cmd_collect(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
