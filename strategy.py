import math
from dataclasses import dataclass
from itertools import product as iterproduct

TYRE_SOFT   = "Soft"
TYRE_MEDIUM = "Medium"
TYRE_HARD   = "Hard"

VERKEHR_MALUS = {0: 2.0, 1: 1.5, 2: 1.0}


@dataclass
class StrategyResult:
    stints: list
    total_time_s: float
    pit_stops: int
    fuel_stops: list
    description: str
    pole: bool


# ─────────────────────────────────────────────
# Degradationskurven
# ─────────────────────────────────────────────

def _deg_curve(base: float, max_laps: int) -> list[float]:
    times = []
    for i in range(1, max_laps + 1):
        p = i / max_laps
        if i == 1:      delta = 0.5
        elif p <= 0.4:  delta = 0.0
        elif p <= 0.7:  delta = ((p - 0.4) / 0.3) * 1.0
        else:           delta = 1.0 + ((p - 0.7) / 0.3) * 2.0
        times.append(base + delta)
    return times

def soft_lap_times(base, max_laps):   return _deg_curve(base, max_laps)
def medium_lap_times(base, max_laps): return _deg_curve(base, max_laps)
def hard_lap_times(base, max_laps):   return _deg_curve(base, max_laps)

def fuel_weight_delta(fuel, tank_size, fw_s):
    return (fuel / tank_size) * fw_s


# ─────────────────────────────────────────────
# Pit-Window Validierung
# ─────────────────────────────────────────────

def parse_pit_windows(settings: dict) -> list[tuple]:
    """
    Liest Pit-Windows aus Settings.
    Gibt Liste von (open_s, closed_s) Tuples zurück.
    Leere Liste wenn pit_windows != TRUE.
    """
    if settings.get("pit_windows", "FALSE").upper() != "TRUE":
        return []

    windows = []
    for i in range(1, 10):
        open_key   = f"pit_open_{i}"
        closed_key = f"pit_closed_{i}"
        open_val   = settings.get(open_key, "").strip()
        closed_val = settings.get(closed_key, "").strip()
        if not open_val or not closed_val:
            break
        try:
            windows.append((float(open_val), float(closed_val)))
        except ValueError:
            break
    return windows


def pit_time_valid(pit_entry_time_s: float, windows: list) -> bool:
    """
    Prüft ob ein Boxenstopp zur gegebenen Rennzeit erlaubt ist.
    Wenn keine Windows definiert: immer erlaubt.
    """
    if not windows:
        return True
    return any(o <= pit_entry_time_s < c for o, c in windows)


# ─────────────────────────────────────────────
# Stint-Bewertung
# ─────────────────────────────────────────────

def evaluate_stints(
    stints, tyre_times, max_laps,
    fuel_per_lap, start_fuel, tank_size,
    tank_rate_l_per_s, pit_loss_s, pole, fuel_weight_s,
    pit_windows: list = None,
):
    if pit_windows is None:
        pit_windows = []

    total     = 0.0
    fuel      = start_fuel
    f_stops   = []
    lap_total = 0

    for i, (tyre, laps) in enumerate(stints):
        for r in range(laps):
            idx = min(r, max_laps[tyre] - 1)
            t   = tyre_times[tyre][idx]
            t  += fuel_weight_delta(fuel, tank_size, fuel_weight_s)
            fuel -= fuel_per_lap

            # Verkehrsmalus bei Nicht-Pole im ersten Stint
            if i == 0 and not pole:
                malus = VERKEHR_MALUS.get(lap_total, 0.0)
                if malus > 0:
                    if tyre == TYRE_SOFT:
                        t += malus
                    else:
                        soft_t = tyre_times[TYRE_SOFT][min(r, max_laps[TYRE_SOFT]-1)]
                        soft_t += fuel_weight_delta(fuel + fuel_per_lap, tank_size, fuel_weight_s)
                        soft_t += malus
                        t = max(t, soft_t)

            if fuel < -0.01:
                return None, None, False

            total     += t
            lap_total += 1

        # Boxenstopp nach diesem Stint (außer letztem)
        if i < len(stints) - 1:
            # Pit-Window prüfen: Einfahrt bei aktueller Rennzeit
            if pit_windows and not pit_time_valid(total, pit_windows):
                return None, None, False  # Stopp außerhalb Fenster → verwerfen

            total += pit_loss_s
            next_laps   = stints[i + 1][1]
            fuel_needed = next_laps * fuel_per_lap
            if fuel < fuel_needed:
                refuel = min(fuel_needed - fuel, tank_size - fuel)
                total += refuel / tank_rate_l_per_s
                fuel  += refuel
                f_stops.append(i)
                if fuel < fuel_needed - 0.01:
                    return None, None, False

    return total, f_stops, True


# ─────────────────────────────────────────────
# Typen-Sequenzen generieren
# ─────────────────────────────────────────────

def _tyre_sequences(num_stints: int, available: list, required: list,
                    tyre_change_required: bool) -> list:
    """
    Alle gültigen Reifentyp-Sequenzen für num_stints Stints.
    - Alle Pflicht-Reifen müssen vorkommen
    - Wenn tyre_change_required: mind. 2 verschiedene Typen
    """
    seqs = []
    for combo in iterproduct(available, repeat=num_stints):
        # Pflicht-Reifen müssen alle vorkommen
        if any(r not in combo for r in required):
            continue
        # Reifenwechsel Pflicht: mind. 2 verschiedene Typen
        if tyre_change_required and len(set(combo)) < 2:
            continue
        seqs.append(combo)
    return seqs


# ─────────────────────────────────────────────
# Optimale Rundenverteilung für eine Sequenz
# ─────────────────────────────────────────────

