from datetime import date
from pathlib import Path
import sys

import pytest


PLUGIN_PARENT = Path(__file__).resolve().parents[1] / "data" / "plugins"
if str(PLUGIN_PARENT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_PARENT))

from astrbot_plugin_cugb_jwc_notice.providers.notice_provider import (
    NoticeProvider,
    resolve_source,
)
from astrbot_plugin_cugb_jwc_notice.providers.vendor.cugb_api.client import (
    ResourceNotFound,
)
from astrbot_plugin_cugb_jwc_notice.providers.vendor.cugb_api.cms_parser import (
    parse_cms_detail,
    parse_cms_notices,
)
from astrbot_plugin_cugb_jwc_notice.providers.vendor.cugb_api.models import Notice
from astrbot_plugin_cugb_jwc_notice.providers.vendor.cugb_api.parser import (
    parse_notice_detail,
    parse_notices,
)
from astrbot_plugin_cugb_jwc_notice.repositories.notice_repo import NoticeRepository
from astrbot_plugin_cugb_jwc_notice.services.notice_service import NoticeService
from astrbot_plugin_cugb_jwc_notice.utils.config_loader import load_plugin_config


CMS_LIST_HTML = """
<div class="list_content"><ul id="list_container">
  <li class="list_cont_li">
    <a class="list_i_l" href="/c/2026-07-23/200002.shtml"><img src="/2.png"></a>
    <div class="list_i_r">
      <a class="p1" href="/c/2026-07-23/200002.shtml"><div class="list_title">学院列表第二条</div></a>
      <p>2026-07-23</p>
    </div>
  </li>
  <li class="list_cont_li">
    <a class="list_i_l" href="/c/2026-07-22/200001.shtml"><img src="/1.png"></a>
    <div class="list_i_r">
      <a class="p1" href="/c/2026-07-22/200001.shtml"><div class="list_title">学院列表第一条</div></a>
      <p>2026-07-22</p>
    </div>
  </li>
</ul></div>
"""

CMS_DETAIL_HTML = """
<div class="detail_cont">
  <p class="tit">学院详情标题</p>
  <p class="des">2026-07-23　发布：测试作者　点击：<span>10</span>次</p>
  <p>正文第一段。</p>
  <p>正文第二段，包含<a href="/upload/file.pdf">附件</a>。</p>
  <p><img src="/upload/image.png"></p>
</div>
"""


def make_row(source, notice_id, title, published_date, content="正文"):
    host = {
        "jwc_student": "https://jwc.cugb.edu.cn",
        "jwc_teaching": "https://jwc.cugb.edu.cn",
        "ai_news": "https://sai.cugb.edu.cn",
        "ai_notice": "https://sai.cugb.edu.cn",
        "xgb_news": "https://bm.cugb.edu.cn",
        "xgb_notice": "https://bm.cugb.edu.cn",
    }[source]
    return {
        "id": notice_id,
        "title": title,
        "published_date": published_date,
        "url": f"{host}/c/{published_date}/{notice_id}.shtml",
    }


class FakeProvider:
    def __init__(self) -> None:
        self.rows = {
            "jwc_student": [
                make_row("jwc_student", "100001", "第一条通知", "2026-07-23")
            ]
        }
        self.details = {
            "jwc_student": {
                "100001": {
                    "notice_id": "100001",
                    "title": "第一条通知",
                    "published_date": "2026-07-23",
                    "author": "教务处",
                    "url": "https://jwc.cugb.edu.cn/c/2026-07-23/100001.shtml",
                    "content": "关于培养方案的正文",
                    "links": [
                        {
                            "text": "附件",
                            "url": "https://jwc.cugb.edu.cn/files/plan.pdf",
                        }
                    ],
                    "images": [],
                }
            }
        }
        self.detail_calls: list[tuple[str, str]] = []
        self.fetch_calls: list[tuple[str, str, set[str]]] = []
        self.detail_failures: dict[tuple[str, str], int] = {}

    def add_notice(self, source, notice_id, title, published_date, content="正文"):
        self.rows.setdefault(source, []).append(
            make_row(source, notice_id, title, published_date)
        )
        self.details.setdefault(source, {})[notice_id] = {
            "notice_id": notice_id,
            "title": title,
            "published_date": published_date,
            "author": "",
            "url": make_row(source, notice_id, title, published_date)["url"],
            "content": content,
            "links": [],
            "images": [],
        }

    async def fetch_list(self, source, earliest_date, stop_at_ids=None):
        self.fetch_calls.append((source, earliest_date, set(stop_at_ids or set())))
        return list(self.rows.get(source, [])), 1

    async def fetch_detail(self, source, published_date, notice_id):
        self.detail_calls.append((source, notice_id))
        if self.detail_failures.get((source, notice_id), 0):
            self.detail_failures[(source, notice_id)] -= 1
            raise RuntimeError("temporary detail failure")
        return dict(self.details[source][notice_id])


