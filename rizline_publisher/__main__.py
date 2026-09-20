import argparse
import json
import sys
from pathlib import Path

from .core import apply_overrides, build, load_overrides, publish, read_json, rollback, validate_catalog, validate_release
from .upstream import STATS_URL, import_catalog
from .publication import retry_cleanup


def main(argv=None):
    parser = argparse.ArgumentParser(description="Rizline personal resource importer and immutable S3 publisher")
    parser.add_argument("--work", type=Path, default=Path("work"))
    parser.add_argument("--cache", type=Path, default=Path(".cache/http"))
    parser.add_argument("--output", type=Path, default=Path("dist"))
    parser.add_argument("--overrides", type=Path, default=Path("overrides.json"))
    sub = parser.add_subparsers(dest="command", required=True)
    importer = sub.add_parser("import", help="Import the complete current official catalog, covers, charts and audio")
    importer.add_argument("--transport", choices=("auto", "urllib", "powershell"), default="auto")
    importer.add_argument("--workers", type=int, choices=range(1, 9), default=4)
    importer.add_argument("--stats-url", default=STATS_URL, help="Pinned statistics source; empty string disables enrichment")
    validator = sub.add_parser("validate", help="Validate imported metadata or the built release")
    validator.add_argument("--release", action="store_true")
    validator.add_argument("--workers", type=int, choices=range(1, 17), default=4)
    builder = sub.add_parser("build", help="Build deterministic immutable release and current.json")
    builder.add_argument("--workers", type=int, choices=range(1, 17), default=4)
    rollback_parser = sub.add_parser("rollback", help="Validate and select an existing local release; does not upload")
    rollback_parser.add_argument("resource_version")
    publisher = sub.add_parser("publish", help="Show upload plan; --execute writes configured S3 bucket")
    publisher.add_argument("--execute", action="store_true")
    publisher.add_argument("--workers", type=int, choices=range(1, 17), default=4, help="Parallel resource upload/verification jobs (default: 4)")
    publisher.add_argument("--report", type=Path, help="Publication success/failure JSON report")
    publisher.add_argument("--publication-output", type=Path, help="Exact selected publication artifact directory")
    publisher.add_argument("--cleanup-receipt", type=Path, help="Local exact-snapshot cleanup retry receipt")
    publisher.add_argument("--delta-only", action="store_true", help="Refuse a full upload unless a comparable remote release exists")
    cleanup = sub.add_parser("cleanup", help="Inspect or retry recorded leftover release deletions")
    cleanup.add_argument("--receipt", type=Path, required=True)
    cleanup.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "import":
            report = import_catalog(args.work, args.cache, args.overrides, args.transport, args.workers, args.stats_url or None)
            result = {"summary": report["summary"], "report": str(args.work / "import-report.json"), "unresolvedStatistics": len(report["unresolvedStatistics"]), "unresolvedAchievements": len(report["unresolvedAchievements"])}
        elif args.command == "validate":
            result = validate_release(args.output, workers=args.workers) if args.release else validate_catalog(apply_overrides(read_json(args.work / "catalog.json"), load_overrides(args.overrides)))
        elif args.command == "build":
            result = build(args.work / "catalog.json", args.overrides, args.output, workers=args.workers)
        elif args.command == "rollback":
            result = rollback(args.output, args.resource_version)
        elif args.command == "cleanup":
            result = retry_cleanup(args.receipt, args.execute)
        else:
            result = publish(args.output, args.execute, workers=args.workers, report_path=args.report,
                             publication_output=args.publication_output, cleanup_receipt=args.cleanup_receipt,
                             delta_only=args.delta_only)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, RuntimeError) as error:
        print("Error: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
