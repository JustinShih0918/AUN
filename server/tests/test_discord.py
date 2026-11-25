"""Discord bot 的邏輯測試：用假的 Discord 物件，不連線。"""

import asyncio
import itertools
from types import SimpleNamespace

import pytest

from aun.config import Config
from aun.discord_bot import AunBot
from aun.hub import Hub

BOT_ID = 999
ids = itertools.count(1000)


class FakeMessage:
    def __init__(self, content="", author=None, channel=None, mentions=(), role_mentions=(), reference=None,
                 msg_id=None):
        self.id = msg_id or next(ids)
        self.content = content
        self.author = author
        self.channel = channel
        self.mentions = list(mentions)
        self.role_mentions = list(role_mentions)
        self.reference = reference
        self.replies: list[str] = []
        self.reactions: list[str] = []

    async def reply(self, text, **kwargs):
        self.replies.append(text)
        sent = FakeMessage(text, channel=self.channel)
        self.channel.messages[sent.id] = sent
        return sent

    async def add_reaction(self, emoji):
        self.reactions.append(emoji)

    async def remove_reaction(self, emoji, user):
        self.reactions.remove(emoji)

    async def edit(self, content):
        self.content = content


class FakeChannel:
    def __init__(self, channel_id=42):
        self.id = channel_id
        self.messages: dict[int, FakeMessage] = {}
        self.sent: list[str] = []

    async def send(self, text, **kwargs):
        self.sent.append(text)
        msg = FakeMessage(text, channel=self)
        self.messages[msg.id] = msg
        return msg

    async def fetch_message(self, msg_id):
        return self.messages[msg_id]

    def post(self, msg: FakeMessage) -> FakeMessage:
        msg.channel = self
        self.messages[msg.id] = msg
        return msg


BOT_USER = SimpleNamespace(id=BOT_ID, name="AUN")


class FakeBot(AunBot):
    user = BOT_USER  # 蓋掉 discord.Client.user（未連線時是 None）

    def __init__(self, hub, cfg, channel):
        super().__init__(hub, cfg)
        self.fake_channel = channel

    async def _channel(self, channel_id):
        return self.fake_channel if channel_id else None


def user(uid, name):
    return SimpleNamespace(id=uid, name=name, display_name=name.title(), bot=False)


@pytest.fixture()
def env(tmp_path):
    cfg = Config(db_path=str(tmp_path / "aun.db"), host="", port=0, progress_interval=0, summary_idle=1200,
                 llm_base_url="", llm_api_key="", llm_models=(), discord_token="x", discord_post_summaries=True)
    hub = Hub(cfg)
    hub.add_agent("justin-agent", "justin", owner_discord="justin_dc")  # 只登記 username
    hub.add_agent("partner-agent", "partner", owner_discord="222")
    hub.ensure_member("justin-agent", "demo")
    hub.ensure_member("partner-agent", "demo")
    channel = FakeChannel()
    bot = FakeBot(hub, cfg, channel)
    return hub, bot, channel