def build_service(tmp_path, provider, send):
    config = load_plugin_config(None)
    repository = NoticeRepository(tmp_path / "notices.sqlite")
    return NoticeService(config, provider, repository, send), repository


def jwc_calls(provider):
    return [call for call in provider.fetch_calls if call[0] == "jwc_student"]


@pytest.mark.asyncio
async def test_first_sync_seeds_baseline_and_unchanged_sync_does_not_send(tmp_path):
    provider = FakeProvider()
    sent = []

    async def send(target, text):
        sent.append((target, text))
        return True

    service, repository = build_service(tmp_path, provider, send)
    service.bind_target("aiocqhttp:FriendMessage:123")

    first = await service.refresh()
    second = await service.refresh()

    assert first["success"] is True
    assert first["data"]["scanned_notice_count"] == 1
    assert first["data"]["database_total_count"] == 1
    assert "fetched_count" not in first["data"]
    assert first["data"]["new_count"] == 0
    assert second["data"]["new_count"] == 0
    assert sent == []
    assert repository.status()["notice_count"] == 1
    assert provider.detail_calls == [("jwc_student", "100001")]
    calls = jwc_calls(provider)
    assert calls[0][1] == "2025-09-01"
    assert calls[1][2] == {"100001"}
    assert first["data"]["sources"]["jwc_student"]["scan_mode"] == (
        "historical_backfill"
    )
    assert second["data"]["sources"]["jwc_student"]["scan_mode"] == "incremental"


@pytest.mark.asyncio
async def test_new_notice_is_pushed_once(tmp_path):
    provider = FakeProvider()
    sent = []

    async def send(target, text):
        sent.append((target, text))
        return True

    service, repository = build_service(tmp_path, provider, send)
    service.bind_target("aiocqhttp:FriendMessage:123")
    await service.refresh()
    provider.rows["jwc_student"].insert(
        0, make_row("jwc_student", "100002", "第二条通知", "2026-07-24")
    )
    provider.details["jwc_student"]["100002"] = {
        "notice_id": "100002",
        "title": "第二条通知",
        "published_date": "2026-07-24",
        "author": "",
        "url": "https://jwc.cugb.edu.cn/c/2026-07-24/100002.shtml",
        "content": "新的正文",
        "links": [],
        "images": [],
    }

    updated = await service.refresh()
    unchanged = await service.refresh()

    assert updated["data"]["new_count"] == 1
    assert updated["data"]["sent_count"] == 1
    assert unchanged["data"]["new_count"] == 0
    assert len(sent) == 1
    assert "第二条通知" in sent[0][1]
    assert "教务处·学生专区" in sent[0][1]
    assert repository.status()["pending_notification_count"] == 0


@pytest.mark.asyncio
async def test_expanding_date_range_backfills_without_push(tmp_path):
    provider = FakeProvider()
    sent = []

    async def send(target, text):
        sent.append((target, text))
        return True

    service, repository = build_service(tmp_path, provider, send)
    service.bind_target("aiocqhttp:FriendMessage:123")
    await service.refresh()
    provider.add_notice(
        "jwc_student", "099999", "历史通知", "2025-09-01", "入学时的历史正文"
    )

    result = await service.refresh("jwc_student", "2025-09-01")

    assert result["data"]["new_count"] == 1
    assert result["data"]["notification_count"] == 0
    assert sent == []
    status = repository.status()
    assert status["sources"]["jwc_student"]["earliest_cached_date"] == "2025-09-01"


