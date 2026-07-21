import os
import discord
import yt_dlp
import time
import random
import asyncio
import json
import chat_exporter
import io
import threading
import requests
from flask import Flask, render_template_string, request
from itertools import count  # <--- 請務必補上這行
from datetime import datetime, timedelta, timezone  # <--- 這裡修正
from discord.ext import commands, tasks
from discord import app_commands
from dotenv import load_dotenv
from supabase import create_client
from typing import Optional
from gtts import gTTS
from collections import defaultdict

if not discord.opus.is_loaded():
    discord.opus.load_opus('/opt/homebrew/lib/libopus.dylib')

user_messages = defaultdict(list) # 在檔案最上方加入
search_cache = {}
queues = {}
ticket_counter = count(1)

# --- 初始化 ---
load_dotenv()
intents = discord.Intents.all()
intents.members = True          # 必須：用於抓取成員、給予身分組
intents.message_content = True  # 必須：用於處理訊息內容（如防刷頻、指令）
intents.voice_states = True     # 必須：用於語音指令 (演奏功能)

bot = commands.Bot(command_prefix="/", intents=intents)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

VERIFY_ROLE_ID = 1526427255130161173  # 你給的驗證身分組 ID
RECAPTCHA_SECRET_KEY = "你的Google_Secret_Key"  # 貼上你的 Secret Key
DISCORD_BOT_TOKEN = "你的Discord_Bot_Token"  # 你的機器人 Token

# --- 補上缺少的 TicketControlsView 避免報錯 ---
class TicketControlsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="關閉客服單 / Close Ticket", style=discord.ButtonStyle.danger, custom_id="close_ticket")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("🔒 正在關閉此客服單...", ephemeral=True)
        await asyncio.sleep(2)
        await interaction.channel.delete()

