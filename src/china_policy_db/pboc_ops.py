"""PBOC open-market operation crawler and parser.

The PBOC no longer maps cleanly to the old Tushare ``cb_op`` endpoint, so this
module keeps a local, parsed mirror of the public open-market announcement
pages under:

    https://www.pbc.gov.cn/zhengcehuobisi/125207/125213/125431/index.html

Raw HTML is kept for audit/reparse, while parsed records are stored as JSONL
and a compact CSV that ``get_pboc_ops`` can feed to macro agents.
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
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urljoin, urlparse

from .exceptions import DataVendorUnavailable

logger = logging.getLogger(__name__)

PBOC_BASE_URL = "https://www.pbc.gov.cn"
PBOC_OMO_INDEX_URL = (
    "https://www.pbc.gov.cn/zhengcehuobisi/125207/125213/125431/index.html"
)


@dataclass(frozen=True)
class PbocCategory:
    id: str
    name: str
    url_path: str

    @property
    def url(self) -> str:
        return urljoin(PBOC_BASE_URL, self.url_path)


PBOC_OMO_CATEGORIES: tuple[PbocCategory, ...] = (
    PbocCategory("summary", "公开市场业务综述", "/zhengcehuobisi/125207/125213/125431/125463/index.html"),
    PbocCategory("business_notice", "公开市场业务公告", "/zhengcehuobisi/125207/125213/125431/125469/index.html"),
    PbocCategory("transaction_notice", "公开市场业务交易公告", "/zhengcehuobisi/125207/125213/125431/125475/index.html"),
    PbocCategory("outright_reverse_repo", "公开市场买断式逆回购业务公告", "/zhengcehuobisi/125207/125213/125431/5492845/index.html"),
    PbocCategory("treasury_trade", "公开市场国债买卖业务公告", "/zhengcehuobisi/125207/125213/125431/5442785/index.html"),
    PbocCategory("central_bank_bill", "中央银行票据业务公告", "/zhengcehuobisi/125207/125213/125431/125472/index.html"),
    PbocCategory("cbs", "央行票据互换(CBS)业务公告", "/zhengcehuobisi/125207/125213/125431/3752466/index.html"),
    PbocCategory("sfisf", "证券、基金、保险公司互换便利（SFISF）业务公告", "/zhengcehuobisi/125207/125213/125431/5481510/index.html"),
    PbocCategory("treasury_cash", "中央国库现金管理业务公告", "/zhengcehuobisi/125207/125213/125431/125481/index.html"),
    PbocCategory("other_notice", "其他业务公告", "/zhengcehuobisi/125207/125213/125431/5442780/index.html"),
)

_CATEGORY_BY_ID = {category.id: category for category in PBOC_OMO_CATEGORIES}
_DEFAULT_INCREMENTAL_PAGES = 2
_HTTP_TIMEOUT_SECONDS = 20
_HTTP_RETRIES = 2
_USER_AGENT = (
    "MOSAIC-Agents pboc-ops crawler "
    "(https://www.pbc.gov.cn/zhengcehuobisi/125207/125213/125431/index.html)"
)

_DATE_FMT = "%Y-%m-%d"
_LIST_ARTICLE_RE = re.compile(
    r"<a\b(?=[^>]*\bistitle\s*=\s*['\"]true['\"])(?P<attrs>[^>]*)>"
    r"(?P<label>.*?)</a>(?:\s*</?[^>]+>)*\s*"
    r"<span\b[^>]*>\s*(?P<date>\d{4}-\d{2}-\d{2})\s*</span>",
    re.IGNORECASE | re.DOTALL,
)
_ATTR_RE = re.compile(
    r"(?P<name>[A-Za-z_:][-A-Za-z0-9_:.]*)\s*=\s*(?P<quote>['\"])(?P<value>.*?)(?P=quote)",
    re.DOTALL,
)
_PAGING_RE = re.compile(
    r"<input\b(?=[^>]*\barticle_paging_list_hidden\b)(?P<attrs>[^>]*)>",
    re.IGNORECASE | re.DOTALL,
)
_PAGING_URL_RE = re.compile(
    r"['\"](?P<path>/zhengcehuobisi/125207/125213/125431/[^'\"]+?-(?P<page>\d+)\.html)['\"]",
    re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")
_DATE_RE = re.compile(r"(\d{4})[-/年.]?(\d{1,2})[-/月.]?(\d{1,2})")
_OPERATION_NO_RE = re.compile(r"第\s*([0-9０-９]+)\s*号")
_AMOUNT_RE = re.compile(r"(?<![A-Za-z0-9])([0-9]+(?:\.[0-9]+)?)\s*(万?亿元|亿)")
_RATE_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*%")
_TERM_RE = re.compile(r"(?<![年月日0-9])([0-9]+(?:\.[0-9]+)?)(天|个月|月|年)期?(?![0-9日])")


FetchText = Callable[[str], str]


def pboc_ops_cache_dir(cache_dir: str | Path | None = None) -> Path:
    """Return the root directory for PBOC open-market cached files."""
    if cache_dir is not None:
        return Path(cache_dir)
    from .config import get_config  # noqa: PLC0415

    configured = get_config().get("data_cache_dir")
    if not configured:
        raise DataVendorUnavailable("data_cache_dir is not configured.")
    return Path(configured) / "pboc_ops"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalise_text(value: Any) -> str:
    text = "" if value is None else str(value)
    text = unescape(text)
    text = text.replace("\u3000", " ")
    return re.sub(r"\s+", " ", text).strip()


def _normalise_multiline(value: str) -> str:
    lines = [_normalise_text(line) for line in value.splitlines()]
    return "\n".join(line for line in lines if line)


def _strip_tags(fragment: str) -> str:
    return _normalise_text(_TAG_RE.sub(" ", fragment))


def _parse_attrs(attrs_fragment: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for match in _ATTR_RE.finditer(attrs_fragment or ""):
        attrs[match.group("name").lower()] = unescape(match.group("value"))
    return attrs


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


def _article_id_from_url(url: str) -> str:
    path_parts = [part for part in urlparse(urljoin(PBOC_BASE_URL, url)).path.split("/") if part]
    if not path_parts:
        return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    if path_parts[-1].lower() == "index.html" and len(path_parts) >= 2:
        return path_parts[-2]
    return path_parts[-1].removesuffix(".html") or hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


def _category_from_id(category_id: str | PbocCategory) -> PbocCategory:
    if isinstance(category_id, PbocCategory):
        return category_id
    try:
        return _CATEGORY_BY_ID[category_id]
    except KeyError as exc:
        raise DataVendorUnavailable(f"Unknown PBOC category: {category_id}") from exc


def _list_page_url(category: PbocCategory, page: int, module_id: str | None = None) -> str:
    if page <= 1:
        return category.url
    if not module_id:
        raise DataVendorUnavailable(
            f"Cannot build page {page} URL for {category.name}: missing module id."
        )
    base_path = category.url_path.rsplit("/", 1)[0] + "/"
    return urljoin(PBOC_BASE_URL, f"{base_path}{module_id}-{page}.html")


def _network_disabled() -> bool:
    raw = os.getenv("MOSAIC_PBOC_OPS_DISABLE_NETWORK") or os.getenv(
        "MOSAIC_PBOC_OPS_OFFLINE"
    )
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


def _fetch_text(url: str) -> str:
    if _network_disabled():
        raise DataVendorUnavailable(
            "PBOC open-market network refresh is disabled by MOSAIC_PBOC_OPS_DISABLE_NETWORK."
        )
    try:
        import requests  # noqa: PLC0415
    except ImportError as exc:
        raise DataVendorUnavailable(
            "requests is required to fetch PBOC open-market pages."
        ) from exc

    response = requests.get(
        url,
        headers={"User-Agent": _USER_AGENT},
        timeout=_HTTP_TIMEOUT_SECONDS,
    )
    try:
        response.raise_for_status()
    except Exception as exc:
        raise DataVendorUnavailable(f"PBOC fetch failed for {url}: {exc}") from exc
    encoding = response.apparent_encoding or response.encoding or "utf-8"
    if encoding.lower().replace("_", "-") in {"iso-8859-1", "ascii"}:
        encoding = "utf-8"
    response.encoding = encoding
    return response.text


def _fetch_with_retry(url: str, fetcher: FetchText | None = None) -> str:
    fn = fetcher or _fetch_text
    last_error: Exception | None = None
    for attempt in range(_HTTP_RETRIES + 1):
        try:
            return fn(url)
        except DataVendorUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < _HTTP_RETRIES:
                time.sleep(0.5 * (attempt + 1))
    raise DataVendorUnavailable(f"PBOC fetch failed for {url}: {last_error}") from last_error


def parse_list_page(html: str, category: str | PbocCategory) -> dict[str, Any]:
    """Parse a PBOC category list page into article links and paging metadata."""
    category_obj = _category_from_id(category)
    articles: list[dict[str, str]] = []
    seen: set[str] = set()
    for match in _LIST_ARTICLE_RE.finditer(html):
        attrs = _parse_attrs(match.group("attrs"))
        href = attrs.get("href", "")
        if not href:
            continue
        url = urljoin(PBOC_BASE_URL, href)
        article_id = _article_id_from_url(url)
        if article_id in seen:
            continue
        seen.add(article_id)
        title = _normalise_text(attrs.get("title")) or _strip_tags(match.group("label"))
        articles.append(
            {
                "article_id": article_id,
                "category_id": category_obj.id,
                "category": category_obj.name,
                "title": title,
                "pub_date": _normalise_date(match.group("date")),
                "url": url,
            }
        )

    module_id = ""
    total_pages = 1
    page_urls: dict[int, str] = {1: category_obj.url}
    paging_match = _PAGING_RE.search(html)
    if paging_match:
        paging_attrs = _parse_attrs(paging_match.group("attrs"))
        module_id = paging_attrs.get("moduleid", "")
        try:
            total_pages = max(int(paging_attrs.get("totalpage", "1") or "1"), 1)
        except ValueError:
            total_pages = 1
    for match in _PAGING_URL_RE.finditer(html):
        try:
            page_number = int(match.group("page"))
        except ValueError:
            continue
        if page_number >= 1:
            page_urls[page_number] = urljoin(PBOC_BASE_URL, match.group("path"))

    return {
        "category_id": category_obj.id,
        "category": category_obj.name,
        "module_id": module_id,
        "total_pages": total_pages,
        "page_urls": page_urls,
        "articles": articles,
    }


class _ArticleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self._in_zoom = False
        self._zoom_div_depth = 0
        self._text_parts: list[str] = []
        self._in_table = False
        self._current_table: list[list[str]] | None = None
        self._current_row: list[str] | None = None
        self._current_cell: list[str] | None = None
        self.tables: list[list[list[str]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attrs_dict = {name.lower(): value or "" for name, value in attrs}
        if tag == "meta":
            name = attrs_dict.get("name") or attrs_dict.get("property")
            content = attrs_dict.get("content", "")
            if name and content:
                self.meta[name] = content
            return

        if tag == "div" and attrs_dict.get("id") == "zoom":
            self._in_zoom = True
            self._zoom_div_depth = 1
            return
        if not self._in_zoom:
            return

        if tag == "div":
            self._zoom_div_depth += 1
        if tag in {"p", "br", "tr", "table"}:
            self._text_parts.append("\n")
        elif tag in {"td", "th"}:
            self._text_parts.append(" ")

        if tag == "table" and not self._in_table:
            self._in_table = True
            self._current_table = []
        elif self._in_table and tag == "tr":
            self._current_row = []
        elif self._in_table and tag in {"td", "th"}:
            self._current_cell = []

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "meta":
            self.handle_starttag(tag, attrs)
        if self._in_zoom and tag == "br":
            self._text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._in_zoom:
            return
        self._text_parts.append(data)
        if self._current_cell is not None:
            self._current_cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if not self._in_zoom:
            return

        if self._in_table and tag in {"td", "th"} and self._current_cell is not None:
            cell = _normalise_text("".join(self._current_cell))
            if self._current_row is not None:
                self._current_row.append(cell)
            self._current_cell = None
        elif self._in_table and tag == "tr" and self._current_row is not None:
            if any(cell for cell in self._current_row):
                assert self._current_table is not None
                self._current_table.append(self._current_row)
            self._current_row = None
            self._text_parts.append("\n")
        elif self._in_table and tag == "table":
            if self._current_table:
                self.tables.append(self._current_table)
            self._current_table = None
            self._in_table = False
            self._text_parts.append("\n")

        if tag in {"p", "div"}:
            self._text_parts.append("\n")
        if tag == "div":
            self._zoom_div_depth -= 1
            if self._zoom_div_depth <= 0:
                self._in_zoom = False

    @property
    def body_text(self) -> str:
        return _normalise_multiline("".join(self._text_parts))


def _table_dicts(tables: list[list[list[str]]]) -> list[list[dict[str, str]]]:
    parsed: list[list[dict[str, str]]] = []
    for table in tables:
        clean_rows = [[_normalise_text(cell) for cell in row] for row in table]
        clean_rows = [row for row in clean_rows if any(row)]
        if len(clean_rows) < 2:
            continue
        headers = [header or f"col_{idx + 1}" for idx, header in enumerate(clean_rows[0])]
        rows: list[dict[str, str]] = []
        for row in clean_rows[1:]:
            row_dict = {
                headers[idx] if idx < len(headers) else f"col_{idx + 1}": cell
                for idx, cell in enumerate(row)
            }
            if any(row_dict.values()):
                rows.append(row_dict)
        if rows:
            parsed.append(rows)
    return parsed


def _infer_operation_type(category: PbocCategory, title: str, body_text: str) -> str:
    text = f"{category.name} {title} {body_text}"
    rules: list[tuple[str, tuple[str, ...]]] = [
        ("sfisf", ("SFISF", "互换便利", "证券、基金、保险公司")),
        ("cbs", ("CBS", "央行票据互换", "央行票据互换")),
        ("outright_reverse_repo", ("买断式逆回购",)),
        ("treasury_trade", ("国债买卖", "国债买入", "国债卖出")),
        ("central_bank_bill", ("中央银行票据", "央行票据", "央票")),
        ("treasury_cash", ("国库现金管理", "中央国库现金管理")),
        ("slo", ("短期流动性调节工具", "SLO")),
        ("mlf", ("中期借贷便利", "MLF")),
        ("reverse_repo", ("逆回购",)),
    ]
    upper_text = text.upper()
    for op_type, keywords in rules:
        for keyword in keywords:
            haystack = upper_text if keyword.isascii() else text
            needle = keyword.upper() if keyword.isascii() else keyword
            if needle in haystack:
                return op_type
    if category.id in {"summary", "business_notice", "other_notice"}:
        return "open_market_notice"
    return category.id


def _extract_operation_no(title: str) -> str:
    match = _OPERATION_NO_RE.search(title)
    if not match:
        return ""
    return match.group(1).translate(str.maketrans("０１２３４５６７８９", "0123456789"))


def _unique_preserve_order(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _extract_amounts_cny_100m(text: str) -> list[float]:
    amounts: list[float] = []
    seen: set[float] = set()
    for match in _AMOUNT_RE.finditer(text):
        value = float(match.group(1))
        unit = match.group(2)
        if unit.startswith("万"):
            value *= 10000.0
        if value not in seen:
            seen.add(value)
            amounts.append(value)
    return amounts


def _extract_terms(text: str) -> list[str]:
    terms: list[str] = []
    for number, unit in _TERM_RE.findall(text):
        numeric = float(number)
        if unit == "年" and numeric > 50:
            continue
        if unit in {"月", "个月"} and numeric > 120:
            continue
        if unit == "天" and numeric > 3650:
            continue
        rendered = str(int(numeric)) if numeric.is_integer() else str(numeric)
        terms.append(f"{rendered}{unit}")
    return _unique_preserve_order(terms)


def _extract_rates(text: str) -> list[str]:
    return _unique_preserve_order(f"{rate}%" for rate in _RATE_RE.findall(text))


def _summarise(body_text: str, description: str, max_chars: int = 360) -> str:
    summary = _normalise_text(description) or _normalise_text(body_text)
    if len(summary) <= max_chars:
        return summary
    return f"{summary[:max_chars].rstrip()}..."


def parse_article_page(
    html: str,
    url: str,
    category: str | PbocCategory,
    *,
    list_title: str = "",
    list_pub_date: str = "",
) -> dict[str, Any]:
    """Parse a PBOC article page into a durable announcement record."""
    category_obj = _category_from_id(category)
    parser = _ArticleParser()
    parser.feed(html)

    meta = parser.meta
    title = (
        _normalise_text(meta.get("ArticleTitle"))
        or _normalise_text(meta.get("eprotalCurrentArticleTitle"))
        or _normalise_text(meta.get("title"))
        or _normalise_text(list_title)
    )
    pub_date = (
        _normalise_date(meta.get("PubDate"))
        or _normalise_date(meta.get("publishdate"))
        or _normalise_date(meta.get("createDate"))
        or _normalise_date(list_pub_date)
    )
    body_text = parser.body_text or _strip_tags(html)
    table_rows = _table_dicts(parser.tables)
    full_text = " ".join(
        [
            title,
            body_text,
            json.dumps(table_rows, ensure_ascii=False, separators=(",", ":")),
        ]
    )

    return {
        "article_id": _article_id_from_url(url),
        "category_id": category_obj.id,
        "category": category_obj.name,
        "pub_date": pub_date,
        "title": title,
        "operation_type": _infer_operation_type(category_obj, title, body_text),
        "operation_no": _extract_operation_no(title),
        "terms": _extract_terms(full_text),
        "amounts_cny_100m": _extract_amounts_cny_100m(full_text),
        "rates": _extract_rates(full_text),
        "url": urljoin(PBOC_BASE_URL, url),
        "keywords": _normalise_text(meta.get("Keywords")),
        "description": _normalise_text(meta.get("Description")),
        "summary": _summarise(body_text, _normalise_text(meta.get("Description"))),
        "body_text": body_text,
        "tables": table_rows,
        "raw_sha256": hashlib.sha256(html.encode("utf-8")).hexdigest(),
        "parsed_at": _utc_now(),
    }


def _manifest_path(cache_root: Path) -> Path:
    return cache_root / "manifest.json"


def _articles_jsonl_path(cache_root: Path) -> Path:
    return cache_root / "parsed" / "articles.jsonl"


def _articles_csv_path(cache_root: Path) -> Path:
    return cache_root / "parsed" / "articles.csv"


def _raw_list_path(cache_root: Path, category: PbocCategory, page: int) -> Path:
    return cache_root / "raw" / "list" / category.id / f"page-{page:04d}.html"


def _raw_article_path(cache_root: Path, article_id: str) -> Path:
    return cache_root / "raw" / "articles" / f"{article_id}.html"


def _load_manifest(cache_root: Path) -> dict[str, Any]:
    path = _manifest_path(cache_root)
    if not path.is_file():
        return {"articles": {}, "categories": {}, "last_full_at": None, "last_incremental_at": None}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("Ignoring invalid PBOC manifest at %s", path)
        return {"articles": {}, "categories": {}, "last_full_at": None, "last_incremental_at": None}
    data.setdefault("articles", {})
    data.setdefault("categories", {})
    data.setdefault("last_full_at", None)
    data.setdefault("last_incremental_at", None)
    return data


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _load_articles(cache_root: Path) -> list[dict[str, Any]]:
    path = _articles_jsonl_path(cache_root)
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
                logger.warning("Skipping invalid PBOC JSONL line in %s", path)
    return records


def _write_articles(cache_root: Path, records: list[dict[str, Any]]) -> None:
    parsed_dir = cache_root / "parsed"
    parsed_dir.mkdir(parents=True, exist_ok=True)
    records = sorted(records, key=lambda row: (row.get("pub_date") or "", row.get("article_id") or ""), reverse=True)

    jsonl_path = _articles_jsonl_path(cache_root)
    with jsonl_path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    csv_path = _articles_csv_path(cache_root)
    fields = [
        "pub_date",
        "category",
        "operation_type",
        "operation_no",
        "title",
        "terms",
        "amounts_cny_100m",
        "rates",
        "url",
        "summary",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for record in records:
            writer.writerow(_compact_record_for_csv(record))


def _compact_record_for_csv(record: dict[str, Any]) -> dict[str, str]:
    return {
        "pub_date": str(record.get("pub_date") or ""),
        "category": str(record.get("category") or ""),
        "operation_type": str(record.get("operation_type") or ""),
        "operation_no": str(record.get("operation_no") or ""),
        "title": str(record.get("title") or ""),
        "terms": "|".join(str(item) for item in record.get("terms") or []),
        "amounts_cny_100m": "|".join(
            f"{float(item):g}" for item in record.get("amounts_cny_100m") or []
        ),
        "rates": "|".join(str(item) for item in record.get("rates") or []),
        "url": str(record.get("url") or ""),
        "summary": _normalise_text(record.get("summary")),
    }


def _merge_articles(
    existing_records: list[dict[str, Any]],
    new_records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_id = {str(record.get("article_id")): record for record in existing_records if record.get("article_id")}
    for record in new_records:
        article_id = str(record.get("article_id") or "")
        if article_id:
            by_id[article_id] = record
    return list(by_id.values())


def crawl_pboc_open_market(
    *,
    cache_dir: str | Path | None = None,
    full: bool = False,
    max_pages_per_category: int | None = None,
    categories: Iterable[str] | None = None,
    fetcher: FetchText | None = None,
    force: bool = False,
    on_log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Fetch list/article pages, update raw cache, and refresh parsed outputs.

    ``full=True`` walks every page in every configured category. Incremental
    runs default to the latest two list pages per category and update records
    whose article HTML changed by checksum.
    """
    cache_root = pboc_ops_cache_dir(cache_dir)
    cache_root.mkdir(parents=True, exist_ok=True)
    manifest = _load_manifest(cache_root)
    existing_records = _load_articles(cache_root)
    new_records: list[dict[str, Any]] = []
    changed = 0
    unchanged = 0
    fetched_articles = 0
    fetched_lists = 0
    reused_raw_articles = 0
    started_at = _utc_now()

    selected_categories = [
        _category_from_id(category_id)
        for category_id in (categories or [category.id for category in PBOC_OMO_CATEGORIES])
    ]

    for category in selected_categories:
        if on_log:
            on_log(f"PBOC crawl category {category.name}")

        first_html = _fetch_with_retry(category.url, fetcher)
        _raw_list_path(cache_root, category, 1).parent.mkdir(parents=True, exist_ok=True)
        _raw_list_path(cache_root, category, 1).write_text(first_html, encoding="utf-8")
        fetched_lists += 1
        first_page = parse_list_page(first_html, category)
        total_pages = int(first_page.get("total_pages") or 1)
        module_id = str(first_page.get("module_id") or "")
        if full:
            page_limit = total_pages
        else:
            page_limit = min(total_pages, max_pages_per_category or _DEFAULT_INCREMENTAL_PAGES)

        list_articles = list(first_page["articles"])
        page_urls = dict(first_page.get("page_urls") or {})
        for page in range(2, page_limit + 1):
            page_url = str(page_urls.get(page) or _list_page_url(category, page, module_id))
            page_html = _fetch_with_retry(page_url, fetcher)
            _raw_list_path(cache_root, category, page).write_text(page_html, encoding="utf-8")
            fetched_lists += 1
            parsed_page = parse_list_page(page_html, category)
            page_urls.update(parsed_page.get("page_urls") or {})
            list_articles.extend(parsed_page["articles"])

        manifest["categories"][category.id] = {
            "name": category.name,
            "url": category.url,
            "module_id": module_id,
            "total_pages": total_pages,
            "last_seen_articles": len(list_articles),
            "last_crawled_at": _utc_now(),
        }

        for item in list_articles:
            article_id = item["article_id"]
            url = item["url"]
            article_path = _raw_article_path(cache_root, article_id)
            previous_checksum = manifest["articles"].get(article_id, {}).get("raw_sha256")
            if article_path.is_file() and not previous_checksum and not force:
                article_html = article_path.read_text(encoding="utf-8")
                reused_raw_articles += 1
            else:
                article_html = _fetch_with_retry(url, fetcher)
                fetched_articles += 1
            checksum = hashlib.sha256(article_html.encode("utf-8")).hexdigest()
            if force or checksum != previous_checksum:
                changed += 1
                article_path.parent.mkdir(parents=True, exist_ok=True)
                article_path.write_text(article_html, encoding="utf-8")
                record = parse_article_page(
                    article_html,
                    url,
                    category,
                    list_title=item.get("title", ""),
                    list_pub_date=item.get("pub_date", ""),
                )
                new_records.append(record)
                manifest["articles"][article_id] = {
                    "url": url,
                    "category_id": category.id,
                    "title": record.get("title"),
                    "pub_date": record.get("pub_date"),
                    "raw_sha256": checksum,
                    "last_seen_at": _utc_now(),
                    "last_changed_at": _utc_now(),
                }
            else:
                unchanged += 1
                manifest["articles"][article_id]["last_seen_at"] = _utc_now()

    merged_records = _merge_articles(existing_records, new_records)
    _write_articles(cache_root, merged_records)
    finished_at = _utc_now()
    if full:
        manifest["last_full_at"] = finished_at
    else:
        manifest["last_incremental_at"] = finished_at
    manifest["last_run"] = {
        "started_at": started_at,
        "finished_at": finished_at,
        "full": full,
        "fetched_list_pages": fetched_lists,
        "fetched_articles": fetched_articles,
        "reused_raw_articles": reused_raw_articles,
        "changed_articles": changed,
        "unchanged_articles": unchanged,
        "parsed_records": len(merged_records),
    }
    _write_json(_manifest_path(cache_root), manifest)
    return manifest["last_run"]


