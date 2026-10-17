"""
התראת עומס מגוגל מפות -> מייל.
רץ ב-GitHub Actions, קורא את מדד העומס החי (Live) של כל מקום ב-config.json,
ושולח מייל כשהמקום נהיה עמוס (ושוב כשהוא נרגע, אם ביקשת).
"""
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import smtplib
from email.message import EmailMessage

ROOT = Path(__file__).parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
STATE_FILE = ROOT / "state.json"
TZ = ZoneInfo(CONFIG.get("timezone", "Asia/Jerusalem"))
TEST_MODE = os.environ.get("TEST_MODE", "false").lower() == "true"

LIVE_RE = re.compile(r"Currently (\d+)% busy, usually (\d+)% busy", re.I)


def in_check_hours(now: datetime) -> bool:
    days = CONFIG.get("check_days")  # 0=ראשון ... 6=שבת
    israeli_weekday = (now.weekday() + 1) % 7
    if days is not None and israeli_weekday not in days:
        return False
    start = CONFIG.get("check_hours", {}).get("start", 0)
    end = CONFIG.get("check_hours", {}).get("end", 24)
    return start <= now.hour < end


def send_email(subject: str, body: str) -> None:
    """שליחת מייל דרך Gmail עם סיסמת אפליקציה."""
    sender = os.environ["EMAIL_ADDRESS"]
    password = os.environ["EMAIL_APP_PASSWORD"].replace(" ", "")
    to = os.environ.get("EMAIL_TO") or sender
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(sender, password)
        smtp.send_message(msg)


def with_english(url: str) -> str:
    if "google." in url and "hl=" not in url:
        url += ("&" if "?" in url else "?") + "hl=en"
    return url


def read_live(page, url: str) -> dict:
    """מחזיר {'live': int|None, 'usual': int|None, 'status': str|None}"""
    page.goto(with_english(url), wait_until="domcontentloaded", timeout=60000)

    # מסך הסכמה לעוגיות (מופיע לפעמים בשרתים באירופה)
    if "consent." in page.url:
        for label in ("Reject all", "Accept all"):
            btn = page.get_by_role("button", name=label)
            if btn.count():
                btn.first.click()
                page.wait_for_load_state("domcontentloaded")
                break

    # קישורים מקוצרים (maps.app.goo.gl) מפנים לדף המלא - מוודאים שהוא באנגלית
    if "google." in page.url and "hl=en" not in page.url:
        page.goto(with_english(page.url.replace("hl=", "hl_old=")), wait_until="domcontentloaded", timeout=60000)

    try:
        page.wait_for_selector('[aria-label*="busy at"], [aria-label^="Currently"]', timeout=25000)
    except Exception:
        # גלילה בחלונית הצד כדי לטעון את גרף שעות העומס
        for _ in range(6):
            page.mouse.move(200, 500)
            page.mouse.wheel(0, 1200)
            page.wait_for_timeout(800)
        try:
            page.wait_for_selector('[aria-label*="busy at"], [aria-label^="Currently"]', timeout=10000)
        except Exception:
            return {"live": None, "usual": None, "status": None, "no_graph": True}

    page.wait_for_timeout(1500)
    labels = page.eval_on_selector_all(
        "[aria-label]", "els => els.map(e => e.getAttribute('aria-label'))"
    )
    live = usual = None
    for lab in labels:
        m = LIVE_RE.search(lab or "")
        if m:
            live, usual = int(m.group(1)), int(m.group(2))
            break

    status = page.evaluate(
        """() => {
          const el = [...document.querySelectorAll('div,span')]
            .find(e => e.children.length < 3 && /^\\s*Live\\b/i.test(e.textContent));
          if (!el) return null;
          let p = el; for (let i = 0; i < 3 && p.parentElement; i++) p = p.parentElement;
          const lines = p.innerText.split('\\n').map(s => s.trim()).filter(Boolean);
          const t = lines[0].replace(/^LIVE\\s*/i, '');
          return t || lines[1] || null;
        }"""
    )
    return {"live": live, "usual": usual, "status": status, "no_graph": False}


