"""Validation of an exported Duoshan chat tree.

Public entry point::

    report = validate_export(output, *, decode_media=False, ffmpeg='ffmpeg')

The function inspects an export written by this package and returns an English
status dictionary. It also refreshes the English report files inside the export
root:

``validation_results.json``
    Aggregate data + reader + media result (always written).
``reader_media_validation_results.json``
    Reader/page focused detail (always written).
``media_validation_results.json``
    Per-resource media detail (written only when ``decode_media`` is true and a
    media manifest exists).

Checks performed

* Export shape: summary, root index, stylesheet and one folder per account.
* SQLite: read-only ``PRAGMA integrity_check`` plus a ``msg`` row count.
* Raw row equality: every ``messages.jsonl`` raw row digest must equal the
  matching ``chat.sqlite`` ``msg`` row digest, including unknown columns,
  unknown message types and large integers.
* Counts: JSONL rows == SQLite rows == CSV data rows == rendered messages ==
  recorded statistics.
* CSV safety: formula prefixes neutralised and long numeric identifiers
  protected from spreadsheet rounding.
* HTML: local-only media references, resolvable local links, escaped text,
  unique message anchors, restored bubbles and inline emoji matching the
  decoration manifest.
* Media manifest associations: every ``status == "ok"`` resource points at an
  existing file and is rendered exactly once for its message. With
  ``decode_media`` audio is decoded to null with ffmpeg and images are fully
  decoded with Pillow, animated frame counts included.
* Optional media index: when ``export_local_attachment`` exists, indexed
  export-relative paths must exist and agree with the media manifest.

Structural problems (an unreadable summary, a missing output directory) raise
``ExportError`` so the CLI can exit non-zero without a traceback. Recoverable
findings are reported through ``status``/``problems`` instead.
"""

from __future__ import annotations

import base64
import collections
import concurrent.futures
import csv
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
from urllib.parse import unquote, urlsplit

from . import paths
from .errors import ExportError

DATA_REPORT = paths.ROOT_FILES["data_validation"]
READER_REPORT = paths.ROOT_FILES["reader_validation"]
MEDIA_REPORT = paths.ROOT_FILES["media_validation"]
MEDIA_MANIFEST = paths.ROOT_FILES["media_manifest"]
DECORATION_MANIFEST = paths.ROOT_FILES["decorations"]
SUMMARY_NAME = paths.ROOT_FILES["summary"]
READER_CSS = "reader.css"
ROOT_INDEX = "index.html"

DECODE_WORKERS = 4        # never run more than four media decoders in parallel
FFMPEG_TIMEOUT = 600
PROBLEM_SAMPLE_LIMIT = 20

# Prefixes a spreadsheet treats as a formula, and the raw control prefixes.
FORMULA_PREFIXES = ("=", "+", "-", "@")
FORMULA_CONTROL_PREFIXES = ("\t", "\r")
ID_HEADER_PATTERN = re.compile(r"(?:^|[^A-Za-z])(?:ID|UID)(?:$|[^A-Za-z])", re.IGNORECASE)
LONG_ID_MIN_LENGTH = 16

LOCAL_REFERENCE_TAGS = {"a": "href", "link": "href", "img": "src", "audio": "src",
                        "video": "src", "source": "src"}
MEDIA_TAGS = {"img", "audio", "video", "source"}

# --------------------------------------------------------------------------- helpers


def _now() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def _sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _json_default(obj):
    if isinstance(obj, bytes):
        return {"_base64": base64.b64encode(obj).decode("ascii")}
    return str(obj)


def _row_digest(row: dict) -> str:
    """Stable digest of one database row / JSONL raw row."""
    text = json.dumps(row, ensure_ascii=False, sort_keys=True, default=_json_default)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except OSError as error:
        return None, f"could not read {path.name}: {error}"
    except ValueError as error:
        return None, f"{path.name} is not valid JSON: {error}"


class Problems:
    """Collect findings, keeping a bounded sample per code."""

    def __init__(self) -> None:
        self.counts: dict[str, int] = collections.Counter()
        self.samples: dict[str, list[dict]] = collections.defaultdict(list)

    def add(self, code: str, message: str, **extra) -> None:
        self.counts[code] += 1
        if len(self.samples[code]) >= PROBLEM_SAMPLE_LIMIT:
            return
        entry = {"code": code, "message": message}
        entry.update({key: value for key, value in extra.items() if value is not None})
        self.samples[code].append(entry)

    def extend(self, counts: dict, samples: dict) -> None:
        for code, count in counts.items():
            self.counts[code] += count
        for code, entries in samples.items():
            room = PROBLEM_SAMPLE_LIMIT - len(self.samples[code])
            if room > 0:
                self.samples[code].extend(entries[:room])

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def flatten(self) -> list[dict]:
        out: list[dict] = []
        for code in sorted(self.samples):
            out.extend(self.samples[code])
        return out


# --------------------------------------------------------------------------- HTML