def ensure_pboc_open_market_updated(
    *,
    cache_dir: str | Path | None = None,
    fetcher: FetchText | None = None,
    max_pages_per_category: int = _DEFAULT_INCREMENTAL_PAGES,
    stale_after_hours: float = 6.0,
) -> dict[str, Any] | None:
    """Incrementally refresh the local mirror unless it was refreshed recently."""
    cache_root = pboc_ops_cache_dir(cache_dir)
    manifest = _load_manifest(cache_root)
    last_at = manifest.get("last_incremental_at") or manifest.get("last_full_at")
    if last_at:
        try:
            last_dt = datetime.fromisoformat(str(last_at))
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            age = datetime.now(timezone.utc) - last_dt
            if age.total_seconds() < stale_after_hours * 3600:
                return None
        except ValueError:
            pass
    return crawl_pboc_open_market(
        cache_dir=cache_root,
        full=False,
        max_pages_per_category=max_pages_per_category,
        fetcher=fetcher,
    )


def load_pboc_open_market_records(cache_dir: str | Path | None = None) -> list[dict[str, Any]]:
    return _load_articles(pboc_ops_cache_dir(cache_dir))


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
    return sorted(filtered, key=lambda row: (row.get("pub_date") or "", row.get("article_id") or ""), reverse=True)


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

    fields = [
        "pub_date",
        "category",
        "operation_type",
        "operation_no",
        "title",
        "terms",
        "amounts_cny_100m",
        "rates",
        "url",
        "summary",
    ]
    writer = csv.DictWriter(buf, fieldnames=fields)
    writer.writeheader()
    for record in records:
        writer.writerow(_compact_record_for_csv(record))
    return buf.getvalue()


