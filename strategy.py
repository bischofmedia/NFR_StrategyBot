from dataclasses import dataclass

TYRE_SOFT   = "Soft"
TYRE_MEDIUM = "Medium"
TYRE_HARD   = "Hard"


@dataclass
class StrategyResult:
    stints: list
    total_time_s: float
    pit_stops: int
    fuel_stops: list
    description: str
    pole: bool


def soft_lap_times(base_time_s: float, max_runden: int) -> list[float]:
    """
    Degradationskurve für Soft-Reifen (ohne Tankgewicht – wird separat addiert):
    Runde 1: +0.5s (kalt)
    Runden 2 bis 40%: Plateau
    40–70%: leichter Abbau bis +1.0s
    70–100%: starker Abbau bis +3.0s
    """
    times = []
    for i in range(1, max_runden + 1):
        progress = i / max_runden
        if i == 1:
            delta = 0.5
        elif progress <= 0.4:
            delta = 0.0
        elif progress <= 0.7:
            delta = ((progress - 0.4) / 0.3) * 1.0
        else:
            delta = 1.0 + ((progress - 0.7) / 0.3) * 2.0
        times.append(base_time_s + delta)
    return times


def fuel_weight_delta(fuel_left: float, tank_size: float, fuel_weight_s: float) -> float:
    """Zeitaufschlag durch Tankgewicht. Linear: voller Tank = +fuel_weight_s, leer = 0."""
    return (fuel_left / tank_size) * fuel_weight_s


def evaluate_stints(
    stints, soft_times, base_time_medium_s, base_time_hard_s,
    max_soft_runden, fuel_per_lap, start_fuel, tank_size,
    tank_rate_l_per_s, pit_loss_s, pole, verkehr_aufschlag_s, verkehr_runden,
    fuel_weight_s
):
    """
    Wertet eine Stint-Liste aus.
    Berücksichtigt Tankgewicht pro Runde, Verkehrsaufschlag bei Nicht-Pole,
    und plant Tanken automatisch bei Bedarf ein.
    """
    total_time = 0.0
    fuel_left  = start_fuel
    fuel_stops = []
    lap_offset = 0

    for i, (tyre, runden) in enumerate(stints):
        for r in range(runden):
            # Basis-Reifenzeit
            if tyre == TYRE_SOFT:
                t = soft_times[min(r, max_soft_runden - 1)]
            elif tyre == TYRE_MEDIUM:
                t = base_time_medium_s
            else:
                t = base_time_hard_s

            # Tankgewicht: basierend auf aktuellem Füllstand zu Beginn dieser Runde
            t += fuel_weight_delta(fuel_left, tank_size, fuel_weight_s)

            # Sprit für diese Runde verbrauchen
            fuel_left -= fuel_per_lap

            # Verkehrsaufschlag erste Runden bei Nicht-Pole
            if i == 0 and not pole and (lap_offset + r) < verkehr_runden:
                t += verkehr_aufschlag_s

            total_time += t

        if fuel_left < -0.01:
            return None, None, False

        lap_offset += runden

        # Boxenstopp nach diesem Stint (außer letztem)
        if i < len(stints) - 1:
            total_time += pit_loss_s
            next_runden = stints[i + 1][1]
            fuel_needed = next_runden * fuel_per_lap
            if fuel_left < fuel_needed:
                refuel = min(tank_size - fuel_left, tank_size)
                total_time += refuel / tank_rate_l_per_s
                fuel_left  += refuel
                fuel_stops.append(i)
                if fuel_left < fuel_needed - 0.01:
                    return None, None, False

    return total_time, fuel_stops, True


def generate_all_stints(total_laps, available_tyres, max_per_tyre, max_stops=3):
    results = []

    def recurse(laps_left, current):
        if laps_left == 0:
            results.append(list(current))
            return
        if len(current) >= max_stops + 1:
            return
        for tyre in available_tyres:
            max_t = min(max_per_tyre[tyre], laps_left)
            for runden in range(1, max_t + 1):
                current.append((tyre, runden))
                recurse(laps_left - runden, current)
                current.pop()

    recurse(total_laps, [])
    return results


