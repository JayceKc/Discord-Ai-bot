import os  
import discord  
from discord import app_commands  # 載入 Discord 斜線指令相關功能
from discord.ext import commands  # 載入文字指令框架，例如 !hello
from dotenv import load_dotenv  # 載入讀取 .env 檔案的函式

# 讀取 .env
load_dotenv()  # 將 .env 中的設定載入為環境變數

# 取得 Bot Token
TOKEN = os.getenv("DISCORD_TOKEN")  # 從環境變數取得 Discord Bot Token

# 設定 Bot 可以接收的事件
intents = discord.Intents.default()  # 建立 Discord 預設事件權限設定。Intents 用來決定 Bot 可以接收哪些 Discord 事件
intents.message_content = True  # 允許 Bot 讀取訊息內容。因為 !hello 是文字指令，所以需要此權限


class MyBot(commands.Bot):  # 建立自訂 Bot 類別，繼承 commands.Bot 的所有功能
    async def setup_hook(self):  # Discord 登入前執行一次初始化工作
        # 啟動時將斜線指令同步到 Discord
        synced_commands = await self.tree.sync()  # 等待 Discord 完成所有斜線指令的同步
        print(f"已同步 {len(synced_commands)} 個斜線指令")  # 在 Terminal 顯示同步成功的指令數量


# 建立 Bot，指令開頭使用 !
bot = MyBot(  # 建立自訂的 Bot 實例
    command_prefix="!",  # 設定文字指令的開頭符號為驚嘆號
    intents=intents  # intents=intents：套用前面設定的事件權限
)  # 完成 Bot 實例的建立


# Bot 成功上線時執行
@bot.event  # 將下面的函式註冊為 Discord 事件
async def on_ready():  # Bot 成功登入並準備完成時自動執行
    print(f"機器人已上線：{bot.user}")  # 在 Terminal 顯示目前登入的 Bot 帳號


# 建立 !hello 指令
@bot.command()  # 將下面的 hello 函式註冊為文字指令
async def hello(ctx):  # ctx 是指令的上下文，包含使用者、頻道、訊息內容和伺服器
    await ctx.send(f"你好，{ctx.author.mention}！")  # 等待 Discord 傳送訊息；await 不會阻塞整個 Bot


# 建立 /help 斜線指令，並使用 Embed 顯示說明
@bot.tree.command(name="help", description="顯示機器人指令說明")  # 註冊名稱為 /help 的斜線指令
async def help_command(interaction: discord.Interaction):  # 接收使用者執行斜線指令時產生的互動資料
    embed = discord.Embed(  
        title="🤖 機器人指令說明",  
        description="以下是目前可以使用的指令：",  # 設定 Embed 的主要說明文字
        color=discord.Color.green()  # 將 Embed 左側顏色設定為 Discord 綠色
    )  # 完成 Embed 基本設定
    embed.add_field(  # 在 Embed 中加入第一個資訊欄位
        name="/help",  # 設定第一個欄位的名稱
        value="顯示這份 Embed 指令說明。",  
        inline=False  # 讓此欄位獨佔一行，不與其他欄位並排
    )  # 完成第一個欄位
    embed.add_field(  # 在 Embed 中加入第二個資訊欄位
        name="!hello",  
        value="讓機器人跟你打招呼。",  # 說明 !hello 指令的作用
        inline=False  # 讓此欄位獨佔一行，不與其他欄位並排
    )  # 完成第二個欄位
    embed.set_footer(text=f"查詢者：{interaction.user.display_name}")  # 在 Embed 底部顯示查詢者名稱

    # ephemeral=True：只有使用指令的人看得到回覆
    await interaction.response.send_message(embed=embed, ephemeral=True)  # 私密傳送 Embed 給執行 /help 的使用者


