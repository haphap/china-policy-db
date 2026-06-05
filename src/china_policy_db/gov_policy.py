"""State Council policy document library crawler.

The public ``gov.cn`` policy document library redirects to a Vue application at
``sousuo.www.gov.cn``.  The SPA loads policy data from ``/search-gov/data``;
this module keeps a small parsed cache of that JSON endpoint so macro agents
can use policy documents without relying on Tushare ``news`` permissions.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from typing import Any, Callable, Iterable

from .exceptions import DataVendorUnavailable

logger = logging.getLogger(__name__)

GOV_POLICY_ENTRY_URL = "https://www.gov.cn/zhengce/zhengcewenjianku/index.htm"
GOV_POLICY_LIBRARY_URL = (
    "https://sousuo.www.gov.cn/zcwjk/policyDocumentLibrary?q=&t=zhengcelibrary&orpro="
)
GOV_POLICY_SEARCH_API = "https://sousuo.www.gov.cn/search-gov/data"


@dataclass(frozen=True)
class GovPolicyCategory:
    id: str
    name: str
    query_t: str


GOV_POLICY_CATEGORIES: tuple[GovPolicyCategory, ...] = (
    GovPolicyCategory("gongwen", "国务院文件", "zhengcelibrary_gw"),
    GovPolicyCategory("bumenfile", "国务院部门文件", "zhengcelibrary_bm"),
    GovPolicyCategory("otherfile", "政策解读", "zhengcelibrary_or"),
    GovPolicyCategory("gongbao", "国务院公报", "zhengcelibrary_gb"),
)

_CATEGORY_BY_ID = {category.id: category for category in GOV_POLICY_CATEGORIES}
_CATEGORY_BY_QUERY_T = {category.query_t: category for category in GOV_POLICY_CATEGORIES}
_CATEGORY_BY_CATMAP = {category.id: category for category in GOV_POLICY_CATEGORIES}
_DEFAULT_PAGE_SIZE = 50
_DEFAULT_MAX_PAGES = 3
_HTTP_TIMEOUT_SECONDS = 20
_HTTP_RETRIES = 2
_DATE_FMT = "%Y-%m-%d"
_TAG_RE = re.compile(r"<[^>]+>")
_DATE_RE = re.compile(r"(\d{4})[-/年.]?(\d{1,2})[-/月.]?(\d{1,2})")
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)
_SEARCH_FIELD = "title:content:summary"

FetchJson = Callable[[dict[str, Any]], dict[str, Any]]


def gov_policy_cache_dir(cache_dir: str | Path | None = None) -> Path:
    """Return the root directory for cached gov.cn policy files."""
    if cache_dir is not None:
        return Path(cache_dir)
    from .config import get_config  # noqa: PLC0415

    configured = get_config().get("data_cache_dir")
    if not configured:
        raise DataVendorUnavailable("data_cache_dir is not configured.")
    return Path(configured) / "gov_policy"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalise_text(value: Any) -> str:
    text = "" if value is None else str(value)
    text = unescape(_TAG_RE.sub(" ", text))
    text = text.replace("\u3000", " ")
    return re.sub(r"\s+", " ", text).strip()


def _normalise_terms(keywords: Iterable[str] | str | None) -> list[str]:
    if keywords is None:
        return []
    raw_terms: Iterable[str]
    if isinstance(keywords, str):
        raw_terms = (keywords,)
    else:
        raw_terms = keywords
    return [term for keyword in raw_terms if (term := _normalise_text(keyword))]


def _normalise_date(value: Any) -> str:
    text = _normalise_text(value)
    match = _DATE_RE.search(text)
    if not match:
        return ""
    year, month, day = match.groups()
    try:
        return datetime(int(year), int(month), int(day)).strftime(_DATE_FMT)
    except ValueError:
        return ""


def _parse_iso_date(value: str, label: str = "date") -> datetime:
    try:
        return datetime.strptime(value, _DATE_FMT)
    except ValueError as exc:
        raise DataVendorUnavailable(
            f"{label} must be in YYYY-MM-DD format, got {value!r}: {exc}"
        ) from exc


def _date_window(curr_date: str, look_back_days: int) -> tuple[str, str]:
    end_dt = _parse_iso_date(curr_date, "curr_date")
    if look_back_days < 0:
        raise DataVendorUnavailable("look_back_days must be >= 0.")
    start_dt = end_dt - timedelta(days=look_back_days)
    return start_dt.strftime(_DATE_FMT), end_dt.strftime(_DATE_FMT)


def _category_from_id(category_id: str | GovPolicyCategory) -> GovPolicyCategory:
    if isinstance(category_id, GovPolicyCategory):
        return category_id
    try:
        return _CATEGORY_BY_ID[category_id]
    except KeyError as exc:
        raise DataVendorUnavailable(f"Unknown gov.cn policy category: {category_id}") from exc


def _article_id(row: dict[str, Any], category: GovPolicyCategory) -> str:
    row_id = _normalise_text(row.get("id"))
    if row_id:
        return f"{category.id}:{row_id}"
    url = _normalise_text(row.get("url"))
    if url:
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
        return f"{category.id}:{digest}"
    payload = json.dumps(row, ensure_ascii=False, sort_keys=True)
    return f"{category.id}:{hashlib.sha1(payload.encode('utf-8')).hexdigest()[:16]}"


def _network_disabled() -> bool:
    raw = os.getenv("MOSAIC_GOV_POLICY_DISABLE_NETWORK") or os.getenv(
        "MOSAIC_GOV_POLICY_OFFLINE"
    )
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


def _query_params(
    category: GovPolicyCategory,
    *,
    start_date: str,
    end_date: str,
    page: int,
    page_size: int,
    q: str = "",
) -> dict[str, Any]:
    return {
        "t": category.query_t,
        "q": q,
        "timetype": "timezd",
        "mintime": start_date,
        "maxtime": end_date,
        "sort": "pubtime",
        "sortType": 1,
        "searchfield": _SEARCH_FIELD,
        "p": page,
        "n": page_size,
        "pcodeJiguan": "",
        "childtype": "",
        "subchildtype": "",
        "tsbq": "",
        "pubtimeyear": "",
        "puborg": "",
        "pcodeYear": "",
        "pcodeNum": "",
        "filetype": "",
        "inpro": "",
        "bmfl": "",
        "dup": "",
        "orpro": "",
        "bmpubyear": "",
    }


def _fetch_json(params: dict[str, Any]) -> dict[str, Any]:
    if _network_disabled():
        raise DataVendorUnavailable(
            "gov.cn policy network refresh is disabled by MOSAIC_GOV_POLICY_DISABLE_NETWORK."
        )
    try:
        import requests  # noqa: PLC0415
    except ImportError as exc:
        raise DataVendorUnavailable("requests is required to fetch gov.cn policy data.") from exc

    response = requests.get(
        GOV_POLICY_SEARCH_API,
        params=params,
        headers={
            "User-Agent": _USER_AGENT,
            "Referer": GOV_POLICY_LIBRARY_URL,
            "Accept": "application/json,text/plain,*/*",
        },
        timeout=_HTTP_TIMEOUT_SECONDS,
    )
    try:
        response.raise_for_status()
    except Exception as exc:
        raise DataVendorUnavailable(f"gov.cn policy fetch failed: {exc}") from exc
    try:
        return response.json()
    except ValueError as exc:
        raise DataVendorUnavailable("gov.cn policy response was not valid JSON.") from exc


def _fetch_with_retry(params: dict[str, Any], fetcher: FetchJson | None = None) -> dict[str, Any]:
    fn = fetcher or _fetch_json
    last_error: Exception | None = None
    for attempt in range(_HTTP_RETRIES + 1):
        try:
            return fn(params)
        except DataVendorUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < _HTTP_RETRIES:
                time.sleep(0.5 * (attempt + 1))
    raise DataVendorUnavailable(f"gov.cn policy fetch failed: {last_error}") from last_error


def _record_from_row(row: dict[str, Any], category: GovPolicyCategory) -> dict[str, Any]:
    title = _normalise_text(row.get("title"))
    summary = _normalise_text(row.get("summary"))
    pub_date = _normalise_date(row.get("pubtimeStr"))
    return {
        "article_id": _article_id(row, category),
        "source": "gov.cn policy document library",
        "category_id": category.id,
        "category": category.name,
        "pub_date": pub_date,
        "puborg": _normalise_text(row.get("puborg") or row.get("source")),
        "pcode": _normalise_text(row.get("pcode") or row.get("wenhao")),
        "index": _normalise_text(row.get("index")),
        "childtype": _normalise_text(row.get("childtype")),
        "title": title,
        "summary": summary,
        "url": _normalise_text(row.get("url")),
        "raw_id": _normalise_text(row.get("id")),
        "raw_pubtime": row.get("pubtime"),
        "raw_ptime": row.get("ptime"),
        "raw_sha256": hashlib.sha256(
            json.dumps(row, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest(),
        "parsed_at": _utc_now(),
    }


def parse_search_response(
    payload: dict[str, Any],
    category: str | GovPolicyCategory,
) -> dict[str, Any]:
    """Parse a ``/search-gov/data`` JSON payload into policy records."""
    category_obj = _category_from_id(category)
    code = str(payload.get("code") or "")
    if code and code != "200":
        raise DataVendorUnavailable(
            f"gov.cn policy response code {code}: {_normalise_text(payload.get('msg'))}"
        )

    search = payload.get("searchVO") or {}
    list_rows = search.get("listVO")
    if list_rows is None:
        cat_map = search.get("catMap") or {}
        cat_entry = cat_map.get(category_obj.id) or {}
        list_rows = cat_entry.get("listVO") or []
        total_count = int(cat_entry.get("totalCount") or 0)
        total_page = int(cat_entry.get("totalpage") or 0)
    else:
        total_count = int(search.get("totalCount") or 0)
        total_page = int(search.get("totalpage") or 0)

    records = [
        _record_from_row(row, category_obj)
        for row in list_rows
        if isinstance(row, dict)
    ]
    params = payload.get("paramsVO") or {}
    parsed_page_size = int(params.get("n") or len(records) or _DEFAULT_PAGE_SIZE)
    return {
        "category_id": category_obj.id,
        "category": category_obj.name,
        "query_t": category_obj.query_t,
        "page": int(params.get("p") or 1),
        "page_size": parsed_page_size,
        "total_count": total_count,
        "total_pages": total_page
        or (total_count + max(parsed_page_size, 1) - 1) // max(parsed_page_size, 1),
        "records": records,
    }


def _manifest_path(cache_root: Path) -> Path:
    return cache_root / "manifest.json"


def _records_jsonl_path(cache_root: Path) -> Path:
    return cache_root / "parsed" / "policy_documents.jsonl"


def _records_csv_path(cache_root: Path) -> Path:
    return cache_root / "parsed" / "policy_documents.csv"


def _raw_page_path(cache_root: Path, category: GovPolicyCategory, page: int) -> Path:
    return cache_root / "raw" / category.id / f"page-{page:04d}.json"


def _load_manifest(cache_root: Path) -> dict[str, Any]:
    path = _manifest_path(cache_root)
    if not path.is_file():
        return {"records": {}, "categories": {}, "last_incremental_at": None}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("Ignoring invalid gov.cn policy manifest at %s", path)
        return {"records": {}, "categories": {}, "last_incremental_at": None}
    data.setdefault("records", {})
    data.setdefault("categories", {})
    data.setdefault("last_incremental_at", None)
    return data


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _load_records(cache_root: Path) -> list[dict[str, Any]]:
    path = _records_jsonl_path(cache_root)
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                records.append(json.loads(stripped))
            except ValueError:
                logger.warning("Skipping invalid gov.cn policy JSONL line in %s", path)
    return records


def _compact_record_for_csv(record: dict[str, Any]) -> dict[str, str]:
    return {
        "pub_date": str(record.get("pub_date") or ""),
        "category": str(record.get("category") or ""),
        "puborg": str(record.get("puborg") or ""),
        "pcode": str(record.get("pcode") or ""),
        "childtype": str(record.get("childtype") or ""),
        "title": str(record.get("title") or ""),
        "url": str(record.get("url") or ""),
        "summary": _normalise_text(record.get("summary")),
    }


def _write_records(cache_root: Path, records: list[dict[str, Any]]) -> None:
    parsed_dir = cache_root / "parsed"
    parsed_dir.mkdir(parents=True, exist_ok=True)
    records = sorted(
        records,
        key=lambda row: (row.get("pub_date") or "", row.get("category_id") or "", row.get("article_id") or ""),
        reverse=True,
    )

    with _records_jsonl_path(cache_root).open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    fields = ["pub_date", "category", "puborg", "pcode", "childtype", "title", "url", "summary"]
    with _records_csv_path(cache_root).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for record in records:
            writer.writerow(_compact_record_for_csv(record))


def _merge_records(
    existing_records: list[dict[str, Any]],
    new_records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_key: dict[str, dict[str, Any]] = {}
    for record in existing_records:
        key = str(record.get("url") or record.get("article_id") or "")
        if key:
            by_key[key] = record
    for record in new_records:
        key = str(record.get("url") or record.get("article_id") or "")
        if key:
            by_key[key] = record
    return list(by_key.values())


def crawl_gov_policy_documents(
    *,
    cache_dir: str | Path | None = None,
    start_date: str,
    end_date: str,
    max_pages_per_category: int = _DEFAULT_MAX_PAGES,
    page_size: int = _DEFAULT_PAGE_SIZE,
    categories: Iterable[str] | None = None,
    fetcher: FetchJson | None = None,
    q: str = "",
    on_log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Fetch gov.cn policy search pages and refresh parsed local records."""
    _parse_iso_date(start_date, "start_date")
    _parse_iso_date(end_date, "end_date")
    if max_pages_per_category <= 0:
        raise DataVendorUnavailable("max_pages_per_category must be > 0.")
    if page_size <= 0:
        raise DataVendorUnavailable("page_size must be > 0.")

    cache_root = gov_policy_cache_dir(cache_dir)
    cache_root.mkdir(parents=True, exist_ok=True)
    manifest = _load_manifest(cache_root)
    existing_records = _load_records(cache_root)
    new_records: list[dict[str, Any]] = []
    fetched_pages = 0
    started_at = _utc_now()

    selected_categories = [
        _category_from_id(category_id)
        for category_id in (categories or [category.id for category in GOV_POLICY_CATEGORIES])
    ]

    for category in selected_categories:
        if on_log:
            on_log(f"gov.cn policy crawl category {category.name}")

        category_records = 0
        total_count = 0
        total_pages = 1
        for page in range(1, max_pages_per_category + 1):
            params = _query_params(
                category,
                start_date=start_date,
                end_date=end_date,
                page=page,
                page_size=page_size,
                q=q,
            )
            payload = _fetch_with_retry(params, fetcher)
            _write_json(_raw_page_path(cache_root, category, page), payload)
            fetched_pages += 1
            parsed = parse_search_response(payload, category)
            total_count = int(parsed.get("total_count") or 0)
            total_pages = max(int(parsed.get("total_pages") or 1), 1)
            records = list(parsed.get("records") or [])
            new_records.extend(records)
            category_records += len(records)
            if page >= total_pages or not records:
                break

        manifest["categories"][category.id] = {
            "name": category.name,
            "query_t": category.query_t,
            "total_count": total_count,
            "total_pages": total_pages,
            "last_seen_records": category_records,
            "last_crawled_at": _utc_now(),
        }

    merged_records = _merge_records(existing_records, new_records)
    _write_records(cache_root, merged_records)
    finished_at = _utc_now()
    manifest["last_incremental_at"] = finished_at
    manifest["last_window"] = {
        "start_date": start_date,
        "end_date": end_date,
        "q": q,
        "categories": [category.id for category in selected_categories],
    }
    manifest["last_run"] = {
        "started_at": started_at,
        "finished_at": finished_at,
        "fetched_pages": fetched_pages,
        "parsed_records": len(merged_records),
        "new_records": len(new_records),
    }
    _write_json(_manifest_path(cache_root), manifest)
    return manifest["last_run"]


