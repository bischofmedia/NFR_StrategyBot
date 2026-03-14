import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
import os

from sheets import (
    get_next_race, get_driver_data, ensure_driver_sheet,
    get_settings, get_brands_and_models, get_track_data
)
from ui import ConfirmDataView, make_modal, SuggestLapTimeView, seconds_to_display, build_prefill

load_dotenv(override=True)

TOKEN              = os.getenv("DISCORD_TOKEN")
ALLOWED_CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID"))
GUILD_ID           = int(os.getenv("DISCORD_GUILD_ID", "0"))

intents = discord.Intents.default()
intents.message_content = True

bot  = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree


@bot.event
async def on_ready():
    if GUILD_ID:
        guild = discord.Object(id=GUILD_ID)
        tree.copy_global_to(guild=guild)
        await tree.sync(guild=guild)
        print(f"✅ Bot eingeloggt als {bot.user} | Commands synchronisiert (Guild {GUILD_ID})")
    else:
        await tree.sync()
        print(f"✅ Bot eingeloggt als {bot.user} | Commands global synchronisiert")


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
            "Strecke oder Rundenanzahl im Kalender fehlt.", ephemeral=True
        )
        return

    brands_models = get_brands_and_models()
    if not brands_models:
        await interaction.followup.send(
            "Keine Fahrzeuge in den Stammdaten gefunden.", ephemeral=True
        )
        return

    track_display = f"{track} – {version}" if version else track
    view = BrandSelectView(
        nickname, track, version, total_laps,
        brands_models, channel, settings, hard_enabled
    )
    await interaction.followup.send(
        f"🏁 Nächstes Rennen: **{track_display}** ({total_laps} Runden)\n"
        f"Bitte wähle zuerst die **Marke**:",
        view=view,
        ephemeral=True
    )


# ─────────────────────────────────────────────
# View: Markenauswahl
# ─────────────────────────────────────────────

class BrandSelectView(discord.ui.View):
    def __init__(self, nickname, track, version, total_laps, brands_models, channel, settings, hard_enabled):
        super().__init__(timeout=120)
        self.nickname      = nickname
        self.track         = track
        self.version       = version
        self.total_laps    = total_laps
        self.brands_models = brands_models
        self.channel       = channel
        self.settings      = settings
        self.hard_enabled  = hard_enabled

        brands  = sorted(brands_models.keys())[:25]
        options = [discord.SelectOption(label=b, value=b) for b in brands]
        select  = discord.ui.Select(placeholder="Marke auswählen...", options=options)
        select.callback = self.brand_selected
        self.add_item(select)

    async def brand_selected(self, interaction: discord.Interaction):
        brand  = interaction.data["values"][0]
        models = self.brands_models.get(brand, [])

        # Nur ein Modell → direkt weiter ohne zweites Dropdown
        if len(models) == 1:
            await proceed_to_car(
                interaction, self.nickname, self.track, self.version,
                self.total_laps, brand, models[0],
                self.channel, self.settings, self.hard_enabled,
                edit=True
            )
        else:
            view = ModelSelectView(
                self.nickname, self.track, self.version, self.total_laps,
                brand, models, self.channel, self.settings, self.hard_enabled
            )
            await interaction.response.edit_message(
                content=f"Marke: **{brand}**\nBitte wähle das **Modell**:",
                view=view
            )


# ─────────────────────────────────────────────
# View: Modellauswahl
# ─────────────────────────────────────────────

class ModelSelectView(discord.ui.View):
    def __init__(self, nickname, track, version, total_laps, brand, models, channel, settings, hard_enabled):
        super().__init__(timeout=120)
        self.nickname     = nickname
        self.track        = track
        self.version      = version
        self.total_laps   = total_laps
        self.brand        = brand
        self.channel      = channel
        self.settings     = settings
        self.hard_enabled = hard_enabled

        options = [discord.SelectOption(label=m, value=m) for m in models[:25]]
        select  = discord.ui.Select(placeholder="Modell auswählen...", options=options)
        select.callback = self.model_selected
        self.add_item(select)

    async def model_selected(self, interaction: discord.Interaction):
        model = interaction.data["values"][0]
        await proceed_to_car(
            interaction, self.nickname, self.track, self.version,
            self.total_laps, self.brand, model,
            self.channel, self.settings, self.hard_enabled,
            edit=True
        )


