#!/usr/bin/env python3
"""导出学习通全部课程的视频章节、作业和讨论信息。

脚本只执行登录和 GET 请求，不会提交作业、播放视频或写入学习进度。
默认输出 UTF-8 BOM CSV（Excel 可直接打开）以及一份 JSON 原始结果。
"""

from __future__ import annotations

import argparse
import base64
import csv
import getpass
import html
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad


AES_KEY = b"u2oh6Vu^HWe4_AES"
LOGIN_PAGE = "https://passport2.chaoxing.com/login"
LOGIN_API = "https://passport2.chaoxing.com/fanyalogin"
COURSES_API = "https://mooc2-ans.chaoxing.com/visit/courses/list"
CHAPTER_API = "https://mooc1-api.chaoxing.com/gas/clazz"
COURSE_SETTING_API = "https://mooc1-api.chaoxing.com/course/phone/get-course-setting"
DISCUSSION_API = "https://groupweb.chaoxing.com/course/topic/topicList"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

CHAPTER_FIELDS = (
    "id,bbsid,classscore,isstart,allowdownload,chatid,name,state,isfiled,"
    "visiblescore,begindate,"
    "coursesetting.fields(id,courseid,hiddencoursecover,coursefacecheck),"
    "course.fields(id,name,infocontent,objectid,app,bulletformat,mappingcourseid,"
    "imageurl,teacherfactor,jobcount,"
    "knowledge.fields(id,name,indexOrder,parentnodeid,status,layer,label,jobcount,"
    "begintime,endtime,attachment.fields(id,type,objectid,extension).type(video)))"
)

OUTPUT_COLUMNS = [
    "课程名称",
    "课程ID",
    "班级ID",
    "任务类型",
    "任务名称",
    "状态",
    "开始时间",
    "截止时间",
    "原始截止时间",
    "任务ID",
    "视频数量",
    "链接",
    "备注",
]


def aes_encrypt(value: str) -> str:
    cipher = AES.new(AES_KEY, AES.MODE_CBC, AES_KEY)
    encrypted = cipher.encrypt(pad(value.encode("utf-8"), AES.block_size))
    return base64.b64encode(encrypted).decode("ascii")


def create_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        }
    )
    return session


def login(session: requests.Session, phone: str, password: str, timeout: float) -> str:
    session.get(
        LOGIN_PAGE,
        params={
            "loginType": "1",
            "newversion": "true",
            "fid": "-1",
            "refer": "http://i.chaoxing.com",
        },
        timeout=timeout,
    )
    response = session.post(
        LOGIN_API,
        data={
            "fid": "-1",
            "uname": aes_encrypt(phone),
            "password": aes_encrypt(password),
            "refer": "http%3A%2F%2Fi.chaoxing.com",
            "t": "true",
            "forbidotherlogin": "0",
        },
        timeout=timeout,
    )
    response.raise_for_status()
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(f"登录接口没有返回 JSON: {response.text[:200]!r}") from exc
    if payload.get("status") is not True:
        message = payload.get("msg2") or payload.get("msg") or repr(payload)
        raise RuntimeError(f"登录失败: {message}")
    return str(session.cookies.get("_uid") or "")