@pytest.mark.asyncio
async def test_rejects_invalid_source_and_date_before_fetch(tmp_path):
    provider = FakeProvider()

    async def send(target, text):
        return True

    service, _ = build_service(tmp_path, provider, send)

    bad_source = await service.refresh("不存在来源")
    bad_date = await service.refresh("jwc_student", "2025/09/01")

    assert bad_source["success"] is False
    assert bad_source["error_code"] == "INVALID_SOURCE"
    assert bad_date["success"] is False
    assert bad_date["error_code"] == "INVALID_EARLIEST_DATE"
    assert provider.fetch_calls == []


@pytest.mark.asyncio
async def test_failed_source_does_not_block_others(tmp_path):
    provider = FakeProvider()

    class FlakyProvider(FakeProvider):
        async def fetch_list(self, source, earliest_date, stop_at_ids=None):
            if source == "ai_news":
                raise RuntimeError("boom")
            return await super().fetch_list(source, earliest_date, stop_at_ids)

    provider = FlakyProvider()

    async def send(target, text):
        return True

    service, repository = build_service(tmp_path, provider, send)

    result = await service.refresh()

    assert result["success"] is False
    assert result["error_code"] == "PARTIAL_SYNC_FAILURE"
    assert result["data"]["failed_sources"] == ["ai_news"]
    assert result["data"]["sources"]["jwc_student"]["success"] is True
    assert repository.status()["sources"]["jwc_student"]["notice_count"] == 1


@pytest.mark.asyncio
async def test_failed_push_stays_pending_and_retries(tmp_path):
    provider = FakeProvider()
    outcomes = [False, True]
    calls = []

    async def send(target, text):
        calls.append(text)
        return outcomes.pop(0)

    service, repository = build_service(tmp_path, provider, send)
    service.bind_target("aiocqhttp:FriendMessage:123")
    await service.refresh()
    provider.add_notice("jwc_student", "100003", "可重试通知", "2026-07-25")

    failed = await service.refresh()
    retried = await service.refresh()

    assert failed["data"]["pending_count"] == 1
    assert retried["data"]["sent_count"] == 1
    assert len(calls) == 2
    assert repository.status()["pending_notification_count"] == 0


@pytest.mark.asyncio
async def test_large_push_queue_is_confirmed_in_independent_batches(tmp_path):
    provider = FakeProvider()
    outcomes = [True, False, True, True]
    calls = []

    async def send(target, text):
        calls.append(text)
        return outcomes.pop(0)

    service, repository = build_service(tmp_path, provider, send)
    service.bind_target("aiocqhttp:FriendMessage:123")
    repository.apply_snapshot(
        "jwc_student",
        [
            {
                "id": "199999",
                "title": "已有通知",
                "published_date": "2026-07-23",
                "url": "https://jwc.cugb.edu.cn/c/2026-07-23/199999.shtml",
                "content": "正文",
                "links": [],
                "images": [],
            }
        ],
    )
    rows = [
        {
            "id": str(200000 + index),
            "title": f"批量通知 {index}",
            "published_date": "2026-07-24",
            "url": f"https://jwc.cugb.edu.cn/c/2026-07-24/{200000 + index}.shtml",
            "content": "正文",
            "links": [],
            "images": [],
        }
        for index in range(21)
    ]
    repository.apply_snapshot("jwc_student", rows)

    partial = await service.deliver_pending()
    retried = await service.deliver_pending()

    assert partial["sent_count"] == 10
    assert partial["pending_count"] == 11
    assert retried["sent_count"] == 11
    assert retried["pending_count"] == 0
    assert len(calls) == 4
    assert "CUGB 新闻公告有 10 条更新" in calls[0]
    assert "CUGB 新闻公告有 1 条更新" in calls[3]
    assert repository.status()["pending_notification_count"] == 0


