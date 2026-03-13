from strategy import (
    StrategyResult, soft_lap_times, fuel_weight_delta,
    TYRE_SOFT, TYRE_MEDIUM, TYRE_HARD
)

TYRE_EMOJI = {TYRE_SOFT: "🔴", TYRE_MEDIUM: "🟡", TYRE_HARD: "⚪"}


def fmt_time(s: float) -> str:
    mins = int(s // 60)
    secs = s % 60
    return f"{mins}:{secs:05.2f}"


def fmt_fuel(f: float) -> str:
    return f"{f:5.1f}l"


def build_lap_table(
    strategies: dict,
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
) -> str:
    """
    Erstellt eine tabellarische Darstellung aller Strategien nebeneinander.
    Gibt einen String zurück der in einen Discord Code-Block passt.
    """
    base_medium = base_soft_s * (1 + medium_plus_pct / 100)
    base_hard   = base_soft_s * (1 + hard_plus_pct   / 100)
    soft_times  = soft_lap_times(base_soft_s, max_soft_runden)

    # Nur Strategien die nicht None sind
    active = {k: v for k, v in strategies.items() if v is not None}
    if not active:
        return "Keine Strategien verfügbar."

    labels = list(active.keys())
    results = list(active.values())
    n = len(labels)

    # Runden-Daten für jede Strategie vorberechnen
    def get_lap_data(result: StrategyResult):
        rows = []
        fuel = start_fuel
        lap  = 0
        stint_idx = 0
        lap_in_stint = 0

        for si, (tyre, runden) in enumerate(result.stints):
            for r in range(runden):
                lap += 1

                if tyre == TYRE_SOFT:
                    base_t = soft_times[min(r, max_soft_runden - 1)]
                elif tyre == TYRE_MEDIUM:
                    base_t = base_medium
                else:
                    base_t = base_hard

                weight_t = fuel_weight_delta(fuel, tank_size, fuel_weight_s)
                lap_t = base_t + weight_t
                fuel -= fuel_per_lap

                rows.append({
                    "lap": lap,
                    "tyre": tyre,
                    "time": lap_t,
                    "fuel": max(fuel, 0),
                    "pit": False,
                    "pit_time": 0,
                    "refuel": 0,
                })

            # Boxenstopp nach diesem Stint?
            if si < len(result.stints) - 1:
                next_runden = result.stints[si + 1][1]
                fuel_needed = next_runden * fuel_per_lap
                refuel = 0
                pit_time = pit_loss_s
                if fuel < fuel_needed:
                    refuel = min(tank_size - fuel, tank_size)
                    pit_time += refuel / tank_rate_l_per_s
                    fuel += refuel
                # Markiere letzte Runde des Stints als Pit-Runde
                rows[-1]["pit"] = True
                rows[-1]["pit_time"] = pit_time
                rows[-1]["refuel"] = refuel

        return rows

    all_data = [get_lap_data(r) for r in results]
    total_laps = len(all_data[0])

    # Spaltenbreiten
    col_w = 22  # Breite pro Strategie-Spalte
    lap_w = 5

    # Kurzbezeichnungen für Header
    short_labels = []
    for label, result in active.items():
        stint_str = " + ".join(
            f"{runden}{t[0]}" for t, runden in
            [(t, r) for t, r in result.stints]
        )
        short_labels.append(f"{label[:12]}")

    # Header
    header1 = " " * lap_w + " │ "
    header2 = "Runde" + " │ "
    sep     = "─" * lap_w + "─┼─"

    for i, (label, result) in enumerate(active.items()):
        stint_str = "→".join(f"{r}{t[0]}" for t, r in result.stints)
        total_str = fmt_time(result.total_time_s)
        col_title = f"{label} ({total_str})"
        header1 += col_title[:col_w].ljust(col_w)
        header2 += stint_str[:col_w].ljust(col_w)
        sep     += "─" * col_w
        if i < n - 1:
            header1 += " │ "
            header2 += " │ "
            sep     += "─┼─"

    lines = [header1, header2, sep]

    # Zeilen
    for lap_i in range(total_laps):
        lap_num = lap_i + 1
        row = f"{lap_num:>4}  │ "

        for i, data in enumerate(all_data):
            if lap_i >= len(data):
                row += " " * col_w
            else:
                d = data[lap_i]
                tyre_e = TYRE_EMOJI[d["tyre"]]
                cell = f"{tyre_e}{fmt_time(d['time'])} {fmt_fuel(d['fuel'])}"

                if d["pit"]:
                    pit_str = f"  🔧 +{d['pit_time']:.0f}s"
                    if d["refuel"] > 0:
                        pit_str += f" ⛽{d['refuel']:.0f}l"
                    cell += pit_str

                row += cell[:col_w].ljust(col_w)

            if i < n - 1:
                row += " │ "

        lines.append(row)

    # Gesamtzeiten
    lines.append(sep)
    total_row = "TOTAL" + " │ "
    for i, result in enumerate(results):
        total_row += fmt_time(result.total_time_s).ljust(col_w)
        if i < n - 1:
            total_row += " │ "
    lines.append(total_row)

    return "\n".join(lines)
