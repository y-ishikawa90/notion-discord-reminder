"""
Googleカレンダー → Notion 同期スクリプト

機能:
  1. サービスアカウントでアクセスできる全カレンダーから、今週分の予定を取得
     （メイン+共有+サブ、終日予定も含む）
  2. 既存のNotionタスクDBに「予定」種別で新規行として追加
     （GCalイベントIDで重複防止）
  3. NOTION_DASHBOARD_PAGE_ID が設定されている場合、そのページ内の
     見出し「📅 今週の予定（自動更新）」より下を、毎時最新の週次スケジュールで
     上書きする（見出しより上の内容には触らない）

必要な環境変数 (GitHub Secrets):
  - NOTION_TOKEN
  - NOTION_DATABASE_ID                 (既存のタスクDB)
  - GOOGLE_SERVICE_ACCOUNT_JSON        (Service AccountのJSONを文字列で)
  - GOOGLE_CALENDAR_IDS                (任意。カンマ区切りで対象カレンダーIDを指定。
                                         未指定の場合はSAのcalendarListから取得)
  - NOTION_DASHBOARD_PAGE_ID           (任意。週次スケジュールを差し込むページ)

Notion DB に必要なプロパティ (事前に追加が必要):
  - タスク名      : title           (既存)
  - ステータス    : status          (既存)
  - タスク種別    : select          (「予定」を新規追加)
  - 期日          : date            (既存)
  - GCalイベントID: rich_text       (新規。重複防止用キー)
  - カレンダー名  : rich_text       (新規。任意)
"""

import os
import json
import datetime
import urllib.request
import urllib.error

from google.oauth2 import service_account
from googleapiclient.discovery import build

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------
NOTION_TOKEN = os.environ["NOTION_TOKEN"]
NOTION_DATABASE_ID = os.environ["NOTION_DATABASE_ID"]
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
NOTION_DASHBOARD_PAGE_ID = os.environ.get("NOTION_DASHBOARD_PAGE_ID", "").strip()
GOOGLE_CALENDAR_IDS = os.environ.get("GOOGLE_CALENDAR_IDS", "").strip()
WEEKLY_SECTION_HEADING = "📅 今週の予定（自動更新）"

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Content-Type": "application/json",
    "Notion-Version": "2022-06-28",
}

JST = datetime.timezone(datetime.timedelta(hours=9))


# ---------------------------------------------------------------------------
# 共通: HTTP
# ---------------------------------------------------------------------------
def http_request(url, method="GET", payload=None, headers=None):
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers=headers or {}, method=method
    )
    try:
        with urllib.request.urlopen(req) as res:
            body = res.read()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="ignore")
        raise RuntimeError(
            f"HTTP {e.code} {method} {url}\n{err_body}"
        ) from e


# ---------------------------------------------------------------------------
# Google Calendar
# ---------------------------------------------------------------------------
def build_calendar_service():
    sa_info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    creds = service_account.Credentials.from_service_account_info(
        sa_info,
        scopes=["https://www.googleapis.com/auth/calendar.readonly"],
    )
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def week_range_jst(now=None):
    """今週(月〜日)の開始/終了 datetime(JST) を返す。"""
    now = now or datetime.datetime.now(JST)
    today = now.date()
    monday = today - datetime.timedelta(days=today.weekday())
    sunday = monday + datetime.timedelta(days=6)
    start = datetime.datetime.combine(monday, datetime.time(0, 0), tzinfo=JST)
    end = datetime.datetime.combine(sunday, datetime.time(23, 59, 59), tzinfo=JST)
    return start, end