# מצבי העומס של גוגל, מהנמוך לגבוה
LEVELS = [
    # (key, ביטוי לזיהוי בתווית של גוגל, אימוג'י, כותרת בעברית)
    ("not_busy",     "not busy",           "🟢", "לא עמוס"),
    ("less_usual",   "less busy than usual", "🟢", "פחות עמוס מהרגיל"),
    ("not_too_busy", "not too busy",       "🟢", "לא עמוס במיוחד"),
    ("a_little",     "a little busy",      "🟡", "קצת עמוס"),
    ("busy",         "busy",               "🟠", "עמוס"),
    ("busier_usual", "busier than usual",  "🔴", "עמוס מהרגיל"),
    ("peak",         "as busy as it gets", "🟣", "עמוס כמו בשיא"),
]
RANK = {k: i for i, (k, *_ ) in enumerate(LEVELS)}
INFO = {k: (emoji, he) for k, _, emoji, he in LEVELS}
# סדר הבדיקה: קודם הביטויים הארוכים, כדי ש-"busy" לא יתפוס את "a little busy"
MATCH_ORDER = sorted(LEVELS, key=lambda x: -len(x[1]))


def level_from_label(label: str | None) -> str | None:
    if not label:
        return None
    t = label.lower()
    for key, phrase, *_ in MATCH_ORDER:
        if phrase in t:
            return key
    return None


def level_from_percent(live: int | None, usual: int | None) -> str | None:
    """גיבוי, אם גוגל לא הציגו תווית אבל יש אחוזים."""
    if live is None:
        return None
    if live >= 90:
        return "peak"
    if usual is not None and live >= usual + 15:
        return "busier_usual"
    if live >= 65:
        return "busy"
    if live >= 40:
        return "a_little"
    if usual is not None and live <= usual - 15:
        return "less_usual"
    return "not_too_busy" if live >= 15 else "not_busy"


def get_level(r: dict) -> str | None:
    return level_from_label(r.get("status")) or level_from_percent(r["live"], r["usual"])


def build_email(name: str, level: str, r: dict, url: str, prev: str | None) -> tuple[str, str]:
    emoji, he = INFO[level]
    subject = f"{emoji} {he} | {name}"
    lines = [f"{emoji} {name}: {he}"]
    if r.get("status"):
        lines.append(f"בגוגל מפות: {r['status']}")
    if r["live"] is not None:
        usual = f" (בדרך כלל בשעה הזו: {r['usual']}%)" if r["usual"] is not None else ""
        lines.append(f"עומס עכשיו: {r['live']}%{usual}")
    if prev and prev in INFO:
        pe, ph = INFO[prev]
        arrow = "⬆️ עלה" if RANK[level] > RANK[prev] else "⬇️ ירד"
        lines.append(f"{arrow} מ-{pe} {ph}")
    lines += ["", url]
    return subject, "\n".join(lines)


def main() -> int:
    now = datetime.now(TZ)
    if not TEST_MODE and not in_check_hours(now):
        print(f"{now:%a %H:%M} - out of check hours, skipping.")
        return 0

    from playwright.sync_api import sync_playwright

    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    report = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(
            locale="en-US",
            timezone_id=str(TZ),
            viewport={"width": 1400, "height": 1000},
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        page = ctx.new_page()
        for place in CONFIG["places"]:
            name = place["name"]
            try:
                r = read_live(page, place["url"])
            except Exception as e:  # noqa: BLE001
                print(f"[{name}] error: {e}")
                report.append(f"⚠️ {name}: שגיאה בקריאה")
                continue

            print(f"[{name}] {r}")
            level = get_level(r)
            min_rank = RANK[level_from_label(place.get("min_level", "A little busy")) or "a_little"]

            if TEST_MODE:
                if r.get("no_graph"):
                    report.append(f"⚪ {name}: לא נמצא גרף שעות עומס למקום הזה")
                elif level is None:
                    report.append(f"⚪ {name}: אין כרגע נתון חי (המקום כנראה סגור)")
                else:
                    subject, body = build_email(name, level, r, place["url"], None)
                    report.append(body)
                continue

            if level is None:
                # אין נתון חי (מקום סגור / תקלה זמנית) - לא משנים מצב
                continue

            prev = state.get(name, {}).get("level")
            alerting = RANK[level] >= min_rank
            was_alerting = prev in RANK and RANK[prev] >= min_rank

            if alerting and level != prev:
                # נכנס לעומס, או שמצב העומס השתנה (למשל מקצת עמוס לעמוס)
                send_email(*build_email(name, level, r, place["url"], prev if was_alerting else None))
            elif was_alerting and not alerting and place.get("notify_when_clear", False):
                emoji, he = INFO[level]
                subject = f"{emoji} נרגע | {name}"
                body = build_email(name, level, r, place["url"], prev)[1]
                send_email(subject, body)
            state[name] = {"level": level}

        browser.close()

    if TEST_MODE:
        send_email("🧪 בדיקת מערכת התראות עומס", "\n\n".join(report))

    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
