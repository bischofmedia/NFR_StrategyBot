import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime
import os

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

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
        rows = ws.get_all_values()
        track_norm = track.strip().lower()
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
    ws = sheet.worksheet("Settings")
    rows = ws.get_all_values()
    settings = {}
    for row in rows[1:]:
        if len(row) >= 2 and row[0]:
            settings[row[0].strip()] = row[1].strip()
    return settings


# ---------- Rennkalender ----------

def get_next_race() -> dict | None:
    sheet = get_sheet()
    ws = sheet.worksheet("Rennkalender")
    rows = ws.get_all_records()
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


# ---------- Stammdaten ----------

def get_cars() -> list[str]:
    sheet = get_sheet()
    ws = sheet.worksheet("Stammdaten")
    rows = ws.get_all_values()
    cars = []
    for row in rows[1:]:  # Erste Zeile = Header überspringen
        if len(row) >= 1 and row[0].strip():
            cars.append(row[0].strip())
    return cars


# ---------- Fahrerdaten ----------

def ensure_driver_sheet(nickname: str):
    sheet = get_sheet()
    titles = [ws.title for ws in sheet.worksheets()]
    if nickname not in titles:
        ws = sheet.add_worksheet(title=nickname, rows=500, cols=20)
        ws.append_row([
            "Strecke", "Version", "Fahrzeug",
            "Zeit_Soft_s",
            "Zeit_Medium_s", "Medium_Pct",
            "Zeit_Hard_s", "Hard_Pct",
            "Max_Soft_Runden", "Reichweite_70pct",
            "Letzte_Aktualisierung"
        ])
    return sheet.worksheet(nickname)


def get_driver_data(nickname: str, track: str, version: str, car: str) -> dict | None:
    """Gibt gespeicherte Daten für eine bestimmte Strecke+Version+Fahrzeug zurück."""
    sheet = get_sheet()
    titles = [ws.title for ws in sheet.worksheets()]
    if nickname not in titles:
        return None
    ws = sheet.worksheet(nickname)
    rows = ws.get_all_records()
    for row in rows:
        if (row.get("Strecke") == track
                and row.get("Version") == version
                and row.get("Fahrzeug") == car):
            return row
    return None


def get_driver_avg_pct(nickname: str) -> dict:
    """
    Berechnet den Durchschnitt aller gespeicherten Medium_Pct und Hard_Pct
    eines Fahrers über alle Strecken und Fahrzeuge.
    Gibt {"medium_pct": float|None, "hard_pct": float|None} zurück.
    """
    sheet = get_sheet()
    titles = [ws.title for ws in sheet.worksheets()]
    if nickname not in titles:
        return {"medium_pct": None, "hard_pct": None}

    ws = sheet.worksheet(nickname)
    rows = ws.get_all_records()

    medium_vals = []
    hard_vals = []

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
        "hard_pct": round(sum(hard_vals) / len(hard_vals), 3) if hard_vals else None,
    }


def save_driver_data(nickname: str, track: str, version: str, car: str, data: dict):
    """Speichert oder aktualisiert Fahrerdaten. Berechnet Pct-Werte automatisch."""
    ws = ensure_driver_sheet(nickname)
    rows = ws.get_all_records()
    now = datetime.now().strftime("%d.%m.%Y %H:%M")

    soft_s = data["zeit_soft_s"]
    medium_s = data["zeit_medium_s"]
    hard_s = data.get("zeit_hard_s")  # kann None sein wenn Hard deaktiviert

    medium_pct = round((medium_s - soft_s) / soft_s * 100, 3) if medium_s else None
    hard_pct = round((hard_s - soft_s) / soft_s * 100, 3) if hard_s else None

    new_row = [
        track, version, car,
        round(soft_s, 3),
        round(medium_s, 3) if medium_s else "",
        medium_pct if medium_pct is not None else "",
        round(hard_s, 3) if hard_s else "",
        hard_pct if hard_pct is not None else "",
        data["max_soft_runden"],
        data["reichweite_70pct"],
        now
    ]

    for i, row in enumerate(rows, start=2):
        if (row.get("Strecke") == track
                and row.get("Version") == version
                and row.get("Fahrzeug") == car):
            ws.update(f"A{i}:K{i}", [new_row])
            return

    ws.append_row(new_row)