def fetch_week_events(service):
    """全カレンダーから今週分のイベントを取得して flat に返す。"""
    start, end = week_range_jst()
    time_min = start.isoformat()
    time_max = end.isoformat()

    calendars = []
    if GOOGLE_CALENDAR_IDS:
        # 明示指定されたIDを使う（推奨ルート）
        for cal_id in [c.strip() for c in GOOGLE_CALENDAR_IDS.split(",") if c.strip()]:
            try:
                meta = service.calendars().get(calendarId=cal_id).execute()
                calendars.append({
                    "id": cal_id,
                    "summary": meta.get("summary", cal_id),
                })
            except Exception as e:
                print(f"  [警告] カレンダー情報取得失敗（IDだけで続行）: {cal_id} ({e})")
                calendars.append({"id": cal_id, "summary": cal_id})
    else:
        # フォールバック: SAのcalendarListから取得（共有カレンダーは載らない場合あり）
        calendars = service.calendarList().list().execute().get("items", [])
    print(f"  対象カレンダー数: {len(calendars)}")

    all_events = []
    for cal in calendars:
        cal_id = cal["id"]
        cal_name = cal.get("summaryOverride") or cal.get("summary") or cal_id
        try:
            events = (
                service.events()
                .list(
                    calendarId=cal_id,
                    timeMin=time_min,
                    timeMax=time_max,
                    singleEvents=True,
                    orderBy="startTime",
                    maxResults=250,
                )
                .execute()
                .get("items", [])
            )
        except Exception as e:
            print(f"  [警告] カレンダー取得失敗: {cal_name} ({e})")
            continue

        for ev in events:
            if ev.get("status") == "cancelled":
                continue
            ev["_calendar_name"] = cal_name
            all_events.append(ev)

    print(f"  取得イベント総数: {len(all_events)}件")
    return all_events


def event_start_date_iso(ev):
    """イベントの開始日(YYYY-MM-DD)を返す。"""
    start = ev.get("start", {})
    if "date" in start:               # 終日
        return start["date"]
    if "dateTime" in start:
        # ISO8601 → JSTに変換して日付だけ取り出す
        dt = datetime.datetime.fromisoformat(start["dateTime"].replace("Z", "+00:00"))
        return dt.astimezone(JST).date().isoformat()
    return None


def event_time_label(ev):
    """イベントの時刻表示用ラベル ('終日' or '14:00-15:00') を返す。"""
    start = ev.get("start", {})
    end = ev.get("end", {})
    if "date" in start:
        return "終日"
    if "dateTime" in start and "dateTime" in end:
        s = datetime.datetime.fromisoformat(start["dateTime"].replace("Z", "+00:00")).astimezone(JST)
        e = datetime.datetime.fromisoformat(end["dateTime"].replace("Z", "+00:00")).astimezone(JST)
        return f"{s.strftime('%H:%M')}-{e.strftime('%H:%M')}"
    return ""


# ---------------------------------------------------------------------------
# Notion: タスクDBへの追加
# ---------------------------------------------------------------------------
def fetch_existing_event_ids():
    """タスクDBに既に同期済みのGCalイベントIDをセットで返す。"""
    existing = set()
    cursor = None
    while True:
        payload = {
            "filter": {
                "property": "GCalイベントID",
                "rich_text": {"is_not_empty": True},
            },
            "page_size": 100,
        }
        if cursor:
            payload["start_cursor"] = cursor
        url = f"{NOTION_API_BASE}/databases/{NOTION_DATABASE_ID}/query"
        res = http_request(url, "POST", payload, NOTION_HEADERS)
        for page in res.get("results", []):
            prop = page["properties"].get("GCalイベントID", {})
            rt = prop.get("rich_text", [])
            if rt:
                existing.add(rt[0]["plain_text"])
        if res.get("has_more"):
            cursor = res.get("next_cursor")
        else:
            break
    return existing


