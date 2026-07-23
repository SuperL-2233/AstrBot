from datetime import date
from pathlib import Path
import sys

import pytest


PLUGIN_PARENT = Path(__file__).resolve().parents[1] / "data" / "plugins"
if str(PLUGIN_PARENT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_PARENT))

from astrbot_plugin_cugb_jwc_notice.providers.vendor.cugb_jwc_api.parser import (
    parse_notice_detail,
    parse_notices,
)
from astrbot_plugin_cugb_jwc_notice.providers.notice_provider import NoticeProvider
from astrbot_plugin_cugb_jwc_notice.providers.vendor.cugb_jwc_api.client import (
    ResourceNotFound,
)
from astrbot_plugin_cugb_jwc_notice.providers.vendor.cugb_jwc_api.models import Notice
from astrbot_plugin_cugb_jwc_notice.repositories.notice_repo import NoticeRepository
from astrbot_plugin_cugb_jwc_notice.services.notice_service import NoticeService
from astrbot_plugin_cugb_jwc_notice.utils.config_loader import load_plugin_config


class FakeProvider:
    def __init__(self) -> None:
        self.rows = [
            {
                "id": "100001",
                "title": "第一条通知",
                "published_date": "2026-07-23",
                "url": "https://jwc.cugb.edu.cn/c/2026-07-23/100001.shtml",
            }
        ]
        self.details = {
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
        self.detail_calls: list[str] = []
        self.fetch_calls: list[tuple[str, set[str]]] = []
        self.detail_failures: dict[str, int] = {}

    async def fetch_list(self, earliest_date, stop_at_ids=None):
        self.fetch_calls.append((earliest_date, set(stop_at_ids or set())))
        return list(self.rows), 1

    async def fetch_detail(self, published_date, notice_id):
        self.detail_calls.append(notice_id)
        if self.detail_failures.get(notice_id, 0):
            self.detail_failures[notice_id] -= 1
            raise RuntimeError("temporary detail failure")
        return dict(self.details[notice_id])


def build_service(tmp_path, provider, send):
    config = load_plugin_config(None)
    repository = NoticeRepository(tmp_path / "jwc.sqlite")
    return NoticeService(config, provider, repository, send), repository


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
    assert provider.detail_calls == ["100001"]
    assert provider.fetch_calls[0][0] == "2025-09-01"
    assert provider.fetch_calls[1][1] == {"100001"}


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
    provider.rows.insert(
        0,
        {
            "id": "100002",
            "title": "第二条通知",
            "published_date": "2026-07-24",
            "url": "https://jwc.cugb.edu.cn/c/2026-07-24/100002.shtml",
        },
    )
    provider.details["100002"] = {
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
    provider.rows.append(
        {
            "id": "099999",
            "title": "历史通知",
            "published_date": "2025-09-01",
            "url": "https://jwc.cugb.edu.cn/c/2025-09-01/099999.shtml",
        }
    )
    provider.details["099999"] = {
        "notice_id": "099999",
        "title": "历史通知",
        "published_date": "2025-09-01",
        "author": "",
        "url": "https://jwc.cugb.edu.cn/c/2025-09-01/099999.shtml",
        "content": "入学时的历史正文",
        "links": [],
        "images": [],
    }

    result = await service.refresh("2025-09-01")

    assert result["data"]["new_count"] == 1
    assert result["data"]["notification_count"] == 0
    assert sent == []
    assert repository.status()["earliest_cached_date"] == "2025-09-01"


@pytest.mark.asyncio
async def test_rejects_invalid_earliest_date_before_fetch(tmp_path):
    provider = FakeProvider()

    async def send(target, text):
        return True

    service, _ = build_service(tmp_path, provider, send)

    result = await service.refresh("2025/09/01")

    assert result["success"] is False
    assert result["error_code"] == "INVALID_EARLIEST_DATE"
    assert provider.fetch_calls == []


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
    provider.rows.append(
        {
            "id": "100003",
            "title": "可重试通知",
            "published_date": "2026-07-25",
            "url": "https://jwc.cugb.edu.cn/c/2026-07-25/100003.shtml",
        }
    )
    provider.details["100003"] = {
        "notice_id": "100003",
        "title": "可重试通知",
        "published_date": "2026-07-25",
        "author": "",
        "url": "https://jwc.cugb.edu.cn/c/2026-07-25/100003.shtml",
        "content": "正文",
        "links": [],
        "images": [],
    }

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
        ]
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
    repository.apply_snapshot(rows)

    partial = await service.deliver_pending()
    retried = await service.deliver_pending()

    assert partial["sent_count"] == 10
    assert partial["pending_count"] == 11
    assert retried["sent_count"] == 11
    assert retried["pending_count"] == 0
    assert len(calls) == 4
    assert "教务处有 10 条新通知" in calls[0]
    assert "教务处有 1 条新通知" in calls[3]
    assert repository.status()["pending_notification_count"] == 0


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
    assert "content" not in result["data"]["items"][0]
    assert "培养方案" in result["data"]["items"][0]["matched_excerpt"]
    assert result["data"]["items"][0]["links"][0]["url"].endswith("plan.pdf")
    empty = service.search("不存在")
    assert empty["data"]["total_count"] == 0
    assert empty["data"]["items"] == []


@pytest.mark.asyncio
async def test_statistics_and_paginated_list_report_true_totals(tmp_path):
    provider = FakeProvider()
    for index, published_date in enumerate(("2026-06-01", "2025-09-01"), start=2):
        notice_id = str(100000 + index)
        provider.rows.append(
            {
                "id": notice_id,
                "title": f"第 {index} 条",
                "published_date": published_date,
                "url": f"https://jwc.cugb.edu.cn/c/{published_date}/{notice_id}.shtml",
            }
        )
        provider.details[notice_id] = {
            "notice_id": notice_id,
            "title": f"第 {index} 条",
            "published_date": published_date,
            "author": "",
            "url": f"https://jwc.cugb.edu.cn/c/{published_date}/{notice_id}.shtml",
            "content": "共同关键词",
            "links": [],
            "images": [],
        }

    async def send(target, text):
        return True

    service, _ = build_service(tmp_path, provider, send)
    await service.refresh()

    stats = service.statistics()
    first_page = service.list_notices(limit=2)
    second_page = service.list_notices(limit=2, offset=2)
    search_page = service.search("共同关键词", limit=1)
    search_next_page = service.search("共同关键词", limit=1, offset=1)

    assert stats["data"]["total_count"] == 3
    assert stats["data"]["earliest_date"] == "2025-09-01"
    assert stats["data"]["monthly_counts"] == [
        {"month": "2025-09", "count": 1},
        {"month": "2026-06", "count": 1},
        {"month": "2026-07", "count": 1},
    ]
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
    provider.detail_failures["100001"] = 1

    async def send(target, text):
        return True

    service, repository = build_service(tmp_path, provider, send)

    first = await service.refresh()
    second = await service.refresh()

    assert first["data"]["detail_error_count"] == 1
    assert second["data"]["detail_retry_count"] == 1
    assert provider.fetch_calls[1][1] == {"100001"}
    assert provider.detail_calls == ["100001", "100001"]
    assert repository.statistics()["missing_content_count"] == 0


def test_query_validation_rejects_bad_ranges_and_pagination(tmp_path):
    async def send(target, text):
        return True

    service, _ = build_service(tmp_path, FakeProvider(), send)

    assert service.statistics("2025/09/01")["error_code"] == "INVALID_QUERY_DATE"
    assert (
        service.list_notices("2026-01-01", "2025-01-01")["error_code"]
        == "INVALID_DATE_RANGE"
    )
    assert service.search("通知", limit=0)["error_code"] == "INVALID_LIMIT"
    assert service.search("通知", limit="10")["error_code"] == "INVALID_LIMIT"
    assert service.list_notices(offset=-1)["error_code"] == "INVALID_OFFSET"
    assert service.list_notices(offset=False)["error_code"] == "INVALID_OFFSET"


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
    provider.client = client

    rows, pages = provider._fetch_list_sync("2025-09-01", set())

    assert pages == 3
    assert [row["id"] for row in rows] == ["3", "2", "1"]
    assert client.calls == [1, 2, 3]

    client.calls.clear()
    rows, pages = provider._fetch_list_sync("2025-09-01", {"2"})

    assert pages == 1
    assert [row["id"] for row in rows] == ["3", "2"]
    assert client.calls == [1]


def test_provider_normalizes_official_search_archive_items():
    item = {
        "id": 831161,
        "catalogID": "17068",
        "title": "<font color=red>2025</font> 年通知",
        "publishDate": "2025-09-01 12:00:00",
        "jsonLink": "https://jwc.cugb.edu.cn/c/2025-09-01/831161.shtml",
    }

    row = NoticeProvider._normalize_search_item(item, date(2025, 9, 1), 2025)

    assert row == {
        "id": "831161",
        "title": "2025 年通知",
        "published_date": "2025-09-01",
        "url": "https://jwc.cugb.edu.cn/c/2025-09-01/831161.shtml",
    }
    assert (
        NoticeProvider._normalize_search_item(
            {**item, "catalogID": "other"}, date(2025, 9, 1), 2025
        )
        is None
    )
