import discord
from discord.ui import Modal, TextInput, View, Button
from strategy import (
    calculate_strategies, format_time, parse_pit_windows,
    build_verkehr_malus, TYRE_SOFT, TYRE_MEDIUM, TYRE_HARD
)
from sheets import (
    get_driver_data, save_driver_data, get_settings,
    get_track_data, get_driver_avg_pct, DEFAULT_LEAGUE
)
from gemini import get_gemini_strategies, fallback_strategies
from table import build_single_column

TYRE_EMOJI = {TYRE_SOFT: "🔴", TYRE_MEDIUM: "🟡", TYRE_HARD: "⚪"}


def parse_time(s: str) -> float:
    s = s.strip().replace(",", ".")
    if ":" in s:
        m, sec = s.split(":", 1)
        return int(m) * 60 + float(sec)
    return float(s)

def seconds_to_display(s: float) -> str:
    mins = int(s // 60); secs = s % 60
    return f"{mins}:{secs:06.3f}"

def apply_pct(base: float, pct: float) -> float:
    return round(base * (1 + pct / 100), 3)

def to_float(v):
    try: return float(str(v).replace(",", "."))
    except: return None


# ─────────────────────────────────────────────
# Kernlogik
# ─────────────────────────────────────────────

async def calculate_and_post(channel, nickname, track, version, brand, model,
                              total_laps, data, settings, league=DEFAULT_LEAGUE,
                              requester_id: int = 0):
    car_display = f"{brand} {model}"

    def s(key, default): return settings.get(key, str(default))
    def b(key, default): return s(key, default).upper() == "TRUE"
    def f(key, default): return float(s(key, default))
    def i_(key, default): return int(float(s(key, default)))

    soft_allowed     = b("soft_tyre_allowed",     True)
    medium_allowed   = b("medium_tyre_allowed",   True)
    hard_allowed     = b("hard_tyre_allowed",     False)
    soft_required    = b("soft_stint_required",   True)
    medium_required  = b("medium_stint_required", False)
    hard_required    = b("hard_stint_required",   False)
    tyre_change_req  = b("tyre_change_required",  True)
    tank_size        = f("tank_size_l",           100)
    tank_rate        = f("tank_rate_l_per_s",     5)
    start_pct        = f("start_fuel_pct",        70)
    fw_s             = f("fuel_weight_s",         0.7)
    gemini_aktiv     = b("gemini",                True)

    track_data = get_track_data(track, version)
    pit_loss   = track_data["pit_loss_s"] or f("pit_loss_s", 25)
    pit_windows = parse_pit_windows(settings)

    soft_s   = to_float(data["zeit_soft_s"])
    medium_s = to_float(data["zeit_medium_s"])
    hard_s   = to_float(data.get("zeit_hard_s")) if hard_allowed else None

    avg = get_driver_avg_pct(nickname, league)

    # Plausibilitätscheck: Medium darf max 10% über Soft liegen, sonst Fallback
    if medium_s is None or medium_s <= 0 or medium_s > soft_s * 1.10:
        if medium_s and medium_s > soft_s * 1.10:
            print(f"[Warnung] Medium-Zeit {medium_s:.3f}s unplausibel (Soft: {soft_s:.3f}s) – verwende Fallback")
        med_pct_val = avg["medium_pct"] if avg["medium_pct"] is not None else f("medium_default_pct", 1.0)
        medium_s = round(soft_s * (1 + med_pct_val / 100), 3)

    # Plausibilitätscheck: Hard-Zeit darf max 20% über Soft liegen, sonst Fallback
    if hard_allowed and (hard_s is None or hard_s <= 0 or hard_s > soft_s * 1.20):
        if hard_s and hard_s > soft_s * 1.20:
            print(f"[Warnung] Hard-Zeit {hard_s:.3f}s unplausibel (Soft: {soft_s:.3f}s) – verwende Fallback")
        hard_pct_val = avg["hard_pct"] if avg["hard_pct"] is not None else f("hard_default_pct", 2.5)
        hard_s = round(soft_s * (1 + hard_pct_val / 100), 3)

    medium_pct = (medium_s - soft_s) / soft_s * 100
    hard_pct   = (hard_s - soft_s)   / soft_s * 100 if hard_s else 0

    start_fuel   = tank_size * (start_pct / 100)
    fuel_per_lap = start_fuel / int(str(data["reichweite_70pct"]).replace(",",".").split(".")[0])
    max_soft     = int(str(data["max_soft_runden"]).replace(",",".").split(".")[0])

    common = dict(
        total_laps=total_laps,
        base_time_soft_s=soft_s,
        medium_plus_pct=medium_pct,
        hard_plus_pct=hard_pct,
        max_soft_runden=max_soft,
        reichweite_70pct=int(str(data["reichweite_70pct"]).replace(",",".").split(".")[0]),
        tank_size=tank_size,
        tank_rate_l_per_s=tank_rate,
        pit_loss_s=pit_loss,
        start_fuel_pct=start_pct,
        soft_required=soft_required,
        fuel_weight_s=fw_s,
        medium_required=medium_required,
        hard_required=hard_required,
        soft_allowed=soft_allowed,
        medium_allowed=medium_allowed,
        hard_allowed=hard_allowed,
        tyre_change_required=tyre_change_req,
        pit_windows=pit_windows,
    )

    vm = build_verkehr_malus(settings)
    all_pole    = calculate_strategies(**common, pole=True, verkehr_malus=vm)
    all_no_pole = calculate_strategies(**common, pole=False, verkehr_malus=vm)

    # Gemini oder Fallback
    gemini_result = None
    if gemini_aktiv:
        gemini_result = get_gemini_strategies(
            all_results_pole=all_pole,
            all_results_no_pole=all_no_pole,
            track=track, version=version, car=car_display,
            total_laps=total_laps,
            base_soft_s=soft_s, medium_pct=medium_pct, hard_pct=hard_pct,
            max_soft_runden=max_soft,
            reichweite=int(str(data["reichweite_70pct"]).replace(",",".").split(".")[0]),
            tank_size=tank_size, start_fuel_pct=start_pct,
            pit_loss=pit_loss, fuel_weight_s=fw_s,
        )

    used_gemini = gemini_result is not None
    if not used_gemini:
        gemini_result = fallback_strategies(all_pole, all_no_pole)

    def best_by_stops_from_pool(results, stops):
        return next((r for r in sorted(results, key=lambda x: x.total_time_s)
                     if r.pit_stops == stops), None)

    strategies = gemini_result["strategies"]
    reasonings = gemini_result["reasonings"]
    overall    = gemini_result.get("overall", "")

    # Fehlende Slots auffüllen
    all_labels = ["Pole – Variante 1", "Pole – Variante 2",
                  "Nicht-Pole – Variante 1", "Nicht-Pole – Variante 2"]
    for i, label in enumerate(all_labels):
        if strategies.get(label) is None:
            pool = all_pole if "Nicht-Pole" not in label else all_no_pole
            idx  = 1 if "Variante 2" in label else 0
            sorted_pool = sorted(pool, key=lambda x: x.total_time_s)
            if idx < len(sorted_pool):
                strategies[label] = sorted_pool[idx]
                if label not in reasonings:
                    reasonings[label] = sorted_pool[idx].description

    # Fünfte Variante: schnellste Mono-Reifen-Strategie (falls nicht schon vorhanden)
    def get_fastest_single_tyre(results, allowed, required):
        """Schnellste Strategie die nur einen Reifentyp verwendet."""
        # Reifen-Priorität: schnellster zuerst (Soft > Medium > Hard)
        priority = [TYRE_SOFT, TYRE_MEDIUM, TYRE_HARD]
        for tyre in priority:
            if tyre not in allowed:
                continue
            if required and tyre not in required:
                # Pflicht-Reifen vorhanden aber dieser nicht dabei → überspringen
                pass
            for r in results:
                if all(t == tyre for t, _ in r.stints):
                    return r
        return None

    # Fünfte Variante: schnellste Mono-Reifen-Strategie, je Pole + Nicht-Pole
    if not tyre_change_req:
        allowed_tyres = []
        if soft_allowed:   allowed_tyres.append(TYRE_SOFT)
        if medium_allowed: allowed_tyres.append(TYRE_MEDIUM)
        if hard_allowed:   allowed_tyres.append(TYRE_HARD)
        required_tyres = []
        if soft_required:   required_tyres.append(TYRE_SOFT)
        if medium_required: required_tyres.append(TYRE_MEDIUM)
        if hard_required:   required_tyres.append(TYRE_HARD)

        # Getrennt prüfen: Pole-Descs und Nicht-Pole-Descs separat
        pole_descs    = {r.description for k, r in strategies.items() if r and "Nicht-Pole" not in k}
        nopole_descs  = {r.description for k, r in strategies.items() if r and "Nicht-Pole" in k}

        best_mono_pole   = get_fastest_single_tyre(all_pole,    allowed_tyres, required_tyres)
        best_mono_nopole = get_fastest_single_tyre(all_no_pole, allowed_tyres, required_tyres)

        def mono_reasoning(mono_result, best_result):
            """Begründung abhängig von Zeitdifferenz zur schnellsten Strategie."""
            # Reifentyp der Mono-Strategie ermitteln
            tyre = mono_result.stints[0][0] if mono_result.stints else "Soft"
            if best_result is None:
                return f"Schnellste Strategie nur mit {tyre}-Reifen."
            diff = mono_result.total_time_s - best_result.total_time_s
            def fmt(s): return f"{s:.1f}s" if s < 60 else f"{int(s//60)}:{s%60:04.1f}min"
            if diff <= 0:
                return f"Nur {tyre}-Reifen – schnellste Option insgesamt."
            elif diff < 5:
                return f"Nur {tyre}-Reifen, {fmt(diff)} langsamer als Variante 1. Kein Wechsel auf anderen Reifentyp nötig."
            else:
                return f"Nur {tyre}-Reifen – zur Info: {fmt(diff)} langsamer als die empfohlene Strategie."

        best_pole_result   = strategies.get("Pole – Variante 1")
        best_nopole_result = strategies.get("Nicht-Pole – Variante 1")

        if best_mono_pole and best_mono_pole.description not in pole_descs:
            strategies["Pole – Schnellster Reifen"]       = best_mono_pole
            reasonings["Pole – Schnellster Reifen"]       = mono_reasoning(best_mono_pole, best_pole_result)
        if best_mono_nopole and best_mono_nopole.description not in nopole_descs:
            strategies["Nicht-Pole – Schnellster Reifen"] = best_mono_nopole
            reasonings["Nicht-Pole – Schnellster Reifen"] = mono_reasoning(best_mono_nopole, best_nopole_result)

    # Embed
    from sheets import VALID_LEAGUES
    league_names = {"rtc": "RTC", "awl": "AWL", "gtfun": "GTFUN"}
    league_display = league_names.get(league, league.upper())
    track_display  = f"{track} – {version}" if version else track
    ai_label       = "🤖 KI-Analyse" if used_gemini else "📊 Analyse"

    pit_window_str = ""
    if pit_windows:
        windows_fmt = ", ".join(f"{int(o//60)}:{int(o%60):02d}–{int(c//60)}:{int(c%60):02d}"
                                for o, c in pit_windows)
        pit_window_str = f"\n🪟 Boxenfenster: {windows_fmt}"

    # Fahrzeugdaten für Embed aufbereiten
    def fmts(v):
        fv = to_float(v)
        return seconds_to_display(fv) if fv and fv > 0 else None

    tyre_parts = []
    if soft_allowed:
        t = fmts(data.get("zeit_soft_s"))
        if t: tyre_parts.append(f"🔴 {t}")
    if medium_allowed:
        t = fmts(medium_s)  # bereits bereinigt
        if t: tyre_parts.append(f"🟡 {t}")
    if hard_allowed:
        t = fmts(hard_s)    # bereits bereinigt
        if t: tyre_parts.append(f"⚪ {t}")

    tyre_line = "  ".join(tyre_parts) if tyre_parts else "–"

    max_soft_disp = str(max_soft)
    reich_disp    = str(int(str(data["reichweite_70pct"]).replace(",",".").split(".")[0]))

    # Startsprit-Prozent aus Settings für Reichweiten-Label
    start_pct_disp = int(float(settings.get("start_fuel_pct", 70)))

    data_line = (
        f"{tyre_line}\n"
        f"Maximale Runden Soft: **{max_soft_disp}**  |  "
        f"Tank {start_pct_disp}% reicht für **{reich_disp} Runden**"
    )

    embed = discord.Embed(
        title=f"🏁 {league_display} – {track_display}",
        description=f"👤 **{nickname}** | 🚗 {car_display} | 🔄 {total_laps} Runden{pit_window_str}\n{data_line}",
        color=0x00BFFF
    )

    pole_labels   = ["Pole – Variante 1",   "Pole – Variante 2"]
    nopole_labels = ["Nicht-Pole – Variante 1", "Nicht-Pole – Variante 2"]

    def add_field(label, result):
        if result is None:
            embed.add_field(name=label, value="Nicht möglich", inline=True)
            return
        stint_str  = " → ".join(f"{TYRE_EMOJI.get(t,t)}{n}" for t, n in result.stints)
        time_str   = seconds_to_display(result.total_time_s)
        reasoning  = reasonings.get(label, "")
        stops_info = ""
        if result.fuel_stops:
            stops_info = f"\n⛽ Tanken bei Stopp {', '.join(str(s+1) for s in result.fuel_stops)}"
        embed.add_field(
            name=label,
            value=f"**{time_str}**\n{stint_str}{stops_info}\n_{reasoning}_",
            inline=True
        )

    for label in pole_labels:
        add_field(label, strategies.get(label))
    embed.add_field(name="\u200b", value="\u200b", inline=True)
    for label in nopole_labels:
        add_field(label, strategies.get(label))
    embed.add_field(name="\u200b", value="\u200b", inline=True)

    # Fünfte Variante: Mono-Reifen (kein Reifenwechsel)
    extra_pole   = strategies.get("Pole – Schnellster Reifen")
    extra_nopole = strategies.get("Nicht-Pole – Schnellster Reifen")
    if extra_pole or extra_nopole:
        embed.add_field(name="── Nur ein Reifentyp ──", value="​", inline=False)
        if extra_pole:
            add_field("Pole – Schnellster Reifen", extra_pole)
        if extra_nopole:
            add_field("Nicht-Pole – Schnellster Reifen", extra_nopole)
        # Leerzeichen für 3-Spalten-Layout
        count = sum(1 for x in [extra_pole, extra_nopole] if x)
        if count == 1:
            embed.add_field(name="​", value="​", inline=True)
            embed.add_field(name="​", value="​", inline=True)


    # Top-5 je Pole/Nicht-Pole
    def top5_text(results, label):
        lines = [f"**{label}**"]
        shown = []
        for r in results:
            if r.description not in shown:
                shown.append(r.description)
                mins = int(r.total_time_s // 60)
                secs = r.total_time_s % 60
                lines.append(f"`{mins}:{secs:06.3f}` {r.description}")
            if len(shown) == 5:
                break
        return "\n".join(lines)

    pole_top5   = top5_text(all_pole,    "🏆 Pole")
    nopole_top5 = top5_text(all_no_pole, "🏆 Nicht-Pole")
    embed.add_field(name="Top 5 Strategien", value=f"{pole_top5}\n\n{nopole_top5}"[:1024], inline=False)

    if overall:
        embed.add_field(name=f"{ai_label} – Gesamtempfehlung", value=overall[:1024], inline=False)

    footer = f"NFR Strategy Bot • GT7 • {'Gemini 2.5 Flash' if used_gemini else 'Fallback-Algorithmus'}"
    embed.set_footer(text=footer)

    table_params = dict(
        base_soft_s=soft_s, medium_plus_pct=medium_pct, hard_plus_pct=hard_pct,
        max_soft_runden=max_soft,
        fuel_per_lap=fuel_per_lap, start_fuel=start_fuel,
        tank_size=tank_size, tank_rate_l_per_s=tank_rate,
        pit_loss_s=pit_loss, fuel_weight_s=fw_s,
    )

    # Pole/Nicht-Pole Auswahl – nur für den Anfragenden sichtbar (ephemeral via DM-ähnliche Struktur)
    pole_view   = PoleSelectView(all_pole,    channel, table_params, requester_id)
    nopole_view = PoleSelectView(all_no_pole, channel, table_params, requester_id)
    detail_view = PoleChoiceView(pole_view, nopole_view, requester_id)

    await channel.send(embed=embed)
    await channel.send("**Detailansicht:** Wähle Pole oder Nicht-Pole:", view=detail_view)


# ─────────────────────────────────────────────
# Modal
# ─────────────────────────────────────────────

def make_modal(nickname, track, version, brand, model, total_laps,
               channel, hard_enabled, league=DEFAULT_LEAGUE, prefill=None):
    if hard_enabled:
        class ModalHard(Modal, title="Deine Daten eingeben"):
            zeit_soft   = TextInput(label="Rundenzeit auf Soft (m:ss.mmm)",            placeholder="z.B. 1:49.300", required=True)
            zeit_medium = TextInput(label="Rundenzeit auf Medium (leer = Durchschnitt)",placeholder="z.B. 1:50.400 (optional)", required=False)
            zeit_hard   = TextInput(label="Rundenzeit auf Hard (leer = Durchschnitt)",  placeholder="z.B. 1:52.000 (optional)", required=False)
            max_soft    = TextInput(label="Maximale Runden auf Soft",                   placeholder="z.B. 13",       required=True)
            reichweite  = TextInput(label="Reichweite bei 70% Tank (Runden)",           placeholder="z.B. 15",       required=True)
            def __init__(self):
                super().__init__()
                self._p = (nickname, track, version, brand, model, total_laps, channel, league)
                if prefill:
                    self.zeit_soft.default   = prefill.get("Zeit_Soft", "")
                    self.zeit_medium.default = prefill.get("Zeit_Medium", "")
                    self.zeit_hard.default   = prefill.get("Zeit_Hard", "")
                    self.max_soft.default    = str(prefill.get("Max_Soft_Runden", ""))
                    self.reichweite.default  = str(prefill.get("Reichweite_70pct", ""))
            async def on_submit(self, interaction: discord.Interaction):
                await _handle_submit(interaction, *self._p,
                    str(self.zeit_soft), str(self.zeit_medium),
                    str(self.zeit_hard), str(self.max_soft), str(self.reichweite))
        return ModalHard()
    else:
        class ModalNoHard(Modal, title="Deine Daten eingeben"):
            zeit_soft   = TextInput(label="Rundenzeit auf Soft (m:ss.mmm)",            placeholder="z.B. 1:49.300", required=True)
            zeit_medium = TextInput(label="Rundenzeit auf Medium (leer = Durchschnitt)",placeholder="z.B. 1:50.400 (optional)", required=False)
            max_soft    = TextInput(label="Maximale Runden auf Soft",                   placeholder="z.B. 13",       required=True)
            reichweite  = TextInput(label="Reichweite bei 70% Tank (Runden)",           placeholder="z.B. 15",       required=True)
            def __init__(self):
                super().__init__()
                self._p = (nickname, track, version, brand, model, total_laps, channel, league)
                if prefill:
                    self.zeit_soft.default   = prefill.get("Zeit_Soft", "")
                    self.zeit_medium.default = prefill.get("Zeit_Medium", "")
                    self.max_soft.default    = str(prefill.get("Max_Soft_Runden", ""))
                    self.reichweite.default  = str(prefill.get("Reichweite_70pct", ""))
            async def on_submit(self, interaction: discord.Interaction):
                await _handle_submit(interaction, *self._p,
                    str(self.zeit_soft), str(self.zeit_medium),
                    None, str(self.max_soft), str(self.reichweite))
        return ModalNoHard()


async def _handle_submit(interaction, nickname, track, version, brand, model,
                         total_laps, channel, league,
                         raw_soft, raw_medium, raw_hard, raw_max_soft, raw_reichweite):
    try:
        soft_s     = parse_time(raw_soft)
        hard_s     = parse_time(raw_hard) if raw_hard and raw_hard.strip() else None
        max_soft   = int(str(raw_max_soft).strip())
        reichweite = int(str(raw_reichweite).strip())
    except ValueError:
        await interaction.response.send_message(
            "❌ Ungültige Eingabe. Format: m:ss.mmm (z.B. 1:49.300)", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    settings = get_settings(league)

    if raw_medium and raw_medium.strip():
        try:    medium_s = parse_time(raw_medium)
        except: medium_s = None
    else:
        medium_s = None

    avg = get_driver_avg_pct(nickname, league)

    if medium_s is None:
        raw_med_pct = avg["medium_pct"]
        # Plausibilitätscheck: Medium_Pct darf max 10% sein
        if raw_med_pct is not None and 0 < raw_med_pct <= 10:
            med_pct = raw_med_pct
        else:
            if raw_med_pct is not None:
                print(f"[Warnung] Medium_Pct {raw_med_pct} unplausibel – verwende Settings-Default")
            med_pct = float(settings.get("medium_default_pct", 1.0))
        medium_s = round(soft_s * (1 + med_pct / 100), 3)

    hard_allowed = settings.get("hard_tyre_allowed", "FALSE").upper() == "TRUE"
    if hard_allowed and hard_s is None:
        raw_pct = avg["hard_pct"]
        # Plausibilitätscheck: Hard_Pct darf max 20% sein, sonst ist der Wert korrupt
        if raw_pct is not None and 0 < raw_pct <= 20:
            hard_pct_val = raw_pct
        else:
            if raw_pct is not None:
                print(f"[Warnung] Hard_Pct {raw_pct} unplausibel – verwende Settings-Default")
            hard_pct_val = float(settings.get("hard_default_pct", 2.5))
        hard_s = round(soft_s * (1 + hard_pct_val / 100), 3)

    data = {
        "zeit_soft_s": soft_s, "zeit_medium_s": medium_s,
        "zeit_hard_s": hard_s, "max_soft_runden": max_soft,
        "reichweite_70pct": reichweite,
    }
    save_driver_data(nickname, track, version, brand, model, data, league)

    msg = await interaction.followup.send("✅ Daten gespeichert! Strategie wird berechnet...", ephemeral=True)
    await calculate_and_post(channel, nickname, track, version, brand, model,
                             total_laps, data, settings, league)
    try:
        await interaction.delete_original_response()
    except Exception:
        pass


def build_prefill(soft_s, nickname, settings, hard_enabled, existing=None, league=DEFAULT_LEAGUE):
    if existing:
        def sd(v):
            f = to_float(v)
            return seconds_to_display(f) if f else ""
        return {
            "Zeit_Soft":        sd(existing.get("Zeit_Soft_s")),
            "Zeit_Medium":      sd(existing.get("Zeit_Medium_s")),
            "Zeit_Hard":        sd(existing.get("Zeit_Hard_s")),
            "Max_Soft_Runden":  existing.get("Max_Soft_Runden", ""),
            "Reichweite_70pct": existing.get("Reichweite_70pct", ""),
        }
    avg = get_driver_avg_pct(nickname, league)
    med_pct  = avg["medium_pct"]  if avg["medium_pct"]  is not None else float(settings.get("medium_default_pct", 1.0))
    hard_pct = avg["hard_pct"]    if avg["hard_pct"]    is not None else float(settings.get("hard_default_pct",   2.5))
    prefill = {
        "Zeit_Soft":        seconds_to_display(soft_s) if soft_s > 0 else "",
        "Zeit_Medium":      seconds_to_display(apply_pct(soft_s, med_pct)) if soft_s > 0 else "",
        "Max_Soft_Runden":  "",
        "Reichweite_70pct": "",
    }
    if hard_enabled:
        prefill["Zeit_Hard"] = seconds_to_display(apply_pct(soft_s, hard_pct)) if soft_s > 0 else ""
    return prefill


# ─────────────────────────────────────────────
# Views
# ─────────────────────────────────────────────

class SuggestLapTimeView(View):
    def __init__(self, nickname, track, version, brand, model, total_laps,
                 channel, suggested_s, settings, hard_enabled, league=DEFAULT_LEAGUE):
        super().__init__(timeout=120)
        self.d = (nickname, track, version, brand, model, total_laps,
                  channel, suggested_s, settings, hard_enabled, league)

    @discord.ui.button(label="✅ Vorgeschlagene Zeit verwenden", style=discord.ButtonStyle.success)
    async def use_suggestion(self, interaction: discord.Interaction, button: Button):
        n,tr,v,br,mo,l,ch,s_s,settings,hard,league = self.d
        prefill = build_prefill(s_s, n, settings, hard, league=league)
        await interaction.response.send_modal(make_modal(n,tr,v,br,mo,l,ch,hard,league,prefill=prefill))
        self.stop()

    @discord.ui.button(label="✏️ Eigene Zeit eingeben", style=discord.ButtonStyle.primary)
    async def enter_own(self, interaction: discord.Interaction, button: Button):
        n,tr,v,br,mo,l,ch,_,_,hard,league = self.d
        await interaction.response.send_modal(make_modal(n,tr,v,br,mo,l,ch,hard,league))
        self.stop()


class ConfirmDataView(View):
    def __init__(self, nickname, track, version, brand, model, total_laps,
                 existing_data, settings, channel, hard_enabled, league=DEFAULT_LEAGUE):
        super().__init__(timeout=120)
        self.d = (nickname, track, version, brand, model, total_laps,
                  existing_data, settings, channel, hard_enabled, league)

    @discord.ui.button(label="✅ Daten verwenden", style=discord.ButtonStyle.success)
    async def use_data(self, interaction: discord.Interaction, button: Button):
        await interaction.response.defer(ephemeral=True)
        n,tr,v,br,mo,l,ex,settings,ch,hard,league = self.d
        data = {
            "zeit_soft_s":      to_float(ex.get("Zeit_Soft_s")),
            "zeit_medium_s":    to_float(ex.get("Zeit_Medium_s")),
            "zeit_hard_s":      to_float(ex.get("Zeit_Hard_s")),
            "max_soft_runden":  ex.get("Max_Soft_Runden", 13),
            "reichweite_70pct": ex.get("Reichweite_70pct", 15),
        }
        await calculate_and_post(ch, n, tr, v, br, mo, l, data, settings, league,
                                   requester_id=interaction.user.id)
        try:
            await interaction.edit_original_response(content="​", embed=None, view=None)
        except Exception:
            pass
        self.stop()

    @discord.ui.button(label="✏️ Daten anpassen", style=discord.ButtonStyle.primary)
    async def edit_data(self, interaction: discord.Interaction, button: Button):
        n,tr,v,br,mo,l,ex,settings,ch,hard,league = self.d
        soft_f  = to_float(ex.get("Zeit_Soft_s")) or 0
        prefill = build_prefill(soft_f, n, settings, hard, existing=ex, league=league)
        await interaction.response.send_modal(make_modal(n,tr,v,br,mo,l,ch,hard,league,prefill=prefill))
        self.stop()


# ─────────────────────────────────────────────
# Detail-Ansicht
# ─────────────────────────────────────────────

class DetailSelectView(discord.ui.View):
    def __init__(self, strategies, channel, base_soft_s, medium_plus_pct,
                 hard_plus_pct, max_soft_runden, fuel_per_lap, start_fuel,
                 tank_size, tank_rate_l_per_s, pit_loss_s, fuel_weight_s):
        super().__init__(timeout=300)
        self.strategies   = strategies
        self.channel      = channel
        self.table_params = dict(
            base_soft_s=base_soft_s, medium_plus_pct=medium_plus_pct,
            hard_plus_pct=hard_plus_pct, max_soft_runden=max_soft_runden,
            fuel_per_lap=fuel_per_lap, start_fuel=start_fuel,
            tank_size=tank_size, tank_rate_l_per_s=tank_rate_l_per_s,
            pit_loss_s=pit_loss_s, fuel_weight_s=fuel_weight_s,
        )

        def stint_short(result):
            """Kurzform der Stints: z.B. 7S-11M oder 6S-6S-6S"""
            parts = []
            for tyre, laps in result.stints:
                parts.append(f"{laps}{tyre[0]}")
            return "-".join(parts)

        prefix_style = {
            "Pole":      discord.ButtonStyle.primary,
            "Nicht-Pole": discord.ButtonStyle.secondary,
            "Schnellster": discord.ButtonStyle.success,
        }

        for label, result in strategies.items():
            if result is None:
                continue
            # Button-Label: "Pole 7S-11M" oder "N-Pole 6S-6S-6S"
            prefix = "Pole" if "Nicht-Pole" not in label else "N-Pole"
            short  = stint_short(result)
            btn_label = f"📋 {prefix} {short}"[:80]  # Discord max 80 Zeichen
            style = discord.ButtonStyle.primary if "Nicht-Pole" not in label and "Schnellster" not in label else                     discord.ButtonStyle.success  if "Schnellster" in label else                     discord.ButtonStyle.secondary
            btn = discord.ui.Button(
                label=btn_label,
                style=style,
                custom_id=label,
            )
            btn.callback = self._make_callback(label)
            self.add_item(btn)

        close_btn = discord.ui.Button(label="✖ Keine Anzeige", style=discord.ButtonStyle.danger)
        close_btn.callback = self._close
        self.add_item(close_btn)

    async def _close(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        self.stop()

    def _make_callback(self, label):
        async def callback(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            result = self.strategies.get(label)
            if result is None:
                await interaction.followup.send("Keine Daten.", ephemeral=True)
                return
            pole  = "Nicht-Pole" not in label
            table = build_single_column(label, result, pole=pole, **self.table_params)
            lines = table.split("\n")
            chunk = "```\n"
            for line in lines:
                if len(chunk) + len(line) + 2 > 1950:
                    await self.channel.send(chunk + "```")
                    chunk = "```\n" + line + "\n"
                else:
                    chunk += line + "\n"
            await self.channel.send(chunk + "```")
            # Buttons erneut anzeigen
            new_view = DetailSelectView(
                strategies=self.strategies, channel=self.channel,
                **self.table_params,
            )
            await self.channel.send("Weitere Detailansicht:", view=new_view)
            self.stop()
        return callback


# ─────────────────────────────────────────────
# Zweistufige Detail-Auswahl
# ─────────────────────────────────────────────

class PoleChoiceView(discord.ui.View):
    """Erste Stufe: Pole oder Nicht-Pole wählen."""
    def __init__(self, pole_view, nopole_view, requester_id: int):
        super().__init__(timeout=300)
        self.pole_view    = pole_view
        self.nopole_view  = nopole_view
        self.requester_id = requester_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.requester_id and interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Diese Auswahl ist nur für denjenigen, der die Strategie angefordert hat.",
                ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="🏁 Pole", style=discord.ButtonStyle.primary)
    async def pole(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.pole_view.back_view = self
        await interaction.response.edit_message(
            content="**Pole** – wähle eine Strategie für die Detailansicht:",
            view=self.pole_view
        )

    @discord.ui.button(label="🚦 Nicht-Pole", style=discord.ButtonStyle.secondary)
    async def nopole(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.nopole_view.back_view = self
        await interaction.response.edit_message(
            content="**Nicht-Pole** – wähle eine Strategie für die Detailansicht:",
            view=self.nopole_view
        )


class PoleSelectView(discord.ui.View):
    """Zweite Stufe: Top-5 Strategien zur Auswahl."""
    def __init__(self, results, channel, table_params, requester_id: int):
        super().__init__(timeout=300)
        self.channel      = channel
        self.table_params = table_params
        self.requester_id = requester_id
        self.results      = results
        self.back_view    = None  # wird von PoleChoiceView gesetzt

        # Top-5 eindeutige Strategien als Buttons
        seen = []
        for r in results:
            if r.description not in seen:
                seen.append(r.description)
            if len(seen) == 5:
                break

        for i, desc in enumerate(seen):
            result = next(x for x in results if x.description == desc)
            mins   = int(result.total_time_s // 60)
            secs   = result.total_time_s % 60
            label  = f"{mins}:{secs:06.3f} {self._short(result)}"[:80]
            btn    = discord.ui.Button(label=label, style=discord.ButtonStyle.primary, row=i // 2)
            btn.callback = self._make_callback(result)
            self.add_item(btn)

        back_btn = discord.ui.Button(label="← Zurück", style=discord.ButtonStyle.secondary, row=2)
        back_btn.callback = self._back
        self.add_item(back_btn)

    def _short(self, result):
        return "-".join(f"{n}{t[0]}" for t, n in result.stints)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.requester_id and interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Diese Auswahl ist nur für denjenigen, der die Strategie angefordert hat.",
                ephemeral=True
            )
            return False
        return True

    async def _back(self, interaction: discord.Interaction):
        if self.back_view:
            await interaction.response.edit_message(
                content="**Detailansicht:** Wähle Pole oder Nicht-Pole:",
                view=self.back_view
            )
        else:
            await interaction.response.defer()

    def _make_callback(self, result):
        async def callback(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            table = build_single_column(result.description, result,
                                        pole=result.pole,
                                        **self.table_params)
            lines = table.split("\n")
            chunk = "```\n"
            for line in lines:
                if len(chunk) + len(line) + 2 > 1950:
                    await self.channel.send(chunk + "```")
                    chunk = "```\n" + line + "\n"
                else:
                    chunk += line + "\n"
            await self.channel.send(chunk + "```")
            # Zurück zur Strategieauswahl
            await interaction.edit_original_response(
                content="Weitere Detailansicht:",
                view=self
            )
        return callback
