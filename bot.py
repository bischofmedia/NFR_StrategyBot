import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
import os

from sheets import (
    get_next_race, get_driver_data, ensure_driver_sheet,
    get_settings, get_brands_and_models, get_track_data,
    VALID_LEAGUES, DEFAULT_LEAGUE, normalise_league
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

LEAGUE_NAMES = {"rtc": "RTC", "awl": "AWL", "gtfun": "GTFUN"}


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

@tree.command(name="strategie", description="Berechne deine optimale Rennstrategie")
@app_commands.describe(liga="Liga auswählen (Standard: RTC)")
@app_commands.choices(liga=[
    app_commands.Choice(name="RTC",   value="rtc"),
    app_commands.Choice(name="AWL",   value="awl"),
    app_commands.Choice(name="GTFUN", value="gtfun"),
])
@check_channel()
async def strategie(interaction: discord.Interaction,
                    liga: app_commands.Choice[str] = None):
    await interaction.response.defer(thinking=True, ephemeral=True)

    league   = normalise_league(liga.value if liga else DEFAULT_LEAGUE)
    nickname = interaction.user.display_name
    channel  = interaction.channel
    ensure_driver_sheet(nickname, league)

    settings     = get_settings(league)
    hard_enabled = settings.get("hard_tyre_allowed", "FALSE").upper() == "TRUE"

    race = get_next_race(league)
    if not race:
        await interaction.followup.send(
            f"Kein nächstes Rennen im {LEAGUE_NAMES.get(league, league)}-Kalender gefunden.",
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

    track_display  = f"{track} – {version}" if version else track
    league_display = LEAGUE_NAMES.get(league, league.upper())
    view = BrandSelectView(
        nickname, track, version, total_laps,
        brands_models, channel, settings, hard_enabled, league
    )
    await interaction.followup.send(
        f"🏁 **{league_display}** | {track_display} ({total_laps} Runden)\n"
        f"Bitte wähle zuerst die **Marke**:",
        view=view,
        ephemeral=True
    )


# ─────────────────────────────────────────────
# Markenauswahl
# ─────────────────────────────────────────────

class BrandSelectView(discord.ui.View):
    def __init__(self, nickname, track, version, total_laps,
                 brands_models, channel, settings, hard_enabled, league):
        super().__init__(timeout=120)
        self.nickname      = nickname
        self.track         = track
        self.version       = version
        self.total_laps    = total_laps
        self.brands_models = brands_models
        self.channel       = channel
        self.settings      = settings
        self.hard_enabled  = hard_enabled
        self.league        = league

        brands  = sorted(brands_models.keys())[:25]
        options = [discord.SelectOption(label=b, value=b) for b in brands]
        select  = discord.ui.Select(placeholder="Marke auswählen...", options=options)
        select.callback = self.brand_selected
        self.add_item(select)

    async def brand_selected(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        brand  = interaction.data["values"][0]
        models = self.brands_models.get(brand, [])

        if len(models) == 1:
            await proceed_to_car(
                interaction, self.nickname, self.track, self.version,
                self.total_laps, brand, models[0],
                self.channel, self.settings, self.hard_enabled, self.league
            )
        else:
            view = ModelSelectView(
                self.nickname, self.track, self.version, self.total_laps,
                brand, models, self.channel, self.settings, self.hard_enabled, self.league
            )
            await interaction.edit_original_response(
                content=f"Marke: **{brand}**\nBitte wähle das **Modell**:",
                view=view
            )


# ─────────────────────────────────────────────
# Modellauswahl
# ─────────────────────────────────────────────

class ModelSelectView(discord.ui.View):
    def __init__(self, nickname, track, version, total_laps,
                 brand, models, channel, settings, hard_enabled, league):
        super().__init__(timeout=120)
        self.nickname     = nickname
        self.track        = track
        self.version      = version
        self.total_laps   = total_laps
        self.brand        = brand
        self.channel      = channel
        self.settings     = settings
        self.hard_enabled = hard_enabled
        self.league       = league

        options = [discord.SelectOption(label=m, value=m) for m in models[:25]]
        select  = discord.ui.Select(placeholder="Modell auswählen...", options=options)
        select.callback = self.model_selected
        self.add_item(select)

    async def model_selected(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        model = interaction.data["values"][0]
        await proceed_to_car(
            interaction, self.nickname, self.track, self.version,
            self.total_laps, self.brand, model,
            self.channel, self.settings, self.hard_enabled, self.league
        )


# ─────────────────────────────────────────────
# Gemeinsame Logik nach Fahrzeugauswahl
# ─────────────────────────────────────────────

async def proceed_to_car(interaction, nickname, track, version, total_laps,
                         brand, model, channel, settings, hard_enabled, league):
    existing    = get_driver_data(nickname, track, version, brand, model, league)
    car_display = f"{brand} {model}"

    async def send(content=None, embed=None, view=None):
        await interaction.edit_original_response(content=content, embed=embed, view=view)

    if existing:
        embed = discord.Embed(
            title="📋 Gespeicherte Daten gefunden",
            description=f"Strecke: **{track}{' – '+version if version else ''}** | Fahrzeug: **{car_display}**",
            color=0xFFA500
        )

        def to_f(v):
            try: return float(str(v).replace(",", "."))
            except: return None

        soft_f   = to_f(existing.get("Zeit_Soft_s"))
        medium_f = to_f(existing.get("Zeit_Medium_s"))
        hard_f   = to_f(existing.get("Zeit_Hard_s"))
        med_pct  = to_f(existing.get("Medium_Pct"))
        hard_pct = to_f(existing.get("Hard_Pct"))

        embed.add_field(name="🔴 Soft",   value=seconds_to_display(soft_f)   if soft_f   else "–", inline=True)
        embed.add_field(name="🟡 Medium", value=seconds_to_display(medium_f) if medium_f else "–", inline=True)
        if hard_enabled:
            embed.add_field(name="⚪ Hard", value=seconds_to_display(hard_f) if hard_f else "–", inline=True)
        embed.add_field(name="Medium %",         value=f"+{med_pct:.2f}%"  if med_pct  else "–", inline=True)
        if hard_enabled:
            embed.add_field(name="Hard %",       value=f"+{hard_pct:.2f}%" if hard_pct else "–", inline=True)
        embed.add_field(name="Max. Soft-Runden", value=existing.get("Max_Soft_Runden", "–"),      inline=True)
        embed.add_field(name="Reichweite 70%",   value=f"{existing.get('Reichweite_70pct','–')} Runden", inline=True)
        embed.add_field(name="Zuletzt",          value=existing.get("Letzte_Aktualisierung", "–"), inline=True)

        view = ConfirmDataView(
            nickname, track, version, brand, model,
            total_laps, existing, settings, channel, hard_enabled, league
        )
        await send(content=None, embed=embed, view=view)

    else:
        track_data = get_track_data(track, version)
        best_lap_s = track_data.get("best_lap_s")

        if best_lap_s:
            track_display = f"{track} – {version}" if version else track
            msg  = (
                f"Keine gespeicherten Daten für **{track_display}** mit **{car_display}**.\n\n"
                f"Schnellste bekannte Runde: **{seconds_to_display(best_lap_s)}** (Soft)\n\n"
                f"Möchtest du diese Zeit als Basis verwenden?"
            )
            view = SuggestLapTimeView(
                nickname, track, version, brand, model,
                total_laps, channel, best_lap_s, settings, hard_enabled, league
            )
            await send(content=msg, embed=None, view=view)
        else:
            prefill = build_prefill(0, nickname, settings, hard_enabled, league=league)
            modal   = make_modal(
                nickname, track, version, brand, model,
                total_laps, channel, hard_enabled, league, prefill=prefill
            )
            await interaction.edit_original_response(
                content="Bitte gib deine Zeiten ein:",
                view=OpenModalView(nickname, track, version, brand, model,
                                   total_laps, channel, hard_enabled, league, prefill)
            )


class OpenModalView(discord.ui.View):
    def __init__(self, nickname, track, version, brand, model,
                 total_laps, channel, hard_enabled, league, prefill):
        super().__init__(timeout=120)
        self.d = (nickname, track, version, brand, model,
                  total_laps, channel, hard_enabled, league, prefill)

    @discord.ui.button(label="✏️ Zeiten eingeben", style=discord.ButtonStyle.primary)
    async def open_modal(self, interaction: discord.Interaction, button: discord.ui.Button):
        n, tr, v, br, mo, l, ch, hard, league, prefill = self.d
        await interaction.response.send_modal(
            make_modal(n, tr, v, br, mo, l, ch, hard, league, prefill=prefill)
        )
        self.stop()


# ─────────────────────────────────────────────
# /naechstes_rennen
# ─────────────────────────────────────────────

@tree.command(name="naechstes_rennen", description="Zeigt das nächste Rennen im Kalender")
@app_commands.describe(liga="Liga auswählen (Standard: RTC)")
@app_commands.choices(liga=[
    app_commands.Choice(name="RTC",   value="rtc"),
    app_commands.Choice(name="AWL",   value="awl"),
    app_commands.Choice(name="GTFUN", value="gtfun"),
])
@check_channel()
async def naechstes_rennen(interaction: discord.Interaction,
                           liga: app_commands.Choice[str] = None):
    league = normalise_league(liga.value if liga else DEFAULT_LEAGUE)
    race   = get_next_race(league)
    if not race:
        await interaction.response.send_message("Kein Rennen im Kalender.", ephemeral=True)
        return

    track         = race.get("Strecke", "–")
    version       = race.get("Version", "")
    track_display = f"{track} – {version}" if version else track
    league_display = LEAGUE_NAMES.get(league, league.upper())

    embed = discord.Embed(title=f"📅 Nächstes Rennen – {league_display}", color=0x00BFFF)
    embed.add_field(name="🏟️ Strecke", value=track_display,          inline=True)
    embed.add_field(name="📆 Datum",   value=race.get("Datum", "–"),  inline=True)
    embed.add_field(name="🔄 Runden",  value=race.get("Runden", "–"), inline=True)
    embed.set_footer(text="NFR Strategy Bot • GT7")

    await interaction.response.send_message(embed=embed)


bot.run(TOKEN)
