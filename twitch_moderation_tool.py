import json
import os
import random
import re
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TypedDict, cast

from dotenv import load_dotenv
from twitchio.ext import commands as twitch_commands  # pyright: ignore[reportMissingTypeStubs]

commands: Any = twitch_commands

load_dotenv()


# --- Configurable keyword sets -------------------------------------------------
PROFANITY_TERMS = {
    "fuck",
    "fucking",
    "shit",
    "bitch",
    "asshole",
    "motherfucker",
    "damn",
    "cunt",
    "bastard",
}

BRAINROT_TERMS = {
    "skibidi",
    "sigma",
    "gyatt",
    "rizz",
    "fanum",
    "ohio",
    "mewing",
    "mogged",
    "looksmaxxing",
    "goofy ahh",
    "edging",
    "grimace shake",
}

CODE_INSULT_PATTERNS = [
    r"\byour code (is|looks|sounds)?\s*(so )?(bad|trash|garbage|awful|terrible|horrible)\b",
    r"\b(code|coding)\s*(is|looks)?\s*(bad|trash|garbage|awful)\b",
    r"\bworst code\b",
    r"\bthis code sucks\b",
    r"\bspaghetti code\b",
    r"\byou('re| are) a bad (coder|programmer|developer)\b",
]


class ShopItem(TypedDict):
    price: int
    desc: str


class SongRequestEntry(TypedDict):
    user: str
    song: str


class BotData(TypedDict):
    balances: dict[str, int]
    inventory: dict[str, dict[str, int]]
    quotes: list[str]
    sr_queue: list[SongRequestEntry]


SHOP_ITEMS: dict[str, ShopItem] = {
    "worm": {"price": 20, "desc": "Better bait for future fishing flex."},
    "golden_rod": {"price": 180, "desc": "A shiny rod to impress chat."},
    "lucky_coin": {"price": 120, "desc": "A pocket charm for gamblers."},
    "energy_drink": {"price": 60, "desc": "For high-octane coding sessions."},
}

SLOT_SYMBOLS = ["7", "BAR", "CHOMP", "FISH", "LEMON", "STAR"]


class MessageChannel(Protocol):
    async def send(self, message: str) -> None:
        ...


class ChatAuthor(Protocol):
    name: str | None
    is_mod: bool
    is_broadcaster: bool


class ChatMessage(Protocol):
    echo: bool
    author: ChatAuthor | None
    content: str | None
    channel: MessageChannel


@dataclass
class ModerationConfig:
    timeout_seconds: int = int(os.getenv("TWITCH_TIMEOUT_SECONDS", "120"))
    max_violations_before_timeout: int = int(os.getenv("TWITCH_MAX_VIOLATIONS", "1"))


def normalize_text(text: str) -> str:
    """Normalize text so matching is less sensitive to punctuation/leet/symbols."""
    text = text.lower()
    text = unicodedata.normalize("NFKD", text)

    # Basic leetspeak folding
    text = (
        text.replace("0", "o")
        .replace("1", "i")
        .replace("3", "e")
        .replace("4", "a")
        .replace("5", "s")
        .replace("7", "t")
        .replace("@", "a")
        .replace("$", "s")
    )

    # Remove non-alphanumeric (keep spaces)
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def has_term(text: str, terms: set[str]) -> bool:
    words = set(text.split())
    for term in terms:
        if " " in term:
            if term in text:
                return True
        elif term in words:
            return True
    return False


def is_code_insult(text: str) -> bool:
    return any(re.search(pattern, text) for pattern in CODE_INSULT_PATTERNS)


