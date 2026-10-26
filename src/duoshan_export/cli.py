"""Command-line entry point for reusable local backup extraction."""

from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import importlib
import json
from pathlib import Path
import shutil
import sys
import tempfile

from . import __version__
from .config import ExportConfig
from .errors import ExportError
from .runtime import STATE_FILE, begin_export, load_state, write_json


def _path(value: str) -> Path:
    return Path(value).expanduser().resolve()


def _common_runtime(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--work-dir", type=_path, help="Private working directory (default: OUTPUT/.work)")
    parser.add_argument("--workers", type=int, default=6, help="Concurrent media requests, 1-32 (default: 6)")
    parser.add_argument("--sqlcipher-module-path", type=_path, help="Directory containing an existing SQLCipher Python binding")
    parser.add_argument("--ffmpeg", default="ffmpeg", help="FFmpeg executable name or path")
    parser.add_argument("--ffprobe", default="ffprobe", help="FFprobe executable name or path")


def _json_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="Write the result as JSON; send progress to stderr")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="duoshan-export", description="Export local Android Duoshan backups to SQLite and an offline chat reader.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect", help="Discover accounts and inspect a backup without exporting")
    inspect.add_argument("source", type=_path)
    inspect.add_argument("--work-dir", type=_path, help="Extraction/snapshot directory (default: a temporary directory)")
    inspect.add_argument("--sqlcipher-module-path", type=_path)
    _json_flag(inspect)

    export = commands.add_parser("export", help="Create a portable export; networking is opt-in")
    export.add_argument("source", type=_path)
    export.add_argument("-o", "--output", required=True, type=_path)
    export.add_argument("--account", action="append", default=[], metavar="UID", help="Only export this account; repeat to select several")
    export.add_argument("--timezone", default="Asia/Shanghai", help="IANA timezone used for display (default: Asia/Shanghai)")
    export.add_argument("--page-size", type=int, default=600, help="Messages per HTML page, 1-5000 (default: 600)")
    export.add_argument("--resume", action="store_true", help="Resume/regenerate an export from the same unchanged source")
    export.add_argument("--download-media", action="store_true", help="Download voice, stickers and bubble resources from original URLs")
    export.add_argument("--reuse-media", type=_path, metavar="EXPORT", help="Copy verified matching media from an existing export")
    export.add_argument("--decode-media", action="store_true", help="Also decode media during final validation")
    _common_runtime(export)
    _json_flag(export)

    media = commands.add_parser("media", help="Reuse/download media, rebuild the reader and refresh the SQLite media index")
    media.add_argument("output", type=_path)
    media.add_argument("--source", type=_path, help="Optional original backup, for refreshing local decoration assets")
    media.add_argument("--download-media", action="store_true", help="Allow network downloads; otherwise use local resources only")
    media.add_argument("--reuse-media", type=_path, metavar="EXPORT")
    media.add_argument("--decode-media", action="store_true")
    _common_runtime(media)
    _json_flag(media)

    validate = commands.add_parser("validate", help="Check exported data, local links and media integrity")
    validate.add_argument("output", type=_path)
    validate.add_argument("--decode-media", action="store_true", help="Decode audio and every image frame as well")
    validate.add_argument("--ffmpeg", default="ffmpeg")
    _json_flag(validate)
    doctor = commands.add_parser("doctor", help="Report installed dependencies and media conversion tools")
    doctor.add_argument("--sqlcipher-module-path", type=_path)
    doctor.add_argument("--ffmpeg", default="ffmpeg")
    doctor.add_argument("--ffprobe", default="ffprobe")
    _json_flag(doctor)
    return parser


def _config(args, *, source: Path | None = None, state: dict | None = None) -> ExportConfig:
    state = state or {}
    work = args.work_dir
    if work is None and state.get("work"):
        work = Path(state["work"])
    if work is None:
        work = args.output / ".work"
    return ExportConfig(
        source=source or args.source,
        output=args.output,
        work=work,
        accounts=tuple(getattr(args, "account", state.get("accounts", []))),
        timezone=getattr(args, "timezone", state.get("timezone", "Asia/Shanghai")),
        page_size=getattr(args, "page_size", state.get("page_size", 600)),
        download=args.download_media,
        workers=args.workers,
        sqlcipher_module_path=args.sqlcipher_module_path,
        ffmpeg=args.ffmpeg,
        ffprobe=args.ffprobe,
        resume=getattr(args, "resume", True),
    )


def _check_validation(report: dict) -> None:
    status = str(report.get("status", "")).lower()
    if status not in {"passed", "pass", "ok", "all_data_checks_passed"}:
        raise ExportError("Export validation failed. Inspect validation_results.json in the output directory.")


def _export(args) -> dict:
    from .source import prepare_source
    from .exporter import export_chats, rerender_export
    from .media import download_media, prepare_decorations
    from .media_index import index_media
    from .validation import validate_export

    config = _config(args)
    state = begin_export(config)
    print("Preparing source backup...", flush=True)
    layout = prepare_source(config.source, config.work)
    print("Exporting chat records...", flush=True)
    summary = export_chats(config, layout)
    reused = None
    if args.reuse_media:
        from .reuse import reuse_media
        print("Importing verified media...", flush=True)
        reused = reuse_media(args.reuse_media, config.output)
    print("Preparing media and decoration manifests...", flush=True)
    download_media(config)
    prepare_decorations(config, layout)
    summary = rerender_export(config)
    index = index_media(config)
    report = validate_export(config.output, decode_media=args.decode_media, ffmpeg=config.ffmpeg)
    _check_validation(report)
    state.update(status="completed", timezone=config.timezone, page_size=config.page_size)
    write_json(config.output / STATE_FILE, state)
    return {"status": "completed", "output": str(config.output), "reader": str(config.output / "index.html"),
            "summary": summary, "media_reuse": reused, "media_index": index, "validation": report}


