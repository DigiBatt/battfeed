"""Command-line interface: ``gleaned sources`` and ``gleaned collect``."""

from __future__ import annotations

import argparse
import datetime
import logging
import signal
import sys
import threading
from pathlib import Path

from .harvester import Harvester
from .registry import available_sources, create_source
from .sinks.bdf_csv import dataset_filename, BdfCsvSink

__all__ = ["main", "build_parser"]

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gleaned",
        description="Turn live battery data sources into BDF (Battery Data Format) feeds.",
    )
    from gleaned import __version__

    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="enable INFO-level logging to stderr"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("sources", help="list available data sources")

    collect = subparsers.add_parser(
        "collect", help="poll a source and write a .bdf.csv file"
    )
    collect.add_argument("--source", required=True, help="source name (see 'gleaned sources')")
    collect.add_argument(
        "--duration", type=float, required=True, metavar="SECONDS",
        help="how long to collect, in seconds",
    )
    collect.add_argument(
        "--interval", type=float, default=1.0, metavar="SECONDS",
        help="polling interval in seconds (default: 1.0)",
    )
    collect.add_argument(
        "--out", type=Path, default=None,
        help="output .bdf.csv path (default: generated from --institution/--cell "
             "as InstitutionCode__CellName__YYYYMMDD_XXX.bdf.csv)",
    )
    collect.add_argument(
        "--institution", default="LOCAL",
        help="institution code used in the generated file name (default: LOCAL)",
    )
    collect.add_argument(
        "--cell", default=None,
        help="cell name used in the generated file name (default: the source name)",
    )
    return parser


def _describe(cls: type) -> str:
    doc = (cls.__doc__ or "").strip()
    return doc.splitlines()[0] if doc else "(no description)"


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
    return 0


def _default_out_path(institution: str, cell: str) -> Path:
    today = datetime.date.today()
    for seq in range(1, 1000):
        candidate = Path(dataset_filename(institution, cell, today, seq))
        if not candidate.exists():
            return candidate
    raise FileExistsError(
        f"All sequence numbers 001-999 are taken for {institution}/{cell} today"
    )


def _cmd_collect(args: argparse.Namespace) -> int:
    try:
        source = create_source(args.source)
    except KeyError as exc:
        print(f"error: {exc.args[0]}", file=sys.stderr)
        return 2
    except TypeError:
        print(
            f"error: source {args.source!r} requires constructor arguments and cannot be "
            "started from the CLI; use the Python API (gleaned.create_source) instead.",
            file=sys.stderr,
        )
        return 2
    except ImportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    cell = args.cell or args.source
    out_path: Path = args.out or _default_out_path(args.institution, cell)

    harvester = Harvester()
    harvester.register(source)
    sink = BdfCsvSink(
        out_path,
        metadata={
            "institution": args.institution,
            "cell_name": cell,
            "source": dict(source.metadata()),
            "requested_duration_second": args.duration,
            "requested_interval_second": args.interval,
        },
    )

    # Ctrl-C sets the stop event so the loop ends cleanly and files are finalised.
    stop = threading.Event()
    previous_handler = None
    try:
        previous_handler = signal.signal(signal.SIGINT, lambda *_: stop.set())
    except ValueError:  # not the main thread; run without a SIGINT hook
        pass

    try:
        stats = harvester.collect(
            args.source,
            duration_s=args.duration,
            interval_s=args.interval,
            sink=sink,
            stop=stop,
        )
    finally:
        sink.close()
        close = getattr(source, "close", None)
        if callable(close):
            close()
        if previous_handler is not None:
            signal.signal(signal.SIGINT, previous_handler)

    interrupted = " (interrupted)" if stop.is_set() else ""
    print(
        f"Collected {stats.samples} sample(s) from '{stats.source}' "
        f"in {stats.duration_s:.1f} s -> {out_path}{interrupted}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)
    if args.command == "sources":
        return _cmd_sources()
    return _cmd_collect(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
