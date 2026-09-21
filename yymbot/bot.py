import re
import sys
sys.dont_write_bytecode = True
import base64
import logging
import traceback
import utils.decorators as decorators

from md2tgmd.src.md2tgmd import escape, split_code, replace_all
from aient.aient.utils.scripts import Document_extract
from aient.aient.core.utils import get_engine, get_image_message, get_text_message
import config
from config import (
    WEB_HOOK,
    PORT,
    BOT_TOKEN,
    GET_MODELS,
    Users,
    PREFERENCES,
    LANGUAGES,
    PLUGINS,
    RESET_TIME,
    get_robot,
    reset_ENGINE,
    get_current_lang,
    update_info_message,
    update_menu_buttons,
    remove_no_text_model,
    update_initial_model,
    update_models_buttons,
    update_language_status,
    update_first_buttons_message,
    get_all_available_models,
    get_model_groups,
    CUSTOM_MODELS_LIST,
    MODEL_GROUPS,
    get_initial_model,
)

from utils.i18n import strings
from utils.scripts import GetMesageInfo, safe_get, is_emoji

from telegram.constants import ChatAction
from telegram import BotCommand, InlineKeyboardMarkup, InlineQueryResultArticle, InputTextMessageContent, Update, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, InputMediaPhoto, InlineKeyboardButton
from telegram.ext import CommandHandler, MessageHandler, ApplicationBuilder, filters, CallbackQueryHandler, Application, AIORateLimiter, InlineQueryHandler, ChosenInlineResultHandler, ContextTypes, TypeHandler
from datetime import timedelta

import asyncio
import uuid
import re as _re2
lock = asyncio.Lock()
event = asyncio.Event()
stop_event = asyncio.Event()
time_out = 12
poll_time_out = 12

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger()
logging.getLogger("telegram.ext.Application").setLevel(logging.DEBUG)

logging.getLogger("httpx").setLevel(logging.CRITICAL)
logging.getLogger('googleapiclient.discovery_cache').setLevel(logging.ERROR)

class SpecificStringFilter(logging.Filter):
    def __init__(self, specific_string):
        super().__init__()
        self.specific_string = specific_string

    def filter(self, record):
        return self.specific_string not in record.getMessage()

specific_string = "httpx.RemoteProtocolError: Server disconnected without sending a response."
my_filter = SpecificStringFilter(specific_string)

update_logger = logging.getLogger("telegram.ext.Updater")
update_logger.addFilter(my_filter)
update_logger = logging.getLogger("root")
update_logger.addFilter(my_filter)

# 定义一个缓存来存储消息
from collections import defaultdict
message_cache = defaultdict(lambda: [])
time_stamps = defaultdict(lambda: [])

@decorators.GroupAuthorization
@decorators.Authorization
@decorators.APICheck
async def command_bot(update, context, title="", has_command=True):
    stop_event.clear()
    message, rawtext, image_url, chatid, messageid, reply_to_message_text, update_message, message_thread_id, convo_id, file_url, reply_to_message_file_content, voice_text = await GetMesageInfo(update, context)

    if has_command == False or len(context.args) > 0:
        if has_command:
            message = ' '.join(context.args)
        pass_history = Users.get_config(convo_id, "PASS_HISTORY")
        if message == None:
            message = voice_text
        # print("message", message)
        if message and len(message) == 1 and is_emoji(message):
            return

        message_has_nick = False
        botNick = config.NICK.lower() if config.NICK else None
        if rawtext and rawtext.split()[0].lower() == botNick:
            message_has_nick = True

        if message_has_nick and update_message.reply_to_message and update_message.reply_to_message.caption and not message:
            message = update_message.reply_to_message.caption

        if message:
            if pass_history >= 3:
                # 移除已存在的任务（如果有）
                remove_job_if_exists(convo_id, context)
                # 添加新的定时任务
                context.job_queue.run_once(
                    scheduled_function,
                    when=timedelta(seconds=RESET_TIME),
                    chat_id=chatid,
                    name=convo_id
                )

            bot_info_username = None
            try:
                bot_info = await context.bot.get_me(read_timeout=time_out, write_timeout=time_out, connect_timeout=time_out, pool_timeout=time_out)
                bot_info_username = bot_info.username
            except Exception as e:
                print("error:", e)
                bot_info_username = update_message.reply_to_message.from_user.username

            if update_message.reply_to_message \
            and update_message.from_user.is_bot == False \
            and (update_message.reply_to_message.from_user.username == bot_info_username or message_has_nick):
                if update_message.reply_to_message.from_user.is_bot and Users.get_config(convo_id, "TITLE") == True:
                    message = message + "\n" + '\n'.join(reply_to_message_text.split('\n')[1:])
                else:
                    if reply_to_message_text:
                        message = message + "\n" + reply_to_message_text
                    if reply_to_message_file_content:
                        message = message + "\n" + reply_to_message_file_content
            elif update_message.reply_to_message and update_message.reply_to_message.from_user.is_bot \
            and update_message.reply_to_message.from_user.username != bot_info_username:
                return

            robot, role, api_key, api_url = get_robot(convo_id)
            engine = Users.get_config(convo_id, "engine")

            if Users.get_config(convo_id, "LONG_TEXT"):
                async with lock:
                    message_cache[convo_id].append(message)
                    import time
                    time_stamps[convo_id].append(time.time())
                    if len(message_cache[convo_id]) == 1:
                        print("first message len:", len(message_cache[convo_id][0]))
                        if len(message_cache[convo_id][0]) > 800:
                            event.clear()
                        else:
                            event.set()
                    else:
                        return
                try:
                    await asyncio.wait_for(event.wait(), timeout=2)
                except asyncio.TimeoutError:
                    print("asyncio.wait timeout!")

                intervals = [
                    time_stamps[convo_id][i] - time_stamps[convo_id][i - 1]
                    for i in range(1, len(time_stamps[convo_id]))
                ]
                if intervals:
                    print(f"Chat ID {convo_id} 时间间隔: {intervals}，总时间：{sum(intervals)}")

                message = "\n".join(message_cache[convo_id])
                message_cache[convo_id] = []
                time_stamps[convo_id] = []
            # if Users.get_config(convo_id, "TYPING"):
            #     await context.bot.send_chat_action(chat_id=chatid, message_thread_id=message_thread_id, action=ChatAction.TYPING)
            if Users.get_config(convo_id, "TITLE"):
                title = f"`{engine}`\n\n"
            if Users.get_config(convo_id, "REPLY") == False:
                messageid = None

            engine_type, _ = get_engine({"base_url": api_url}, endpoint=None, original_model=engine)
            if robot.__class__.__name__ == "chatgpt":
                engine_type = "gpt"
            if image_url:
                message_list = []
                image_message = await get_image_message(image_url, engine_type)
                text_message = await get_text_message(message, engine_type)
                message_list.append(text_message)
                message_list.append(image_message)
                message = message_list
            elif file_url:
                image_url = file_url
                message = await Document_extract(file_url, image_url, engine_type) + message

            print(f"DBG pre-brain convo={convo_id}", flush=True)
            await getChatGPT(update_message, context, title, robot, message, chatid, messageid, convo_id, message_thread_id, pass_history, api_key, api_url, engine)
    else:
        message = await context.bot.send_message(
            chat_id=chatid,
            message_thread_id=message_thread_id,
            text=escape(strings['message_command_text_none'][get_current_lang(convo_id)]),
            parse_mode='MarkdownV2',
            reply_to_message_id=messageid,
        )

async def delete_message(update, context, messageid = [], delay=60):
    await asyncio.sleep(delay)
    if isinstance(messageid, list):
        for mid in messageid:
            try:
                await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=mid)
            except Exception as e:
                pass
                # print('\033[31m')
                # print("delete_message error", e)
                # print('\033[0m')

from telegram.error import Forbidden, TelegramError
async def is_bot_blocked(bot, user_id: int) -> bool:
    try:
        # 尝试向用户发送一条测试消息
        await bot.send_chat_action(chat_id=user_id, action="typing")
        return False  # 如果成功发送，说明机器人未被封禁
    except Forbidden:
        print("error:", user_id, "已封禁机器人")
        return True  # 如果收到Forbidden错误，说明机器人被封禁
    except TelegramError:
        # 处理其他可能的错误
        return False  # 如果是其他错误，我们假设机器人未被封禁

