import discord
from discord.ui import Modal, TextInput, View, Button
from strategy import (
    calculate_strategies, get_top_strategies, format_time,
    TYRE_SOFT, TYRE_MEDIUM, TYRE_HARD
)
from sheets import get_driver_data, save_driver_data, get_settings, get_track_data, get_driver_avg_pct

TYRE_EMOJI = {
    TYRE_SOFT:   "🔴",
    TYRE_MEDIUM: "🟡",
    TYRE_HARD:   "⚪",
}
MEDAL = ["🥇", "🥈", "🥉"]


# ─────────────────────────────────────────────
# Hilfsfunktionen
# ─────────────────────────────────────────────

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
# Kernlogik: Berechnen + öffentlich posten
# ─────────────────────────────────────────────

async def calculate_and_post(channel, nickname, track, version, car, total_laps, data, settings):
    hard_enabled = settings.get("hard", "TRUE").upper() != "FALSE"

    track_data = get_track_data(track, version)
    pit_loss = track_data["pit_loss_s"] or float(settings.get("pit_loss_s", 25))

    soft_s = data["zeit_soft_s"]
    medium_s = data["zeit_medium_s"]
    hard_s = data.get("zeit_hard_s") if hard_enabled else None

    # Prozentwerte für die Berechnungslogik
    medium_pct = (medium_s - soft_s) / soft_s * 100
    hard_pct = (hard_s - soft_s) / soft_s * 100 if hard_s else 0

    common = dict(
        total_laps=total_laps,
        base_time_soft_s=soft_s,
        medium_plus_pct=medium_pct,
        hard_plus_pct=hard_pct,
        hard_enabled=hard_enabled,
        max_soft_runden=data["max_soft_runden"],
        reichweite_70pct=data["reichweite_70pct"],
        tank_size=float(settings.get("tank_size_l", 100)),
        tank_rate_l_per_s=float(settings.get("tank_rate_l_per_s", 5)),
        pit_loss_s=pit_loss,
        start_fuel_pct=float(settings.get("start_fuel_pct", 70)),
        soft_required=settings.get("soft_stint_required", "TRUE").upper() == "TRUE",
        verkehr_aufschlag_s=float(settings.get("verkehr_aufschlag_s", 2.0)),
        verkehr_runden=int(settings.get("verkehr_runden", 3)),
    )

    results_pole    = calculate_strategies(**common, pole=True)
    results_no_pole = calculate_strategies(**common, pole=False)

    best_is_same = (
        results_pole and results_no_pole and
        results_pole[0].description == results_no_pole[0].description
    )

    embed = build_strategy_embed(
        results_pole, results_no_pole, track, version, car, total_laps, best_is_same, nickname
    )
    await channel.send(embed=embed)


# ─────────────────────────────────────────────
# Modal: Fahrerdaten eingeben / anpassen
# ─────────────────────────────────────────────

