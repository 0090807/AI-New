import os
import json
import smtplib
import ssl
import hashlib
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from urllib.parse import urlparse

import feedparser
import requests
from bs4 import BeautifulSoup
from dateutil import parser as dtparser

try:
    from zoneinfo import ZoneInfo  # py3.9+
except Exception:
    ZoneInfo = None

TOKYO_TZ = ZoneInfo("Asia/Tokyo") if ZoneInfo else None


def tokyo_now():
    if TOKYO_TZ:
        return datetime.now(TOKYO_TZ)
    return datetime.now(timezone(timedelta(hours=9)))


def normalize_url(u: str) -> str:
    try:
        p = urlparse(u)
        return f"{p.scheme}://{p.netloc}{p.path}".rstrip("/")
    except Exception:
        return u.strip()


def stable_id(title: str, link: str) -> str:
    s = (title.strip() + "|" + normalize_url(link)).encode("utf-8")
    return hashlib.sha256(s).hexdigest()[:16]


def parse_entry_time(entry) -> datetime | None:
    for key in ["published", "updated", "created", "pubDate"]:
        if key in entry and entry[key]:
            try:
                dt = dtparser.parse(entry[key])
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except Exception:
                continue
    return None


def load_sources(path="sources.json"):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)["feeds"]


def collect_items(feeds, start_dt_tokyo: datetime, end_dt_tokyo: datetime, max_per_feed=12):
    items = []
    seen = set()

    for feed in feeds:
        d = feedparser.parse(feed["url"])
        cnt = 0
        for e in d.entries:
            if cnt >= max_per_feed:
                break
            t = parse_entry_time(e)
            if not t:
                continue

            if TOKYO_TZ:
                t_tokyo = t.astimezone(TOKYO_TZ)
            else:
                t_tokyo = t.astimezone(timezone(timedelta(hours=9)))

            if not (start_dt_tokyo <= t_tokyo < end_dt_tokyo):
                continue

            title = getattr(e, "title", "").strip()
            link = getattr(e, "link", "").strip()
            if not title or not link:
                continue

            uid = stable_id(title, link)
            if uid in seen:
                continue
            seen.add(uid)

            items.append({
                "id": uid,
                "title": title,
                "link": link,
                "time_tokyo": t_tokyo,
                "feed": feed["name"],
                "tag": feed.get("tag", "新闻")
            })
            cnt += 1

    items.sort(key=lambda x: x["time_tokyo"], reverse=True)
    return items


def classify(item):
    title_lower = item["title"].lower()
    if "arxiv" in item["feed"].lower() or "arxiv" in item["link"].lower():
        return "研究突破"
    if item["tag"] == "官方动态":
        return "官方动态"
    if any(k in title_lower for k in ["paper", "benchmark", "dataset", "state-of-the-art", "sota"]):
        return "研究突破"
    if any(k in title_lower for k in ["release", "launch", "introducing", "updates", "api"]):
        return "官方动态"
    return item["tag"] if item["tag"] else "行业新闻"


def build_email(subject_date: str, items):
    groups = {"研究突破": [], "官方动态": [], "行业新闻": []}
    for it in items:
        g = classify(it)
        groups.setdefault(g, []).append(it)

    lines = []
    lines.append(f"AI 每日简报（{subject_date}，覆盖前一天 JST）")
    lines.append("")
    lines.append("说明：以下为前一天在权威来源发布的 AI/人工智能进展与新闻，按类别整理。")
    lines.append("")

    def fmt_item(it):
        t = it["time_tokyo"].strftime("%H:%M")
        return f"- [{t}] {it['title']}（{it['feed']}）\n  {it['link']}"

    for sec in ["研究突破", "官方动态", "行业新闻"]:
        sec_items = groups.get(sec, [])
        if not sec_items:
            continue
        lines.append(f"【{sec}】")
        for it in sec_items[:20]:
            lines.append(fmt_item(it))
        lines.append("")

    if not items:
        lines.append("今天未抓取到符合“前一天（JST）”窗口的条目。你可以在 sources.json 里增加更多 RSS 源。")

    return "\n".join(lines)


def send_email_qq(sender_email, sender_auth_code, to_email, subject, body):
    msg = MIMEMultipart()
    msg["From"] = sender_email
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    smtp_host = "smtp.qq.com"
    smtp_port = 465

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(smtp_host, smtp_port, context=context) as server:
        server.login(sender_email, sender_auth_code)
        server.sendmail(sender_email, [to_email], msg.as_string())


def main():
    now = tokyo_now()
    today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    yday0 = today0 - timedelta(days=1)

    feeds = load_sources("sources.json")
    items = collect_items(feeds, yday0, today0)

    subject_date = today0.strftime("%Y-%m-%d")
    subject = f"AI 每日简报 | {subject_date}（前一天）"
    body = build_email(subject_date, items)

    print(body)

    sender_email = os.environ["QQ_EMAIL"]
    auth_code = os.environ["QQ_SMTP_AUTH_CODE"]
    to_email = os.environ.get("TO_EMAIL", sender_email)
    send_email_qq(sender_email, auth_code, to_email, subject, body)


if __name__ == "__main__":
    main()