def ensure_gov_policy_documents_updated(
    *,
    cache_dir: str | Path | None = None,
    start_date: str,
    end_date: str,
    max_pages_per_category: int = _DEFAULT_MAX_PAGES,
    page_size: int = _DEFAULT_PAGE_SIZE,
    stale_after_hours: float = 6.0,
    categories: Iterable[str] | None = None,
    fetcher: FetchJson | None = None,
    q: str = "",
) -> dict[str, Any] | None:
    """Refresh local gov.cn policy cache unless the same window is fresh."""
    cache_root = gov_policy_cache_dir(cache_dir)
    manifest = _load_manifest(cache_root)
    selected_ids = list(categories or [category.id for category in GOV_POLICY_CATEGORIES])
    last_window = manifest.get("last_window") or {}
    same_window = (
        last_window.get("start_date") == start_date
        and last_window.get("end_date") == end_date
        and last_window.get("q", "") == q
        and list(last_window.get("categories") or []) == selected_ids
    )
    last_at = manifest.get("last_incremental_at")
    if same_window and last_at:
        try:
            last_dt = datetime.fromisoformat(str(last_at))
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            age = datetime.now(timezone.utc) - last_dt
            if age.total_seconds() < stale_after_hours * 3600:
                return None
        except ValueError:
            pass
    return crawl_gov_policy_documents(
        cache_dir=cache_root,
        start_date=start_date,
        end_date=end_date,
        max_pages_per_category=max_pages_per_category,
        page_size=page_size,
        categories=selected_ids,
        fetcher=fetcher,
        q=q,
    )