def make_modal(nickname, track, version, car, total_laps, channel,
               hard_enabled, prefill=None):
    """Erstellt das passende Modal – mit oder ohne Hard-Feld."""

    if hard_enabled:
        class DriverDataModalHard(Modal, title="Deine Daten eingeben"):
            zeit_soft   = TextInput(label="Rundenzeit auf Soft (m:ss.mmm)",   placeholder="z.B. 1:58.450", required=True)
            zeit_medium = TextInput(label="Rundenzeit auf Medium (m:ss.mmm)", placeholder="z.B. 1:59.300", required=True)
            zeit_hard   = TextInput(label="Rundenzeit auf Hard (m:ss.mmm)",   placeholder="z.B. 2:01.000", required=True)
            max_soft    = TextInput(label="Maximale Runden auf Soft",         placeholder="z.B. 10",       required=True)
            reichweite  = TextInput(label="Reichweite bei 70% Tank (Runden)", placeholder="z.B. 18",       required=True)

            def __init__(self):
                super().__init__()
                self._nickname   = nickname
                self._track      = track
                self._version    = version
                self._car        = car
                self._total_laps = total_laps
                self._channel    = channel
                if prefill:
                    self.zeit_soft.default   = prefill.get("Zeit_Soft",   "")
                    self.zeit_medium.default = prefill.get("Zeit_Medium", "")
                    self.zeit_hard.default   = prefill.get("Zeit_Hard",   "")
                    self.max_soft.default    = str(prefill.get("Max_Soft_Runden", ""))
                    self.reichweite.default  = str(prefill.get("Reichweite_70pct", ""))

            async def on_submit(self, interaction: discord.Interaction):
                await _handle_submit(
                    interaction, self._nickname, self._track, self._version,
                    self._car, self._total_laps, self._channel,
                    str(self.zeit_soft), str(self.zeit_medium),
                    str(self.zeit_hard), str(self.max_soft), str(self.reichweite)
                )

        return DriverDataModalHard()

    else:
        class DriverDataModalNoHard(Modal, title="Deine Daten eingeben"):
            zeit_soft   = TextInput(label="Rundenzeit auf Soft (m:ss.mmm)",   placeholder="z.B. 1:58.450", required=True)
            zeit_medium = TextInput(label="Rundenzeit auf Medium (m:ss.mmm)", placeholder="z.B. 1:59.300", required=True)
            max_soft    = TextInput(label="Maximale Runden auf Soft",         placeholder="z.B. 10",       required=True)
            reichweite  = TextInput(label="Reichweite bei 70% Tank (Runden)", placeholder="z.B. 18",       required=True)

            def __init__(self):
                super().__init__()
                self._nickname   = nickname
                self._track      = track
                self._version    = version
                self._car        = car
                self._total_laps = total_laps
                self._channel    = channel
                if prefill:
                    self.zeit_soft.default   = prefill.get("Zeit_Soft",   "")
                    self.zeit_medium.default = prefill.get("Zeit_Medium", "")
                    self.max_soft.default    = str(prefill.get("Max_Soft_Runden", ""))
                    self.reichweite.default  = str(prefill.get("Reichweite_70pct", ""))

            async def on_submit(self, interaction: discord.Interaction):
                await _handle_submit(
                    interaction, self._nickname, self._track, self._version,
                    self._car, self._total_laps, self._channel,
                    str(self.zeit_soft), str(self.zeit_medium),
                    None, str(self.max_soft), str(self.reichweite)
                )

        return DriverDataModalNoHard()


async def _handle_submit(interaction, nickname, track, version, car, total_laps, channel,
                         raw_soft, raw_medium, raw_hard, raw_max_soft, raw_reichweite):
    try:
        soft_s   = parse_time(raw_soft)
        medium_s = parse_time(raw_medium)
        hard_s   = parse_time(raw_hard) if raw_hard else None
        max_soft = int(raw_max_soft.strip())
        reichweite = int(raw_reichweite.strip())
    except ValueError:
        await interaction.response.send_message(
            "❌ Ungültige Eingabe. Bitte prüfe deine Zeitangaben (Format: m:ss.mmm).",
            ephemeral=True
        )
        return

    data = {
        "zeit_soft_s":   soft_s,
        "zeit_medium_s": medium_s,
        "zeit_hard_s":   hard_s,
        "max_soft_runden": max_soft,
        "reichweite_70pct": reichweite,
    }

    save_driver_data(nickname, track, version, car, data)
    settings = get_settings()

    await interaction.response.send_message(
        "✅ Daten gespeichert! Strategie wird berechnet...", ephemeral=True
    )
    await calculate_and_post(channel, nickname, track, version, car, total_laps, data, settings)


# ─────────────────────────────────────────────
# Prefill-Helfer: Vorschlagswerte zusammenstellen
# ─────────────────────────────────────────────

