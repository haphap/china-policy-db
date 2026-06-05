from __future__ import annotations

import json

import pytest

from china_policy_db import pboc_ops
from china_policy_db.exceptions import DataVendorUnavailable


LIST_HTML = """
<html><body>
<input id="article_paging_list_hidden" moduleid="17081" totalpage="2" />
<table><tr><td>
<font class="newslist_style"><a href="/zhengcehuobisi/125207/125213/125431/125475/2026060308521286488/index.html"
   title="公开市场业务交易公告 [2026]第105号" istitle="true">公开市场业务交易公告 [2026]第105号</a>
</font>
<span class="hui12">2026-06-03</span>
</td></tr></table>
<a tagname="/zhengcehuobisi/125207/125213/125431/125475/17081-2.html">下一页</a>
</body></html>
"""

ARTICLE_URL = (
    "https://www.pbc.gov.cn/zhengcehuobisi/125207/125213/125431/"
    "125475/2026060308521286488/index.html"
)

ARTICLE_HTML = """
<html><head>
<meta name="ArticleTitle" content="公开市场业务交易公告 [2026]第105号" />
<meta name="PubDate" content="2026-06-03" />
<meta name="Keywords" content="操作,利率,中国人民银行,逆回购,市场" />
<meta name="Description" content="2026年6月3日7天期逆回购操作量为零。" />
</head><body>
<div id="zoom" class="zoom1">
  <p>根据公开市场业务一级交易商的需求，2026年6月3日7天期逆回购操作量为零。具体情况如下：</p>
  <table>
    <tr><td>期限</td><td>投标量</td><td>中标量</td><td>中标利率</td></tr>
    <tr><td>7天</td><td>0亿元</td><td>0亿元</td><td>1.50%</td></tr>
  </table>
  <p>中国人民银行公开市场业务操作室</p>
</div>
</body></html>
"""


def test_parse_list_page_extracts_articles_and_paging():
    parsed = pboc_ops.parse_list_page(LIST_HTML, "transaction_notice")

    assert parsed["module_id"] == "17081"
    assert parsed["total_pages"] == 2
    assert parsed["page_urls"][2].endswith("/125475/17081-2.html")
    assert parsed["articles"] == [
        {
            "article_id": "2026060308521286488",
            "category_id": "transaction_notice",
            "category": "公开市场业务交易公告",
            "title": "公开市场业务交易公告 [2026]第105号",
            "pub_date": "2026-06-03",
            "url": ARTICLE_URL,
        }
    ]


def test_parse_article_page_extracts_structured_fields():
    record = pboc_ops.parse_article_page(
        ARTICLE_HTML,
        ARTICLE_URL,
        "transaction_notice",
    )

    assert record["article_id"] == "2026060308521286488"
    assert record["pub_date"] == "2026-06-03"
    assert record["title"] == "公开市场业务交易公告 [2026]第105号"
    assert record["operation_type"] == "reverse_repo"
    assert record["operation_no"] == "105"
    assert record["terms"] == ["7天"]
    assert record["amounts_cny_100m"] == [0.0]
    assert record["rates"] == ["1.50%"]
    assert record["tables"] == [
        [{"期限": "7天", "投标量": "0亿元", "中标量": "0亿元", "中标利率": "1.50%"}]
    ]
    assert "逆回购操作量为零" in record["body_text"]
    assert "<table>" not in record["summary"]


def test_parse_article_page_handles_central_bank_bill_wording_without_month_noise():
    html = ARTICLE_HTML.replace(
        "公开市场业务交易公告 [2026]第105号",
        "公开市场业务交易公告 [2026]第100号",
    ).replace(
        "2026年6月3日7天期逆回购操作量为零。",
        "人民银行于本周三（5月27日）发行3个月期央行票据150亿元，利率1.13%。",
    ).replace("7天", "3个月").replace("0亿元", "150亿元").replace("1.50%", "1.13%")

    record = pboc_ops.parse_article_page(
        html,
        ARTICLE_URL,
        "transaction_notice",
    )

    assert record["operation_type"] == "central_bank_bill"
    assert "3个月" in record["terms"]
    assert "5月" not in record["terms"]


