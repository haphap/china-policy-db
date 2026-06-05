"""Command line interface for updating and validating the data repo."""

from __future__ import annotations

import argparse
import json
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from . import gov_policy, pboc_ops
from .config import data_root
from .exceptions import DataVendorUnavailable


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise DataVendorUnavailable(f"Missing JSONL file: {path}")
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
            except ValueError as exc:
                raise DataVendorUnavailable(f"Invalid JSONL in {path}:{lineno}: {exc}") from exc
            if not isinstance(record, dict):
                raise DataVendorUnavailable(f"JSONL record in {path}:{lineno} is not an object")
            records.append(record)
    return records


def _date_bounds(records: Iterable[dict[str, Any]]) -> tuple[str | None, str | None]:
    dates = sorted(
        str(record.get("pub_date") or "")
        for record in records
        if str(record.get("pub_date") or "")
    )
    if not dates:
        return None, None
    return dates[0], dates[-1]


def _validate_records(
    records: list[dict[str, Any]],
    *,
    required_fields: tuple[str, ...],
    dataset: str,
) -> None:
    seen: set[str] = set()
    for idx, record in enumerate(records, start=1):
        missing = [field for field in required_fields if not record.get(field)]
        if missing:
            raise DataVendorUnavailable(
                f"{dataset} record {idx} missing required fields: {', '.join(missing)}"
            )
        article_id = str(record.get("article_id"))
        if article_id in seen:
            raise DataVendorUnavailable(f"{dataset} duplicate article_id: {article_id}")
        seen.add(article_id)


def build_catalog(root: Path | None = None) -> dict[str, Any]:
    root = root or data_root()
    datasets = {
        "pboc_ops": {
            "jsonl": root / "pboc_ops" / "parsed" / "articles.jsonl",
            "csv": root / "pboc_ops" / "parsed" / "articles.csv",
            "required": ("article_id", "pub_date", "category_id", "title", "url"),
        },
        "gov_policy": {
            "jsonl": root / "gov_policy" / "parsed" / "policy_documents.jsonl",
            "csv": root / "gov_policy" / "parsed" / "policy_documents.csv",
            "required": ("article_id", "pub_date", "category_id", "title", "url"),
        },
    }

    catalog: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": _utc_now(),
        "datasets": {},
    }
    for name, spec in datasets.items():
        records = _read_jsonl(spec["jsonl"])
        _validate_records(records, required_fields=spec["required"], dataset=name)
        csv_path = spec["csv"]
        if not csv_path.is_file():
            raise DataVendorUnavailable(f"Missing CSV file: {csv_path}")
        first_date, last_date = _date_bounds(records)
        catalog["datasets"][name] = {
            "record_count": len(records),
            "first_pub_date": first_date,
            "last_pub_date": last_date,
            "jsonl_path": str(spec["jsonl"].relative_to(root)),
            "csv_path": str(csv_path.relative_to(root)),
        }
    return catalog


def write_catalog(root: Path | None = None) -> dict[str, Any]:
    root = root or data_root()
    catalog = build_catalog(root)
    path = root / "manifest.json"
    path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return catalog


def _export() -> dict[str, Any]:
    root = data_root()
    pboc_records = pboc_ops.load_pboc_open_market_records(root / "pboc_ops")
    pboc_ops._write_articles(root / "pboc_ops", pboc_records)
    gov_records = gov_policy.load_gov_policy_records(root / "gov_policy")
    gov_policy._write_records(root / "gov_policy", gov_records)
    return write_catalog(root)


def _default_date_window(days: int = 10) -> tuple[str, str]:
    end = date.today()
    start = end - timedelta(days=days)
    return start.isoformat(), end.isoformat()


def _gov_policy_fetcher_with_delay(delay_seconds: float) -> gov_policy.FetchJson | None:
    if delay_seconds <= 0:
        return None

    def fetcher(params: dict[str, Any]) -> dict[str, Any]:
        try:
            return gov_policy._fetch_json(params)
        finally:
            time.sleep(delay_seconds)

    return fetcher