def build_prefill(soft_s: float, nickname: str, settings: dict, hard_enabled: bool,
                  existing: dict | None = None) -> dict:
    """
    Baut das prefill-Dict für das Modal.
    Priorität: gespeicherte Werte > Fahrer-Durchschnitt > Settings-Standard
    """
    if existing:
        return {
            "Zeit_Soft":       seconds_to_display(float(existing["Zeit_Soft_s"])),
            "Zeit_Medium":     seconds_to_display(float(existing["Zeit_Medium_s"])) if existing.get("Zeit_Medium_s") else "",
            "Zeit_Hard":       seconds_to_display(float(existing["Zeit_Hard_s"])) if existing.get("Zeit_Hard_s") else "",
            "Max_Soft_Runden": existing.get("Max_Soft_Runden", ""),
            "Reichweite_70pct": existing.get("Reichweite_70pct", ""),
        }

    # Kein gespeicherter Eintrag – Durchschnitt oder Standard verwenden
    avg = get_driver_avg_pct(nickname)

    medium_pct = avg["medium_pct"] if avg["medium_pct"] is not None \
        else float(settings.get("medium_default_pct", 1.0))
    hard_pct   = avg["hard_pct"] if avg["hard_pct"] is not None \
        else float(settings.get("hard_default_pct", 2.5))

    prefill = {
        "Zeit_Soft":       seconds_to_display(soft_s),
        "Zeit_Medium":     seconds_to_display(apply_pct(soft_s, medium_pct)),
        "Max_Soft_Runden": "",
        "Reichweite_70pct": "",
    }
    if hard_enabled:
        prefill["Zeit_Hard"] = seconds_to_display(apply_pct(soft_s, hard_pct))

    src = "deinem Durchschnitt" if avg["medium_pct"] is not None else "Standardwerten"
    prefill["_hint"] = f"Medium/Hard-Zeiten basieren auf {src} – bitte prüfen!"

    return prefill


# ─────────────────────────────────────────────
# View: Vorschlag aus Zeiten-Sheet
# ─────────────────────────────────────────────

class SuggestLapTimeView(View):
    def __init__(self, nickname, track, version, car, total_laps, channel,
                 suggested_s, settings, hard_enabled):
        super().__init__(timeout=120)
        self.nickname     = nickname
        self.track        = track
        self.version      = version
        self.car          = car
        self.total_laps   = total_laps
        self.channel      = channel
        self.suggested_s  = suggested_s
        self.settings     = settings
        self.hard_enabled = hard_enabled

    @discord.ui.button(label="✅ Vorgeschlagene Zeit verwenden", style=discord.ButtonStyle.success)
    async def use_suggestion(self, interaction: discord.Interaction, button: Button):
        prefill = build_prefill(
            self.suggested_s, self.nickname, self.settings,
            self.hard_enabled, existing=None
        )
        modal = make_modal(
            self.nickname, self.track, self.version, self.car,
            self.total_laps, self.channel, self.hard_enabled, prefill=prefill
        )
        await interaction.response.send_modal(modal)
        self.stop()

    @discord.ui.button(label="✏️ Eigene Zeit eingeben", style=discord.ButtonStyle.primary)
    async def enter_own(self, interaction: discord.Interaction, button: Button):
        modal = make_modal(
            self.nickname, self.track, self.version, self.car,
            self.total_laps, self.channel, self.hard_enabled
        )
        await interaction.response.send_modal(modal)
        self.stop()


# ─────────────────────────────────────────────
# View: Bestätigung mit vorhandenen Daten
# ─────────────────────────────────────────────

class ConfirmDataView(View):
    def __init__(self, nickname, track, version, car, total_laps,
                 existing_data, settings, channel, hard_enabled):
        super().__init__(timeout=120)
        self.nickname      = nickname
        self.track         = track
        self.version       = version
        self.car           = car
        self.total_laps    = total_laps
        self.existing_data = existing_data
        self.settings      = settings
        self.channel       = channel
        self.hard_enabled  = hard_enabled

    @discord.ui.button(label="✅ Daten verwenden", style=discord.ButtonStyle.success)
    async def use_data(self, interaction: discord.Interaction, button: Button):
        d = self.existing_data
        data = {
            "zeit_soft_s":    float(d["Zeit_Soft_s"]),
            "zeit_medium_s":  float(d["Zeit_Medium_s"]) if d.get("Zeit_Medium_s") else None,
            "zeit_hard_s":    float(d["Zeit_Hard_s"])   if d.get("Zeit_Hard_s")   else None,
            "max_soft_runden": int(d["Max_Soft_Runden"]),
            "reichweite_70pct": int(d["Reichweite_70pct"]),
        }
        await interaction.response.send_message(
            "✅ Strategie wird berechnet...", ephemeral=True
        )
        await calculate_and_post(
            self.channel, self.nickname, self.track, self.version,
            self.car, self.total_laps, data, self.settings
        )
        self.stop()

    @discord.ui.button(label="✏️ Daten anpassen", style=discord.ButtonStyle.primary)
    async def edit_data(self, interaction: discord.Interaction, button: Button):
        prefill = build_prefill(
            float(self.existing_data["Zeit_Soft_s"]),
            self.nickname, self.settings, self.hard_enabled,
            existing=self.existing_data
        )
        modal = make_modal(
            self.nickname, self.track, self.version, self.car,
            self.total_laps, self.channel, self.hard_enabled, prefill=prefill
        )
        await interaction.response.send_modal(modal)
        self.stop()