def test_discord_flows(env):
    hub, bot, channel = env
    justin, partner = user(111, "justin_dc"), user(222, "partner_dc")

    async def flow():
        # 綁定頻道
        await bot.on_message(channel.post(FakeMessage("<@999> 綁定 demo", justin, mentions=[BOT_USER])))
        assert hub.project_by_channel("42") == "demo"

        # 打 @AUN 時選到 bot 的自動身分組，也要當成 tag 了 bot
        own_role = SimpleNamespace(id=7, name="AUN", tags=SimpleNamespace(bot_id=BOT_ID))
        via_role = channel.post(FakeMessage("<@&7> 說明", justin, role_mentions=[own_role]))
        await bot.on_message(via_role)
        assert "AUN 使用方式" in via_role.replies[0]

        # 沒 tag bot 的訊息完全不處理
        ignored = channel.post(FakeMessage("我們討論一下", justin))
        await bot.on_message(ignored)
        assert ignored.replies == []

        # 結論 → 全體；username 第一次出現時補上 Discord id
        msg = channel.post(FakeMessage("<@999> 錯誤一律回 {code, message}", justin, mentions=[BOT_USER]))
        await bot.on_message(msg)
        item = hub.db.execute("SELECT * FROM items WHERE kind='conclusion'").fetchone()
        assert item["author"] == "justin" and item["body"] == "錯誤一律回 {code, message}"
        assert hub.agent("justin-agent")["owner_discord"] == "111"
        assert "justin-agent ⏳" in msg.replies[0] and msg.reactions == ["⏳"]

        # 兩個 agent 都讀到後：狀態訊息更新、原訊息改成 ✅
        hub.inbox("justin-agent", "demo")
        hub.inbox("partner-agent", "demo")
        while not bot.events.empty():
            event = bot.events.get_nowait()
            if event["type"] == "delivered":
                await bot._update_delivery(event["item_ids"])
        status_id = int(hub.item(item["id"])["meta"]["status_msg"])
        assert "justin-agent ✅ · partner-agent ✅" in channel.messages[status_id].content
        assert msg.reactions == ["✅"]

        # 搭檔 tag justin-agent 的身分組 → 需要 justin 確認
        role = SimpleNamespace(id=5, name="justin-agent")
        await bot.on_message(channel.post(
            FakeMessage("<@999> <@&5> API 改 v2", partner, mentions=[BOT_USER], role_mentions=[role])))
        inst = hub.db.execute("SELECT * FROM items WHERE kind='instruction'").fetchone()
        assert inst["target"] == "justin-agent" and '"needs_approval": true' in inst["meta"]

        # 未知的身分組
        bad = channel.post(FakeMessage("<@999> <@&6> x", partner, mentions=[BOT_USER],
                                       role_mentions=[SimpleNamespace(id=6, name="nobody")]))
        await bot.on_message(bad)
        assert "沒有對應的 agent" in bad.replies[0]

        # agent 向人類提問 → 貼到頻道並 tag 主人
        qid = hub.ask_human("justin-agent", "demo", "schema 要不要加 index？")
        await bot._post(qid)
        assert "<@111>" in channel.sent[-1] and "schema 要不要加 index" in channel.sent[-1]
        question_msg = channel.messages[max(channel.messages)]

        # 人類回覆那則訊息並 @AUN → 變成給 justin-agent 的回覆
        reference = SimpleNamespace(message_id=question_msg.id, resolved=question_msg)
        await bot.on_message(channel.post(
            FakeMessage("<@999> 要，加在 user_id", partner, mentions=[BOT_USER], reference=reference)))
        rep = hub.db.execute("SELECT * FROM items WHERE kind='reply'").fetchone()
        assert rep["target"] == "justin-agent" and rep["ref_id"] == qid and rep["author"] == "partner"
        assert "[partner（人類） 回覆" in hub.inbox("justin-agent", "demo")

        # 提議決策 → 貼出並附 ✅；人類按 ✅ 即確認
        did = hub.propose_decision("partner-agent", "demo", "所有時間用 UTC")
        await bot._post(did)
        decision_msg = channel.messages[max(channel.messages)]
        assert decision_msg.reactions == ["✅"]
        await bot.on_raw_reaction_add(SimpleNamespace(user_id=111, emoji="✅", message_id=decision_msg.id,
                                                      member=user(111, "justin_dc")))
        assert hub.item(did)["meta"]["status"] == "confirmed"
        assert "已由 justin 確認" in hub.inbox("partner-agent", "demo")

        # 指示處理結果 → 回覆原訊息並 tag 提出的人
        hub.resolve_request("justin-agent", "demo", inst["id"], "done", "已改成 /v2")
        update = hub.db.execute("SELECT id FROM items WHERE kind='request_update'").fetchone()["id"]
        await bot._post(update)
        original = next(m for m in channel.messages.values() if m.content.startswith("<@999> <@&5>"))
        assert "<@222>" in original.replies[-1] and "已完成" in original.replies[-1]

    asyncio.run(flow())