@pytest.mark.asyncio
async def test_same_notice_id_in_different_sources_is_isolated(tmp_path):
    provider = FakeProvider()
    provider.rows = {"jwc_student": [], "ai_news": []}
    provider.details = {"jwc_student": {}, "ai_news": {}}
    provider.add_notice("jwc_student", "100001", "教务条目", "2026-07-23", "教务正文")
    provider.add_notice("ai_news", "100001", "学院条目", "2026-07-23", "学院正文")

    async def send(target, text):
        return True

    service, repository = build_service(tmp_path, provider, send)
    await service.refresh()

    jwc = service.search("教务正文", "jwc_student")
    ai = service.search("学院正文", "ai_news")
    everything = service.list_notices()

    assert jwc["data"]["total_count"] == 1
    assert jwc["data"]["items"][0]["title"] == "教务条目"
    assert ai["data"]["total_count"] == 1
    assert ai["data"]["items"][0]["title"] == "学院条目"
    assert everything["data"]["total_count"] == 2
    assert repository.statistics("jwc_student")["total_count"] == 1
    assert repository.statistics()["source_counts"] == {"ai_news": 1, "jwc_student": 1}


@pytest.mark.asyncio
async def test_searches_cached_body_and_returns_links(tmp_path):
    provider = FakeProvider()

    async def send(target, text):
        return True

    service, _ = build_service(tmp_path, provider, send)
    await service.refresh()

    result = service.search("培养方案")

    assert result["success"] is True
    assert result["data"]["total_count"] == 1
    assert result["data"]["returned_count"] == 1
    assert result["data"]["items"][0]["notice_id"] == "100001"
    assert result["data"]["items"][0]["source"] == "jwc_student"
    assert result["data"]["items"][0]["source_label"] == "教务处·学生专区"
    assert "content" not in result["data"]["items"][0]
    assert "培养方案" in result["data"]["items"][0]["matched_excerpt"]
    assert result["data"]["items"][0]["links"][0]["url"].endswith("plan.pdf")
    empty = service.search("不存在")
    assert empty["data"]["total_count"] == 0
    assert empty["data"]["items"] == []
    wrong_source = service.search("培养方案", "ai_news")
    assert wrong_source["data"]["total_count"] == 0


@pytest.mark.asyncio
async def test_statistics_and_paginated_list_report_true_totals(tmp_path):
    provider = FakeProvider()
    for index, published_date in enumerate(("2026-06-01", "2025-09-01"), start=2):
        notice_id = str(100000 + index)
        provider.add_notice(
            "jwc_student", notice_id, f"第 {index} 条", published_date, "共同关键词"
        )

    async def send(target, text):
        return True

    service, _ = build_service(tmp_path, provider, send)
    await service.refresh()

    stats = service.statistics()
    jwc_stats = service.statistics("jwc_student")
    first_page = service.list_notices(limit=2)
    second_page = service.list_notices(limit=2, offset=2)
    search_page = service.search("共同关键词", limit=1)
    search_next_page = service.search("共同关键词", limit=1, offset=1)

    assert stats["data"]["total_count"] == 3
    assert stats["data"]["earliest_date"] == "2025-09-01"
    assert stats["data"]["source_counts"] == {"jwc_student": 3}
    assert stats["data"]["monthly_counts"] == [
        {"month": "2025-09", "count": 1},
        {"month": "2026-06", "count": 1},
        {"month": "2026-07", "count": 1},
    ]
    assert jwc_stats["data"]["coverage_since_date"] == {"jwc_student": "2025-09-01"}
    assert first_page["data"]["total_count"] == 3
    assert first_page["data"]["returned_count"] == 2
    assert first_page["data"]["has_more"] is True
    assert second_page["data"]["returned_count"] == 1
    assert second_page["data"]["has_more"] is False
    assert search_page["data"]["total_count"] == 2
    assert search_page["data"]["returned_count"] == 1
    assert search_page["data"]["has_more"] is True
    assert search_next_page["data"]["total_count"] == 2
    assert search_next_page["data"]["returned_count"] == 1
    assert search_next_page["data"]["has_more"] is False