class _PageScanner(HTMLParser):
    """Collect message anchors, local references and bubble metadata per page."""

    def __init__(self, page: Path, output: Path, problems: Problems) -> None:
        super().__init__(convert_charrefs=True)
        self.page = page
        self.output = output
        self.problems = problems
        self.messages = 0
        self.local_references = 0
        self.external_links = 0
        self.uuids: list[str] = []
        self.per_message: dict[str, dict] = {}
        self.stickers: list[str] = []
        self.audio: list[str] = []
        self.inline_emoji: list[tuple[str, str]] = []
        self.bubbles: list[dict] = []
        self.bubble_corners = 0
        self._current: dict | None = None

    def _page_rel(self) -> str:
        try:
            return self.page.relative_to(self.output).as_posix()
        except ValueError:
            return self.page.name

    def _resolve(self, value: str) -> Path:
        target = (self.page.parent / unquote(urlsplit(value).path)).resolve()
        if not target.is_relative_to(self.output):
            self.problems.add("local_link_outside_export",
                              "A local reference points outside the export tree.",
                              page=self._page_rel(), reference=value)
        elif not target.is_file():
            self.problems.add("local_link_missing",
                              "A local reference does not resolve to an existing file.",
                              page=self._page_rel(), reference=value)
        return target

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        classes = (attributes.get("class") or "").split()

        for key in ("href", "src"):
            value = attributes.get(key)
            if not value or value.startswith("#") or value.startswith("data:"):
                continue
            parsed = urlsplit(value)
            if parsed.scheme or parsed.netloc:
                if tag in MEDIA_TAGS:
                    self.problems.add(
                        "remote_media_reference",
                        "A media element loads a remote URL; exported pages must work offline.",
                        page=self._page_rel(), reference=value)
                else:
                    self.external_links += 1
                continue
            target = self._resolve(value)
            self.local_references += 1
            if self._current is not None and target.is_file():
                reference = target.relative_to(self.output).as_posix()
                self._current["refs"].append(reference)
                if tag in MEDIA_TAGS:
                    self._current["media"].append(reference)
                if tag == "img" and "sticker" in classes:
                    self._current["stickers"].append(reference)
                if tag == "audio":
                    self._current["audio"].append(reference)

        if tag == "article" and "message" in classes:
            anchor = attributes.get("id") or ""
            if not anchor.startswith("msg-"):
                self.problems.add("message_anchor_malformed",
                                  "A message article has no msg-<uuid> anchor.",
                                  page=self._page_rel(), anchor=anchor)
                self._current = None
                return
            uuid = anchor[4:]
            self.messages += 1
            self.uuids.append(uuid)
            self._current = {"uuid": uuid, "refs": [], "media": [], "stickers": [],
                             "audio": [], "bubble": None}
            return

        if self._current is None:
            return

        if tag == "div" and "bubble" in classes:
            self._current["bubble"] = classes
        if tag == "img":
            if "bubble-corner" in classes:
                self.bubble_corners += 1
            if "inline-emoji" in classes:
                self.inline_emoji.append((attributes.get("alt") or "",
                                          attributes.get("src") or ""))

    def handle_endtag(self, tag):
        if tag != "article" or self._current is None:
            return
        message = self._current
        self._current = None
        self.per_message[message["uuid"]] = message
        self.stickers.extend(message["stickers"])
        self.audio.extend(message["audio"])
        if message["bubble"]:
            self.bubbles.append({"uuid": message["uuid"], "classes": message["bubble"]})


# --------------------------------------------------------------------------- validator

