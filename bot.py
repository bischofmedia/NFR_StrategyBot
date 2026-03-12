import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
import os

from sheets import (
    get_next_race, get_driver_data, ensure_driver_sheet,
    get_settings, get_cars, get_track_data
)
from ui import ConfirmDataView, make_modal, SuggestLapTimeView, seconds_to_display, build_prefill

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
ALLOWED_CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID"))

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree


@bot.event
async def on_ready():
    await tree.sync()
    print(f"✅ Bot eingeloggt als {bot.user} | Commands synchronisiert")


def check_channel():
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.channel_id != ALLOWED_CHANNEL_ID:
            await interaction.response.send_message(
                f"Dieser Befehl ist nur in <#{ALLOWED_CHANNEL_ID}> verfügbar.",
                ephemeral=True
            )
            return False
        return True
    return app_commands.check(predicate)


# ─────────────────────────────────────────────
# /strategie
# ─────────────────────────────────────────────

@tree.command(name="strategie", description="Berechne deine optimale Rennstrategie für das nächste Rennen")
@check_channel()
async def strategie(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True, ephemeral=True)

    nickname = interaction.user.display_name
    channel  = interaction.channel
    ensure_driver_sheet(nickname)

    settings     = get_settings()
    hard_enabled = settings.get("hard", "TRUE").upper() != "FALSE"

    race = get_next_race()
    if not race:
        await interaction.followup.send(
            "Kein nächstes Rennen im Kalender gefunden. Bitte den Admin kontaktieren.",
            ephemeral=True
        )
        return

    track      = race.get("Strecke", "")
    version    = race.get("Version", "")
    total_laps = int(race.get("Runden", 0))

    if not track or total_laps == 0:
        await interaction.followup.send(
            "Strecke oder Rundenanzahl im Kalender fehlt. Bitte den Admin kontaktieren.",
            ephemeral=True
        )
        return

    cars = get_cars()
    if not cars:
        await interaction.followup.send(
            "Keine Fahrzeuge in den Stammdaten gefunden.", ephemeral=True
        )
        return

    track_display = f"{track} – {version}" if version else track
    view = CarSelectView(nickname, track, version, total_laps, cars, channel, settings, hard_enabled)
    await interaction.followup.send(
        f"🏁 Nächstes Rennen: **{track_display}** ({total_laps} Runden)\n"
        f"Bitte wähle dein Fahrzeug:",
        view=view,
        ephemeral=True
    )


# ─────────────────────────────────────────────
# View: Fahrzeugauswahl
# ─────────────────────────────────────────────

class CarSelectView(discord.ui.View):
    def __init__(self, nickname, track, version, total_laps, cars, channel, settings, hard_enabled):
        super().__init__(timeout=120)
        self.nickname     = nickname
        self.track        = track
        self.version      = version
        self.total_laps   = total_laps
        self.channel      = channel
        self.settings     = settings
        self.hard_enabled = hard_enabled

        options = [discord.SelectOption(label=car, value=car) for car in cars[:25]]
        select  = discord.ui.Select(placeholder="Fahrzeug auswählen...", options=options)
        select.callback = self.car_selected
        self.add_item(select)

    async def car_selected(self, interaction: discord.Interaction):
        car      = interaction.data["values"][0]
        existing = get_driver_data(self.nickname, self.track, self.version, car)

        if existing:
            embed = discord.Embed(
                title="📋 Gespeicherte Daten gefunden",
                description=(
                    f"Strecke: **{self.track}"
                    + (f" – {self.version}" if self.version else "")
                    + f"** | Fahrzeug: **{car}**"
                ),
                color=0xFFA500
            )
            embed.add_field(name="🔴 Soft",   value=seconds_to_display(float(existing["Zeit_Soft_s"])),   inline=True)
            embed.add_field(name="🟡 Medium", value=seconds_to_display(float(existing["Zeit_Medium_s"])) if existing.get("Zeit_Medium_s") else "–", inline=True)
            if self.hard_enabled:
                embed.add_field(name="⚪ Hard", value=seconds_to_display(float(existing["Zeit_Hard_s"])) if existing.get("Zeit_Hard_s") else "–", inline=True)
            embed.add_field(name="Medium %", value=f"{existing.get('Medium_Pct', '–')}%", inline=True)
            if self.hard_enabled:
                embed.add_field(name="Hard %", value=f"{existing.get('Hard_Pct', '–')}%", inline=True)
            embed.add_field(name="Max. Soft-Runden",  value=existing["Max_Soft_Runden"],          inline=True)
            embed.add_field(name="Reichweite 70%",    value=f"{existing['Reichweite_70pct']} Runden", inline=True)
            embed.add_field(name="Zuletzt aktualisiert", value=existing.get("Letzte_Aktualisierung", "–"), inline=True)

            view = ConfirmDataView(
                self.nickname, self.track, self.version, car,
                self.total_laps, existing, self.settings, self.channel, self.hard_enabled
            )
            await interaction.response.edit_message(content=None, embed=embed, view=view)

        else:
            track_data = get_track_data(self.track, self.version)
            best_lap_s = track_data.get("best_lap_s")

            if best_lap_s:
                track_display = f"{self.track} – {self.version}" if self.version else self.track
                await interaction.response.edit_message(
                    content=(
                        f"Keine gespeicherten Daten für **{track_display}** mit **{car}**.\n\n"
                        f"Schnellste bekannte Runde auf dieser Strecke: "
                        f"**{seconds_to_display(best_lap_s)}** (auf Soft)\n\n"
                        f"Möchtest du diese Zeit als Basis verwenden?"
                    ),
                    embed=None,
                    view=SuggestLapTimeView(
                        self.nickname, self.track, self.version, car,
                        self.total_laps, self.channel, best_lap_s,
                        self.settings, self.hard_enabled
                    )
                )
            else:
                # Kein Vorschlag – Modal mit Durchschnitts-Prefill öffnen
                prefill = build_prefill(
                    0, self.nickname, self.settings, self.hard_enabled, existing=None
                )
                modal = make_modal(
                    self.nickname, self.track, self.version, car,
                    self.total_laps, self.channel, self.hard_enabled, prefill=prefill
                )
                await interaction.response.send_modal(modal)


# ─────────────────────────────────────────────
# /naechstes_rennen
# ─────────────────────────────────────────────

@tree.command(name="naechstes_rennen", description="Zeigt das nächste Rennen im Kalender")
@check_channel()
async def naechstes_rennen(interaction: discord.Interaction):
    race = get_next_race()
    if not race:
        await interaction.response.send_message("Kein Rennen im Kalender.", ephemeral=True)
        return

    track   = race.get("Strecke", "–")
    version = race.get("Version", "")
    track_display = f"{track} – {version}" if version else track

    embed = discord.Embed(title="📅 Nächstes Rennen", color=0x00BFFF)
    embed.add_field(name="🏟️ Strecke", value=track_display, inline=True)
    embed.add_field(name="📆 Datum",   value=race.get("Datum", "–"),  inline=True)
    embed.add_field(name="🔄 Runden",  value=race.get("Runden", "–"), inline=True)
    embed.set_footer(text="NFR Strategy Bot • GT7")

    await interaction.response.send_message(embed=embed)


# ─────────────────────────────────────────────
# Start
# ─────────────────────────────────────────────

bot.run(TOKEN)
