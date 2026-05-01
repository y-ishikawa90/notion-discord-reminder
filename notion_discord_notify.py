import os
import json
import datetime
import urllib.request

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
NOTION_DATABASE_ID = os.environ["NOTION_DATABASE_ID"]
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
NTFY_TOPIC = "yasu-tasks-20260501"

def query_notion_tasks():
    url = f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}/query"
    payload = {
        "filter": {
            "and": [
                {"property": "ステータス", "status": {"does_not_equal": "完了"}},
                {"or": [
                    {"property": "タスク種別", "select": {"equals": "毎日"}},
                    {"property": "タスク種別", "select": {"equals": "期日付き"}},
                    {"property": "タスク種別", "select": {"equals": "定期"}}
                ]}
            ]
        }
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={
            "Authorization": f"Bearer {NOTION_TOKEN}",
            "Content-Type": "application/json",
            "Notion-Version": "2022-06-28"
        }, method="POST"
    )
    with urllib.request.urlopen(req) as res:
        return json.loads(res.read())

def format_tasks(results):
    today = datetime.date.today()
    high, mid, low = [], [], []
    for page in results:
        props = page["properties"]
        title_list = props.get("タスク名", {}).get("title", [])
        name = title_list[0]["plain_text"] if title_list else "(名前なし)"
        category_list = props.get("カテゴリ", {}).get("multi_select", [])
        category = category_list[0]["name"] if category_list else ""
        importance_obj = props.get("重要度", {}).get("select")
        importance = importance_obj["name"] if importance_obj else "low"
        due_date_obj = props.get("期日", {}).get("date")
        overdue = ""
        if due_date_obj and due_date_obj.get("start"):
            due = datetime.date.fromisoformat(due_date_obj["start"])
            if due < today:
                overdue = " [期限切れ]"
        label = f"・{name}({category}){overdue}" if category else f"・{name}{overdue}"
        if "高" in importance:
            high.append(label)
        elif "中" in importance:
            mid.append(label)
        else:
            low.append(label)
    return high, mid, low

def send_ntfy(high, mid, low):
    today = datetime.date.today().strftime("%Y/%m/%d")
    title = f"Today Tasks {today}"
    message = (
        f"[高] 高優先度
"
        f"{chr(10).join(high) if high else '(なし)'}

"
        f"[中] 中優先度
"
        f"{chr(10).join(mid) if mid else '(なし)'}

"
        f"[低] 低優先度
"
        f"{chr(10).join(low) if low else '(なし)'}

"
        f"今日も頓張りましょう！"
    )
    req = urllib.request.Request(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers={
            "Title": title,
            "Priority": "high",
            "Tags": "calendar",
            "Content-Type": "text/plain; charset=utf-8"
        },
        method="POST"
    )
    with urllib.request.urlopen(req) as res:
        print(f"ntfy送信成功: {res.status}")

def send_discord(high, mid, low):
    today = datetime.date.today().strftime("%Y/%m/%d")
    message = (
        f"おはようございます！今日のタスク一覧です（{today}）

"
        f"U0001f534 **高優先度**
"
        f"{chr(10).join(high) if high else '（なし）'}

"
        f"U0001f7e1 **中優先度**
"
        f"{chr(10).join(mid) if mid else '（なし）'}

"
        f"U0001f7e2 **習慣・低優先度**
"
        f"{chr(10).join(low) if low else '（なし）'}

"
        f"今日も一日頓張りましょう！"
    )
    data = json.dumps({"content": message}).encode("utf-8")
    req = urllib.request.Request(
        DISCORD_WEBHOOK_URL, data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "DiscordBot (notion-discord-reminder, 1.0)"
        }, method="POST"
    )
    with urllib.request.urlopen(req) as res:
        print(f"Discord送信成功: {res.status}")

if __name__ == "__main__":
    print("Notionからタスクを取得中...")
    result = query_notion_tasks()
    print(f"取得件数: {len(result['results'])}件")
    high, mid, low = format_tasks(result["results"])
    print("ntfyに送信中...")
    send_ntfy(high, mid, low)
    print("Discordに送信中...")
    send_discord(high, mid, low)
    print("完了！")