class ModBot(commands.Bot):
    def __init__(self) -> None:
        token = os.getenv("TWITCH_BOT_TOKEN")
        client_id = os.getenv("TWITCH_CLIENT_ID")
        nick = os.getenv("TWITCH_BOT_NICK")
        prefix = os.getenv("TWITCH_PREFIX", "!")
        initial_channel = os.getenv("TWITCH_CHANNEL")

        required = {
            "TWITCH_BOT_TOKEN": token,
            "TWITCH_CLIENT_ID": client_id,
            "TWITCH_BOT_NICK": nick,
            "TWITCH_CHANNEL": initial_channel,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise RuntimeError(f"Missing env vars: {', '.join(missing)}")

        safe_token = cast(str, token)
        safe_client_id = cast(str, client_id)
        safe_nick = cast(str, nick)
        safe_initial_channel = cast(str, initial_channel)

        super().__init__(
            token=safe_token,
            client_id=safe_client_id,
            nick=safe_nick,
            prefix=prefix,
            initial_channels=[safe_initial_channel],
        )

        self.cfg = ModerationConfig()
        self.user_violations: dict[str, int] = {}
        self.data_file = Path("bot_data.json")
        self.data: BotData = self._load_data()
        self.last_fish_time: dict[str, float] = {}
        self.start_balance = int(os.getenv("TWITCH_START_BALANCE", "200"))
        self.fish_cooldown_seconds = int(os.getenv("TWITCH_FISH_COOLDOWN_SECONDS", "45"))

    async def event_ready(self) -> None:
        print(f"Logged in as: {self.nick}")
        print("Moderation tool is live.")

    async def event_message(self, message: ChatMessage) -> None:
        if message.echo:
            return

        # Do not moderate broadcaster or moderators.
        author = message.author
        if author is None:
            return
        if author.is_mod or author.is_broadcaster:
            await self.handle_commands(cast(Any, message))
            return

        original: str = message.content or ""
        clean = normalize_text(original)

        violation_reasons: list[str] = []
        if has_term(clean, PROFANITY_TERMS):
            violation_reasons.append("swearing")
        if has_term(clean, BRAINROT_TERMS):
            violation_reasons.append("brainrot")
        if is_code_insult(clean):
            violation_reasons.append("code insult")

        if violation_reasons:
            author_name = author.name or "unknown"
            user = author_name.lower()
            self.user_violations[user] = self.user_violations.get(user, 0) + 1

            if self.user_violations[user] >= self.cfg.max_violations_before_timeout:
                reason = ", ".join(sorted(set(violation_reasons)))
                await message.channel.send(
                    f"/timeout {author_name} {self.cfg.timeout_seconds} {reason}"
                )
                print(
                    f"Timed out {author_name} for {self.cfg.timeout_seconds}s | reason: {reason} | message: {original!r}"
                )
            else:
                remaining = self.cfg.max_violations_before_timeout - self.user_violations[user]
                print(
                    f"Violation from {author_name}; {remaining} warning(s) left before timeout."
                )

        await self.handle_commands(cast(Any, message))

    @commands.command(name="modstats")
    async def modstats(self, ctx: commands.Context) -> None:
        author = cast(ChatAuthor | None, ctx.author)
        if not (author and (author.is_mod or author.is_broadcaster)):
            return
        offenders = len(self.user_violations)
        total = sum(self.user_violations.values())
        await ctx.send(f"Moderation stats: {total} violations across {offenders} users.")

    @commands.command(name="forgive")
    async def forgive(self, ctx: commands.Context, username: str) -> None:
        author = cast(ChatAuthor | None, ctx.author)
        if not (author and (author.is_mod or author.is_broadcaster)):
            return
        username = username.lower().lstrip("@")
        if username in self.user_violations:
            del self.user_violations[username]
            await ctx.send(f"Forgiven @{username}.")
        else:
            await ctx.send(f"No violations found for @{username}.")

    def _load_data(self) -> BotData:
        if self.data_file.exists():
            try:
                loaded = cast(dict[str, Any], json.loads(self.data_file.read_text(encoding="utf-8")))
                raw_balances = loaded.get("balances", {})
                raw_inventory = loaded.get("inventory", {})
                raw_quotes = loaded.get("quotes", [])
                raw_sr_queue = loaded.get("sr_queue", [])

                balances: dict[str, int] = {}
                if isinstance(raw_balances, dict):
                    for key, value in raw_balances.items():
                        if isinstance(key, str):
                            balances[key] = int(value)

                inventory: dict[str, dict[str, int]] = {}
                if isinstance(raw_inventory, dict):
                    for user, items in raw_inventory.items():
                        if not isinstance(user, str) or not isinstance(items, dict):
                            continue
                        clean_items: dict[str, int] = {}
                        for item_name, count in items.items():
                            if isinstance(item_name, str):
                                clean_items[item_name] = int(count)
                        inventory[user] = clean_items

                quotes: list[str] = []
                if isinstance(raw_quotes, list):
                    quotes = [str(entry) for entry in raw_quotes]

                sr_queue: list[SongRequestEntry] = []
                if isinstance(raw_sr_queue, list):
                    for entry in raw_sr_queue:
                        if isinstance(entry, dict):
                            user = str(entry.get("user", "unknown"))
                            song = str(entry.get("song", ""))
                            if song:
                                sr_queue.append({"user": user, "song": song})

                return {
                    "balances": balances,
                    "inventory": inventory,
                    "quotes": quotes,
                    "sr_queue": sr_queue,
                }
            except (json.JSONDecodeError, OSError):
                print("bot_data.json unreadable; starting with fresh data.")
        return {"balances": {}, "inventory": {}, "quotes": [], "sr_queue": []}

    def _save_data(self) -> None:
        self.data_file.write_text(json.dumps(self.data, indent=2), encoding="utf-8")

    def _user_key(self, username: str) -> str:
        return username.lower().lstrip("@")

    def _get_balance(self, username: str) -> int:
        key = self._user_key(username)
        if key not in self.data["balances"]:
            self.data["balances"][key] = self.start_balance
            self._save_data()
        return int(self.data["balances"][key])

    def _add_balance(self, username: str, amount: int) -> int:
        key = self._user_key(username)
        current = self._get_balance(key)
        new_balance = max(0, current + amount)
        self.data["balances"][key] = new_balance
        self._save_data()
        return new_balance

    def _roulette_color(self, number: int) -> str:
        if number == 0:
            return "green"
        red = {
            1,
            3,
            5,
            7,
            9,
            12,
            14,
            16,
            18,
            19,
            21,
            23,
            25,
            27,
            30,
            32,
            34,
            36,
        }
        return "red" if number in red else "black"

    @commands.command(name="bal")
    async def balance(self, ctx: commands.Context, username: str = "") -> None:
        author = cast(ChatAuthor | None, ctx.author)
        target = username or (author.name if author and author.name else "unknown")
        bal = self._get_balance(target)
        await ctx.send(f"@{self._user_key(target)} has {bal} coins.")

    @commands.command(name="quote")
    async def quote(self, ctx: commands.Context, *, text: str = "") -> None:
        quote_text = text.strip()
        if not quote_text:
            if not self.data["quotes"]:
                await ctx.send("No quotes yet. Use !quote add <text>.")
                return
            index = random.randint(0, len(self.data["quotes"]) - 1)
            picked: str = self.data["quotes"][index]
            await ctx.send(f"Quote #{index + 1}: {picked}")
            return

        if quote_text.lower().startswith("add "):
            new_quote = quote_text[4:].strip()
            if not new_quote:
                await ctx.send("Usage: !quote add <text>")
                return
            self.data["quotes"].append(new_quote)
            self._save_data()
            await ctx.send(f"Saved quote #{len(self.data['quotes'])}.")
            return

        if quote_text.isdigit():
            index = int(quote_text) - 1
            if 0 <= index < len(self.data["quotes"]):
                await ctx.send(f"Quote #{index + 1}: {self.data['quotes'][index]}")
            else:
                await ctx.send("That quote number does not exist.")
            return

        await ctx.send("Use: !quote, !quote <number>, or !quote add <text>")

    @commands.command(name="sr")
    async def song_request(self, ctx: commands.Context, *, text: str = "") -> None:
        request = text.strip()
        if not request:
            await ctx.send("Usage: !sr <song/link>, !sr list, !sr next")
            return

        lowered = request.lower()
        if lowered == "list":
            queue: list[SongRequestEntry] = self.data["sr_queue"]
            if not queue:
                await ctx.send("Song queue is empty.")
                return
            preview = " | ".join(
                f"{i + 1}. {item['song']} (@{item['user']})" for i, item in enumerate(queue[:3])
            )
            await ctx.send(f"Queue ({len(queue)}): {preview}")
            return

        if lowered == "next":
            author = cast(ChatAuthor | None, ctx.author)
            if not (author and (author.is_mod or author.is_broadcaster)):
                await ctx.send("Only mods/broadcaster can pop the next song.")
                return
            queue: list[SongRequestEntry] = self.data["sr_queue"]
            if not queue:
                await ctx.send("Song queue is already empty.")
                return
            next_song = queue.pop(0)
            self._save_data()
            await ctx.send(f"Now up: {next_song['song']} (requested by @{next_song['user']})")
            return

        author = cast(ChatAuthor | None, ctx.author)
        author_name = author.name if author and author.name else "unknown"
        self.data["sr_queue"].append({"user": author_name, "song": request})
        self._save_data()
        await ctx.send(f"Added to queue at #{len(self.data['sr_queue'])}: {request}")

    @commands.command(name="fish")
    async def fish(self, ctx: commands.Context) -> None:
        author = cast(ChatAuthor | None, ctx.author)
        user = author.name if author and author.name else "unknown"
        key = self._user_key(user)
        now = time.time()
        last = self.last_fish_time.get(key, 0.0)
        wait_seconds = int(self.fish_cooldown_seconds - (now - last))
        if wait_seconds > 0:
            await ctx.send(f"@{key} your rod is cooling down. Try again in {wait_seconds}s.")
            return

        self.last_fish_time[key] = now
        catches = [
            ("tin can", 4),
            ("boot", 8),
            ("trout", 25),
            ("salmon", 45),
            ("golden koi", 120),
            ("legendary shark", 260),
        ]
        caught, reward = random.choices(catches, weights=[30, 24, 20, 14, 9, 3], k=1)[0]
        balance = self._add_balance(key, reward)
        await ctx.send(f"@{key} caught a {caught} and earned {reward} coins. Balance: {balance}")

    @commands.command(name="slots")
    async def slots(self, ctx: commands.Context, bet: str = "") -> None:
        if not bet.isdigit():
            await ctx.send("Usage: !slots <bet>")
            return
        wager = int(bet)
        if wager <= 0:
            await ctx.send("Bet must be positive.")
            return

        author = cast(ChatAuthor | None, ctx.author)
        author_name = author.name if author and author.name else "unknown"
        user = self._user_key(author_name)
        bal = self._get_balance(user)
        if wager > bal:
            await ctx.send(f"@{user} you only have {bal} coins.")
            return

        self._add_balance(user, -wager)
        reel = [random.choice(SLOT_SYMBOLS) for _ in range(3)]
        payout_multiplier = 0
        if reel[0] == reel[1] == reel[2]:
            if reel[0] == "7":
                payout_multiplier = 8
            elif reel[0] == "CHOMP":
                payout_multiplier = 6
            else:
                payout_multiplier = 4
        elif len(set(reel)) == 2:
            payout_multiplier = 1

        winnings = wager * payout_multiplier
        if winnings:
            new_bal = self._add_balance(user, winnings + wager)
            await ctx.send(f"@{user} [{' | '.join(reel)}] WIN +{winnings} coins. Balance: {new_bal}")
            return

        new_bal = self._get_balance(user)
        await ctx.send(f"@{user} [{' | '.join(reel)}] no win. Balance: {new_bal}")

    @commands.command(name="roulette")
    async def roulette(self, ctx: commands.Context, bet: str = "", choice: str = "") -> None:
        if not bet.isdigit() or not choice:
            await ctx.send("Usage: !roulette <bet> <red|black|even|odd|0-36>")
            return
        wager = int(bet)
        if wager <= 0:
            await ctx.send("Bet must be positive.")
            return

        choice = choice.lower()
        valid_simple = {"red", "black", "even", "odd"}
        is_number_bet = choice.isdigit() and 0 <= int(choice) <= 36
        if choice not in valid_simple and not is_number_bet:
            await ctx.send("Choice must be red, black, even, odd, or a number 0-36.")
            return

        author = cast(ChatAuthor | None, ctx.author)
        author_name = author.name if author and author.name else "unknown"
        user = self._user_key(author_name)
        bal = self._get_balance(user)
        if wager > bal:
            await ctx.send(f"@{user} you only have {bal} coins.")
            return

        self._add_balance(user, -wager)
        spin = random.randint(0, 36)
        spin_color = self._roulette_color(spin)

        won = False
        payout_total = 0
        if is_number_bet:
            if int(choice) == spin:
                won = True
                payout_total = wager * 36
        elif choice in {"red", "black"}:
            if choice == spin_color:
                won = True
                payout_total = wager * 2
        elif choice == "even":
            if spin != 0 and spin % 2 == 0:
                won = True
                payout_total = wager * 2
        elif choice == "odd":
            if spin % 2 == 1:
                won = True
                payout_total = wager * 2

        if won:
            new_bal = self._add_balance(user, payout_total)
            await ctx.send(f"@{user} roulette hit {spin} {spin_color}. You won! Balance: {new_bal}")
        else:
            new_bal = self._get_balance(user)
            await ctx.send(f"@{user} roulette hit {spin} {spin_color}. You lost. Balance: {new_bal}")

    @commands.command(name="shop")
    async def shop(self, ctx: commands.Context, action: str = "", item: str = "") -> None:
        author = cast(ChatAuthor | None, ctx.author)
        author_name = author.name if author and author.name else "unknown"
        user = self._user_key(author_name)
        if not action:
            listing = " | ".join(
                f"{name}: {meta['price']} ({meta['desc']})" for name, meta in SHOP_ITEMS.items()
            )
            await ctx.send(f"Shop: {listing} | buy with !shop buy <item>")
            return

        action = action.lower()
        if action == "inv":
            inv: dict[str, int] = self.data["inventory"].get(user, {})
            if not inv:
                await ctx.send(f"@{user} inventory is empty.")
                return
            inv_text = " | ".join(f"{name} x{count}" for name, count in inv.items())
            await ctx.send(f"@{user} inventory: {inv_text}")
            return

        if action != "buy":
            await ctx.send("Usage: !shop, !shop buy <item>, !shop inv")
            return
        if not item:
            await ctx.send("Usage: !shop buy <item>")
            return

        item = item.lower()
        if item not in SHOP_ITEMS:
            await ctx.send("That item is not in the shop.")
            return

        price = int(SHOP_ITEMS[item]["price"])
        bal = self._get_balance(user)
        if bal < price:
            await ctx.send(f"@{user} not enough coins. Cost: {price}, Balance: {bal}")
            return

        self._add_balance(user, -price)
        inventory = self.data["inventory"].setdefault(user, {})
        inventory[item] = int(inventory.get(item, 0)) + 1
        self._save_data()
        await ctx.send(f"@{user} bought {item} for {price} coins.")


if __name__ == "__main__":
    bot = ModBot()
    bot.run()