def test_crawl_writes_raw_parsed_manifest_and_tracks_unchanged(tmp_path):
    category = pboc_ops.PBOC_OMO_CATEGORIES[2]

    def fetcher(url: str) -> str:
        if url == category.url:
            return LIST_HTML
        if url == ARTICLE_URL:
            return ARTICLE_HTML
        raise AssertionError(url)

    first = pboc_ops.crawl_pboc_open_market(
        cache_dir=tmp_path,
        categories=["transaction_notice"],
        max_pages_per_category=1,
        fetcher=fetcher,
    )
    second = pboc_ops.crawl_pboc_open_market(
        cache_dir=tmp_path,
        categories=["transaction_notice"],
        max_pages_per_category=1,
        fetcher=fetcher,
    )

    assert first["changed_articles"] == 1
    assert second["changed_articles"] == 0
    assert second["unchanged_articles"] == 1
    assert (tmp_path / "raw" / "list" / "transaction_notice" / "page-0001.html").is_file()
    assert (tmp_path / "raw" / "articles" / "2026060308521286488.html").is_file()
    assert (tmp_path / "parsed" / "articles.jsonl").is_file()
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["articles"]["2026060308521286488"]["raw_sha256"]


def test_crawl_uses_discovered_paging_urls_for_short_module_keys(tmp_path):
    category = pboc_ops.PBOC_OMO_CATEGORIES[3]
    first_page = LIST_HTML.replace(
        "/125475/17081-2.html",
        "/5492845/b0da893b-2.html",
    ).replace("/125475/", "/5492845/").replace(
        'moduleid="17081"',
        'moduleid="b0da893b8199419588137040eca247e2"',
    )
    page_2 = """
    <html><body>
    <input id="article_paging_list_hidden" moduleid="b0da893b8199419588137040eca247e2" totalpage="2" />
    </body></html>
    """
    article_url = ARTICLE_URL.replace("/125475/", "/5492845/")
    requested = []

    def fetcher(url: str) -> str:
        requested.append(url)
        if url == category.url:
            return first_page
        if url.endswith("/5492845/b0da893b-2.html"):
            return page_2
        if url == article_url:
            return ARTICLE_HTML
        raise AssertionError(url)

    pboc_ops.crawl_pboc_open_market(
        cache_dir=tmp_path,
        categories=["outright_reverse_repo"],
        max_pages_per_category=2,
        fetcher=fetcher,
    )

    assert any(url.endswith("/5492845/b0da893b-2.html") for url in requested)
    assert not any("b0da893b8199419588137040eca247e2-2.html" in url for url in requested)


def test_get_pboc_ops_uses_recent_local_cache_without_network(tmp_path, monkeypatch):
    def fetcher(url: str) -> str:
        if url == pboc_ops.PBOC_OMO_CATEGORIES[2].url:
            return LIST_HTML
        if url == ARTICLE_URL:
            return ARTICLE_HTML
        raise AssertionError(url)

    pboc_ops.crawl_pboc_open_market(
        cache_dir=tmp_path,
        categories=["transaction_notice"],
        max_pages_per_category=1,
        fetcher=fetcher,
    )
    monkeypatch.setenv("MOSAIC_PBOC_OPS_DISABLE_NETWORK", "1")

    out = pboc_ops.get_pboc_ops("2026-06-04", 2, cache_dir=tmp_path)

    assert "PBOC Open Market Announcements" in out
    assert "公开市场业务交易公告 [2026]第105号" in out
    assert "reverse_repo" in out
    assert "1.50%" in out


def test_get_pboc_ops_empty_cache_reports_refresh_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("MOSAIC_PBOC_OPS_DISABLE_NETWORK", "1")

    with pytest.raises(DataVendorUnavailable, match="cache is empty"):
        pboc_ops.get_pboc_ops("2026-06-04", 2, cache_dir=tmp_path)