@pytest.mark.asyncio
async def test_missing_detail_retries_without_repeating_historical_scan(tmp_path):
    provider = FakeProvider()
    provider.detail_failures[("jwc_student", "100001")] = 1

    async def send(target, text):
        return True

    service, repository = build_service(tmp_path, provider, send)

    first = await service.refresh()
    second = await service.refresh()

    assert first["data"]["sources"]["jwc_student"]["detail_error_count"] == 1
    assert second["data"]["sources"]["jwc_student"]["detail_retry_count"] == 1
    assert jwc_calls(provider)[1][2] == {"100001"}
    assert provider.detail_calls == [("jwc_student", "100001")] * 2
    assert repository.statistics("jwc_student")["missing_content_count"] == 0


def test_query_validation_rejects_bad_ranges_and_pagination(tmp_path):
    async def send(target, text):
        return True

    service, _ = build_service(tmp_path, FakeProvider(), send)

    assert service.statistics("", "2025/09/01")["error_code"] == "INVALID_QUERY_DATE"
    assert (
        service.list_notices("", "2026-01-01", "2025-01-01")["error_code"]
        == "INVALID_DATE_RANGE"
    )
    assert service.search("通知", limit=0)["error_code"] == "INVALID_LIMIT"
    assert service.search("通知", limit="10")["error_code"] == "INVALID_LIMIT"
    assert service.list_notices(offset=-1)["error_code"] == "INVALID_OFFSET"
    assert service.list_notices(offset=False)["error_code"] == "INVALID_OFFSET"
    assert service.list_notices("不存在来源")["error_code"] == "INVALID_SOURCE"
    assert service.statistics("不存在来源")["error_code"] == "INVALID_SOURCE"


def test_source_alias_resolution():
    assert resolve_source("") == ""
    assert resolve_source("教务") == "jwc_student"
    assert resolve_source("jwc_student") == "jwc_student"
    assert resolve_source("JXDT") == "jwc_teaching"
    assert resolve_source("AI新闻") == "ai_news"
    assert resolve_source("ai-notice") == "ai_notice"
    assert resolve_source("学工新闻") == "xgb_news"
    assert resolve_source("学工公告") == "xgb_notice"
    with pytest.raises(ValueError):
        resolve_source("不存在")


def test_vendored_parser_reads_list_and_detail_links():
    list_html = """
    <div id="list_detail_box"><a href="/c/2026-07-23/100001.shtml">
      <div class="list_con_main">测试通知</div>
      <div class="list_con_time">2026-07-23</div>
    </a></div>
    """
    detail_html = """
    <div class="detail_title">测试通知</div>
    <div class="detail_info">作者：教务处 发布时间：2026-07-23</div>
    <div class="detail_content_box"><p>正文关键词</p><a href="/a.pdf">附件</a></div>
    """

    notices = parse_notices(list_html, "https://jwc.cugb.edu.cn/xszq/")
    detail = parse_notice_detail(
        detail_html,
        notices[0].url,
        notice_id=notices[0].notice_id,
        fallback_date=notices[0].published_date,
    )

    assert notices[0].notice_id == "100001"
    assert detail.content == "正文关键词\n附件"
    assert detail.links[0].url == "https://jwc.cugb.edu.cn/a.pdf"


def test_vendored_cms_parser_reads_list_and_detail():
    notices = parse_cms_notices(CMS_LIST_HTML, "https://sai.cugb.edu.cn/xyxw/")
    detail = parse_cms_detail(
        CMS_DETAIL_HTML,
        notices[0].url,
        notice_id=notices[0].notice_id,
        fallback_date=notices[0].published_date,
    )

    assert len(notices) == 2
    assert notices[0].title == "学院列表第二条"
    assert notices[0].notice_id == "200002"
    assert detail.title == "学院详情标题"
    assert detail.author == "测试作者"
    assert detail.content == "正文第一段。\n正文第二段，包含附件。"
    assert detail.links[0].url == "https://sai.cugb.edu.cn/upload/file.pdf"
    assert detail.images == ("https://sai.cugb.edu.cn/upload/image.png",)


