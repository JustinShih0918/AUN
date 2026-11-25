"""Discord 總管 bot：只處理有 @ 它的訊息。

  @AUN 綁定 <專案>            把這個頻道（含其中的討論串）綁定到 AUN 專案
  @AUN 狀態                   列出各成員最新進度
  @AUN <結論>                 結論送給專案內所有 agent
  @AUN @某-agent <內容>       只送給該身分組對應的 agent（不是主人下的，agent 會先問主人）
  回覆某則訊息 + @AUN          被回覆的那則一起當作內容；若回覆的是 agent 的提問，則變成對它的回答

反方向：agent 之間的訊息、向人類提問、提議決策、指示處理結果、進度摘要，都會貼回綁定的頻道。
"""

import asyncio
import logging
import re
from typing import Any

import discord

from .config import Config
from .hub import Hub, HubError

log = logging.getLogger("aun.discord")

MENTION = re.compile(r"<@[!&]?\d+>")
HELP = (
    "**AUN 使用方式**\n"
    "`@AUN 綁定 <專案>`：把這個頻道綁定到 AUN 專案（專案名稱要和 repo 裡的 AUN_PROJECT 相同）\n"
    "`@AUN 狀態`：各 agent 最新進度\n"
    "`@AUN <結論>`：送給專案內所有 agent\n"
    "`@AUN @某-agent <內容>`：只送給那個 agent\n"
    "回覆某則訊息並 `@AUN`：被回覆的訊息會一起送出；回覆的是 agent 的提問時，就是回答它"
)
LABELS = {"conclusion": "結論", "instruction": "指示", "reply": "回覆"}
STATUS_TEXT = {"done": "已完成 ✅", "declined": "已拒絕 ❌", "acknowledged": "已收到 👍"}


def _clip(text: str, limit: int = 1900) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