def create_notion_task(ev):
    title = ev.get("summary", "(無題の予定)")
    date_iso = event_start_date_iso(ev)
    if not date_iso:
        return None

    properties = {
        "タスク名": {"title": [{"text": {"content": title}}]},
        "タスク種別": {"select": {"name": "予定"}},
        "期日": {"date": {"start": date_iso}},
        "GCalイベントID": {
            "rich_text": [{"text": {"content": ev["id"]}}]
        },
        "カレンダー名": {
            "rich_text": [{"text": {"content": ev.get("_calendar_name", "")}}]
        },
    }

    payload = {
        "parent": {"database_id": NOTION_DATABASE_ID},
        "properties": properties,
    }

    # 説明や場所、URLが分かれば本文に追加
    blocks = []
    time_label = event_time_label(ev)
    info_lines = []
    if time_label:
        info_lines.append(f"⏰ {time_label}")
    if ev.get("location"):
        info_lines.append(f"📍 {ev['location']}")
    if ev.get("hangoutLink"):
        info_lines.append(f"🔗 {ev['hangoutLink']}")
    elif ev.get("htmlLink"):
        info_lines.append(f"🔗 {ev['htmlLink']}")
    if info_lines:
        blocks.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [{"type": "text", "text": {"content": "\n".join(info_lines)}}]
            },
        })
    if ev.get("description"):
        blocks.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [{"type": "text", "text": {"content": ev["description"][:1900]}}]
            },
        })
    if blocks:
        payload["children"] = blocks

    return http_request(f"{NOTION_API_BASE}/pages", "POST", payload, NOTION_HEADERS)


def sync_events_to_db(events):
    existing_ids = fetch_existing_event_ids()
    created = 0
    skipped = 0
    for ev in events:
        if ev["id"] in existing_ids:
            skipped += 1
            continue
        try:
            create_notion_task(ev)
            created += 1
        except Exception as e:
            print(f"  [警告] 作成失敗: {ev.get('summary')} ({e})")
    print(f"  → 新規作成: {created}件 / 既存スキップ: {skipped}件")


# ---------------------------------------------------------------------------
# Notion: ダッシュボードページ内の「週次セクション」を上書き更新
# ---------------------------------------------------------------------------
def build_weekly_blocks(events):
    """イベントを曜日ごとにグループ化し、Notionブロックの配列を返す。
    （セクション見出しは含まず、見出しの「下」に置く中身だけを返す）"""
    start, end = week_range_jst()
    week_days = [start.date() + datetime.timedelta(days=i) for i in range(7)]
    weekday_jp = ["月", "火", "水", "木", "金", "土", "日"]

    grouped = {d: [] for d in week_days}
    for ev in events:
        ds = event_start_date_iso(ev)
        if not ds:
            continue
        try:
            d = datetime.date.fromisoformat(ds)
        except ValueError:
            continue
        if d in grouped:
            grouped[d].append(ev)

    def _sort_key(ev):
        s = ev.get("start", {})
        if "dateTime" in s:
            return s["dateTime"]
        return s.get("date", "") + "T00:00:00"

    blocks = []

    # サブヘッダー: 期間と最終更新時刻
    now_str = datetime.datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    period_str = (
        f"{start.month}/{start.day}〜{end.month}/{end.day}　"
        f"最終更新: {now_str}（毎時自動更新）"
    )
    blocks.append({
        "object": "block",
        "type": "callout",
        "callout": {
            "icon": {"type": "emoji", "emoji": "🔄"},
            "rich_text": [{"type": "text", "text": {"content": period_str}}],
        },
    })

    today = datetime.datetime.now(JST).date()
    for d in week_days:
        head = f"{d.month}/{d.day} ({weekday_jp[d.weekday()]})"
        if d == today:
            head += "  ← 今日"
        blocks.append({
            "object": "block",
            "type": "heading_3",
            "heading_3": {
                "rich_text": [{"type": "text", "text": {"content": head}}]
            },
        })
        items = sorted(grouped[d], key=_sort_key)
        if not items:
            blocks.append({
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{"type": "text", "text": {"content": "（予定なし）"}}]
                },
            })
            continue
        for ev in items:
            time_label = event_time_label(ev)
            cal = ev.get("_calendar_name", "")
            title = ev.get("summary", "(無題)")
            line = f"{time_label}  {title}" if time_label else title
            if cal:
                line += f"  〔{cal}〕"
            blocks.append({
                "object": "block",
                "type": "bulleted_list_item",
                "bulleted_list_item": {
                    "rich_text": [{"type": "text", "text": {"content": line}}]
                },
            })

    return blocks