# ─────────────────────────────────────────────
# Gemeinsame Logik nach Fahrzeugauswahl
# ─────────────────────────────────────────────

async def proceed_to_car(interaction, nickname, track, version, total_laps,
                         brand, model, channel, settings, hard_enabled, edit=True):
    existing = get_driver_data(nickname, track, version, brand, model)

    car_display = f"{brand} {model}"

    if existing:
        embed = discord.Embed(
            title="📋 Gespeicherte Daten gefunden",
            description=(
                f"Strecke: **{track}{' – ' + version if version else ''}**"
                f" | Fahrzeug: **{car_display}**"
            ),
            color=0xFFA500
        )
        embed.add_field(name="🔴 Soft",   value=seconds_to_display(float(existing["Zeit_Soft_s"])), inline=True)
        embed.add_field(name="🟡 Medium", value=seconds_to_display(float(existing["Zeit_Medium_s"])) if existing.get("Zeit_Medium_s") else "–", inline=True)
        if hard_enabled:
            embed.add_field(name="⚪ Hard", value=seconds_to_display(float(existing["Zeit_Hard_s"])) if existing.get("Zeit_Hard_s") else "–", inline=True)
        embed.add_field(name="Medium %",          value=f"{existing.get('Medium_Pct', '–')}%",         inline=True)
        if hard_enabled:
            embed.add_field(name="Hard %",        value=f"{existing.get('Hard_Pct', '–')}%",           inline=True)
        embed.add_field(name="Max. Soft-Runden",  value=existing["Max_Soft_Runden"],                   inline=True)
        embed.add_field(name="Reichweite 70%",    value=f"{existing['Reichweite_70pct']} Runden",      inline=True)
        embed.add_field(name="Zuletzt",           value=existing.get("Letzte_Aktualisierung", "–"),    inline=True)

        view = ConfirmDataView(
            nickname, track, version, brand, model,
            total_laps, existing, settings, channel, hard_enabled
        )
        if edit:
            await interaction.response.edit_message(content=None, embed=embed, view=view)
        else:
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    else:
        track_data = get_track_data(track, version)
        best_lap_s = track_data.get("best_lap_s")

        if best_lap_s:
            track_display = f"{track} – {version}" if version else track
            msg = (
                f"Keine gespeicherten Daten für **{track_display}** mit **{car_display}**.\n\n"
                f"Schnellste bekannte Runde: **{seconds_to_display(best_lap_s)}** (Soft)\n\n"
                f"Möchtest du diese Zeit als Basis verwenden?"
            )
            view = SuggestLapTimeView(
                nickname, track, version, brand, model,
                total_laps, channel, best_lap_s, settings, hard_enabled
            )
            if edit:
                await interaction.response.edit_message(content=msg, embed=None, view=view)
            else:
                await interaction.response.send_message(content=msg, view=view, ephemeral=True)
        else:
            prefill = build_prefill(0, nickname, settings, hard_enabled)
            modal   = make_modal(
                nickname, track, version, brand, model,
                total_laps, channel, hard_enabled, prefill=prefill
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
    embed.add_field(name="🏟️ Strecke", value=track_display,          inline=True)
    embed.add_field(name="📆 Datum",   value=race.get("Datum", "–"),  inline=True)
    embed.add_field(name="🔄 Runden",  value=race.get("Runden", "–"), inline=True)
    embed.set_footer(text="NFR Strategy Bot • GT7")

    await interaction.response.send_message(embed=embed)


# ─────────────────────────────────────────────
# Start
# ─────────────────────────────────────────────

bot.run(TOKEN)