# 處理 ! 開頭的文字指令錯誤
@bot.event  # 將下面的函式註冊為文字指令錯誤事件
async def on_command_error(ctx, error):  # 接收發生錯誤的指令上下文與錯誤物件
    if isinstance(error, commands.CommandNotFound):  # 判斷是否輸入了不存在的文字指令
        await ctx.send("找不到這個指令，請輸入 `/help` 查看可用指令。")  # 告知使用者查閱指令列表
    elif isinstance(error, commands.MissingRequiredArgument):  # 判斷是否缺少必要參數
        await ctx.send(f"缺少必要參數：`{error.param.name}`")  # 顯示缺少的參數名稱
    elif isinstance(error, commands.BadArgument):  # 判斷參數是否無法轉換成指定格式
        await ctx.send("參數格式不正確，請檢查後再試。")  # 提示使用者修正參數格式
    elif isinstance(error, commands.CommandOnCooldown):  # 判斷指令是否仍在冷卻時間內
        await ctx.send(f"指令冷卻中，請在 {error.retry_after:.1f} 秒後再試。")  # 顯示還需要等待的秒數
    elif isinstance(error, commands.MissingPermissions):  # 判斷使用者是否缺少 Discord 權限
        await ctx.send("你沒有使用這個指令的權限。")  # 告知使用者權限不足
    elif isinstance(error, commands.CheckFailure):  # 判斷使用者是否未通過指令的其他檢查
        await ctx.send("你目前無法使用這個指令。")  # 顯示一般檢查失敗訊息
    else:  # 處理前面沒有列出的其他文字指令錯誤
        print(f"未處理的文字指令錯誤：{error!r}")  # 將完整錯誤資訊輸出至 Terminal 方便除錯
        await ctx.send("指令執行時發生錯誤，請稍後再試。")  # 向 Discord 使用者顯示通用錯誤訊息


# 處理 / 開頭的斜線指令錯誤
@bot.tree.error  # 將下面的函式註冊為所有斜線指令的錯誤處理器
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):  # 接收互動資料與斜線指令錯誤
    if isinstance(error, app_commands.CommandOnCooldown):  # 判斷斜線指令是否仍在冷卻時間內
        message = f"指令冷卻中，請在 {error.retry_after:.1f} 秒後再試。"  # 建立包含剩餘秒數的錯誤訊息
    elif isinstance(error, app_commands.MissingPermissions):  # 判斷使用者是否缺少 Discord 權限
        message = "你沒有使用這個指令的權限。"  # 建立權限不足訊息
    elif isinstance(error, app_commands.TransformerError):  # 判斷參數是否無法轉換成指定格式
        message = "參數格式不正確，請檢查後再試。"  # 建立參數格式錯誤訊息
    elif isinstance(error, app_commands.CheckFailure):  # 判斷使用者是否未通過斜線指令檢查
        message = "你目前無法使用這個指令。"  # 建立一般檢查失敗訊息
    else:  # 處理前面沒有列出的其他斜線指令錯誤
        print(f"未處理的斜線指令錯誤：{error!r}")  # 將完整錯誤資訊輸出至 Terminal 方便除錯
        message = "指令執行時發生錯誤，請稍後再試。"  # 建立給使用者看的通用錯誤訊息

    # 若指令已經回覆過，需改用 followup 傳送錯誤訊息
    if interaction.response.is_done():  # 檢查這次互動是否已經回覆過
        await interaction.followup.send(message, ephemeral=True)  # 已回覆時使用 followup 私密傳送錯誤訊息
    else:  # 處理這次互動尚未回覆的情況
        await interaction.response.send_message(message, ephemeral=True)  # 首次私密回覆錯誤訊息


# 使用 Token 啟動 Bot
if not TOKEN:  # 檢查環境變數中是否有讀取到 Token
    raise RuntimeError("找不到 DISCORD_TOKEN，請檢查 .env 設定。")  # 沒有 Token 時停止程式並顯示明確原因

bot.run(TOKEN)  # 使用 Token 登入 Discord 並持續執行 Bot
