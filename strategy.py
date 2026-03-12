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
    Degradationskurve für Soft-Reifen:
    Runde 1: +0.5s (kalt)
    Runden 2 bis 40%: Plateau
    40-70%: leichter Abbau bis +1.0s
    70-100%: starker Abbau bis +3.0s
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
) -> list[StrategyResult]:

    base_time_medium_s = base_time_soft_s * (1 + medium_plus_pct / 100)
    base_time_hard_s   = base_time_soft_s * (1 + hard_plus_pct   / 100)

    max_medium = max_soft_runden * 2
    max_hard   = max_soft_runden * 4

    start_fuel    = tank_size * (start_fuel_pct / 100)
    fuel_per_lap  = start_fuel / reichweite_70pct

    soft_times = soft_lap_times(base_time_soft_s, max_soft_runden)

    # Nur aktivierte Reifentypen
    available_tyres = [TYRE_SOFT, TYRE_MEDIUM]
    if hard_enabled:
        available_tyres.append(TYRE_HARD)

    def stint_time(tyre, runden, is_first, lap_offset=0):
        total = 0.0
        for r in range(runden):
            if tyre == TYRE_SOFT:
                t = soft_times[min(r, max_soft_runden - 1)]
            elif tyre == TYRE_MEDIUM:
                t = base_time_medium_s
            else:
                t = base_time_hard_s
            if is_first and not pole and (lap_offset + r) < verkehr_runden:
                t += verkehr_aufschlag_s
            total += t
        return total

    def generate_stints(laps_remaining, max_stops=4):
        results = []
        def recurse(laps_left, current):
            if laps_left == 0:
                results.append(list(current))
                return
            if len(current) > max_stops + 1:
                return
            for tyre in available_tyres:
                max_stint = {
                    TYRE_SOFT:   max_soft_runden,
                    TYRE_MEDIUM: max_medium,
                    TYRE_HARD:   max_hard,
                }[tyre]
                for runden in range(1, min(max_stint, laps_left) + 1):
                    current.append((tyre, runden))
                    recurse(laps_left - runden, current)
                    current.pop()
        recurse(laps_remaining, [])
        return results

    all_stints = generate_stints(total_laps)
    results = []

    for stints in all_stints:
        if soft_required and not any(t == TYRE_SOFT for t, _ in stints):
            continue

        total_time  = 0.0
        fuel_left   = start_fuel
        fuel_stops  = []
        lap_offset  = 0
        valid       = True

        for i, (tyre, runden) in enumerate(stints):
            total_time += stint_time(tyre, runden, i == 0, lap_offset)
            fuel_left  -= runden * fuel_per_lap

            if fuel_left < -0.001:
                valid = False
                break

            lap_offset += runden

            if i < len(stints) - 1:
                total_time += pit_loss_s
                next_tyre, next_runden = stints[i + 1]
                fuel_needed = next_runden * fuel_per_lap
                if fuel_left < fuel_needed:
                    refuel = min(tank_size - fuel_left, tank_size)
                    total_time += refuel / tank_rate_l_per_s
                    fuel_left  += refuel
                    fuel_stops.append(i)

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
