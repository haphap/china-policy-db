# china-policy-db

Parsed China policy datasets used by MOSAIC-Agents and other downstream agents.

This repository publishes durable parsed records only. It intentionally does not
commit cached PBOC HTML pages or raw gov.cn API responses.

## Datasets

| Dataset | Canonical file | CSV export | Source |
| --- | --- | --- | --- |
| PBOC open-market announcements | `data/pboc_ops/parsed/articles.jsonl` | `data/pboc_ops/parsed/articles.csv` | `www.pbc.gov.cn` public open-market pages |
| State Council policy documents | `data/gov_policy/parsed/policy_documents.jsonl` | `data/gov_policy/parsed/policy_documents.csv` | `sousuo.www.gov.cn/search-gov/data` |

`data/manifest.json` records counts, date coverage, and relative file paths for
machine readers.

## Update

Install locally:

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[test]"
```

Run incremental updates:

```bash
python -m china_policy_db update pboc
python -m china_policy_db update gov-policy
python -m china_policy_db validate
```

Run a bounded gov.cn backfill window:

```bash
python -m china_policy_db update gov-policy --start-date 2005-01-01 --end-date 2005-01-31
```

Run a multi-window gov.cn backfill:

```bash
python -m china_policy_db backfill gov-policy --start-date 2005-01-01 --end-date 2026-06-05
```

Run a full PBOC crawl:

```bash
python -m china_policy_db update pboc --full
```

## Storage Contract

- JSONL is canonical; CSV files are regenerated exports for quick inspection and
  agent context.
- Records are merged by stable IDs and URLs, so updates are idempotent.
- Raw source checksums are retained as provenance fields, but raw pages are not
  published.
- PBOC seed data currently covers records back to 2004, which is earlier than
  the 2005 target requested by MOSAIC.

## License

Apache-2.0 for code in this repository. Source data is collected from public
government websites; respect the source websites' terms and availability.