def clean_answer(text: str) -> str:
    """Strip tool-call artifacts (XML/JSON tool blocks, tool fences).

    Kills 'run python script' style tool echoes; preserves prose/code.
    """
    t = text or ""
    t = _re2.sub(r"```tool.*?```", "", t, flags=_re2.DOTALL)
    t = _re2.sub(r"</?(function|invoke|tool_call|tool_calls|arguments|parameters)[^>]*>", "", t)
    t = _re2.sub(r"\{[^{}]{0,500}?\"(name|function)\"\s*:\s*\"(run_python_script|get_time|get_search_results|generate_image|download_read_arxiv_pdf|get_url_content)[^\n]{0,400}\}?", "", t)
    t = _re2.sub(r"\n{3,}", "\n\n", t)
    return t


async def getChatGPT(update_message, context, title, robot, message, chatid, messageid, convo_id, message_thread_id, pass_history=0, api_key=None, api_url=None, engine = None):
    lastresult = title
    text = message
    result = ""
    tmpresult = ""
    modifytime = 0
    image_has_send = 0
    model_name = engine
    language = Users.get_config(convo_id, "language")
    system_prompt = Users.get_config(convo_id, "systemprompt")
    plugins = Users.extract_plugins_config(convo_id)

    Frequency_Modification = 20
    if "gpt-5" in model_name:
        Frequency_Modification = 25
    if message_thread_id or convo_id.startswith("-"):
        Frequency_Modification = 35
    if "gemini" in model_name:
        Frequency_Modification = 1


    if not await is_bot_blocked(context.bot, chatid):
        answer_messageid = (await context.bot.send_message(
            chat_id=chatid,
            message_thread_id=message_thread_id,
            text=escape(strings['message_think'][get_current_lang(convo_id)]),
            parse_mode='MarkdownV2',
            reply_to_message_id=messageid,
        )).message_id
    else:
        return

    try:
        # print("text", text)
        _type_ci = 0
        async for data in robot.ask_stream_async(text, convo_id=convo_id, pass_history=pass_history, model=model_name, language=language, api_url=api_url, api_key=api_key, system_prompt=system_prompt, plugins=plugins):
        # for data in robot.ask_stream(text, convo_id=convo_id, pass_history=pass_history, model=model_name):
            if stop_event.is_set() and convo_id == target_convo_id and answer_messageid < reset_mess_id:
                return
            _type_ci += 1
            if _type_ci % 12 == 0:
                try:
                    await context.bot.send_chat_action(
                        chat_id=chatid, action="typing")
                except Exception:
                    pass
            if "message_search_stage_" not in data:
                result = result + data
                try:
                    result = clean_answer(result)
                except Exception:
                    pass
            image_match = re.search(r"!\[image\]\(data:image\/png;base64,([a-zA-Z0-9+/=]+)\)", result)
            if image_match and image_has_send == 0:
                base64_str = image_match.group(1)
                try:
                    img_url = base64.b64decode(base64_str)
                    media_group = []
                    media_group.append(InputMediaPhoto(media=img_url))
                    await context.bot.send_media_group(
                        chat_id=chatid,
                        media=media_group,
                        message_thread_id=message_thread_id,
                        reply_to_message_id=messageid,
                    )
                    result = result.replace(image_match.group(0), "")
                    image_has_send = 1
                except Exception as e:
                    logger.warning(f"Could not process base64 image: {e}")
                continue
            if result.strip().startswith("![image](data:image/") and image_has_send:
                await context.bot.delete_message(chat_id=chatid, message_id=answer_messageid)
                break
            tmpresult = result
            if re.sub(r"```", '', result.split("\n")[-1]).count("`") % 2 != 0:
                tmpresult = result + "`"
            if sum([line.strip().startswith("```") for line in result.split('\n')]) % 2 != 0:
                tmpresult = tmpresult + "\n```"
            tmpresult = title + tmpresult
            if "message_search_stage_" in data:
                tmpresult = strings[data][get_current_lang(convo_id)]
            history = robot.conversation[convo_id]
            if safe_get(history, -2, "tool_calls", 0, 'function', 'name') == "generate_image" and not image_has_send and safe_get(history, -1, 'content'):
                image_result = history[-1]['content'].split('\n\n')[1]
                await context.bot.send_photo(chat_id=chatid, photo=image_result, reply_to_message_id=messageid)
                image_has_send = 1
            modifytime = modifytime + 1

            split_len = 3500
            if len(tmpresult) > split_len and Users.get_config(convo_id, "LONG_TEXT_SPLIT"):
                Frequency_Modification = 40

                # print("tmpresult", tmpresult)
                replace_text = replace_all(tmpresult, r"(```[\D\d\s]+?```)", split_code)
                if "@|@|@|@" in replace_text:
                    print("@|@|@|@", replace_text)
                    split_messages = replace_text.split("@|@|@|@")
                    send_split_message = split_messages[0]
                    result = split_messages[1][:-4]
                else:
                    print("replace_text", replace_text)
                    if replace_text.strip().endswith("```"):
                        replace_text = replace_text.strip()[:-4]
                    split_messages_new = []
                    split_messages = replace_text.split("```")
                    for index, item in enumerate(split_messages):
                        if index % 2 == 1:
                            item = "```" + item
                            if index != len(split_messages) - 1:
                                item = item + "```"
                            split_messages_new.append(item)
                        if index % 2 == 0:
                            item_split_new = []
                            item_split = item.split("\n\n")
                            for sub_index, sub_item in enumerate(item_split):
                                if sub_index % 2 == 1:
                                    sub_item = "\n\n" + sub_item
                                    if sub_index != len(item_split) - 1:
                                        sub_item = sub_item + "\n\n"
                                    item_split_new.append(sub_item)
                                if sub_index % 2 == 0:
                                    item_split_new.append(sub_item)
                            split_messages_new.extend(item_split_new)

                    split_index = 0
                    for index, _ in enumerate(split_messages_new):
                        if len("".join(split_messages_new[:index])) < split_len:
                            split_index += 1
                            continue
                        else:
                            break
                    # print("split_messages_new", split_messages_new)
                    send_split_message = ''.join(split_messages_new[:split_index])
                    matches = re.findall(r"(```.*?\n)", send_split_message)
                    if len(matches) % 2 != 0:
                        send_split_message = send_split_message + "```\n"
                    # print("send_split_message", send_split_message)
                    tmp = ''.join(split_messages_new[split_index:])
                    if tmp.strip().endswith("```"):
                        result = tmp[:-4]
                    else:
                        result = tmp
                    # print("result", result)
                    matches = re.findall(r"(```.*?\n)", send_split_message)
                    result_matches = re.findall(r"(```.*?\n)", result)
                    # print("matches", matches)
                    # print("result_matches", result_matches)
                    if len(result_matches) > 0 and result_matches[0].startswith("```\n") and len(result_matches) >= 2:
                        result = matches[-2] + result
                    # print("result", result)

                title = ""
                if lastresult != escape(send_split_message, italic=False):
                    try:
                        await context.bot.edit_message_text(
                            chat_id=chatid,
                            message_id=answer_messageid,
                            text=escape(send_split_message, italic=False),
                            parse_mode='MarkdownV2',
                            disable_web_page_preview=True,
                            read_timeout=time_out,
                            write_timeout=time_out,
                            pool_timeout=time_out,
                            connect_timeout=time_out
                        )
                        lastresult = escape(send_split_message, italic=False)
                    except Exception as e:
                        if "parse entities" in str(e):
                            await context.bot.edit_message_text(
                                chat_id=chatid,
                                message_id=answer_messageid,
                                text=send_split_message,
                                disable_web_page_preview=True,
                                read_timeout=time_out,
                                write_timeout=time_out,
                                pool_timeout=time_out,
                                connect_timeout=time_out
                            )
                            print("error:", send_split_message)
                        else:
                            print("error:", str(e))
                answer_messageid = (await context.bot.send_message(
                    chat_id=chatid,
                    message_thread_id=message_thread_id,
                    text=escape(strings['message_think'][get_current_lang(convo_id)]),
                    parse_mode='MarkdownV2',
                    reply_to_message_id=messageid,
                )).message_id

            now_result = escape(tmpresult, italic=False)
            if now_result and (modifytime % Frequency_Modification == 0 and lastresult != now_result) or "message_search_stage_" in data:
                try:
                    await context.bot.edit_message_text(chat_id=chatid, message_id=answer_messageid, text=now_result, parse_mode='MarkdownV2', disable_web_page_preview=True, read_timeout=time_out, write_timeout=time_out, pool_timeout=time_out, connect_timeout=time_out)
                    lastresult = now_result
                except Exception as e:
                    # print('\033[31m')
                    # print("error: edit_message_text")
                    # print('\033[0m')
                    continue
    except Exception as e:
        logger.warning("native stream failed: %s", e)
        api_key = Users.get_config(convo_id, "api_key")
        systemprompt = Users.get_config(convo_id, "systemprompt")
        if api_key:
            robot.reset(convo_id=convo_id, system_prompt=systemprompt)
        if "parse entities" in str(e):
            await context.bot.edit_message_text(chat_id=chatid, message_id=answer_messageid, text=tmpresult, disable_web_page_preview=True, read_timeout=time_out, write_timeout=time_out, pool_timeout=time_out, connect_timeout=time_out)
        else:
            tmpresult = f"{tmpresult}\n\n`{e}`"

    # 添加图片URL检测和发送
    if image_has_send == 0:
        image_extensions = r'(https?://[^\s<>\"()]+(?:\.(?:webp|jpg|jpeg|png|gif)|/image)[^\s<>\"()]*)'
        image_urls = re.findall(image_extensions, tmpresult, re.IGNORECASE)
        image_urls_result = [url[0] if isinstance(url, tuple) else url for url in image_urls]
        if image_urls_result:
            try:
                # Limit the number of images to 10 (Telegram limit for albums)
                image_urls_result = image_urls_result[:10]

                # We send an album with all images
                media_group = []
                for img_url in image_urls_result:
                    media_group.append(InputMediaPhoto(media=img_url))

                await context.bot.send_media_group(
                    chat_id=chatid,
                    media=media_group,
                    message_thread_id=message_thread_id,
                    reply_to_message_id=messageid,
                )
            except Exception as e:
                logger.warning(f"Failed to send image(s): {str(e)}")

    now_result = escape(tmpresult, italic=False)
    if lastresult != now_result and answer_messageid:
        if "Can't parse entities: can't find end of code entity at byte offset" in tmpresult:
            await update_message.reply_text(tmpresult)
            print(now_result)
        elif now_result:
            try:
                await context.bot.edit_message_text(chat_id=chatid, message_id=answer_messageid, text=now_result, parse_mode='MarkdownV2', disable_web_page_preview=True, read_timeout=time_out, write_timeout=time_out, pool_timeout=time_out, connect_timeout=time_out)
            except Exception as e:
                if "parse entities" in str(e):
                    await context.bot.edit_message_text(chat_id=chatid, message_id=answer_messageid, text=tmpresult, disable_web_page_preview=True, read_timeout=time_out, write_timeout=time_out, pool_timeout=time_out, connect_timeout=time_out)

    if Users.get_config(convo_id, "FOLLOW_UP") and tmpresult.strip():
        if title != "":
            info = "\n\n".join(tmpresult.split("\n\n")[1:])
        else:
            info = tmpresult
        prompt = (
            f"You are a professional Q&A expert. You will now be given reference information. Based on the reference information, please help me ask three most relevant questions that you most want to know from my perspective. Be concise and to the point. Do not have numbers in front of questions. Separate each question with a line break. Only output three questions in {language}, no need for any explanation. reference infomation is provided inside <infomation></infomation> XML tags."
            "Here is the reference infomation, inside <infomation></infomation> XML tags:"
            "<infomation>"
            "{}"
            "</infomation>"
        ).format(info)
        result = (await config.SummaryBot.ask_async(prompt, convo_id=convo_id, model=model_name, pass_history=0, api_url=api_url, api_key=api_key)).split('\n')
        keyboard = []
        result = [i for i in result if i.strip() and len(i) > 5]
        print(result)
        for ques in result:
            keyboard.append([KeyboardButton(ques)])
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)
        await update_message.reply_text(text=escape(tmpresult, italic=False), parse_mode='MarkdownV2', reply_to_message_id=messageid, reply_markup=reply_markup)
        await context.bot.delete_message(chat_id=chatid, message_id=answer_messageid)

