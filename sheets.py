import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime
import os
from dotenv import load_dotenv
load_dotenv(override=True)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def parse_number(val, is_small=False) -> float | None:
    """
    Parst Zahlen aus Google Sheets robust.
    Behandelt deutsche Locale: "109,3" → 109.3, "1.093" → 1093.
    is_small=True: Wert ist bekannt klein (<100), Komma ist immer Dezimaltrenner.
    Gibt None zurück wenn nicht parsebar.
    """
    if val is None:
        return None
    s = str(val).strip()
    if not s or s in ("-", "–"):
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        parts = s.split(",")
        # Tausendertrennzeichen nur wenn: 3 Stellen nach Komma UND Wert > 100
        if (len(parts) == 2 and len(parts[1]) == 3
                and parts[0].isdigit() and not is_small
                and int(parts[0]) >= 1):
            # Ambig: "1,006" könnte 1006 oder 1.006 sein
            # Wenn Vorkommastelle = 1 Ziffer → wahrscheinlich Dezimal (z.B. Prozentwert)
            if len(parts[0]) == 1:
                s = s.replace(",", ".")  # Dezimaltrenner
            else:
                s = s.replace(",", "")   # Tausendertrenner
        else:
            s = s.replace(",", ".")
    elif "." in s:
        parts = s.split(".")
        if (len(parts) == 2 and len(parts[1]) == 3
                and parts[0].isdigit() and not is_small
                and len(parts[0]) > 1):
            s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return None

def get_client():
    creds = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
    return gspread.authorize(creds)

def get_sheet():
    client = get_client()
    return client.open_by_key(os.getenv("GOOGLE_SHEET_ID"))

def get_times_sheet():
    client = get_client()
    return client.open_by_key(os.getenv("GOOGLE_TIMES_SHEET_ID"))


# ---------- Zeiten-Sheet ----------

def get_track_data(track: str, version: str) -> dict:
    try:
        sheet = get_times_sheet()
        ws = sheet.worksheet("Zeiten")
        rows = ws.get_all_values(value_render_option='UNFORMATTED_VALUE')
        track_norm   = track.strip().lower()
        version_norm = version.strip().lower() if version else ""
        for row in rows[1:]:
            if len(row) < 6:
                continue
            if row[0].strip().lower() == track_norm and row[1].strip().lower() == version_norm:
                best_lap_s = None
                try:
                    val = float(row[5].strip().replace(",", "."))
                    if val > 0:
                        best_lap_s = val
                except (ValueError, IndexError):
                    pass
                pit_loss_s = None
                try:
                    val = float(row[7].strip().replace(",", "."))
                    if val > 0:
                        pit_loss_s = val
                except (ValueError, IndexError):
                    pass
                return {"pit_loss_s": pit_loss_s, "best_lap_s": best_lap_s}
    except Exception as e:
        print(f"Fehler beim Lesen des Zeiten-Sheets: {e}")
    return {"pit_loss_s": None, "best_lap_s": None}


# ---------- Settings ----------

def get_settings() -> dict:
    sheet = get_sheet()
    ws    = sheet.worksheet("Settings")
    rows  = ws.get_all_values()
    settings = {}
    for row in rows[1:]:
        if len(row) >= 2 and row[0]:
            settings[row[0].strip()] = row[1].strip()
    return settings


# ---------- Rennkalender ----------

def get_next_race() -> dict | None:
    sheet = get_sheet()
    ws    = sheet.worksheet("Rennkalender")
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


# ---------- Stammdaten: Marken + Modelle ----------

def get_brands_and_models() -> dict:
    """
    Gibt ein dict zurück: { "Marke": ["Modell1", "Modell2", ...], ... }
    Stammdaten-Sheet: Spalte A = Marke, Spalte B = Modell
    """
    sheet = get_sheet()
    ws    = sheet.worksheet("Stammdaten")
    rows  = ws.get_all_values()
    brands = {}
    for row in rows[1:]:  # Header überspringen
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


# ---------- Fahrerdaten ----------

def ensure_driver_sheet(nickname: str):
    sheet  = get_sheet()
    titles = [ws.title for ws in sheet.worksheets()]
    if nickname not in titles:
        ws = sheet.add_worksheet(title=nickname, rows=500, cols=20)
        ws.append_row([
            "Strecke", "Version", "Marke", "Modell",
            "Zeit_Soft_s",
            "Zeit_Medium_s", "Medium_Pct",
            "Zeit_Hard_s",   "Hard_Pct",
            "Max_Soft_Runden", "Reichweite_70pct",
            "Letzte_Aktualisierung"
        ])
    return sheet.worksheet(nickname)


def get_driver_data(nickname: str, track: str, version: str, brand: str, model: str) -> dict | None:
    sheet  = get_sheet()
    titles = [ws.title for ws in sheet.worksheets()]
    if nickname not in titles:
        return None
    ws   = sheet.worksheet(nickname)
    rows = ws.get_all_records(value_render_option='UNFORMATTED_VALUE')
    for row in rows:
        if (row.get("Strecke")  == track
                and row.get("Version") == version
                and row.get("Marke")   == brand
                and row.get("Modell")  == model):
            # Zahlenfelder robust normalisieren
            for field in ["Zeit_Soft_s", "Zeit_Medium_s", "Zeit_Hard_s"]:
                if row.get(field) not in (None, "", "–", "-"):
                    row[field] = parse_number(row[field])
            for field in ["Medium_Pct", "Hard_Pct"]:
                if row.get(field) not in (None, "", "–", "-"):
                    row[field] = parse_number(row[field], is_small=True)
            for field in ["Max_Soft_Runden", "Reichweite_70pct"]:
                if row.get(field) not in (None, "", "–", "-"):
                    v = parse_number(row[field])
                    if v is not None:
                        row[field] = int(v)
            return row
    return None


def get_driver_avg_pct(nickname: str) -> dict:
    sheet  = get_sheet()
    titles = [ws.title for ws in sheet.worksheets()]
    if nickname not in titles:
        return {"medium_pct": None, "hard_pct": None}
    ws   = sheet.worksheet(nickname)
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
        "medium_pct": round(sum(medium_vals) / len(medium_vals), 3) if medium_vals else None,
        "hard_pct":   round(sum(hard_vals)   / len(hard_vals),   3) if hard_vals   else None,
    }


def save_driver_data(nickname: str, track: str, version: str, brand: str, model: str, data: dict):
    ws   = ensure_driver_sheet(nickname)
    rows = ws.get_all_records(value_render_option='UNFORMATTED_VALUE')
    now  = datetime.now().strftime("%d.%m.%Y %H:%M")

    soft_s   = data["zeit_soft_s"]
    medium_s = data["zeit_medium_s"]
    hard_s   = data.get("zeit_hard_s")

    medium_pct = round((medium_s - soft_s) / soft_s * 100, 3) if medium_s else None
    hard_pct   = round((hard_s   - soft_s) / soft_s * 100, 3) if hard_s   else None

    # Zahlen als Python-floats/ints übergeben – gspread sendet diese als
    # echte Zahlen an die Sheets API, keine String-Konvertierung, keine Locale-Probleme
    new_row = [
        track, version, brand, model,
        round(float(soft_s), 3),
        round(float(medium_s), 3) if medium_s else "",
        round(float(medium_pct), 4) if medium_pct is not None else "",
        round(float(hard_s), 3)   if hard_s   else "",
        round(float(hard_pct), 4) if hard_pct is not None else "",
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