def _parse_date(value: str, label: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise DataVendorUnavailable(f"{label} must be YYYY-MM-DD, got {value!r}") from exc


def _window_ranges(start_date: str, end_date: str, window_days: int) -> list[tuple[str, str]]:
    if window_days <= 0:
        raise DataVendorUnavailable("window_days must be > 0")
    start = _parse_date(start_date, "start_date")
    end = _parse_date(end_date, "end_date")
    if start > end:
        raise DataVendorUnavailable("start_date must be <= end_date")
    windows: list[tuple[str, str]] = []
    cursor = start
    while cursor <= end:
        window_end = min(cursor + timedelta(days=window_days - 1), end)
        windows.append((cursor.isoformat(), window_end.isoformat()))
        cursor = window_end + timedelta(days=1)
    return windows


def _cmd_update_pboc(args: argparse.Namespace) -> int:
    run = pboc_ops.crawl_pboc_open_market(
        cache_dir=data_root() / "pboc_ops",
        full=args.full,
        max_pages_per_category=args.max_pages_per_category,
        categories=args.category,
        force=args.force,
        on_log=lambda msg: print(msg, flush=True),
    )
    catalog = _export()
    print(json.dumps({"run": run, "catalog": catalog["datasets"]["pboc_ops"]}, ensure_ascii=False, indent=2))
    return 0


def _cmd_update_gov_policy(args: argparse.Namespace) -> int:
    start_date = args.start_date
    end_date = args.end_date
    if not start_date or not end_date:
        start_date, end_date = _default_date_window()
    run = gov_policy.crawl_gov_policy_documents(
        cache_dir=data_root() / "gov_policy",
        start_date=start_date,
        end_date=end_date,
        max_pages_per_category=args.max_pages_per_category,
        page_size=args.page_size,
        categories=args.category,
        fetcher=_gov_policy_fetcher_with_delay(args.request_delay_seconds),
        q=args.q,
        on_log=lambda msg: print(msg, flush=True),
    )
    catalog = _export()
    print(json.dumps({"run": run, "catalog": catalog["datasets"]["gov_policy"]}, ensure_ascii=False, indent=2))
    return 0


def _cmd_backfill_gov_policy(args: argparse.Namespace) -> int:
    root = data_root() / "gov_policy"
    windows = _window_ranges(args.start_date, args.end_date, args.window_days)
    if args.max_windows:
        windows = windows[: args.max_windows]

    completed: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for start_date, end_date in windows:
        try:
            print(f"gov.cn policy backfill {start_date} -> {end_date}", flush=True)
            run = gov_policy.crawl_gov_policy_documents(
                cache_dir=root,
                start_date=start_date,
                end_date=end_date,
                max_pages_per_category=args.max_pages_per_category,
                page_size=args.page_size,
                categories=args.category,
                fetcher=_gov_policy_fetcher_with_delay(args.request_delay_seconds),
                q=args.q,
            )
            completed.append({"start_date": start_date, "end_date": end_date, **run})
        except Exception as exc:  # noqa: BLE001
            failures.append({"start_date": start_date, "end_date": end_date, "error": str(exc)})
            print(f"gov.cn policy backfill failed {start_date} -> {end_date}: {exc}", flush=True)

    catalog = _export()
    manifest = gov_policy._load_manifest(root)
    manifest["last_backfill"] = {
        "started_at": completed[0]["started_at"] if completed else None,
        "finished_at": _utc_now(),
        "requested_start_date": args.start_date,
        "requested_end_date": args.end_date,
        "window_days": args.window_days,
        "completed_windows": len(completed),
        "failed_windows": len(failures),
    }
    manifest["backfill_failures"] = failures
    gov_policy._write_json(gov_policy._manifest_path(root), manifest)

    print(
        json.dumps(
            {
                "completed": completed,
                "failures": failures,
                "catalog": catalog["datasets"]["gov_policy"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 1 if failures and not completed else 0


def _cmd_export(_: argparse.Namespace) -> int:
    catalog = _export()
    print(json.dumps(catalog, ensure_ascii=False, indent=2))
    return 0


def _cmd_validate(_: argparse.Namespace) -> int:
    catalog = write_catalog()
    print(json.dumps(catalog, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="china-policy-db")
    subparsers = parser.add_subparsers(dest="command", required=True)

    update_parser = subparsers.add_parser("update")
    update_subparsers = update_parser.add_subparsers(dest="source", required=True)

    pboc_parser = update_subparsers.add_parser("pboc")
    pboc_parser.add_argument("--full", action="store_true", help="crawl every discovered list page")
    pboc_parser.add_argument("--force", action="store_true", help="reparse articles even when raw checksum is unchanged")
    pboc_parser.add_argument("--max-pages-per-category", type=int, default=2)
    pboc_parser.add_argument("--category", action="append", choices=sorted(pboc_ops._CATEGORY_BY_ID))
    pboc_parser.set_defaults(func=_cmd_update_pboc)

    gov_parser = update_subparsers.add_parser("gov-policy")
    gov_parser.add_argument("--start-date")
    gov_parser.add_argument("--end-date")
    gov_parser.add_argument("--max-pages-per-category", type=int, default=3)
    gov_parser.add_argument("--page-size", type=int, default=50)
    gov_parser.add_argument("--category", action="append", choices=sorted(gov_policy._CATEGORY_BY_ID))
    gov_parser.add_argument("--request-delay-seconds", type=float, default=0.0)
    gov_parser.add_argument("--q", default="")
    gov_parser.set_defaults(func=_cmd_update_gov_policy)

    export_parser = subparsers.add_parser("export")
    export_parser.set_defaults(func=_cmd_export)

    backfill_parser = subparsers.add_parser("backfill")
    backfill_subparsers = backfill_parser.add_subparsers(dest="source", required=True)

    gov_backfill_parser = backfill_subparsers.add_parser("gov-policy")
    gov_backfill_parser.add_argument("--start-date", required=True)
    gov_backfill_parser.add_argument("--end-date", required=True)
    gov_backfill_parser.add_argument("--window-days", type=int, default=31)
    gov_backfill_parser.add_argument("--max-windows", type=int, default=0)
    gov_backfill_parser.add_argument("--max-pages-per-category", type=int, default=20)
    gov_backfill_parser.add_argument("--page-size", type=int, default=50)
    gov_backfill_parser.add_argument("--category", action="append", choices=sorted(gov_policy._CATEGORY_BY_ID))
    gov_backfill_parser.add_argument("--request-delay-seconds", type=float, default=0.0)
    gov_backfill_parser.add_argument("--q", default="")
    gov_backfill_parser.set_defaults(func=_cmd_backfill_gov_policy)

    validate_parser = subparsers.add_parser("validate")
    validate_parser.set_defaults(func=_cmd_validate)

    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except DataVendorUnavailable as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