# 網頁前端畫面樣版 (包含 Google reCAPTCHA)
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="zh-Hant">
<head>
    <meta charset="UTF-8">
    <title>星津蓮汐 · 安全驗證</title>
    <script src="https://www.google.com/recaptcha/api.js" async defer></script>
    <style>
        body { background-color: #121212; color: #e0e0e0; font-family: sans-serif; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; }
        .card { background: #1e1e1e; padding: 30px; border-radius: 12px; box-shadow: 0 4px 15px rgba(0,0,0,0.5); text-align: center; }
        .btn { margin-top: 20px; padding: 10px 20px; background: #8e44ad; color: white; border: none; border-radius: 5px; cursor: pointer; font-size: 16px; }
        .btn:hover { background: #732d91; }
    </style>
</head>
<body>
    <div class="card">
        <h2>📜 星津蓮汐 · 入境結界驗證</h2>
        <p>請完成下方人機驗證以取得 Discord 身分組</p>
        <form action="/verify" method="POST">
            <input type="hidden" name="user_id" value="{{ user_id }}">
            <input type="hidden" name="guild_id" value="{{ guild_id }}">
            <div class="g-recaptcha" data-sitekey="你的Google_Site_Key"></div>
            <button type="submit" class="btn">確認驗證 / Verify</button>
        </form>
    </div>
</body>
</html>
"""

app = Flask(__name__)
@app.route("/")
def web_index():
    user_id = request.args.get("user_id", "")
    guild_id = request.args.get("guild_id", "")
    return render_template_string(HTML_TEMPLATE, user_id=user_id, guild_id=guild_id)

@app.route("/verify", methods=["POST"])
def web_verify():
    user_id = request.form.get("user_id")
    guild_id = request.form.get("guild_id")
    recaptcha_response = request.form.get("g-recaptcha-response")

    payload = {
        "secret": RECAPTCHA_SECRET_KEY,
        "response": recaptcha_response
    }
    resp = requests.post("https://www.google.com/recaptcha/api/siteverify", data=payload)
    result = resp.json()

    if not result.get("success"):
        return "❌ 驗證失敗：人機驗證未通過，請返回重試。"

    if not user_id or not guild_id:
        return "❌ 錯誤：找不到使用者或伺服器資訊。"

    url = f"https://discord.com/api/v10/guilds/{guild_id}/members/{user_id}/roles/{VERIFY_ROLE_ID}"
    headers = {
        "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
        "Content-Type": "application/json"
    }
    discord_resp = requests.put(url, headers=headers)

    if discord_resp.status_code in [204, 200]:
        return "✨ 驗證成功！你已通過結界，請返回 Discord 查看身分組。"
    else:
        return f"❌ 驗證通過，但發放身分組失敗（代碼: {discord_resp.status_code}），請聯絡管理員。"

def run_flask():
    app.run(host="0.0.0.0", port=5001)

threading.Thread(target=run_flask, daemon=True).start()

def migrate_data():
    try:
        with open("roles.json", "r", encoding="utf-8") as f:
            data = json.load(f)
        
        for guild_id, channels in data.items():
            payload = {
                "guild_id": str(guild_id),
                "level_channel_id": str(channels.get("level_channel", "")),
                "log_channel_id": str(channels.get("log_channel", "")),
                "welcome_channel_id": str(channels.get("welcome_channel", "")),
                "leave_channel_id": str(channels.get("leave_channel", ""))
            }
            supabase.table("guild_settings").upsert(payload).execute()
            print(f"伺服器 {guild_id} 已遷移成功！")
    except FileNotFoundError:
        print("未找到 roles.json 檔案，跳過遷移。")

migrate_data()

# --- 符號與資料庫定義 ---
INK = {
    0: "<a:ink_0:1527227763231166474>", 
    1: "<a:ink_1:1527227779089960990>", 
    2: "<a:ink_2:1527227794898161734>", 
    3: "<a:ink_3:1527227807082479687>", 
    4: "<a:ink_4:1527227830079852555>",
    5: "<a:ink_5_gif:1527264618844524585>"
}
FEATHER_ICON = "༗" 
SOMA_ICON = "𑣿"
GOLD_BELL = "<a:goldbell_swing:1527272243560976495>"
SILVER_BELL = "<a:silverbell_swing:1527272426910777435>"
SCROLL_IMG = "<a:scroll:1527274271879991346>"
INSTRUMENT_IMG = "<a:instrument:1527274164610793533>"

ROLES = {
    "白靈鵠": "https://cdn.discordapp.com/attachments/1526215549720330300/1527638191035646062/Hailuo_Video_1_1_534052645028671496-ezgif.com-optimize.gif?ex=6a5b636e&is=6a5a11ee&hm=2e71b0d4435e3dc3768de47d8f942cd511c366125927e9f4ea80f0ccd97c2cb0",
    "‧₊˚♪ 金鈴 𝐿𝓎𝓇𝑒 𝄞₊˚⊹": "https://cdn.discordapp.com/attachments/1526215549720330300/1527637943299211364/ezgif.com-video-to-gif-converter.gif?ex=6a5b6333&is=6a5a11b3&hm=34c3dc9e062fae1adc6877e97be1dc58b72d2792cb0229aec54e307b3b402727",
    "˗ˏˋ꒰ 銀鈴 ✉ 𝐿𝓎𝓉𝑒 ꒱ˎˊ˗": "https://cdn.discordapp.com/attachments/1526215549720330300/1527638254105530378/Hailuo_Video__534484207067148289-ezgif.com-video-to-gif-converter.gif?ex=6a5b637d&is=6a5a11fd&hm=13b1d593406550d87d85701514d6f273220a3e98f9bfee379e23f84b6de2c9a3",
    "₊‧꒰ა 銅鈴 ✮ 𝐿𝓎𝓇𝒶 ໒꒱‧₊": "https://cdn.discordapp.com/attachments/1526215549720330300/1528286558912905226/Copilot_20260719_142455.png?ex=6a5dbf45&is=6a5c6dc5&hm=4e25f2c3d3ef5a847da08338be0f8cc5ca0c4ac7431838a313a55b97123f26bd&",
    "❨˚˖茶茶 ꕤ࿔ 𝒞𝒽𝒶𝓇𝓁𝒾𝚎 ༘⋆⊹": "https://cdn.discordapp.com/attachments/1526215549720330300/1528321918133076079/Copilot_20260719_160430.jpeg?ex=6a5de034&is=6a5c8eb4&hm=86f510e304ea1f5bff045d0e3499113479d52f60fba373fb8b7aea415cd6271e&",
    "☀︎˚₊溫溫 𑄝࿔ 𝒲𝒾𝓃𝓈𝑜𝓃⋆˚࿔": "https://cdn.discordapp.com/attachments/1526215549720330300/1528321917709320262/Copilot_20260719_160430.jpeg?ex=6a5de034&is=6a5c8eb4&hm=c524a0fada6a16628769f1e53087e1dac2b9d6ba5961a0d4ee94e7a231dffc6e&",
    "黑貓夫妻 𝐵𝓁𝒶𝒸𝓀𝒞𝒶𝓉 𝒞𝑜𝓊𝓅𝓁𝑒": "https://cdn.discordapp.com/attachments/1526215549720330300/1528324563841187860/Copilot_20260719_160042.png?ex=6a5de2aa&is=6a5c912a&hm=a4dab7e4cf4c28b21c1bd2a197ef347f0700044a34bd3e45e11597f62930b67c&",
    "⋆༺𓆩瑞恩 ⚔ 𝑅𝓎𝒶𝓃𓆪༻⋆": "https://cdn.discordapp.com/attachments/1526215549720330300/1527969128344719460/Copilot_20260718_164950.png?ex=6a5d4064&is=6a5beee4&hm=29d8284491c561a017a531e2c4a684d9158d711b96d4e58529b5886650a9ab81&",
    "°❀ 綻次郎 𐀔 𝒞𝒽𝑜𝒿𝒾𝓇𝑜 ❀°": "https://cdn.discordapp.com/attachments/1526215549720330300/1527971370666037288/Copilot_20260718_173222.png?ex=6a5d427b&is=6a5bf0fb&hm=ebff85e35aa2f23f5a6860235fdbf4f9d06e2fb4041da534048809de41206868&"
}

VOW_ITEMS = {
    "名號刻印 (Naming Right)": 10000,
    "殊榮詞牌 (Title Coronation)": 50000,
    "永恆之名 (Permanent)": 80000,
    "聖域開啟 (Sanctum)": 100000
}

def get_queue(guild_id):
    if guild_id not in queues:
        queues[guild_id] = asyncio.Queue()
    return queues[guild_id]

async def play_next(guild_id: int):
    q = get_queue(guild_id)
    if not q.empty():
        func = await q.get()
        await func()

# --- 資料庫基礎函數 (保留唯一一份，避免重複定義) ---
def get_user(user_id):
    uid = str(user_id)
    res = supabase.table("users").select("*").eq("id", uid).execute()
    return res.data[0] if res.data else {"id": uid, "level": 1, "exp": 0, "feathers": 0, "soma": 0}

def update_user(user_id, data_to_update):
    supabase.table("users").update(data_to_update).eq("id", str(user_id)).execute()

def get_guild_settings(guild_id):
    guild_id_str = str(guild_id)
    res = supabase.table("guild_settings").select("*").eq("guild_id", guild_id_str).execute()
    return res.data[0] if res.data else {}

# --- 唯一且正確的 Webhook 發送函式 ---
async def send_as_role(role_name, destination, category="", content="", gif="", roles=None, view=None, embed=None, is_zh=True, is_ticket=False):
    if roles is None:
        roles = []
    
    if isinstance(destination, discord.Interaction):
        interaction = destination
        channel = interaction.channel
        guild = interaction.guild
    else:
        interaction = None
        channel = destination
        guild = channel.guild

    if is_ticket:
        num = next(ticket_counter)
        ticket_id = f"{num:04}" 
        
        prefix_map = {
            "普通客服 / General Support": "普通-normal",
            "合作 / Partnership": "合作-collab",
            "建議 / Suggestions": "建議-suggest",
            "舉報 / Report": "舉報-report"
        }
        channel_name = f"{prefix_map.get(category, 'ticket')}-{ticket_id}"
        
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            interaction.user: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        }
        for rid in roles:
            role = guild.get_role(rid)
            if role: 
                overwrites[role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
                
        new_channel = await guild.create_text_channel(name=channel_name, overwrites=overwrites)
        
        await asyncio.sleep(1.0)
        webhook = await new_channel.create_webhook(name=role_name)
        
        avatar_map = {
            "☀︎˚₊溫溫 𑄝࿔ 𝒲𝒾𝓃𝓈𝑜𝓃⋆˚࿔": "https://cdn.discordapp.com/attachments/1526215549720330300/1528321917709320262/Copilot_20260719_160430.jpeg",
            "❨˚˖茶茶 ꕤ࿔ 𝒞𝒽𝒶𝓇𝓁𝒾𝚎 ༘⋆⊹": "https://cdn.discordapp.com/attachments/1526215549720330300/1528321918133076079/Copilot_20260719_160430.jpeg"
        }
        role_avatar = avatar_map.get(role_name)
 
        mentions = " ".join([f"<@&{rid}>" for rid in roles])
        final_content = f"{mentions}\n歡迎開啟客服單。"
        
        try:
            await webhook.send(
                content=final_content,
                embed=discord.Embed(title=category, description=content, color=0x00ff80).set_image(url=gif),
                view=TicketControlsView(),
                username=role_name,
                avatar_url=role_avatar
            )
        except Exception as e:
            print(f"客服單 Webhook 發送失敗: {e}")
        
        if interaction:
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(f"✅ 已為您開啟客服單：{new_channel.mention}", ephemeral=True)
            except Exception:
                pass
            
        return 

    avatar_url = ROLES.get(role_name, ROLES.get("白靈鵠"))
    name = "⋆｡ﾟ白靈鵠 ☁ 𝑨𝒆𝒕𝒉𝒆𝒓𝒘𝒚𝒏｡⋆𓂃 ོ " if role_name == "白靈鵠" else role_name
    
    webhooks = await channel.webhooks()
    webhook = discord.utils.get(webhooks, name="AetherwynHook")
    if not webhook:
        webhook = await channel.create_webhook(name="AetherwynHook")
    
    send_params = {"username": name, "avatar_url": avatar_url}
    if content:
        send_params["content"] = content
    if embed:
        if gif:
            embed.set_image(url=gif)
        send_params["embed"] = embed
    if view: 
        send_params["view"] = view
        
    await webhook.send(**send_params)

# --- 2. 處理扣款與互動邏輯 ---
async def process_vow_redemption(interaction, role_name, category, item_name, is_zh=True):
    user_id = interaction.user.id
    cost = VOW_ITEMS.get(item_name, 0)
    
    res = supabase.table("users").select("feathers").eq("id", user_id).execute()
    current_feathers = res.data[0]['feathers'] if res.data else 0
    
    if current_feathers < cost:
        msg = "❌ 靈羽不足，銅鈴未能感知您的願望。" if is_zh else "❌ Insufficient feathers."
        return await interaction.response.send_message(msg, ephemeral=True)
    
    new_balance = current_feathers - cost
    supabase.table("users").update({"feathers": new_balance}).eq("id", user_id).execute()
    
    embed = discord.Embed(title="✨ 祈願應允 / Vow Granted", color=0x00ff80)
    embed.description = f"你向靈泉獻上神羽，銅鈴吟唱古老咒語，應允了你的願望：**{item_name}**。"
    
    await interaction.response.send_message(embed=embed, ephemeral=True)
    
    await send_as_role(role_name, interaction.channel, embed=embed)
    admin_channel = interaction.client.get_channel(1527905097319841863)
    if admin_channel:
        await admin_channel.send(f"🎐 Lyra reports: {interaction.user.mention} has redeemed: {item_name}.")

# --- 商單面板部署指令 ---
@bot.tree.command(name="setup_vow_shop", description="部署靈泉祈願商店面板 (Admin Only)")
@app_commands.checks.has_permissions(administrator=True)
async def setup_vow_shop(interaction: discord.Interaction):
    desc_text = (
        "在銅鈴的幫助下，向靈泉獻上神羽，換取修煉福報。\n"
        "Offer feathers to the spring with Lyra's help to gain rewards.\n"
        "記得扣除靈羽後到 <#1527905097319841863> 開客服單領取獎勵 \n"
        "After deducting feathers, remember to open a ticket at <#1527905097319841863> to recieve your rewards."
    )
    
    embed = discord.Embed(title="🎐 靈泉祈願商店", description=desc_text, color=0x00ff80)
    embed.add_field(name="祈願項目", value="\n".join([f"• {name}: {cost}" for name, cost in VOW_ITEMS.items()]), inline=False)
    embed.set_footer(text="Lyra's Vow System")
    
    # 修正參數傳遞：將 interaction 作為第二個位置參數傳入 destination
    await send_as_role(
        role_name="₊‧꒰ა 銅鈴 ✮ 𝐿𝓎𝓇𝒶 ໒꒱‧₊", 
        destination=interaction, 
        category="商城兌換", 
        content="", 
        gif="", 
        roles=[], 
        view=VowView(), 
        embed=embed
    )
    
    await interaction.response.send_message("✅ 商店面板已部署。", ephemeral=True)

class VowSelect(discord.ui.Select):
    def __init__(self, lang="zh"):
        self.lang = lang
        options = []
        
        for name, cost in VOW_ITEMS.items():
            cost_text = f"(Requires) {cost} {FEATHER_ICON}"
            options.append(
                discord.SelectOption(
                    label=name, 
                    value=name, 
                    description=cost_text
                )
            )
            
        placeholder = "請選擇你要祈願的項目... / Please select your vow item..."
        super().__init__(placeholder=placeholder, options=options, custom_id="vow_select_menu")

    async def callback(self, interaction: discord.Interaction):
        item_name = self.values[0]
        await process_vow_redemption(
            interaction, 
            "₊‧꒰ა 銅鈴 ✮ 𝐿𝓎𝓇𝒶 ໒꒱‧₊", 
            "商城兌換", 
            item_name
        )

class VowView(discord.ui.View):
    def __init__(self, lang="zh"):
        super().__init__(timeout=None)
        self.add_item(VowSelect(lang=lang))

# --- 等級系統相關函數 ---
def get_verse_name(level, lang="zh"):
    levels = {50: {"zh": "詩佛/詩仙/詩聖", "en": "Nirvana/Aeon/Deity"}, 1: {"zh": "初醒", "en": "Awakening"}}
    return levels.get(50 if level >= 50 else 1, levels[1])[lang]

def calculate_required_exp(level):
    req = 50.0
    for i in range(1, level): req = req * 1.2
    return int(min(req, 50000))

TITLES = {
    5: {"name": ("含英咀華", "Poet"), "id": 1527699215030030416},
    10: {"name": ("翰墨丹青", "Artist"), "id": 1527699361973272666},
    15: {"name": ("文采斐然", "Virtuoso"), "id": 1527699500490031206},
    20: {"name": ("絕世奇才", "Muse"), "id": 1527699678349496453},
    25: {"name": ("詞章大家", "Vates"), "id": 1527699841063587840},
    30: {"name": ("結廬幽篁", "Hermes"), "id": 1527700120873861291},
    35: {"name": ("東籬採菊", "Arcadia"), "id": 1527700432455991516},
    40: {"name": ("松下問童", "Sigil"), "id": 1527700901035380900},
    45: {"name": ("枕流漱石", "Epoch"), "id": 1527701317425037498}
}

BRANCH_IDS = {
    "Nirvana": 1527701542810157218,
    "Aeon": 1527701809035083866,
    "Deity": 1527702118432117002
}

class BranchSelectView(discord.ui.View):
    def __init__(self, member):
        super().__init__(timeout=None)
        self.member = member

    @discord.ui.select(
        placeholder="請選擇你的詩之巔峰 / Select your pinnacle of poetry",
        options=[
            discord.SelectOption(label="詩佛 / Nirvana", value="Nirvana"),
            discord.SelectOption(label="詩仙 / Aeon", value="Aeon"),
            discord.SelectOption(label="詩聖 / Deity", value="Deity"),
        ]
    )
    async def select_branch(self, interaction: discord.Interaction, select: discord.ui.Select):
        if interaction.user.id != self.member.id:
            return await interaction.response.send_message("這不是你的選擇 / Not your choice.", ephemeral=True)
        
        branch = select.values[0]
        role = interaction.guild.get_role(BRANCH_IDS[branch])
        if role: await self.member.add_roles(role)
        
        update_user(self.member.id, {"branch": branch})
        await interaction.response.send_message(f"恭喜晉升為：{branch}！ / Promoted to {branch}!", ephemeral=True)
        self.stop()

async def check_and_announce_level_up(member, new_level, channel_or_message):
    settings = get_guild_settings(member.guild.id)
    channel_id = settings.get("level_channel_id")
    
    if channel_id:
        channel = member.guild.get_channel(int(channel_id))
    else:
        channel = None
    if not channel:
        return

    if new_level in TITLES:
        info = TITLES[new_level]
        role = member.guild.get_role(info["id"])
        if role: 
            await member.add_roles(role)

        embed = discord.Embed(title="✨ 晉升公告 / Rank Promotion", color=discord.Color.gold())
        desc = f"恭喜 {member.mention} 達到 Lv.{new_level}!"
        
        if new_level % 5 == 0:
            desc += f"\n獲得稱號：**{info['name'][0]}** | **{info['name'][1]}**"
            
        embed.description = desc
        await channel.send(embed=embed)

    elif new_level == 50:
        embed = discord.Embed(title="✨ 詩之巔峰 / Pinnacle of Poetry", description="請選擇你的稱號分支：\nPlease select your title branch:")
        await channel.send(embed=embed, view=BranchSelectView(member))

# --- 自動補全與音樂搜尋 ---
def get_yt_search_results(query):
    ydl_opts = {
        'format': 'bestaudio', 
        'noplaylist': True, 
        'quiet': False, 
        'default_search': 'ytsearch' 
    }
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(f"ytsearch5:{query}", download=False)
            results = info.get('entries', [])
            return [{'title': entry['title'], 'url': entry['webpage_url']} for entry in results]
        except Exception as e:
            print(f"搜尋邏輯報錯: {e}")
            return []

async def song_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    if len(current) < 2:
        return []

    if current in search_cache:
        return search_cache[current][:5]

    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': True,
        'force_generic_extractor': True
    }

    try:
        loop = asyncio.get_running_loop()
        info = await loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(ydl_opts).extract_info(f"ytsearch5:{current}", download=False))
        
        results = info.get('entries', [])
        choices = [app_commands.Choice(name=song['title'][:100], value=song['url']) for song in results]
        
        search_cache[current] = choices
        return choices
    except Exception as e:
        print(f"自動補全失敗: {e}")
        return []

# --- 音樂控制面板 ---
class MusicControls(discord.ui.View):
    def __init__(self, voice_client, lang="zh", is_paused=False):
        super().__init__(timeout=None)
        self.voice_client = voice_client
        self.lang = lang
        self.is_paused = is_paused

        txt = {
            "zh": {"prev": "上一首", "pause": "暫停", "resume": "繼續", "next": "下一首", "restart": "重播", "stop": "停止"},
            "en": {"prev": "Prev", "pause": "Pause", "resume": "Resume", "next": "Next", "restart": "Restart", "stop": "Stop"}
        }
        l = txt.get(lang, txt["zh"])

        self.add_item(discord.ui.Button(label=l["prev"], style=discord.ButtonStyle.secondary, emoji="⏮", custom_id="prev"))
        
        p_label = l["pause"] if not is_paused else l["resume"]
        p_emoji = "⏸" if not is_paused else "▶"
        self.add_item(discord.ui.Button(label=p_label, style=discord.ButtonStyle.primary, emoji=p_emoji, custom_id="toggle_pause"))
        
        self.add_item(discord.ui.Button(label=l["next"], style=discord.ButtonStyle.secondary, emoji="⏭", custom_id="next"))
        self.add_item(discord.ui.Button(label=l["restart"], style=discord.ButtonStyle.secondary, emoji="↩", custom_id="restart"))
        self.add_item(discord.ui.Button(label=l["stop"], style=discord.ButtonStyle.danger, emoji="⏹", custom_id="stop"))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        cid = interaction.data["custom_id"]
        
        msg = {
            "zh": {"no_prev": "沒有上一首歌了", "no_next": "這是最後一首了", "prev": "切換至上一首", "next": "切換至下一首", "pause": "已暫停", "resume": "繼續播放"},
            "en": {"no_prev": "No previous song", "no_next": "This is the last song", "prev": "Switching to previous", "next": "Switching to next", "pause": "Paused", "resume": "Resumed"}
        }
        m = msg.get(self.lang, msg["zh"])

        if cid == "toggle_pause":
            if self.is_paused:
                self.voice_client.resume()
                new_state = False
                await interaction.response.send_message(m["resume"], ephemeral=True)
            else:
                self.voice_client.pause()
                new_state = True
                await interaction.response.send_message(m["pause"], ephemeral=True)
            
            new_view = MusicControls(self.voice_client, self.lang, new_state)
            await interaction.message.edit(view=new_view)

        elif cid == "stop":
            if self.voice_client.is_playing():
                self.voice_client.stop()
            if interaction.message: 
                await interaction.message.delete()
            if self.voice_client.is_connected(): 
                await self.voice_client.disconnect()
            
        return True

# --- 演奏指令 (中文版) ---
@bot.tree.command(name="演奏", description="召喚金鈴演奏樂章")
@app_commands.autocomplete(query=song_autocomplete)
async def tune_zh(interaction: discord.Interaction, query: str):
    if not interaction.user.voice:
        return await interaction.response.send_message(f"{GOLD_BELL} 請先進入語音頻道。", ephemeral=True)
    
    await interaction.response.defer(ephemeral=True)
    
    async def execute_play():
        voice_client = discord.utils.get(bot.voice_clients, guild=interaction.guild)
        if not voice_client: 
            voice_client = await interaction.user.voice.channel.connect()

        with yt_dlp.YoutubeDL({'format': 'bestaudio', 'quiet': True}) as ydl:
            info = ydl.extract_info(query, download=False)
            url, title, uploader, thumbnail, webpage_url = info['url'], info.get('title', 'Unknown'), info.get('uploader', 'Unknown'), info.get('thumbnail', ''), info.get('webpage_url', query)

        if voice_client.is_playing():
            voice_client.stop()

        voice_client.play(
            discord.FFmpegPCMAudio(
                url, 
                before_options='-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5', 
                options='-vn'
            ), 
            after=lambda e: bot.loop.create_task(play_next(interaction.guild_id))
        )

        embed = discord.Embed(title=f"{INSTRUMENT_IMG} 金鈴正在演奏...", color=discord.Color.blue())
        embed.add_field(name="曲名", value=title, inline=False)
        embed.add_field(name="演奏者", value=uploader, inline=True)
        embed.add_field(name="連結", value=f"[點擊跳轉]({webpage_url})", inline=True)
        embed.set_thumbnail(url=thumbnail)

        await send_as_role("‧₊˚♪ 金鈴 𝐿𝓎𝓇𝑒 𝄞₊˚⊹", interaction.channel, embed=embed, content=f"{GOLD_BELL} 金鈴輕輕一笑，享受著演奏的瞬間。", view=MusicControls(voice_client))

    q = get_queue(interaction.guild_id)
    await q.put(execute_play)
    
    voice_client = discord.utils.get(bot.voice_clients, guild=interaction.guild)
    if not voice_client or not voice_client.is_playing():
        await play_next(interaction.guild_id)
        await interaction.followup.send("🎶 開始播放！", ephemeral=True)
    else:
        await interaction.followup.send("已加入演奏佇列。", ephemeral=True)

# --- 演奏指令 (英文版) ---
@bot.tree.command(name="tune", description="Summon Lyre to perform")
@app_commands.autocomplete(query=song_autocomplete)
async def tune_en(interaction: discord.Interaction, query: str):
    if not interaction.user.voice:
        return await interaction.response.send_message(f"{GOLD_BELL} Please join a voice channel first.", ephemeral=True)
    
    await interaction.response.defer(ephemeral=True)
    
    async def execute_play():
        YDL_OPTIONS = {
            'format': 'bestaudio', 
            'quiet': True,
        }
        voice_client = discord.utils.get(bot.voice_clients, guild=interaction.guild)
        if not voice_client: 
            voice_client = await interaction.user.voice.channel.connect()

        with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
            info = ydl.extract_info(query, download=False)
            url, title, uploader, thumbnail, webpage_url = info['url'], info.get('title', 'Unknown'), info.get('uploader', 'Unknown'), info.get('thumbnail', ''), info.get('webpage_url', query)

        if voice_client.is_playing():
            voice_client.stop()

        voice_client.play(
            discord.FFmpegPCMAudio(
                url, 
                before_options='-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5', 
                options='-vn'
            ), 
            after=lambda e: bot.loop.create_task(play_next(interaction.guild_id))
        )

        embed = discord.Embed(title=f"{INSTRUMENT_IMG} Lyre is performing...", color=discord.Color.blue())
        embed.add_field(name="Title", value=title, inline=False)
        embed.add_field(name="Artist", value=uploader, inline=True)
        embed.add_field(name="Link", value=f"[Click to view]({webpage_url})", inline=True)
        embed.set_thumbnail(url=thumbnail)

        await send_as_role("‧₊˚♪ 金鈴 𝐿𝓎𝓇𝑒 𝄞₊˚⊹", interaction.channel, embed=embed, content=f"{GOLD_BELL} Lyre smiles softly, enjoying the music.", view=MusicControls(voice_client, lang="en"))

    q = get_queue(interaction.guild_id)
    await q.put(execute_play)
    
    voice_client = discord.utils.get(bot.voice_clients, guild=interaction.guild)
    if not voice_client or not voice_client.is_playing():
        await play_next(interaction.guild_id)
        await interaction.followup.send("🎶 Playing now!", ephemeral=True)
    else:
        await interaction.followup.send("Added to queue.", ephemeral=True)
# --- 音樂播放與佇列觸發（接續前段邏輯） ---
        await send_as_role(
            "‧₊˚♪ 金鈴 𝐿𝓎𝓇𝑒 𝄞₊˚⊹", 
            interaction.channel, 
            f"{GOLD_BELL} Lyre's lips curved into a small smile, enjoying the moment of performance.", 
            embed, 
            view=MusicControls(voice_client, lang="en")
        )

    if interaction.guild_id not in queues: queues[interaction.guild_id] = asyncio.Queue()
    await queues[interaction.guild_id].put(execute_play)
    
    voice_client = discord.utils.get(bot.voice_clients, guild=interaction.guild)
    if not voice_client or not voice_client.is_playing():
        await play_next(interaction.guild_id)
    else:
        await interaction.followup.send("Added to performance queue.", ephemeral=True)

# --- 境界指令 ---
async def handle_realm(interaction: discord.Interaction, target_user: Optional[discord.Member], lang: str):
    user = target_user or interaction.user
    u = get_user(user.id)
    
    current_exp = u.get('exp') or 0 
    current_level = u.get('level') or 1
    req = calculate_required_exp(current_level)
    
    bar_length = 10
    filled = int((current_exp / req) * bar_length)
    bar = f"[{'✦' * filled}{'✧' * (bar_length - filled)}] {current_exp}/{req}"
    
    if lang == "zh":
        title = f"{user.display_name} 的修煉紀錄"
        verse_field_name = f"{INK[1]} 詞章"
        verse_val = get_verse_name(current_level, lang="zh")
        progress_name = f"{INK[2]} 修煉進度 ({int((current_exp / req) * 100)}%)"
        feather_label = f"{INK[3]} 羽毛"
        soma_label = f"{INK[4]} 靈果"
    else:
        title = f"{user.display_name}'s Cultivation Record"
        verse_field_name = f"{INK[1]} Verse"
        verse_val = get_verse_name(current_level, lang="en")
        progress_name = f"{INK[2]} Progress ({int((current_exp / req) * 100)}%)"
        feather_label = f"{INK[3]} Feathers"
        soma_label = f"{INK[4]} Soma"

    embed = discord.Embed(title=title, color=0x236950)
    if user.display_avatar:
        embed.set_thumbnail(url=user.display_avatar.url)
        
    embed.add_field(name=verse_field_name, value=verse_val, inline=False)
    embed.add_field(name=progress_name, value=bar, inline=False)
    embed.add_field(name=feather_label, value=str(u.get('feathers') or 0) + " " + FEATHER_ICON, inline=True)
    embed.add_field(name=soma_label, value=str(u.get('soma') or 0) + " " + SOMA_ICON, inline=True)
    
    embed.set_footer(text=f"UTC+8 | {time.strftime('%Y-%m-%d %H:%M')}")
    await interaction.response.send_message(embed=embed)

# --- 指令註冊 ---
@bot.tree.command(name="境界", description="查看你的或他人的修煉紀錄")
@app_commands.describe(member="要查詢的對象 (若不填則默認為自己)")
async def realm_zh(interaction: discord.Interaction, member: Optional[discord.Member] = None):
    await handle_realm(interaction, member, lang="zh")

@bot.tree.command(name="realm", description="View your or another user's cultivation record")
@app_commands.describe(member="The user to view (optional, defaults to yourself)")
async def realm_en(interaction: discord.Interaction, member: Optional[discord.Member] = None):
    await handle_realm(interaction, member, lang="en")

# --- 冷卻與靈性互動共用邏輯 ---
async def check_and_update_cooldown(user_id, column, minutes):
    response = supabase.table("users").select(column).eq("id", user_id).execute()
    now = datetime.now(timezone.utc)
    
    if response.data and response.data[0].get(column):
        try:
            last_time = datetime.fromisoformat(response.data[0][column].replace("Z", "+00:00"))
            if now - last_time < timedelta(minutes=minutes):
                remaining = int(minutes - (now - last_time).total_seconds() / 60)
                return False, remaining
        except:
            pass

    supabase.table("users").update({column: now.isoformat()}).eq("id", user_id).execute()
    return True, 0

async def handle_rinse(interaction: discord.Interaction, lang: str):
    user_id = str(interaction.user.id)
    is_ready, rem = await check_and_update_cooldown(user_id, "last_rinse", 15)
    
    if not is_ready:
        msg = f"靈泉尚在平復中，請於 {rem} 分鐘後再來。 / The sacred spring is still settling, please return in {rem} minutes."
        return await interaction.response.send_message(msg, ephemeral=True)
    
    res = supabase.table("users").select("soma").eq("id", user_id).execute()
    current = res.data[0]['soma'] if res.data else 0
    supabase.table("users").update({"soma": current + 200}).eq("id", user_id).execute()
    
    await interaction.response.send_message(
        f"你於靈泉深處漱玉淨魂，自漣漪間拾獲 200 {SOMA_ICON}。" if lang == "zh"
        else f"You rinse your spirit within the sacred spring, retrieving 200 {SOMA_ICON} from the ripples."
    )

async def handle_plumage(interaction: discord.Interaction, lang: str):
    user_id = str(interaction.user.id)
    is_ready, rem = await check_and_update_cooldown(user_id, "last_plumage", 30)
    
    if not is_ready:
        msg = f"羽毛尚未飄落，請於 {rem} 分鐘後再來。 / No feathers drifting yet, please return in {rem} minutes."
        return await interaction.response.send_message(msg, ephemeral=True)
    
    amount = random.randint(25, 100)
    res = supabase.table("users").select("feathers").eq("id", user_id).execute()
    current = res.data[0]['feathers'] if res.data else 0
    supabase.table("users").update({"feathers": current + amount}).eq("id", user_id).execute()
    
    await interaction.response.send_message(
        f"你拾起自高天飄落的羽毛，微光在掌心如流金般流轉，獲得 {amount} {FEATHER_ICON}。" if lang == "zh"
        else f"You catch a stray feather drifting from the heavens, as aventurine light ripples across your palm. Gained {amount} {FEATHER_ICON}."
    )

async def handle_brew(interaction: discord.Interaction, lang: str):
    user_id = str(interaction.user.id)
    is_ready, rem = await check_and_update_cooldown(user_id, "last_brew_at", 120)
    
    if not is_ready:
        msg = f"茶香未成，請於 {rem} 分鐘後再來。 / The tea is not ready, please return in {rem} minutes."
        return await interaction.response.send_message(msg, ephemeral=True)
    
    amount = random.randint(300, 600)
    res = supabase.table("users").select("feathers").eq("id", user_id).execute()
    current = res.data[0]['feathers'] if res.data else 0
    supabase.table("users").update({"feathers": current + amount}).eq("id", user_id).execute()
    
    await interaction.response.send_message(
        f"燃修為為薪以煎香茗，清芬四溢，獲得 {amount} {FEATHER_ICON}。" if lang == "zh"
        else f"Kindling your cultivation to brew the sacred leaves, the rising fragrance distills into {amount} {FEATHER_ICON}."
    )

# --- 漱玉、拾羽、煎茶指令註冊 ---
@bot.tree.command(name="漱玉", description="於靈泉深處漱玉淨魂")
async def rinse_zh(i: discord.Interaction): await handle_rinse(i, "zh")

@bot.tree.command(name="rinse", description="Rinse your spirit in the sacred spring")
async def rinse_en(i: discord.Interaction): await handle_rinse(i, "en")

@bot.tree.command(name="拾羽", description="拾起高天飄落的羽毛")
async def plumage_zh(i: discord.Interaction): await handle_plumage(i, "zh")

@bot.tree.command(name="plumage", description="Catch a feather drifting from the heavens")
async def plumage_en(i: discord.Interaction): await handle_plumage(i, "en")

@bot.tree.command(name="煎茶", description="燃修為為薪以煎香茗")
async def brew_zh(i: discord.Interaction): await handle_brew(i, "zh")

@bot.tree.command(name="brew", description="Kindle your cultivation to brew sacred leaves")
async def brew_en(i: discord.Interaction): await handle_brew(i, "en")

# --- 叩問指令 ---
@bot.tree.command(name="叩問", description="向白靈鵠詢問箴言")
async def inquire_zh(interaction: discord.Interaction):
    proverbs = ["萬物靜觀皆自得", "心如止水，方能映照星辰", "羽落無聲，靈動有跡", "浮生若夢，修行為真"]
    await interaction.response.send_message(f"白靈鵠拂羽低語：{random.choice(proverbs)}")

@bot.tree.command(name="inquire", description="Ask Aetherwyn for a proverb")
async def inquire_en(interaction: discord.Interaction):
    proverbs = [
        "All things are seen clearly when the mind is still.",
        "Like water in a still pool, the heart reflects the stars.",
        "Feathers fall silently, yet the spirit leaves its mark.",
        "Life is but a dream; cultivation is the truth."
    ]
    await interaction.response.send_message(f"Aetherwyn rustles its plumage, whispering into the wind: {random.choice(proverbs)}")

# --- 守望核心與指令 ---
async def process_vigil(interaction: discord.Interaction, lang="zh"):
    u = get_user(interaction.user.id)
    today = datetime.now().strftime("%Y-%m-%d")
    
    if u.get('last_vigil') == today:
        if lang == "zh":
            return await interaction.response.send_message("白靈鵠今日已守望過你了，靜待明日晨曦吧。", ephemeral=True)
        else:
            return await interaction.response.send_message("Aetherwyn has already watched over you today; wait for the next dawn.", ephemeral=True)
    
    new_feathers = (u.get('feathers') or 0) + 50
    new_soma = (u.get('soma') or 0) + 1000
    update_user(interaction.user.id, {"feathers": new_feathers, "soma": new_soma, "last_vigil": today})
    
    if lang == "zh":
        await interaction.response.send_message(f"白靈鵠居高注視著你，今日的守望已銘刻於晨曦之中。獲得 50 {FEATHER_ICON} , 1000 {SOMA_ICON}。")
    else:
        await interaction.response.send_message(f"Aetherwyn gazes down upon you; today’s vigil has been woven into the dawn. Gained 50 {FEATHER_ICON}, 1000 {SOMA_ICON}.")

@bot.tree.command(name="守望", description="白靈鵠的每日庇護")
async def vigil_zh(interaction: discord.Interaction):
    await process_vigil(interaction, lang="zh")

@bot.tree.command(name="vigil", description="Aetherwyn's daily blessing")
async def vigil_en(interaction: discord.Interaction):
    await process_vigil(interaction, lang="en")

# --- 品茗 (Handle Sip) 核心與指令 ---
async def handle_sip(interaction: discord.Interaction, lang: str):
    user_id = str(interaction.user.id)
    
    response = supabase.table("users").select("last_brew_at, feathers").eq("id", user_id).execute()
    
    if not response.data:
        return await interaction.response.send_message("尚未煎茶，無法品茗。 / No tea brewed yet.", ephemeral=True)

    user_data = response.data[0]
    last_brew_at = user_data.get("last_brew_at")
    current = user_data.get("feathers", 0)

    if not last_brew_at:
        return await interaction.response.send_message("尚未煎茶，無法品茗。 / No tea brewed yet.", ephemeral=True)

    last_brew = datetime.fromisoformat(last_brew_at.replace("Z", "+00:00"))
    if datetime.now(timezone.utc) - last_brew > timedelta(hours=6):
        return await interaction.response.send_message("茶氣已散，請先煎茶。 / Tea has gone cold.", ephemeral=True)  

    data = [
        {"id": "god", "change": 0.5, "zh": f"「此茶中竟有古神殘影... 罷了，這神{FEATHER_ICON}便與你重塑靈源。」(1.5倍餘額)", "en": f"「A flicker of ancient divinity lingers within the dregs... take this essence, let it reforge your very soul.」 (1.5x)"},
        {"id": "spring", "change": 1000, "zh": f"「泉水隨茶香而沸，看來你已觸及靈泉最深處的秘藏。」(+1000 {FEATHER_ICON})", "en": f"「The spring waters pulse in rhythm with the tea; you have touched the hidden veins of Elysia.」 (+1000 {FEATHER_ICON})"},
        {"id": "rain", "change": 200, "zh": f"「茶氣舒緩經脈，你拾得幾片落下的靈羽。」(+200 {FEATHER_ICON})", "en": f"「As the mist rises, so do the lost feathers of the heavens—take what the wind has returned to you.」 (+200 {FEATHER_ICON})"},
        {"id": "dew", "change": 50, "zh": f"「茶香清冽，如林間露水點滴入懷，神思清明。」(+50 {FEATHER_ICON})", "en": f"「The brew runs clear, like dew upon a fern at dawn; your spirit finds a fleeting, crystalline clarity.」 (+50 {FEATHER_ICON})"},
        {"id": "calm", "change": 0, "zh": f"「茶氣平平，心如止水，這份寧靜便是今日的最佳回贈。」(平靜)", "en": f"「The tea is tranquil, and so is the mind; in this stillness, the silence is your only—and greatest—reward.」"},
        {"id": "dust", "change": -5, "zh": f"「茶中有雜念，擾了靜心，需散去些許靈羽來淨化。」(-5 {FEATHER_ICON})", "en": f"「Mortal thoughts stir like dust in the sunbeams; you must cast off a few feathers to quiet the inner clamor.」 (-5 {FEATHER_ICON})"},
        {"id": "fog", "change": -50, "zh": f"「你的靈台似有灰霧遮掩，需以靈羽洗滌，方可重見靈識。」(-50 {FEATHER_ICON})", "en": f"「Grey veils drift across your inner sight... yield a portion of your light, that you may wash away the shadows.」 (-50 {FEATHER_ICON})"},
        {"id": "erosion", "change": -100, "zh": f"「這杯茶，倒出了你心中的執念，若不捨棄，靈力便會外洩。」(-100 {FEATHER_ICON})", "en": f"「The bitter aftertaste reveals a knot of lingering attachment; release the weight, or watch your essence seep away.」 (-100 {FEATHER_ICON})"},
        {"id": "void", "change": -500, "zh": f"「茶已涼，道心卻亂了。去吧，將這份損失視作對修行的警示。」(-500 {FEATHER_ICON})", "en": f"「The tea has turned cold, and so has the clarity of your path. Heed this discord as a grave warning for your spirit.」 (-500 {FEATHER_ICON})"}
    ]
    
    choice = random.choices(data, weights=[0.005, 0.5, 5, 26.735, 50, 15.005, 2.5, 0.75, 0.005], k=1)[0]
    
    if choice['id'] == "god":
        new_val = int(current * 1.5)
    else:
        new_val = max(0, current + int(choice['change']))
    
    supabase.table("users").update({"feathers": new_val}).eq("id", user_id).execute()
    
    if lang == "zh":
        embed = discord.Embed(title="!品茗", description=f"白靈鵠與你於案前對弈，茶香裊裊，杯中自成一番天地至理。\n\n{choice['zh']}", color=discord.Color.purple())
    else:
        embed = discord.Embed(title="!sip", description=f"Aetherwyn plays a game of wits with you across the board; the floating aroma carries the deepest mysteries of the realm.\n\n{choice['en']}", color=discord.Color.purple())
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="茗茶", description="白靈鵠與你品茗對弈")
async def sip_zh(interaction: discord.Interaction):
    await handle_sip(interaction, lang="zh")

@bot.tree.command(name="sip", description="Aetherwyn plays a game of wits with you")
async def sip_en(interaction: discord.Interaction):
    await handle_sip(interaction, lang="en")

# --- 語音朗讀核心與指令 ---
async def play_tts_in_channel(interaction, text, lang="zh-tw"):
    if not interaction.user.voice:
        return await interaction.followup.send("請先加入語音頻道，銀鈴才能為您朗讀。", ephemeral=True)
    
    current_time = time.strftime("%Y-%m-%d %H:%M", time.localtime())
    user_name = interaction.user.display_name
    
    if lang == "zh-tw":
        full_text = f"銀鈴正在閱讀來自 {user_name} 於 {current_time} 的訊息：{text}"
    else:
        full_text = f"Lyte is reading a message from {user_name} at {current_time}: {text}"
    
    tts = gTTS(text=full_text, lang="zh" if "zh" in lang else "en")
    tts.save("tts.mp3")
    
    channel = interaction.user.voice.channel
    voice_client = discord.utils.get(bot.voice_clients, guild=interaction.guild)
    if voice_client and voice_client.channel != channel:
        await voice_client.move_to(channel)
    elif not voice_client:
        voice_client = await channel.connect()
    
    voice_client.play(discord.FFmpegPCMAudio("tts.mp3"))
    
    webhook_content = "銀鈴已就位。" if "zh" in lang else "Lyte is ready to relay your message."
    embed = discord.Embed(description=f"**{full_text}**", color=0xC0C0C0)
    await send_as_role("˗ˏˋ꒰ 銀鈴 ✉︎ 𝐿𝓎𝓉𝑒 ꒱ˎˊ˗", interaction.channel, webhook_content, embed)

@bot.tree.command(name="朗讀", description="請銀鈴以中文朗讀文字")
async def read_zh(interaction: discord.Interaction, text: str):
    await interaction.response.defer(ephemeral=True)
    await play_tts_in_channel(interaction, text, lang="zh-tw")
    await interaction.followup.send("銀鈴已進入頻道準備朗讀。", ephemeral=True)

@bot.tree.command(name="read", description="Have Lyte read text aloud in English")
async def read_en(interaction: discord.Interaction, text: str):
    await interaction.response.defer(ephemeral=True)
    await play_tts_in_channel(interaction, text, lang="en")
    await interaction.followup.send("Lyte has entered the voice channel and is ready to read.", ephemeral=True)
 # --- 測試指令 ---

@bot.tree.command(name="test_join", description="測試歡迎訊息卡片")
@app_commands.checks.has_permissions(administrator=True)
async def test_join(interaction: discord.Interaction):
    # 1. 搶先延遲回應，避免 3 秒超時或重複回應報錯
    await interaction.response.defer(ephemeral=True)
    
    try:
        # 2. 執行歡迎邏輯
        await on_member_join(interaction.user)
        # 3. 使用 followup 發送測試成功提示
        await interaction.followup.send("✅ 歡迎訊息測試已觸發完成。", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ 測試時發生錯誤: {e}", ephemeral=True)

@bot.tree.command(name="test_leave", description="測試離別訊息卡片")
@app_commands.checks.has_permissions(administrator=True)
async def test_leave(interaction: discord.Interaction):
    # 1. 搶先延遲回應
    await interaction.response.defer(ephemeral=True)
    
    try:
        # 2. 執行離別邏輯
        await on_member_remove(interaction.user)
        # 3. 使用 followup 發送測試成功提示
        await interaction.followup.send("✅ 離別訊息測試已觸發完成。", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ 測試時發生錯誤: {e}", ephemeral=True)
# --- 調整設定區 ---
SPAM_LIMIT = 10      # 允許在時間窗口內發送的訊息數量
TIME_WINDOW = 3      # 時間窗口 (秒)，例如 3 秒內發送超過 10 則訊息才判定為刷頻
# ------------------


@bot.tree.command(name="setup_honeypot", description="設置星津蓮汐的防禦結界")
@app_commands.checks.has_permissions(administrator=True)
async def setup_honeypot(interaction: discord.Interaction):
    honeypot_channel = bot.get_channel(1527728585547190493)
    
    if not honeypot_channel:
        return await interaction.response.send_message("找不到星津蓮汐的防禦結界入口。", ephemeral=True)

    embed = discord.Embed(
        title="✨ 星津蓮汐・防禦結界已啟動 / The Barrier of Elysia is Active",
        color=0x4b0082 # 深邃的星空色
    )
    
    embed.description = (
        "**此處乃星津蓮汐的禁忌漣漪，凡觸動者必受天道懲戒。**\n"
        "**This is the forbidden ripple of Elysia; those who disturb it shall face the judgment of the heavens.**\n\n"
        "🛡️ **結界法則 / The Law of the Barrier**\n"
        "星光在此駐足，靈魂需保持純粹。任何試圖突破結界、違規發聲的異動，將瞬間被星塵抹去。\n"
        "Starlight lingers here, and souls must remain pure. Any intrusion or unauthorized sound will be instantly erased by stardust.\n\n"
        "📜 **懲戒說明 / The Consequence**\n"
        "• 任何發言均視為入侵，將觸發自動放逐 / Any message is treated as an intrusion, triggering an immediate ban.\n"
        "• 此帳號的所有足跡將被蓮汐之水徹底洗淨 / All traces of this account will be washed away by the tides of the lotus.\n"
        "• 管理者的目光已鎖定此處 / The Gaze of the Guardians is locked upon this realm."
    )
    
    embed.set_image(url="https://cdn.discordapp.com/attachments/1526215616485392475/1527730295846076436/ban.gif?ex=6a5bb936&is=6a5a67b6&hm=681173b4418079634aa7e77da9a525feb8710280080538c1629a90ab4aad20bb")
    
    await honeypot_channel.send(embed=embed)
    await interaction.response.send_message("結界告示已發送。", ephemeral=True)
@bot.tree.command(name="reset_honeypot", description="清除蜜罐頻道訊息並重新發送警告")
@app_commands.checks.has_permissions(administrator=True)
async def reset_honeypot(interaction: discord.Interaction):
    # 1. 宣告處理中 (這是唯一一次對 response 的回應)
    await interaction.response.defer(ephemeral=True)
    
    honeypot_channel = bot.get_channel(1527728585547190493)
    if not honeypot_channel:
        # 使用 followup 進行錯誤回報
        return await interaction.followup.send("找不到結界入口。", ephemeral=True)

    # 2. 清除舊訊息
    await honeypot_channel.purge(limit=10)
    
    # 3. 建立資訊卡
    embed = discord.Embed(
        title="🍯 星津蓮汐・防禦結界已啟動 / The Barrier of Elysia is Active",
        color=0x4b0082
    )
    embed.description = (
        "**此處乃星津蓮汐的禁忌漣漪，凡觸動者必受天道懲戒。**\n"
        "**This is the forbidden ripple of Elysia; those who disturb it shall face the judgment of the heavens.**\n\n"
        "🛡️ **結界法則 / The Law of the Barrier**\n"
        "星光在此駐足，靈魂需保持純粹。任何試圖突破結界、違規發聲的異動，將瞬間被星塵抹去。\n"
        "Starlight lingers here, and souls must remain pure. Any intrusion or unauthorized sound will be instantly erased by stardust.\n\n"
        "📜 **懲戒說明 / The Consequence**\n"
        "• 任何發言均視為入侵，將觸發自動放逐 / Any message is treated as an intrusion, triggering an immediate ban.\n"
        "• 此帳號的所有足跡將被蓮汐之水徹底洗淨 / All traces of this account will be washed away by the tides of the lotus.\n"
        "• 管理者的目光已鎖定此處 / The Gaze of the Guardians is locked upon this realm."
    )
    embed.set_image(url="https://cdn.discordapp.com/attachments/1526215616485392475/1527730295846076436/ban.gif?ex=6a5bb936&is=6a5a67b6&hm=681173b4418079634aa7e77da9a525feb8710280080538c1629a90ab4aad20bb")
    
    # 4. 發送並通知完成
    await honeypot_channel.send(embed=embed)
    await interaction.followup.send("結界已清理完畢，警告資訊卡已重新部署。", ephemeral=True)
    # 3. 調整後的刷頻偵測
    await handle_spam_detection(message)

    await bot.process_commands(message)

async def handle_spam_detection(message):
    now = time.time()
    user_history = user_messages[message.author.id]
    user_history.append(now)
    
    # 檢查是否超出次數限制
    if len(user_history) >= SPAM_LIMIT:
        # 如果最後一則訊息與第 SPAM_LIMIT 則之前的時間差小於 TIME_WINDOW
        if now - user_history[0] < TIME_WINDOW:
            await message.channel.send(f"⚠️ {message.author.mention} 發言速度過快，請稍作休息。")
            # 這裡可以選擇刪除訊息或是禁言
            # await message.delete() 
            user_history.clear() # 清空歷史紀錄，防止持續觸發

# 假設你有一個資料庫檔案叫 database.json
def update_user_balance(user_id, feathers_change, fruits_change):
    # 1. 讀取現有資料
    try:
        with open("database.json", "r") as f:
            data = json.load(f)
    except:
        data = {}

    # 2. 修改數值
    uid = str(user_id)
    if uid not in data:
        data[uid] = {"feathers": 0, "fruits": 0}
    
    # 執行增減 (add_balance 就傳入正數，remove_balance 就傳入負數)
    data[uid]["feathers"] = max(0, data[uid].get("feathers", 0) + feathers_change)
    data[uid]["fruits"] = max(0, data[uid].get("fruits", 0) + fruits_change)

    # 3. 寫入存檔
    with open("database.json", "w") as f:
        json.dump(data, f)

# --- 你的指令修正 ---
# 增加餘額 (Add Balance)
@bot.tree.command(name="add_balance", description="增加指定用戶的靈羽與靈果 / Add feathers and fruits to a user")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(user="目標用戶 / Target User", feathers="要增加的數量 / Amount to add", fruits="要增加的數量 / Amount to add")
async def add_balance(interaction: discord.Interaction, user: discord.Member, feathers: int, fruits: int):
    response = supabase.table("users").select("feathers, soma").eq("id", str(user.id)).execute()
    
    if response.data:
        current = response.data[0]
        supabase.table("users").update({
            "feathers": current['feathers'] + feathers,
            "soma": current['soma'] + fruits
        }).eq("id", str(user.id)).execute()
    else:
        supabase.table("users").insert({"id": str(user.id), "feathers": feathers, "soma": fruits}).execute()

    await interaction.response.send_message(
        f"✅ 已將 {feathers} 靈羽與 {fruits} 靈果注入 {user.mention} 的靈魂中。\n"
        f"({feathers} feathers and {fruits} soma have been infused into {user.mention}'s soul.)", 
        ephemeral=True
    )

# 扣除餘額 (Remove Balance)
@bot.tree.command(name="remove_balance", description="扣除指定用戶的靈羽與靈果 / Remove feathers and fruits from a user")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(user="目標用戶 / Target User", feathers="要扣除的數量 / Amount to remove", fruits="要扣除的數量 / Amount to remove")
async def remove_balance(interaction: discord.Interaction, user: discord.Member, feathers: int, fruits: int):
    response = supabase.table("users").select("feathers, soma").eq("id", str(user.id)).execute()
    
    if response.data:
        current = response.data[0]
        # 使用 max(0, ...) 確保數值不會變成負數
        supabase.table("users").update({
            "feathers": max(0, current['feathers'] - feathers),
            "soma": max(0, current['soma'] - fruits)
        }).eq("id", str(user.id)).execute()

    await interaction.response.send_message(
        f"✨ 已從 {user.mention} 的靈魂中收回 {feathers} 靈羽與 {fruits} 靈果。\n"
        f"({feathers} feathers and {fruits} soma have been reclaimed from {user.mention}'s soul.)", 
        ephemeral=True
    )
@bot.tree.command(name="nuke", description="將當前頻道瞬間重置 (僅限管理員)")
@app_commands.checks.has_permissions(administrator=True)
async def nuke(interaction: discord.Interaction):
    # 宣告處理中
    await interaction.response.defer(ephemeral=True)
    
    channel = interaction.channel
    # 記錄原本的頻道位置
    position = channel.position
    
    # 複製頻道
    new_channel = await channel.clone(reason="星津蓮汐・防禦結界重置")
    # 將新頻道移到原位
    await new_channel.edit(position=position)
    
    # 刪除舊頻道
    await channel.delete(reason="星津蓮汐・防禦結界重置")
# 讀取或建立設定檔
class TicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    def get_mentions(self, role_ids):
        return " ".join([f"<@&{rid}>" for rid in role_ids])

    # 修改 create_ticket：改為從資料庫獲取角色設定
    async def create_ticket(self, interaction: discord.Interaction, ticket_type: str):
        guild = interaction.guild
        
        # 1. 從 Supabase 獲取設定 (取代原先寫死在檔案中的 roles)
        settings = get_guild_settings(guild.id)
        # 假設你的 Supabase 表中有個欄位叫 'support_role_ids' (存成字串如 "123,456")
        raw_roles = settings.get("support_role_ids", "") 
        roles = [int(r) for r in raw_roles.split(",")] if raw_roles else []

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            interaction.user: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
        }
        
        for role_id in roles:
            role = guild.get_role(role_id)
            if role:
                overwrites[role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

        channel = await guild.create_text_channel(
            name=f"{ticket_type}-{interaction.user.name}",
            overwrites=overwrites
        )
        
        mentions = self.get_mentions(roles)
        await interaction.response.send_message(f"✅ 已為您開啟諮詢單：{channel.mention}", ephemeral=True)
        
        welcome_msg = (
            f"{interaction.user.mention} 歡迎來到 {ticket_type} 諮詢單。\n"
            f"{mentions} 會盡快協助您！\n請詳述您的需求。"
        )
        # 確保 send_as_role 函數已正確定義並能運作
        await send_as_role("黑貓夫妻 𝐵𝓁𝒶𝒸𝓀𝒞𝒶𝓉 𝒞𝑜𝓊𝓅𝓁𝑒", channel, welcome_msg)

class ServicePanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="① 普通客服 / General Support", style=discord.ButtonStyle.secondary, custom_id="btn_normal")
    async def btn_normal(self, interaction: discord.Interaction, button: discord.ui.Button):
        roles = [1526534796447060088, 1526534897622056980, 1526535031009574942]
        content = f"{interaction.user.mention} 請問如何能解決您的煩憂？\nHow can we help resolve your concerns?"
        gif = "https://cdn.discordapp.com/attachments/1526215549720330300/1528318995005046926/redtea.gif"
        # 加上 is_ticket=True 觸發開單邏輯
        await send_as_role("☀︎˚₊溫溫 𑄝࿔ 𝒲𝒾𝓃𝓈𝑜𝓃⋆˚࿔", interaction, "普通客服 / General Support", content, gif, roles, is_ticket=True)

    @discord.ui.button(label="② 合作 / Partnership", style=discord.ButtonStyle.secondary, custom_id="btn_collab")
    async def btn_collab(self, interaction: discord.Interaction, button: discord.ui.Button):
        roles = [1526534796447060088, 1526534897622056980, 1526537048645959731]
        content = (
            f"{interaction.user.mention} 如果是初次盟約，請分開提供：\n"
            f"If this is your first alliance, please provide separately:\n"
            f"{INK[1]} 貴群宣文 / Your server promotion\n"
            f"{INK[2]} 貴群名字 / Your server name\n"
            f"{INK[3]} 貴群規章(如有) / Your server rules (if any)\n"
            f"{INK[4]} 貴群白單(如有) / Your server white-list / bio (if any)\n"
            f"{INK[5]} 填寫好的本群白單 / Completed white-list / bio for our server\n\n"
            f"若已是盟約，請寫出你的疑慮：\n"
            f"If you are already allies, please state your concerns:\n"
            f"{INK[1]} 解除合作 / Terminate partnership\n"
            f"{INK[2]} 更換代表 / 永久連結 / 宣文 / Update representative / permanent invite / promotion\n"
            f"{INK[3]} 其他 / Others"
        )
        gif = "https://cdn.discordapp.com/attachments/1526215549720330300/1528319035303919706/greentea.gif"
        await send_as_role("❨˚˖茶茶 ꕤ࿔ 𝒞𝒽𝒶𝓇𝓁𝒾𝚎 ༘⋆⊹", interaction, "合作 / Partnership", content, gif, roles, is_ticket=True)

    @discord.ui.button(label="③ 建議 / Suggestions", style=discord.ButtonStyle.secondary, custom_id="btn_suggest")
    async def btn_suggest(self, interaction: discord.Interaction, button: discord.ui.Button):
        roles = [1526534796447060088, 1526534897622056980, 1526535031009574942]
        content = f"{interaction.user.mention} 感謝您抽空參與改進此地。請自由的發表你的高見喔！\nThank you for taking the time to help improve this place. Please feel free to share your insights!"
        gif = "https://cdn.discordapp.com/attachments/1526215549720330300/1528319278040748042/sweets.gif"
        await send_as_role("☀︎˚₊溫溫 𑄝࿔ 𝒲𝒾𝓃𝓈𝑜𝓃⋆˚࿔", interaction, "建議 / Suggestions", content, gif, roles, is_ticket=True)

    @discord.ui.button(label="④ 舉報 / Report", style=discord.ButtonStyle.secondary, custom_id="btn_report")
    async def btn_report(self, interaction: discord.Interaction, button: discord.ui.Button):
        roles = [1526534796447060088, 1526534897622056980]
        content = f"{interaction.user.mention} 請問哪位成員擾亂風氣？請列出對應規則。\nWhich member is disrupting the atmosphere? Please list the corresponding rules."
        gif = "https://cdn.discordapp.com/attachments/1526215549720330300/1528319153604263956/coffee.gif"
        await send_as_role("❨˚˖茶茶 ꕤ࿔ 𝒞𝒽𝒶𝓇𝓁𝒾𝚎 ༘⋆⊹", interaction, "舉報 / Report", content, gif, roles, is_ticket=True)

@bot.tree.command(name="setup_ticket_panel", description="發送客服資訊卡")
@app_commands.checks.has_permissions(administrator=True)
async def setup_ticket_panel(interaction: discord.Interaction):
    # 準備 Embed
    embed = discord.Embed(
        title="歡迎使用客服單系統 / Welcome to the Ticket System",
        description=(
            "黑貓茶茶溫溫夫妻非常樂意為你服務！\n"
            "Charlie and Winston, the black cat couple are happy to serve you!\n\n"
            "請按需要的服務開啟相應的單喔。\nPlease open a ticket for the service you need.\n\n"
            f"{INK[1]} 普通（領取獎勵、查詢、不知道要選哪一個都可以選這個）\n"
            f"{INK[1]} General (For rewards, inquiries, or if you're unsure which to choose)\n\n"
            f"{INK[2]} 合作（申請合作、合作相關的事務都選這個喔）\n"
            f"{INK[2]} Partnership (For partnership applications and related matters)\n\n"
            f"{INK[3]} 建議（有什麼想新增都可以點這個提議，成功可以獲得特殊獎勵？）\n"
            f"{INK[3]} . Suggestions (Share your ideas; you might get a special reward if implemented!)\n\n"
            f"{INK[4]} 舉報（匿名舉報不正之風，不用怕溫溫、茶茶、和瑞恩會保護你的！）\n"
            f"{INK[4]}  Report (Report misconduct anonymously. Don't worry, Winston, Charlie, and Ryan will protect you!)"
        ),
        color=discord.Color.purple()
    )

    # 先回覆互動，告知正在處理（避免互動超時）
    await interaction.response.defer(ephemeral=True)

# 執行發送邏輯
    await send_as_role(
        role_name="黑貓夫妻 𝐵𝓁𝒶𝒸𝓀𝒞𝒶𝓉 𝒞𝑜𝓊𝓅𝓁𝑒", 
        interaction=interaction,
        category="客服中心 / Support",
        content="",
        gif="https://cdn.discordapp.com/attachments/1526215549720330300/1528319153604263956/coffee.gif",
        roles=[1526534796447060088],
        view=ServicePanelView(),
        embed=embed
    )
    # 這裡的 await 前面請確保是 4 個空格，且這行與上面的 await send_as_role 是完全平行的
    await interaction.followup.send("✅ 資訊卡已發送。", ephemeral=True)
# --- 客服單內部的控制按鈕面板 ---
class TicketControlsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None) # 持久化按鈕，重啟依然有效

    @discord.ui.button(label="🔒 關閉客服單 / Close Ticket", style=discord.ButtonStyle.danger, custom_id="btn_close_ticket")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 點擊關閉後，彈出 ConfirmActionView 讓用戶確認
        await interaction.response.send_message(
            "⚠️ 您確定要關閉此客服單嗎？ / Are you sure you want to close this ticket?",
            view=ConfirmActionView(),
            ephemeral=True
        )
class ConfirmActionView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    @discord.ui.button(label="確認關閉 Confirm Closing", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 這裡彈出管理員的進階選項
        await interaction.response.edit_message(
            content="客服單已關閉。請守護者選擇後續處理方式：/n The ticket is closed. Guardians please choose from the following choices:", 
            view=AdminActionView()
        )

class AdminActionView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.button(label="🔄 重新開啟 Reopen", style=discord.ButtonStyle.success)
    async def reopen(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 重新設定權限，讓使用者可以再次發言
        await interaction.channel.edit(topic="客服單已重新開啟")
        await interaction.response.send_message("客服單已重新開啟。")
    @discord.ui.button(label="🗑️ 直接刪除 Delete directly", style=discord.ButtonStyle.danger)
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 為了安全，建議刪除前也存檔，或者維持你原本的直接刪除
        await interaction.channel.delete()

    @discord.ui.button(label="💾 存檔後刪除 Delete after transcript", style=discord.ButtonStyle.primary)
    async def archive(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 1. 匯出聊天紀錄為 HTML
        transcript = await chat_exporter.export(interaction.channel)
        
        # 2. 準備檔案
        transcript_file = discord.File(
            io.BytesIO(transcript.encode()), 
            filename=f"transcript-{interaction.channel.name}.html"
        )
        
        # 3. 發送到紀錄頻道
        archive_channel = interaction.client.get_channel(1527913335276965909)
        if archive_channel:
            await archive_channel.send(
                f"📄 **客服單存檔 Ticket Transcript：{interaction.channel.name}**\n處理人 Related guardian: {interaction.user.mention}", 
                file=transcript_file
            )
        
        # 4. 最後才刪除頻道
        await interaction.channel.delete()
# 輔助：查詢資料庫是否已經存在該綁定
async def is_role_linked(message_id, emoji, role_id):
    res = supabase.table("reaction_roles").select("*").eq("message_id", str(message_id)).eq("emoji", emoji).eq("role_id", role_id).execute()
    return len(res.data) > 0

@bot.tree.command(name="add_reaction_and_react", description="為現有訊息掛上反應並綁定角色")
@app_commands.checks.has_permissions(administrator=True)
async def add_reaction_and_react(interaction: discord.Interaction, message_id: str, emoji: str, role: discord.Role):
    # 1. 取得訊息並掛反應
    try:
        msg = await interaction.channel.fetch_message(int(message_id))
        await msg.add_reaction(emoji)
    except Exception as e:
        return await interaction.response.send_message(f"❌ 發生錯誤: {e}", ephemeral=True)

    # 2. 存入 Supabase
    if not await is_role_linked(message_id, emoji, role.id):
        supabase.table("reaction_roles").insert({
            "message_id": str(message_id),
            "emoji": emoji,
            "role_id": role.id,
            "guild_id": str(interaction.guild.id)
        }).execute()
        await interaction.response.send_message(f"✅ 已綁定 {emoji} 與 {role.mention}。", ephemeral=True)
    else:
        await interaction.response.send_message(f"⚠️ 該綁定已存在。", ephemeral=True)

@bot.tree.command(name="remove_single_reaction", description="移除綁定並自動收回反應")
@app_commands.checks.has_permissions(administrator=True)
async def remove_single_reaction(interaction: discord.Interaction, message_id: str, emoji: str, role: discord.Role):
    # 刪除資料庫紀錄
    res = supabase.table("reaction_roles").delete().eq("message_id", str(message_id)).eq("emoji", emoji).eq("role_id", role.id).execute()
    
    # 檢查該 Emoji 是否還有其他角色綁定，若無則移除機器人反應
    remaining = supabase.table("reaction_roles").select("*").eq("message_id", str(message_id)).eq("emoji", emoji).execute()
    if not remaining.data:
        try:
            msg = await interaction.channel.fetch_message(int(message_id))
            await msg.remove_reaction(emoji, interaction.client.user)
        except: pass

    await interaction.response.send_message(f"🗑️ 已解除：{role.mention} 的綁定。", ephemeral=True)

@bot.tree.command(name="remove_all_reactions", description="清除訊息上所有綁定並移除所有反應")
@app_commands.checks.has_permissions(administrator=True)
async def remove_all_reactions(interaction: discord.Interaction, message_id: str):
    # 刪除資料庫所有對應 message_id 的紀錄
    supabase.table("reaction_roles").delete().eq("message_id", str(message_id)).execute()
    
    # 移除反應
    try:
        msg = await interaction.channel.fetch_message(int(message_id))
        await msg.clear_reactions()
    except: pass
    
    await interaction.response.send_message(f"🔥 已清除 `{message_id}` 所有綁定。", ephemeral=True)
@bot.tree.command(name="add_keyword", description="新增一個關鍵字回覆")
@app_commands.checks.has_permissions(administrator=True)
async def add_keyword(interaction: discord.Interaction, trigger: str, response: str):
    data = {
        "guild_id": str(interaction.guild_id),
        "trigger_word": trigger,
        "response_text": response
    }
    
    # 寫入 Supabase
    supabase.table("keywords").insert(data).execute()
    
    await interaction.response.send_message(f"✅ 已成功新增！當有人說「{trigger}」時，我會回覆「{response}」", ephemeral=True)
# --- 4. 管理員啟動指令 (Slash Command) ---
# --- 管理員啟動指令 (Slash Command) ---
@bot.tree.command(name="建立驗證介面", description="🔒 設置星津蓮汐的入境雲徑 / Establish the Cloud Path")
@app_commands.checks.has_permissions(administrator=True)
async def setup_verify(interaction: discord.Interaction):
    # 定義你的 GIF 連結
    verify_gif_url = "https://cdn.discordapp.com/attachments/1526215549720330300/1529086405907714068/cloud.gif"
    
    embed = discord.Embed(
        title="🪷 星津蓮汐 · 入境雲徑 / The Cloud Path of Elysia",
        description="初來乍到，請點擊下方按鈕進行安全驗證（reCAPTCHA），通過後方能在此地留下足跡。\n\nPlease click the button below to complete the web verification.",
        color=0x8e44ad
    )
    # 加入 GIF 圖片
    embed.set_image(url=verify_gif_url)
    
    # 發送回應（僅自己可見）
    await interaction.response.send_message("雲朵已散開。 / The clouds has been woven.", ephemeral=True)
    
    # 在當前頻道發送帶有 GIF 的 Embed 公告與驗證按鈕
    await interaction.channel.send(embed=embed, view=VerifyView(interaction.guild.id))
class GiveawayView(discord.ui.View):
    def __init__(self, message_id):
        super().__init__(timeout=None)  # 持久化，重啟後按鈕依然有效
        self.message_id = str(message_id)

    def get_participant_count(self):
        # 從資料庫獲取目前參與人數
        res = supabase.table("giveaway_entries").select("user_id", count='exact').eq("message_id", self.message_id).execute()
        return len(res.data)

    @discord.ui.button(label="參與抽獎 / Join (0)", style=discord.ButtonStyle.green, custom_id="join_giveaway")
    async def join_giveaway(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = str(interaction.user.id)
        
        # 先搶先回應互動，避免點擊按鈕時逾時報錯
        await interaction.response.defer(ephemeral=True)
        
        # 檢查是否已參加
        exists = supabase.table("giveaway_entries").select("id").eq("message_id", self.message_id).eq("user_id", user_id).execute()
        if exists.data:
            return await interaction.followup.send("您已經登記過這場福緣了。 / You have already joined.", ephemeral=True)

        # 寫入參與者
        supabase.table("giveaway_entries").insert({
            "message_id": self.message_id,
            "user_id": user_id
        }).execute()

        # 更新按鈕顯示人數並編輯訊息
        count = self.get_participant_count()
        button.label = f"參與抽獎 / Join ({count})"
        await interaction.message.edit(view=self)
        await interaction.followup.send("✅ 已記錄您的福緣。 / Your entry has been recorded.", ephemeral=True)


@bot.tree.command(name="啟動福緣", description="✨ 開啟星津蓮汐的奇幻抽獎 / Initiate a Miracle Giveaway")
@app_commands.checks.has_permissions(administrator=True)
async def giveaway_start(
    interaction: discord.Interaction,
    獎品名稱: str,
    持續時長_分: int,
    中獎名額: int,
    內容描述: str = "共赴這場星辰之約 / Join this starry encounter.",
    特定身分組: discord.Role = None,
    最少發言數: int = 0
):
    end_time = datetime.utcnow() + timedelta(minutes=持續時長_分)
    
    embed = discord.Embed(title=f"🎁 緣定：{獎品名稱}", description=內容描述, color=0xffd700)
    embed.add_field(name="✨ 終焉時刻 (UTC+8)", value=(end_time + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S"))
    embed.add_field(name="📜 結緣條件", value=f"身分要求: {特定身分組.mention if 特定身分組 else '無'}, 靈魂磨礪: {最少發言數}+")
    
    await interaction.response.send_message("福緣已啟動，願星津護佑。 / The miracle has begun.", ephemeral=True)
    
    # 透過 send_as_role 讓指定角色發送抽獎公告
    msg = await send_as_role(
        "₊‧꒰ა 銅鈴 ✮ 𝐿𝓎𝓇𝒶 ໒꒱‧₊",  # 銅鈴在 ROLES 字典裡的精確名稱
        interaction.channel,
        "🎁 抽獎大典",
        content="✨ 神秘的獎品降臨了，請有緣人把握機會！",
        gif="https://cdn.discordapp.com/attachments/1526215549720330300/1529086405907714068/cloud.gif",
        roles=[],
        embed=embed
    )
    
    # 如果 send_as_role 沒有回傳 msg 物件，則透過歷史紀錄抓取最後一則訊息
    if not msg:
        async for message in interaction.channel.history(limit=1):
            msg = message

    # 初始化 View 並賦予到訊息上（讓按鈕可以點擊互動）
    view = GiveawayView(msg.id)
    await msg.edit(view=view)
    
    # 寫入抽獎總表資料庫
    supabase.table("giveaways").insert({
        "message_id": str(msg.id),
        "prize_name": 獎品名稱,
        "end_time": end_time.isoformat(),
        "required_role_id": str(特定身分組.id) if 特定身分組 else None,
        "required_msg_count": 最少發言數,
        "winners_count": 中獎名額
    }).execute()

# --- 假設前置全域變數與模組已存在 ---
# bot, supabase, get_user, update_user, ROLES

# --- 靈物圖鑑定義 (固定專屬靈力值) ---
ARTIFACTS_DB = {
    "東方祥瑞 - Eastern": {
        "凝露": [
            ("翠竹芽", "Bamboo Sprout", 12), ("龍涎香灰", "Dragon Incense Ash", 18),
            ("麒麟草", "Kirin Grass", 25), ("鳳巢枝", "Phoenix Branch", 35), ("鯉魚鱗", "Carp Scale", 42)
        ],
        "草木": [
            ("龍鱗片", "Dragon Scale", 55), ("鳳翎羽", "Phoenix Plume", 70),
            ("麒麟角", "Kirin Horn", 90), ("玄武殼", "Basalt Shell", 115), ("靈脈草", "Spirit Vein Herb", 140)
        ],
        "靈獸": [
            ("青龍珠", "Azure Dragon Pearl", 160), ("鳳凰血", "Phoenix Blood", 210),
            ("麒麟膽", "Kirin Gall", 260), ("玄武精", "Basalt Essence", 300), ("騰雲霧", "Cloud Mist", 340)
        ],
        "星辰": [
            ("應龍逆鱗", "Ying-Dragon Scale", 380), ("鳳凰真羽", "True Phoenix Plume", 460),
            ("麒麟瑞氣", "Kirin Aura", 540), ("玄武背甲", "Basalt Back Shell", 620), ("天界雲", "Heavenly Cloud", 690)
        ],
        "神蹟": [
            ("青龍之心", "Azure Dragon Heart", 750), ("鳳凰靈火", "Phoenix Soul Fire", 850),
            ("麒麟聖印", "Kirin Holy Seal", 920), ("玄武鎮海珠", "Basalt Ocean Pearl", 980), ("龍神敕令", "Dragon God Edict", 1050)
        ]
    },
    "西方神話 - Western": {
        "凝露": [
            ("獅鷲羽毛", "Griffin Feather", 15), ("龍骨粉", "Dragon Bone Powder", 20),
            ("火鳥灰", "Firebird Ash", 28), ("獨角獸鬃", "Unicorn Mane", 38), ("秘銀屑", "Mithril Dust", 45)
        ],
        "草木": [
            ("獅鷲之羽", "Griffin Wing", 60), ("龍之逆鱗", "Dragon Reverse Scale", 75),
            ("不死火", "Undying Fire", 95), ("獨角獸淚", "Unicorn Tear", 120), ("橡木杖", "Oak Staff", 145)
        ],
        "靈獸": [
            ("獅鷲之爪", "Griffin Claw", 170), ("龍魂晶", "Dragon Soul Crystal", 220),
            ("不死火種", "Undying Fire Seed", 270), ("獨角獸角", "Unicorn Horn", 310), ("聖盃碎片", "Holy Grail Fragment", 350)
        ],
        "星辰": [
            ("獅鷲金冕", "Griffin Crown", 390), ("龍神之火", "Dragon God Fire", 470),
            ("鳳凰涅槃灰", "Phoenix Ash", 550), ("獨角獸光", "Unicorn Light", 630), ("深淵龍牙", "Abyssal Dragon Tooth", 700)
        ],
        "神蹟": [
            ("獅鷲王冠", "Griffin King Crown", 770), ("真龍龍珠", "True Dragon Pearl", 860),
            ("鳳凰之魂", "Phoenix Soul", 930), ("獨角獸之淚", "Pure Unicorn Tear", 990), ("神聖龍息", "Divine Dragon Breath", 1080)
        ]
    },
    "北歐/靈性 - Nordic": {
        "凝露": [
            ("芬里爾狼毫", "Fenrir Fur", 10), ("世界蛇皮屑", "Jormungandr Skin", 16),
            ("英靈斷劍", "Broken Hero Sword", 22), ("霜鐵釘", "Frost Iron Nail", 32), ("符文石片", "Rune Stone", 40)
        ],
        "草木": [
            ("霜巨人指甲", "Frost Giant Nail", 52), ("世界樹葉", "World Tree Leaf", 68),
            ("雷霆殘響", "Thunder Echo", 85), ("女武神甲片", "Valkyrie Plate", 110), ("塵世血", "World Blood", 135)
        ],
        "靈獸": [
            ("巨狼獠牙", "Dire Wolf Fang", 155), ("蛇之毒液", "Serpent Venom", 205),
            ("英靈面具", "Hero Mask", 255), ("霜巨人之心", "Frost Giant Heart", 295), ("雷光刃", "Thunder Blade", 335)
        ],
        "星辰": [
            ("芬里爾鎖鏈", "Fenrir Chain", 375), ("世界蛇鱗", "Jormungandr Scale", 455),
            ("女武神號角", "Valkyrie Horn", 535), ("霜巨人王骨", "Frost Giant Bone", 615), ("奧丁殘念", "Odin’s Remnant", 685)
        ],
        "神蹟": [
            ("芬里爾魂魄", "Fenrir Soul", 740), ("世界蛇之眼", "Jormungandr Eye", 840),
            ("女武神羽翼", "Valkyrie Wing", 910), ("霜巨人王血", "Frost Giant King Blood", 970), ("世界樹精華", "World Tree Essence", 1030)
        ]
    },
    "遠古奇幻 - Ancient": {
        "凝露": [
            ("史芬克斯沙", "Sphinx Sand", 14), ("蛇髮絲", "Medusa Hair", 19),
            ("狐毛", "Fox Fur", 27), ("神廟塵", "Temple Dust", 36), ("石像碎片", "Statue Fragment", 44)
        ],
        "草木": [
            ("凝視之石", "Gaze Stone", 58), ("魅惑之息", "Charm Breath", 72),
            ("狐火珠", "Fox Fire Orb", 92), ("神之印章", "God Seal", 118), ("禁忌捲軸", "Forbidden Scroll", 142)
        ],
        "靈獸": [
            ("史芬克斯眼", "Sphinx Eye", 165), ("美杜莎之盾", "Medusa Shield", 215),
            ("九尾妖丹", "Nine-Tail Core", 265), ("時光沙漏", "Time Hourglass", 305), ("靈魂瓶", "Soul Bottle", 345)
        ],
        "星辰": [
            ("史芬克斯謎題", "Sphinx Riddle", 385), ("石化凝視", "Petrify Gaze", 465),
            ("九尾狐尾", "Nine-Tail Fur", 545), ("遺跡鑰匙", "Relic Key", 625), ("古文明晶體", "Ancient Crystal", 695)
        ],
        "神蹟": [
            ("史芬克斯祕語", "Sphinx Secret", 760), ("石化本源", "Petrify Origin", 855),
            ("九尾神珠", "Nine-Tail God Orb", 925), ("時空裂縫核", "Rift Core", 985), ("創世靈光", "Genesis Light", 1060)
        ]
    }
}


# --- 輔助資料庫函式 (若全域未定義可直接使用此版本) ---
def get_user(user_id: str):
    res = supabase.table("users").select("*").eq("id", user_id).execute()
    if res.data:
        return res.data[0]
    # 若無資料則自動初始化
    default_data = {"id": user_id, "feathers": 0, "spirit_fruit": 0}
    supabase.table("users").insert(default_data).execute()
    return default_data

def update_user(user_id: str, data: dict):
    supabase.table("users").update(data).eq("id", user_id).execute()


# --- 1. /凝化 與 /manifest ---
async def handle_manifest(interaction: discord.Interaction, lang: str):
    user_id = str(interaction.user.id)
    user_data = get_user(user_id)
    current_fruits = user_data.get('spirit_fruit', 0)
    cost = 300
    
    if current_fruits < cost:
        if lang == "zh":
            await interaction.response.send_message(f"❌ 你的靈果不足！需要 {cost} 顆靈果，目前僅有 {current_fruits} 顆。", ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ Insufficient Spirit Fruit! Required: {cost}, Current: {current_fruits}.", ephemeral=True)
        return
        
    update_user(user_id, {"spirit_fruit": current_fruits - cost})
    
    rarities = ["凝露", "草木", "靈獸", "星辰", "神蹟"]
    weights = [60, 25, 10, 4, 1]
    chosen_rarity = random.choices(rarities, weights=weights, k=1)[0]
    
    system_name = random.choice(list(ARTIFACTS_DB.keys()))
    item_tuple = random.choice(ARTIFACTS_DB[system_name][chosen_rarity])
    item_name, item_en_name, item_power = item_tuple
    
    supabase.table("user_inventory").insert({
        "user_id": user_id,
        "system": system_name,
        "rarity": chosen_rarity,
        "item_name": item_name,
        "item_en_name": item_en_name,
        "power": item_power
    }).execute()
    
    if lang == "zh":
        embed = discord.Embed(title="✨ 靈物凝化成功", color=0x00ff80)
        embed.description = f"白靈鵠引動時空裂縫，你耗費 {cost} 靈果，凝鍊出屬於「{system_name}」體系的：\n🌟 **[{chosen_rarity}] {item_name}** (靈力值: {item_power})"
    else:
        embed = discord.Embed(title="✨ Manifestation Success", color=0x00ff80)
        embed.description = f"Aetherwyn stirs the rift; you spend {cost} Spirit Fruit to manifest a {system_name} artifact:\n🌟 **[{chosen_rarity}] {item_en_name}** (SP: {item_power})"
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="凝化", description="消耗靈果，獲取初始靈物")
@app_commands.checks.cooldown(1, 3600, key=lambda i: (i.guild_id, i.user.id))
async def manifest_zh(interaction: discord.Interaction):
    await handle_manifest(interaction, "zh")

@manifest_zh.error
async def manifest_zh_error(interaction: discord.Interaction, error: app_commands.CommandOnCooldown):
    await interaction.response.send_message(f"⏳ 凝化能力冷卻中，請在 **{int(error.retry_after // 60)}** 分鐘後再試。", ephemeral=True)

@bot.tree.command(name="manifest", description="Spend Spirit Fruit to manifest an artifact")
@app_commands.checks.cooldown(1, 3600, key=lambda i: (i.guild_id, i.user.id))
async def manifest_en(interaction: discord.Interaction):
    await handle_manifest(interaction, "en")

@manifest_en.error
async def manifest_en_error(interaction: discord.Interaction, error: app_commands.CommandOnCooldown):
    await interaction.response.send_message(f"⏳ Ability on cooldown. Please try again in {int(error.retry_after // 60)} minutes.", ephemeral=True)


# --- 2. /圖譜 與 /tome ---
async def handle_tome(interaction: discord.Interaction, lang: str):
    user_id = str(interaction.user.id)
    res = supabase.table("user_inventory").select("item_name").eq("user_id", user_id).execute()
    user_items = {row["item_name"] for row in res.data} if res.data else set()
    
    if lang == "zh":
        embed = discord.Embed(title="📖 白靈鵠的神獸圖譜", color=0x9b59b6)
        embed.description = "翻開塵封的神獸圖譜，星津蓮汐的遠古史詩於此頁頁顯現。\n"
    else:
        embed = discord.Embed(title="📖 Beast Tome", color=0x9b59b6)
        embed.description = "_Unveiling the long-forgotten Beast Tome, the ancient lore of Elysia unfolds before your eyes._\n"
    
    total_items = 0
    collected_items = len(user_items)
    
    for system, rarities in ARTIFACTS_DB.items():
        sys_str = ""
        for rarity, items in rarities.items():
            line = []
            for item_name, _, _ in items:
                total_items += 1
                if item_name in user_items:
                    line.append(f"🟢 **{item_name}**")
                else:
                    line.append(f"⚫ ~~{item_name}~~")
            sys_str += f"**[{rarity}]** {' | '.join(line)}\n"
        embed.add_field(name=f"🏛️ {system}", value=sys_str, inline=False)
        
    embed.set_footer(text=f"圖鑑解鎖進度 / Progress: {collected_items} / {total_items}")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="圖譜", description="查看神獸圖鑑與你的收藏進度")
@app_commands.checks.cooldown(1, 30, key=lambda i: (i.guild_id, i.user.id))
async def tome_zh(interaction: discord.Interaction):
    await handle_tome(interaction, "zh")

@tome_zh.error
async def tome_zh_error(interaction: discord.Interaction, error: app_commands.CommandOnCooldown):
    await interaction.response.send_message(f"⏳ 圖譜翻閱過於頻繁，請在 {int(error.retry_after)} 秒後再試。", ephemeral=True)

@bot.tree.command(name="tome", description="View the Beast Tome and your collection progress")
@app_commands.checks.cooldown(1, 30, key=lambda i: (i.guild_id, i.user.id))
async def tome_en(interaction: discord.Interaction):
    await handle_tome(interaction, "en")

@tome_en.error
async def tome_en_error(interaction: discord.Interaction, error: app_commands.CommandOnCooldown):
    await interaction.response.send_message(f"⏳ Too frequent. Please try again in {int(error.retry_after)} seconds.", ephemeral=True)


# --- 3. /合成 與 /fuse ---
class FuseSelect(discord.ui.Select):
    def __init__(self, user_items, lang):
        self.lang = lang
        placeholder = "請選擇 2 至 5 件靈物進行合成..." if lang == "zh" else "Select 2 to 5 items to fuse..."
        # 加上 [:25] 避免超過 Discord Select 選項上限 25 的限制
        options = [
            discord.SelectOption(label=f"[{item['rarity']}] {item['item_name']} (SP: {item['power']})", value=str(item['id']))
            for item in user_items[:25]
        ]
        super().__init__(placeholder=placeholder, min_values=2, max_values=min(5, len(options)), options=options)

    async def callback(self, interaction: discord.Interaction):
        selected_ids = self.values
        res = supabase.table("user_inventory").select("*").in_("id", selected_ids).execute()
        items = res.data
        
        if not items:
            msg = "❌ 找不到選中的靈物資料。" if self.lang == "zh" else "❌ Artifact data not found."
            await interaction.response.send_message(msg, ephemeral=True)
            return
            
        total_power = sum(item["power"] for item in items)
        count = len(items)
        avg_power = total_power / count
        
        if avg_power <= 50:
            tier, new_name, new_en = "凝露 (Dew)", "靈能聚合體", "Spirit Aggregate"
        elif avg_power <= 150:
            tier, new_name, new_en = "草木 (Flora)", "萬生之苗", "Seed of Being"
        elif avg_power <= 350:
            tier, new_name, new_en = "靈獸 (Beast)", "神獸寄宿甲", "Beast-Host Armor"
        elif avg_power <= 700:
            tier, new_name, new_en = "星辰 (Cosmic)", "星痕羅盤", "Stellar Trace Compass"
        else:
            tier, new_name, new_en = "神蹟 (Miracle)", "星津蓮汐之冠", "Crown of Elysia"
            
        for item_id in selected_ids:
            supabase.table("user_inventory").delete().eq("id", item_id).execute()
            
        user_id = str(interaction.user.id)
        supabase.table("user_inventory").insert({
            "user_id": user_id,
            "system": "合成產物",
            "rarity": tier.split()[0],
            "item_name": new_name,
            "item_en_name": new_en,
            "power": int(avg_power * 1.2)
        }).execute()
        
        embed = discord.Embed(title="🔮 靈物融合完畢 / Fusion Complete", color=0xe67e22)
        if self.lang == "zh":
            embed.description = f"爐中靈光交織，融合出 [{tier}] {new_name}！\n\n**平均靈力值 (SP):** {int(avg_power)}"
        else:
            embed.description = f"The furnace interweaves light; you fused a [{tier}] {new_en}!\n\n**Average SP:** {int(avg_power)}"
        await interaction.response.edit_message(content=None, embed=embed, view=None)

class FuseView(discord.ui.View):
    def __init__(self, user_items, lang):
        super().__init__(timeout=60)
        self.add_item(FuseSelect(user_items, lang))

async def handle_fuse(interaction: discord.Interaction, lang: str):
    user_id = str(interaction.user.id)
    res = supabase.table("user_inventory").select("*").eq("user_id", user_id).execute()
    
    if not res.data or len(res.data) < 2:
        msg = "❌ 你的背包中至少需要擁有 2 件靈物才能進行合成！" if lang == "zh" else "❌ You need at least 2 artifacts to fuse!"
        await interaction.response.send_message(msg, ephemeral=True)
        return
        
    view = FuseView(res.data, lang)
    prompt_text = "✨ 請在下方選擇你要投入合成的靈物 (2 ~ 5 件)：" if lang == "zh" else "✨ Please select 2 to 5 artifacts below to fuse:"
    await interaction.response.send_message(prompt_text, view=view, ephemeral=True)

@bot.tree.command(name="合成", description="將 2-5 件庫存靈物互相融合")
async def fuse_zh(interaction: discord.Interaction):
    await handle_fuse(interaction, "zh")

@bot.tree.command(name="fuse", description="Fuse 2 to 5 inventory artifacts together")
async def fuse_en(interaction: discord.Interaction):
    await handle_fuse(interaction, "en")


# --- 4. 社交與關係指令 (/結緣 /bond, /斷緣 /break, /分享 /share) ---
@bot.tree.command(name="結緣", description="與他人締結特殊羈絆")
async def bond_zh(interaction: discord.Interaction, member: discord.Member, relationship_type: str):
    embed = discord.Embed(title=f"🌸 締結緣分：{relationship_type}", color=0xff69b4)
    embed.description = f"{interaction.user.mention} 與 {member.mention} 結為 **{relationship_type}**！\n\n兩心因緣於此締結，願星津蓮汐永存此誓，千載不渝。"
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="bond", description="Forge a special bond with someone")
async def bond_en(interaction: discord.Interaction, member: discord.Member, relationship_type: str):
    embed = discord.Embed(title=f"🌸 Forge Bond: {relationship_type}", color=0xff69b4)
    embed.description = f"{interaction.user.mention} and {member.mention} are now bonded as **{relationship_type}**!\n\nA bond of destinies is forged; may this vow echo through Elysia."
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="斷緣", description="解除現有羈絆")
@app_commands.checks.cooldown(1, 2592000, key=lambda i: (i.guild_id, i.user.id))
async def break_zh(interaction: discord.Interaction, member: discord.Member):
    embed = discord.Embed(title="🍂 緣分已盡", color=0x7f8c8d)
    embed.description = f"{interaction.user.mention} 與 {member.mention} 的羈絆已隨風消散。\n\n繁華落盡，緣分已終，白靈鵠拂袖將此間羈絆悄然抹去。"
    await interaction.response.send_message(embed=embed)

@break_zh.error
async def break_zh_error(interaction: discord.Interaction, error: app_commands.CommandOnCooldown):
    await interaction.response.send_message("⏳ 斷緣每個月僅能進行一次，請靜待緣分流轉。", ephemeral=True)

@bot.tree.command(name="break", description="Sever an existing relationship bond")
@app_commands.checks.cooldown(1, 2592000, key=lambda i: (i.guild_id, i.user.id))
async def break_en(interaction: discord.Interaction, member: discord.Member):
    embed = discord.Embed(title="🍂 Relationship Severed", color=0x7f8c8d)
    embed.description = f"The bond between {interaction.user.mention} and {member.mention} has faded.\n\nThe blossoms have faded; Aetherwyn erases this bond."
    await interaction.response.send_message(embed=embed)

@break_en.error
async def break_en_error(interaction: discord.Interaction, error: app_commands.CommandOnCooldown):
    await interaction.response.send_message("⏳ Severing a bond is limited to once a month.", ephemeral=True)

@bot.tree.command(name="分享", description="與靈魂伴侶共賞修行成果")
@app_commands.checks.cooldown(1, 86400, key=lambda i: (i.guild_id, i.user.id))
async def share_zh(interaction: discord.Interaction):
    embed = discord.Embed(title="💫 靈力交融", color=0x3498db)
    embed.description = f"{interaction.user.mention} 進行了每日的靈力分享。\n\n你與靈魂伴侶共賞修行成果，兩股靈力於繾綣間交融流轉。"
    await interaction.response.send_message(embed=embed)

@share_zh.error
async def share_zh_error(interaction: discord.Interaction, error: app_commands.CommandOnCooldown):
    await interaction.response.send_message("⏳ 每日分享已達上限，請 24 小時後再來。", ephemeral=True)

@bot.tree.command(name="share", description="Share cultivation progress with your soulmate")
@app_commands.checks.cooldown(1, 86400, key=lambda i: (i.guild_id, i.user.id))
async def share_en(interaction: discord.Interaction):
    embed = discord.Embed(title="💫 Spiritual Sharing", color=0x3498db)
    embed.description = f"{interaction.user.mention} performed daily spiritual sharing.\n\nYou share your cultivation; twin currents of spiritual light interlace."
    await interaction.response.send_message(embed=embed)

@share_en.error
async def share_en_error(interaction: discord.Interaction, error: app_commands.CommandOnCooldown):
    await interaction.response.send_message("⏳ Daily sharing limit returned. Please try again in 24 hours.", ephemeral=True)


# --- 5. 導覽指令 (/秘典 /grimoire, /指令總覽 /help_guide) ---
@bot.tree.command(name="秘典", description="翻閱星津蓮汐的遠古靈物秘典")
@app_commands.checks.cooldown(1, 10, key=lambda i: (i.guild_id, i.user.id))
async def grimoire_zh(interaction: discord.Interaction):
    embed = discord.Embed(
        title="✨ 星津蓮汐 • 遠古靈物秘典",
        description=(
            "歡迎來到星津蓮汐的修真世界：\n"
            "• **`/凝化`**：消耗 300 靈果引動裂縫，凝鍊四大體系珍稀靈物！\n"
            "• **`/圖譜`**：翻閱神獸圖譜，檢視你的收藏與解鎖進度。\n"
            "• **`/合成`**：融合 2-5 件靈物突破階級，產出更高階寶物！\n\n"
            "🌟 **稀有度與靈力級距：**\n"
            "• 🟢 **凝露 (Dew)**: 約 10 ~ 45\n"
            "• 🟢 **草木 (Flora)**: 約 50 ~ 145\n"
            "• 🔵 **靈獸 (Beast)**: 約 150 ~ 350\n"
            "• 🟣 **星辰 (Cosmic)**: 約 375 ~ 700\n"
            "• 🟡 **神蹟 (Miracle)**: 約 740 ~ 1080+\n"
        ),
        color=0x00ffcc
    )
    embed.set_footer(text="願星津蓮汐的星光指引你的前路。")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@grimoire_zh.error
async def grimoire_zh_error(interaction: discord.Interaction, error: app_commands.CommandOnCooldown):
    await interaction.response.send_message(f"⏳ 秘典翻閱過於頻繁，請在 {int(error.retry_after)} 秒後再試。", ephemeral=True)

@bot.tree.command(name="grimoire", description="Consult the ancient artifact grimoire")
@app_commands.checks.cooldown(1, 10, key=lambda i: (i.guild_id, i.user.id))
async def grimoire_en(interaction: discord.Interaction):
    embed = discord.Embed(
        title="✨ Ancient Artifact Grimoire of Elysia",
        description=(
            "Welcome to the mystical realm of Elysia:\n"
            "• **`/manifest`**: Spend 300 Spirit Fruit to manifest rare artifacts.\n"
            "• **`/tome`**: View the Beast Tome and track your collection.\n"
            "• **`/fuse`**: Interweave 2-5 artifacts to create higher-tier treasures.\n\n"
            "🌟 **Rarity & SP Tiers:**\n"
            "• 🟢 **Dew**: Approx. 10 ~ 45\n"
            "• 🟢 **Flora**: Approx. 50 ~ 145\n"
            "• 🔵 **Beast**: Approx. 150 ~ 350\n"
            "• 🟣 **Cosmic**: Approx. 375 ~ 700\n"
            "• 🟡 **Miracle**: Approx. 740 ~ 1080+\n"
        ),
        color=0x00ffcc
    )
    embed.set_footer(text="May the starlight guide your path.")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@grimoire_en.error
async def grimoire_en_error(interaction: discord.Interaction, error: app_commands.CommandOnCooldown):
    await interaction.response.send_message(f"⏳ Too frequent. Please try again in {int(error.retry_after)} seconds.", ephemeral=True)


async def send_log_as_ryan(guild_id, embed: discord.Embed):
    res = supabase.table("logs_config").select("log_channel_id").eq("guild_id", str(guild_id)).execute()
    if not res.data: 
        return
    channel = bot.get_channel(int(res.data[0]["log_channel_id"]))
    if not channel: 
        return

    ryan_name = "⋆༺𓆩瑞恩 ⚔ 𝑅𝓎𝒶𝓃𓆪༻⋆"
    ryan_avatar = ROLES.get(ryan_name) if 'ROLES' in globals() else None
    webhooks = await channel.webhooks()
    webhook = discord.utils.get(webhooks, name="MonitorHook") or await channel.create_webhook(name="MonitorHook")

    embed.set_author(name=ryan_name, icon_url=ryan_avatar)
    embed.timestamp = discord.utils.utcnow()
    await webhook.send(embed=embed, username=ryan_name, avatar_url=ryan_avatar)


# --- 綜合指令總覽 (/神諭 /oracle) ---
@bot.tree.command(name="神諭", description="查看星津蓮汐所有居民可用指令與指引")
@app_commands.checks.cooldown(1, 10, key=lambda i: (i.guild_id, i.user.id))
async def oracle_zh(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📜 星津蓮汐 • 神諭居民指引",
        description="以下為星津蓮汐所有居民皆可通曉的完整指令列表（中文版）：\n",
        color=0x3498db
    )
    embed.add_field(
        name="🌟 A. 核心交互與修行指令 (Core Commands)",
        value=(
            "• `/守望` (1次/日) - 居高注視，獲取羽毛與靈果\n"
            "• `/漱玉` (15分冷卻) - 於靈泉深處淨魂獲靈果\n"
            "• `/拾羽` (30分冷卻) - 拾起高天飄落的隨機羽毛\n"
            "• `/煎茶` (2小時冷卻) - 燃修為煎茶化為羽毛收益\n"
            "• `/品茗` (6小時冷卻) - 與白靈鵠對弈品茶，福禍隨心\n"
            "• `/叩問` (無限制) - 聆聽白靈鵠的低語神箴\n"
            "• `/祈願` (無限制) - 獻上神羽應允心中祈願\n"
            "• `/境界` (30秒冷卻) - 檢視自身階位、修為與因緣進度\n"
            "• `/秘典` (10秒冷卻) - 查看靈物與修行體系導覽"
        ),
        inline=False
    )
    embed.add_field(
        name="✨ 靈物與圖鑑體系 (Artifact & Tome)",
        value=(
            "• `/凝化` (1小時冷卻) - 消耗 300 靈果凝化初始靈物\n"
            "• `/圖譜` (30秒冷卻) - 翻閱神獸圖譜檢視收藏碎片與成品\n"
            "• `/合成` - 將 2 至 5 件庫存靈物互相融合突破階級"
        ),
        inline=False
    )
    embed.add_field(
        name="🌸 社交與情感系 (Social Bonds)",
        value=(
            "• `/結緣` - 與他人締結特殊羈絆\n"
            "• `/斷緣` (1次/月) - 解除現有羈絆\n"
            "• `/分享` (1次/日) - 與靈魂伴侶共賞修行成果"
        ),
        inline=False
    )
    embed.set_footer(text="願星津蓮汐的星光與神諭指引你的前路。")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@oracle_zh.error
async def oracle_zh_error(interaction: discord.Interaction, error: app_commands.CommandOnCooldown):
    await interaction.response.send_message(f"⏳ 神諭翻閱過於頻繁，請在 {int(error.retry_after)} 秒後再試。", ephemeral=True)

@bot.tree.command(name="oracle", description="View all available resident commands and guidance")
@app_commands.checks.cooldown(1, 10, key=lambda i: (i.guild_id, i.user.id))
async def oracle_en(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📜 Oracle • Resident Commands Guide",
        description="Here is the complete list of resident commands available in Elysia (English version):\n",
        color=0x3498db
    )
    embed.add_field(
        name="🌟 A. Core & Cultivation Commands",
        value=(
            "• `/vigil` (1/day) - Receive daily feathers and spirit fruit\n"
            "• `/rinse` (15m CD) - Rinse spirit in the sacred spring\n"
            "• `/plumage` (30m CD) - Catch a stray feather from the heavens\n"
            "• `/brew` (2h CD) - Brew tea using cultivation for feathers\n"
            "• `/sip` (6h CD) - Play wits with Aetherwyn over tea\n"
            "• `/inquire` - Listen to Aetherwyn's proverbs\n"
            "• `/vow` - Offer a feather for your sacred vow\n"
            "• `/realm` (30s CD) - Check your cosmic rank and progress\n"
            "• `/grimoire` (10s CD) - View artifact and cultivation guide"
        ),
        inline=False
    )
    embed.add_field(
        name="✨ Artifact & Tome System",
        value=(
            "• `/manifest` (1h CD) - Spend 300 Spirit Fruit to manifest artifacts\n"
            "• `/tome` (30s CD) - View the Beast Tome and your collection\n"
            "• `/fuse` - Interweave 2 to 5 artifacts together"
        ),
        inline=False
    )
    embed.add_field(
        name="🌸 Social & Bonding System",
        value=(
            "• `/bond` - Forge a special bond with someone\n"
            "• `/break` (1/month) - Sever an existing bond\n"
            "• `/share` (1/day) - Share cultivation with your soulmate"
        ),
        inline=False
    )
    embed.set_footer(text="May starlight and the oracle guide your path.")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@oracle_en.error
async def oracle_en_error(interaction: discord.Interaction, error: app_commands.CommandOnCooldown):
    await interaction.response.send_message(f"⏳ Too frequent. Please try again in {int(error.retry_after)} seconds.", ephemeral=True)

# --- 1. 警告相關處理函數 ---
async def handle_warn(interaction: discord.Interaction, member: discord.Member, reason: str):
    # 提前 defer 互動，避免 Discord 互動逾時或重複回應衝突
    await interaction.response.defer(ephemeral=True)

    # 檢查執行者是否具有管理訊息權限
    if not interaction.user.guild_permissions.manage_messages:
        await interaction.followup.send("❌ 你沒有權限執行此制裁。", ephemeral=True)
        return
        
    user_id = str(member.id)
    admin_id = str(interaction.user.id)
    
    # 1. 將本次警告紀錄存入 Supabase 的 warnings 資料表
    supabase.table("warnings").insert({
        "user_id": user_id,
        "admin_id": admin_id,
        "reason": reason,
        "created_at": datetime.utcnow().isoformat()
    }).execute()
    
    # 2. 查詢該成員在當前資料庫中總共被警告了幾次
    response = supabase.table("warnings").select("*", count="exact").eq("user_id", user_id).execute()
    warn_count = response.count if response.count is not None else 1
    
    # 3. 根據警告次數執行對應的懲罰
    punishment_text_zh = ""
    punishment_text_en = ""
    
    try:
        if warn_count == 1:
            timeout_duration = timedelta(hours=3)
            await member.timeout(timeout_duration, reason=f"累積警告 1 次：{reason}")
            punishment_text_zh = "此為第 1 次警告，已被禁言 3 小時。"
            punishment_text_en = "This is the 1st warning. Timed out for 3 hours."
            
        elif 2 <= warn_count <= 4:
            timeout_duration = timedelta(days=1)
            await member.timeout(timeout_duration, reason=f"累積警告 {warn_count} 次：{reason}")
            punishment_text_zh = f"此為第 {warn_count} 次警告，已被禁言 1 天。"
            punishment_text_en = f"This is the {warn_count}th warning. Timed out for 1 day."
            
        elif warn_count >= 5:
            await member.ban(reason=f"累積警告達 5 次：{reason}")
            punishment_text_zh = "累積警告已達 5 次，已執行永久逐出（Ban）。"
            punishment_text_en = "Reached 5 warnings. The user has been permanently banned."
            
    except discord.Forbidden:
        punishment_text_zh += "\n*(⚠️ 機器人權限不足)*"
        punishment_text_en += "\n*(⚠️ Bot lacks permissions)*"
    except Exception as e:
        punishment_text_zh += f"\n*(❌ 發生錯誤: {e})*"

    # 4. 建立警示 Embed 內容
    embed = discord.Embed(title="⚠️ 靈臺警示 / Warning Issued", color=0xe74c3c)
    embed.description = (
        f"**【中文】**\n"
        f"管理員已對 {member.mention} 發出警告。\n"
        f"**原因：** {reason}\n"
        f"**目前累積警告：** `{warn_count}` 次\n"
        f"**制裁結果：** {punishment_text_zh}\n\n"
        f"**【English】**\n"
        f"A warning has been recorded for {member.mention}.\n"
        f"**Reason:** {reason}\n"
        f"**Total Warnings:** `{warn_count}`\n"
        f"**Action Taken:** {punishment_text_en}"
    )

    # 5. 判斷執行者的名字是否在 ROLES 字典裡，如果是就用他的身分發，預設用瑞恩
    sender_name = "⋆༺𓆩瑞恩 ⚔ 𝑅𝓎𝒶𝓃𓆪༻⋆" 
    admin_display_name = interaction.user.display_name
    
    # 確保 ROLES 存在才執行迴圈
    if 'ROLES' in globals() and isinstance(ROLES, dict):
        for role_name in ROLES.keys():
            if role_name in admin_display_name or admin_display_name in role_name:
                sender_name = role_name
                break

    # 6. 直接呼叫 send_as_role 函數發送
    await send_as_role(
        sender_name, 
        interaction.channel, 
        "⚠️ 靈臺警示", 
        content=f"{member.mention} 違規警告", 
        gif="https://cdn.discordapp.com/attachments/1526215549720330300/1529086405907714068/cloud.gif", 
        roles=[], 
        embed=embed
    )

    # 回應指令執行者（僅自己可見）
    await interaction.followup.send("✅ 警告已成功發布。", ephemeral=True)


# --- 2. 斜線指令群組 ---
@bot.tree.command(name="警告", description="對違規成員發出靈臺警示並自動執行階梯懲罰")
@app_commands.describe(member="要警告的成員", reason="違規原因")
async def warn_zh(interaction: discord.Interaction, member: discord.Member, reason: str):
    await handle_warn(interaction, member, reason)

@bot.tree.command(name="warn", description="Issue a warning and apply automated progressive punishment")
@app_commands.describe(member="The member to warn", reason="Reason for warning")
async def warn_en(interaction: discord.Interaction, member: discord.Member, reason: str):
    await handle_warn(interaction, member, reason)

@bot.tree.command(name="櫻花名言", description="🌸 發布浪漫的櫻花透明分隔線與中英名言")
@app_commands.checks.has_permissions(administrator=True)
async def sakura_quote(interaction: discord.Interaction):
    sakura_gif = "https://cdn.discordapp.com/attachments/1526215549720330300/1529092654619558000/sakura.gif"
    quotes = [
        "落花不是無情物，化作春泥更護花。 ｜ Falling flowers are not ruthless things; they turn to spring soil to nurture more life.",
        "櫻花啊，櫻花，陽春三月晴空下。 ｜ Sakura, sakura, under the spring sky in March.",
        "生命如櫻花般燦爛而短暫。 ｜ Life is as brilliant and fleeting as cherry blossoms.",
        "在落櫻紛飛的季節裡，思念悄然綻放。 ｜ In the season of falling cherry blossoms, longing quietly blooms."
    ]
    selected_quote = random.choice(quotes)
    
    embed = discord.Embed(description=f"{selected_quote}", color=0xffb7c5)
    embed.set_image(url=sakura_gif)
    
    await interaction.response.send_message("✨ 櫻花名言已成功落下。", ephemeral=True)
    await interaction.channel.send(embed=embed)

@bot.tree.command(name="設置伺服器統計", description="🌸 自動建立雙語花園統計語音頻道")
@app_commands.checks.has_permissions(administrator=True)
async def setup_server_stats(interaction: discord.Interaction):
    await interaction.response.send_message("⏳ 正在建立花園祕境數據頻道... / Cultivating the arboretum statistics...", ephemeral=True)
    guild = interaction.guild
    
    total_members = guild.member_count
    human_members = len([m for m in guild.members if not m.bot])
    bot_members = len([m for m in guild.members if m.bot])
    
    channels_data = [
        f"🌸 百花齊放 / Flora: {total_members}",
        f"📜 遷客騷人 / Wanderers: {human_members}",
        f"🌱 露水花匠 / Warden: {bot_members}"
    ]
    
    category = await guild.create_category("❀ 靈露繁花 / Arboretum ❀")
    
    for name in channels_data:
        vc = await guild.create_voice_channel(name, category=category)
        await vc.set_permissions(guild.default_role, connect=False)
        
    await interaction.followup.send("✅ 花園祕境統計語音頻道已成功架設！ / The arboretum channels have been successfully established!", ephemeral=True)


# --- 3. 機器人啟動事件 ---
@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()
        print(f"成功同步了 {len(synced)} 個指令。")
    except Exception as e:
        print(f"指令同步失敗: {e}")

    bot.add_view(VowView())
    bot.add_view(ServicePanelView())
    bot.add_view(TicketView())
    bot.add_view(TicketControlsView())
    bot.add_view(VerifyView()) # 確保驗證按鈕也有註冊持久化 UI
    print("星津蓮汐系統已全數啟動。")


# --- 4. 訊息處理與經驗值系統 (已合併為單一 on_message) ---
@bot.event
async def on_message(message):
    if message.author.bot: 
        return

    # 1. 訊息監控系統 (監控特定語音頻道文字)
    target_categories = ["•°•°~笙歌樂坊 ᪥ 𝑪𝒂𝒃𝒂𝒓𝒆𝒕 ~°•°•", "•°•°~幽篁別苑 ᪥ 𝑺𝒂𝒏𝒄𝒕𝒖𝒎 ~°•°•"]
    if message.channel.category and message.channel.category.name in target_categories:
        embed = discord.Embed(title="💬 語音室文字紀錄 / Voice-Channel Chat", description=message.content, color=0x3498db)
        embed.set_author(name=message.author.display_name, icon_url=message.author.display_avatar.url)
        embed.add_field(name="所在室 / Location", value=message.channel.mention)
        await send_log_as_ryan(message.guild.id, embed)

    # 唱歌頻道經驗值系統
    SINGING_CHANNEL_ID = 1527915355534655579
    if message.channel.id == SINGING_CHANNEL_ID:
        is_voice_message = message.flags.voice or any(att.content_type and 'audio' in att.content_type for att in message.attachments)
        
        if is_voice_message:
            has_singer_role = any(role.id == 1529050575587770461 for role in message.author.roles)
            
            if has_singer_role:
                base_exp = 90
                boost_role_ids = {1529050371849453658, 1526426658607726613}
                has_boost_role = any(role.id in boost_role_ids for role in message.author.roles)
                
                multiplier = 1.05 if has_boost_role else 1.0
                final_exp = int(base_exp * multiplier)
                
                # update_user_exp(str(message.author.id), final_exp)
                await message.reply(f"✨ 靈音迴盪，白靈鵠為你注入了 **{final_exp}** 點修為靈力！", delete_after=5)

    # 2. 防刷頻檢查 (管理員除外)
    if not message.author.guild_permissions.administrator:
        await handle_spam_detection(message)

    # 3. 一般經驗值系統
    exp_gain = random.randint(5, 15)
    u = get_user(message.author.id)
    new_exp = u.get('exp', 0) + exp_gain
    new_level = u.get('level', 1)
    
    req = calculate_required_exp(new_level)
    if new_exp >= req:
        new_level += 1
        new_exp = 0
        await check_and_announce_level_up(message.author, message.channel, new_level)
    
    update_user(message.author.id, {"exp": new_exp, "level": new_level})

    # 4. 關鍵字系統
    res = supabase.table("keywords").select("trigger_word", "response_text").eq("guild_id", str(message.guild.id)).execute()
    for item in res.data:
        if item["trigger_word"] in message.content:
            await message.channel.send(item["response_text"])
            break

    # 5. 處理指令
    await bot.process_commands(message)


# --- 5. 反應角色系統 (Supabase 版本) ---
@bot.event
async def on_raw_reaction_add(payload):
    if payload.user_id == bot.user.id: 
        return
    
    res = supabase.table("reaction_roles").select("role_id").eq("message_id", str(payload.message_id)).eq("emoji", str(payload.emoji)).execute()
    
    if res.data:
        guild = bot.get_guild(payload.guild_id)
        if not guild: 
            return
        member = await guild.fetch_member(payload.user_id)
        for item in res.data:
            role = guild.get_role(item['role_id'])
            if role and role not in member.roles:
                await member.add_roles(role)

@bot.event
async def on_raw_reaction_remove(payload):
    res = supabase.table("reaction_roles").select("role_id").eq("message_id", str(payload.message_id)).eq("emoji", str(payload.emoji)).execute()
    
    if res.data:
        guild = bot.get_guild(payload.guild_id)
        if not guild: 
            return
        try:
            member = await guild.fetch_member(payload.user_id)
            for item in res.data:
                role = guild.get_role(item['role_id'])
                if role and role in member.roles:
                    await member.remove_roles(role)
        except discord.NotFound:
            pass


# --- 6. 語音掛機機制 ---
@tasks.loop(minutes=30)
async def voice_exp_task():
    for guild in bot.guilds:
        for channel in guild.voice_channels:
            members = [m for m in channel.members if not m.bot]
            for member in members:
                u = get_user(member.id)
                new_exp = u.get('exp', 0) + 30
                new_level = u.get('level', 1)
                
                req = calculate_required_exp(new_level)
                if new_exp >= req:
                    new_level += 1
                    new_exp = 0
                    text_channel = guild.text_channels[0]
                    await check_and_announce_level_up(member, text_channel, new_level)
                
                update_user(member.id, {"exp": new_exp, "level": new_level})


# --- 7. 成員離開事件 ---
@bot.event
async def on_member_remove(member):
    guild_id = str(member.guild.id)
    user_id = str(member.id)
    
    res = supabase.table("member_stats").select("leave_count").eq("user_id", user_id).execute()
    new_leave_count = (res.data[0]['leave_count'] + 1) if res.data else 1
    
    if not res.data:
        supabase.table("member_stats").insert({"guild_id": guild_id, "user_id": user_id, "join_count": 0, "leave_count": 1}).execute()
    else:
        supabase.table("member_stats").update({"leave_count": new_leave_count}).eq("user_id", user_id).execute()

    try:
        channel = await bot.fetch_channel(1528256339686592703)
    except Exception as e:
        print(f"無法獲取離別頻道: {e}")
        return

    embed = discord.Embed(title="🍂 蝶影遠去 / The Butterfly Departs", color=0x8e44ad)
    embed.description = (
        "蝶已盡興，離了蓮花；這份緣分將化作星塵，被收進星津蓮汐的詩篇裡。\n"
        "The butterfly has sipped its fill and departed the water lily; this bond shall turn into stardust.\n\n"
        f"{member.mention} 踏上了歸途。 / **Left us to embark a new journey.**\n"
        f"這是他第 {new_leave_count} 次告別。 / This is their {new_leave_count}th departure.\n\n"
        "雖然身影遠去，但你曾帶來的悸動，如同星光般在漣漪中長存。\n"
        "Though your shadow fades, the heartbeat you brought remains like starlight.\n\n"
        "山水有相逢，期待在世界的盡頭再見。\n"
        "May our paths cross again at the end of the world."
    )
    
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_image(url="https://cdn.discordapp.com/attachments/1526215616485392475/1527709097842184383/butterfly.gif")
    
    await send_as_role(
        "°❀ 綻次郎 𐀔 𝒞𝒽𝑜𝒿𝒾𝓇𝑜 ❀°", 
        channel, 
        "離別 / Farewell", 
        content=f"{member.mention} 踏上了歸途。", 
        gif="https://cdn.discordapp.com/attachments/1526215616485392475/1527709097842184383/butterfly.gif", 
        roles=[], 
        embed=embed
    )


# --- 8. 成員加入事件 ---
@bot.event
async def on_member_join(member):
    auto_role_ids = [1526494444797558794, 1526516054686302280, 1527699111929974985, 1526473371179548672]
    for rid in auto_role_ids:
        role = member.guild.get_role(rid)
        if role:
            try:
                await member.add_roles(role)
            except discord.Forbidden:
                print(f"權限不足：無法自動派發 {role.name}")
                
    pending_role = member.guild.get_role(1528755613683679362)
    if pending_role:
        await member.add_roles(pending_role)

    try:
        welcome_channel = await bot.fetch_channel(1527913917060485171)
        welcome_embed = discord.Embed(title="🌸 歡迎來到「星津蓮汐」Welcome to Elysia", color=0xffb6c1)
        welcome_embed.description = (
            f"歡迎 {member.mention} 來到這片仙境。\n**Welcome to this land of fantasies.**\n\n"
            "初見如昨，願你在此尋得內心深處的純粹。\nOur first meeting is like a dream; may you find the depth of your heart here.\n\n"
            f"📖 [閱讀規則 / READ RULES](https://discord.com/channels/{member.guild.id}/1527914588677341314) | 📇 [驗證 / VERFICATION](https://discord.com/channels/{member.guild.id}/1528987497999241349)\n\n"
            f"💬 [一般交流 / CHAT](https://discord.com/channels/{member.guild.id}/1526215218726699068)| 🏷️ [領取身分組 / ROLES](https://discord.com/channels/{member.guild.id}/1527913992201441301)\n\n"
        )
        welcome_embed.set_thumbnail(url=member.display_avatar.url)
        welcome_embed.set_image(url="https://cdn.discordapp.com/attachments/1526215616485392475/1527709017906876546/lotus.gif")
        
        await send_as_role(
            "°❀ 綻次郎 𐀔 𝒞𝒽𝑜𝒿𝒾𝓇𝑜 ❀°", 
            welcome_channel, 
            "歡迎 / Welcome", 
            content=f"{member.mention} 歡迎來到星津蓮汐！", 
            gif="https://cdn.discordapp.com/attachments/1526215616485392475/1527709017906876546/lotus.gif", 
            roles=[], 
            embed=welcome_embed
        )
    except Exception as e:
        print(f"歡迎程序錯誤: {e}")

    try:
        guild_id = str(member.guild.id)
        res = supabase.table("member_stats").select("join_count").eq("user_id", str(member.id)).execute()
        count = (res.data[0]['join_count'] + 1) if res.data else 1
        
        if not res.data:
            supabase.table("member_stats").insert({"guild_id": guild_id, "user_id": str(member.id), "join_count": 1, "leave_count": 0}).execute()
        else:
            supabase.table("member_stats").update({"join_count": count}).eq("user_id", str(member.id)).execute()
        
        inviter = await get_inviter(member) if 'get_inviter' in globals() else None
        inviter_mention = inviter.mention if inviter else "未知邀請者 / Unknown"
        inviter_id_str = str(inviter.id) if inviter else "0"
        
        invites_res = supabase.table("invites_tracker").select("id").eq("inviter_id", inviter_id_str).execute()
        total_crystals = len(invites_res.data) if invites_res.data else 0
        
        monitor_embed = discord.Embed(title="新訪客到來 / New Guest Arrival", color=0x00ff80)
        monitor_embed.add_field(name="訪問資訊 / Visit Info", value=f"這是他的第 {count} 次到訪 / This is their {count}th visit.\n由 {inviter_mention} 派發純水晶 / Crystals distributed by {inviter_mention}.", inline=False)
        monitor_embed.add_field(name="邀請者貢獻 / Inviter Contribution", value=f"{inviter_mention} 已累積派發 {total_crystals} 顆水晶 / {inviter_mention} has distributed {total_crystals} crystals.", inline=False)
        monitor_embed.set_thumbnail(url=member.display_avatar.url)
        
        await send_log_as_ryan(guild_id, monitor_embed)
    except Exception as e:
        print(f"監控數據程序錯誤: {e}")


# --- 9. 頻道與身分組異動事件 ---
@bot.event
async def on_guild_channel_create(channel):
    embed = discord.Embed(title="🌱 新的土地被挖掘 / New land has been excavated", color=0x7289da)
    embed.description = f"頻道 {channel.mention} 已誕生。"
    await send_log_as_ryan(channel.guild.id, embed)

@bot.event
async def on_guild_channel_delete(channel):
    embed = discord.Embed(title="🍂 土地回歸大自然 / Returning land to nature", color=0xed4245)
    embed.description = f"頻道 {channel.name} 已消逝於大地。"
    await send_log_as_ryan(channel.guild.id, embed)

@bot.event
async def on_guild_role_create(role):
    embed = discord.Embed(title="✨ 領悟新道 / A new path realized", color=0xf1c40f)
    embed.description = f"身分組 {role.mention} 已建立。"
    await send_log_as_ryan(role.guild.id, embed)

@bot.event
async def on_guild_role_delete(role):
    embed = discord.Embed(title="⌛ 塵歸塵 / Return to Ashes", color=0x36393f)
    embed.description = f"身分組 **{role.name}** 已被徹底抹除，過往的足跡隨風消散。"
    embed.add_field(name="ID", value=role.id, inline=False)
    await send_log_as_ryan(role.guild.id, embed)

@bot.event
async def on_member_update(before, after):
    if before.roles != after.roles:
        added = [r.mention for r in after.roles if r not in before.roles]
        removed = [r.mention for r in before.roles if r not in after.roles]
        
        if added:
            embed = discord.Embed(title="🍀 賦予名諱 / Bestowed with a Name", color=0x00ff80)
            embed.add_field(name="成員 / Member", value=after.mention)
            embed.add_field(name="被封為 / Was given", value=" ".join(added))
            await send_log_as_ryan(after.guild.id, embed)
            
        if removed:
            embed = discord.Embed(title="🌑 遺忘舊夢 / Memory faded away", color=0x99aab5)
            embed.add_field(name="成員 / Member", value=after.mention, inline=False)
            embed.add_field(name="捨棄 / Forsaken", value=" ".join(removed), inline=False)
            await send_log_as_ryan(after.guild.id, embed)


# --- 10. 驗證系統 UI (Modal & View) ---
class VerifyModal(discord.ui.Modal, title="星津蓮汐 • 安全驗證 / Elysia Security Verify"):
    captcha_input = discord.ui.TextInput(
        label="請輸入您看到的驗證碼 / Please enter the verification code",
        style=discord.TextStyle.short,
        placeholder="請查看上方訊息的驗證碼 / See code in the previous message",
        required=True,
        min_length=4,
        max_length=4,
    )

    async def on_submit(self, interaction: discord.Interaction):
        user_id = str(interaction.user.id)
        input_code = self.captcha_input.value

        res = supabase.table("verification_codes").select("code").eq("user_id", user_id).execute()
        
        if not res.data or res.data[0]["code"] != input_code:
            await interaction.response.send_message("❌ 驗證碼錯誤或已過期 / Verification code invalid or expired.", ephemeral=True)
            return

        formal_role = interaction.guild.get_role(1526427255130161173)
        pending_role = interaction.guild.get_role(1528755613683679362)
        
        if formal_role: 
            await interaction.user.add_roles(formal_role)
        if pending_role: 
            await interaction.user.remove_roles(pending_role)
        
        supabase.table("verification_codes").delete().eq("user_id", user_id).execute()
        
        await interaction.response.send_message("✅ 驗證通過，歡迎來到星津蓮汐 / Verification successful. Welcome to Elysia.", ephemeral=True)

class VerifyView(discord.ui.View):
    def __init__(self, guild_id: int = None):
        super().__init__(timeout=None) 
        self.guild_id = guild_id

    @discord.ui.button(label="點擊開啟安全驗證 / Start Verification", style=discord.ButtonStyle.blurple, custom_id="verify_button")
    async def verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        code = str(random.randint(1000, 9999))
        expires_at = (datetime.utcnow() + timedelta(minutes=5)).isoformat()
        
        supabase.table("verification_codes").upsert({
            "user_id": str(interaction.user.id), 
            "code": code, 
            "expires_at": expires_at
        }).execute()
        
        await interaction.response.send_message(f"您的驗證碼是 / Your code is: **{code}** (5分鐘內有效)\n請填寫彈出的視窗 / Please fill in the popup.", ephemeral=True)
        await interaction.followup.send_modal(VerifyModal())


# --- 11. 語音監控事件 ---
@bot.event
async def on_voice_state_update(member, before, after):
    target_categories = ["•°•°~笙歌樂坊 ᪥ 𝑪𝒂𝒃𝒂𝒓𝒆𝒕 ~°•°•", "•°•°~幽篁別苑 ᪥ 𝑺𝒂𝒏𝒄𝒕𝒖𝒎 ~°•°•"]
    
    in_target = (after.channel and after.channel.category and after.channel.category.name in target_categories) or \
                (before.channel and before.channel.category and before.channel.category.name in target_categories)
    
    if not in_target:
        return

    changes = []
    if before.mute != after.mute: 
        changes.append("靜音狀態變更" if after.mute else "已解除靜音")
    if before.self_mute != after.self_mute: 
        changes.append("已靜音" if after.self_mute else "已開麥")
    if before.self_video != after.self_video: 
        changes.append("開啟鏡頭" if after.self_video else "關閉鏡頭")
    
    if changes:
        embed = discord.Embed(title="⚙️ 語音狀態更新 / Voice Status", color=0xf1c40f)
        channel_name = after.channel.name if after.channel else before.channel.name
        embed.description = f"{member.mention} 在 **{channel_name}** 進行了操作。"
        embed.add_field(name="動作 / Action", value=" | ".join(changes))
        await send_log_as_ryan(member.guild.id, embed)


# --- 12. 程式進入點 ---
if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    
    if not token:
        print("錯誤：未找到 DISCORD_TOKEN，請檢查環境變數")
    else:
        bot.run(token)