def _optimal_laps_for_sequence(
    tyre_seq, total_laps, max_laps, min_laps_pct,
    fuel_per_lap, start_fuel, tank_size,
    tank_rate_l_per_s, pit_loss_s, pole, fuel_weight_s,
    tyre_times, pit_windows,
) -> StrategyResult | None:

    n = len(tyre_seq)
    min_per_stint = []
    max_per_stint = []
    for tyre in tyre_seq:
        max_t = max_laps[tyre]
        min_t = max(1, int(max_t * min_laps_pct.get(tyre, 0.0)))
        min_per_stint.append(min_t)
        max_per_stint.append(max_t)

    best = None

    def recurse(idx, laps_left, current):
        nonlocal best

        if idx == n - 1:
            if laps_left < min_per_stint[idx] or laps_left > max_per_stint[idx]:
                return
            stints = list(zip(tyre_seq, current + [laps_left]))
            t, fs, v = evaluate_stints(
                stints, tyre_times, max_laps,
                fuel_per_lap, start_fuel, tank_size,
                tank_rate_l_per_s, pit_loss_s, pole, fuel_weight_s,
                pit_windows,
            )
            if v and (best is None or t < best.total_time_s):
                desc = " → ".join(f"{l}x {ty}" for ty, l in stints)
                best = StrategyResult(
                    stints=stints, total_time_s=t,
                    pit_stops=n-1, fuel_stops=fs,
                    description=desc, pole=pole,
                )
            return

        remaining_stints = n - idx - 1
        for laps in range(min_per_stint[idx], max_per_stint[idx] + 1):
            remaining = laps_left - laps
            if remaining < remaining_stints:
                break
            recurse(idx + 1, remaining, current + [laps])

    recurse(0, total_laps, [])
    return best


# ─────────────────────────────────────────────
# Hauptfunktion
# ─────────────────────────────────────────────

def calculate_strategies(
    total_laps: int,
    base_time_soft_s: float,
    medium_plus_pct: float,
    hard_plus_pct: float,
    max_soft_runden: int,
    reichweite_70pct: int,
    tank_size: float,
    tank_rate_l_per_s: float,
    pit_loss_s: float,
    start_fuel_pct: float,
    soft_required: bool,
    pole: bool,
    hard_enabled: bool = True,
    verkehr_aufschlag_s: float = 2.0,  # Legacy
    verkehr_runden: int = 3,           # Legacy
    fuel_weight_s: float = 0.7,
    # Neue Parameter
    medium_required: bool = False,
    hard_required: bool = False,
    soft_allowed: bool = True,
    medium_allowed: bool = True,
    hard_allowed: bool = True,
    tyre_change_required: bool = True,
    pit_windows: list = None,
) -> list[StrategyResult]:

    if pit_windows is None:
        pit_windows = []

    base_medium = base_time_soft_s * (1 + medium_plus_pct / 100)
    base_hard   = base_time_soft_s * (1 + hard_plus_pct   / 100)

    max_laps = {
        TYRE_SOFT:   max_soft_runden,
        TYRE_MEDIUM: max_soft_runden * 2,
        TYRE_HARD:   max_soft_runden * 4,
    }

    tyre_times = {
        TYRE_SOFT:   soft_lap_times(base_time_soft_s, max_laps[TYRE_SOFT]),
        TYRE_MEDIUM: medium_lap_times(base_medium,    max_laps[TYRE_MEDIUM]),
        TYRE_HARD:   hard_lap_times(base_hard,        max_laps[TYRE_HARD]),
    }

    start_fuel   = tank_size * (start_fuel_pct / 100)
    fuel_per_lap = start_fuel / reichweite_70pct

    # Erlaubte Reifen
    available = []
    if soft_allowed:   available.append(TYRE_SOFT)
    if medium_allowed: available.append(TYRE_MEDIUM)
    if hard_allowed and hard_enabled: available.append(TYRE_HARD)

    if not available:
        return []

    # Pflicht-Reifen
    required = []
    if soft_required   and soft_allowed:                     required.append(TYRE_SOFT)
    if medium_required and medium_allowed:                   required.append(TYRE_MEDIUM)
    if hard_required   and hard_allowed and hard_enabled:    required.append(TYRE_HARD)

    # Minimale Stint-Länge pro Reifen (als Anteil der max Haltbarkeit)
    # Soft: mind. 50% um Plateau zu nutzen; Medium/Hard: mind. 1 Runde
    min_laps_pct = {
        TYRE_SOFT:   0.5,
        TYRE_MEDIUM: 0.0,
        TYRE_HARD:   0.0,
    }

    # Reifen mit kürzester Haltbarkeit bestimmt max Stopps
    shortest_max = min(max_laps[t] for t in available)
    max_stops = math.ceil(total_laps / shortest_max) - 1
    max_stops = max(1, min(max_stops, 5))

    results = []

    for num_stops in range(0, max_stops + 1):
        num_stints = num_stops + 1
        sequences  = _tyre_sequences(num_stints, available, required,
                                     tyre_change_required)

        for seq in sequences:
            best = _optimal_laps_for_sequence(
                seq, total_laps, max_laps, min_laps_pct,
                fuel_per_lap, start_fuel, tank_size,
                tank_rate_l_per_s, pit_loss_s, pole, fuel_weight_s,
                tyre_times, pit_windows,
            )
            if best:
                results.append(best)

    results.sort(key=lambda r: r.total_time_s)
    return results


def format_time(seconds: float) -> str:
    mins = int(seconds // 60)
    secs = seconds % 60
    return f"{mins}:{secs:06.3f}"

def get_top_strategies(results, top_n=3):
    return results[:top_n]