class AunBot(discord.Client):
    def __init__(self, hub: Hub, cfg: Config):
        intents = discord.Intents.default()
        # 需要在 Developer Portal 開啟 Message Content Intent，才能讀到「被回覆的那則訊息」的內容
        intents.message_content = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions(everyone=False, roles=False))
        self.hub = hub
        self.cfg = cfg
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        hub.listeners.append(self._on_hub_event)

    def _on_hub_event(self, event: dict[str, Any]) -> None:
        if event["type"] == "delivered" or event.get("layer", 0) >= 1:
            self.events.put_nowait(event)

    async def setup_hook(self) -> None:
        self.loop.create_task(self._pump())

    async def on_ready(self) -> None:
        log.info("Discord 已連線：%s", self.user)

    # ── 人類 → AUN ───────────────────────────────────────────────────

    def _is_own_role(self, role: discord.Role) -> bool:
        # bot 加入伺服器時 Discord 會自動建立同名身分組，大家打 @AUN 時常會選到它
        tags = getattr(role, "tags", None)
        return bool(tags and self.user and tags.bot_id == self.user.id)

    async def on_message(self, msg: discord.Message) -> None:
        if msg.author.bot or self.user is None:
            return
        if self.user not in msg.mentions and not any(self._is_own_role(r) for r in msg.role_mentions):
            return
        try:
            await self._handle(msg)
        except HubError as e:
            await msg.reply(f"⚠️ {e}", mention_author=False)
        except Exception:
            log.exception("處理 Discord 訊息失敗")
            await msg.reply("⚠️ AUN 處理這則訊息時發生錯誤，請看 server log", mention_author=False)

    async def _handle(self, msg: discord.Message) -> None:
        text = MENTION.sub("", msg.content).strip()
        is_thread = isinstance(msg.channel, discord.Thread)
        channel_id = str(msg.channel.parent_id if is_thread else msg.channel.id)
        cmd, _, arg = text.partition(" ")

        if cmd.lower() in ("bind", "綁定"):
            self.hub.bind_channel(arg.strip(), channel_id)
            await msg.reply(f"已將這個頻道綁定到專案 **{arg.strip()}**", mention_author=False)
            return
        if cmd.lower() in ("help", "說明") or (not text and msg.reference is None):
            await msg.reply(HELP, mention_author=False)
            return

        project_id = self.hub.project_by_channel(channel_id)
        if project_id is None:
            await msg.reply("這個頻道還沒綁定專案，請先 `@AUN 綁定 <專案>`", mention_author=False)
            return
        if cmd.lower() in ("status", "狀態"):
            await msg.reply(_clip(self._status(project_id)), mention_author=False)
            return

        human = self.hub.human_name(str(msg.author.id), msg.author.name, msg.author.display_name)
        meta = {"discord_channel": str(msg.channel.id), "discord_msg": str(msg.id),
                "requester_discord": str(msg.author.id), "thread": msg.channel.name if is_thread else None}

        ref = await self._referenced(msg)
        if ref is not None:
            ref_item = self.hub.item_by_discord(str(ref.id))
            # 回覆 agent 的提問／訊息 → 視為回答那個 agent
            if ref_item and ref_item["author_type"] == "agent" and ref_item["kind"] in ("ask_human", "question",
                                                                                      "message"):
                item_id = self.hub.reply(human, "human", project_id, ref_item["id"], text or "（空白回覆）", meta=meta)
                await self._ack(msg, [item_id], "回覆")
                return
            text = f"{text}\n（引用 {ref.author.display_name}：{ref.content}）".strip()

        targets = []
        for role in msg.role_mentions:
            if self._is_own_role(role):
                continue
            agent = self.hub.agent_for_role(str(role.id), role.name)
            if agent is None:
                await msg.reply(f"⚠️ 身分組 {role.name} 沒有對應的 agent", mention_author=False)
                return
            targets.append(agent)
        ids = self.hub.human_post(project_id, human, str(msg.author.id), text, targets, meta)
        await self._ack(msg, ids, "指示" if targets else "結論")

    async def _referenced(self, msg: discord.Message) -> discord.Message | None:
        if msg.reference is None or msg.reference.message_id is None:
            return None
        if isinstance(msg.reference.resolved, discord.Message):
            return msg.reference.resolved
        try:
            return await msg.channel.fetch_message(msg.reference.message_id)
        except discord.HTTPException:
            return None

    async def _ack(self, msg: discord.Message, ids: list[int], label: str) -> None:
        status = await msg.reply(self._delivery_text(ids, label), mention_author=False)
        for item_id in ids:
            self.hub.update_meta(item_id, status_msg=str(status.id), group=ids, label=label)
            self.hub.link_discord(str(msg.id), item_id)
        await msg.add_reaction("⏳")

    def _delivery_text(self, ids: list[int], label: str) -> str:
        parts = [f"{agent} {'✅' if done else '⏳'}" for agent, done in self.hub.delivery_status(ids)]
        refs = "、".join(f"#{i}" for i in ids)
        return f"📥 已記錄為{label} {refs} → " + (" · ".join(parts) or "（專案內還沒有 agent 連上）")

    def _status(self, project_id: str) -> str:
        summaries = self.hub.latest_summaries(project_id)
        members = self.hub.members(project_id)
        lines = [f"**{project_id}** 成員：{'、'.join(members) or '（還沒有 agent 連上）'}"]
        lines += [f"\n**{s['author']}**\n{s['body']}" for s in summaries]
        return "\n".join(lines) if summaries else lines[0] + "\n目前還沒有進度摘要"

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        # 人類對「提議決策」按 ✅ → 確認
        if self.user is None or payload.user_id == self.user.id or str(payload.emoji) != "✅":
            return
        item = self.hub.item_by_discord(str(payload.message_id))
        if not item or item["kind"] != "decision" or item["meta"].get("status") != "proposed":
            return
        member = payload.member
        name = (self.hub.human_name(str(member.id), member.name, member.display_name)
                if member else str(payload.user_id))
        self.hub.confirm_decision(item["project_id"], item["id"], by=name, by_type="human")

    # ── AUN → Discord ───────────────────────────────────────────────

    async def _pump(self) -> None:
        await self.wait_until_ready()
        while True:
            event = await self.events.get()
            try:
                if event["type"] == "delivered":
                    await self._update_delivery(event["item_ids"])
                else:
                    await self._post(event["id"])
            except Exception:
                log.exception("同步到 Discord 失敗：%s", event)

    async def _channel(self, channel_id: str | None) -> discord.abc.Messageable | None:
        if not channel_id:
            return None
        channel = self.get_channel(int(channel_id))
        if channel is None:
            try:
                channel = await self.fetch_channel(int(channel_id))
            except discord.HTTPException:
                return None
        return channel  # type: ignore[return-value]

    async def _update_delivery(self, item_ids: list[int]) -> None:
        done_groups: set[str] = set()
        for item_id in item_ids:
            item = self.hub.item(item_id)
            meta = item["meta"] if item else {}
            if not meta.get("status_msg") or meta.get("all_delivered") or meta["status_msg"] in done_groups:
                continue
            done_groups.add(meta["status_msg"])
            channel = await self._channel(meta.get("discord_channel"))
            if channel is None:
                continue
            group = meta.get("group") or [item_id]
            status = await channel.fetch_message(int(meta["status_msg"]))
            await status.edit(content=self._delivery_text(group, meta.get("label", "")))
            if all(done for _, done in self.hub.delivery_status(group)):
                for member_id in group:
                    self.hub.update_meta(member_id, all_delivered=True)
                original = await channel.fetch_message(int(meta["discord_msg"]))
                await original.add_reaction("✅")
                await original.remove_reaction("⏳", self.user)

    def _mention_owner(self, agent_name: str | None) -> str:
        agent = self.hub.agent(agent_name) if agent_name else None
        if agent is None:
            return ""
        if agent["owner_discord"] and agent["owner_discord"].isdigit():
            return f"<@{agent['owner_discord']}>"
        return f"@{agent['owner']}"

    async def _post(self, item_id: int) -> None:
        item = self.hub.item(item_id)
        if item is None or item["author_type"] != "agent":
            return
        kind, author, body, meta = item["kind"], item["author"], item["body"], item["meta"]
        channel = await self._channel(self.hub.project_channel(item["project_id"]))
        if channel is None:
            return
        reference: discord.Message | None = None
        add_check = False

        if kind in ("message", "question"):
            to = item["target"] or "全體"
            label = "問題" if kind == "question" else "訊息"
            text = f"🤖 **{author} → {to}**（{label} #{item_id}）：{body}"
            if meta.get("urgent"):
                text = f"🚨 {self._mention_owner(item['target'])} " + text
        elif kind == "reply":
            text = f"🤖 **{author}** 回覆 #{item['ref_id']}：{body}"
            reference = await self._discord_ref(item["ref_id"])
            ref_item = self.hub.item(item["ref_id"])
            if ref_item and ref_item["author_type"] == "human" and ref_item["meta"].get("requester_discord"):
                text = f"<@{ref_item['meta']['requester_discord']}> " + text
        elif kind == "ask_human":
            text = (f"🙋 **{author}** 想問 {self._mention_owner(author)}（#{item_id}）：{body}\n"
                    "（回覆這則訊息並 @AUN 即可回答）")
        elif kind == "decision":
            text = f"📝 **{author}** 提議決策 #{item_id}：{body}\n（任何人按 ✅ 即確認）"
            add_check = True
        elif kind == "decision_confirmed":
            text = f"✅ 決策 #{item['ref_id']} 已由 {author} 確認：{body}"
        elif kind == "request_update":
            ref_item = self.hub.item(item["ref_id"]) or {"meta": {}}
            requester = ref_item["meta"].get("requester_discord")
            text = (f"{f'<@{requester}> ' if requester else ''}**{author}** "
                    f"{STATUS_TEXT.get(meta.get('status'), meta.get('status'))}（#{item['ref_id']}）：{body}")
            reference = await self._discord_ref(item["ref_id"])
        elif kind == "summary" and self.cfg.discord_post_summaries:
            text = f"📊 **{author} 進度**（#{item_id}）\n{body}"
        elif kind == "correction":
            text = f"✏️ **{author}** 修正了進度摘要 #{item['ref_id']}：{body}"
        else:
            return

        allowed = discord.AllowedMentions(users=True, roles=False, everyone=False)
        if reference is not None:
            sent = await reference.reply(_clip(text), mention_author=False, allowed_mentions=allowed)
        else:
            sent = await channel.send(_clip(text), allowed_mentions=allowed)
        self.hub.link_discord(str(sent.id), item_id)
        if add_check:
            await sent.add_reaction("✅")

    async def _discord_ref(self, item_id: int | None) -> discord.Message | None:
        item = self.hub.item(item_id) if item_id else None
        if not item:
            return None
        row = self.hub.db.execute("SELECT message_id FROM discord_links WHERE item_id=? LIMIT 1",
                                  (item_id,)).fetchone()
        channel_id = item["meta"].get("discord_channel") or self.hub.project_channel(item["project_id"])
        channel = await self._channel(channel_id)
        if channel is None or row is None:
            return None
        try:
            return await channel.fetch_message(int(row["message_id"]))
        except discord.HTTPException:
            return None