def _media(args) -> dict:
    from .exporter import rerender_export
    from .media import download_media, prepare_decorations
    from .media_index import index_media
    from .validation import validate_export

    state = load_state(args.output)
    source = args.source or Path(state["source"])
    config = _config(args, source=source, state=state)
    # Media updates require only the exported data unless a source is explicitly supplied.
    config.work.mkdir(parents=True, exist_ok=True)
    reused = None
    if args.reuse_media:
        from .reuse import reuse_media
        reused = reuse_media(args.reuse_media, config.output)
    download_media(config)
    if args.source:
        from .source import prepare_source
        from .runtime import check_locations
        check_locations(config)
        prepare_decorations(config, prepare_source(config.source, config.work))
    summary = rerender_export(config)
    index = index_media(config)
    report = validate_export(config.output, decode_media=args.decode_media, ffmpeg=config.ffmpeg)
    _check_validation(report)
    state["status"] = "completed"
    write_json(config.output / STATE_FILE, state)
    return {"status": "completed", "output": str(config.output), "summary": summary,
            "media_reuse": reused, "media_index": index, "validation": report}


def _inspect(args) -> dict:
    from .source import prepare_source, discover_accounts
    from .database import open_database

    def inspect_at(work):
        layout = prepare_source(args.source, work)
        results = []
        for account in discover_accounts(layout):
            row = {"uid": account.uid, "database": str(account.database), "contacts_available": bool(account.contacts_database)}
            try:
                with open_database(account.database, account.uid, layout.work_dir,
                                   sqlcipher_module_path=args.sqlcipher_module_path) as database:
                    row["message_count"] = database.execute("SELECT count(*) FROM msg").fetchone()[0]
            except ExportError as error:
                row["inspection_error"] = str(error)
            results.append(row)
        return {"source": str(args.source), "app_directory": str(layout.app_dir), "accounts": results,
                "note": "Archive inspection paths are temporary unless --work-dir was supplied."}
    if args.work_dir:
        # Prevent an inspection from unpacking into the source itself.
        source_root = args.source
        if args.work_dir == source_root or args.work_dir.is_relative_to(source_root) or source_root.is_relative_to(args.work_dir):
            raise ExportError("The inspection work directory must be separate from the source backup.")
        return inspect_at(args.work_dir)
    with tempfile.TemporaryDirectory(prefix="duoshan-inspect-") as directory:
        return inspect_at(Path(directory))


def _doctor(args) -> dict:
    if args.sqlcipher_module_path:
        sys.path.insert(0, str(args.sqlcipher_module_path))
    dependencies = {}
    for module, requirement in (("PIL", "Pillow"), ("requests", "requests"), ("zstandard", "zstandard"),
                                ("tzdata", "tzdata"), ("sqlcipher3", "sqlcipher3-wheels"), ("pillow_heif", "pillow-heif")):
        try:
            loaded = importlib.import_module(module)
            dependencies[requirement] = {"available": True, "version": str(getattr(loaded, "__version__", "installed"))}
        except (ImportError, OSError) as error:
            dependencies[requirement] = {"available": False, "install": f"python -m pip install {requirement}", "reason": str(error)}
    programs = {name: shutil.which(value) for name, value in (("ffmpeg", args.ffmpeg), ("ffprobe", args.ffprobe))}
    core_ready = all(dependencies[name]["available"] for name in ("Pillow", "requests", "zstandard", "tzdata"))
    return {"version": __version__, "python": sys.version.split()[0], "core_ready": core_ready,
            "encrypted_database_ready": dependencies["sqlcipher3-wheels"]["available"],
            "audio_conversion_ready": all(programs.values()), "dependencies": dependencies, "programs": programs}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.json:
            with redirect_stdout(sys.stderr):
                result = _dispatch(args)
            # ASCII escapes preserve every Unicode value on legacy Windows pipes.
            print(json.dumps(result, ensure_ascii=True, indent=2, default=str))
        else:
            result = _dispatch(args)
            if args.command in {"export", "media"}:
                print(f"Completed. Open: {args.output / 'index.html'}")
            else:
                print(json.dumps(result, ensure_ascii=True, indent=2, default=str))
        return 0 if args.command != "doctor" or result["core_ready"] else 1
    except KeyboardInterrupt:
        print("Interrupted. Use --resume to continue an export from the same source.", file=sys.stderr)
        return 130
    except Exception as error:
        if args.json:
            print(json.dumps({"status": "error", "error": str(error)}, ensure_ascii=True))
        else:
            print(f"Error: {error}", file=sys.stderr)
        return 1


def _dispatch(args) -> dict:
    if args.command == "export":
        return _export(args)
    if args.command == "media":
        return _media(args)
    if args.command == "inspect":
        return _inspect(args)
    if args.command == "doctor":
        return _doctor(args)
    from .validation import validate_export
    result = validate_export(args.output, decode_media=args.decode_media, ffmpeg=args.ffmpeg)
    _check_validation(result)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