# ─────────────────────────────────────────────
# Embed-Ausgabe
# ─────────────────────────────────────────────

def build_strategy_embed(
    results_pole, results_no_pole, track, version, car,
    total_laps, best_is_same, nickname
) -> discord.Embed:

    track_display = f"{track} – {version}" if version else track
    embed = discord.Embed(
        title=f"🏁 Strategieanalyse – {track_display}",
        description=f"👤 **{nickname}** | 🚗 {car} | 🔄 {total_laps} Runden",
        color=0x00BFFF
    )

    def add_results(label, results):
        if label:
            embed.add_field(name=label, value="\u200b", inline=False)
        if not results:
            embed.add_field(name="\u200b", value="Keine valide Strategie gefunden.", inline=False)
            return
        top = get_top_strategies(results, 3)
        best_time = top[0].total_time_s
        for i, r in enumerate(top):
            delta = r.total_time_s - best_time
            delta_str = f"+{format_time(delta)}" if delta > 0 else "Beste Zeit"
            stint_str = "".join(
                f"{TYRE_EMOJI[t]} {t}: {runden} Runde{'n' if runden > 1 else ''}\n"
                for t, runden in r.stints
            )
            stops_str = f"{r.pit_stops} Stopp{'s' if r.pit_stops != 1 else ''}"
            if r.fuel_stops:
                stops_str += f" (Tanken bei Stopp {', '.join(str(s+1) for s in r.fuel_stops)})"
            explanation = explain_strategy(r, top[0] if i > 0 else None, i)
            embed.add_field(
                name=f"{MEDAL[i]} Variante {i+1}  –  {seconds_to_display(r.total_time_s)}  ({delta_str})",
                value=f"```\n{stint_str}```{stops_str}\n_{explanation}_",
                inline=False
            )

    if best_is_same:
        embed.add_field(
            name="ℹ️ Hinweis",
            value="Optimale Strategie ist identisch für **Pole** und **Nicht-Pole**.",
            inline=False
        )
        add_results("", results_pole)
    else:
        add_results("🟢 Von der Pole", results_pole)
        embed.add_field(name="\u200b", value="\u200b", inline=False)
        add_results("🟠 Nicht von der Pole", results_no_pole)

    embed.set_footer(text="NFR Strategy Bot • GT7")
    return embed


def explain_strategy(result, best=None, rank: int = 0) -> str:
    parts = []
    if rank == 0:
        if result.pit_stops == 0:
            parts.append("Keine Stopps nötig – maximale Zeit auf der Strecke.")
        elif result.pit_stops == 1:
            parts.append("Ein Stopp hält den Zeitverlust in der Box minimal.")
        else:
            parts.append(f"{result.pit_stops} Stopps ermöglichen frische Reifen in den entscheidenden Phasen.")
        soft_runden = sum(r for t, r in result.stints if t == TYRE_SOFT)
        if soft_runden:
            parts.append(f"Soft über {soft_runden} Runden für maximale Pace.")
        if result.fuel_stops:
            parts.append("Tankstopp optimal in den Reifenwechsel integriert.")
    else:
        if best:
            diff = result.total_time_s - best.total_time_s
            parts.append(f"Verliert {format_time(diff)} gegenüber Variante 1.")
        if result.pit_stops > (best.pit_stops if best else 0):
            parts.append("Mehr Stopps bedeuten mehr Zeitverlust in der Box.")
        elif result.pit_stops < (best.pit_stops if best else 0):
            parts.append("Weniger Stopps, aber ältere Reifen am Ende.")
    return " ".join(parts) if parts else "Valide Strategie."