@decorators.AdminAuthorization
@decorators.GroupAuthorization
@decorators.Authorization
async def button_press(update, context):
    """Function to handle the button press"""
    _, _, _, _, _, _, _, _, convo_id, _, _, _ = await GetMesageInfo(update, context)
    callback_query = update.callback_query
    info_message = update_info_message(convo_id)
    await callback_query.answer()
    data = callback_query.data
    banner = strings['message_banner'][get_current_lang(convo_id)]
    import telegram
    try:
        if data.endswith("_MODELS"):
            data = data[:-7]
            Users.set_config(convo_id, "engine", data)
            try:
                info_message = update_info_message(convo_id)
                message = await callback_query.edit_message_text(
                    text=escape(info_message + banner),
                    reply_markup=InlineKeyboardMarkup(update_models_buttons(convo_id)),
                    parse_mode='MarkdownV2'
                )
            except Exception as e:
                logger.info(e)
                pass
        elif data.endswith("_GROUP"):
            # Processing a click on a group of models
            group_name = data[:-6]
            try:
                message = await callback_query.edit_message_text(
                    text=escape(info_message + f"\n\n**{strings['group_title'][get_current_lang(convo_id)]}:** `{group_name}`"),
                    reply_markup=InlineKeyboardMarkup(update_models_buttons(convo_id, group=group_name)),
                    parse_mode='MarkdownV2'
                )
            except Exception as e:
                logger.info(e)
                pass
        elif data.startswith("MODELS"):
            message = await callback_query.edit_message_text(
                text=escape(info_message + banner),
                reply_markup=InlineKeyboardMarkup(update_models_buttons(convo_id)),
                parse_mode='MarkdownV2'
            )

        elif data.endswith("_LANGUAGES"):
            data = data[:-10]
            update_language_status(data, chat_id=convo_id)
            try:
                info_message = update_info_message(convo_id)
                message = await callback_query.edit_message_text(
                    text=escape(info_message, italic=False),
                    reply_markup=InlineKeyboardMarkup(update_menu_buttons(LANGUAGES, "_LANGUAGES", convo_id)),
                    parse_mode='MarkdownV2'
                )
            except Exception as e:
                logger.info(e)
                pass
        elif data.startswith("LANGUAGE"):
            message = await callback_query.edit_message_text(
                text=escape(info_message, italic=False),
                reply_markup=InlineKeyboardMarkup(update_menu_buttons(LANGUAGES, "_LANGUAGES", convo_id)),
                parse_mode='MarkdownV2'
            )

        if data.endswith("_PREFERENCES"):
            data = data[:-12]
            try:
                current_data = Users.get_config(convo_id, data)
                if data == "PASS_HISTORY":
                    if current_data == 0:
                        current_data = config.PASS_HISTORY or 9999
                    else:
                        current_data = 0
                    Users.set_config(convo_id, data, current_data)
                else:
                    Users.set_config(convo_id, data, not current_data)
            except Exception as e:
                logger.info(e)
            try:
                info_message = update_info_message(convo_id)
                message = await callback_query.edit_message_text(
                    text=escape(info_message, italic=False),
                    reply_markup=InlineKeyboardMarkup(update_menu_buttons(PREFERENCES, "_PREFERENCES", convo_id)),
                    parse_mode='MarkdownV2'
                )
            except Exception as e:
                logger.info(e)
                pass
        elif data.startswith("PREFERENCES"):
            message = await callback_query.edit_message_text(
                text=escape(info_message, italic=False),
                reply_markup=InlineKeyboardMarkup(update_menu_buttons(PREFERENCES, "_PREFERENCES", convo_id)),
                parse_mode='MarkdownV2'
            )

        if data.endswith("_PLUGINS"):
            data = data[:-8]
            try:
                current_data = Users.get_config(convo_id, data)
                Users.set_config(convo_id, data, not current_data)
            except Exception as e:
                logger.info(e)
            try:
                info_message = update_info_message(convo_id)
                message = await callback_query.edit_message_text(
                    text=escape(info_message, italic=False),
                    reply_markup=InlineKeyboardMarkup(update_menu_buttons(PLUGINS, "_PLUGINS", convo_id)),
                    parse_mode='MarkdownV2'
                )
            except Exception as e:
                logger.info(e)
                pass
        elif data.startswith("PLUGINS"):
            message = await callback_query.edit_message_text(
                text=escape(info_message, italic=False),
                reply_markup=InlineKeyboardMarkup(update_menu_buttons(PLUGINS, "_PLUGINS", convo_id)),
                parse_mode='MarkdownV2'
            )

        elif data.startswith("BACK"):
            message = await callback_query.edit_message_text(
                text=escape(info_message, italic=False),
                reply_markup=InlineKeyboardMarkup(update_first_buttons_message(convo_id)),
                parse_mode='MarkdownV2'
            )
    except telegram.error.BadRequest as e:
        from src.services.status import brief_net_error as _bneB
        _bneB(logger, "native edit", e)

