import os
import json
import smtplib
import ssl
import hashlib
import re
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import urlparse, quote_plus

import feedparser
import requests
from bs4 import BeautifulSoup
from dateutil import parser as dtparser

import trafilatura

try:
    from zoneinfo import ZoneInfo  # py3.9+
except Exception:
    ZoneInfo = None

TOKYO_TZ = ZoneInfo("Asia/Tokyo") if ZoneInfo else timezone(timedelta(hours=9))
UA = "Mozilla/5.0 (compatible; AI-Daily-Digest/1.0; +https://github.com/)"

def tokyo_now():
    return datetime.now(TOKYO_TZ)

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
        return json.load(f)

def google_news_rss_url(query: str, hl="zh-CN", gl="CN", ceid="CN:zh-Hans"):
    # Google News RSS（关键词聚合，全网来源）
    q = quote_plus(query)
    return f"https://news.google.com/rss/search?q={q}&hl={hl}&gl={gl}&ceid={ceid}"

def fetch_url_text(url: str, timeout=15) -> str:
    try:
        r = requests.get(url, timeout=timeout, headers={"User-Agent": UA})
        r.raise_for_status()
        html = r.text
        # trafilatura 提取正文
        extracted = trafilatura.extract(html, include_comments=False, include_tables=False)
        if extracted:
            return extracted.strip()
        return ""
    except Exception:
        return ""

def clean_text(s: str) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s

def summarize(text: str, max_sentences=3):
    """
    不用外部大模型的本地“可读摘要”：
    - 取前面若干句作为“简要内容”
    - 取出现频率高的关键词句作为“要点”
    """
    text = clean_text(text)
    if not text:
        return ("（未能抓取到正文，可能有反爬或需要登录）", [])

    # 分句（中英文）
    parts = re.split(r"(?<=[。！？.!?])\s+", text)
    parts = [p.strip() for p in parts if len(p.strip()) >= 12]

    brief = " ".join(parts[:max_sentences]) if parts else text[:220]

    # 生成要点：选几句较短且信息密度高的句子
    bullets = []
    for p in parts:
        if 30 <= len(p) <= 120:
            bullets.append(p)
        if len(bullets) >= 3:
            break
    if not bullets:
        bullets = [text[:120] + "…"] if len(text) > 120 else [text]

    return (brief, bullets)

def collect_from_feed(feed_name, feed_url, tag, start_dt_tokyo, end_dt_tokyo, max_items=20):
    items = []
    d = feedparser.parse(feed_url)
    for e in d.entries[:max_items]:
        t = parse_entry_time(e)
        if not t:
            continue
        t_tokyo = t.astimezone(TOKYO_TZ)
        if not (start_dt_tokyo <= t_tokyo < end_dt_tokyo):
            continue

        title = getattr(e, "title", "").strip()
        link = getattr(e, "link", "").strip()
        if not title or not link:
            continue

        items.append({
            "id": stable_id(title, link),
            "title": title,
            "link": link,
            "time_tokyo": t_tokyo,
            "feed": feed_name,
            "tag": tag
        })
    return items

