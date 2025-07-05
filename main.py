import os
import json
import aiohttp
import asyncio
import datetime
from datetime import timedelta
from collections import defaultdict
import threading
from aiohttp import web
import socket
from urllib.parse import urlparse, parse_qs
import sys
from queue import Queue
from pathlib import Path
import re
import codecs
import time
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register, StarTools
import astrbot.api
from astrbot.api import logger

# --- 插件介绍代码内容 ---
@register("astrbot_plugin_mp", "EWEDL", "MoviePilot小工具", "1.5.0")
class MediaSearchPlugin(Star):
    """集成媒体搜索、订阅管理以及基于HTTP的分类消息通知功能。
    支持持久化订阅和文件日志。"""
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        self._init_paths()
        self._init_logging()
        self._load_api_config()
        self._load_display_notification_config()
        self.access_token = None
        self.token_refresh_count = 0
        self.user_search_results = {}
        self.notification_subscriptions = defaultdict(set)
        self.message_queue = Queue()
        self.httpd = None
        self.server_thread = None
        self.message_processor_task = None
        self.server_stop_event = threading.Event()
        self.user_states = {}  # 多轮交互状态
        self.pending_subscription = {}  # 记录待处理的多季订阅信息
        self.logger.info(f"Subscription persistence file path: {self.subscriptions_file}")

    def _init_paths(self):
        """初始化插件所需的路径配置"""
        try:
            self.data_dir = StarTools.get_data_dir()
            self.plugin_dir = self.data_dir
            self.subscriptions_file = self.data_dir / "mp_sub.json"
            self.log_file_path = self.data_dir / "http.log"
        except Exception:
            self.plugin_dir = Path(".")
            self.subscriptions_file = Path("./mp_sub.json")
            self.log_file_path = Path("./http.log")

    def _init_logging(self):
        """初始化日志系统，使用框架logger，不再自定义文件日志"""
        self.logger = logger
        self.logger.info(f"详细日志系统初始化完成，保存在框架日志中")

    def _check_and_clear_daily_log(self):
        """已弃用：每日清空日志逻辑，交由框架日志管理"""
        pass

    def _log_and_check_daily(self, level, msg, *args, **kwargs):
        """写日志，直接调用框架logger"""
        if level == 'debug':
            self.logger.debug(msg, *args, **kwargs)
        elif level == 'info':
            self.logger.info(msg, *args, **kwargs)
        elif level == 'warning':
            self.logger.warning(msg, *args, **kwargs)
        elif level == 'error':
            self.logger.error(msg, *args, **kwargs)
        elif level == 'exception':
            self.logger.exception(msg, *args, **kwargs)
        else:
            self.logger.info(msg, *args, **kwargs)

    def _load_api_config(self):
        """加载API配置"""
        api_config = self.config.get("api_config", {})
        self.username = api_config.get("username", "")
        self.password = api_config.get("password", "")
        self.base_url = api_config.get("base_url", "").rstrip('/')
        
        if not self.base_url:
            self.logger.warning("MP base_url未配置")
            
        self.token_url = f"{self.base_url}/api/v1/login/access-token" if self.base_url else ""
        self.subscribe_url = f"{self.base_url}/api/v1/subscribe/" if self.base_url else ""

    def _load_display_notification_config(self):
        """加载显示和通知配置"""
        # 加载搜索结果设置
        display_config = self.config.get("display_config", {})
        self.max_results = display_config.get("max_results", 5)
        
        # 加载通知设置
        notification_config = self.config.get("notification_config", {})
        
        # 从notification_config加载HTTP转发功能开关
        self.http_forward_enabled = notification_config.get("enabled", False)
        
        # 加载HTTP监听端口
        self.listen_port = notification_config.get("listen_port", 8080)
        
        try:
            port_num = int(self.listen_port)
            if not (1024 <= port_num <= 65535):
                raise ValueError("Port out of range")
            self.listen_port = port_num
        except (ValueError, TypeError):
            self.logger.warning(f"无效的端口号 ('{self.listen_port}')，使用默认值8080")
            self.listen_port = 8080

    async def initialize(self):
        """初始化插件，加载订阅、获取令牌并启动HTTP服务器"""
        self.logger.info("初始化MediaSearchPlugin...")
        self.logger.info(f"插件目录: {self.plugin_dir}")
        self.logger.info(f"日志文件路径: {self.log_file_path}")
        self.logger.info(f"订阅文件路径: {self.subscriptions_file}")
        # 加载订阅
        if self.subscriptions_file:
            self._load_subscriptions()
        else:
            self.logger.warning("订阅持久化已禁用")
        # 初始化API令牌
        if not self.base_url or not self.username or not self.password:
            self.logger.error("API配置不完整")
        else:
            self.access_token = await self.get_access_token(self.username, self.password, self.token_url)
            if not self.access_token:
                self.logger.warning("初始令牌获取失败")
        # 初始化通知系统
        if not self.http_forward_enabled:
            self.logger.info("HTTP转发功能已禁用，不启动HTTP服务器")
            return
        self.logger.info("正在初始化通知系统...")
        try:
            self.http_app = web.Application()
            self.http_app.add_routes([
                web.post('/', self.aiohttp_notification_handler),
                web.put('/', self.aiohttp_notification_handler),
            ])
            self.http_runner = web.AppRunner(self.http_app)
            await self.http_runner.setup()
            self.http_site = web.TCPSite(self.http_runner, LISTEN_ADDRESS, self.listen_port)
            await self.http_site.start()
            self.logger.info(f"HTTP服务器启动在 {LISTEN_ADDRESS}:{self.listen_port}")
            self.message_processor_task = asyncio.create_task(self.process_message_queue())
            self.logger.info("消息处理任务已启动")
        except OSError as e:
            self.logger.error(f"HTTP服务器在端口 {self.listen_port} 启动失败: {e}")
            self.http_app = None
            self.http_runner = None
            self.http_site = None
        except Exception as e:
            self.logger.error(f"通知系统初始化失败: {e}", exc_info=True)
            self.http_app = None
            self.http_runner = None
            self.http_site = None

    async def aiohttp_notification_handler(self, request):
        """aiohttp异步HTTP通知处理"""
        timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        msg_type = "未知"; content_str = ""; raw_body_str_for_log = "(No body)"
        body_type_guess = "Unknown"
        log_entry = f"\n----- HTTP Log Start -----\n"
        log_entry += f"Timestamp: {timestamp}\n"
        log_entry += f"Client: {request.remote}\n"
        log_entry += f"Method: {request.method}\n"
        log_entry += f"Path: {request.path}\n"
        try:
            body = await request.read()
            if body:
                try:
                    body_str = body.decode('utf-8')
                    decoded_body_str = codecs.decode(body_str, 'unicode_escape')
                    raw_body_str_for_log = decoded_body_str
                    content_str = decoded_body_str
                    type_match = re.search(r'"type":\s*"(.*?)"', body_str)
                    if type_match:
                        msg_type = type_match.group(1)
                        body_type_guess = "Text (Type found by Regex)"
                        log_entry += f"Body Type Guess: {body_type_guess}: {msg_type}\n"
                    else:
                        try:
                            json.loads(body_str)
                            body_type_guess = "Valid JSON (Type missing?)"
                        except json.JSONDecodeError:
                            body_type_guess = "Text (Type not found)"
                        msg_type = "文本"
                        log_entry += f"Body Type Guess: {body_type_guess}\n"
                except UnicodeDecodeError:
                    msg_type = "原始数据"
                    content_str = f"(Undecodable: {body!r})"
                    raw_body_str_for_log = content_str
                    body_type_guess = "Undecodable"
                    log_entry += f"Body Type Guess: {body_type_guess}\n"
                    log_entry += f"Raw Bytes: {body!r}\n"
            else:
                log_entry += f"Body: None\n"
                body_type_guess = "No Body"
            log_entry += f"Body Type Determination: {body_type_guess}\n"
            log_entry += f"Raw Body Content: {raw_body_str_for_log}\n"
            if content_str not in ["", "(No body)"]:
                self.message_queue.put((timestamp, msg_type, content_str))
                log_entry += f"Message Queued: Yes (Final Type: {msg_type})\n"
            else:
                log_entry += f"Message Queued: No (Empty content)\n"
            log_entry += f"----- HTTP Log End -----\n"
            self.logger.info(log_entry)
            sys.stdout.flush()
            return web.Response(status=200, text="Notification received.")
        except Exception as e:
            self.logger.error(f"aiohttp通知处理异常: {e}", exc_info=True)
            return web.Response(status=500, text="Internal Server Error.")

    def _load_subscriptions(self):
        """从文件加载通知订阅"""
        if not self.subscriptions_file:
            return
            
        if self.subscriptions_file.exists() and self.subscriptions_file.is_file():
            try:
                with open(self.subscriptions_file, 'r', encoding='utf-8') as f:
                    loaded_data = json.load(f)
                    
                temp_subscriptions = defaultdict(set)
                for chat_id, categories in loaded_data.items():
                    if isinstance(categories, list):
                        valid_cats = {cat for cat in categories if cat in ALLOWED_CATEGORIES}
                        if valid_cats:
                            temp_subscriptions[str(chat_id)] = valid_cats
                        else:
                            self.logger.warning(f"聊天ID {chat_id} 的订阅只包含无效类别，已跳过")
                    else:
                        self.logger.warning(f"聊天ID {chat_id} 的订阅格式无效，已跳过")
                        
                self.notification_subscriptions = temp_subscriptions
                self.logger.info(f"从 {self.subscriptions_file} 加载了 {len(self.notification_subscriptions)} 个聊天订阅")
            except Exception as e:
                self.logger.error(f"加载订阅失败: {e}", exc_info=True)
                self.notification_subscriptions = defaultdict(set)
        else:
            self.logger.info("订阅文件不存在，使用空订阅")
            self.notification_subscriptions = defaultdict(set)

    def _save_subscriptions(self):
        """保存通知订阅到文件"""
        if not self.subscriptions_file:
            self.logger.warning("无法保存订阅：未设置文件路径")
            return
            
        try:
            self.subscriptions_file.parent.mkdir(parents=True, exist_ok=True)
            data_to_save = {chat_id: list(categories) for chat_id, categories in self.notification_subscriptions.items()}
            
            with open(self.subscriptions_file, 'w', encoding='utf-8') as f:
                json.dump(data_to_save, f, ensure_ascii=False, indent=4)
                
            self.logger.info(f"已保存 {len(data_to_save)} 个聊天订阅到 {self.subscriptions_file}")
        except Exception as e:
            self.logger.error(f"保存订阅失败: {e}", exc_info=True)

    def run_http_server(self):
        """运行HTTP服务器的线程函数"""
        if not self.httpd:
            self.logger.error("HTTP服务器实例为None")
            return
            
        self.logger.info("HTTP服务器线程已启动")
        self.httpd.timeout = 1.0
        
        while not self.server_stop_event.is_set():
            try:
                self.httpd.handle_request()
            except socket.timeout:
                continue
            except Exception as e:
                self.logger.error(f"HTTP服务器循环出错: {e}", exc_info=True)
                
        self.logger.info("HTTP服务器收到停止信号")
        
        try:
            self.httpd.server_close()
        except Exception as e:
            self.logger.error(f"关闭HTTP socket时出错: {e}")
            
        self.logger.info("HTTP服务器socket已关闭")

    async def process_message_queue(self):
        """异步处理消息队列，解析通知并发送到订阅者"""
        self.logger.info("消息处理循环已启动 (正则优先 + 条件解码模式)")
        title_pattern = re.compile(r'"title":\s*"(.*?)"', re.DOTALL)
        message_pattern = re.compile(r'"message":\s*"((?:.|\n)*?)"\s*(?:,|}|\Z)', re.DOTALL)
        unicode_escape_pattern = re.compile(r'\\u[0-9a-fA-F]{4}')
        
        while True:
            try:
                if self.message_queue.empty():
                    await asyncio.sleep(0.5)
                    continue
                    
                timestamp, msg_type, content_str = self.message_queue.get_nowait()
                self.logger.info(f"处理消息. 类型='{msg_type}'")
                
                # 解析消息内容
                formatted_message = ""
                title = None
                message = None
                processed_by = "正则提取"
                
                try:
                    # 尝试匹配标题和消息内容
                    title_match = title_pattern.search(content_str)
                    message_match = message_pattern.search(content_str)
                    
                    raw_title = title_match.group(1) if title_match else None
                    raw_message = message_match.group(1) if message_match else None
                    
                    # 处理Unicode转义
                    if raw_title is not None and raw_message is not None:
                        if unicode_escape_pattern.search(content_str):
                            self.logger.info("发现Unicode转义字符，尝试解码")
                            try:
                                title = codecs.decode(raw_title, 'unicode_escape')
                                message = codecs.decode(raw_message, 'unicode_escape')
                            except Exception as decode_err:
                                self.logger.error(f"解码失败: {decode_err}，使用原始文本")
                                title = raw_title
                                message = raw_message
                        else:
                            title = raw_title
                            message = raw_message
                    else:
                        if raw_title is not None:
                            title = raw_title
                        if raw_message is not None:
                            message = raw_message
                except Exception as regex_err:
                    self.logger.error(f"正则解析错误: {regex_err}")
                
                # 组装最终消息（优化：过滤None和'None'字符串）
                if title is not None and message is not None and message not in [None, "None"]:
                    formatted_message = f"{title}\n{message}"
                elif title is not None:
                    formatted_message = title
                elif message is not None and message not in [None, "None"]:
                    formatted_message = message
                else:
                    formatted_message = content_str
                    self.logger.warning("使用原始内容")
                
                # 发送消息给订阅者
                if formatted_message:
                    subs_copy = dict(self.notification_subscriptions)
                    sent = False
                    
                    for cid, cats in subs_copy.items():
                        match = (msg_type in cats) and (msg_type in ALLOWED_CATEGORIES)
                        all_cats = "所有" in cats
                        
                        if all_cats or match:
                            try:
                                await self.context.send_message(cid, MessageChain().message(formatted_message))
                                self.logger.info(f"消息已发送 (类型: {msg_type}) 到: {cid}")
                                sent = True
                            except Exception as e:
                                self.logger.error(f"发送失败 到 {cid}: {e}")
                    
                    if not sent:
                        self.logger.debug(f"没有类型为 '{msg_type}' 的订阅")
                else:
                    self.logger.warning(f"格式化消息为空 (类型: {msg_type})，不发送")
                
                self.message_queue.task_done()
            except asyncio.CancelledError:
                self.logger.info("处理任务被取消")
                break
            except Exception as e:
                self.logger.error(f"消息处理循环错误: {e}", exc_info=True)
                await asyncio.sleep(1)

    # --- 命令组和命令实现 ---
    @filter.command_group("MP")
    def mp(self): 
        """MP命令组"""
        pass
        
    @mp.command("菜单")
    async def menu_command(self, event: AstrMessageEvent):
        """显示插件功能菜单"""
        # 确定HTTP服务器状态
        http_status = "未启用 ⛔"
        if self.http_forward_enabled:
            if self.httpd and self.server_thread and self.server_thread.is_alive():
                ip_addr = get_local_ip() if LISTEN_ADDRESS == '0.0.0.0' else LISTEN_ADDRESS
                http_status = f"运行中 ✅ - http://{ip_addr}:{self.listen_port}"
            elif self.server_thread:
                http_status = f"异常 ⚠️ - 端口 {self.listen_port}"
            else:
                http_status = f"未运行 ❌ - 端口 {self.listen_port}"
            
        menu_text = f"""📺 MP 功能菜单 v1.5.0 📺
---------------------------------
【媒体管理】
  MP 搜索 [关键词] > 搜索媒体。 例: MP 搜索 黑暗荣耀
  MP 新增订阅 [序号] > 订阅搜索结果。 例: MP 新增订阅 1
  MP 查看订阅 > 显示已订阅内容。
  MP 搜索订阅 [ID] > 检查订阅任务(可选ID)。 例: MP 搜索订阅 123
【消息通知】
  MP 启用通知 [所有/类别|类别]
  MP 取消通知 [所有/类别|类别]
  多个类别用|分割
  MP 菜单 > 显示此菜单。
---------------------------------
可用类别: 资源下载,整理入库,订阅,媒体服务器,手动处理,插件,其他,站点,所有
HTTP服务: {http_status}
---------------------------------
注意: 所有命令仅支持大写形式(MP)"""
        yield event.plain_result(menu_text.strip())
        
    @mp.command("搜索")
    async def search_command(self, event: AstrMessageEvent, keyword: str):
        """搜索媒体内容"""
        userid = str(event.unified_msg_origin)
        if not self.access_token:
            self.access_token = await self.get_access_token(self.username, self.password, self.token_url)
            if not self.access_token:
                yield event.plain_result("⚠️ Token获取失败。")
                return
        self.token_refresh_count = 0
        media_data = await self.search_media(self.access_token, keyword)
        if media_data:
            cleaned_data = self.remove_empty_keys(media_data)
            if not cleaned_data:
                yield event.plain_result("无匹配内容。")
                return
            self.user_search_results[userid] = cleaned_data
            result_text = self.format_search_results(cleaned_data) + "\n\n👉 可用 `MP 新增订阅 序号` 订阅"
            yield event.plain_result(result_text)
        else:
            yield event.plain_result("⚠️ 搜索失败或无结果。")
        
    @mp.command("新增订阅")
    async def add_subscription_command(self, event: AstrMessageEvent, index: str):
        """将搜索结果添加到订阅（支持TMDB多季选择）"""
        userid = str(event.unified_msg_origin)
        if userid not in self.user_search_results or not self.user_search_results[userid]:
            yield event.plain_result("⚠️ 请先搜索。")
            return
        try:
            idx = int(index) - 1
            search_results = self.user_search_results[userid]
            if not (0 <= idx < len(search_results)):
                yield event.plain_result(f"⚠️ 无效序号 {index} (范围 1-{len(search_results)})")
                return
        except ValueError:
            yield event.plain_result(f"⚠️ 无效序号 {index} (请输入数字)。")
            return
        media_item = search_results[idx]
        media_title = media_item.get("title", "未知")
        source = media_item.get("source", "")
        tmdbid = media_item.get("tmdb_id")
        # TMDB来源，需查季
        if source == "themoviedb" and tmdbid:
            if not self.access_token:
                self.access_token = await self.get_access_token(self.username, self.password, self.token_url)
                if not self.access_token:
                    yield event.plain_result("⚠️ Token获取失败。")
                    return
            # 查询季信息
            tmdb_url = f"{self.base_url}/api/v1/tmdb/seasons/{tmdbid}"
            try:
                headers = {"accept": "application/json", "Authorization": f"Bearer {self.access_token}"}
                timeout = aiohttp.ClientTimeout(total=10)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(tmdb_url, headers=headers) as resp:
                        if resp.status == 200:
                            seasons = await resp.json()
                            if isinstance(seasons, list) and len(seasons) > 1:
                                # 多季，提示用户选择
                                season_list = []
                                for i, s in enumerate(seasons, 1):
                                    sn = s.get("season_number", "?")
                                    name = s.get("name", "?")
                                    season_list.append(f"{i}、第{sn}季 {name}")
                                msg = "该剧集有多季，请回复所有或序号选择要订阅的季：\n" + "\n".join(season_list) + "\n（输入退出可取消）"
                                self.user_states[userid] = "waiting_tmdb_season"
                                self.pending_subscription[userid] = {
                                    "media_item": media_item,
                                    "seasons": seasons
                                }
                                yield event.plain_result(msg)
                                return
                            elif isinstance(seasons, list) and len(seasons) == 1:
                                # 只有一季，直接订阅
                                media_item = dict(media_item)
                                media_item["season"] = seasons[0].get("season_number", 1)
                                transformed_data = self.transform_data(media_item)
                                response = await self.add_subscription(self.access_token, transformed_data)
                                if response and response.get("success") == True:
                                    yield event.plain_result(f"✅ `{media_title}` 订阅成功。")
                                else:
                                    error_msg = response.get("msg", "看日志。") if isinstance(response, dict) else "看日志。"
                                    yield event.plain_result(f"⚠️ 订阅 `{media_title}` 失败: {error_msg}")
                                return
                            else:
                                yield event.plain_result("⚠️ 未获取到季信息，无法订阅。")
                                return
                        else:
                            yield event.plain_result(f"⚠️ 查询季信息失败: {resp.status}")
                            return
            except Exception as e:
                yield event.plain_result(f"⚠️ 查询季信息异常: {e}")
                return
        # 其他来源或无tmdbid，直接订阅
        if not self.access_token:
            self.access_token = await self.get_access_token(self.username, self.password, self.token_url)
            if not self.access_token:
                yield event.plain_result("⚠️ Token获取失败。")
                return
        transformed_data = self.transform_data(media_item)
        response = await self.add_subscription(self.access_token, transformed_data)
        if response and response.get("success") == True:
            yield event.plain_result(f"✅ `{media_title}` 订阅成功。")
        else:
            error_msg = response.get("msg", "看日志。") if isinstance(response, dict) else "看日志。"
            yield event.plain_result(f"⚠️ 订阅 `{media_title}` 失败: {error_msg}")
        
    @mp.command("查看订阅")
    async def view_subscriptions_command(self, event: AstrMessageEvent):
        """查看当前订阅"""
        if not self.access_token:
            self.access_token = await self.get_access_token(self.username, self.password, self.token_url)
            if not self.access_token:
                yield event.plain_result("⚠️ Token获取失败。")
                return
        self.token_refresh_count = 0
        subscription_data = await self.get_subscription_data(self.access_token)
        if subscription_data:
            yield event.plain_result(self.format_subscription_data(subscription_data))
        else:
            yield event.plain_result("⚠️ 获取订阅数据失败或无订阅。")
        
    @mp.command("搜索订阅")
    async def search_subscription_command(self, event: AstrMessageEvent, subscription_id: str = ""):
        """执行订阅搜索"""
        if not self.access_token:
            self.access_token = await self.get_access_token(self.username, self.password, self.token_url)
            if not self.access_token:
                yield event.plain_result("⚠️ Token获取失败。")
                return
        self.token_refresh_count = 0
        search_result = await self.search_subscription(self.access_token, subscription_id.strip())
        if search_result:
            if search_result.get("success"):
                yield event.plain_result(f"✅ 执行订阅搜索{' (ID: ' + subscription_id + ')' if subscription_id else ''}。看后台任务。")
            else:
                msg = search_result.get("msg", "?")
                yield event.plain_result(f"⚠️ 订阅搜索API失败: {msg}")
        else:
            yield event.plain_result("⚠️ 无法获取订阅搜索结果。")
        
    @mp.command("启用通知")
    async def enable_notification_command(self, event: AstrMessageEvent, categories_str: str):
        """启用指定类别的通知"""
        chat_id = str(event.unified_msg_origin)
        cats_add = set(c.strip() for c in categories_str.split('|') if c.strip())
        
        if not cats_add:
            yield event.plain_result(f"⚠️ 请指定类别. 可用: {', '.join(sorted(ALLOWED_CATEGORIES))}")
            return
            
        valid_cats = cats_add & ALLOWED_CATEGORIES
        invalid_cats = cats_add - ALLOWED_CATEGORIES
        
        if not valid_cats:
            yield event.plain_result(f"⚠️ 类别无效. 可用: {', '.join(sorted(ALLOWED_CATEGORIES - {'所有'}))} 或 '所有'")
            return
            
        added = set()
        current = self.notification_subscriptions.setdefault(chat_id, set())
        changed = False
        
        if "所有" in valid_cats:
            if "所有" not in current or len(current) > 1:
                current.clear()
                current.add("所有")
                added.add("所有")
                changed = True
        else:
            if "所有" in current:
                current.remove("所有")
                changed = True
                
            for cat in valid_cats:
                if cat not in current:
                    current.add(cat)
                    added.add(cat)
                    changed = True
        
        if changed:
            self._save_subscriptions()
            
        parts = []
        if added:
            parts.append(f"✅ 已启用: {', '.join(sorted(added))}。")
        elif valid_cats:
            parts.append(f"ℹ️ 已启用状态: {', '.join(sorted(valid_cats))}。")
            
        if invalid_cats:
            parts.append(f"⚠️ 忽略无效: {', '.join(sorted(invalid_cats))}。")
            
        final = self.notification_subscriptions.get(chat_id, set())
        parts.append(f"当前启用: {', '.join(sorted(final)) if final else '无'}")
        
        yield event.plain_result("\n".join(parts))
        
    @mp.command("取消通知")
    async def disable_notification_command(self, event: AstrMessageEvent, categories_str: str):
        """取消指定类别的通知"""
        chat_id = str(event.unified_msg_origin)
        cats_rem = set(c.strip() for c in categories_str.split('|') if c.strip())
        
        if not cats_rem:
            yield event.plain_result(f"⚠️ 请指定类别. 可用: {', '.join(sorted(ALLOWED_CATEGORIES))}")
            return
            
        current = self.notification_subscriptions.get(chat_id)
        if not current:
            yield event.plain_result("ℹ️ 当前无启用通知。")
            return
            
        removed = set()
        changed = False
        
        if "所有" in cats_rem:
            if chat_id in self.notification_subscriptions:
                del self.notification_subscriptions[chat_id]
                changed = True
                yield event.plain_result("✅ 已取消所有通知。")
            else:
                yield event.plain_result("ℹ️ 当前无启用通知。")
                
            if changed:
                self._save_subscriptions()
            return
            
        valid_rem = cats_rem & ALLOWED_CATEGORIES
        invalid_cats = cats_rem - ALLOWED_CATEGORIES
        
        if not valid_rem:
            yield event.plain_result(f"⚠️ 类别无效. 可用: {', '.join(sorted(ALLOWED_CATEGORIES - {'所有'}))} 或 '所有'")
            return
            
        if chat_id in self.notification_subscriptions:
            for cat in valid_rem:
                if cat in current:
                    current.remove(cat)
                    removed.add(cat)
                    changed = True
                    
            if not current:
                del self.notification_subscriptions[chat_id]
                
        if changed:
            self._save_subscriptions()
            
        parts = []
        if removed:
            parts.append(f"✅ 已取消: {', '.join(sorted(removed))}。")
        elif valid_rem:
            parts.append(f"ℹ️ 指定类别未启用: {', '.join(sorted(valid_rem))}。")
            
        if invalid_cats:
            parts.append(f"⚠️ 忽略无效: {', '.join(sorted(invalid_cats))}。")
            
        final = self.notification_subscriptions.get(chat_id, set())
        parts.append(f"当前剩余: {', '.join(sorted(final)) if final else '无'}")
        
        yield event.plain_result("\n".join(parts))

    @filter.regex(r"^[\s\S]+")
    async def handle_tmdb_season_input(self, event: AstrMessageEvent):
        userid = str(getattr(event, "unified_msg_origin", None))
        if userid not in self.user_states:
            return
        state = self.user_states[userid]
        msg = event.message_str.strip()
        if state == "waiting_tmdb_season":
            if msg == "退出":
                self.logger.info(f"[TMDB多季订阅] 用户{userid} 退出流程")
                self.user_states.pop(userid, None)
                self.pending_subscription.pop(userid, None)
                yield event.plain_result("已退出多季订阅流程。")
                return
            if msg == "所有":
                pending = self.pending_subscription.get(userid)
                if not pending:
                    self.user_states.pop(userid, None)
                    yield event.plain_result("⚠️ 状态已失效，请重新发起订阅。")
                    return
                seasons = pending["seasons"]
                media_item = dict(pending["media_item"])
                results = []
                for s in seasons:
                    season_number = s.get("season_number", 1)
                    media_item["season"] = season_number
                    media_title = media_item.get("title", "未知")
                    if not self.access_token:
                        self.access_token = await self.get_access_token(self.username, self.password, self.token_url)
                        if not self.access_token:
                            results.append(f"❌ `{media_title}` 第{season_number}季 Token获取失败。")
                            continue
                    transformed_data = self.transform_data(media_item)
                    response = await self.add_subscription(self.access_token, transformed_data)
                    if response and response.get("success") == True:
                        results.append(f"✅ `{media_title}` 第{season_number}季 订阅成功。")
                    else:
                        error_msg = response.get("msg", "看日志。") if isinstance(response, dict) else "看日志。"
                        results.append(f"⚠️ 订阅 `{media_title}` 第{season_number}季 失败: {error_msg}")
                self.user_states.pop(userid, None)
                self.pending_subscription.pop(userid, None)
                yield event.plain_result("\n".join(results))
                return
            if msg.isdigit():
                idx = int(msg) - 1
                pending = self.pending_subscription.get(userid)
                if not pending:
                    self.user_states.pop(userid, None)
                    yield event.plain_result("⚠️ 状态已失效，请重新发起订阅。")
                    return
                seasons = pending["seasons"]
                media_item = dict(pending["media_item"])
                if 0 <= idx < len(seasons):
                    season_number = seasons[idx].get("season_number", 1)
                    media_item["season"] = season_number
                    media_title = media_item.get("title", "未知")
                    if not self.access_token:
                        self.access_token = await self.get_access_token(self.username, self.password, self.token_url)
                        if not self.access_token:
                            yield event.plain_result("⚠️ Token获取失败。")
                            return
                    transformed_data = self.transform_data(media_item)
                    response = await self.add_subscription(self.access_token, transformed_data)
                    if response and response.get("success") == True:
                        yield event.plain_result(f"✅ `{media_title}` 第{season_number}季 订阅成功。")
                    else:
                        error_msg = response.get("msg", "看日志。") if isinstance(response, dict) else "看日志。"
                        yield event.plain_result(f"⚠️ 订阅 `{media_title}` 第{season_number}季 失败: {error_msg}")
                    self.user_states.pop(userid, None)
                    self.pending_subscription.pop(userid, None)
                    return
                else:
                    yield event.plain_result("⚠️ 序号无效，请重新输入或输入退出。")
                    return
            # 非数字输入直接忽略
            return

    # --- terminate, _ensure_token, API methods (保持 v1.3.5 的状态) ---
    async def terminate(self):
        """清理资源并终止插件"""
        self.logger.info("终止MediaSearchPlugin...")
        if not self.http_forward_enabled:
            self.logger.info("HTTP转发功能未启用，跳过HTTP资源清理")
            return
        # 停止HTTP服务器
        if hasattr(self, 'http_runner') and self.http_runner:
            self.logger.info("正在停止HTTP服务器...")
            try:
                await self.http_runner.cleanup()
                self.logger.info("HTTP服务器已停止")
            except Exception as e:
                self.logger.warning(f"HTTP服务器关闭异常: {e}")
            self.http_runner = None
            self.http_site = None
            self.http_app = None
        # 取消消息处理任务
        if self.message_processor_task and not self.message_processor_task.done():
            self.logger.info("正在取消消息处理任务...")
            self.message_processor_task.cancel()
            try:
                await self.message_processor_task
            except asyncio.CancelledError:
                self.logger.info("消息处理任务已取消")
            except Exception as e:
                self.logger.error(f"取消任务时出错: {e}")
        self.logger.info("MediaSearchPlugin已终止")

    def _ensure_token(self) -> bool:
        """确保访问令牌有效，必要时获取新令牌"""
        if not self.access_token:
            self.logger.info("令牌缺失，获取新令牌")
            return False
        return True
        
    async def get_access_token(self, username, password, token_url):
        """获取API访问令牌 (aiohttp)"""
        self._log_and_check_daily('info', f"请求令牌: {token_url}, 用户名: {username}")
        if not token_url: 
            self._log_and_check_daily('error', "令牌URL未配置")
            return None
        data = {"username": username, "password": password}
        headers = {'Content-Type': 'application/x-www-form-urlencoded'}
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(token_url, data=data, headers=headers) as response:
                    self._log_and_check_daily('debug', f"令牌请求响应状态: {response.status}")
                    response.raise_for_status()
                    token_data = await response.json()
                    self._log_and_check_daily('debug', f"令牌响应内容: {token_data}")
                    token = token_data.get("access_token")
                    if token: 
                        self._log_and_check_daily('info', "令牌获取成功")
                        return token
                    else: 
                        self._log_and_check_daily('warning', f"响应中无令牌: {await response.text()}")
                        return None
        except aiohttp.ClientError as e:
            self._log_and_check_daily('error', f"令牌请求错误: {e}")
            return None
        except Exception as e:
            self._log_and_check_daily('exception', f"令牌获取未知错误: {e}")
            return None
            
    async def search_media(self, access_token, title):
        """搜索媒体内容 (aiohttp)"""
        self._log_and_check_daily('info', f"搜索媒体: {title}")
        if not self.base_url: 
            self._log_and_check_daily('warning', "base_url未配置")
            return None
        search_url = f"{self.base_url}/api/v1/media/search"
        params = {'title': title, 'type': 'media', 'page': 1, 'count': self.max_results * 2}
        headers = {"accept": "application/json", "Authorization": f"Bearer {access_token}"}
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(search_url, headers=headers, params=params) as response:
                    self._log_and_check_daily('debug', f"媒体搜索响应状态: {response.status}")
                    if response.status == 200:
                        data = await response.json()
                        self._log_and_check_daily('debug', f"媒体搜索响应内容: {data}")
                        return data
                    elif response.status == 401 and self.token_refresh_count < 1:
                        self._log_and_check_daily('warning', "搜索需要更新令牌")
                        self.token_refresh_count += 1
                        return None
                    else:
                        self._log_and_check_daily('error', f"搜索失败: {response.status} - {await response.text()}")
                        return None
        except aiohttp.ClientError as e:
            self._log_and_check_daily('error', f"搜索请求错误: {e}")
            return None
        except Exception as e:
            self._log_and_check_daily('exception', f"搜索未知错误: {e}")
            return None
            
    async def get_subscription_data(self, access_token):
        """获取订阅数据 (aiohttp)"""
        self._log_and_check_daily('info', "获取订阅数据")
        if not self.subscribe_url: 
            self._log_and_check_daily('warning', "subscribe_url未配置")
            return None
        headers = {"accept": "application/json", "Authorization": f"Bearer {access_token}"}
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(self.subscribe_url, headers=headers) as response:
                    self._log_and_check_daily('debug', f"订阅数据响应状态: {response.status}")
                    if response.status == 200:
                        data = await response.json()
                        self._log_and_check_daily('debug', f"订阅数据响应内容: {data}")
                        return data
                    elif response.status == 401 and self.token_refresh_count < 1:
                        self._log_and_check_daily('warning', "获取订阅需要更新令牌")
                        self.token_refresh_count += 1
                        return None
                    else:
                        self._log_and_check_daily('error', f"获取订阅失败: {response.status} - {await response.text()}")
                        return None
        except aiohttp.ClientError as e:
            self._log_and_check_daily('error', f"获取订阅请求错误: {e}")
            return None
        except Exception as e:
            self._log_and_check_daily('exception', f"获取订阅未知错误: {e}")
            return None
            
    def remove_empty_keys(self, data):
        """递归移除空值键"""
        if isinstance(data, dict): 
            return {k: v_clean for k, v in data.items() if (v_clean := self.remove_empty_keys(v)) is not None} or None
        elif isinstance(data, list): 
            return [item_clean for item in data if (item_clean := self.remove_empty_keys(item)) is not None] or None
        elif isinstance(data, str): 
            return data if data.strip() else None
        elif data in (0, False): 
            return data
        else: 
            return data
            
    def format_search_results(self, data):
        """格式化搜索结果"""
        if not data: 
            return "没有找到匹配的媒体内容。"
            
        text = "【搜索结果】\n"
        count = 0
        
        for i, item in enumerate(data):
            item = self.remove_empty_keys(item) or {}
            if not item or count >= self.max_results:
                if count >= self.max_results: 
                    text += f"\n... (最多显示 {self.max_results} 条)"
                break
                
            title = item.get("title", "?")
            year = item.get("year")
            mtype = item.get("type", "?")
            src = item.get("source", "?")
            detail_link = item.get("detail_link", "")
            
            year_str = f" ({year})" if year else ""
            text += f"{i+1}. {title}{year_str} [{mtype}]\n   来源: {src}\n"
            if detail_link:
                text += f"   地址: {detail_link}\n"
            
            count += 1
            
        return text.strip() if count > 0 else "没有找到有效的媒体内容。"
        
    def format_subscription_data(self, data):
        """格式化订阅数据"""
        if not data or not (cleaned := self.remove_empty_keys(data)): 
            return "当前无有效订阅数据。"
            
        tv = sorted([i for i in cleaned if i.get("type") == "电视剧"], key=lambda x: (x.get('name', ''), x.get('season', 0)))
        mv = sorted([i for i in cleaned if i.get("type") == "电影"], key=lambda x: (x.get('name', ''), x.get('year', '')))
        
        text = "💎 当前订阅详情 💎\n---------------------\n【📺 电视系列】\n"
        
        if tv:
            for i, s in enumerate(tv):
                n = s.get('name','?')
                seas = s.get('season','?')
                yr = s.get('year','')
                te = s.get('total_episode')
                le = s.get('lack_episode')
                sid = s.get('id','?')
                
                yr_s = f"({yr})" if yr else ""
                text += f"{i+1}. 《{n}》 第 {seas} 季 {yr_s}\n"
                
                if isinstance(te, int) and isinstance(le, int) and te > 0:
                    p = te-le
                    pct = int((p/te)*100)
                    text += f"   进度: {p}/{te} ({pct}%) | ID: {sid}\n"
                elif isinstance(te, int) and te == 0:
                    text += f"   进度: 未开播 | ID: {sid}\n"
                else:
                    text += f"   进度: 信息不详 | ID: {sid}\n"
        else:
            text += "  (无)\n"
            
        text += "\n【🎬 电影】\n"
        
        if mv:
            for i, m in enumerate(mv):
                n = m.get('name','?')
                yr = m.get('year','')
                mid = m.get('id','?')
                yr_s = f"({yr})" if yr else ""
                text += f"{i+1}. 《{n}》 {yr_s} | ID: {mid}\n"
        else:
            text += "  (无)\n"
            
        return text.strip()
        
    def transform_data(self, data):
        """转换媒体数据为订阅格式"""
        cleaned = self.remove_empty_keys(data) or {}
        # 处理季数
        season = cleaned.get("season")
        if season is None:
            s_years = cleaned.get("season_years")
            num_s = cleaned.get("number_of_seasons")
            if isinstance(s_years, dict) and s_years:
                try:
                    nums = [int(s) for s in s_years if s.isdigit()]
                    season = max(nums) if nums else 1
                except:
                    season = 1
            elif isinstance(num_s, int) and num_s > 0:
                season = num_s
            else:
                season = 1
        else:
            try:
                season = int(season)
            except:
                season = 1
        t = {
            k: cleaned.get(m, d) for k, m, d in [
                ("n", "title", ""),
                ("y", "year", ""),
                ("t", "type", ""),
                ("tm", "tmdb_id", 0),
                ("d", "douban_id", ""),
                ("b", "bangumi_id", 0),
                ("p", "poster_path", ""),
                ("bp", "backdrop_path", ""),
                ("v", "vote_average", 0.0),
                ("desc", "overview", ""),
                ("lu", "release_date", ""),
                ("dt", "release_date", "")
            ]
        }
        tx = {
            "name": t['n'],
            "year": str(t['y'] or ""),
            "type": t['t'],
            "keyword": "",
            "tmdbid": int(t['tm'] or 0),
            "doubanid": str(t['d'] or ""),
            "bangumiid": int(t['b'] or 0),
            "mediaid": "",
            "season": season,
            "poster": t['p'],
            "backdrop": t['bp'],
            "vote": float(t['v'] or 0.0),
            "description": t['desc'],
            "filter": "",
            "include": "",
            "exclude": "",
            "quality": "",
            "resolution": "",
            "effect": "",
            "total_episode": 0,
            "start_episode": 0,
            "lack_episode": 0,
            "note": "",
            "state": "",
            "last_update": t['lu'],
            "username": self.username,
            "sites": [],
            "downloader": "",
            "best_version": 0,
            "current_priority": 0,
            "save_path": "",
            "search_imdbid": 0,
            "date": t['dt'],
            "custom_words": "",
            "media_category": "",
            "filter_groups": []
        }
        tx.pop('id', None)
        return tx
        
    async def add_subscription(self, access_token, sub_data):
        """添加订阅 (aiohttp)"""
        self._log_and_check_daily('info', f"添加订阅: {sub_data}")
        if not self.subscribe_url:
            self._log_and_check_daily('warning', "订阅URL未配置")
            return {"success": False, "msg": "订阅URL未配置"}
        headers = {"accept": "application/json", "Authorization": f"Bearer {access_token}"}
        try:
            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(self.subscribe_url, headers=headers, json=sub_data) as response:
                    self._log_and_check_daily('debug', f"添加订阅响应状态: {response.status}")
                    try:
                        data = await response.json()
                        self._log_and_check_daily('debug', f"添加订阅响应内容: {data}")
                        return data
                    except Exception:
                        self._log_and_check_daily('error', f"添加订阅响应JSON解析错误: {await response.text()}")
                        return {"success": False, "msg": "服务器响应格式错误"}
        except aiohttp.ClientResponseError as e:
            self._log_and_check_daily('error', f"添加订阅HTTP错误: {e.status} - {e.message}")
            return {"success": False, "msg": f"HTTP {e.status}"}
        except aiohttp.ClientError as e:
            self._log_and_check_daily('error', f"添加订阅请求错误: {e}")
            return {"success": False, "msg": "网络错误"}
        except Exception as e:
            self._log_and_check_daily('exception', f"添加订阅未知错误: {e}")
            return {"success": False, "msg": "内部错误"}
            
    async def search_subscription(self, access_token, sub_id=""):
        """搜索订阅 (aiohttp)"""
        self._log_and_check_daily('info', f"搜索订阅: {sub_id}")
        if not self.base_url:
            self._log_and_check_daily('warning', "base_url未配置")
            return None
        headers = {"accept": "application/json", "Authorization": f"Bearer {access_token}"}
        url = f"{self.base_url}/api/v1/subscribe/search/{sub_id}" if sub_id else f"{self.base_url}/api/v1/subscribe/search"
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=headers) as response:
                    self._log_and_check_daily('debug', f"搜索订阅响应状态: {response.status}")
                    if response.status == 200:
                        data = await response.json()
                        self._log_and_check_daily('debug', f"搜索订阅响应内容: {data}")
                        return data
                    elif response.status == 401 and self.token_refresh_count < 1:
                        self._log_and_check_daily('warning', "搜索订阅需要更新令牌")
                        self.token_refresh_count += 1
                        return None
                    elif response.status == 404:
                        self._log_and_check_daily('warning', f"搜索订阅404错误: ID '{sub_id}'")
                        return {"success": False, "msg": f"ID {sub_id} 未找到"} if sub_id else {"success": True, "data": {"list": []}}
                    else:
                        self._log_and_check_daily('error', f"搜索订阅失败: {response.status} - {await response.text()}")
                        return None
        except aiohttp.ClientError as e:
            self._log_and_check_daily('error', f"搜索订阅请求错误: {e}")
            return None
        except Exception as e:
            self._log_and_check_daily('exception', f"搜索订阅未知错误: {e}")
            return None

# 允许的通知类别
ALLOWED_CATEGORIES = {
    "资源下载", "整理入库", "订阅", "媒体服务器",
    "手动处理", "插件", "其他", "站点", "所有"
}

# HTTP 服务器基础配置
LISTEN_ADDRESS = '0.0.0.0'

# 获取本机 IP 地址
def get_local_ip():
    """安全地获取本机的主要IPv4地址，不进行外部网络连接"""
    try:
        hostname = socket.gethostname()
        ip = socket.gethostbyname(hostname)
        if ip and not ip.startswith("127."):
            return ip
        # 进一步遍历所有网卡
        for info in socket.getaddrinfo(hostname, None):
            family, _, _, _, sockaddr = info
            if family == socket.AF_INET:
                candidate = sockaddr[0]
                if not candidate.startswith("127."):
                    return candidate
    except Exception:
        pass
    return "127.0.0.1"
