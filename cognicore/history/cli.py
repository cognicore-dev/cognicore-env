"""Command-line access to private retrospective memory."""

import json
import sys
from pathlib import Path

from cognicore.sync import SyncError

from .engine import open_history
from .extraction import KINDS


def register(sub):
    parser = sub.add_parser("history", help="Import and search private historical conversations")
    commands = parser.add_subparsers(dest="history_command", required=True)

    ingest = commands.add_parser("import", help="Import a ChatGPT JSON or ZIP export")
    ingest.add_argument("archive", type=Path)
    ingest.add_argument("--consent", action="store_true", help="Confirm permission to store this archive locally")
    ingest.add_argument("--project", default="")
    ingest.add_argument("--no-redact", action="store_true", help="Store source content without secret redaction")

    for name in ("search", "timeline", "reconstruct"):
        query = commands.add_parser(name)
        query.add_argument("query", nargs="?" if name == "timeline" else None, default="")
        query.add_argument("--project", default="")
        query.add_argument("--limit", type=int, default=20)
        if name != "reconstruct":
            query.add_argument("--kind", choices=("message", *KINDS),
                               default="")
        if name == "timeline":
            query.add_argument("--earliest", action="store_true")

    inspect = commands.add_parser("inspect")
    inspect.add_argument("conversation")
    inspect.add_argument("--message", default="")
    inspect.add_argument("--limit", type=int, default=50)
    inspect.add_argument("--offset", type=int, default=0)

    commands.add_parser("status").add_argument("batch", nargs="?", default="")
    commands.add_parser("experience").add_argument("ref")
    commands.add_parser("audit").add_argument("--limit", type=int, default=50)

    sync = commands.add_parser("sync", help="Synchronize the stored archive with the signed-in account")
    sync.add_argument("sync_action", choices=("enable", "disable", "now", "status"), default="status", nargs="?")
    sync.add_argument("--consent", action="store_true", help="Authorize uploading the entire local archive")

    delete = commands.add_parser("delete")
    target = delete.add_mutually_exclusive_group(required=True)
    target.add_argument("--batch", default="")
    target.add_argument("--conversation", default="")

    link = commands.add_parser("link")
    link.add_argument("origin")
    link.add_argument("target")
    link.add_argument("kind", choices=("related_approach", "conflict", "supersedes"))

    evidence = commands.add_parser("evidence", help="Attach a receipt for execution observed by the local operator")
    evidence.add_argument("ref")
    evidence.add_argument("--command", required=True)
    evidence.add_argument("--exit-code", required=True, type=int)
    evidence.add_argument("--output-file", required=True, type=Path)
    evidence.add_argument("--timestamp", required=True)
    evidence.add_argument("--source", required=True)
    evidence.add_argument("--commit", default="")
    evidence.add_argument("--reverify", action="store_true")


def run(args):
    from . import sync

    values = vars(args).copy()
    values.pop("command")
    action = values.pop("history_command")
    try:
        if action == "import" and not values["consent"]:
            raise PermissionError("Explicit consent is required. Pass --consent to authorize local storage.")
        engine = open_history()
        if action == "sync":
            operation = values["sync_action"]
            if operation == "enable":
                result = sync.enable(engine, consent=values["consent"])
            elif operation == "now":
                result = sync.synchronize(engine, discover=True)
            else:
                result = {"status": sync.status, "disable": sync.disable}[operation](engine)
        elif action == "import":
            path = values.pop("archive")
            result = engine.import_archive(path, redact=not values.pop("no_redact"), **values)
        elif action == "link":
            engine.relate(**values)
            result = {"linked": True}
        elif action == "evidence":
            output = values.pop("output_file").read_bytes()
            result = engine.attach_evidence(output=output, **values)
        elif action == "delete":
            engine.delete(**values)
            result = {"deleted": True}
        else:
            result = getattr(engine, action)(**values)
        if action != "sync":
            sync.schedule(mutated=action in {"import", "link", "evidence", "delete"})
        print(json.dumps(result, indent=2))
        return 0
    except (OSError, KeyError, ValueError, SyncError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
