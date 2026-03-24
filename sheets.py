import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime, timezone, timedelta
import os
from dotenv import load_dotenv
load_dotenv(override=True)

BERLIN = timezone(timedelta(hours=1))

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

VALID_LEAGUES   = ["rtc", "awl", "gtfun"]
DEFAULT_LEAGUE  = "rtc"

# Herkunft-Codes
SRC_EINGABE   = 1  # manuell eingegeben
SRC_SETTINGS  = 2  # aus Settings berechnet
SRC_AVERAGE   = 3  # aus früheren Eingaben (Durchschnitt)
SRC_ZEITEN    = 4  # aus der Zeiten-Tabelle


def get_client():
    creds = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
    return gspread.authorize(creds)

def get_sheet():
    client = get_client()
    return client.open_by_key(os.getenv("GOOGLE_SHEET_ID"))

def get_times_sheet():
    client = get_client()
    return client.open_by_key(os.getenv("GOOGLE_TIMES_SHEET_ID"))


def normalise_league(league: str) -> str:
    league = (league or DEFAULT_LEAGUE).lower().strip()
    return league if league in VALID_LEAGUES else DEFAULT_LEAGUE


# ─────────────────────────────────────────────
# Zeiten-Sheet
# ─────────────────────────────────────────────

def get_track_data(track: str, version: str) -> dict:
    try:
        sheet = get_times_sheet()
        ws    = sheet.worksheet("Zeiten")
        rows  = ws.get_all_values(value_render_option='UNFORMATTED_VALUE')
        track_norm   = track.strip().lower()
        version_norm = version.strip().lower() if version else ""
        for row in rows[1:]:
            if len(row) < 6:
                continue
            if row[0].strip().lower() == track_norm and row[1].strip().lower() == version_norm:
                best_lap_s = None
                try:
                    val = float(str(row[5]).replace(",", "."))
                    if val > 0:
                        best_lap_s = val
                except (ValueError, IndexError):
                    pass
                pit_loss_s = None
                try:
                    val = float(str(row[7]).replace(",", "."))
                    if val > 0:
                        pit_loss_s = val
                except (ValueError, IndexError):
                    pass
                return {"pit_loss_s": pit_loss_s, "best_lap_s": best_lap_s}
    except Exception as e:
        print(f"Fehler beim Lesen des Zeiten-Sheets: {e}")
    return {"pit_loss_s": None, "best_lap_s": None}


# ─────────────────────────────────────────────
# Settings
# ─────────────────────────────────────────────

def get_settings(league: str = DEFAULT_LEAGUE) -> dict:
    league = normalise_league(league)
    sheet  = get_sheet()
    for tab_name in [f"{league}_settings", "Settings"]:
        try:
            ws   = sheet.worksheet(tab_name)
            rows = ws.get_all_values(value_render_option='UNFORMATTED_VALUE')
            settings = {}
            for row in rows[1:]:
                if len(row) >= 2 and row[0]:
                    settings[str(row[0]).strip()] = str(row[1]).strip()
            return settings
        except Exception:
            continue
    return {}


# ─────────────────────────────────────────────
# Rennkalender
# ─────────────────────────────────────────────

def get_next_race(league: str = DEFAULT_LEAGUE) -> dict | None:
    league = normalise_league(league)
    sheet  = get_sheet()
    ws = None
    for tab_name in [f"{league}_rennkalender", "Rennkalender"]:
        try:
            ws = sheet.worksheet(tab_name)
            break
        except Exception:
            continue
    if ws is None:
        return None
    rows  = ws.get_all_records()
    today = datetime.today().date()
    upcoming = []
    for row in rows:
        try:
            race_date = datetime.strptime(str(row["Datum"]), "%d.%m.%Y").date()
            if race_date >= today:
                upcoming.append((race_date, row))
        except (ValueError, KeyError):
            continue
    if not upcoming:
        return None
    upcoming.sort(key=lambda x: x[0])
    return upcoming[0][1]


# ─────────────────────────────────────────────
# Stammdaten
# ─────────────────────────────────────────────

def get_brands_and_models() -> dict:
    sheet = get_sheet()
    ws    = sheet.worksheet("Stammdaten")
    rows  = ws.get_all_values()
    brands = {}
    for row in rows[1:]:
        if len(row) < 2:
            continue
        brand = row[0].strip()
        model = row[1].strip()
        if not brand or not model:
            continue
        if brand not in brands:
            brands[brand] = []
        if model not in brands[brand]:
            brands[brand].append(model)
    return brands


# ─────────────────────────────────────────────
# Fahrerdaten
# ─────────────────────────────────────────────

# Spalten-Header mit Herkunfts-Spalten
DRIVER_HEADERS = [
    "Strecke", "Version", "Marke", "Modell",
    "Zeit_Soft_s", "Soft_Src",
    "Zeit_Medium_s", "Medium_Pct", "Medium_Src",
    "Zeit_Hard_s", "Hard_Pct", "Hard_Src",
    "Max_Soft_Runden", "Reichweite_70pct",
    "Letzte_Aktualisierung"
]