class _Validator:
    def __init__(self, output: Path, decode_media: bool, ffmpeg: str) -> None:
        self.output = output
        self.decode_media = decode_media
        self.ffmpeg = ffmpeg
        self.problems = Problems()
        self.notes: list[str] = []
        self.accounts: list[dict] = []
        self.totals: collections.Counter = collections.Counter()
        self.summary: dict = {}
        self.manifest: dict = {}
        self.decorations: dict = {}
        self.media_report: dict | None = None
        self.reader: dict = {}
        self._records: dict[str, str] = {}
        self._meta: dict[str, dict] = {}

    # ------------------------------------------------------------------ public
    def run(self) -> dict:
        self._check_root()
        self._load_manifests()
        for account in self.summary.get("accounts") or []:
            self.accounts.append(self._check_account(account))
        self._check_summary_totals()
        self._check_media_index()
        if self.decode_media:
            if self.manifest:
                self.media_report = self._decode_media()
                if self.media_report.get("problem_count"):
                    grouped: dict[str, list[dict]] = {}
                    for entry in self.media_report["problems"]:
                        grouped.setdefault(entry["code"], []).append(entry)
                    self.problems.extend(self.media_report["problem_counts"], grouped)
            else:
                self.notes.append("media decoding skipped: no media manifest present")
        return self._report()

    # -------------------------------------------------------------- export root
    def _check_root(self) -> None:
        if not self.output.exists():
            raise ExportError(f"Export directory does not exist: {self.output}")
        if not self.output.is_dir():
            raise ExportError(f"Export path is not a directory: {self.output}")
        for name in (SUMMARY_NAME, ROOT_INDEX, READER_CSS):
            if not (self.output / name).is_file():
                self.problems.add("missing_export_file",
                                  "A required file is missing from the export root.",
                                  path=name)
        summary, error = _load_json(self.output / SUMMARY_NAME)
        if error:
            raise ExportError(f"Cannot validate this export: {error}")
        if not isinstance(summary, dict) or not isinstance(summary.get("accounts"), list):
            raise ExportError(
                f"{SUMMARY_NAME} does not contain an account list; "
                "this is not a DuoshanChatExport tree.")
        self.summary = summary
        if not summary["accounts"]:
            self.problems.add("no_accounts", "The export summary lists no accounts.")
        listed: set[str] = set()
        for account in summary["accounts"]:
            uid = str(account.get("uid") or "")
            if not uid:
                self.problems.add("account_without_uid",
                                  "An account entry in the export summary has no uid.")
                continue
            listed.add(uid)
            if not (self.output / uid).is_dir():
                self.problems.add("missing_account_directory",
                                  "An account listed in the summary has no folder.",
                                  account=uid, path=uid)
        for child in sorted(self.output.iterdir()):
            if child.is_dir() and child.name.isdecimal() and child.name not in listed:
                self.problems.add("unlisted_account_directory",
                                  "An account-looking folder is not listed in the export summary.",
                                  account=child.name, path=child.name)
        self._check_root_index()

    def _check_root_index(self) -> None:
        index = self.output / ROOT_INDEX
        if index.is_file():
            scanner = _PageScanner(index, self.output, self.problems)
            try:
                scanner.feed(index.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                self.problems.add("index_unreadable",
                                  f"Root index could not be parsed: {error}", path=ROOT_INDEX)
            self.totals["root_index_local_references"] += scanner.local_references
        self._check_stylesheet()

    def _check_stylesheet(self) -> None:
        css = self.output / READER_CSS
        if not css.is_file():
            return
        text = css.read_text(encoding="utf-8", errors="replace")
        for value in re.findall(r"url\([\"']?([^)\"']+)", text):
            if value.startswith(("http://", "https://", "data:")):
                self.problems.add("remote_media_reference",
                                  "The stylesheet loads a remote URL.",
                                  path=READER_CSS, reference=value)
                continue
            target = (css.parent / unquote(urlsplit(value).path)).resolve()
            if not target.is_relative_to(self.output):
                self.problems.add("local_link_outside_export",
                                  "A stylesheet reference points outside the export tree.",
                                  path=READER_CSS, reference=value)
            elif not target.is_file():
                self.problems.add("local_link_missing",
                                  "A stylesheet reference does not exist.",
                                  path=READER_CSS, reference=value)
            else:
                self.totals["css_local_references"] += 1

    # ---------------------------------------------------------------- manifests
    def _load_manifests(self) -> None:
        manifest, error = _load_json(self.output / MEDIA_MANIFEST)
        if error:
            self.notes.append(f"media manifest unavailable ({error}); associations skipped")
            manifest = {}
        self.manifest = manifest if isinstance(manifest, dict) else {}
        if not (self.output / MEDIA_MANIFEST).is_file():
            self.notes.append("media manifest not present; media association checks skipped")
        decorations, error = _load_json(self.output / DECORATION_MANIFEST)
        if error:
            self.notes.append(f"decoration manifest unavailable ({error}); bubble checks skipped")
            decorations = {}
        self.decorations = decorations if isinstance(decorations, dict) else {}
        if not (self.output / DECORATION_MANIFEST).is_file():
            self.notes.append("decoration manifest not present; bubble checks skipped")

    # -------------------------------------------------------------- one account
    def _check_account(self, summary_account: dict) -> dict:
        uid = str(summary_account.get("uid") or "")
        folder = self.output / uid
        result = {"uid": uid, "jsonl_rows": 0, "sqlite_rows": 0, "csv_rows": 0,
                  "html_messages": 0, "conversations": 0, "transcripts": 0,
                  "raw_rows_mismatched": 0, "local_references": 0,
                  "raw_fields_match": False, "sqlite_integrity": None}
        if not folder.is_dir():
            return result

        # Records are scoped to one account: two accounts may legitimately hold
        # messages with the same uuid, and a shared map would cross-contaminate.
        self._records = {}
        self._meta = {}

        for key in ("database", "messages_jsonl", "messages_csv", "contacts_json",
                    "contacts_csv", "conversations", "statistics", "index_csv"):
            if not (folder / paths.ACCOUNT_FILES[key]).is_file():
                self.problems.add("missing_account_file",
                                  "A required account file is missing.",
                                  account=uid, path=f"{uid}/{paths.ACCOUNT_FILES[key]}")
        for key in ("pages", "transcripts"):
            if not (folder / paths.DIRECTORIES[key]).is_dir():
                self.problems.add("missing_account_directory",
                                  "A required account directory is missing.",
                                  account=uid, path=f"{uid}/{paths.DIRECTORIES[key]}")

        digests = self._scan_jsonl(uid, folder, result)
        self._scan_sqlite(uid, folder, result, digests)
        self._scan_csv(uid, folder, result)
        self._scan_pages(uid, folder, result)
        self._scan_conversations(uid, folder, result)
        self._scan_statistics(uid, folder, result)
        self._check_media_associations(uid)
        return result

    def _scan_jsonl(self, uid: str, folder: Path, result: dict) -> dict[str, str]:
        """Read messages.jsonl once, keeping digests and small metadata only."""
        digests: dict[str, str] = {}
        path = folder / paths.ACCOUNT_FILES["messages_jsonl"]
        if not path.is_file():
            return digests
        try:
            with path.open("r", encoding="utf-8") as handle:
                for number, line in enumerate(handle, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError as error:
                        self.problems.add("jsonl_line_invalid",
                                          f"messages.jsonl line {number} is not valid JSON: {error}",
                                          account=uid)
                        continue
                    meta = row.pop("_export", None)
                    uuid = str(row.get("msg_uuid") or "")
                    if not uuid:
                        self.problems.add("jsonl_row_without_uuid",
                                          "A JSONL row has no msg_uuid.", account=uid)
                        continue
                    if uuid in digests:
                        self.problems.add("jsonl_duplicate_uuid",
                                          "A message uuid appears more than once in JSONL.",
                                          account=uid, path=uuid)
                    digests[uuid] = _row_digest(row)
                    record = {"text": None, "local_media": [], "media": {}, "bubble": {}}
                    if not isinstance(meta, dict):
                        self.problems.add("jsonl_missing_export_metadata",
                                          "A JSONL row has no _export metadata block.",
                                          account=uid, path=uuid)
                    else:
                        if not isinstance(meta.get("text"), str):
                            self.problems.add("export_meta_text_not_string",
                                              "The _export.text field is not a string.",
                                              account=uid, path=uuid)
                        record["text"] = meta.get("text")
                        for value in meta.get("local_media") or []:
                            if isinstance(value, str):
                                record["local_media"].append(value)
                                if not (self.output / value).is_file():
                                    self.problems.add("local_media_missing",
                                                      "A message local_media path does not exist.",
                                                      account=uid, path=value)
                        if isinstance(meta.get("media"), dict):
                            record["media"] = meta["media"]
                        if isinstance(meta.get("bubble"), dict):
                            record["bubble"] = meta["bubble"]
                        account_uid = str(meta.get("account_uid") or "")
                        if account_uid and account_uid != uid:
                            self.problems.add("export_meta_account_mismatch",
                                              "The _export.account_uid differs from the folder name.",
                                              account=uid, path=uuid)
                    self._records[uuid] = digests[uuid]
                    self._meta[uuid] = record
        except OSError as error:
            self.problems.add("jsonl_unreadable", f"messages.jsonl could not be read: {error}",
                              account=uid)
        result["jsonl_rows"] = len(digests)
        return digests

    def _scan_sqlite(self, uid: str, folder: Path, result: dict,
                     digests: dict[str, str]) -> None:
        path = folder / paths.ACCOUNT_FILES["database"]
        if not path.is_file():
            return
        try:
            connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        except sqlite3.Error as error:
            self.problems.add("sqlite_unreadable",
                              f"chat.sqlite could not be opened: {error}", account=uid)
            return
        try:
            integrity = [row[0] for row in connection.execute("PRAGMA integrity_check")]
            if integrity != ["ok"]:
                result["sqlite_integrity"] = "; ".join(str(item) for item in integrity[:5])
                self.problems.add("sqlite_integrity_check_failed",
                                  "PRAGMA integrity_check did not return ok.",
                                  account=uid, path=result["sqlite_integrity"])
            else:
                result["sqlite_integrity"] = "ok"

            cursor = connection.execute("SELECT * FROM msg")
            columns = [description[0] for description in cursor.description]
            seen: set[str] = set()
            count = 0
            mismatched = 0
            for values in cursor:
                row = dict(zip(columns, values))
                uuid = str(row.get("msg_uuid") or "")
                seen.add(uuid)
                count += 1
                expected = digests.get(uuid)
                if expected is None or _row_digest(row) != expected:
                    mismatched += 1
                    if mismatched <= PROBLEM_SAMPLE_LIMIT:
                        self.problems.add("raw_row_mismatch",
                                          "A SQLite msg row does not match its JSONL raw row.",
                                          account=uid, path=uuid)
            result["sqlite_rows"] = count
            result["raw_rows_mismatched"] = mismatched
            result["raw_fields_match"] = mismatched == 0 and len(seen) == len(digests)

            for uuid in sorted(set(digests) - seen)[:PROBLEM_SAMPLE_LIMIT]:
                self.problems.add("raw_row_missing_in_sqlite",
                                  "A JSONL message has no matching SQLite msg row.",
                                  account=uid, path=uuid)
            for uuid in sorted(seen - set(digests))[:PROBLEM_SAMPLE_LIMIT]:
                self.problems.add("raw_row_missing_in_jsonl",
                                  "A SQLite msg row has no matching JSONL line.",
                                  account=uid, path=uuid)
            if count != len(digests):
                self.problems.add("row_count_mismatch",
                                  "SQLite msg row count differs from the JSONL row count.",
                                  account=uid, path=f"{count} != {len(digests)}")
        except sqlite3.Error as error:
            self.problems.add("sqlite_query_failed",
                              f"chat.sqlite could not be queried: {error}", account=uid)
        finally:
            connection.close()

    def _scan_csv(self, uid: str, folder: Path, result: dict) -> None:
        path = folder / paths.ACCOUNT_FILES["messages_csv"]
        if not path.is_file():
            return
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.reader(handle)
                try:
                    headings = next(reader)
                except StopIteration:
                    self.problems.add("csv_without_header",
                                      "messages.csv has no header row.", account=uid)
                    return
                id_columns = [index for index, name in enumerate(headings)
                              if ID_HEADER_PATTERN.search(name or "")]
                rows = 0
                for number, row in enumerate(reader, 2):
                    if len(row) != len(headings):
                        self.problems.add("csv_row_width_mismatch",
                                          "A CSV row has a different number of cells than the header.",
                                          account=uid, path=f"line {number}")
                        continue
                    rows += 1
                    for index, cell in enumerate(row):
                        if not cell:
                            continue
                        if (cell.lstrip()[:1] in FORMULA_PREFIXES
                                or cell[:1] in FORMULA_CONTROL_PREFIXES) \
                                and not cell.startswith("'"):
                            self.problems.add("csv_formula_injection",
                                              "A CSV cell starts with a formula prefix and is not neutralised.",
                                              account=uid, path=f"line {number} column {index + 1}")
                        if index in id_columns and cell.isdecimal() \
                                and len(cell) >= LONG_ID_MIN_LENGTH and not cell.startswith("'"):
                            self.problems.add("csv_long_id_unquoted",
                                              "A long numeric identifier is not protected from spreadsheet rounding.",
                                              account=uid, path=f"line {number} column {index + 1}")
                result["csv_rows"] = rows
                if rows != result["jsonl_rows"]:
                    self.problems.add("csv_row_count_mismatch",
                                      "messages.csv data rows differ from the JSONL row count.",
                                      account=uid, path=f"{rows} != {result['jsonl_rows']}")
        except OSError as error:
            self.problems.add("csv_unreadable", f"messages.csv could not be read: {error}",
                              account=uid)

    # -------------------------------------------------------------------- pages
    def _scan_pages(self, uid: str, folder: Path, result: dict) -> None:
        directory = folder / paths.DIRECTORIES["pages"]
        if not directory.is_dir():
            return
        seen: set[str] = set()
        rendered = 0
        for page in sorted(directory.glob("*.html")):
            scanner = _PageScanner(page, self.output, self.problems)
            try:
                scanner.feed(page.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                self.problems.add("page_unreadable",
                                  f"An export page could not be parsed: {error}",
                                  account=uid, path=page.relative_to(self.output).as_posix())
                continue
            rendered += scanner.messages
            result["local_references"] += scanner.local_references
            self.totals["rendered_stickers"] += len(scanner.stickers)
            self.totals["rendered_audio"] += len(scanner.audio)
            self.totals["rendered_inline_emoji"] += len(scanner.inline_emoji)
            self.totals["bubble_corners"] += scanner.bubble_corners
            self.totals["page_external_links"] += scanner.external_links
            for uuid in scanner.uuids:
                if uuid in seen:
                    self.problems.add("page_duplicate_message",
                                      "A message uuid is rendered on more than one page.",
                                      account=uid, path=uuid)
                seen.add(uuid)
                if uuid not in self._records:
                    self.problems.add("page_unknown_message",
                                      "A rendered message uuid is not present in messages.jsonl.",
                                      account=uid, path=uuid)
                    continue
                self._check_message_media(uid, uuid, scanner.per_message.get(uuid) or {})
            self._check_inline_emoji(uid, scanner)
            self._check_bubbles(uid, scanner)
        for uuid in sorted(set(self._records) - seen)[:PROBLEM_SAMPLE_LIMIT]:
            self.problems.add("message_not_rendered",
                              "A message from messages.jsonl is not rendered on any page.",
                              account=uid, path=uuid)
        result["html_messages"] = rendered
        if rendered != result["jsonl_rows"]:
            self.problems.add("rendered_message_count_mismatch",
                              "Rendered message count differs from the JSONL row count.",
                              account=uid, path=f"{rendered} != {result['jsonl_rows']}")

    def _check_message_media(self, uid: str, uuid: str, message: dict) -> None:
        """The media an 'ok' resource promises must be rendered exactly once."""
        resource = (self._meta.get(uuid) or {}).get("media") or {}
        if resource.get("status") != "ok":
            return
        path = resource.get("path")
        if not path:
            return
        references = list(message.get("media") or [])
        count = references.count(path)
        if count == 0:
            self.problems.add("media_not_rendered",
                              "A downloaded media resource is not rendered for its message.",
                              account=uid, path=path)
        elif count > 1:
            self.problems.add("media_reference_duplicate",
                              "A downloaded media resource is referenced more than once for its message.",
                              account=uid, path=path)
        if resource.get("kind") == "voice" and path not in (message.get("audio") or []):
            self.problems.add("voice_not_playable",
                              "A voice resource is not embedded as playable audio.",
                              account=uid, path=path)

    def _check_inline_emoji(self, uid: str, scanner: _PageScanner) -> None:
        inline = self.decorations.get("inline_emoji") or {}
        if not inline:
            return
        for token, reference in scanner.inline_emoji:
            asset = inline.get(token)
            if asset is None:
                self.problems.add("inline_emoji_unknown_token",
                                  "A rendered inline emoji token has no decoration manifest entry.",
                                  account=uid, path=token)
                continue
            expected = asset.get("path")
            if expected and expected not in reference.replace("../", ""):
                self.problems.add("inline_emoji_path_mismatch",
                                  "A rendered inline emoji does not use the manifest asset path.",
                                  account=uid, path=expected)

    def _check_bubbles(self, uid: str, scanner: _PageScanner) -> None:
        bubbles = self.decorations.get("bubbles") or {}
        for entry in scanner.bubbles:
            uuid = entry["uuid"]
            classes = entry["classes"]
            applied = "original-bubble" in classes
            if applied:
                self.totals["rendered_original_bubbles"] += 1
            meta_bubble = (self._meta.get(uuid) or {}).get("bubble") or {}
            if bool(meta_bubble.get("applied")) != applied:
                self.problems.add("bubble_render_mismatch",
                                  "The rendered original-bubble state differs from the message metadata.",
                                  account=uid, path=uuid)
            if not applied:
                continue
            theme = next((item for item in classes if item.startswith("theme-")), "")
            side = next((item for item in classes if item.startswith("side-")), "")
            if not theme or not side:
                self.problems.add("bubble_classes_incomplete",
                                  "An original bubble is missing its theme or side class.",
                                  account=uid, path=uuid)
                continue
            bubble_id = theme[len("theme-"):]
            side_id = side[len("side-"):]
            asset = ((bubbles.get(bubble_id) or {}).get("sides") or {}).get(side_id) or {}
            if not asset.get("path"):
                self.problems.add("bubble_asset_missing",
                                  "A rendered bubble has no matching asset in the decoration manifest.",
                                  account=uid, path=bubble_id)
            elif not (self.output / asset["path"]).is_file():
                self.problems.add("bubble_asset_file_missing",
                                  "A bubble asset referenced by the decoration manifest does not exist.",
                                  account=uid, path=asset["path"])

    # ----------------------------------------------------- conversations/statistics
    def _scan_conversations(self, uid: str, folder: Path, result: dict) -> None:
        text_directory = folder / paths.DIRECTORIES["transcripts"]
        if text_directory.is_dir():
            result["transcripts"] = len(list(text_directory.glob("*.txt")))
        metadata, error = _load_json(folder / paths.ACCOUNT_FILES["conversations"])
        if error:
            self.problems.add("account_json_unreadable", error, account=uid)
        elif not isinstance(metadata, dict):
            self.problems.add("account_json_invalid_shape",
                              "conversation_metadata.json is not a JSON object.", account=uid)
        index_path = folder / paths.ACCOUNT_FILES["index_csv"]
        if index_path.is_file():
            try:
                with index_path.open("r", encoding="utf-8-sig", newline="") as handle:
                    rows = list(csv.reader(handle))
                result["conversations"] = max(0, len(rows) - 1)
            except OSError as error:
                self.problems.add("csv_unreadable",
                                  f"conversation_index.csv could not be read: {error}",
                                  account=uid)

    def _scan_statistics(self, uid: str, folder: Path, result: dict) -> None:
        statistics, error = _load_json(folder / paths.ACCOUNT_FILES["statistics"])
        if error:
            self.problems.add("account_json_unreadable", error, account=uid)
            return
        if not isinstance(statistics, dict):
            self.problems.add("account_json_invalid_shape",
                              "statistics.json is not a JSON object.", account=uid)
            return
        if statistics.get("uid") not in (None, uid):
            self.problems.add("statistics_uid_mismatch",
                              "statistics.json records a different account uid.", account=uid)
        if statistics.get("message_count") not in (None, result["jsonl_rows"]):
            self.problems.add("statistics_message_count_mismatch",
                              "statistics.json message_count differs from the JSONL row count.",
                              account=uid)
        count = statistics.get("conversation_count")
        if count is not None and result["transcripts"] and count != result["transcripts"]:
            self.problems.add("conversation_count_mismatch",
                              "statistics.json conversation_count differs from the transcript count.",
                              account=uid)
        for entry in statistics.get("conversations") or []:
            for key in ("txt", "page", "latest_page"):
                value = entry.get(key)
                if value and not (folder / value).is_file():
                    self.problems.add("conversation_reference_missing",
                                      "A conversation reference in statistics.json does not exist.",
                                      account=uid, path=value)
        if result["transcripts"] and result["conversations"] \
                and result["transcripts"] != result["conversations"]:
            self.problems.add("transcript_count_mismatch",
                              "The transcript file count differs from the conversation index count.",
                              account=uid)

    # ------------------------------------------------------------ media manifest
    def _check_media_associations(self, uid: str) -> None:
        messages = self.manifest.get("messages") or {}
        if not messages:
            return
        prefix = uid + ":"
        for key, resource in messages.items():
            if not str(key).startswith(prefix):
                continue
            uuid = str(key)[len(prefix):]
            if uuid not in self._records:
                self.problems.add("media_manifest_unknown_message",
                                  "The media manifest references a message absent from messages.jsonl.",
                                  account=uid, path=uuid)
                continue
            meta_media = (self._meta.get(uuid) or {}).get("media") or {}
            if meta_media and meta_media != resource:
                self.problems.add("media_manifest_mismatch",
                                  "The JSONL _export.media entry differs from the media manifest entry.",
                                  account=uid, path=uuid)
            if resource.get("kind") == "voice" and resource.get("status") == "ok":
                # The summary counter counts manifest voice messages, not <audio>
                # elements: a message may also carry a legitimate local audio
                # attachment, which must not be mistaken for a voice resource.
                self.totals["manifest_voice_messages"] += 1
            if resource.get("status") != "ok":
                continue
            path = resource.get("path")
            if not path:
                self.problems.add("media_ok_without_path",
                                  "A media resource marked ok has no path.", account=uid, path=uuid)
            elif not (self.output / path).is_file():
                self.problems.add("media_file_missing",
                                  "A media resource marked ok points at a missing file.",
                                  account=uid, path=path)

    # ------------------------------------------------------------------ totals
    def _check_summary_totals(self) -> None:
        total = sum(account.get("jsonl_rows", 0) for account in self.accounts)
        recorded = self.summary.get("total_message_count")
        if recorded is not None and recorded != total:
            self.problems.add("summary_total_mismatch",
                              "export_summary.json total_message_count differs from the account total.",
                              path=f"{recorded} != {total}")
        self.totals["messages"] = total
        self.totals["accounts"] = len(self.accounts)
        self._check_reader_counters()

    def _check_reader_counters(self) -> None:
        presentation = self.summary.get("media_and_presentation") or {}
        rendered = presentation.get("rendered") or {}
        if not rendered:
            self.notes.append("reader counter cross-check skipped: summary has no rendered counters")
            self.reader = {"status": "passed", "note": "no rendered counters in export summary"}
            return
        expectations = {
            "inline_emoji_occurrences": self.totals["rendered_inline_emoji"],
            "original_bubble_messages": self.totals["rendered_original_bubbles"],
            "voice_ok": self.totals["manifest_voice_messages"],
            "emoji_ok": self.totals["rendered_stickers"],
        }
        mismatches = {}
        for name, actual in expectations.items():
            if name in rendered and rendered[name] != actual:
                mismatches[name] = {"summary": rendered[name], "rendered": actual}
                self.problems.add("reader_counter_mismatch",
                                  "A rendered-element counter differs from the export summary.",
                                  path=f"{name}: {rendered[name]} != {actual}")
        self.reader = {
            "status": "passed" if not mismatches else "failed",
            "summary_counters": dict(rendered),
            "rendered_counters": {name: expectations[name] for name in expectations},
            "audio_elements": self.totals["rendered_audio"],
            "mismatches": mismatches,
            "pages_checked": self.totals["messages"],
            "local_references": sum(account.get("local_references", 0)
                                    for account in self.accounts),
        }

    # --------------------------------------------------------------- media index
    def _check_media_index(self) -> None:
        for account in self.summary.get("accounts") or []:
            uid = str(account.get("uid") or "")
            database = self.output / uid / paths.ACCOUNT_FILES["database"]
            if not database.is_file():
                continue
            rows, has_table = self._read_media_index(database, uid)
            if not has_table:
                self.notes.append(
                    f"media index not present in {uid}/{paths.ACCOUNT_FILES['database']}; "
                    "index path checks skipped")
                continue
            manifest_paths = {str(resource.get("path")) for resource in
                              self.manifest.get("resources") or [] if resource.get("path")}
            for path, exists_flag in rows:
                if not path:
                    continue
                on_disk = (self.output / path).is_file()
                if bool(exists_flag) != on_disk:
                    self.problems.add(
                        "media_index_state_mismatch",
                        "export_local_attachment.exists_on_disk disagrees with the file system.",
                        account=uid, path=path)
                if on_disk:
                    self.totals["media_index_rows_checked"] += 1
                if manifest_paths and path not in manifest_paths:
                    self.problems.add(
                        "media_index_unknown_path",
                        "export_local_attachment references a path absent from the media manifest.",
                        account=uid, path=path)

    def _read_media_index(self, database: Path, uid: str):
        try:
            connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
        except sqlite3.Error as error:
            self.problems.add("media_index_unreadable",
                              f"chat.sqlite could not be opened for index checks: {error}",
                              account=uid)
            return [], False
        try:
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
            if "export_local_attachment" not in tables:
                return [], False
            rows = connection.execute(
                "SELECT path_rel_export, exists_on_disk FROM export_local_attachment").fetchall()
            return rows, True
        except sqlite3.Error as error:
            self.problems.add("media_index_unreadable",
                              f"export_local_attachment could not be read: {error}", account=uid)
            return [], True
        finally:
            connection.close()

    # ------------------------------------------------------------------- report
    def _report(self) -> dict:
        failed = self.problems.total > 0
        status = "failed" if failed else "passed"
        accounts = []
        for account in self.accounts:
            entry = dict(account)
            entry["ok"] = not any(
                sample.get("account") == account["uid"] for sample in self.problems.flatten())
            accounts.append(entry)
        report = {
            "status": status,
            "ok": not failed,
            "output": str(self.output),
            "decode_media": bool(self.decode_media),
            "generated_at": _now(),
            "accounts": accounts,
            "totals": {key: value for key, value in self.totals.items()
                       if isinstance(value, int)},
            "problem_counts": dict(self.problems.counts),
            "problems": self.problems.flatten(),
            "problem_count": self.problems.total,
            "notes": list(self.notes),
            "checks": {
                "sqlite_integrity": "PRAGMA integrity_check must return ok for every chat.sqlite",
                "raw_row_equality": "Each messages.jsonl raw row digest must equal its msg row digest",
                "counts": "JSONL, SQLite, CSV, rendered pages and statistics must agree",
                "csv_safety": "Formula prefixes neutralised and long numeric identifiers quoted",
                "local_links": "Every local href/src/url() resolves to a file inside the export",
                "offline_media": "Media elements never load remote URLs; original web references stay anchors",
                "media_associations": "Each ok media resource exists and is rendered exactly once for its message",
                "media_decode": "Optional ffmpeg audio decode to null and Pillow full-frame image decode",
            },
            "reader": self.reader,
            "media": None if self.media_report is None else {
                "status": self.media_report.get("status"),
                "summary": self.media_report.get("summary"),
            },
            "reports": {},
        }
        return report

    def write_reports(self, report: dict) -> None:
        paths_written = {}
        reader_document = {
            "status": report["status"],
            "generated_at": report["generated_at"],
            "output": report["output"],
            **self.reader,
            "problems": [item for item in report["problems"]
                         if item["code"] in READER_PROBLEM_CODES],
        }
        _write_json(self.output / READER_REPORT, reader_document)
        paths_written["reader"] = READER_REPORT

        if self.media_report is not None:
            self.media_report.setdefault("generated_at", report["generated_at"])
            self.media_report.setdefault("output", report["output"])
            self.media_report["problem_counts"] = dict(self.media_report["problem_counts"])
            _write_json(self.output / MEDIA_REPORT, self.media_report)
            paths_written["media"] = MEDIA_REPORT

        report["reports"] = paths_written
        _write_json(self.output / DATA_REPORT, report)
        report["reports"]["validation"] = DATA_REPORT

    # -------------------------------------------------------------- media decode
    def _decode_media(self) -> dict:
        problems = Problems()
        summary = collections.Counter()
        resources = [item for item in self.manifest.get("resources") or []
                     if isinstance(item, dict)]
        verified = [item for item in resources if item.get("status") == "ok"]
        summary["resources_total"] = len(resources)
        summary["resources_ok"] = len(verified)
        summary["resources_not_ok"] = len(resources) - len(verified)

        targets = []
        for item in verified:
            path = item.get("path")
            if not path:
                problems.add("media_ok_without_path", "A resource marked ok has no path.",
                             path=str(item.get("resource_id")))
                continue
            full = self.output / path
            if not full.is_file():
                problems.add("media_file_missing", "A resource marked ok is missing on disk.",
                             path=path)
                continue
            expected = item.get("bytes")
            actual = full.stat().st_size
            if expected is not None and expected != actual:
                problems.add("media_bytes_mismatch", "A downloaded resource has a different size.",
                             path=path, expected=expected, actual=actual)
            targets.append((item, full))

        def work(entry):
            item, full = entry
            digest = _sha256_file(full)
            expected = item.get("sha256")
            if expected and expected != digest:
                problems.add("media_sha256_mismatch", "A downloaded resource failed its sha256 check.",
                             path=item.get("path"))
            kind = item.get("kind")
            if kind == "voice":
                ok, detail = _decode_audio(full, self.ffmpeg)
                if not ok:
                    problems.add("audio_decode_failed", "ffmpeg could not decode this voice file.",
                                 path=item.get("path"), detail=detail)
                summary["voice_decoded"] += 1
            elif kind == "emoji":
                info, decode_problems = _decode_image(full, item)
                for code, message in decode_problems:
                    problems.add(code, message, path=item.get("path"))
                summary["images_decoded"] += 1
                summary["frames_decoded"] += int(info.get("frames_decoded") or 0)
                if (info.get("frames") or 1) > 1:
                    summary["animated_decoded"] += 1
            return item

        if targets:
            with concurrent.futures.ThreadPoolExecutor(max_workers=DECODE_WORKERS) as pool:
                list(pool.map(work, targets, chunksize=8))

        decorations = self._decode_decorations(problems, summary)
        return {
            "status": "failed" if problems.total else "passed",
            "generated_at": _now(),
            "output": str(self.output),
            "decode_media": True,
            "ffmpeg": self.ffmpeg,
            "summary": dict(summary),
            "problem_counts": dict(problems.counts),
            "problems": problems.flatten(),
            "problem_count": problems.total,
            "decorations": decorations,
            "checks": {
                "exists": "Every ok resource and decoration file must exist",
                "bytes": "File size must equal the manifest bytes when recorded",
                "sha256": "sha256 must equal the manifest digest when recorded",
                "voice": "ffmpeg -v error -i <file> -f null - must exit 0 with no stderr",
                "image": "Pillow must decode every frame; frame count must match the manifest",
            },
        }

    def _decode_decorations(self, problems: Problems, summary) -> dict:
        bubbles = self.decorations.get("bubbles") or {}
        inline = self.decorations.get("inline_emoji") or {}
        entries = []
        for bubble_id, payload in bubbles.items():
            for side, asset in ((payload or {}).get("sides") or {}).items():
                entries.append(("bubble", str(bubble_id), str(side), asset or {}))
            for corner, asset in ((payload or {}).get("corners") or {}).items():
                entries.append(("bubble_corner", str(bubble_id), str(corner), asset or {}))
        for token, asset in inline.items():
            entries.append(("inline_emoji", str(token), "", asset or {}))

        decoded = 0
        for kind, owner, side, asset in entries:
            path = asset.get("path")
            if not path:
                problems.add("decoration_without_path", "A decoration entry has no path.",
                             path=f"{kind}:{owner}")
                continue
            full = self.output / path
            if not full.is_file():
                problems.add("decoration_file_missing", "A decoration file does not exist.",
                             path=path)
                continue
            expected = asset.get("sha256")
            if expected and _sha256_file(full) != expected:
                problems.add("decoration_sha256_mismatch", "A decoration file failed its sha256 check.",
                             path=path)
            info, decode_problems = _decode_image(full, asset)
            for code, message in decode_problems:
                problems.add(code, message, path=path)
            decoded += 1
            if (info.get("frames") or 1) > 1:
                summary["decoration_animated"] += 1
        summary["decorations_decoded"] = decoded
        summary["decorations_total"] = len(entries)
        return {"entries": len(entries), "decoded": decoded}


READER_PROBLEM_CODES = {
    "local_link_missing", "local_link_outside_export", "remote_media_reference",
    "message_anchor_malformed", "page_duplicate_message", "page_unknown_message",
    "message_not_rendered", "rendered_message_count_mismatch", "media_not_rendered",
    "media_reference_duplicate", "voice_not_playable", "inline_emoji_unknown_token",
    "inline_emoji_path_mismatch", "bubble_render_mismatch", "bubble_classes_incomplete",
    "bubble_asset_missing", "bubble_asset_file_missing", "reader_counter_mismatch",
    "page_unreadable", "index_unreadable", "missing_export_file",
}


def _write_json(path: Path, document: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(temporary, path)


def _decode_audio(path: Path, ffmpeg: str):
    executable = shutil.which(ffmpeg) or ffmpeg
    command = [executable, "-nostdin", "-hide_banner", "-v", "error",
               "-i", str(path), "-f", "null", "-"]
    try:
        completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   timeout=FFMPEG_TIMEOUT)
    except FileNotFoundError:
        return False, f"ffmpeg not found: {ffmpeg}"
    except subprocess.TimeoutExpired:
        return False, "ffmpeg timed out"
    message = completed.stderr.decode("utf-8", "replace").strip()
    if completed.returncode != 0 or message:
        return False, (message or f"ffmpeg exit code {completed.returncode}")[-400:]
    return True, ""


def _decode_image(path: Path, item: dict):
    """Decode every frame with Pillow and compare format/size/frames."""
    problems: list[tuple[str, str]] = []
    try:
        from PIL import Image
    except ImportError:
        return {}, [("pillow_missing",
                     "Pillow is not installed, so images cannot be decoded.")]
    Image.MAX_IMAGE_PIXELS = None
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except Exception:  # HEIF support is optional
        pass
    info: dict = {}
    try:
        with Image.open(path) as image:
            info["format"] = image.format
            info["width"], info["height"] = image.size
            frames = getattr(image, "n_frames", 1)
            info["frames"] = frames
            decoded = 0
            if frames > 1:
                for index in range(frames):
                    image.seek(index)
                    image.load()
                    decoded += 1
            else:
                image.load()
                decoded = 1
            info["frames_decoded"] = decoded
    except Exception as error:  # any Pillow failure means the file is unusable
        return info, [("image_decode_failed",
                       f"Pillow could not decode this image: {type(error).__name__}: {error}")]
    expected_frames = item.get("frames")
    if expected_frames is not None and info.get("frames") != expected_frames:
        problems.append(("image_frame_count_mismatch",
                         f"Decoded frame count {info.get('frames')} differs from the manifest "
                         f"value {expected_frames}."))
    expected_size = (item.get("width"), item.get("height"))
    if None not in expected_size and (info.get("width"), info.get("height")) != expected_size:
        problems.append(("image_size_mismatch",
                         f"Decoded size {info.get('width')}x{info.get('height')} differs from the "
                         f"manifest value {expected_size[0]}x{expected_size[1]}."))
    return info, problems


def validate_export(output: Path, *, decode_media: bool = False,
                    ffmpeg: str = "ffmpeg") -> dict:
    """Validate one export tree and write the English validation reports."""
    output = Path(output).expanduser().resolve()
    validator = _Validator(output, decode_media=decode_media, ffmpeg=ffmpeg)
    report = validator.run()
    validator.write_reports(report)
    return report
