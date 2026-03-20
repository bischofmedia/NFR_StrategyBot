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

VALID_LEAGUES = ["rtc", "awl", "gtfun"]
DEFAULT_LEAGUE = "rtc"


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
# Settings (liga-abhängig)
# ─────────────────────────────────────────────

def get_settings(league: str = DEFAULT_LEAGUE) -> dict:
    league = normalise_league(league)
    sheet  = get_sheet()
    ws     = sheet.worksheet(f"{league}_settings")
    rows   = ws.get_all_values(value_render_option='UNFORMATTED_VALUE')
    settings = {}
    for row in rows[1:]:
        if len(row) >= 2 and row[0]:
            settings[str(row[0]).strip()] = str(row[1]).strip()
    return settings


# ─────────────────────────────────────────────
# Rennkalender (liga-abhängig)
# ─────────────────────────────────────────────

def get_next_race(league: str = DEFAULT_LEAGUE) -> dict | None:
    league = normalise_league(league)
    sheet  = get_sheet()
    ws     = sheet.worksheet(f"{league}_rennkalender")
    rows   = ws.get_all_records()
    today  = datetime.today().date()
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
# Stammdaten: Marken + Modelle
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
# Fahrerdaten (liga-abhängig)
# ─────────────────────────────────────────────

def _driver_sheet_name(nickname: str, league: str) -> str:
    return f"{normalise_league(league)}_{nickname}"


def ensure_driver_sheet(nickname: str, league: str = DEFAULT_LEAGUE):
    sheet  = get_sheet()
    name   = _driver_sheet_name(nickname, league)
    titles = [ws.title for ws in sheet.worksheets()]
    if name not in titles:
        ws = sheet.add_worksheet(title=name, rows=500, cols=20)
        ws.append_row([
            "Strecke", "Version", "Marke", "Modell",
            "Zeit_Soft_s", "Zeit_Medium_s", "Medium_Pct",
            "Zeit_Hard_s", "Hard_Pct",
            "Max_Soft_Runden", "Reichweite_70pct",
            "Letzte_Aktualisierung"
        ])
    return sheet.worksheet(name)


def get_driver_data(nickname: str, track: str, version: str,
                    brand: str, model: str, league: str = DEFAULT_LEAGUE) -> dict | None:
    sheet  = get_sheet()
    name   = _driver_sheet_name(nickname, league)
    titles = [ws.title for ws in sheet.worksheets()]
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
    sheet  = get_sheet()
    name   = _driver_sheet_name(nickname, league)
    titles = [ws.title for ws in sheet.worksheets()]
    if name not in titles:
        return {"medium_pct": None, "hard_pct": None}
    ws   = sheet.worksheet(name)
    rows = ws.get_all_records(value_render_option='UNFORMATTED_VALUE')
    medium_vals, hard_vals = [], []
    for row in rows:
        try:
            m = float(str(row.get("Medium_Pct", "")).replace(",", "."))
            if m > 0:
                medium_vals.append(m)
        except (ValueError, TypeError):
            pass
        try:
            h = float(str(row.get("Hard_Pct", "")).replace(",", "."))
            if h > 0:
                hard_vals.append(h)
        except (ValueError, TypeError):
            pass
    return {
        "medium_pct": round(sum(medium_vals)/len(medium_vals), 3) if medium_vals else None,
        "hard_pct":   round(sum(hard_vals)/len(hard_vals),     3) if hard_vals   else None,
    }


def save_driver_data(nickname: str, track: str, version: str,
                     brand: str, model: str, data: dict,
                     league: str = DEFAULT_LEAGUE):
    ws   = ensure_driver_sheet(nickname, league)
    rows = ws.get_all_records(value_render_option='UNFORMATTED_VALUE')
    now  = datetime.now(BERLIN).strftime("%d.%m.%Y %H:%M")

    soft_s   = data["zeit_soft_s"]
    medium_s = data["zeit_medium_s"]
    hard_s   = data.get("zeit_hard_s")

    medium_pct = round((medium_s - soft_s) / soft_s * 100, 4) if medium_s else None
    hard_pct   = round((hard_s   - soft_s) / soft_s * 100, 4) if hard_s   else None

    new_row = [
        track, version, brand, model,
        round(float(soft_s), 3),
        round(float(medium_s), 3) if medium_s else "",
        round(float(medium_pct), 4) if medium_pct is not None else "",
        round(float(hard_s), 3)    if hard_s   else "",
        round(float(hard_pct), 4)  if hard_pct is not None else "",
        int(data["max_soft_runden"]),
        int(data["reichweite_70pct"]),
        now
    ]

    for i, row in enumerate(rows, start=2):
        if (row.get("Strecke")  == track
                and row.get("Version") == version
                and row.get("Marke")   == brand
                and row.get("Modell")  == model):
            ws.update(f"A{i}:L{i}", [new_row])
            return

    ws.append_row(new_row)