def parse_attrs(tag: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    pattern = r"([:\w-]+)\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))"
    for match in re.finditer(pattern, tag, re.S):
        value = next((part for part in match.groups()[1:] if part is not None), "")
        attrs[match.group(1).lower()] = html.unescape(value)
    return attrs


def clean_text(fragment: str) -> str:
    fragment = re.sub(r"<(script|style)\b.*?</\1>", " ", fragment, flags=re.I | re.S)
    fragment = re.sub(r"<br\s*/?>", "\n", fragment, flags=re.I)
    fragment = re.sub(r"<[^>]+>", " ", fragment)
    return re.sub(r"[\t\r\f\v ]+", " ", html.unescape(fragment)).strip()


def get_courses(session: requests.Session, timeout: float) -> list[dict[str, str]]:
    response = session.get(
        COURSES_API,
        params={"v": int(time.time() * 1000), "rss": 1, "start": 0, "size": 500},
        timeout=timeout,
    )
    response.raise_for_status()
    response.encoding = "utf-8"
    if "passport2.chaoxing.com/login" in response.url:
        raise RuntimeError("课程列表请求被重定向到登录页，Cookie 未生效")

    courses: list[dict[str, str]] = []
    anchors = re.findall(r"<a\b[^>]*href=[\"'][^\"']*stucoursemiddle[^\"']*[\"'][^>]*>.*?</a>", response.text, re.I | re.S)
    for anchor in anchors:
        attrs = parse_attrs(anchor.split(">", 1)[0] + ">")
        link = attrs.get("href", "")
        if not link:
            continue
        query = parse_qs(urlparse(html.unescape(link)).query)
        course_id = (query.get("courseid") or query.get("courseId") or [""])[0]
        class_id = (query.get("clazzid") or query.get("classId") or [""])[0]
        if not course_id or not class_id:
            continue

        title_match = re.search(r"<span\b[^>]*title=[\"']([^\"']+)[\"']", anchor, re.I | re.S)
        name = html.unescape(title_match.group(1)).strip() if title_match else clean_text(anchor)
        name = re.sub(r"\s+", " ", name).strip()
        if name:
            courses.append(
                {
                    "name": name,
                    "courseId": course_id,
                    "classId": class_id,
                    "url": urljoin(response.url, html.unescape(link)),
                }
            )

    # 同一课程在页面中常出现“课程名/教师名”两个重复入口。
    deduped: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for course in courses:
        key = (course["courseId"], course["classId"])
        if key not in seen:
            seen.add(key)
            deduped.append(course)
    if not deduped:
        raise RuntimeError("课程页面在线，但没有解析到课程；页面结构可能已经变化")
    return deduped


def extract_hidden_fields(page: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for tag in re.findall(r"<input\b[^>]*>", page, re.I | re.S):
        attrs = parse_attrs(tag)
        key = attrs.get("name") or attrs.get("id")
        if key:
            fields[key] = attrs.get("value", "")
    return fields


def get_course_context(
    session: requests.Session, course: dict[str, str], timeout: float
) -> tuple[dict[str, str], str]:
    response = session.get(course["url"], timeout=timeout)
    response.raise_for_status()
    response.encoding = "utf-8"
    if "passport2.chaoxing.com/login" in response.url:
        raise RuntimeError("课程主页被重定向到登录页")

    fields = extract_hidden_fields(response.text)
    aliases = {
        "courseId": ("courseId", "courseid"),
        "classId": ("classId", "clazzid"),
        "cpi": ("cpi", "personId", "personid"),
        "userId": ("userId", "userid", "puid"),
        "bbsid": ("bbsid", "bbsId"),
        "fid": ("fid", "cfid", "courseBelongSchoolId"),
        "workEnc": ("workEnc", "workenc"),
        "openc": ("openc", "openenc"),
        "enc": ("enc", "stuenc"),
        "oldenc": ("oldenc", "oldEnc"),
        "t": ("t",),
        "showAIEnc": ("showAIEnc",),
        "showAiAssistantTea": ("showAiAssistantTea",),
        "learnSilverStartTime": ("learnSilverStartTime",),
        "learnSilverEndTime": ("learnSilverEndTime",),
    }
    context: dict[str, str] = dict(course)
    context["url"] = response.url
    for target, choices in aliases.items():
        value = next((fields[key] for key in choices if fields.get(key)), "")
        if value:
            context[target] = value

    # 页面中的 JS/URL 有时比隐藏 input 更完整。
    for target, pattern in {
        "cpi": r"(?:[?&]|[\"'])cpi=([0-9]+)",
        "userId": r"(?:userId|userid)\s*[:=]\s*[\"']?([0-9]+)",
        "workEnc": r"(?:workEnc|workenc)\s*[:=]\s*[\"']([^\"']+)",
    }.items():
        if not context.get(target):
            match = re.search(pattern, response.text, re.I)
            if match:
                context[target] = match.group(1)
    return context, response.text


def value_at(data: Any, *path: str) -> Any:
    current = data
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def data_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        nested = value.get("data")
        return nested if isinstance(nested, list) else []
    return []


def flatten_knowledge(nodes: Iterable[Any]) -> list[dict[str, Any]]:
    flattened: list[dict[str, Any]] = []
    for item in nodes:
        if not isinstance(item, dict):
            continue
        flattened.append(item)
        for key in ("children", "child", "knowledge"):
            children = data_list(item.get(key))
            if children:
                flattened.extend(flatten_knowledge(children))
    return flattened


def attachment_list(node: dict[str, Any]) -> list[dict[str, Any]]:
    attachments = data_list(node.get("attachment"))
    if not attachments and isinstance(node.get("attachment"), list):
        attachments = node["attachment"]
    return [item for item in attachments if isinstance(item, dict)]


def normalize_datetime(value: Any) -> str:
    if value is None or value is False:
        return ""
    if isinstance(value, dict):
        for key in ("time", "value", "date"):
            if key in value:
                return normalize_datetime(value[key])
        return ""
    raw = str(value).strip()
    if not raw or raw.lower() in {"0", "-1", "none", "null", "false", "未设置", "无"}:
        return ""

    if re.fullmatch(r"\d{10,13}", raw):
        timestamp = int(raw)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        try:
            return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
        except (OverflowError, OSError, ValueError):
            return ""

    match = re.search(
        r"(20\d{2})\s*[年/.-]\s*(\d{1,2})\s*[月/.-]\s*(\d{1,2})"
        r"(?:\s*[日号]?\s*[T ]?\s*(\d{1,2})\s*[:时]\s*(\d{1,2})"
        r"(?:\s*[:分]\s*(\d{1,2}))?)?",
        raw,
    )
    if not match:
        return ""
    year, month, day, hour, minute, second = match.groups()
    try:
        parsed = datetime(
            int(year),
            int(month),
            int(day),
            int(hour or 0),
            int(minute or 0),
            int(second or 0),
        )
    except ValueError:
        return ""
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def raw_time(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value).strip()


def extract_video_rows(
    session: requests.Session,
    context: dict[str, str],
    timeout: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    response = session.get(
        CHAPTER_API,
        params={
            "id": context["classId"],
            "personid": context["courseId"],
            "fields": CHAPTER_FIELDS,
            "view": "json",
        },
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()

    clazzes = data_list(payload)
    if not clazzes and isinstance(payload, dict):
        clazzes = data_list(payload.get("data"))
    clazz = clazzes[0] if clazzes else {}
    courses = data_list(value_at(clazz, "course"))
    course_data = courses[0] if courses else {}
    nodes = flatten_knowledge(data_list(course_data.get("knowledge")))

    rows: list[dict[str, Any]] = []
    for node in nodes:
        attachments = attachment_list(node)
        video_attachments = [
            item
            for item in attachments
            if str(item.get("type", "")).lower() == "video"
            or item.get("objectid")
            or item.get("objectId")
        ]
        if not video_attachments:
            continue
        chapter_id = raw_time(node.get("id"))
        begin_raw = node.get("begintime") or node.get("beginTime") or ""
        end_raw = node.get("endtime") or node.get("endTime") or ""
        chapter_url = (
            "https://mooc1.chaoxing.com/mycourse/studentstudy?"
            + urlencode(
                {
                    "chapterId": chapter_id,
                    "courseId": context["courseId"],
                    "clazzid": context["classId"],
                    "cpi": context.get("cpi", ""),
                    "mooc2": 1,
                }
            )
        )
        rows.append(
            {
                "type": "视频",
                "name": raw_time(node.get("name") or node.get("label") or f"章节 {chapter_id}"),
                "status": raw_time(node.get("status")),
                "start": normalize_datetime(begin_raw),
                "deadline": normalize_datetime(end_raw),
                "deadline_raw": raw_time(end_raw),
                "item_id": chapter_id,
                "video_count": len(video_attachments),
                "url": chapter_url,
                "note": "章节未提供截止时间" if not normalize_datetime(end_raw) else "",
            }
        )
    return rows, payload


def extract_chapter_candidates(page: str, base_url: str) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    # 当前版页面把叶子章节放在 chapter_item div 中，而不是 studentstudy 链接中。
    leaf_items: list[tuple[re.Match[str], dict[str, str], str]] = []
    for match in re.finditer(
        r"<div\b[^>]*class=[\"'][^\"']*\bchapter_item\b[^\"']*[\"'][^>]*>",
        page,
        re.I | re.S,
    ):
        attrs = parse_attrs(match.group(0))
        id_match = re.fullmatch(r"cur(\d+)", attrs.get("id", ""), re.I)
        if not id_match:
            onclick = html.unescape(attrs.get("onclick", ""))
            id_match = re.search(
                r"\btoOld\s*\(\s*['\"]\d+['\"]\s*,\s*['\"](\d+)", onclick, re.I
            )
        if id_match:
            leaf_items.append((match, attrs, id_match.group(1)))

    for index, (match, attrs, chapter_id) in enumerate(leaf_items):
        block_end = (
            leaf_items[index + 1][0].start()
            if index + 1 < len(leaf_items)
            else min(len(page), match.end() + 12000)
        )
        fragment = page[match.start():block_end]
        name = html.unescape(attrs.get("title", "")).strip()
        if not name:
            name_match = re.search(
                r"<div\b[^>]*class=[\"'][^\"']*\bcatalog_name\b[^\"']*[\"'][^>]*>(.*?)</div>",
                fragment,
                re.I | re.S,
            )
            if name_match:
                name = clean_text(name_match.group(1))
        name = re.sub(r"\s+", " ", name).strip()

        status_match = re.search(
            r"<span\b[^>]*class=[\"'][^\"']*\bbntHoverTips\b[^\"']*[\"'][^>]*>(.*?)</span>",
            fragment,
            re.I | re.S,
        )
        status = clean_text(status_match.group(1)) if status_match else ""
        candidates.append(
            {
                "id": chapter_id,
                "name": name or f"章节 {chapter_id}",
                "status": status,
                "url": "",
                "fragment": fragment,
            }
        )

    deduped: list[dict[str, str]] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate["id"] not in seen:
            seen.add(candidate["id"])
            deduped.append(candidate)
    return deduped


def chapter_contains_video(
    session: requests.Session,
    context: dict[str, str],
    chapter: dict[str, str],
    timeout: float,
) -> tuple[bool, int, str]:
    fragment = chapter.get("fragment", "")
    direct_count = len(re.findall(r"(?:视频|[\"']type[\"']\s*:\s*[\"']video[\"'])", fragment, re.I))
    if direct_count:
        return True, direct_count, fragment

    params = {
        "courseId": context["courseId"],
        "clazzid": context["classId"],
        "chapterId": chapter["id"],
        "cpi": context.get("cpi", ""),
        "verificationcode": "",
        "mooc2": 1,
        "toComputer": "false",
        "microTopicId": 0,
        "editorPreview": 0,
        "isPreviewVideo": "false",
        "cardIndex": 0,
    }
    endpoints = [
        "https://mooc1.chaoxing.com/mooc-ans/mycourse/studentstudyAjax",
        "https://mooc1-2.chaoxing.com/mooc-ans/mycourse/studentstudyAjax",
    ]
    for endpoint in endpoints:
        try:
            response = session.get(
                endpoint,
                params=params,
                headers={"Referer": chapter.get("url") or context["url"]},
                timeout=timeout,
            )
            if response.status_code >= 400:
                continue
            response.encoding = "utf-8"
            body = response.text
            count = len(
                re.findall(
                    r"[\"']type[\"']\s*:\s*[\"']video[\"']|\btype\s*:\s*[\"']video[\"']",
                    body,
                    re.I,
                )
            )
            if count:
                return True, count, body
        except requests.RequestException:
            continue
    return False, 0, fragment


def extract_video_rows_from_chapter_page(
    session: requests.Session,
    context: dict[str, str],
    timeout: float,
    debug_dir: Path | None = None,
) -> tuple[list[dict[str, Any]], str]:
    params = {
        "courseid": context["courseId"],
        "courseId": context["courseId"],
        "clazzid": context["classId"],
        "cpi": context.get("cpi", ""),
        "ut": "s",
        "t": context.get("t") or int(time.time() * 1000),
        "stuenc": context.get("enc", ""),
        "enc": context.get("enc", ""),
        "openc": context.get("openc", ""),
        "fromMiddle": 1,
    }
    urls = (
        "https://mooc1.chaoxing.com/mycourse/studentcourse",
        "https://mooc2-ans.chaoxing.com/mooc2-ans/mycourse/studentcourse",
        "https://mooc2-ans.chaoxing.com/mycourse/studentcourse",
    )
    last_error = ""
    for url in urls:
        try:
            response = session.get(
                url,
                params=params,
                headers={"Referer": context["url"]},
                timeout=timeout,
            )
            response.raise_for_status()
            response.encoding = "utf-8"
            if debug_dir is not None:
                debug_dir.mkdir(parents=True, exist_ok=True)
                (debug_dir / "studentcourse.html").write_text(
                    response.text, encoding="utf-8"
                )
            if "passport2.chaoxing.com/login" in response.url:
                last_error = "章节页被重定向到登录页"
                continue
            chapters = extract_chapter_candidates(response.text, response.url)
            if not chapters:
                last_error = "章节页在线，但未解析到章节链接"
                continue

            rows: list[dict[str, Any]] = []
            for chapter in chapters:
                start_raw, start, deadline_raw, deadline = extract_start_and_deadline(
                    chapter["fragment"]
                )
                inaccessible_timed_chapter = bool(start or deadline) and any(
                    marker in chapter.get("status", "")
                    for marker in ("暂未开放", "不能查看", "已关闭")
                )
                if inaccessible_timed_chapter:
                    # 页面已经给出开放窗口，但在窗口外无法读取附件类型。
                    is_video, video_count, detail = False, 0, chapter["fragment"]
                else:
                    is_video, video_count, detail = chapter_contains_video(
                        session, context, chapter, timeout
                    )
                    if not start and not deadline:
                        start_raw, start, deadline_raw, deadline = extract_start_and_deadline(
                            chapter["fragment"] + " " + detail
                        )
                # 有明确开放窗口的章节即使当前无法确认附件类型，也必须保留，
                # 否则最重要的截止日期反而会被过滤掉。
                if not is_video and not (start or deadline):
                    continue
                chapter_url = chapter["url"] or (
                    "https://mooc1.chaoxing.com/mycourse/studentstudy?"
                    + urlencode(
                        {
                            "chapterId": chapter["id"],
                            "courseId": context["courseId"],
                            "clazzid": context["classId"],
                            "cpi": context.get("cpi", ""),
                            "mooc2": 1,
                        }
                    )
                )
                rows.append(
                    {
                        "type": "视频" if is_video else "限时章节",
                        "name": chapter["name"],
                        "status": chapter.get("status", ""),
                        "start": start,
                        "deadline": deadline,
                        "deadline_raw": deadline_raw,
                        "item_id": chapter["id"],
                        "video_count": video_count,
                        "url": chapter_url,
                        "note": (
                            "章节未提供截止时间"
                            if not deadline
                            else "页面给出了开放时间；当前不可查看，无法确认是否含视频"
                            if not is_video
                            else ""
                        ),
                    }
                )
            return rows, response.text
        except requests.RequestException as exc:
            last_error = str(exc)
    raise RuntimeError(last_error or "无法访问章节页")


def first_datetime_for_keys(data: Any, keys: set[str]) -> tuple[str, str]:
    """在 JSON 中按层级查找第一个可识别的日期字段。"""
    queue = [data]
    lowered = {key.lower() for key in keys}
    while queue:
        current = queue.pop(0)
        if isinstance(current, dict):
            for key, value in current.items():
                if key.lower() in lowered:
                    normalized = normalize_datetime(value)
                    if normalized:
                        return raw_time(value), normalized
            queue.extend(current.values())
        elif isinstance(current, list):
            queue.extend(current)
    return "", ""


def extract_course_schedule(
    session: requests.Session,
    context: dict[str, str],
    chapter_payload: dict[str, Any],
    timeout: float,
) -> dict[str, str]:
    clazzes = data_list(chapter_payload)
    clazz = clazzes[0] if clazzes and isinstance(clazzes[0], dict) else {}

    start_raw, start = first_datetime_for_keys(
        clazz,
        {"begindate", "beginDate", "startTime", "courseStartTime", "openTime"},
    )
    # gas/clazz 不一定返回课程结束时间，再用只读课程设置接口补齐。
    setting_payload: Any = {}
    try:
        response = session.get(
            COURSE_SETTING_API,
            params={
                "clazzId": context["classId"],
                "courseId": context["courseId"],
                "cpi": context.get("cpi", ""),
            },
            timeout=timeout,
        )
        response.raise_for_status()
        setting_payload = response.json()
    except (requests.RequestException, ValueError):
        setting_payload = {}

    if not start:
        start_raw, start = first_datetime_for_keys(
            setting_payload,
            {"begindate", "beginDate", "startTime", "courseStartTime", "openTime"},
        )
    end_raw, end = first_datetime_for_keys(
        setting_payload,
        {
            "enddate",
            "endDate",
            "endTime",
            "courseEndTime",
            "closeTime",
            "deadline",
        },
    )
    if not end:
        # 仅检查班级顶层，不扫描 knowledge，避免把第一章的截止时间当成课程截止时间。
        end_raw, end = first_datetime_for_keys(
            {key: value for key, value in clazz.items() if key not in {"course", "knowledge"}},
            {"enddate", "endDate", "endTime", "courseEndTime", "closeTime", "deadline"},
        )
    return {
        "start": start,
        "start_raw": start_raw,
        "deadline": end,
        "deadline_raw": end_raw,
    }


def absolute_https_url(value: str, base_url: str) -> str:
    url = urljoin(base_url, html.unescape(value))
    parsed = urlparse(url)
    if parsed.scheme == "http":
        parsed = parsed._replace(scheme="https")
    return urlunparse(parsed)


def extract_labeled_time(fragment: str, label: str) -> tuple[str, str]:
    plain = clean_text(fragment)
    patterns = [
        rf"{label}\s*[：:]?\s*((?:20\d{{2}}|1\d{{9,12}})[^\n;；<]{{0,30}})",
        rf"(?:{label.lower()}|{label})\s*[=:]\s*[\"']?([^\"'&,}}<]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, plain, re.I)
        if match:
            candidate = match.group(1).strip(" \t,，。;；'")
            normalized = normalize_datetime(candidate)
            if normalized:
                return candidate, normalized

    keys = (
        "endTime",
        "endtime",
        "endDate",
        "deadline",
        "deadLine",
        "expireTime",
    )
    for key in keys:
        patterns = (
            rf"[\"']?{key}[\"']?\s*[:=]\s*[\"']([^\"']+)[\"']",
            rf"[\"']?{key}[\"']?\s*[:=]\s*(\d{{10,13}})",
            rf"(?:name|id)=[\"']{key}[\"'][^>]*value=[\"']([^\"']+)[\"']",
            rf"value=[\"']([^\"']+)[\"'][^>]*(?:name|id)=[\"']{key}[\"']",
        )
        for pattern in patterns:
            match = re.search(pattern, fragment, re.I | re.S)
            if match:
                candidate = html.unescape(match.group(1)).strip()
                normalized = normalize_datetime(candidate)
                if normalized:
                    return candidate, normalized
    return "", ""


def extract_all_datetimes(fragment: str) -> list[tuple[str, str]]:
    """按出现顺序提取页面中的日期，返回（原文、标准时间）。"""
    plain = clean_text(fragment)
    patterns = (
        r"20\d{2}\s*[年/.-]\s*\d{1,2}\s*[月/.-]\s*\d{1,2}"
        r"(?:\s*[日号]?\s*[T ]?\s*\d{1,2}\s*[:时]\s*\d{1,2}(?:\s*[:分]\s*\d{1,2})?)?",
        r"(?<!\d)1\d{9,12}(?!\d)",
    )
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for pattern in patterns:
        for match in re.finditer(pattern, plain):
            raw = match.group(0).strip()
            normalized = normalize_datetime(raw)
            if normalized and normalized not in seen:
                seen.add(normalized)
                found.append((raw, normalized))
    return found


def extract_start_and_deadline(fragment: str) -> tuple[str, str, str, str]:
    start_raw = start = deadline_raw = deadline = ""
    for label in ("开始时间", "开放时间", "发布时间", "作业开始时间"):
        start_raw, start = extract_labeled_time(fragment, label)
        if start:
            break
    for label in (
        "截止时间",
        "结束时间",
        "提交截止时间",
        "作业结束时间",
        "截止日期",
        "有效期至",
    ):
        deadline_raw, deadline = extract_labeled_time(fragment, label)
        if deadline:
            break

    dates = extract_all_datetimes(fragment)
    if not start and len(dates) >= 2:
        start_raw, start = dates[0]
    if not deadline:
        if len(dates) >= 2:
            deadline_raw, deadline = dates[-1]
        elif len(dates) == 1:
            # 作业列表通常只展示截止时间；单个日期按截止时间处理。
            deadline_raw, deadline = dates[0]
    return start_raw, start, deadline_raw, deadline


def detect_status(label: str) -> str:
    match = re.search(
        r"超时|逾期|已过期|已截止|已结束|已关闭|已完成|待批阅|未批阅|已提交|未交|未做|未完成",
        label,
    )
    return match.group(0) if match else ""


def enrich_work_rows_from_legacy_page(
    session: requests.Session,
    context: dict[str, str],
    rows: list[dict[str, Any]],
    timeout: float,
    debug_dir: Path | None,
) -> None:
    """新版简洁列表不含日期时，从当前前端保留的旧版作业元数据页补齐。"""
    if not rows or not any(not row.get("deadline") for row in rows):
        return
    enc = context.get("oldenc") or context.get("enc")
    if not enc:
        return
    try:
        response = session.get(
            "https://mooc1.chaoxing.com/work/getAllWork",
            params={
                "courseId": context["courseId"],
                "classId": context["classId"],
                "isdisplaytable": 2,
                "ut": "s",
                "cpi": context.get("cpi", ""),
                "openc": context.get("openc", ""),
                "enc": enc,
            },
            headers={"Referer": context["url"]},
            timeout=timeout,
        )
        response.raise_for_status()
        response.encoding = "utf-8"
    except requests.RequestException:
        return
    if debug_dir is not None:
        debug_dir.mkdir(parents=True, exist_ok=True)
        (debug_dir / "work_legacy.html").write_text(response.text, encoding="utf-8")

    source = response.text
    for row in rows:
        if row.get("deadline"):
            continue
        needles = [row.get("item_id", ""), row.get("name", "")]
        best: tuple[int, str, str, str, str] | None = None
        for needle in needles:
            if not needle:
                continue
            for match in re.finditer(re.escape(str(needle)), source, re.I):
                fragment = source[max(0, match.start() - 2500):min(len(source), match.end() + 5000)]
                start_raw, start, deadline_raw, deadline = extract_start_and_deadline(fragment)
                score = int(bool(start)) + 2 * int(bool(deadline))
                if deadline and (best is None or score > best[0]):
                    best = (score, start_raw, start, deadline_raw, deadline)
        if best:
            _, start_raw, start, deadline_raw, deadline = best
            row["start_raw"] = start_raw
            row["start"] = start
            row["deadline_raw"] = deadline_raw
            row["deadline"] = deadline
            row["note"] = "时间来自旧版作业元数据页"


def extract_work_rows(
    session: requests.Session,
    context: dict[str, str],
    course_page: str,
    timeout: float,
    detail_fallback: bool,
    debug_dir: Path | None = None,
) -> tuple[list[dict[str, Any]], str]:
    work_url_match = re.search(
        r"title=[\"']作业[\"'][^>]*data-url=[\"']([^\"']+)[\"']",
        course_page,
        re.I,
    ) or re.search(r"data-url=[\"']([^\"']*/work/list[^\"']*)[\"']", course_page, re.I)
    work_enc = context.get("workEnc", "")
    if not work_url_match or not work_enc:
        return [], "课程主页未提供作业入口或 workEnc"

    work_url = absolute_https_url(work_url_match.group(1), context["url"])
    response = session.get(
        work_url,
        params={
            "courseId": context["courseId"],
            "classId": context["classId"],
            "enc": work_enc,
        },
        headers={"Referer": context["url"], "X-Requested-With": "XMLHttpRequest"},
        timeout=timeout,
    )
    response.raise_for_status()
    response.encoding = "utf-8"
    if debug_dir is not None:
        debug_dir.mkdir(parents=True, exist_ok=True)
        (debug_dir / "work_list.html").write_text(response.text, encoding="utf-8")

    rows: list[dict[str, Any]] = []
    # 作业条目内部可能还有嵌套 <li>，不能用第一个 </li> 作为条目结尾。
    # 以带 data/aria-label 的下一个顶层候选开头切块，保留完整时间区域。
    openings = list(
        re.finditer(
            r"<li\b(?=[^>]*(?:data|aria-label)\s*=)(?P<tag>[^>]*)>",
            response.text,
            re.I | re.S,
        )
    )
    for position, match in enumerate(openings):
        block_end = openings[position + 1].start() if position + 1 < len(openings) else len(response.text)
        block = response.text[match.start():block_end]
        tag = "<li " + match.group("tag") + ">"
        attrs = parse_attrs(tag)
        detail_raw = attrs.get("data", "")
        aria = attrs.get("aria-label", "")
        searchable = f"{aria} {detail_raw} {block}"
        work_id_match = re.search(r"(?:workId|workid)=(\d+)", detail_raw)
        if not work_id_match and not re.search(r"作业|未交|已完成|待批阅", searchable):
            continue

        parts = [part.strip() for part in aria.replace("&nbsp;", " ").split(";") if part.strip()]
        name = parts[0] if parts else ""
        status = detect_status(aria)
        if not status and len(parts) > 1:
            status = parts[1]
        body_text = clean_text(block)
        if not name:
            name_match = re.search(r"(?:title|aria-label)=[\"']([^\"']+)[\"']", block, re.I)
            name = html.unescape(name_match.group(1)).strip() if name_match else body_text[:100]

        start_raw, start, deadline_raw, deadline = extract_start_and_deadline(searchable)
        detail_url = absolute_https_url(detail_raw, response.url) if detail_raw else ""

        # 某些版本只在详情页展示截止时间。这里只做 GET，不提交表单。
        if detail_fallback and detail_url and not deadline:
            try:
                detail_response = session.get(
                    detail_url,
                    headers={"Referer": response.url},
                    timeout=timeout,
                )
                detail_response.raise_for_status()
                detail_response.encoding = "utf-8"
                if debug_dir is not None and work_id_match:
                    (debug_dir / f"work_{work_id_match.group(1)}.html").write_text(
                        detail_response.text, encoding="utf-8"
                    )
                detail_start_raw, detail_start, detail_deadline_raw, detail_deadline = (
                    extract_start_and_deadline(detail_response.text)
                )
                if detail_deadline:
                    deadline_raw, deadline = detail_deadline_raw, detail_deadline
                if not start and detail_start:
                    start_raw, start = detail_start_raw, detail_start
            except requests.RequestException:
                pass

        rows.append(
            {
                "type": "作业",
                "name": re.sub(r"\s+", " ", name).strip(),
                "status": status,
                "start": start,
                "start_raw": start_raw,
                "deadline": deadline,
                "deadline_raw": deadline_raw,
                "item_id": work_id_match.group(1) if work_id_match else "",
                "video_count": "",
                "url": detail_url,
                "note": "页面未提供截止时间" if not deadline else "",
            }
        )
    enrich_work_rows_from_legacy_page(
        session, context, rows, timeout, debug_dir
    )
    return rows, "" if rows else "作业页面未解析到条目"


def extract_discussion_rows(
    session: requests.Session,
    context: dict[str, str],
    timeout: float,
    debug_dir: Path | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """读取课程讨论区；讨论通常只有发布时间，不一定设置截止时间。"""
    if not context.get("bbsid"):
        return [], ""
    response = session.get(
        DISCUSSION_API,
        params={
            # 与当前 course-stu.js 为“讨论”导航拼出的参数保持一致。
            "courseid": context["courseId"],
            "clazzid": context["classId"],
            "bbsid": context["bbsid"],
            "cpi": context.get("cpi", ""),
            "ut": "s",
            "t": context.get("t", ""),
            "stuenc": context.get("enc", ""),
            "enc": context.get("enc", ""),
            "learnSilverStartTime": context.get("learnSilverStartTime", ""),
            "learnSilverEndTime": context.get("learnSilverEndTime", ""),
            "showAIEnc": context.get("showAIEnc", ""),
            "supportAI": context.get("showAiAssistantTea", "false"),
        },
        headers={"Referer": context["url"]},
        timeout=timeout,
    )
    response.raise_for_status()
    response.encoding = "utf-8"
    if debug_dir is not None:
        debug_dir.mkdir(parents=True, exist_ok=True)
        (debug_dir / "discussion_list.html").write_text(response.text, encoding="utf-8")
    if "passport2.chaoxing.com/login" in response.url:
        raise RuntimeError("讨论区被重定向到登录页")
    if "非法请求" in response.text:
        raise RuntimeError(f"讨论区返回提示页：{clean_text(response.text)}")

    # 当前讨论页只输出空容器，主题由 discuss.list.js 再通过 JSON 接口加载。
    api_root = f"https://groupweb.chaoxing.com/course/topic/{context['bbsid']}"
    # 学生查看具体班级时，前端必须在 POST body 中带三段 tags；缺失时
    # 服务端会返回“你没有权限操作”，即使讨论主页本身可以正常打开。
    query_data = {
        "tags": (
            f"classId0000001,classId{context['classId']},"
            f"courseId{context['courseId']}"
        )
    }
    topic_payloads: list[dict[str, Any]] = []
    top_response = session.post(
        f"{api_root}/getTopTopicList",
        params={
            "folder_uuid": "",
            "courseId": context["courseId"],
            "isSetTop": "",
            "learnSilverStartTime": context.get("learnSilverStartTime", ""),
            "learnSilverEndTime": context.get("learnSilverEndTime", ""),
        },
        data=query_data,
        headers={"Referer": response.url},
        timeout=timeout,
    )
    top_response.raise_for_status()
    try:
        top_payload = top_response.json()
    except requests.JSONDecodeError:
        top_payload = {}
    if isinstance(top_payload, dict) and top_payload.get("status"):
        topic_payloads.extend(
            item for item in top_payload.get("datas", []) if isinstance(item, dict)
        )

    last_reply_time = ""
    last_aux_value = ""
    normal_pages: list[dict[str, Any]] = []
    for page_number in range(1, 51):
        list_params = {
            "folder_uuid": "",
            "page": page_number,
            "pageSize": 20,
            "kw": "",
            "courseId": context["courseId"],
            "isSetTop": "",
            "selectedStartTime": "",
            "selectedEndTime": "",
            "selectedType": 0,
            "learnSilverStartTime": context.get("learnSilverStartTime", ""),
            "learnSilverEndTime": context.get("learnSilverEndTime", ""),
            "lastAuxValue": last_aux_value,
        }
        if last_reply_time:
            list_params["last_reply_time"] = last_reply_time
        list_response = session.post(
            f"{api_root}/getTopicList",
            params=list_params,
            data=query_data,
            headers={"Referer": response.url},
            timeout=timeout,
        )
        list_response.raise_for_status()
        try:
            payload = list_response.json()
        except requests.JSONDecodeError:
            break
        if not isinstance(payload, dict) or not payload.get("status"):
            break
        normal_pages.append(payload)
        topics = [item for item in payload.get("datas", []) if isinstance(item, dict)]
        topic_payloads.extend(topics)
        poff = payload.get("poff") if isinstance(payload.get("poff"), dict) else {}
        if not topics or poff.get("lastPage") in (1, "1", True):
            break
        new_last_reply_time = raw_time(poff.get("lastValue"))
        new_last_aux_value = raw_time(poff.get("lastAuxValue"))
        if (
            new_last_reply_time == last_reply_time
            and new_last_aux_value == last_aux_value
        ):
            break
        last_reply_time = new_last_reply_time
        last_aux_value = new_last_aux_value

    if debug_dir is not None:
        (debug_dir / "discussion_topics.json").write_text(
            json.dumps(
                {"top": top_payload, "pages": normal_pages},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for topic in topic_payloads:
        topic_id = raw_time(topic.get("id") or topic.get("uuid"))
        topic_uuid = raw_time(topic.get("uuid") or topic.get("id"))
        if not topic_id or topic_id in seen:
            continue
        seen.add(topic_id)
        name = clean_text(raw_time(topic.get("title")))
        if not name:
            name = clean_text(raw_time(topic.get("content")))[:160]
        start_raw = raw_time(topic.get("create_time") or topic.get("createTime"))
        start = normalize_datetime(start_raw)
        deadline_raw, deadline = first_datetime_for_keys(
            topic,
            {
                "replyDeadline",
                "reply_deadline",
                "deadline",
                "endTime",
                "end_time",
            },
        )
        status_parts: list[str] = []
        if topic.get("top") in (1, "1", True):
            status_parts.append("置顶")
        if topic.get("choice") in (1, "1", True):
            status_parts.append("加精")
        creator = raw_time(topic.get("createrName") or topic.get("creatorName"))
        note_parts = [f"发布人：{creator}"] if creator else []
        if not deadline:
            note_parts.append("讨论区未提供截止时间")
        rows.append(
            {
                "type": "讨论",
                "name": name or f"讨论 {topic_id}",
                "status": "、".join(status_parts),
                "start": start,
                "start_raw": start_raw,
                "deadline": deadline,
                "deadline_raw": deadline_raw,
                "item_id": topic_id,
                "video_count": "",
                "url": (
                    f"https://groupweb.chaoxing.com/course/topic/v3/bbs/"
                    f"{context['bbsid']}/{topic_uuid}/replysList?"
                    + urlencode(
                        {
                            "courseId": context["courseId"],
                            "classId": context["classId"],
                            "cpi": context.get("cpi", ""),
                        }
                    )
                ),
                "note": "；".join(note_parts),
            }
        )
    if rows:
        return rows, ""

    # 兼容仍将主题直接渲染进 HTML 的旧版页面。
    page = response.text
    anchors = list(
        re.finditer(
            r"<a\b[^>]*(?:topicId|topicDetail|topicdetail)[^>]*>.*?</a>",
            page,
            re.I | re.S,
        )
    )
    rows = []
    seen = set()
    for anchor_match in anchors:
        anchor = anchor_match.group(0)
        attrs = parse_attrs(anchor.split(">", 1)[0] + ">")
        combined = html.unescape(
            " ".join(
                (attrs.get("href", ""), attrs.get("onclick", ""), anchor)
            )
        )
        id_match = re.search(
            r"(?:topicId|topicid)[=:'\"\s,(]+([A-Za-z0-9_-]+)", combined, re.I
        )
        if not id_match:
            continue
        topic_id = id_match.group(1)
        if topic_id in seen:
            continue
        seen.add(topic_id)

        block_start = page.rfind("<li", max(0, anchor_match.start() - 12000), anchor_match.start())
        block_end = page.find("</li>", anchor_match.end(), min(len(page), anchor_match.end() + 12000))
        if block_start < 0 or block_end < 0:
            block_start = max(0, anchor_match.start() - 2500)
            block_end = min(len(page), anchor_match.end() + 5000)
        else:
            block_end += len("</li>")
        fragment = page[block_start:block_end]

        name = html.unescape(attrs.get("title", "")).strip() or clean_text(anchor)
        name = re.sub(r"\s+", " ", name).strip()
        raw_url = attrs.get("href", "")
        topic_url = (
            absolute_https_url(raw_url, response.url)
            if raw_url and not raw_url.lower().startswith("javascript")
            else response.url
        )

        start_raw = start = ""
        for label in ("发布时间", "发表时间", "创建时间", "开始时间"):
            start_raw, start = extract_labeled_time(fragment, label)
            if start:
                break
        dates = extract_all_datetimes(fragment)
        if not start and dates:
            start_raw, start = dates[0]

        deadline_raw = deadline = ""
        for label in ("截止时间", "结束时间", "有效期至", "截止日期"):
            deadline_raw, deadline = extract_labeled_time(fragment, label)
            if deadline:
                break
        rows.append(
            {
                "type": "讨论",
                "name": name or f"讨论 {topic_id}",
                "status": detect_status(clean_text(fragment)),
                "start": start,
                "start_raw": start_raw,
                "deadline": deadline,
                "deadline_raw": deadline_raw,
                "item_id": topic_id,
                "video_count": "",
                "url": topic_url,
                "note": "讨论区未提供截止时间" if not deadline else "",
            }
        )
    return rows, "" if rows else "讨论区页面在线，但未解析到讨论主题"


def output_row(course: dict[str, str], item: dict[str, Any]) -> dict[str, Any]:
    return {
        "课程名称": course["name"],
        "课程ID": course["courseId"],
        "班级ID": course["classId"],
        "任务类型": item.get("type", ""),
        "任务名称": item.get("name", ""),
        "状态": item.get("status", ""),
        "开始时间": item.get("start", ""),
        "截止时间": item.get("deadline", ""),
        "原始截止时间": item.get("deadline_raw", ""),
        "任务ID": item.get("item_id", ""),
        "视频数量": item.get("video_count", ""),
        "链接": item.get("url", ""),
        "备注": item.get("note", ""),
    }


def write_outputs(
    output_dir: Path,
    rows: list[dict[str, Any]],
    raw_result: dict[str, Any],
) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = output_dir / f"学习通课程截止日期_{stamp}.csv"
    json_path = output_dir / f"学习通课程截止日期_{stamp}.json"

    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    with json_path.open("w", encoding="utf-8") as handle:
        json.dump(raw_result, handle, ensure_ascii=False, indent=2)
    return csv_path, json_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default="outputs", help="输出目录，默认 outputs")
    parser.add_argument("--timeout", type=float, default=20.0, help="单请求超时秒数")
    parser.add_argument(
        "--no-detail-fallback",
        action="store_true",
        help="作业列表缺少日期时，不再读取作业详情页",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="保存课程页、章节页和作业页原始 HTML，便于适配定制页面",
    )
    args = parser.parse_args()

    phone = os.environ.get("CHAOXING_PHONE") or input("学习通手机号: ").strip()
    password = os.environ.get("CHAOXING_PASSWORD") or getpass.getpass("学习通密码: ")
    if not phone or not password:
        print("手机号和密码不能为空", file=sys.stderr)
        return 2

    session = create_session()
    print("正在登录...")
    try:
        uid = login(session, phone, password, args.timeout)
        print(f"登录成功，UID={uid or '未知'}")
        courses = get_courses(session, args.timeout)
    except (requests.RequestException, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(f"共找到 {len(courses)} 门课程")
    csv_rows: list[dict[str, Any]] = []
    raw_result: dict[str, Any] = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "course_count": len(courses),
        "courses": [],
    }
    debug_root = Path(args.output_dir) / "_debug" if args.debug else None

    for index, course in enumerate(courses, 1):
        print(f"[{index}/{len(courses)}] {course['name']}")
        course_result: dict[str, Any] = {
            "course": course,
            "videos": [],
            "homeworks": [],
            "discussions": [],
            "errors": [],
        }
        items: list[dict[str, Any]] = []
        try:
            context, course_page = get_course_context(session, course, args.timeout)
            course_debug_dir = debug_root / course["classId"] if debug_root else None
            if course_debug_dir is not None:
                course_debug_dir.mkdir(parents=True, exist_ok=True)
                (course_debug_dir / "course.html").write_text(course_page, encoding="utf-8")
            course_result["context"] = {
                key: value
                for key, value in context.items()
                if key not in {"workEnc", "openc", "enc", "oldenc"}
            }
        except (requests.RequestException, RuntimeError) as exc:
            message = f"课程主页: {exc}"
            course_result["errors"].append(message)
            csv_rows.append(output_row(course, {"type": "课程", "name": course["name"], "note": message}))
            raw_result["courses"].append(course_result)
            continue

        try:
            chapter_page = ""
            try:
                videos, chapter_payload = extract_video_rows(session, context, args.timeout)
                course_result["chapter_source"] = "gas/clazz JSON"
            except (requests.RequestException, ValueError, KeyError):
                videos, chapter_page = extract_video_rows_from_chapter_page(
                    session, context, args.timeout, course_debug_dir
                )
                chapter_payload = {}
                course_result["chapter_source"] = "studentcourse HTML fallback"
                if course_debug_dir is not None:
                    (course_debug_dir / "studentcourse.html").write_text(
                        chapter_page, encoding="utf-8"
                    )

            schedule = extract_course_schedule(session, context, chapter_payload, args.timeout)
            if not schedule.get("start") or not schedule.get("deadline"):
                html_start_raw, html_start, html_end_raw, html_end = extract_start_and_deadline(
                    course_page + " " + chapter_page
                )
                if not schedule.get("start") and html_start:
                    schedule["start"] = html_start
                    schedule["start_raw"] = html_start_raw
                if not schedule.get("deadline") and html_end:
                    schedule["deadline"] = html_end
                    schedule["deadline_raw"] = html_end_raw
            course_result["schedule"] = schedule
            for video in videos:
                if not video.get("deadline") and schedule.get("deadline"):
                    video["deadline"] = schedule["deadline"]
                    video["deadline_raw"] = schedule["deadline_raw"]
                    video["note"] = "章节未设置单独截止时间；使用课程截止时间"
            course_result["videos"] = videos
            course_result["chapter_api_received"] = bool(chapter_payload)
            items.extend(videos)
        except (requests.RequestException, RuntimeError, ValueError, KeyError) as exc:
            course_result["errors"].append(f"视频章节: {exc}")
            schedule = {"start": "", "deadline": "", "deadline_raw": ""}

        try:
            homeworks, homework_note = extract_work_rows(
                session,
                context,
                course_page,
                args.timeout,
                detail_fallback=not args.no_detail_fallback,
                debug_dir=course_debug_dir,
            )
            course_result["homeworks"] = homeworks
            if homework_note:
                course_result["errors"].append(homework_note)
            items.extend(homeworks)
        except (requests.RequestException, ValueError, KeyError) as exc:
            course_result["errors"].append(f"作业列表: {exc}")

        try:
            discussions, discussion_note = extract_discussion_rows(
                session, context, args.timeout, course_debug_dir
            )
            course_result["discussions"] = discussions
            if discussion_note:
                course_result["errors"].append(discussion_note)
            items.extend(discussions)
        except (requests.RequestException, RuntimeError, ValueError, KeyError) as exc:
            course_result["errors"].append(f"讨论区: {exc}")

        course_note = "；".join(course_result["errors"])
        if not items and not course_note:
            course_note = "未发现视频章节或作业"
        csv_rows.append(
            output_row(
                course,
                {
                    "type": "课程",
                    "name": course["name"],
                    "start": schedule.get("start", ""),
                    "deadline": schedule.get("deadline", ""),
                    "deadline_raw": schedule.get("deadline_raw", ""),
                    "url": course["url"],
                    "note": course_note,
                },
            )
        )
        csv_rows.extend(output_row(course, item) for item in items)
        raw_result["courses"].append(course_result)
        time.sleep(0.15)

    csv_path, json_path = write_outputs(Path(args.output_dir), csv_rows, raw_result)
    video_count = sum(row["任务类型"] == "视频" for row in csv_rows)
    timed_chapter_count = sum(row["任务类型"] == "限时章节" for row in csv_rows)
    homework_count = sum(row["任务类型"] == "作业" for row in csv_rows)
    discussion_count = sum(row["任务类型"] == "讨论" for row in csv_rows)
    task_types = {"视频", "限时章节", "作业", "讨论"}
    deadline_count = sum(
        bool(row["截止时间"]) for row in csv_rows if row["任务类型"] in task_types
    )
    print("\n导出完成")
    print(
        f"课程: {len(courses)}，视频章节: {video_count}，"
        f"限时章节: {timed_chapter_count}，作业: {homework_count}，"
        f"讨论: {discussion_count}"
    )
    print(
        "解析到截止时间: "
        f"{deadline_count}/"
        f"{video_count + timed_chapter_count + homework_count + discussion_count}"
    )
    print(f"CSV:  {csv_path.resolve()}")
    print(f"JSON: {json_path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
