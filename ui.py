import discord
from discord.ui import Modal, TextInput, View, Button
from strategy import (
    calculate_strategies, format_time, parse_pit_windows,
    TYRE_SOFT, TYRE_MEDIUM, TYRE_HARD
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
                              total_laps, data, settings, league=DEFAULT_LEAGUE):
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

    if medium_s is None or medium_s <= 0:
        med_pct_val = avg["medium_pct"] if avg["medium_pct"] is not None else f("medium_default_pct", 1.0)
        medium_s = round(soft_s * (1 + med_pct_val / 100), 3)

    if hard_allowed and (hard_s is None or hard_s <= 0):
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

    all_pole    = calculate_strategies(**common, pole=True)
    all_no_pole = calculate_strategies(**common, pole=False)

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

    embed = discord.Embed(
        title=f"🏁 {league_display} – {track_display}",
        description=f"👤 **{nickname}** | 🚗 {car_display} | 🔄 {total_laps} Runden{pit_window_str}",
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

    if overall:
        embed.add_field(name=f"{ai_label} – Gesamtempfehlung", value=overall[:1024], inline=False)

    footer = f"NFR Strategy Bot • GT7 • {'Gemini 2.5 Flash' if used_gemini else 'Fallback-Algorithmus'}"
    embed.set_footer(text=footer)

    detail_msg = (
        "**Detailansicht:** Wähle eine Strategie für die Runde-für-Runde Aufschlüsselung, "
        "oder klicke **Keine Anzeige** um zu beenden."
    )
    view = DetailSelectView(
        strategies=strategies,
        channel=channel,
        base_soft_s=soft_s, medium_plus_pct=medium_pct, hard_plus_pct=hard_pct,
        max_soft_runden=max_soft,
        fuel_per_lap=fuel_per_lap, start_fuel=start_fuel,
        tank_size=tank_size, tank_rate_l_per_s=tank_rate,
        pit_loss_s=pit_loss, fuel_weight_s=fw_s,
    )
    await channel.send(embed=embed)
    await channel.send(detail_msg, view=view)


# ─────────────────────────────────────────────
# Modal
# ─────────────────────────────────────────────

def make_modal(nickname, track, version, brand, model, total_laps,
               channel, hard_enabled, league=DEFAULT_LEAGUE, prefill=None):
    if hard_enabled:
        class ModalHard(Modal, title="Deine Daten eingeben"):
            zeit_soft   = TextInput(label="Rundenzeit auf Soft (m:ss.mmm)",            placeholder="z.B. 1:49.300", required=True)
            zeit_medium = TextInput(label="Rundenzeit auf Medium (leer = Durchschnitt)",placeholder="z.B. 1:50.400 (optional)", required=False)
            zeit_hard   = TextInput(label="Rundenzeit auf Hard (m:ss.mmm)",             placeholder="z.B. 1:52.000", required=True)
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
        max_soft   = int(raw_max_soft.strip())
        reichweite = int(raw_reichweite.strip())
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

    if medium_s is None:
        avg = get_driver_avg_pct(nickname, league)
        med_pct = avg["medium_pct"] if avg["medium_pct"] is not None else float(settings.get("medium_default_pct", 1.0))
        medium_s = round(soft_s * (1 + med_pct / 100), 3)

    data = {
        "zeit_soft_s": soft_s, "zeit_medium_s": medium_s,
        "zeit_hard_s": hard_s, "max_soft_runden": max_soft,
        "reichweite_70pct": reichweite,
    }
    save_driver_data(nickname, track, version, brand, model, data, league)

    await interaction.followup.send("✅ Daten gespeichert! Strategie wird berechnet...", ephemeral=True)
    await calculate_and_post(channel, nickname, track, version, brand, model,
                             total_laps, data, settings, league)


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
        await interaction.followup.send("✅ Strategie wird berechnet...", ephemeral=True)
        await calculate_and_post(ch, n, tr, v, br, mo, l, data, settings, league)
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

        labels_short = {
            "Pole – Variante 1":       "📋 Pole V1",
            "Pole – Variante 2":       "📋 Pole V2",
            "Nicht-Pole – Variante 1": "📋 Nicht-Pole V1",
            "Nicht-Pole – Variante 2": "📋 Nicht-Pole V2",
        }
        styles = {
            "Pole – Variante 1":       discord.ButtonStyle.primary,
            "Pole – Variante 2":       discord.ButtonStyle.primary,
            "Nicht-Pole – Variante 1": discord.ButtonStyle.secondary,
            "Nicht-Pole – Variante 2": discord.ButtonStyle.secondary,
        }
        for label, result in strategies.items():
            if result is None:
                continue
            btn = discord.ui.Button(
                label=labels_short.get(label, label),
                style=styles.get(label, discord.ButtonStyle.secondary),
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
            await interaction.followup.send("✅ Detailansicht gepostet.", ephemeral=True)
            # Buttons erneut anzeigen
            new_view = DetailSelectView(
                strategies=self.strategies, channel=self.channel,
                **self.table_params,
            )
            await self.channel.send("Weitere Detailansicht:", view=new_view)
            self.stop()
        return callback