def load_gov_policy_records(cache_dir: str | Path | None = None) -> list[dict[str, Any]]:
    return _load_records(gov_policy_cache_dir(cache_dir))


def _records_in_window(
    records: Iterable[dict[str, Any]],
    start_date: str,
    end_date: str,
) -> list[dict[str, Any]]:
    start_dt = _parse_iso_date(start_date, "start_date")
    end_dt = _parse_iso_date(end_date, "end_date")
    filtered: list[dict[str, Any]] = []
    for record in records:
        pub_date = str(record.get("pub_date") or "")
        if not pub_date:
            continue
        try:
            pub_dt = datetime.strptime(pub_date, _DATE_FMT)
        except ValueError:
            continue
        if start_dt <= pub_dt <= end_dt:
            filtered.append(record)
    return sorted(
        filtered,
        key=lambda row: (row.get("pub_date") or "", row.get("category_id") or "", row.get("article_id") or ""),
        reverse=True,
    )


def _filter_keywords(
    records: list[dict[str, Any]],
    keywords: Iterable[str] | str | None,
) -> list[dict[str, Any]]:
    terms = _normalise_terms(keywords)
    if not terms:
        return records
    filtered: list[dict[str, Any]] = []
    for record in records:
        haystack = " ".join(
            str(record.get(field) or "")
            for field in ("title", "summary", "puborg", "pcode", "childtype", "category")
        )
        if any(term in haystack for term in terms):
            filtered.append(record)
    return filtered


