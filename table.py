from strategy import (
    StrategyResult, soft_lap_times, medium_lap_times, fuel_weight_delta,
    TYRE_SOFT, TYRE_MEDIUM, TYRE_HARD
)

TYRE_EMOJI = {TYRE_SOFT: "🔴", TYRE_MEDIUM: "🟡", TYRE_HARD: "⚪"}
TYRE_NAME  = {TYRE_SOFT: "Soft", TYRE_MEDIUM: "Medium", TYRE_HARD: "Hard"}


def fmt_time(s: float) -> str:
    mins = int(s // 60)
    secs = s % 60
    return f"{mins}:{secs:05.2f}"


def build_single_column(
    label: str,
    result: StrategyResult,
    base_soft_s: float,
    medium_plus_pct: float,
    hard_plus_pct: float,
    max_soft_runden: int,
    fuel_per_lap: float,
    start_fuel: float,
    tank_size: float,
    tank_rate_l_per_s: float,
    pit_loss_s: float,
    fuel_weight_s: float,
    pole: bool = True,
    verkehr_aufschlag_s: float = 2.0,
    verkehr_runden: int = 3,
) -> str:
    """Einzelne Strategie als kompakte, mobile-freundliche Tabelle."""
    base_medium  = base_soft_s * (1 + medium_plus_pct / 100)
    base_hard    = base_soft_s * (1 + hard_plus_pct   / 100)
    max_medium   = max_soft_runden * 2
    soft_times   = soft_lap_times(base_soft_s, max_soft_runden)
    medium_times = medium_lap_times(base_medium, max_medium)

    # Runden-Daten berechnen
    rows = []
    fuel      = start_fuel
    lap_total = 0
    for si, (tyre, runden) in enumerate(result.stints):
        for r in range(runden):
            if tyre == TYRE_SOFT:
                base_t = soft_times[min(r, max_soft_runden - 1)]
            elif tyre == TYRE_MEDIUM:
                base_t = medium_times[min(r, max_medium - 1)]
            else:
                base_t = base_hard

            lap_t = base_t + fuel_weight_delta(fuel, tank_size, fuel_weight_s)

            # Verkehrsmalus bei Nicht-Pole in den ersten Runden
            traffic_note = ""
            if si == 0 and not pole and lap_total < verkehr_runden:
                if tyre == TYRE_SOFT:
                    lap_t += verkehr_aufschlag_s
                    traffic_note = f" (+{verkehr_aufschlag_s:.0f}s Verkehr)"
                else:
                    soft_with_traffic = soft_times[min(r, max_soft_runden-1)] + verkehr_aufschlag_s
                    soft_with_traffic += fuel_weight_delta(fuel, tank_size, fuel_weight_s)
                    if soft_with_traffic > lap_t:
                        lap_t = soft_with_traffic
                        traffic_note = f" (Verkehr, ={verkehr_aufschlag_s:.0f}s Malus)"

            fuel      -= fuel_per_lap
            lap_total += 1

            rows.append({
                "tyre":         tyre,
                "time":         lap_t,
                "fuel":         max(fuel, 0),
                "traffic_note": traffic_note,
                "pit":          False,
                "pit_time":     0,
                "refuel":       0,
            })

        # Boxenstopp
        if si < len(result.stints) - 1:
            remaining_laps = sum(n for _, n in result.stints[si+1:])
            fuel_needed    = remaining_laps * fuel_per_lap
            refuel   = 0
            pit_time = pit_loss_s
            if fuel < fuel_needed:
                refuel    = min(fuel_needed - fuel, tank_size - fuel)
                pit_time += refuel / tank_rate_l_per_s
                fuel     += refuel
            rows[-1]["pit"]      = True
            rows[-1]["pit_time"] = pit_time
            rows[-1]["refuel"]   = refuel

    # Header
    stint_str   = " → ".join(f"{TYRE_EMOJI[t]}{n}" for t, n in result.stints)
    total_str   = fmt_time(result.total_time_s)
    lines = [
        f"{label}: {total_str}",
        f"{stint_str}",
        "─" * 26,
        f"{'Rd':<4} {'Reifen':<8} {'Zeit':<9} {'Tank':<7}",
        "─" * 26,
    ]

    for i, d in enumerate(rows):
        lap_num = i + 1
        emoji   = TYRE_EMOJI[d["tyre"]]
        name    = TYRE_NAME[d["tyre"]][:6]
        time_s  = fmt_time(d["time"])
        fuel_s  = f"{d['fuel']:.1f}l"
        traffic = d.get("traffic_note", "")
        line    = f"{lap_num:<4} {emoji}{name:<7} {time_s:<9} {fuel_s:<7}{traffic}"
        lines.append(line)

        if d["pit"]:
            pit_s = f"🔧 BOX +{d['pit_time']:.0f}s"
            if d["refuel"] > 0:
                pit_s += f"  ⛽+{d['refuel']:.0f}l"
            lines.append(f"     {pit_s}")

    lines.append("─" * 26)
    lines.append(f"{'TOTAL':<4} {' ':<8} {total_str}")

    return "\n".join(lines)
