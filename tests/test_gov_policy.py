from __future__ import annotations

import json

from china_policy_db import gov_policy


def _payload(rows, *, page: int = 1, page_size: int = 50, total_count: int | None = None):
    count = len(rows) if total_count is None else total_count
    total_pages = max((count + page_size - 1) // page_size, 1)
    return {
        "code": 200,
        "paramsVO": {"p": page, "n": page_size},
        "searchVO": {
            "listVO": rows,
            "totalCount": count,
            "totalpage": total_pages,
        },
    }


ROW_POLICY = {
    "id": "26164818",
    "pcode": "国发〔2026〕14号",
    "title": "国务院关于印发《加快农业农村现代化“十五五”规划》的通知",
    "pubtimeStr": "2026.06.02",
    "summary": "国务院关于印发《加快农业农村现代化 “十五五”规划》的通知<br/>请认真贯彻执行。",
    "url": "https://www.gov.cn/zhengce/zhengceku/202606/content_7070902.htm",
    "childtype": "农业、林业、水利\\农业、畜牧业、渔业",
    "puborg": "国务院",
}

ROW_ENERGY = {
    "id": "26164426",
    "pcode": "发改能源〔2026〕622号",
    "title": "国家发展改革委等部门关于印发《非化石能源电力消费核算指南（试行）》的通知",
    "pubtimeStr": "2026.06.02",
    "summary": "非化石能源电力消费核算指南发布。",
    "url": "https://www.gov.cn/zhengce/zhengceku/202606/content_7070873.htm",
    "childtype": "国土资源、能源\\其他",
    "puborg": "国家发展改革委 国家能源局",
}


def test_parse_search_response_extracts_policy_fields():
    row = dict(ROW_POLICY)
    row["title"] = "国务院文件<br/>测试"
    parsed = gov_policy.parse_search_response(_payload([row]), "gongwen")

    assert parsed["category"] == "国务院文件"
    assert parsed["total_count"] == 1
    assert parsed["records"] == [
        {
            "article_id": "gongwen:26164818",
            "source": "gov.cn policy document library",
            "category_id": "gongwen",
            "category": "国务院文件",
            "pub_date": "2026-06-02",
            "puborg": "国务院",
            "pcode": "国发〔2026〕14号",
            "index": "",
            "childtype": "农业、林业、水利\\农业、畜牧业、渔业",
            "title": "国务院文件 测试",
            "summary": "国务院关于印发《加快农业农村现代化 “十五五”规划》的通知 请认真贯彻执行。",
            "url": "https://www.gov.cn/zhengce/zhengceku/202606/content_7070902.htm",
            "raw_id": "26164818",
            "raw_pubtime": None,
            "raw_ptime": None,
            "raw_sha256": parsed["records"][0]["raw_sha256"],
            "parsed_at": parsed["records"][0]["parsed_at"],
        }
    ]


def test_crawl_writes_raw_parsed_manifest_and_get_returns_csv(tmp_path):
    responses = {
        1: _payload([ROW_POLICY], page=1, page_size=1, total_count=2),
        2: _payload([ROW_ENERGY], page=2, page_size=1, total_count=2),
    }
    seen_pages: list[int] = []

    def fetcher(params):
        assert params["t"] == "zhengcelibrary_gw"
        assert params["mintime"] == "2026-05-29"
        assert params["maxtime"] == "2026-06-05"
        page = int(params["p"])
        seen_pages.append(page)
        return responses[page]

    run = gov_policy.crawl_gov_policy_documents(
        cache_dir=tmp_path,
        start_date="2026-05-29",
        end_date="2026-06-05",
        categories=["gongwen"],
        max_pages_per_category=5,
        page_size=1,
        fetcher=fetcher,
    )

    assert seen_pages == [1, 2]
    assert run["fetched_pages"] == 2
    assert run["new_records"] == 2
    assert (tmp_path / "raw" / "gongwen" / "page-0001.json").is_file()
    assert (tmp_path / "parsed" / "policy_documents.jsonl").is_file()
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["categories"]["gongwen"]["total_count"] == 2

    out = gov_policy.get_gov_policy_documents(
        "2026-06-05",
        7,
        cache_dir=tmp_path,
        keywords=("能源",),
    )

    assert "Gov.cn Policy Documents" in out
    assert "非化石能源" in out
    assert "农业农村现代化" not in out


def test_get_gov_policy_uses_cached_records_when_refresh_fails(tmp_path):
    gov_policy.crawl_gov_policy_documents(
        cache_dir=tmp_path,
        start_date="2026-05-29",
        end_date="2026-06-05",
        categories=["gongwen"],
        fetcher=lambda params: _payload([ROW_POLICY], page=int(params["p"])),
    )

    def fail_fetcher(params):
        raise RuntimeError(f"unexpected fetch {params}")

    out = gov_policy.get_gov_policy_documents(
        "2026-06-04",
        3,
        cache_dir=tmp_path,
        fetcher=fail_fetcher,
    )

    assert "local cache; refresh skipped" in out
    assert "加快农业农村现代化" in out