@decorators.GroupAuthorization
@decorators.Authorization
@decorators.APICheck
async def handle_file(update, context):
    _, _, image_url, chatid, _, _, _, message_thread_id, convo_id, file_url, _, voice_text = await GetMesageInfo(update, context)
    robot, role, api_key, api_url = get_robot(convo_id)
    engine = Users.get_config(convo_id, "engine")

    if file_url == None and image_url:
        file_url = image_url
        if Users.get_config(convo_id, "IMAGEQA") == False:
            return
    if image_url == None and file_url:
        image_url = file_url
    engine_type, _ = get_engine({"base_url": api_url}, endpoint=None, original_model=engine)
    if robot.__class__.__name__ == "chatgpt":
        engine_type = "gpt"
    message = await Document_extract(file_url, image_url, engine_type)

    robot.add_to_conversation(message, role, convo_id)

    if Users.get_config(convo_id, "FILE_UPLOAD_MESS"):
        message = await context.bot.send_message(chat_id=chatid, message_thread_id=message_thread_id, text=escape(strings['message_doc'][get_current_lang(convo_id)]), parse_mode='MarkdownV2', disable_web_page_preview=True)
        await delete_message(update, context, [message.message_id])

@decorators.GroupAuthorization
@decorators.Authorization
@decorators.APICheck
async def inlinequery(update: Update, context) -> None:
    """New-mode inline: instant placeholder (no LLM wait, no timeouts).

    Tiers: unseen 5/hr, started 100/hr (switch_pm referral to /start).
    Full agent runs only after user SENDS (chosen_inline_result -> edit).
    """
    uid = update.effective_user.id
    if config.whitelist and str(uid) not in [str(x) for x in config.whitelist]:
        await update.inline_query.answer([], cache_time=5)
        return
    try:
        from src.services import inline_limits as _lim
        ok, limit, _u = _lim.allow(uid, False)
    except Exception:
        ok, limit = True, 100
    if not ok:
        await update.inline_query.answer(
            [], cache_time=10,
            switch_pm_text=f"Limit {limit}/hr. Tap to raise to 100",
            switch_pm_parameter="raise_limit")
        return
    query = (update.inline_query.query or "").strip()
    if not query:
        await update.inline_query.answer([], cache_time=5)
        return
    body = f"{query[:300]}\n\nProcessing…"
    try:
        from src.services.safeformat import format_one as _fmt
        safe_text, _pm = _fmt(body)
    except Exception:
        safe_text, _pm = body, None
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(
        "...", callback_data="inline_status")]])
    results = [InlineQueryResultArticle(
        id=str(uuid.uuid4()),
        title="Agent answer",
        description=query[:100],
        input_message_content=InputTextMessageContent(
            message_text=safe_text, parse_mode=_pm),
        reply_markup=kb)]
    await update.inline_query.answer(results, cache_time=0, is_personal=True)


async def chosen_inline(update: Update, context) -> None:
    """User SENT the placeholder -> run full agent (tools OK, no timeout)."""
    cir = update.chosen_inline_result
    uid = cir.from_user.id
    if config.whitelist and str(uid) not in [str(x) for x in config.whitelist]:
        return
    if not cir.inline_message_id:
        return
    asyncio.create_task(_chosen_agent_task(update, context))


async def _chosen_agent_task(update, context) -> None:
    import logging as _lg
    cir = update.chosen_inline_result
    uid = cir.from_user.id
    query = (cir.query or "").strip()
    mid = cir.inline_message_id
    if not query or not mid:
        return
    try:
        from src.services.agent_loop import deep_scrub as _scrub
    except Exception:
        _scrub = lambda t: t  # noqa
    try:
        robot, _role, api_key, api_url = get_robot(f"inline_{uid}")
        answer = await robot.ask_async(
            query, convo_id=f"inline_{uid}", model="local",
            pass_history=5, api_url=api_url, api_key=api_key,
            plugins=["run_python_script", "get_time"])
        answer = _scrub(answer or "")
        if not answer.strip():
            answer = "failed to answer."
        sent = f"{query[:300]}\n\n{answer}"
        try:
            from src.services import richdraft as _rdfC
            _blocks = _rdfC.md_to_blocks(sent[:30000])
            if _blocks and await asyncio.to_thread(
                    _rdfC.edit_rich_blocks, mid, _blocks):
                return
        except Exception:
            pass
        try:
            from src.services.safeformat import format_one as _fmt2
            safe_text, pm = _fmt2(sent[:3800])
        except Exception:
            safe_text, pm = sent[:3800], None
        await context.bot.edit_message_text(
            text=safe_text, inline_message_id=mid, parse_mode=pm)
    except Exception as e:
        _lg.getLogger(__name__).exception("chosen agent failed: %s", e)
        try:
            await context.bot.edit_message_text(
                text="agent error.",
                inline_message_id=mid)
        except Exception:
            pass


async def inline_status_cb(update: Update, context) -> None:
    try:
        await update.callback_query.answer(
            "Agent is already on it…", show_alert=False)
    except Exception:
        pass


async def guest_query(update: Update, context) -> None:
    """Guest Mode (Bot API 10.0): @mention in ANY chat -> full reply there.

    Needs BotFather Guest Mode ON. Pure chat (no plugins -> no 500s).
    """
    gm = getattr(update, "guest_message", None)
    if not gm or not getattr(gm, "guest_query_id", None):
        return
    qid = gm.guest_query_id
    text = ((getattr(gm, "text", None) or getattr(gm, "caption", None)) or "").strip()
    fu = getattr(gm, "from_user", None)
    uid = fu.id if fu else 0
    if config.whitelist and str(uid) not in [str(x) for x in config.whitelist]:
        return
    if not text:
        return
    reply_ctx = ""
    try:
        rm = getattr(gm, "reply_to_message", None)
        rt = ((getattr(rm, "text", None) or getattr(rm, "caption", None)) or "").strip() if rm else ""
        if rt:
            reply_ctx = rt[:800]
    except Exception:
        pass
    asyncio.create_task(_guest_answer(context, qid, text, uid, reply_ctx))


