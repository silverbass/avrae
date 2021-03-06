"""
Created on Jan 19, 2017

@author: andrew
"""
import asyncio
import copy
import logging
import re
import shlex
import sys
import traceback
from socket import timeout

import aiohttp
import discord
import pygsheets
from discord.ext import commands
from discord.ext.commands.cooldowns import BucketType
from googleapiclient.errors import HttpError
from pygsheets.exceptions import NoValidUrlKeyFound, SpreadsheetNotFound

from cogs5e.funcs import scripting
from cogs5e.funcs.dice import roll
from cogs5e.funcs.sheetFuncs import sheet_attack
from cogs5e.models import embeds
from cogs5e.models.character import Character, SKILL_MAP
from cogs5e.models.embeds import EmbedWithCharacter
from cogs5e.models.errors import AvraeException, InvalidArgument
from cogs5e.sheets.beyond import BeyondSheetParser
from cogs5e.sheets.dicecloud import DicecloudParser
from cogs5e.sheets.gsheet import GoogleSheet
from utils.argparser import argparse
from utils.functions import a_or_an, auth_and_chan, format_d20, get_positivity, list_get
from utils.functions import camel_to_title, extract_gsheet_id_from_url, generate_token, search_and_select, verbose_stat

log = logging.getLogger(__name__)