def get_pboc_ops(
    curr_date: str,
    look_back_days: int = 7,
    *,
    cache_dir: str | Path | None = None,
    fetcher: FetchText | None = None,
) -> str:
    """Return parsed PBOC open-market announcements for a date window."""
    start_date, end_date = _date_window(curr_date, int(look_back_days or 0))
    cache_root = pboc_ops_cache_dir(cache_dir)
    refresh_note = "local cache"
    try:
        run = ensure_pboc_open_market_updated(cache_dir=cache_root, fetcher=fetcher)
        if run:
            refresh_note = (
                "PBOC website incremental refresh "
                f"(list_pages={run['fetched_list_pages']}, articles={run['fetched_articles']}, "
                f"changed={run['changed_articles']}, unchanged={run['unchanged_articles']})"
            )
    except DataVendorUnavailable as exc:
        records = load_pboc_open_market_records(cache_root)
        if not records:
            raise DataVendorUnavailable(
                f"PBOC open-market cache is empty and refresh failed: {exc}"
            ) from exc
        refresh_note = f"local cache; refresh skipped after PBOC website error: {exc}"

    records = _records_in_window(load_pboc_open_market_records(cache_root), start_date, end_date)
    category_names = " / ".join(category.name for category in PBOC_OMO_CATEGORIES)
    return _records_to_markdown_csv(
        records,
        title=f"PBOC Open Market Announcements ({start_date} → {end_date})",
        subtitle=f"Source: PBOC website mirror ({refresh_note}). Categories: {category_names}.",
        empty_note=f"No PBOC open-market announcements recorded between {start_date} and {end_date}.",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mosaic.dataflows.pboc_ops")
    parser.add_argument("--cache-dir", default=None)
    parser.add_argument("--full", action="store_true", help="crawl every list page")
    parser.add_argument(
        "--max-pages-per-category",
        type=int,
        default=_DEFAULT_INCREMENTAL_PAGES,
        help="incremental page count per category",
    )
    parser.add_argument(
        "--category",
        action="append",
        dest="categories",
        choices=sorted(_CATEGORY_BY_ID),
        help="limit crawl to a category id; may be repeated",
    )
    args = parser.parse_args(argv)

    run = crawl_pboc_open_market(
        cache_dir=args.cache_dir,
        full=args.full,
        max_pages_per_category=args.max_pages_per_category,
        categories=args.categories,
        on_log=lambda msg: print(msg, flush=True),
    )
    print(json.dumps(run, ensure_ascii=False, indent=2))
    return 0


__all__ = [
    "PBOC_BASE_URL",
    "PBOC_OMO_CATEGORIES",
    "PBOC_OMO_INDEX_URL",
    "PbocCategory",
    "crawl_pboc_open_market",
    "ensure_pboc_open_market_updated",
    "get_pboc_ops",
    "load_pboc_open_market_records",
    "parse_article_page",
    "parse_list_page",
    "pboc_ops_cache_dir",
]


if __name__ == "__main__":
    raise SystemExit(main())