def is_sensible(stints, max_soft_runden, fuel_per_lap, start_fuel):
    """
    Filtert unsinnige Strategien:
    - Gleicher Reifentyp in aufeinanderfolgenden Stints
    - Soft-Stint kürzer als 50% der max Haltbarkeit
    - Erster Stint verbraucht mehr Sprit als vorhanden
    - Erster Stint ist nicht Soft (Soft-Start ist immer optimal)
    - Einzelner kurzer Nicht-Soft-Stint vor einem langen Soft-Stint
    """
    for i in range(len(stints) - 1):
        if stints[i][0] == stints[i+1][0]:
            return False

    # Erster Stint sollte immer Soft sein
    if stints[0][0] != TYRE_SOFT:
        return False

    min_soft = max(1, int(max_soft_runden * 0.5))
    for tyre, runden in stints:
        if tyre == TYRE_SOFT and runden < min_soft:
            return False

    if stints[0][1] * fuel_per_lap > start_fuel:
        return False

    return True


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
    verkehr_aufschlag_s: float = 2.0,
    verkehr_runden: int = 3,
    fuel_weight_s: float = 0.7,
) -> list[StrategyResult]:

    base_time_medium_s = base_time_soft_s * (1 + medium_plus_pct / 100)
    base_time_hard_s   = base_time_soft_s * (1 + hard_plus_pct   / 100)

    max_medium = max_soft_runden * 2
    max_hard   = max_soft_runden * 4

    start_fuel   = tank_size * (start_fuel_pct / 100)
    fuel_per_lap = start_fuel / reichweite_70pct

    soft_times = soft_lap_times(base_time_soft_s, max_soft_runden)

    available_tyres = [TYRE_SOFT, TYRE_MEDIUM]
    if hard_enabled:
        available_tyres.append(TYRE_HARD)

    max_per_tyre = {
        TYRE_SOFT:   max_soft_runden,
        TYRE_MEDIUM: max_medium,
        TYRE_HARD:   max_hard,
    }

    all_stints = generate_all_stints(total_laps, available_tyres, max_per_tyre)

    results = []
    seen = set()

    for stints in all_stints:
        if soft_required and not any(t == TYRE_SOFT for t, _ in stints):
            continue

        if not is_sensible(stints, max_soft_runden, fuel_per_lap, start_fuel):
            continue

        # Duplikat-Check: gleiche Tyre-Summen + gleiche Stopp-Anzahl
        tyre_totals = {}
        for tyre, runden in stints:
            tyre_totals[tyre] = tyre_totals.get(tyre, 0) + runden
        sig = (len(stints), frozenset(tyre_totals.items()))
        if sig in seen:
            continue
        seen.add(sig)

        total_time, fuel_stops, valid = evaluate_stints(
            stints, soft_times, base_time_medium_s, base_time_hard_s,
            max_soft_runden, fuel_per_lap, start_fuel, tank_size,
            tank_rate_l_per_s, pit_loss_s, pole, verkehr_aufschlag_s,
            verkehr_runden, fuel_weight_s
        )

        if not valid:
            continue

        description = " → ".join(f"{runden}x {t}" for t, runden in stints)
        results.append(StrategyResult(
            stints=stints,
            total_time_s=total_time,
            pit_stops=len(stints) - 1,
            fuel_stops=fuel_stops,
            description=description,
            pole=pole
        ))

    results.sort(key=lambda r: r.total_time_s)
    return results[:10]


def format_time(seconds: float) -> str:
    mins = int(seconds // 60)
    secs = seconds % 60
    return f"{mins}:{secs:06.3f}"


def get_top_strategies(results: list[StrategyResult], top_n: int = 3) -> list[StrategyResult]:
    return results[:top_n]

# Patch: Hilfsfunktion um ersten Stint zu validieren
def first_stint_is_soft(stints):
    return stints[0][0] == "Soft"