def _records_to_markdown_csv(
    records: list[dict[str, Any]],
    *,
    title: str,
    subtitle: str,
    empty_note: str,
) -> str:
    buf = io.StringIO()
    buf.write(f"# {title}\n")
    buf.write(f"# {subtitle}\n")
    if not records:
        buf.write(f"{empty_note}\n")
        return buf.getvalue()

    fields = ["pub_date", "category", "puborg", "pcode", "childtype", "title", "url", "summary"]
    writer = csv.DictWriter(buf, fieldnames=fields)
    writer.writeheader()
    for record in records:
        writer.writerow(_compact_record_for_csv(record))
    return buf.getvalue()


def get_gov_policy_documents(
    curr_date: str,
    look_back_days: int = 7,
    *,
    cache_dir: str | Path | None = None,
    fetcher: FetchJson | None = None,
    keywords: Iterable[str] | str | None = None,
    max_pages_per_category: int = _DEFAULT_MAX_PAGES,
) -> str:
    """Return gov.cn policy documents for a date window."""
    start_date, end_date = _date_window(curr_date, int(look_back_days or 0))
    cache_root = gov_policy_cache_dir(cache_dir)
    refresh_note = "local cache"
    try:
        run = ensure_gov_policy_documents_updated(
            cache_dir=cache_root,
            start_date=start_date,
            end_date=end_date,
            max_pages_per_category=max_pages_per_category,
            fetcher=fetcher,
        )
        if run:
            refresh_note = (
                "gov.cn policy incremental refresh "
                f"(pages={run['fetched_pages']}, new_records={run['new_records']}, "
                f"parsed={run['parsed_records']})"
            )
    except DataVendorUnavailable as exc:
        records = load_gov_policy_records(cache_root)
        if not records:
            raise DataVendorUnavailable(
                f"gov.cn policy cache is empty and refresh failed: {exc}"
            ) from exc
        refresh_note = f"local cache; refresh skipped after gov.cn policy error: {exc}"

    records = _records_in_window(load_gov_policy_records(cache_root), start_date, end_date)
    records = _filter_keywords(records, keywords)
    category_names = " / ".join(category.name for category in GOV_POLICY_CATEGORIES)
    suffix = ""
    terms = _normalise_terms(keywords)
    if terms:
        suffix = f"; keyword filter: {', '.join(terms)}"
    return _records_to_markdown_csv(
        records,
        title=f"产业政策 / Gov.cn Policy Documents ({start_date} → {end_date})",
        subtitle=(
            f"Source: State Council policy document library ({refresh_note}). "
            f"Categories: {category_names}{suffix}."
        ),
        empty_note=f"No gov.cn policy documents recorded between {start_date} and {end_date}.",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mosaic.dataflows.gov_policy")
    parser.add_argument("--cache-dir", default=None)
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--max-pages-per-category", type=int, default=_DEFAULT_MAX_PAGES)
    parser.add_argument("--page-size", type=int, default=_DEFAULT_PAGE_SIZE)
    parser.add_argument(
        "--category",
        action="append",
        dest="categories",
        choices=sorted(_CATEGORY_BY_ID),
        help="limit crawl to a category id; may be repeated",
    )
    args = parser.parse_args(argv)

    run = crawl_gov_policy_documents(
        cache_dir=args.cache_dir,
        start_date=args.start_date,
        end_date=args.end_date,
        max_pages_per_category=args.max_pages_per_category,
        page_size=args.page_size,
        categories=args.categories,
        on_log=lambda msg: print(msg, flush=True),
    )
    print(json.dumps(run, ensure_ascii=False, indent=2))
    return 0


__all__ = [
    "GOV_POLICY_CATEGORIES",
    "GOV_POLICY_ENTRY_URL",
    "GOV_POLICY_LIBRARY_URL",
    "GOV_POLICY_SEARCH_API",
    "GovPolicyCategory",
    "crawl_gov_policy_documents",
    "ensure_gov_policy_documents_updated",
    "get_gov_policy_documents",
    "gov_policy_cache_dir",
    "load_gov_policy_records",
    "parse_search_response",
]


if __name__ == "__main__":
    raise SystemExit(main())