def _driver_sheet_name(nickname: str, league: str) -> str:
    return f"{normalise_league(league)}_{nickname}"


def ensure_driver_sheet(nickname: str, league: str = DEFAULT_LEAGUE):
    sheet  = get_sheet()
    name   = _driver_sheet_name(nickname, league)
    titles = [ws.title for ws in sheet.worksheets()]
    if name not in titles and nickname in titles:
        try:
            old_ws = sheet.worksheet(nickname)
            old_ws.update_title(name)
            titles = [ws.title for ws in sheet.worksheets()]
        except Exception:
            pass
    if name not in titles:
        ws = sheet.add_worksheet(title=name, rows=500, cols=20)
        ws.append_row(DRIVER_HEADERS)
    return sheet.worksheet(name)


def get_driver_data(nickname: str, track: str, version: str,
                    brand: str, model: str, league: str = DEFAULT_LEAGUE) -> dict | None:
    sheet  = get_sheet()
    name   = _driver_sheet_name(nickname, league)
    titles = [ws.title for ws in sheet.worksheets()]
    if name not in titles and nickname in titles:
        name = nickname
    if name not in titles:
        return None
    ws   = sheet.worksheet(name)
    rows = ws.get_all_records(value_render_option='UNFORMATTED_VALUE')
    for row in rows:
        if (row.get("Strecke")  == track
                and row.get("Version") == version
                and row.get("Marke")   == brand
                and row.get("Modell")  == model):
            return row
    return None


def get_driver_avg_pct(nickname: str, league: str = DEFAULT_LEAGUE) -> dict:
    """
    Berechnet Durchschnitt nur aus Zeiten mit Src=1 (manuell eingegeben).
    Fallback auf alle Zeiten wenn keine Src=1 vorhanden.
    """
    sheet  = get_sheet()
    name   = _driver_sheet_name(nickname, league)
    titles = [ws.title for ws in sheet.worksheets()]
    if name not in titles and nickname in titles:
        name = nickname
    if name not in titles:
        return {"medium_pct": None, "hard_pct": None}

    ws   = sheet.worksheet(name)
    rows = ws.get_all_records(value_render_option='UNFORMATTED_VALUE')

    med_entered, hard_entered = [], []  # Src=1
    med_all, hard_all = [], []           # alle

    for row in rows:
        def safe_float(v):
            try: return float(str(v).replace(",", "."))
            except: return None

        m = safe_float(row.get("Medium_Pct"))
        if m and 0 < m <= 10:
            med_all.append(m)
            if str(row.get("Medium_Src", "")).strip() == "1":
                med_entered.append(m)

        h = safe_float(row.get("Hard_Pct"))
        if h and 0 < h <= 20:
            hard_all.append(h)
            if str(row.get("Hard_Src", "")).strip() == "1":
                hard_entered.append(h)

    med_pool  = med_entered  if med_entered  else med_all
    hard_pool = hard_entered if hard_entered else hard_all

    return {
        "medium_pct": round(sum(med_pool)/len(med_pool),   3) if med_pool  else None,
        "hard_pct":   round(sum(hard_pool)/len(hard_pool), 3) if hard_pool else None,
        "medium_src": SRC_AVERAGE if med_pool  else None,
        "hard_src":   SRC_AVERAGE if hard_pool else None,
    }


def save_driver_data(nickname: str, track: str, version: str,
                     brand: str, model: str, data: dict,
                     league: str = DEFAULT_LEAGUE):
    ws   = ensure_driver_sheet(nickname, league)
    rows = ws.get_all_records(value_render_option='UNFORMATTED_VALUE')
    now  = datetime.now(BERLIN).strftime("%d.%m.%Y %H:%M")

    def fp(v, d): return f"{float(v):.{d}f}" if v is not None and v != "" else ""

    soft_s     = data["zeit_soft_s"]
    medium_s   = data["zeit_medium_s"]
    hard_s     = data.get("zeit_hard_s")
    soft_src   = data.get("soft_src",   SRC_EINGABE)
    medium_src = data.get("medium_src", SRC_EINGABE)
    hard_src   = data.get("hard_src",   SRC_EINGABE)

    medium_pct = round((float(medium_s) - float(soft_s)) / float(soft_s) * 100, 4) if medium_s else None
    hard_pct   = round((float(hard_s)   - float(soft_s)) / float(soft_s) * 100, 4) if hard_s   else None

    new_row = [
        track, version, brand, model,
        fp(soft_s, 3),   soft_src,
        fp(medium_s, 3), fp(medium_pct, 4), medium_src,
        fp(hard_s, 3),   fp(hard_pct, 4),   hard_src,
        int(data["max_soft_runden"]),
        int(data["reichweite_70pct"]),
        now
    ]

    for i, row in enumerate(rows, start=2):
        if (row.get("Strecke")  == track
                and row.get("Version") == version
                and row.get("Marke")   == brand
                and row.get("Modell")  == model):
            ws.update(f"A{i}:O{i}", [new_row])
            return

    ws.append_row(new_row)