def list_child_blocks(page_id):
    """指定ページの直下子ブロックを全部返す。"""
    items = []
    cursor = None
    while True:
        url = f"{NOTION_API_BASE}/blocks/{page_id}/children?page_size=100"
        if cursor:
            url += f"&start_cursor={cursor}"
        res = http_request(url, "GET", None, NOTION_HEADERS)
        items.extend(res.get("results", []))
        if res.get("has_more"):
            cursor = res.get("next_cursor")
        else:
            break
    return items


def block_plain_text(blk):
    btype = blk.get("type")
    if not btype:
        return ""
    rt = blk.get(btype, {}).get("rich_text", [])
    return "".join(r.get("plain_text", "") for r in rt)


def find_section_marker_block(blocks, marker_text):
    """見出しブロックでテキストが marker_text と一致するものを探す。"""
    for blk in blocks:
        if blk.get("type", "").startswith("heading_") and block_plain_text(blk) == marker_text:
            return blk
    return None


def delete_block(block_id):
    try:
        http_request(
            f"{NOTION_API_BASE}/blocks/{block_id}",
            "DELETE",
            None,
            NOTION_HEADERS,
        )
    except Exception as e:
        print(f"  [警告] ブロック削除失敗: {e}")


def append_blocks(page_id, blocks):
    # Notion API は children を1リクエスト100件まで
    for i in range(0, len(blocks), 90):
        chunk = blocks[i:i + 90]
        http_request(
            f"{NOTION_API_BASE}/blocks/{page_id}/children",
            "PATCH",
            {"children": chunk},
            NOTION_HEADERS,
        )


def update_dashboard_section(events):
    """ダッシュボードページ内の「週次セクション」だけを最新内容で置き換える。
    ・見出し「📅 今週の予定（自動更新）」の下にあるブロックだけを削除して、
      新しい週次ブロックを追加する。
    ・見出しが無ければページ末尾に見出し+内容を追加する。
    ・見出しより上のユーザー手書き内容には一切触らない。
    """
    if not NOTION_DASHBOARD_PAGE_ID:
        print("  NOTION_DASHBOARD_PAGE_ID 未設定 → ダッシュボード更新はスキップ")
        return

    children = list_child_blocks(NOTION_DASHBOARD_PAGE_ID)
    marker = find_section_marker_block(children, WEEKLY_SECTION_HEADING)
    weekly_blocks = build_weekly_blocks(events)

    if marker:
        print(f"  既存セクション「{WEEKLY_SECTION_HEADING}」を更新")
        # マーカー以降（マーカー自身は残す）のブロックをすべて削除
        marker_idx = next(
            i for i, b in enumerate(children) if b["id"] == marker["id"]
        )
        for blk in children[marker_idx + 1:]:
            delete_block(blk["id"])
        # 新しい中身を追加（マーカーの直後ではなくページ末尾になるが、
        # マーカー以降を全削除済みなので結果として直後に並ぶ）
        append_blocks(NOTION_DASHBOARD_PAGE_ID, weekly_blocks)
    else:
        print(f"  セクション見出しが無いのでページ末尾に追加")
        marker_block = {
            "object": "block",
            "type": "heading_2",
            "heading_2": {
                "rich_text": [{
                    "type": "text",
                    "text": {"content": WEEKLY_SECTION_HEADING},
                }]
            },
        }
        append_blocks(NOTION_DASHBOARD_PAGE_ID, [marker_block] + weekly_blocks)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("Googleカレンダーの今週分の予定を取得中...")
    service = build_calendar_service()
    events = fetch_week_events(service)

    print("Notionタスクdbへ同期中...")
    sync_events_to_db(events)

    print("ダッシュボードの週次セクションを更新中...")
    update_dashboard_section(events)

    print("完了！")