class SheetManager:
    """Commands to import a character sheet from [Dicecloud](https://dicecloud.com),
    a [Google Sheet](https://gsheet.avrae.io), or a D&D Beyond PDF."""

    def __init__(self, bot):
        self.bot = bot

        self.gsheet_client = None
        self._gsheet_initializing = False
        self.bot.loop.create_task(self.init_gsheet_client())

    async def init_gsheet_client(self):
        if self._gsheet_initializing:
            return
        self._gsheet_initializing = True

        def _():
            return pygsheets.authorize(service_file='avrae-google.json', no_cache=True)

        self.gsheet_client = await self.bot.loop.run_in_executor(None, _)
        self._gsheet_initializing = False

    async def new_arg_stuff(self, args, ctx, character):
        args = await scripting.parse_snippets(args, ctx)
        args = await character.parse_cvars(args, ctx)
        args = shlex.split(args)
        args = argparse(args)
        return args

    @commands.group(aliases=['a'], invoke_without_command=True)
    async def attack(self, ctx, atk_name=None, *, args: str = ''):
        """Rolls an attack for the current active character.
        __Valid Arguments__
        adv/dis
        adv#/dis# (applies adv to the first # attacks)
        ea (Elven Accuracy double advantage)
        
        -ac [target ac]
        -t [target]
        
        -b [to hit bonus]
        -criton [a number to crit on if rolled on or above]
        -d [damage bonus]
        -d# [applies damage to the first # hits]
        -c [damage bonus on crit]
        -rr [times to reroll]
        
        -resist [damage resistance]
        -immune [damage immunity]
        -vuln [damage vulnerability]
        -neutral [damage non-resistance]
        
        hit (automatically hits)
        miss (automatically misses)
        crit (automatically crit)
        max (deals max damage)
        
        -phrase [flavor text]
        -title [title] *note: [charname], [aname], and [target] will be replaced automatically*
        -f "Field Title|Field Text" (see !embed)
        -h (hides attack details)
        [user snippet]"""
        if atk_name is None:
            return await ctx.invoke(self.attack_list)

        char = await Character.from_ctx(ctx)
        attacks = char.get_attacks()

        try:  # fuzzy search for atk_name
            attack = next(a for a in attacks if atk_name.lower() == a.get('name').lower())
        except StopIteration:
            try:
                attack = next(a for a in attacks if atk_name.lower() in a.get('name').lower())
            except StopIteration:
                return await ctx.send('No attack with that name found.')

        args = await self.new_arg_stuff(args, ctx, char)
        args['name'] = char.get_name()
        args['criton'] = args.last('criton') or char.get_setting('criton', 20)
        args['reroll'] = char.get_setting('reroll', 0)
        args['critdice'] = int(char.get_setting('hocrit', False)) + char.get_setting('critdice', 0)
        args['crittype'] = char.get_setting('crittype', 'default')
        if attack.get('details') is not None:
            try:
                attack['details'] = await char.parse_cvars(attack['details'], ctx)
            except AvraeException:
                pass  # failed to eval, probably DDB nonsense

        result = sheet_attack(attack, args, EmbedWithCharacter(char, name=False))
        embed = result['embed']
        if args.last('h', type_=bool):
            try:
                await ctx.author.send(embed=result['full_embed'])
            except:
                pass

        _fields = args.get('f')
        embeds.add_fields_from_args(embed, _fields)

        await ctx.send(embed=embed)
        try:
            await ctx.message.delete()
        except:
            pass

    @attack.command(name="list")
    async def attack_list(self, ctx):
        """Lists the active character's attacks."""
        char = await Character.from_ctx(ctx)
        attacks = char.get_attacks()

        tempAttacks = []
        for a in attacks:
            damage = a['damage'] if a['damage'] is not None else 'no'
            if a['attackBonus'] is not None:
                try:
                    bonus = roll(a['attackBonus']).total
                except:
                    bonus = a['attackBonus']
                tempAttacks.append(f"**{a['name']}:** +{bonus} To Hit, {damage} damage.")
            else:
                tempAttacks.append(f"**{a['name']}:** {damage} damage.")
        if not tempAttacks:
            tempAttacks = ['No attacks.']
        a = '\n'.join(tempAttacks)
        if len(a) > 2000:
            a = ', '.join(atk['name'] for atk in attacks)
        if len(a) > 2000:
            a = "Too many attacks, values hidden!"
        return await ctx.send("{}'s attacks:\n{}".format(char.get_name(), a))

    @attack.command(name="add", aliases=['create'])
    async def attack_add(self, ctx, name, *, args=""):
        """
        Adds an attack to the active character.
        __Arguments__
        -d [damage]: How much damage the attack should do.
        -b [to-hit]: The to-hit bonus of the attack.
        -desc [description]: A description of the attack.
        """
        parsed = argparse(args)
        attack = {
            "name": name,
            "attackBonus": parsed.join('b', '+'),
            "damage": parsed.join('d', '+'),
            "details": parsed.join('desc', '\n')
        }
        character = await Character.from_ctx(ctx)
        attack_overrides = character.get_override("attacks", [])
        duplicate = next((a for a in attack_overrides if a['name'].lower() == attack['name'].lower()), None)
        if duplicate:
            attack_overrides.remove(duplicate)
        attack_overrides.append(attack)
        character.set_override("attacks", attack_overrides)

        await character.commit(ctx)
        out = f"Created attack {attack['name']}!"
        if duplicate:
            out += f" Removed a duplicate attack."
        await ctx.send(out)

    @attack.command(name="delete", aliases=['remove'])
    async def attack_delete(self, ctx, name):
        """
        Deletes an attack override.
        """
        character = await Character.from_ctx(ctx)
        attack_overrides = character.get_override("attacks", [])
        attack = await search_and_select(ctx, attack_overrides, name, lambda a: a['name'])

        attack_overrides.remove(attack)
        character.set_override("attacks", attack_overrides)
        await character.commit(ctx)

        await ctx.send(f"Okay, deleted attack {attack['name']}.")

    @commands.command(aliases=['s'])
    async def save(self, ctx, skill, *, args: str = ''):
        """Rolls a save for your current active character.
        __Valid Arguments__
        adv/dis
        -b [conditional bonus]
        -phrase [flavor text]
        -title [title] *note: [charname] and [sname] will be replaced automatically*
        -image [image URL]
        -dc [dc] (does not apply to Death Saves)
        -rr [iterations] (does not apply to Death Saves)"""
        if skill == 'death':
            ds_cmd = self.bot.get_command('game deathsave')
            if ds_cmd is None:
                return await ctx.send("Error: GameTrack cog not loaded.")
            return await ctx.invoke(ds_cmd, *shlex.split(args))

        char = await Character.from_ctx(ctx)
        saves = char.get_saves()
        if not saves:
            return await ctx.send('You must update your character sheet first.')
        try:
            save = next(a for a in saves.keys() if skill.lower() == a.lower())
        except StopIteration:
            try:
                save = next(a for a in saves.keys() if skill.lower() in a.lower())
            except StopIteration:
                return await ctx.send('That\'s not a valid save.')

        embed = EmbedWithCharacter(char, name=False)

        skill_effects = char.get_skill_effects()
        args += ' ' + skill_effects.get(save, '')  # dicecloud v11 - autoadv

        args = await self.new_arg_stuff(args, ctx, char)
        adv = args.adv()
        b = args.join('b', '+')
        phrase = args.join('phrase', '\n')
        iterations = min(args.last('rr', 1, int), 25)
        dc = args.last('dc', type_=int)
        num_successes = 0

        formatted_d20 = format_d20(adv, char.get_setting('reroll'))

        if b is not None:
            roll_str = formatted_d20 + '{:+}'.format(saves[save]) + '+' + b
        else:
            roll_str = formatted_d20 + '{:+}'.format(saves[save])

        embed.title = args.last('title', '') \
                          .replace('[charname]', char.get_name()) \
                          .replace('[sname]', camel_to_title(save)) \
                      or '{} makes {}!'.format(char.get_name(), a_or_an(camel_to_title(save)))

        if iterations > 1:
            embed.description = (f"**DC {dc}**\n" if dc else '') + ('*' + phrase + '*' if phrase is not None else '')
            for i in range(iterations):
                result = roll(roll_str, adv=adv, inline=True)
                if dc and result.total >= dc:
                    num_successes += 1
                embed.add_field(name=f"Save {i+1}", value=result.skeleton)
            if dc:
                embed.set_footer(text=f"{num_successes} Successes | {iterations - num_successes} Failues")
        else:
            result = roll(roll_str, adv=adv, inline=True)
            if dc:
                embed.set_footer(text="Success!" if result.total >= dc else "Failure!")
            embed.description = (f"**DC {dc}**\n" if dc else '') + result.skeleton + (
                '\n*' + phrase + '*' if phrase is not None else '')

        embeds.add_fields_from_args(embed, args.get('f'))

        if args.last('image') is not None:
            embed.set_thumbnail(url=args.last('image'))

        await ctx.send(embed=embed)
        try:
            await ctx.message.delete()
        except:
            pass

    @commands.command(aliases=['c'])
    async def check(self, ctx, check, *, args: str = ''):
        """Rolls a check for your current active character.
        __Valid Arguments__
        adv/dis
        -b [conditional bonus]
        -mc [minimum roll]
        -phrase [flavor text]
        -title [title] *note: [charname] and [cname] will be replaced automatically*
        -dc [dc]
        -rr [iterations]
        str/dex/con/int/wis/cha (different skill base; e.g. Strength (Intimidation))
        """
        char = await Character.from_ctx(ctx)
        skills = char.get_skills()
        if not skills:
            return await ctx.send('You must update your character sheet first.')
        try:
            skill = next(a for a in skills.keys() if check.lower() == a.lower())
        except StopIteration:
            try:
                skill = next(a for a in skills.keys() if check.lower() in a.lower())
            except StopIteration:
                return await ctx.send('That\'s not a valid check.')

        embed = EmbedWithCharacter(char, False)

        skill_effects = char.get_skill_effects()
        args += ' ' + skill_effects.get(skill, '')  # dicecloud v7 - autoadv

        args = await self.new_arg_stuff(args, ctx, char)
        adv = args.adv()
        b = args.join('b', '+')
        phrase = args.join('phrase', '\n')
        iterations = min(args.last('rr', 1, int), 25)
        dc = args.last('dc', type_=int)
        num_successes = 0

        formatted_d20 = format_d20(adv, char.get_setting('reroll'))

        mc = args.last('mc', None)
        if mc:
            formatted_d20 = f"{formatted_d20}mi{mc}"

        mod = skills[skill]
        skill_name = skill
        if any(args.last(s, type_=bool) for s in ("str", "dex", "con", "int", "wis", "cha")):
            base = next(s for s in ("str", "dex", "con", "int", "wis", "cha") if args.last(s, type_=bool))
            mod = mod - char.get_mod(SKILL_MAP[skill]) + char.get_mod(base)
            skill_name = f"{verbose_stat(base)} ({skill})"

        skill_name = camel_to_title(skill_name)
        default_title = '{} makes {} check!'.format(char.get_name(), a_or_an(skill_name))

        if b is not None:
            roll_str = formatted_d20 + '{:+}'.format(mod) + '+' + b
        else:
            roll_str = formatted_d20 + '{:+}'.format(mod)

        embed.title = args.last('title', '') \
                          .replace('[charname]', char.get_name()) \
                          .replace('[cname]', skill_name) \
                      or default_title

        if iterations > 1:
            embed.description = (f"**DC {dc}**\n" if dc else '') + ('*' + phrase + '*' if phrase is not None else '')
            for i in range(iterations):
                result = roll(roll_str, adv=adv, inline=True)
                if dc and result.total >= dc:
                    num_successes += 1
                embed.add_field(name=f"Check {i+1}", value=result.skeleton)
            if dc:
                embed.set_footer(text=f"{num_successes} Successes | {iterations - num_successes} Failues")
        else:
            result = roll(roll_str, adv=adv, inline=True)
            if dc:
                embed.set_footer(text="Success!" if result.total >= dc else "Failure!")
            embed.description = (f"**DC {dc}**\n" if dc else '') + result.skeleton + (
                '\n*' + phrase + '*' if phrase is not None else '')

        embeds.add_fields_from_args(embed, args.get('f'))

        if args.last('image') is not None:
            embed.set_thumbnail(url=args.last('image'))
        await ctx.send(embed=embed)
        try:
            await ctx.message.delete()
        except:
            pass

    @commands.group(invoke_without_command=True)
    async def desc(self, ctx):
        """Prints or edits a description of your currently active character."""
        char = await Character.from_ctx(ctx)

        desc = char.character['stats'].get('description', 'No description available.')
        if not desc:
            desc = 'No description available.'
        if len(desc) > 2048:
            desc = desc[:2044] + '...'
        elif len(desc) < 2:
            desc = 'No description available.'

        embed = EmbedWithCharacter(char, name=False)
        embed.title = char.get_name()
        embed.description = desc

        await ctx.send(embed=embed)
        try:
            await ctx.message.delete()
        except:
            pass

    @desc.command(name='update', aliases=['edit'])
    async def edit_desc(self, ctx, *, desc):
        """Updates the character description."""
        char = await Character.from_ctx(ctx)

        overrides = char.character.get('overrides', {})
        overrides['desc'] = desc
        char.character['stats']['description'] = desc

        char.character['overrides'] = overrides
        await char.commit(ctx)
        await ctx.send("Description updated!")

    @desc.command(name='remove', aliases=['delete'])
    async def remove_desc(self, ctx):
        """Removes the character description, returning to the default."""
        char = await Character.from_ctx(ctx)

        overrides = char.character.get('overrides', {})
        if not 'desc' in overrides:
            return await ctx.send("There is no custom description set.")
        else:
            del overrides['desc']

        char.character['overrides'] = overrides
        await char.commit(ctx)
        await ctx.send(f"Description override removed! Use `{ctx.prefix}update` to return to the old description.")

    @commands.group(invoke_without_command=True)
    async def portrait(self, ctx):
        """Shows or edits the image of your currently active character."""
        char = await Character.from_ctx(ctx)

        image = char.get_image()
        if not image:
            return await ctx.send("No image available.")
        embed = discord.Embed()
        embed.title = char.get_name()
        embed.colour = char.get_color()
        embed.set_image(url=image)

        await ctx.send(embed=embed)
        try:
            await ctx.message.delete()
        except:
            pass

    @portrait.command(name='update', aliases=['edit'])
    async def edit_portrait(self, ctx, *, url):
        """Updates the character portrait."""
        char = await Character.from_ctx(ctx)

        overrides = char.character.get('overrides', {})
        overrides['image'] = url
        char.character['stats']['image'] = url

        char.character['overrides'] = overrides

        await char.commit(ctx)
        await ctx.send("Portrait updated!")

    @portrait.command(name='remove', aliases=['delete'])
    async def remove_portrait(self, ctx):
        """Removes the character portrait, returning to the default."""
        char = await Character.from_ctx(ctx)

        overrides = char.character.get('overrides', {})
        if not 'image' in overrides:
            return await ctx.send("There is no custom portrait set.")
        else:
            del overrides['image']

        char.character['overrides'] = overrides

        await char.commit(ctx)
        await ctx.send(f"Portrait override removed! Use `{ctx.prefix}update` to return to the old portrait.")

    @commands.command(hidden=True)  # hidden, as just called by token command
    async def playertoken(self, ctx):
        """Generates and sends a token for use on VTTs."""

        char = await Character.from_ctx(ctx)
        img_url = char.get_image()
        color_override = char.get_setting('color')
        if not img_url:
            return await ctx.send("This character has no image.")

        try:
            processed = await generate_token(img_url, color_override)
        except Exception as e:
            return await ctx.send(f"Error generating token: {e}")

        file = discord.File(processed, filename="image.png")
        await ctx.send("I generated this token for you! If it seems  wrong, you can make your own at "
                       "<http://rolladvantage.com/tokenstamp/>!", file=file)

    @commands.command()
    async def sheet(self, ctx):
        """Prints the embed sheet of your currently active character."""
        char = await Character.from_ctx(ctx)

        await ctx.send(embed=char.get_sheet_embed())
        try:
            await ctx.message.delete()
        except:
            pass

    @commands.group(aliases=['char'], invoke_without_command=True)
    async def character(self, ctx, *, name: str = None):
        """Switches the active character.
        Breaks for characters created before Jan. 20, 2017."""
        user_characters = await self.bot.mdb.characters.find({"owner": str(ctx.author.id)}).to_list(None)
        active_character = next((c for c in user_characters if c['active']), None)
        if not user_characters:
            return await ctx.send('You have no characters.')

        if name is None:
            if active_character is None:
                return await ctx.send('You have no character active.')
            return await ctx.send(
                'Currently active: {}'.format(active_character.get('stats', {}).get('name')))

        _character = await search_and_select(ctx, user_characters, name,
                                             lambda e: e.get('stats', {}).get('name', ''),
                                             selectkey=lambda
                                                 e: f"{e.get('stats', {}).get('name', '')} (`{e['upstream']}`)")

        char_name = _character.get('stats', {}).get('name', 'Unnamed')
        char_url = _character['upstream']

        name = char_name

        char = Character(_character, char_url)
        await char.set_active(ctx)

        try:
            await ctx.message.delete()
        except:
            pass

        await ctx.send("Active character changed to {}.".format(name), delete_after=20)

    @character.command(name='list')
    async def character_list(self, ctx):
        """Lists your characters."""
        user_characters = await self.bot.mdb.characters.find({"owner": str(ctx.author.id)}).to_list(None)
        if not user_characters:
            return await ctx.send('You have no characters.')

        await ctx.send('Your characters:\n{}'.format(
            ', '.join(c.get('stats', {}).get('name', '') for c in user_characters)))

    @character.command(name='delete')
    async def character_delete(self, ctx, *, name):
        """Deletes a character."""
        user_characters = await self.bot.mdb.characters.find({"owner": str(ctx.author.id)}).to_list(None)
        if not user_characters:
            return await ctx.send('You have no characters.')

        _character = await search_and_select(ctx, user_characters, name,
                                             lambda e: e.get('stats', {}).get('name', ''),
                                             selectkey=lambda e: f"{e['stats'].get('name', '')} (`{e['upstream']}`)")

        name = _character.get('stats', {}).get('name', 'Unnamed')
        char_url = _character['upstream']

        await ctx.send('Are you sure you want to delete {}? (Reply with yes/no)'.format(name))
        try:
            reply = await self.bot.wait_for('message', timeout=30, check=auth_and_chan(ctx))
        except asyncio.TimeoutError:
            reply = None
        reply = get_positivity(reply.content) if reply is not None else None
        if reply is None:
            return await ctx.send('Timed out waiting for a response or invalid response.')
        elif reply:
            # _character = Character(_character, char_url)
            # if _character.get_combat_id() is not None:
            #     combat = await Combat.from_id(_character.get_combat_id(), ctx)
            #     me = next((c for c in combat.get_combatants() if getattr(c, 'character_id', None) == char_url),
            #               None)
            #     if me:
            #         combat.remove_combatant(me, True)
            #         await combat.commit()

            await self.bot.mdb.characters.delete_one({"owner": str(ctx.author.id), "upstream": char_url})
            return await ctx.send('{} has been deleted.'.format(name))
        else:
            return await ctx.send("OK, cancelling.")

    @commands.command()
    @commands.cooldown(1, 15, BucketType.user)
    async def update(self, ctx, *, args=''):
        """Updates the current character sheet, preserving all settings.
        Valid Arguments: `-v` - Shows character sheet after update is complete.
        `-cc` - Updates custom counters from Dicecloud."""
        char = await Character.from_ctx(ctx)
        url = char.id
        old_character = char.character

        prefixes = 'dicecloud-', 'pdf-', 'google-', 'beyond-'
        _id = copy.copy(url)
        for p in prefixes:
            if url.startswith(p):
                _id = url[len(p):]
                break
        sheet_type = old_character.get('type', 'dicecloud')
        if sheet_type == 'dicecloud':
            parser = DicecloudParser(_id)
            loading = await ctx.send('Updating character data from Dicecloud...')
        elif sheet_type == 'google':
            try:
                parser = GoogleSheet(_id, self.gsheet_client)
            except AssertionError:
                await self.init_gsheet_client()  # attempt reconnection
                return await ctx.send("I am still connecting to Google. Try again in 15-30 seconds.")
            loading = await ctx.send('Updating character data from Google...')
        elif sheet_type == 'beyond':
            loading = await ctx.send('Updating character data from Beyond...')
            parser = BeyondSheetParser(_id)
        else:
            return await ctx.send("Error: Unknown sheet type.")
        try:
            await parser.get_character()
        except (timeout, aiohttp.ClientResponseError) as e:
            log.warning(
                f"Response error importing char:\n{''.join(traceback.format_exception(type(e), e, e.__traceback__))}")
            return await loading.edit(content=
                                      "I'm having some issues connecting to Dicecloud or Google right now. "
                                      "Please try again in a few minutes.")
        except HttpError:
            return await loading.edit(content=
                                      "Google returned an error trying to access your sheet. "
                                      "Please ensure your sheet is shared and try again in a few minutes.")
        except Exception as e:
            log.warning(
                f"Failed to import character\n{''.join(traceback.format_exception(type(e), e, e.__traceback__))}")
            return await loading.edit(content='Error: Invalid character sheet.\n' + str(e))

        try:
            if sheet_type == 'dicecloud':
                sheet = parser.get_sheet()
            elif sheet_type == 'pdf':
                sheet = parser.get_sheet()
            elif sheet_type == 'google':
                sheet = await parser.get_sheet()
            elif sheet_type == 'beyond':
                sheet = parser.get_sheet()
            else:
                return await ctx.send("Error: Unknown sheet type.")
            await loading.edit(content=
                               'Updated and saved data for {}!'.format(sheet['sheet']['stats']['name']))
        except TypeError as e:
            del parser
            log.info(f"Exception in parser.get_sheet: {e}")
            log.debug('\n'.join(traceback.format_exception(type(e), e, e.__traceback__)))
            return await loading.edit(content=
                                      'Invalid character sheet. '
                                      'If you are using a dicecloud sheet, '
                                      'make sure you have shared the sheet so that anyone with the '
                                      'link can view.')
        except Exception as e:
            del parser
            return await loading.edit(content='Error: Invalid character sheet.\n' + str(e))

        sheet = sheet['sheet']
        sheet['settings'] = old_character.get('settings', {})
        sheet['overrides'] = old_character.get('overrides', {})
        sheet['cvars'] = old_character.get('cvars', {})
        sheet['consumables'] = old_character.get('consumables', {})

        overrides = old_character.get('overrides', {})
        sheet['stats']['description'] = overrides.get('desc') or sheet.get('stats', {}).get("description",
                                                                                            "No description available.")
        sheet['stats']['image'] = overrides.get('image') or sheet.get('stats', {}).get('image', '')
        override_spells = []
        for s in overrides.get('spells', []):
            if isinstance(s, str):
                override_spells.append({'name': s, 'strict': True})
            else:
                override_spells.append(s)
        sheet['spellbook']['spells'].extend(override_spells)

        c = Character(sheet, url).initialize_consumables()

        if '-cc' in args and sheet_type == 'dicecloud':
            counters = parser.get_custom_counters()
            for counter in counters:
                displayType = 'bubble' if c.evaluate_cvar(counter['max']) < 6 else None
                try:
                    c.create_consumable(counter['name'], maxValue=str(counter['max']),
                                        minValue=str(counter['min']),
                                        reset=counter['reset'], displayType=displayType, live=counter['live'])
                except InvalidArgument:
                    pass

        # if c.get_combat_id() and not self.bot.rdb.exists(c.get_combat_id()):
        #     c.leave_combat()
        # reimplement this later

        await c.commit(ctx)
        await c.set_active(ctx)
        del parser, old_character  # pls don't freak out avrae
        if '-v' in args:
            await ctx.send(embed=c.get_sheet_embed())

    @commands.command()
    async def transferchar(self, ctx, user: discord.Member):
        """Gives a copy of the active character to another user."""
        character = await Character.from_ctx(ctx)
        overwrite = ''

        conflict = await self.bot.mdb.characters.find_one({"owner": str(user.id), "upstream": character.id})
        if conflict:
            overwrite = "**WARNING**: This will overwrite an existing character."

        await ctx.send(f"{user.mention}, accept a copy of {character.get_name()}? (Type yes/no)\n{overwrite}")
        try:
            m = await self.bot.wait_for('message', timeout=300,
                                        check=lambda msg: msg.author == user
                                                          and msg.channel == ctx.channel
                                                          and get_positivity(msg.content) is not None)
        except asyncio.TimeoutError:
            m = None

        if m is None or not get_positivity(m.content): return await ctx.send("Transfer not confirmed, aborting.")

        await character.manual_commit(self.bot, str(user.id))
        await ctx.send(f"Copied {character.get_name()} to {user.display_name}'s storage.")

    @commands.command()
    async def csettings(self, ctx, *, args):
        """Updates personalization settings for the currently active character.
        Valid Arguments:
        `color <hex color>` - Colors all embeds this color.
        `criton <number>` - Makes attacks crit on something other than a 20.
        `reroll <number>` - Defines a number that a check will automatically reroll on, for cases such as Halfling Luck.
        `srslots true/false` - Enables/disables whether spell slots reset on a Short Rest.
        `embedimage true/false` - Enables/disables whether a character's image is automatically embedded.
        `crittype 2x/default` - Sets whether crits double damage or dice.
        `critdice <number>` - Adds additional dice for to critical attacks."""
        char = await Character.from_ctx(ctx)
        character = char.character

        args = shlex.split(args)

        if character.get('settings') is None:
            character['settings'] = {}

        out = 'Operations complete!\n'
        index = 0
        for arg in args:
            if arg == 'color':
                color = list_get(index + 1, None, args)
                if color is None:
                    current_color = hex(char.get_color()) if char.get_setting('color') else "random"
                    out += f'\u2139 Your character\'s current color is {current_color}. ' \
                           f'Use "{ctx.prefix}csettings color reset" to reset it to random.\n'
                elif color.lower() == 'reset':
                    character['settings']['color'] = None
                    out += "\u2705 Color reset to random.\n"
                else:
                    try:
                        color = int(color, base=16)
                    except (ValueError, TypeError):
                        out += f'\u274c Unknown color. Use "{ctx.prefix}csettings color reset" to reset it to random.\n'
                    else:
                        if not 0 <= color <= 0xffffff:
                            out += '\u274c Invalid color.\n'
                        else:
                            character['settings']['color'] = color
                            out += "\u2705 Color set to {}.\n".format(hex(color))
            if arg == 'criton':
                criton = list_get(index + 1, None, args)
                if criton is None:
                    current = str(char.get_setting('criton')) + '-20' if char.get_setting('criton') else "20"
                    out += f'\u2139 Your character\'s current crit range is {current}. ' \
                           f'Use "{ctx.prefix}csettings criton reset" to reset it to 20.\n'
                elif criton.lower() == 'reset':
                    character['settings']['criton'] = None
                    out += "\u2705 Crit range reset to 20.\n"
                else:
                    try:
                        criton = int(criton)
                    except (ValueError, TypeError):
                        out += f'\u274c Invalid number. Use "{ctx.prefix}csettings criton reset" to reset it to 20.\n'
                    else:
                        if not 0 < criton <= 20:
                            out += '\u274c Crit range must be between 1 and 20.\n'
                        elif criton == 20:
                            character['settings']['criton'] = None
                            out += "\u2705 Crit range reset to 20.\n"
                        else:
                            character['settings']['criton'] = criton
                            out += "\u2705 Crit range set to {}-20.\n".format(criton)
            if arg == 'reroll':
                reroll = list_get(index + 1, None, args)
                if reroll is None:
                    current = str(character['settings'].get('reroll')) if character['settings'].get(
                        'reroll') is not '0' else "0"
                    out += f'\u2139 Your character\'s current reroll is {current}. ' \
                           f'Use "{ctx.prefix}csettings reroll reset" to reset it.\n'
                elif reroll.lower() == 'reset':
                    character['settings']['reroll'] = '0'
                    out += "\u2705 Reroll reset.\n"
                else:
                    try:
                        reroll = int(reroll)
                    except (ValueError, TypeError):
                        out += f'\u274c Invalid number. Use "{ctx.prefix}csettings reroll reset" to reset it.\n'
                    else:
                        if not 1 <= reroll <= 20:
                            out += '\u274c Reroll must be between 1 and 20.\n'
                        else:
                            character['settings']['reroll'] = reroll
                            out += "\u2705 Reroll set to {}.\n".format(reroll)
            if arg == 'critdice':
                critdice = list_get(index + 1, None, args)
                if 'hocrit' in character['settings']:
                    character['settings']['critdice'] += int(character['settings']['hocrit'])
                    del character['settings']['hocrit']
                if critdice is None:
                    current = str(character['settings'].get('critdice')) if character['settings'].get(
                        'critdice') is not '0' else "0"
                    out += f'\u2139 Extra crit dice are currently set to {current}. ' \
                           f'Use "{ctx.prefix}csettings critdice reset" to reset it.\n'
                elif critdice.lower() == 'reset':
                    character['settings']['critdice'] = 0
                    out += "\u2705 Extra crit dice reset.\n"
                else:
                    try:
                        critdice = int(critdice)
                    except (ValueError, TypeError):
                        out += f'\u274c Invalid number. Use "{ctx.prefix}csettings critdice reset" to reset it.\n'
                    else:
                        if not 0 <= critdice <= 20:
                            out += f'\u274c Extra crit dice must be between 1 and 20. Use "{ctx.prefix}csettings critdice reset" to reset it.\n'
                        else:
                            character['settings']['critdice'] = critdice
                            out += "\u2705 Extra crit dice set to {}.\n".format(critdice)
            if arg == 'srslots':
                srslots = list_get(index + 1, None, args)
                if srslots is None:
                    out += '\u2139 Short rest slots are currently {}.\n' \
                        .format("enabled" if character['settings'].get('srslots') else "disabled")
                else:
                    try:
                        srslots = get_positivity(srslots)
                    except AttributeError:
                        out += f'\u274c Invalid input. Use "{ctx.prefix}csettings srslots false" to reset it.\n'
                    else:
                        character['settings']['srslots'] = srslots
                        out += "\u2705 Short Rest slots {}.\n".format(
                            "enabled" if character['settings'].get('srslots') else "disabled")
            if arg == 'embedimage':
                embedimage = list_get(index + 1, None, args)
                if embedimage is None:
                    out += '\u2139 Embed Image is currently {}.\n' \
                        .format("enabled" if character['settings'].get('embedimage') else "disabled")
                else:
                    try:
                        embedimage = get_positivity(embedimage)
                    except AttributeError:
                        out += f'\u274c Invalid input. Use "{ctx.prefix}csettings embedimage true" to reset it.\n'
                    else:
                        character['settings']['embedimage'] = embedimage
                        out += "\u2705 Embed Image {}.\n".format(
                            "enabled" if character['settings'].get('embedimage') else "disabled")
            if arg == 'crittype':
                crittype = list_get(index + 1, None, args)
                if crittype is None:
                    out += '\u2139 Crit type is currently {}.\n' \
                        .format(character['settings'].get('crittype', 'default'))
                else:
                    try:
                        assert crittype in ('2x', 'default')
                    except AssertionError:
                        out += f'\u274c Invalid input. Use "{ctx.prefix}csettings crittype default" to reset it.\n'
                    else:
                        character['settings']['crittype'] = crittype
                        out += "\u2705 Crit type set to {}.\n".format(character['settings'].get('crittype'))
            index += 1

        await char.commit(ctx)
        await ctx.send(out)

    @commands.group(invoke_without_command=True)
    async def cvar(self, ctx, name=None, *, value=None):
        """Commands to manage character variables for use in snippets and aliases.
        Character variables can be called in the `-phrase` tag by surrounding the variable name with `{}` (calculates) or `<>` (prints).
        Arguments surrounded with `{{}}` will be evaluated as a custom script.
        See http://avrae.io/cheatsheets/aliasing for more help.
        Dicecloud `statMod` and `stat` variables are also available."""
        if name is None:
            return await ctx.invoke(self.bot.get_command("cvar list"))

        character = await Character.from_ctx(ctx)

        if value is None:  # display value
            cvar = character.get_cvar(name)
            if cvar is None: cvar = 'Not defined.'
            return await ctx.send('**' + name + '**:\n' + cvar)

        try:
            assert not name in character.get_stat_vars()
            assert not any(c in name for c in '-/()[]\\.^$*+?|{}')
        except AssertionError:
            return await ctx.send("Could not create cvar: already builtin, or contains invalid character!")

        character.set_cvar(name, value)
        await character.commit(ctx)
        await ctx.send('Character variable `{}` set to: `{}`'.format(name, value))

    @cvar.command(name='remove', aliases=['delete'])
    async def remove_cvar(self, ctx, name):
        """Deletes a cvar from the currently active character."""
        char = await Character.from_ctx(ctx)

        try:
            del char.character.get('cvars', {})[name]
        except KeyError:
            return await ctx.send('Character variable not found.')

        await char.commit(ctx)
        await ctx.send('Character variable {} removed.'.format(name))

    @cvar.command(name='deleteall', aliases=['removeall'])
    async def cvar_deleteall(self, ctx):
        """Deletes ALL character variables for the active character."""
        char = await Character.from_ctx(ctx)

        await ctx.send(f"This will delete **ALL** of your character variables for {char.get_name()}. "
                       "Are you *absolutely sure* you want to continue?\n"
                       "Type `Yes, I am sure` to confirm.")
        try:
            reply = await self.bot.wait_for('message', timeout=30, check=auth_and_chan(ctx))
        except asyncio.TimeoutError:
            reply = None
        if (not reply) or (not reply.content == "Yes, I am sure"):
            return await ctx.send("Unconfirmed. Aborting.")

        char.character['cvars'] = {}

        await char.commit(ctx)
        return await ctx.send(f"OK. I have deleted all of {char.get_name()}'s cvars.")

    @cvar.command(name='list')
    async def list_cvar(self, ctx):
        """Lists all cvars for the currently active character."""
        character = await Character.from_ctx(ctx)
        cvars = character.get_cvars()

        await ctx.send('{}\'s character variables:\n{}'.format(character.get_name(),
                                                               ', '.join(sorted([name for name in cvars.keys()]))))

    async def _confirm_overwrite(self, ctx, _id):
        """Prompts the user if command would overwrite another character.
        Returns True to overwrite, False or None otherwise."""
        conflict = await self.bot.mdb.characters.find_one({"owner": str(ctx.author.id), "upstream": _id})
        if conflict:
            await ctx.channel.send(
                "Warning: This will overwrite a character with the same ID. Do you wish to continue (reply yes/no)?\n"
                f"If you only wanted to update your character, run `{ctx.prefix}update` instead.")
            try:
                reply = await self.bot.wait_for('message', timeout=30, check=auth_and_chan(ctx))
            except asyncio.TimeoutError:
                reply = None
            replyBool = get_positivity(reply.content) if reply is not None else None
            return replyBool
        return True

    @commands.command()
    async def dicecloud(self, ctx, url: str, *, args=""):
        """Loads a character sheet from [Dicecloud](https://dicecloud.com/), resetting all settings.
        Share your character with `avrae` on Dicecloud (edit perms) for live updates.
        __Valid Arguments__
        `-cc` - Will automatically create custom counters for class resources and features."""
        if 'dicecloud.com' in url:
            url = url.split('/character/')[-1].split('/')[0]

        override = await self._confirm_overwrite(ctx, f"dicecloud-{url}")
        if not override: return await ctx.send("Character overwrite unconfirmed. Aborting.")

        loading = await ctx.send('Loading character data from Dicecloud...')
        parser = DicecloudParser(url)
        try:
            await parser.get_character()
        except Exception as eep:
            return await loading.edit(content=f"Dicecloud returned an error: {eep}")

        try:
            sheet = parser.get_sheet()
        except Exception as e:
            traceback.print_exception(type(e), e, e.__traceback__, file=sys.stderr)
            return await loading.edit(content=
                                      'Error: Invalid character sheet. Capitalization matters!\n' + str(e))

        c = Character(sheet['sheet'], f"dicecloud-{url}").initialize_consumables()
        await loading.edit(content=f'Loaded and saved data for {c.get_name()}!')

        if '-cc' in args:
            for counter in parser.get_custom_counters():
                displayType = 'bubble' if c.evaluate_cvar(counter['max']) < 6 else None
                try:
                    c.create_consumable(counter['name'], maxValue=str(counter['max']), minValue=str(counter['min']),
                                        reset=counter['reset'], displayType=displayType, live=counter['live'])
                except InvalidArgument:
                    pass

        del parser  # uh. maybe some weird instance things going on here.
        await c.commit(ctx)
        await c.set_active(ctx)
        try:
            await ctx.send(embed=c.get_sheet_embed())
        except:
            await ctx.send(
                "...something went wrong generating your character sheet. Don't worry, your character has been saved. "
                "This is usually due to an invalid image.")

    @commands.command()
    async def gsheet(self, ctx, url: str):
        """Loads a character sheet from [GSheet v2.0](http://gsheet2.avrae.io) (auto) or [GSheet v1.3](http://gsheet.avrae.io) (manual), resetting all settings.
        The sheet must be shared with Avrae for this to work.
        Avrae's google account is `avrae-320@avrae-bot.iam.gserviceaccount.com`."""

        loading = await ctx.send('Loading character data from Google... (This usually takes ~30 sec)')
        try:
            url = extract_gsheet_id_from_url(url)
        except NoValidUrlKeyFound:
            return await loading.edit(content="This is not a Google Sheets link.")

        override = await self._confirm_overwrite(ctx, f"google-{url}")
        if not override: return await ctx.send("Character overwrite unconfirmed. Aborting.")

        try:
            parser = GoogleSheet(url, self.gsheet_client)
        except AssertionError:
            await self.init_gsheet_client()  # hmm.
            return await loading.edit(content="I am still connecting to Google. Try again in 15-30 seconds.")

        try:
            await parser.get_character()
        except (KeyError, SpreadsheetNotFound):
            return await loading.edit(content=
                                      "Invalid character sheet. Make sure you've shared it with me at "
                                      "`avrae-320@avrae-bot.iam.gserviceaccount.com`!")
        except HttpError:
            return await loading.edit(content=
                                      "Error: Google returned an error. Please ensure your sheet is shared with "
                                      "`avrae-320@avrae-bot.iam.gserviceaccount.com` and try again in a few minutes.")
        except Exception as e:
            return await loading.edit(content='Error: Could not load character sheet.\n' + str(e))

        try:
            sheet = await parser.get_sheet()
        except Exception as e:
            traceback.print_exception(type(e), e, e.__traceback__, file=sys.stderr)
            return await loading.edit(content='Error: Invalid character sheet.\n' + str(e))

        try:
            await loading.edit(content=
                               'Loaded and saved data for {}!'.format(sheet['sheet']['stats']['name']))
        except TypeError as e:
            traceback.print_exception(type(e), e, e.__traceback__, file=sys.stderr)
            return await loading.edit(content=
                                      'Invalid character sheet. Make sure you have shared the sheet so that anyone with the link can view.')

        char = Character(sheet['sheet'], f"google-{url}").initialize_consumables()
        await char.commit(ctx)
        await char.set_active(ctx)

        try:
            await ctx.send(embed=char.get_sheet_embed())
        except:
            await ctx.send(
                "...something went wrong generating your character sheet. Don't worry, your character has been saved. "
                "This is usually due to an invalid image.")

    @commands.command()
    async def beyond(self, ctx, url: str):
        """Loads a character sheet from D&D Beyond, resetting all settings."""

        loading = await ctx.send('Loading character data from Beyond...')
        url = re.search(r"/characters/(\d+)", url)
        if url is None:
            return await loading.edit(content="This is not a D&D Beyond link.")
        url = url.group(1)

        override = await self._confirm_overwrite(ctx, f"beyond-{url}")
        if not override: return await ctx.send("Character overwrite unconfirmed. Aborting.")

        parser = BeyondSheetParser(url)

        try:
            character = await parser.get_character()
        except Exception as e:
            return await loading.edit(content='Error: Could not load character sheet.\n' + str(e))

        try:
            sheet = parser.get_sheet()
        except Exception as e:
            traceback.print_exception(type(e), e, e.__traceback__, file=sys.stderr)
            return await loading.edit(content='Error: Invalid character sheet.\n' + str(e))

        await loading.edit(content='Loaded and saved data for {}!'.format(character['name']))

        char = Character(sheet['sheet'], f"beyond-{url}").initialize_consumables()
        await char.commit(ctx)
        await char.set_active(ctx)

        try:
            await ctx.send(embed=char.get_sheet_embed())
        except:
            await ctx.send(
                "...something went wrong generating your character sheet. Don't worry, your character has been saved. "
                "This is usually due to an invalid image.")


def setup(bot):
    bot.add_cog(SheetManager(bot))
