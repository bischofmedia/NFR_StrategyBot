import discord
from discord.ui import Modal, TextInput, View, Button
from strategy import calculate_strategies, format_time, TYRE_SOFT, TYRE_MEDIUM, TYRE_HARD
from sheets import get_driver_data, save_driver_data, get_settings, get_track_data, get_driver_avg_pct
from gemini import get_gemini_strategies, fallback_strategies
from table import build_single_column

TYRE_EMOJI = {TYRE_SOFT: "🔴", TYRE_MEDIUM: "🟡", TYRE_HARD: "⚪"}


def parse_time(time_str: str) -> float:
    time_str = time_str.strip().replace(",", ".")
    if ":" in time_str:
        parts = time_str.split(":")
        return int(parts[0]) * 60 + float(parts[1])
    return float(time_str)

def seconds_to_display(s: float) -> str:
    mins = int(s // 60)
    secs = s % 60
    return f"{mins}:{secs:06.3f}"

def apply_pct(base_s: float, pct: float) -> float:
    return round(base_s * (1 + pct / 100), 3)


# ─────────────────────────────────────────────
# Kernlogik
# ─────────────────────────────────────────────

async def calculate_and_post(channel, nickname, track, version, brand, model, total_laps, data, settings):
    car_display  = f"{brand} {model}"
    hard_enabled = settings.get("hard", "TRUE").upper() != "FALSE"
    track_data   = get_track_data(track, version)
    pit_loss     = track_data["pit_loss_s"] or float(settings.get("pit_loss_s", 25))
    tank_size    = float(settings.get("tank_size_l", 100))
    tank_rate    = float(settings.get("tank_rate_l_per_s", 5))
    start_pct    = float(settings.get("start_fuel_pct", 70))
    soft_req     = settings.get("soft_stint_required", "TRUE").upper() == "TRUE"
    verk_s       = float(settings.get("verkehr_aufschlag_s", 2.0))
    verk_r       = int(settings.get("verkehr_runden", 3))
    fw_s         = float(settings.get("fuel_weight_s", 0.7))

    def to_float(v):
        """Sicher zu float konvertieren, None/leer → None."""
        if v is None or str(v).strip() == "":
            return None
        return float(str(v).replace(",", "."))

    soft_s   = to_float(data["zeit_soft_s"])
    medium_s = to_float(data["zeit_medium_s"])
    hard_s   = to_float(data.get("zeit_hard_s")) if hard_enabled else None

    # Medium: Fallback auf Durchschnitt oder Settings wenn nicht vorhanden
    if medium_s is None or medium_s <= 0:
        avg = get_driver_avg_pct(nickname)
        medium_pct_val = avg["medium_pct"] if avg["medium_pct"] is not None else float(settings.get("medium_default_pct", 1.0))
        medium_s = round(soft_s * (1 + medium_pct_val / 100), 3)

    medium_pct = (medium_s - soft_s) / soft_s * 100
    hard_pct   = (hard_s - soft_s) / soft_s * 100 if hard_s else 0

    start_fuel   = tank_size * (start_pct / 100)
    fuel_per_lap = start_fuel / int(str(data["reichweite_70pct"]).replace(",", ".").split(".")[0])

    common = dict(
        total_laps=total_laps,
        base_time_soft_s=soft_s,
        medium_plus_pct=medium_pct,
        hard_plus_pct=hard_pct,
        hard_enabled=hard_enabled,
        max_soft_runden=int(str(data["max_soft_runden"]).replace(",",".").split(".")[0]),
        reichweite_70pct=data["reichweite_70pct"],
        tank_size=tank_size,
        tank_rate_l_per_s=tank_rate,
        pit_loss_s=pit_loss,
        start_fuel_pct=start_pct,
        soft_required=soft_req,
        verkehr_aufschlag_s=verk_s,
        verkehr_runden=verk_r,
        fuel_weight_s=fw_s,
    )

    all_pole    = calculate_strategies(**common, pole=True)
    all_no_pole = calculate_strategies(**common, pole=False)

    gemini_aktiv = settings.get("gemini", "TRUE").upper() != "FALSE"
    gemini_result = None
    if gemini_aktiv:
        gemini_result = get_gemini_strategies(
            all_results_pole=all_pole,
        all_results_no_pole=all_no_pole,
        track=track, version=version, car=car_display,
        total_laps=total_laps,
        base_soft_s=soft_s, medium_pct=medium_pct, hard_pct=hard_pct,
        max_soft_runden=int(str(data["max_soft_runden"]).replace(",",".").split(".")[0]),
        reichweite=data["reichweite_70pct"],
        tank_size=tank_size, start_fuel_pct=start_pct,
        pit_loss=pit_loss, fuel_weight_s=fw_s,
        hard_enabled=hard_enabled,
    )

    used_gemini = gemini_result is not None
    if not used_gemini:
        gemini_result = fallback_strategies(all_pole, all_no_pole)

    strategies = gemini_result["strategies"]
    reasonings = gemini_result["reasonings"]
    overall    = gemini_result.get("overall", "")

    track_display = f"{track} – {version}" if version else track
    ai_label = "🤖 KI-Analyse" if used_gemini else "📊 Analyse"

    embed = discord.Embed(
        title=f"🏁 Strategieanalyse – {track_display}",
        description=f"👤 **{nickname}** | 🚗 {car_display} | 🔄 {total_laps} Runden",
        color=0x00BFFF
    )

    # Layout: Pole-Strategien nebeneinander, darunter Nicht-Pole nebeneinander
    pole_labels    = ["1-Stopp Pole", "2-Stopp Pole"]
    nopole_labels  = ["1-Stopp Nicht-Pole", "2-Stopp Nicht-Pole"]

    def add_strategy_field(label, result):
        if result is None:
            embed.add_field(name=label, value="Nicht möglich", inline=True)
            return
        stint_str  = " → ".join(f"{TYRE_EMOJI[t]}{n}" for t, n in result.stints)
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

    # Zeile 1: Pole
    for label in pole_labels:
        add_strategy_field(label, strategies.get(label))
    embed.add_field(name="​", value="​", inline=True)  # Leerzeichen für 3-Spalten-Layout

    # Zeile 2: Nicht-Pole
    for label in nopole_labels:
        add_strategy_field(label, strategies.get(label))
    embed.add_field(name="​", value="​", inline=True)

    if overall:
        embed.add_field(name=f"{ai_label} – Gesamtempfehlung", value=overall[:1024], inline=False)

    footer_suffix = "Gemini 2.5 Flash" if used_gemini else "Fallback-Algorithmus"
    embed.set_footer(text=f"NFR Strategy Bot • GT7 • {footer_suffix}")

    # Detail-View: Buttons für jede Strategie
    detail_msg = (
        "**Detailansicht:** Wähle eine Strategie für die Runde-für-Runde Aufschlüsselung, "
        "oder klicke **Keine Anzeige** um zu beenden."
    )
    view = DetailSelectView(
        strategies=strategies,
        base_soft_s=soft_s,
        medium_plus_pct=medium_pct,
        hard_plus_pct=hard_pct,
        max_soft_runden=data["max_soft_runden"] if isinstance(data["max_soft_runden"], int) else int(str(data["max_soft_runden"]).split(".")[0]),
        fuel_per_lap=fuel_per_lap,
        start_fuel=start_fuel,
        tank_size=tank_size,
        tank_rate_l_per_s=tank_rate,
        pit_loss_s=pit_loss,
        fuel_weight_s=fw_s,
        channel=channel,
        verkehr_aufschlag_s=verk_s,
        verkehr_runden=verk_r,
    )
    await channel.send(embed=embed)
    await channel.send(detail_msg, view=view)


# ─────────────────────────────────────────────
# Modal
# ─────────────────────────────────────────────

def make_modal(nickname, track, version, brand, model, total_laps, channel, hard_enabled, prefill=None):
    if hard_enabled:
        class ModalHard(Modal, title="Deine Daten eingeben"):
            zeit_soft   = TextInput(label="Rundenzeit auf Soft (m:ss.mmm)",   placeholder="z.B. 1:49.300", required=True)
            zeit_medium = TextInput(label="Rundenzeit auf Medium (leer = Durchschnitt)", placeholder="z.B. 1:50.400 (optional)", required=False)
            zeit_hard   = TextInput(label="Rundenzeit auf Hard (m:ss.mmm)",   placeholder="z.B. 1:52.000", required=True)
            max_soft    = TextInput(label="Maximale Runden auf Soft",         placeholder="z.B. 13",       required=True)
            reichweite  = TextInput(label="Reichweite bei 70% Tank (Runden)", placeholder="z.B. 15",       required=True)
            def __init__(self):
                super().__init__()
                self._p = (nickname, track, version, brand, model, total_laps, channel)
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
            zeit_soft   = TextInput(label="Rundenzeit auf Soft (m:ss.mmm)",   placeholder="z.B. 1:49.300", required=True)
            zeit_medium = TextInput(label="Rundenzeit auf Medium (leer = Durchschnitt)", placeholder="z.B. 1:50.400 (optional)", required=False)
            max_soft    = TextInput(label="Maximale Runden auf Soft",         placeholder="z.B. 13",       required=True)
            reichweite  = TextInput(label="Reichweite bei 70% Tank (Runden)", placeholder="z.B. 15",       required=True)
            def __init__(self):
                super().__init__()
                self._p = (nickname, track, version, brand, model, total_laps, channel)
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
                         total_laps, channel, raw_soft, raw_medium, raw_hard,
                         raw_max_soft, raw_reichweite):
    # Sofort bestätigen – Google Sheets Abfragen dauern zu lang für Discord Timeout
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

    # Sofort defer – alle weiteren Operationen dauern zu lang
    await interaction.response.defer(ephemeral=True)

    settings = get_settings()

    # Medium: aus Eingabe, sonst Fahrer-Durchschnitt, sonst Settings-Standard
    if raw_medium and raw_medium.strip():
        try:
            medium_s = parse_time(raw_medium)
        except ValueError:
            medium_s = None
    else:
        medium_s = None

    if medium_s is None:
        avg = get_driver_avg_pct(nickname)
        medium_pct = avg["medium_pct"] if avg["medium_pct"] is not None else float(settings.get("medium_default_pct", 1.0))
        medium_s = round(soft_s * (1 + medium_pct / 100), 3)

    data = {
        "zeit_soft_s": soft_s, "zeit_medium_s": medium_s,
        "zeit_hard_s": hard_s, "max_soft_runden": max_soft,
        "reichweite_70pct": reichweite,
    }
    save_driver_data(nickname, track, version, brand, model, data)

    await interaction.followup.send(
        "✅ Daten gespeichert! Strategie wird berechnet...", ephemeral=True
    )
    await calculate_and_post(channel, nickname, track, version, brand, model, total_laps, data, settings)