def build_email_html(subject_date: str, groups: dict):
    # 简洁的 HTML 模板，邮箱里直接可读
    def esc(x):
        return (x or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    sections_html = []
    for sec, items in groups.items():
        if not items:
            continue
        block = [f"<h2 style='margin:20px 0 10px'>{esc(sec)}</h2>"]
        for it in items:
            t = it["time_tokyo"].strftime("%H:%M")
            block.append(
                f"""
                <div style="padding:12px 14px;margin:12px 0;border:1px solid #e5e7eb;border-radius:10px;">
                  <div style="font-size:16px;font-weight:700;margin-bottom:6px;">
                    [{esc(t)}] {esc(it["title"])}
                  </div>
                  <div style="color:#6b7280;font-size:12px;margin-bottom:10px;">
                    来源：{esc(it["feed"])} ｜ 分类：{esc(it["tag"])}
                  </div>
                  <div style="font-size:14px;line-height:1.6;margin-bottom:10px;">
                    <b>简要内容：</b>{esc(it["brief"])}
                  </div>
                  <div style="font-size:14px;line-height:1.6;margin-bottom:10px;">
                    <b>要点：</b>
                    <ul style="margin:8px 0 0 18px;">
                      {''.join([f"<li>{esc(b)}</li>" for b in it["bullets"]])}
                    </ul>
                  </div>
                  <div style="font-size:13px;">
                    <a href="{esc(it["link"])}" target="_blank">原文链接（详细）</a>
                  </div>
                </div>
                """
            )
        sections_html.append("\n".join(block))

    html = f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,'Noto Sans','PingFang SC','Microsoft YaHei',sans-serif;color:#111827;">
      <h1 style="margin:0 0 10px;">AI 每日简报（{esc(subject_date)}，覆盖前一天 JST）</h1>
      <div style="color:#6b7280;font-size:13px;margin-bottom:18px;">
        说明：以下为前一天全网聚合（Google News RSS + 权威源 RSS）抓取的 AI/人工智能新闻与进展，已在邮件中直接整理为可阅读摘要；每条附原文链接。
      </div>
      {''.join(sections_html) if sections_html else "<div>今天未抓取到符合“前一天（JST）”窗口的条目，你可以在 sources.json 增加关键词或来源。</div>"}
      <hr style="margin:24px 0;border:none;border-top:1px solid #e5e7eb;">
      <div style="color:#9ca3af;font-size:12px;">
        自动生成于 {esc(tokyo_now().strftime("%Y-%m-%d %H:%M JST"))}
      </div>
    </div>
    """
    return html

def send_email_qq(sender_email, sender_auth_code, to_email, subject, html_body):
    msg = MIMEMultipart("alternative")
    msg["From"] = sender_email
    msg["To"] = to_email
    msg["Subject"] = subject

    msg.attach(MIMEText(html_body, "html", "utf-8"))

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

    config = load_sources("sources.json")
    feeds = config.get("feeds", [])
    queries = config.get("google_news_queries", [])

    items = []
    seen = set()

    # 1) Google News 全网聚合（按关键词）
    for q in queries:
        url = google_news_rss_url(q["q"])
        got = collect_from_feed(q["name"], url, q.get("tag", "行业新闻"), yday0, today0, max_items=25)
        for it in got:
            if it["id"] in seen:
                continue
            seen.add(it["id"])
            items.append(it)

    # 2) 权威源 RSS
    for f in feeds:
        got = collect_from_feed(f["name"], f["url"], f.get("tag", "新闻"), yday0, today0, max_items=25)
        for it in got:
            if it["id"] in seen:
                continue
            seen.add(it["id"])
            items.append(it)

    # 按时间排序（最新在前）
    items.sort(key=lambda x: x["time_tokyo"], reverse=True)

    # 抓取正文 + 生成摘要（控制总量，避免邮件太长）
    MAX_TOTAL = int(os.environ.get("MAX_ITEMS", "18"))
    items = items[:MAX_TOTAL]

    for it in items:
        text = fetch_url_text(it["link"])
        brief, bullets = summarize(text, max_sentences=3)
        it["brief"] = brief
        it["bullets"] = bullets

    # 分类分组
    groups = {
        "研究突破": [],
        "官方动态": [],
        "行业新闻": [],
        "产业动态": [],
        "政策": []
    }
    for it in items:
        groups.setdefault(it["tag"], []).append(it)

    subject_date = today0.strftime("%Y-%m-%d")
    subject = f"AI 每日简报 | {subject_date}（前一天）"
    html_body = build_email_html(subject_date, groups)

    sender_email = os.environ["QQ_EMAIL"]
    auth_code = os.environ["QQ_SMTP_AUTH_CODE"]
    to_email = os.environ.get("TO_EMAIL", sender_email)

    send_email_qq(sender_email, auth_code, to_email, subject, html_body)

if __name__ == "__main__":
    main()