def guest_web_search(query: str, k: int = 4) -> str:
    """Direct DDG search (HTML then lite fallback). Context block or ''."""
    import re as _re3
    try:
        import httpx as _hx
    except Exception:
        return ""
    for base in ("https://html.duckduckgo.com/html/",
                 "https://lite.duckduckgo.com/lite/"):
        try:
            r = _hx.get(base, params={"q": query}, timeout=8,
                        headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code != 200:
                continue
            html = r.text
            links = _re3.findall(
                r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html,
                flags=_re3.DOTALL) or _re3.findall(
                r'<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html,
                flags=_re3.DOTALL)
            snips = _re3.findall(
                r'class="result__snippet"[^>]*>(.*?)</a>', html,
                flags=_re3.DOTALL)
            out = []
            for i, (href, title) in enumerate(links[:k]):
                if href.startswith("/") or "duckduckgo" in href:
                    continue
                t = _re3.sub(r"<[^>]+>", "", title).strip()
                s = _re3.sub(r"<[^>]+>", "",
                             snips[i] if i < len(snips) else "").strip()
                if t:
                    out.append(f"- {t} ({href[:120]}): {s[:250]}")
            if out:
                return "[Web search results:]\n" + "\n".join(out)
        except Exception:
            continue
    return ""


def guest_weather(query: str) -> str:
    """wttr.in fast path for weather questions. Returns context or ''."""
    import re as _re4
    q = query.lower()
    if not any(w in q for w in ("погод", "weather", "температур", "дождь",
                                "градус", "прогноз")):
        return ""
    city = ""
    m = _re4.search(r"\b[вв]о?\s+([А-ЯЁA-Z][а-яёa-z\-]{2,})", query)
    if not m:
        m = _re4.search(r"\bin\s+([A-Z][a-z\-]{2,})", query)
    if m:
        city = m.group(1)
    if not city:
        return ""
    try:
        import httpx as _hx2
        r = _hx2.get(f"https://wttr.in/{city}?format=j1", timeout=8,
                     headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return ""
        cur = (r.json().get("current_condition") or [{}])[0]
        t = cur.get("temp_C", "?")
        d = (cur.get("weatherDesc") or [{}])[0].get("value", "")
        hum = cur.get("humidity", "?")
        return (f"[Current weather in {city}: {t}°C, {d}, "
                f"humidity {hum}% (via wttr.in)]")
    except Exception:
        return ""


async def _guest_answer(context, qid: str, text: str, uid: int,
                        reply_ctx: str = "") -> None:
    """Guest via smolagents (search tools, no tools-param) with visible progress.

    Placeholder post -> throttled progress edits -> final edit. Stateless.
    """
    from src.services.status import run_staged
    prompt = text
    if reply_ctx:
        prompt = (f"[Replied-to message context:]\n{reply_ctx}\n\n"
                  f"[User request:]\n{text}")

    import uuid as _uu
    cancel_ev = asyncio.Event()

    async def _post(t, pm=None):
        r = InlineQueryResultArticle(
            id=str(uuid.uuid4()), title="Answer",
            description=text[:100],
            input_message_content=InputTextMessageContent(
                message_text=t[:3800], parse_mode=pm))
        sent = await context.bot.answer_guest_query(qid, r)
        return getattr(sent, "inline_message_id", None)

    async def _edit(mid, t, pm=None):
        if not mid:
            return
        try:
            await context.bot.edit_message_text(
                text=t[:3800], inline_message_id=mid, parse_mode=pm)
        except Exception as e1:
            try:
                await context.bot.edit_message_text(
                    text=t[:3800], inline_message_id=mid)
            except Exception as e2:
                nonlocal _edit_err_n
                _edit_err_n += 1
                if _edit_err_n <= 2:
                    logger.warning("guest progress edit failed: %s / %s",
                                   e1, e2)

    async def _remove(mid):
        return

    async def _finalize(t):
        return

    mid = None
    _edit_err_n = 0
    try:
        mid = await _post("`processing...`", "MarkdownV2")
        logger.info("guest placeholder ok, mid=%s", mid)
    except Exception as e:
        from src.services.status import brief_net_error as _bne1
        _bne1(logger, "guest placeholder", e)
        try:
            pass
        except Exception:
            pass
        return
    if not mid:
        try:
            from src.services import smo_agent as _smo0
            import asyncio as _aio0
            final0 = await _aio0.to_thread(
                _smo0.run_task, prompt, uid, 0, True, 120, None)
            from src.services.agent_loop import deep_scrub as _sc0
            final0 = clean_answer(_sc0(final0 or "")) or "failed to answer."
            try:
                final0 = _smo0.final_clean(final0)
            except Exception:
                pass
            if final0.strip() != "failed to answer.":
                try:
                    _smo0.remember(uid, 0, text, final0)
                except Exception:
                    pass
            if final0.count("```") % 2 == 1:
                final0 += "\n```"
            try:
                safe0 = escape(final0, italic=False)
                pm0 = "MarkdownV2"
            except Exception:
                safe0, pm0 = final0[:3800], None
            r0 = InlineQueryResultArticle(
                id=str(uuid.uuid4()), title="Ответ",
                description=(final0 or "")[:100],
                input_message_content=InputTextMessageContent(
                    message_text=safe0[:3800], parse_mode=pm0))
            await context.bot.answer_guest_query(qid, r0)
        except Exception as e:
            from src.services.status import brief_net_error as _bne2
            _bne2(logger, "guest direct", e)
        return
    sink: list = []
    from src.services import smo_agent as _smog

    async def _run2():
        return await asyncio.to_thread(
            _smog.run_task, prompt, uid, 0, True, 180, sink)

    async def _post2(t):
        return mid

    try:
        final = await run_staged(_post2, _edit, _remove, _finalize,
                                 _run2, sink, cancel_event=cancel_ev)
    except asyncio.CancelledError:
        try:
            await context.bot.edit_message_text(
                text="`[stopped]`", inline_message_id=mid,
                parse_mode="MarkdownV2",
                reply_markup=InlineKeyboardMarkup([]))
        except Exception:
            pass
        try:
            pass
        except Exception:
            pass
        return
    except Exception as e:
        from src.services.status import brief_net_error as _bne3
        _bne3(logger, "guest agent", e)
        final = ""

    try:
        from src.services.agent_loop import deep_scrub as _scrub2
        final = _scrub2(final or "")
    except Exception:
        pass
    final = clean_answer(final or "")
    try:
        final = _smog.final_clean(final)
    except Exception:
        pass
    if not final.strip():
        final = "failed to answer."
    else:
        try:
            from src.services import smo_agent as _smoG2
            _smoG2.remember(uid, 0, text, final)
        except Exception:
            pass
    if final.count("```") % 2 == 1:
        final += "\n```"
    try:
        from src.services import richdraft as _rdfG
        _blocks = _rdfG.md_to_blocks(final)
        if _blocks and await asyncio.to_thread(
                _rdfG.edit_rich_blocks, mid, _blocks):
            return
    except Exception:
        pass
    try:
        safe = escape(final, italic=False)
        await context.bot.edit_message_text(
            text=safe[:3800], inline_message_id=mid, parse_mode="MarkdownV2",
            reply_markup=InlineKeyboardMarkup([]))
    except Exception:
        try:
            await context.bot.edit_message_text(
                text=final[:3800], inline_message_id=mid,
                reply_markup=InlineKeyboardMarkup([]))
        except Exception as e:
            from src.services.status import brief_net_error as _bne4
            _bne4(logger, "guest final edit", e)

@decorators.GroupAuthorization
@decorators.Authorization
async def change_model(update, context):
    """Quick model change using the command"""
    _, _, _, chatid, user_message_id, _, _, message_thread_id, convo_id, _, _, _ = await GetMesageInfo(update, context)
    lang = get_current_lang(convo_id)

    if not context.args:
        message = await context.bot.send_message(
            chat_id=chatid,
            message_thread_id=message_thread_id,
            text=escape(strings['model_command_usage'][lang]),
            parse_mode='MarkdownV2',
            reply_to_message_id=user_message_id,
        )
        return

    # Combine all arguments into one model name
    model_name = ' '.join(context.args)

    # Check if the model name is valid (allowing all common model name characters)
    if not re.match(r'^[a-zA-Z0-9\-_\./:\\@+\s]+$', model_name) or len(model_name) > 100:
        message = await context.bot.send_message(
            chat_id=chatid,
            message_thread_id=message_thread_id,
            text=escape(strings['model_name_invalid'][lang]),
            parse_mode='MarkdownV2',
            reply_to_message_id=user_message_id,
        )
        return

    # Get all available models from initial_model and MODEL_GROUPS
    available_models = get_all_available_models()
    for group_name, models in get_model_groups().items():
        available_models.extend(models)

    # Add debug output
    print(f"Requested model: '{model_name}'")
    print(f"Available models: {available_models}")

    # Check if the requested model is in the available models list
    if model_name not in available_models:
        message = await context.bot.send_message(
            chat_id=chatid,
            message_thread_id=message_thread_id,
            text=escape(strings['model_not_available'][lang].format(model_name=model_name)),
            parse_mode='MarkdownV2',
            reply_to_message_id=user_message_id,
        )
        return

    # Saving the new model in the user's configuration
    Users.set_config(convo_id, "engine", model_name)

    # Sending a message about changing the model
    message = await context.bot.send_message(
        chat_id=chatid,
        message_thread_id=message_thread_id,
        text=escape(strings['model_changed'][lang].format(model_name=model_name), italic=False),
        parse_mode='MarkdownV2',
        reply_to_message_id=user_message_id,
    )

async def scheduled_function(context: ContextTypes.DEFAULT_TYPE) -> None:
    """这个函数将在RESET_TIME秒后执行一次，重置特定用户的对话"""
    job = context.job
    chat_id = job.chat_id

    if config.ADMIN_LIST and str(chat_id) in [str(x) for x in config.ADMIN_LIST]:
        return

    reset_ENGINE(chat_id)

    # 任务执行完毕后自动移除
    remove_job_if_exists(str(chat_id), context)

def remove_job_if_exists(name: str, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """如果存在，则移除指定名称的任务"""
    current_jobs = context.job_queue.get_jobs_by_name(name)
    if not current_jobs:
        return False
    for job in current_jobs:
        job.schedule_removal()
    return True

# 定义一个全局变量来存储 chatid
target_convo_id = None
reset_mess_id = 9999

@decorators.GroupAuthorization
@decorators.Authorization
async def reset_chat(update, context):
    global target_convo_id, reset_mess_id
    _, _, _, chatid, user_message_id, _, _, message_thread_id, convo_id, _, _, _ = await GetMesageInfo(update, context)
    try:
        target_convo_id = convo_id
        try:
            reset_mess_id = user_message_id or 0
        except Exception:
            pass
        try:
            stop_event.set()
        except Exception:
            pass
        try:
            reset_ENGINE(convo_id, None)
        except Exception:
            pass
        try:
            from src.services import smo_agent as _smoR
            fu = getattr(update, "effective_user", None)
            if fu:
                _smoR.reset_user(fu.id)
        except Exception:
            pass
        try:
            await context.bot.send_message(
                chat_id=chatid,
                message_thread_id=message_thread_id,
                text="`[context cleared]`", parse_mode="MarkdownV2")
        except Exception:
            try:
                await context.bot.send_message(
                    chat_id=chatid,
                    message_thread_id=message_thread_id,
                    text="[context cleared]")
            except Exception:
                pass
    except Exception:
        pass


@decorators.GroupAuthorization
@decorators.AdminAuthorization
async def cmd_admin(update, context) -> None:
    """Admin switch: open bot to everyone vs admin-only. Persists to .env.

    Usage: /admin open | /admin close | /admin status (no args = toggle).
    """
    try:
        import config as _C
        parts = ((getattr(update.message, "text", None) or "")).split()
        arg = parts[1].lower() if len(parts) > 1 else "toggle"
        cur_open = _C.whitelist is None
        if arg in ("open", "all", "on", "1", "everyone"):
            want_open = True
        elif arg in ("close", "closed", "off", "0", "admin", "me",
                     "private"):
            want_open = False
        elif arg in ("status", "st", "?"):
            want_open = cur_open
        else:
            want_open = not cur_open
        try:
            adm = (_C.ADMIN_LIST[0] if _C.ADMIN_LIST else "1346796505")
        except Exception:
            adm = "1346796505"
        _C.whitelist = None if want_open else [str(adm)]
        try:
            import os as _os
            envp = _os.path.join(
                _os.path.dirname(_os.path.abspath(__file__)), ".env")
            with open(envp, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()
            val = "" if want_open else str(adm)
            seen = False
            for i, ln in enumerate(lines):
                if ln.startswith("whitelist="):
                    lines[i] = "whitelist=" + val
                    seen = True
            if not seen:
                lines.append("whitelist=" + val)
            with open(envp, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        except Exception:
            pass
        mode = "OPEN to everyone" if want_open else "ADMIN-ONLY"
        try:
            await update.message.reply_text(f"`[mode: {mode}]`",
                                            parse_mode="MarkdownV2")
        except Exception:
            try:
                await update.message.reply_text(f"[mode: {mode}]")
            except Exception:
                pass
    except Exception:
        pass


async def cmd_ctx(update, context) -> None:
    """Show session context usage (smo transcript, est tokens vs window)."""
    try:
        u = update.effective_user
        ch = update.effective_chat
        uid = u.id if u else 0
        cid = ch.id if ch else uid
        from src.services import smo_agent as _sm
        pairs = _sm.load_transcript(uid, cid)
        chars = sum(len(p.get("q", "")) + len(p.get("a", "")) for p in pairs)
        est = chars // 4
        win = 131072
        pct = min(99, int(est * 100 / win)) if win else 0
        txt = (f"Context: ~{est} tok / {win} ({pct}%)\n"
               f"Turns kept: {len(pairs)}")
        await update.message.reply_text(f"`{txt}`", parse_mode="MarkdownV2")
    except Exception:
        pass


@decorators.GroupAuthorization
@decorators.AdminAuthorization
async def cmd_models(update, context) -> None:
    """Fetch model list from active API + pick buttons (admin)."""
    try:
        from src.services import llm_config as _lcM
        from telegram import InlineKeyboardButton as _BM
        from telegram import InlineKeyboardMarkup as _MM
        base, key, cur = _lcM.get_active()
        try:
            ids = await asyncio.to_thread(_lcM.list_models, base, key)
        except Exception as e:
            await update.message.reply_text(f"[api unreachable: {e}]")
            return
        _lcM.set_pending(update.effective_user.id, ids)
        shown = [(i, m) for i, m in enumerate(ids)
                 if _lcM.allowed_by_cap(m)][:10]
        kb = [[_BM(m[:60], callback_data=f"model_pick:{i}")]
              for i, m in shown]
        listed = [m for m in ids if _lcM.allowed_by_cap(m)]
        hidden = len(ids) - len(listed)
        txt = (f"API: {base}\nCurrent: {cur}\n"
               f"Models ({len(listed)} allowed"
               + (f", {hidden} over class limit" if hidden else "") + "):\n"
               + "\n".join(f"{i + 1}. {m[:70]}"
                           for i, m in enumerate(listed[:15])))
        if len(listed) > 15:
            txt += f"\n... +{len(listed) - 15} more, use /setmodel <exact-id>"
        await update.message.reply_text(
            txt[:3800], reply_markup=_MM(kb) if kb else None)
    except Exception:
        pass


@decorators.GroupAuthorization
@decorators.AdminAuthorization
async def cmd_setapi(update, context) -> None:
    """/setapi <base_url> [api_key] — switch backend, auto-list models."""
    try:
        from src.services import llm_config as _lcS
        parts = ((getattr(update.message, "text", None) or "")).split()
        if len(parts) < 2:
            b, _, m = _lcS.get_active()
            await update.message.reply_text(
                f"Usage: /setapi <base_url> [api_key]\nNow: {b} / {m}")
            return
        base = parts[1].rstrip("/")
        cur_base, cur_key, _ = _lcS.get_active()
        key = parts[2] if len(parts) > 2 else (
            cur_key if base == cur_base else "sk-noop")
        try:
            ids = await asyncio.to_thread(_lcS.list_models, base, key)
        except Exception as e:
            _lcS.persist(base, key, "")
            try:
                from src.services import smo_agent as _smoS
                _smoS.refresh_llm()
            except Exception:
                pass
            await update.message.reply_text(
                f"[endpoint saved, list failed: {e}]\n"
                "Set model manually: /setmodel <exact-id>")
            return
        _lcS.persist(base, key, "")
        try:
            from src.services import smo_agent as _smoS2
            _smoS2.refresh_llm()
        except Exception:
            pass
        _lcS.set_pending(update.effective_user.id, ids)
        from telegram import InlineKeyboardButton as _BS
        from telegram import InlineKeyboardMarkup as _MS
        kb = [[_BS(m[:60], callback_data=f"model_pick:{i}")]
              for i, m in enumerate(ids[:10])]
        await update.message.reply_text(
            f"Backend: {base}\nPick a model:"
            [:500] + "\n" + "\n".join(
                f"{i + 1}. {m[:70]}" for i, m in enumerate(ids[:15]))[:3300],
            reply_markup=_MS(kb) if kb else None)
    except Exception:
        pass


@decorators.GroupAuthorization
@decorators.AdminAuthorization
async def cmd_setmodel(update, context) -> None:
    """/setmodel <exact-id> — manual model set (fetch failed path)."""
    try:
        from src.services import llm_config as _lcT
        parts = ((getattr(update.message, "text", None) or "")).split(None, 1)
        if len(parts) < 2 or not parts[1].strip():
            _, _, m = _lcT.get_active()
            await update.message.reply_text(
                f"Usage: /setmodel <exact-id>\nCurrent: {m}")
            return
        mid = parts[1].strip()
        if not _lcT.allowed_by_cap(mid):
            await update.message.reply_text("[over class limit]")
            return
        _lcT.persist("", "", mid)
        try:
            from src.services import smo_agent as _smoT
            b, _, mm = _smoT.refresh_llm()
        except Exception:
            b, _, mm = _lcT.get_active()
        await update.message.reply_text(f"[model: {mm} @ {b}]")
    except Exception:
        pass


@decorators.GroupAuthorization
@decorators.AdminAuthorization
async def cmd_setcd(update, context) -> None:
    """/setcd [private_secs group_secs] — view/set anti-spam cooldowns."""
    try:
        import os as _os
        parts = ((getattr(update.message, "text", None) or "")).split()
        if len(parts) >= 3:
            try:
                pv, gv = max(0.0, float(parts[1])), max(0.0, float(parts[2]))
            except Exception:
                await update.message.reply_text("Usage: /setcd [private group]")
                return
            try:
                import os as _osP
                envp = _osP.path.join(
                    _osP.path.dirname(_osP.path.abspath(__file__)), ".env")
                with open(envp, "r", encoding="utf-8") as f:
                    lines = f.read().splitlines()
                vals = {"CD_PRIVATE": str(pv), "CD_GROUP": str(gv)}
                seen = set()
                for i, ln in enumerate(lines):
                    k = ln.split("=", 1)[0].strip()
                    if k in vals:
                        lines[i] = k + "=" + vals[k]
                        seen.add(k)
                for k, v in vals.items():
                    if k not in seen:
                        lines.append(k + "=" + v)
                with open(envp, "w", encoding="utf-8") as f:
                    f.write("\n".join(lines) + "\n")
            except Exception:
                pass
            _os.environ["CD_PRIVATE"] = str(pv)
            _os.environ["CD_GROUP"] = str(gv)
            await update.message.reply_text(f"[cooldowns: private={pv}s group={gv}s]")
            return
        pv = _os.getenv("CD_PRIVATE", "15")
        gv = _os.getenv("CD_GROUP", "20")
        await update.message.reply_text(
            f"Cooldowns: private={pv}s group={gv}s\nUsage: /setcd [private group]")
    except Exception:
        pass


async def model_pick_cb(update, context) -> None:
    """Model pick button (admin only, expires with pending list)."""
    try:
        q = getattr(update, "callback_query", None)
        data = (getattr(q, "data", None) or "")
        if not data.startswith("model_pick:"):
            return
        import config as _CC
        uid = update.effective_user.id
        adm = _CC.ADMIN_LIST or []
        if adm and str(uid) not in [str(x) for x in adm]:
            try:
                await q.answer("Admin only.")
            except Exception:
                pass
            return
        try:
            idx = int(data.split(":")[1])
        except Exception:
            return
        from src.services import llm_config as _lcP
        ids = _lcP.pop_pending(uid)
        if not ids or idx < 0 or idx >= len(ids):
            try:
                await q.answer("Expired, run /models again.")
            except Exception:
                pass
            return
        if not _lcP.allowed_by_cap(ids[idx]):
            try:
                await q.answer("Over class limit.")
            except Exception:
                pass
            return
        _lcP.persist("", "", ids[idx])
        try:
            from src.services import smo_agent as _smoP
            b, _, mm = _smoP.refresh_llm()
        except Exception:
            b, _, mm = _lcP.get_active()
        try:
            await q.answer(f"Model: {mm[:50]}")
        except Exception:
            pass
        try:
            await q.message.reply_text(f"[model: {mm} @ {b}]")
        except Exception:
            pass
    except Exception:
        pass


async def reset_confirm(update, context) -> None:
    global target_convo_id, reset_mess_id
    try:
        q = getattr(update, "callback_query", None)
        data = (getattr(q, "data", None) or "")
        if not data.startswith("reset:"):
            return
        parts = data.split(":")
        if len(parts) < 2:
            return
        if parts[1] == "no":
            try:
                await q.message.delete()
            except Exception:
                pass
            try:
                await q.answer("Cancelled.")
            except Exception:
                pass
            return
        convo_id = parts[2] if len(parts) > 2 else None
        if not convo_id:
            return
        chatid = q.message.chat.id if q.message else 0
        try:
            reset_mess_id = q.message.message_id if q.message else 0
        except Exception:
            pass
        target_convo_id = convo_id
        stop_event.set()
        reset_ENGINE(target_convo_id, None)
        try:
            from src.services import smo_agent as _smoR2
            fu = getattr(update, "effective_user", None)
            if fu:
                _smoR2.reset_user(fu.id)
        except Exception:
            pass
        try:
            await q.answer("Context cleared.")
        except Exception:
            pass
        try:
            await q.message.delete()
        except Exception:
            pass
        try:
            chat_id = q.message.chat.id if q.message else 0
            if chat_id:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="`[context cleared]`", parse_mode="MarkdownV2")
        except Exception:
            pass
    except Exception as e:
        from src.services.status import brief_net_error as _bne5
        _bne5(logger, "reset_confirm", e)


@decorators.AdminAuthorization
@decorators.GroupAuthorization
@decorators.Authorization
async def info(update, context):
    _, _, _, chatid, user_message_id, _, _, message_thread_id, convo_id, _, _, voice_text = await GetMesageInfo(update, context)
    info_message = update_info_message(convo_id)
    message = await context.bot.send_message(
        chat_id=chatid,
        message_thread_id=message_thread_id,
        text=escape(info_message, italic=False),
        reply_markup=InlineKeyboardMarkup(update_first_buttons_message(convo_id)),
        parse_mode='MarkdownV2',
        disable_web_page_preview=True,
        read_timeout=time_out,
    )
    await delete_message(update, context, [message.message_id, user_message_id])

@decorators.GroupAuthorization
@decorators.Authorization
async def start(update, context): # 当用户输入/start时，返回文本
    _, _, _, _, _, _, _, _, convo_id, _, _, _ = await GetMesageInfo(update, context)
    user = update.effective_user
    update_language_status("English", chat_id=convo_id)
    bname = getattr(getattr(context, "bot", None), "username", "") or "this bot"
    message = (
        f"Hi `{user.username}`! I am an AI assistant on a local model.\n"
        "Send me a task or a file and I will handle it.\n\n"
        "Commands\n"
        "/reset — clear chat context\n"
        "/ctx — context usage\n"
        "/admin open|close|status — access for all / admin-only / status\n"
        "/models — list models from the active API\n"
        "/setapi <url> [key] — switch backend\n"
        "/setmodel <id> — set model manually\n"
        "/setcd [private group] — anti-spam cooldowns\n\n"
        "Groups & guests\n"
        f"Mention me (@{bname}) in any group or reply to my message — "
        "I answer right there.\n"
        "Guest mode: tag me even in chats where I'm not a member.\n"
        f"Inline: type @{bname} + query in any chat.\n\n"
        "Notes\n"
        "- Send stop or cancel to abort a running task.\n"
        "- Cooldown 15s (private) / 20s (groups) between answers.\n"
        "- Long texts are trimmed to 4000 chars for the agent.\n"
        "- This bot is open source: "
        "https://github.com/Fakeonomics/llm_tg_bot\n"
    )
    if context.args and context.args[0] == "raise_limit":
        try:
            from src.services import inline_limits as _lim2
            _lim2.mark_seen(user.id)
        except Exception:
            pass
        message += "Done: inline limit raised to 100/hr. "
    # NOTE: /start API-key override removed (locked to local backend).

    # message = (
    #     ">Block quotation started\n"
    #     ">Block quotation continued\n"
    #     ">Block quotation continued\n"
    #     ">Block quotation continued\n"
    #     ">The last line of the block quotation\n"
    #     "**>The expandable block quotation started right after the previous block quotation\n"
    #     ">It is separated from the previous block quotation by an empty bold entity\n"
    #     ">Expandable block quotation continued\n"
    #     ">Hidden by default part of the expandable block quotation started\n"
    #     ">Expandable block quotation continued\n"
    #     ">The last line of the expandable block quotation with the expandability mark||\n"
    # )
    # await update.message.reply_text(message, parse_mode='MarkdownV2', disable_web_page_preview=True)
    await update.message.reply_text(escape(message, italic=False), parse_mode='MarkdownV2', disable_web_page_preview=True)

async def error(update, context):
    traceback_string = "".join(
        traceback.format_exception(None, context.error,
                                   context.error.__traceback__))
    if "telegram.error.TimedOut: Timed out" in traceback_string:
        logger.warning('error: telegram.error.TimedOut: Timed out')
        return
    if "Message to be replied not found" in traceback_string:
        logger.warning('error: telegram.error.BadRequest: Message to be replied not found')
        return
    # Harmless races (edit/delete of an already-gone message, stale
    # query, blocked bot): one line, no traceback.
    _harmless = ("Message_id_invalid", "Message to delete not found",
                 "Message to edit not found", "message is not modified",
                 "Message can't be edited", "Query is too old",
                 "query is too old", "Bot was blocked by the user",
                 "User is deactivated", "bot was kicked")
    for _h in _harmless:
        if _h in traceback_string:
            logger.warning('harmless tg error (suppressed): %s', _h)
            return
    # Flaky net (storm/VPN): connection drops are routine — one line,
    # no multi-screen tracebacks. Real bugs still log fully below.
    try:
        from telegram.error import NetworkError as _NetErr
        import httpx as _httpx
        _net_types = (_NetErr, ConnectionError, TimeoutError,
                      _httpx.ConnectError, _httpx.TimeoutException,
                      _httpx.NetworkError, _httpx.RemoteProtocolError)
    except Exception:
        _net_types = (ConnectionError, TimeoutError)
    if isinstance(context.error, _net_types):
        logger.warning('net hiccup (suppressed): %s',
                       type(context.error).__name__)
        return
    logger.warning('Update "%s" caused error "%s"', update, context.error)
    logger.warning('Error traceback: %s', traceback_string)

@decorators.GroupAuthorization
@decorators.Authorization
async def unknown(update, context): # 当用户输入未知命令时，返回文本
    return
    # await context.bot.send_message(chat_id=update.effective_chat.id, text="Sorry, I didn't understand that command.")

async def post_init(application: Application) -> None:
    if GET_MODELS:
        await get_initial_model()
    await application.bot.set_my_commands([
        BotCommand('start', 'Start the bot'),
        BotCommand('reset', 'Clear chat context'),
        BotCommand('ctx', 'Show context usage'),
        BotCommand('admin', 'Access mode (admin)'),
        BotCommand('models', 'List models from the active API (admin)'),
        BotCommand('setapi', 'Switch backend (admin)'),
        BotCommand('setmodel', 'Set model manually (admin)'),
        BotCommand('setcd', 'Anti-spam cooldowns (admin)'),
    ])
    description = (
        "I am an AI assistant. Send me a task or a file."
    )
    await application.bot.set_my_description(description)

if __name__ == '__main__':
    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .connection_pool_size(65536)
        .get_updates_connection_pool_size(65536)
        .read_timeout(time_out)
        .write_timeout(time_out)
        .connect_timeout(time_out)
        .pool_timeout(time_out)
        .get_updates_read_timeout(poll_time_out)
        .get_updates_write_timeout(poll_time_out)
        .get_updates_connect_timeout(poll_time_out)
        .get_updates_pool_timeout(poll_time_out)
        .rate_limiter(AIORateLimiter(max_retries=2))
        .post_init(post_init)
        .build()
    )

    application.add_handler(CommandHandler("info", info))
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("reset", reset_chat))
    application.add_handler(CommandHandler("admin", cmd_admin))
    application.add_handler(CommandHandler("models", cmd_models))
    application.add_handler(CommandHandler("setapi", cmd_setapi))
    application.add_handler(CommandHandler("setmodel", cmd_setmodel))
    application.add_handler(CommandHandler("setcd", cmd_setcd))
    application.add_handler(CallbackQueryHandler(
        model_pick_cb, pattern="^model_pick:"))
    application.add_handler(CommandHandler("ctx", cmd_ctx))
    application.add_handler(CommandHandler("model", change_model))
    application.add_handler(InlineQueryHandler(inlinequery))
    application.add_handler(ChosenInlineResultHandler(chosen_inline))
    application.add_handler(TypeHandler(Update, guest_query))
    application.add_handler(CallbackQueryHandler(
        inline_status_cb, pattern="^inline_status$"))
    from smo_bridge import smo_photo, smo_group_mention
    from smo_bridge import smo_private as _smo_private
    application.add_handler(MessageHandler(
        filters.TEXT, _smo_private), group=-1)
    from smo_bridge import cancel_cb as _smo_cancel_cb
    application.add_handler(CallbackQueryHandler(
        _smo_cancel_cb, pattern="^cancel:"))
    application.add_handler(CallbackQueryHandler(
        reset_confirm, pattern="^reset:"))

    async def _reaction_cb(update, context) -> None:
        try:
            mr = getattr(update, "message_reaction", None)
            if not mr:
                return
            emojis = []
            for r in (getattr(mr, "new_reaction", []) or []):
                e = getattr(r, "emoji", None)
                if e:
                    emojis.append(e)
            if not emojis:
                return
            u = getattr(update, "effective_user", None)
            logger.info("reaction %s from %s msg %s",
                        ",".join(emojis), u.id if u else 0,
                        getattr(mr, "message_id", 0))
        except Exception:
            pass

    try:
        from telegram.ext import MessageReactionHandler as _MRH
        application.add_handler(_MRH(_reaction_cb))
    except Exception as e:
        logger.warning("reaction handler unavailable: %s", e)
    application.add_handler(MessageHandler(
        filters.PHOTO, smo_photo), group=-1)
    application.add_handler(MessageHandler(
        filters.TEXT, smo_group_mention), group=-1)
    application.add_handler(CallbackQueryHandler(button_press))
    application.add_handler(MessageHandler((filters.TEXT | filters.VOICE) & ~filters.COMMAND, lambda update, context: command_bot(update, context, has_command=False), block = False))
    application.add_handler(MessageHandler(
        filters.CAPTION &
        (
            (filters.PHOTO & ~filters.COMMAND) |
            (
                filters.Document.PDF |
                filters.Document.TXT |
                filters.Document.DOC |
                filters.Document.FileExtension("jpg") |
                filters.Document.FileExtension("jpeg") |
                filters.Document.FileExtension("png") |
                filters.Document.FileExtension("md") |
                filters.Document.FileExtension("py") |
                filters.Document.FileExtension("yml")
            )
        ), lambda update, context: command_bot(update, context, has_command=False)))
    application.add_handler(MessageHandler(
        ~filters.CAPTION &
        (
            (filters.PHOTO & ~filters.COMMAND) |
            (
                filters.Document.PDF |
                filters.Document.TXT |
                filters.Document.DOC |
                filters.Document.FileExtension("jpg") |
                filters.Document.FileExtension("jpeg") |
                filters.Document.FileExtension("png") |
                filters.Document.FileExtension("md") |
                filters.Document.FileExtension("py") |
                filters.Document.FileExtension("yml") |
                filters.AUDIO |
                filters.Document.FileExtension("wav")
            )
        ), handle_file))
    application.add_handler(MessageHandler(filters.COMMAND, unknown))
    application.add_error_handler(error)

    if WEB_HOOK:
        print("WEB_HOOK:", WEB_HOOK)
        application.run_webhook("0.0.0.0", PORT, webhook_url=WEB_HOOK)
    else:
        application.run_polling(timeout=poll_time_out,
                                 allowed_updates=Update.ALL_TYPES,
                                 drop_pending_updates=False)