def build_prefill(soft_s, nickname, settings, hard_enabled, existing=None):
    if existing:
        return {
            "Zeit_Soft":        seconds_to_display(float(str(existing["Zeit_Soft_s"]).replace(",","."))),
            "Zeit_Medium":      seconds_to_display(float(str(existing["Zeit_Medium_s"]).replace(",","."))) if existing.get("Zeit_Medium_s") else "",
            "Zeit_Hard":        seconds_to_display(float(str(existing["Zeit_Hard_s"]).replace(",",".")))   if existing.get("Zeit_Hard_s")   else "",
            "Max_Soft_Runden":  existing.get("Max_Soft_Runden", ""),
            "Reichweite_70pct": existing.get("Reichweite_70pct", ""),
        }
    avg = get_driver_avg_pct(nickname)
    medium_pct = avg["medium_pct"] if avg["medium_pct"] is not None else float(settings.get("medium_default_pct", 1.0))
    hard_pct   = avg["hard_pct"]   if avg["hard_pct"]   is not None else float(settings.get("hard_default_pct",   2.5))
    prefill = {
        "Zeit_Soft":        seconds_to_display(soft_s) if soft_s > 0 else "",
        "Zeit_Medium":      seconds_to_display(apply_pct(soft_s, medium_pct)) if soft_s > 0 else "",
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
    def __init__(self, nickname, track, version, brand, model, total_laps, channel, suggested_s, settings, hard_enabled):
        super().__init__(timeout=120)
        self.d = (nickname, track, version, brand, model, total_laps, channel, suggested_s, settings, hard_enabled)

    @discord.ui.button(label="✅ Vorgeschlagene Zeit verwenden", style=discord.ButtonStyle.success)
    async def use_suggestion(self, interaction: discord.Interaction, button: Button):
        n, tr, v, br, mo, l, ch, s_s, settings, hard = self.d
        prefill = build_prefill(s_s, n, settings, hard)
        await interaction.response.send_modal(make_modal(n, tr, v, br, mo, l, ch, hard, prefill=prefill))
        self.stop()

    @discord.ui.button(label="✏️ Eigene Zeit eingeben", style=discord.ButtonStyle.primary)
    async def enter_own(self, interaction: discord.Interaction, button: Button):
        n, tr, v, br, mo, l, ch, _, _, hard = self.d
        await interaction.response.send_modal(make_modal(n, tr, v, br, mo, l, ch, hard))
        self.stop()


class ConfirmDataView(View):
    def __init__(self, nickname, track, version, brand, model, total_laps, existing_data, settings, channel, hard_enabled):
        super().__init__(timeout=120)
        self.d = (nickname, track, version, brand, model, total_laps, existing_data, settings, channel, hard_enabled)

    @discord.ui.button(label="✅ Daten verwenden", style=discord.ButtonStyle.success)
    async def use_data(self, interaction: discord.Interaction, button: Button):
        await interaction.response.defer(ephemeral=True)
        n, tr, v, br, mo, l, ex, settings, ch, hard = self.d
        data = {
            "zeit_soft_s":      float(str(ex["Zeit_Soft_s"]).replace(",",".")),
            "zeit_medium_s":    float(str(ex["Zeit_Medium_s"]).replace(",",".")) if ex.get("Zeit_Medium_s") else None,
            "zeit_hard_s":      float(str(ex["Zeit_Hard_s"]).replace(",","."))   if ex.get("Zeit_Hard_s")   else None,
            "max_soft_runden":  int(ex["Max_Soft_Runden"]),
            "reichweite_70pct": int(ex["Reichweite_70pct"]),
        }
        await interaction.followup.send("✅ Strategie wird berechnet...", ephemeral=True)
        await calculate_and_post(ch, n, tr, v, br, mo, l, data, settings)
        self.stop()

    @discord.ui.button(label="✏️ Daten anpassen", style=discord.ButtonStyle.primary)
    async def edit_data(self, interaction: discord.Interaction, button: Button):
        n, tr, v, br, mo, l, ex, settings, ch, hard = self.d
        prefill = build_prefill(float(ex["Zeit_Soft_s"]), n, settings, hard, existing=ex)
        await interaction.response.send_modal(make_modal(n, tr, v, br, mo, l, ch, hard, prefill=prefill))
        self.stop()


# ─────────────────────────────────────────────
# Detail-Ansicht: Buttons für einzelne Strategie
# ─────────────────────────────────────────────

class DetailSelectView(discord.ui.View):
    def __init__(self, strategies, base_soft_s, medium_plus_pct, hard_plus_pct,
                 max_soft_runden, fuel_per_lap, start_fuel, tank_size,
                 tank_rate_l_per_s, pit_loss_s, fuel_weight_s, channel,
                 verkehr_aufschlag_s=2.0, verkehr_runden=3):
        super().__init__(timeout=300)
        self.strategies   = strategies
        self.table_params = dict(
            base_soft_s=base_soft_s, medium_plus_pct=medium_plus_pct,
            hard_plus_pct=hard_plus_pct, max_soft_runden=max_soft_runden,
            fuel_per_lap=fuel_per_lap, start_fuel=start_fuel,
            tank_size=tank_size, tank_rate_l_per_s=tank_rate_l_per_s,
            pit_loss_s=pit_loss_s, fuel_weight_s=fuel_weight_s,
            verkehr_aufschlag_s=verkehr_aufschlag_s, verkehr_runden=verkehr_runden,
        )
        self.channel = channel

        # Button pro Strategie
        labels_short = {
            "1-Stopp Pole":       "📋 1-Stopp Pole",
            "2-Stopp Pole":       "📋 2-Stopp Pole",
            "1-Stopp Nicht-Pole": "📋 1-Stopp Nicht-Pole",
            "2-Stopp Nicht-Pole": "📋 2-Stopp Nicht-Pole",
        }
        styles = {
            "1-Stopp Pole":       discord.ButtonStyle.primary,
            "2-Stopp Pole":       discord.ButtonStyle.primary,
            "1-Stopp Nicht-Pole": discord.ButtonStyle.secondary,
            "2-Stopp Nicht-Pole": discord.ButtonStyle.secondary,
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

        # "Keine Anzeige" Button
        close_btn = discord.ui.Button(
            label="✖ Keine Anzeige",
            style=discord.ButtonStyle.danger,
            custom_id="close",
        )
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
            # Pole-Flag aus Label ableiten
            pole = "Nicht-Pole" not in label
            table = build_single_column(label, result, pole=pole, **self.table_params)
            # In Chunks aufteilen falls nötig
            lines = table.split("\n")
            chunk = "```\n"
            for line in lines:
                if len(chunk) + len(line) + 2 > 1950:
                    await self.channel.send(chunk + "```")
                    chunk = "```\n" + line + "\n"
                else:
                    chunk += line + "\n"
            await self.channel.send(chunk + "```")
            await interaction.followup.send("✅ Detailansicht wurde im Channel gepostet.", ephemeral=True)
            # Buttons erneut anzeigen für weitere Detailabfragen
            new_view = DetailSelectView(
                strategies=self.strategies,
                channel=self.channel,
                **self.table_params,
            )
            await self.channel.send("Weitere Detailansicht:", view=new_view)
            self.stop()
        return callback