def test_provider_pages_until_cutoff_and_stops_at_known_notice():
    class FakeClient:
        def __init__(self):
            self.calls = []

        def fetch_page(self, page):
            self.calls.append(page)
            pages = {
                1: [
                    Notice("新通知", "2026-07-23", "https://example/3.shtml"),
                    Notice("已知通知", "2026-07-01", "https://example/2.shtml"),
                ],
                2: [
                    Notice("范围内", "2025-09-01", "https://example/1.shtml"),
                ],
                3: [
                    Notice("范围外", "2025-08-31", "https://example/0.shtml"),
                ],
            }
            if page not in pages:
                raise ResourceNotFound("end")
            return pages[page]

    provider = NoticeProvider(load_plugin_config(None))
    client = FakeClient()
    provider.clients["jwc_student"] = client

    rows, pages = provider._fetch_list_sync("jwc_student", "2025-09-01", set())

    assert pages == 3
    assert [row["id"] for row in rows] == ["3", "2", "1"]
    assert client.calls == [1, 2, 3]

    client.calls.clear()
    rows, pages = provider._fetch_list_sync("jwc_student", "2025-09-01", {"2"})

    assert pages == 1
    assert [row["id"] for row in rows] == ["3", "2"]
    assert client.calls == [1]


def test_provider_skips_search_archive_for_sources_without_catalog():
    class EmptyClient:
        def fetch_page(self, page):
            if page > 1:
                raise ResourceNotFound("end")
            return [Notice("新条目", "2026-07-23", "https://example/9.shtml")]

    provider = NoticeProvider(load_plugin_config(None))
    provider.clients["ai_news"] = EmptyClient()
    provider.clients["jwc_student"] = EmptyClient()

    search_calls = []

    def fake_search(catalog_id, cutoff):
        search_calls.append(catalog_id)
        return [], 0

    provider._fetch_search_archive = fake_search

    rows, _ = provider._fetch_list_sync("ai_news", "2025-09-01", set())
    assert [row["id"] for row in rows] == ["9"]
    assert search_calls == []

    provider._fetch_list_sync("jwc_student", "2025-09-01", set())
    assert search_calls == ["17068"]


def test_provider_normalizes_official_search_archive_items():
    item = {
        "id": 831161,
        "catalogID": "17068",
        "title": "<font color=red>2025</font> 年通知",
        "publishDate": "2025-09-01 12:00:00",
        "jsonLink": "https://jwc.cugb.edu.cn/c/2025-09-01/831161.shtml",
    }

    row = NoticeProvider._normalize_search_item(item, "17068", date(2025, 9, 1), 2025)

    assert row == {
        "id": "831161",
        "title": "2025 年通知",
        "published_date": "2025-09-01",
        "url": "https://jwc.cugb.edu.cn/c/2025-09-01/831161.shtml",
    }
    assert (
        NoticeProvider._normalize_search_item(
            {**item, "catalogID": "other"}, "17068", date(2025, 9, 1), 2025
        )
        is None
    )


def test_legacy_single_source_database_is_migrated(tmp_path):
    import sqlite3

    database = tmp_path / "legacy.sqlite"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE notices (
                notice_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                published_date TEXT NOT NULL,
                url TEXT NOT NULL,
                author TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL DEFAULT '',
                links_json TEXT NOT NULL DEFAULT '[]',
                images_json TEXT NOT NULL DEFAULT '[]',
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            );
            CREATE TABLE notification_deliveries (
                notice_id TEXT PRIMARY KEY,
                state TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                delivered_at TEXT
            );
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO notices VALUES (
                '100001', '旧通知', '2026-07-01',
                'https://jwc.cugb.edu.cn/c/2026-07-01/100001.shtml',
                '教务处', '旧正文', '[]', '[]', '2026-07-01', '2026-07-01'
            );
            INSERT INTO notification_deliveries VALUES (
                '100001', 'delivered', '2026-07-01', '2026-07-01'
            );
            INSERT INTO settings VALUES ('coverage_since_date', '2025-09-01');
            INSERT INTO settings VALUES ('initialized', '1');
            """
        )

    repository = NoticeRepository(database)
    status = repository.status()

    assert status["sources"]["jwc_student"]["notice_count"] == 1
    assert status["sources"]["jwc_student"]["coverage_since_date"] == "2025-09-01"
    assert repository.get_setting("coverage_since_date") == ""
    row = repository.query_notices(keyword="旧正文", content_only=True)["rows"][0]
    assert row["source"] == "jwc_student"
    assert row["notice_id"] == "100001"
    pending = repository.pending_notifications()
    assert pending == []
