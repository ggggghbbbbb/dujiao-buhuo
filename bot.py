#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import time
from datetime import datetime
from threading import Thread
import mysql.connector
from mysql.connector import Error
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Updater, CommandHandler, MessageHandler, Filters, 
    CallbackContext, ChatMemberHandler
)
from telegram.error import TelegramError

# ==================== 配置部分 ====================
DB_HOST = '数据库ip'
DB_PORT = 3306
DB_USER = '用户名'
DB_PASSWORD = '密码'
DB_NAME = '数据库名'

TELEGRAM_TOKEN = '############'  # 替换为你的 token

USER_CONFIG_FILE = 'user.config'
PRODUCT_CACHE_FILE = 'product_cache.json'

# ==================== 数据管理类 ====================
class DataManager:
    """管理用户、群组和产品缓存数据"""
    
    @staticmethod
    def load_user_config():
        """加载用户和群组数据"""
        try:
            with open(USER_CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except FileNotFoundError:
            print(f"{USER_CONFIG_FILE} 不存在，创建新文件")
            return {'users': {}, 'groups': {}}
        except json.JSONDecodeError:
            print(f"{USER_CONFIG_FILE} 格式错误")
            return {'users': {}, 'groups': {}}
    
    @staticmethod
    def save_user_config(data):
        """保存用户和群组数据"""
        try:
            with open(USER_CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            print("用户/群组数据已保存")
        except Exception as e:
            print(f"保存用户/群组数据失败: {e}")
    
    @staticmethod
    def load_product_cache():
        """加载产品缓存"""
        try:
            with open(PRODUCT_CACHE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}
    
    @staticmethod
    def save_product_cache(data):
        """保存产品缓存"""
        try:
            with open(PRODUCT_CACHE_FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"保存产品缓存失败: {e}")
    
    @staticmethod
    def add_user(user_id, username=None):
        """添加用户"""
        data = DataManager.load_user_config()
        if str(user_id) not in data['users']:
            data['users'][str(user_id)] = {
                'username': username,
                'added_at': datetime.now().isoformat()
            }
            DataManager.save_user_config(data)
            print(f"新用户已添加: {user_id} (@{username})")
    
    @staticmethod
    def add_group(group_id, group_name=None):
        """添加群组"""
        data = DataManager.load_user_config()
        if str(group_id) not in data['groups']:
            data['groups'][str(group_id)] = {
                'group_name': group_name,
                'added_at': datetime.now().isoformat()
            }
            DataManager.save_user_config(data)
            print(f"新群组已添加: {group_id} ({group_name})")
    
    @staticmethod
    def get_all_recipients():
        """获取所有需要通知的用户和群组"""
        data = DataManager.load_user_config()
        recipients = []
        
        # 添加所有用户
        for user_id in data['users'].keys():
            try:
                recipients.append(int(user_id))
            except (ValueError, TypeError):
                continue
        
        # 添加所有群组
        for group_id in data['groups'].keys():
            try:
                recipients.append(int(group_id))
            except (ValueError, TypeError):
                continue
        
        return recipients

# ==================== 数据库操作类 ====================
class DatabaseManager:
    """管理数据库连接和查询"""
    
    @staticmethod
    def get_connection(retries=3):
        """获取数据库连接，带重试机制"""
        for attempt in range(retries):
            try:
                conn = mysql.connector.connect(
                    host=DB_HOST,
                    port=DB_PORT,
                    user=DB_USER,
                    password=DB_PASSWORD,
                    database=DB_NAME,
                    autocommit=True,
                    connection_timeout=10
                )
                print("✅ 数据库连接成功")
                return conn
            except Error as e:
                print(f"❌ 数据库连接失败 (尝试 {attempt + 1}/{retries}): {e}")
                if attempt < retries - 1:
                    time.sleep(2)  # 等待后重试
        
        return None
    
    @staticmethod
    def fetch_all_products():
        """获取所有库存 > 0 的商品（从 carmis 表计算自动发货库存）"""
        conn = DatabaseManager.get_connection()
        if not conn:
            print("❌ 无法连接到数据库，跳过本次查询")
            return []
        
        try:
            cursor = conn.cursor(dictionary=True)
            
            # 从 carmis 表计算库存：status=1 表示未售出（有效库存）
            query = """
                SELECT 
                    g.id,
                    g.gd_name,
                    g.actual_price,
                    COUNT(CASE WHEN c.status = 1 THEN 1 END) as in_stock
                FROM goods g
                LEFT JOIN carmis c ON g.id = c.goods_id
                GROUP BY g.id, g.gd_name, g.actual_price
                HAVING in_stock > 0
                ORDER BY g.id ASC
            """
            cursor.execute(query)
            products = cursor.fetchall()
            cursor.close()
            print(f"✅ 成功获取 {len(products)} 个商品")
            
            # 打印获取到的商品，用于调试
            if products:
                print("📦 商品列表:")
                for p in products[:10]:  # 只打印前10个
                    print(f"   - ID:{p['id']}, {p['gd_name']}, ¥{p['actual_price']}, 库存:{p['in_stock']}")
                if len(products) > 10:
                    print(f"   ... 还有 {len(products) - 10} 个商品")
            
            return products
        except Error as e:
            print(f"❌ 查询商品失败: {e}")
            return []
        finally:
            if conn.is_connected():
                conn.close()

# ==================== 通知管理类 ====================
class NotificationManager:
    """管理通知发送"""
    
    def __init__(self, bot: Bot):
        self.bot = bot
        self.max_buttons_per_message = 20  # 每条消息最多按钮数
        self.max_message_length = 4000  # 每条消息最多字符数（Telegram 限制 4096）
    
    def build_product_buttons(self, products, start_idx=0, end_idx=None):
        """构建商品按钮，返回 (按钮列表, 结束索引)"""
        if end_idx is None:
            end_idx = min(start_idx + self.max_buttons_per_message, len(products))
        
        keyboard = []
        for product in products[start_idx:end_idx]:
            button = InlineKeyboardButton(
                text=f"{product['gd_name']} | ¥{product['actual_price']} | 库存:{product['in_stock']}",
                url=f"https://fk.o808o.com/buy/{product['id']}"
            )
            keyboard.append([button])
        
        return keyboard, end_idx
    
    def build_notification_message(self, products, change_type="update", page_info=""):
        """构建通知消息"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        if change_type == "new":
            message = f"🆕 <b>新增商品通知</b>\n发卡通知频道 @ananansfk \n时间: {timestamp}\n\n"
        elif change_type == "delete":
            message = f"❌ <b>商品删除通知</b>\n发卡通知频道 @ananansfk \n时间: {timestamp}\n\n"
        else:
            message = f"📦 <b>库存变化通知</b>\n发卡通知频道 @ananansfk \n时间: {timestamp}\n\n"
        
        message += f"当前有库存商品: <b>{len(products)}</b> 个\n"
        if page_info:
            message += f"<b>{page_info}</b>\n"
        message += "\n<b>点击下方按钮直接购买：</b>\n"
        
        return message
    
    def send_notifications(self, products, change_type="update"):
        """发送通知到所有用户和群组，支持分页"""
        if not products:
            print("⚠️  没有商品需要通知")
            return
        
        recipients = DataManager.get_all_recipients()
        
        if not recipients:
            print("⚠️  没有用户或群组需要通知")
            return
        
        print(f"📤 准备向 {len(recipients)} 个接收者发送通知")
        
        # 分页发送商品
        total_products = len(products)
        current_idx = 0
        page_num = 1
        
        while current_idx < total_products:
            buttons, next_idx = self.build_product_buttons(products, current_idx)
            
            if not buttons:
                break
            
            total_pages = (total_products + self.max_buttons_per_message - 1) // self.max_buttons_per_message
            page_info = f"第 {page_num}/{total_pages} 页 (共 {len(buttons)} 个商品)"
            
            message = self.build_notification_message(products, change_type, page_info)
            keyboard = InlineKeyboardMarkup(buttons)
            
            for chat_id in recipients:
                try:
                    self.bot.send_message(
                        chat_id=chat_id,
                        text=message,
                        reply_markup=keyboard,
                        parse_mode='HTML'
                    )
                    print(f"✅ 通知已发送到 {chat_id} (第 {page_num} 页)")
                    time.sleep(0.05)  # 避免触发限流
                except TelegramError as e:
                    print(f"❌ 向 {chat_id} 发送通知失败: {e}")
            
            current_idx = next_idx
            page_num += 1
            time.sleep(0.1)  # 页面间隔
    
    def detect_changes(self, new_products):
        """检测商品变化"""
        old_cache = DataManager.load_product_cache()
        old_ids = set(old_cache.keys())
        new_ids = set(str(p['id']) for p in new_products)
        
        changes_detected = False
        change_type = "update"
        
        # 检查新增商品
        new_added = new_ids - old_ids
        if new_added:
            print(f"🆕 检测到新增商品: {new_added}")
            changes_detected = True
            change_type = "new"
        
        # 检查删除商品
        deleted = old_ids - new_ids
        if deleted:
            print(f"❌ 检测到删除商品: {deleted}")
            changes_detected = True
            change_type = "delete"
        
        # 检查商品属性变化（名称、价格、库存）
        for product in new_products:
            pid = str(product['id'])
            if pid in old_cache:
                old_product = old_cache[pid]
                old_name = old_product.get('gd_name', '')
                old_price = float(old_product.get('actual_price', 0))
                old_stock = old_product.get('in_stock', 0)
                
                new_name = product['gd_name']
                new_price = float(product['actual_price'])
                new_stock = product['in_stock']
                
                if old_name != new_name:
                    print(f"📝 商品 {pid} 名称变化: {old_name} → {new_name}")
                    changes_detected = True
                
                if old_price != new_price:
                    print(f"💰 商品 {pid} 价格变化: ¥{old_price} → ¥{new_price}")
                    changes_detected = True
                
                if old_stock != new_stock:
                    print(f"📦 商品 {pid} 库存变化: {old_stock} → {new_stock}")
                    changes_detected = True
        
        return changes_detected, change_type

# ==================== 机器人命令处理 ====================
def start_handler(update: Update, context: CallbackContext):
    """处理 /start 命令"""
    user = update.effective_user
    chat = update.effective_chat
    
    print(f"用户 {user.id} (@{user.username}) 触发 /start")
    
    # 如果是私聊，记录用户
    if chat.type == 'private':
        DataManager.add_user(user.id, user.username)
        update.message.reply_text(
            "👋 欢迎使用独角数卡库存监控机器人！\n"
            "我会实时监控商品库存变化并通知你。"
        )
    # 如果是群组，记录群组
    else:
        DataManager.add_group(chat.id, chat.title)
        update.message.reply_text(
            "✅ 机器人已加入群组！\n"
            "我会在这里发送库存变化通知。"
        )

def message_handler(update: Update, context: CallbackContext):
    """处理普通消息"""
    user = update.effective_user
    chat = update.effective_chat
    
    # 如果是私聊，记录用户
    if chat.type == 'private':
        DataManager.add_user(user.id, user.username)
        print(f"私聊用户已记录: {user.id} (@{user.username})")

def chat_member_handler(update: Update, context: CallbackContext):
    """处理机器人加入/离开群组的事件"""
    chat_member = update.my_chat_member
    chat = update.effective_chat
    
    print(f"Chat member update: {chat.id}, Type: {chat.type}, Title: {chat.title}")
    
    # 检查机器人的状态变化
    if chat_member.new_chat_member and chat_member.old_chat_member:
        old_status = chat_member.old_chat_member.status
        new_status = chat_member.new_chat_member.status
        
        print(f"机器人状态变化: {old_status} -> {new_status}")
        
        # 机器人被添加到群组/频道
        if old_status in ['left', 'kicked'] and new_status in ['member', 'administrator', 'creator']:
            print(f"🤖 机器人被添加到群组/频道: {chat.id} ({chat.title})")
            DataManager.add_group(chat.id, chat.title)
        
        # 机器人被移除出群组/频道
        elif old_status in ['member', 'administrator', 'creator'] and new_status in ['left', 'kicked']:
            print(f"🤖 机器人被移除出群组/频道: {chat.id} ({chat.title})")
            # 可以选择从记录中删除该群组
            data = DataManager.load_user_config()
            if str(chat.id) in data['groups']:
                del data['groups'][str(chat.id)]
                DataManager.save_user_config(data)
                print(f"❌ 已从记录中删除群组: {chat.id}")

def status_handler(update: Update, context: CallbackContext):
    """处理 /status 命令，显示当前状态"""
    data = DataManager.load_user_config()
    cache = DataManager.load_product_cache()
    
    message = (
        f"📊 <b>机器人状态</b>\n\n"
        f"👤 <b>用户数:</b> {len(data['users'])}\n"
        f"👥 <b>群组数:</b> {len(data['groups'])}\n"
        f"📦 <b>缓存商品数:</b> {len(cache)}\n"
        f"⏰ <b>更新时间:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    
    update.message.reply_text(message, parse_mode='HTML')

# ==================== 监听线程 ====================
class InventoryMonitor:
    """库存监听线程"""
    
    def __init__(self, bot: Bot):
        self.bot = bot
        self.notification_manager = NotificationManager(bot)
        self.running = True
        self.first_run = True
    
    def run(self):
        """监听循环"""
        print("=" * 60)
        print("🚀 库存监听线程已启动，每30秒检查一次")
        print("=" * 60)
        
        while self.running:
            try:
                # 获取当前商品列表
                products = DatabaseManager.fetch_all_products()
                
                if products:
                    # 检查是否有变化
                    changes_detected, change_type = self.notification_manager.detect_changes(products)
                    
                    # 首次运行或检测到变化时发送通知
                    if self.first_run:
                        print("🎬 首次运行，发送初始通知")
                        self.notification_manager.send_notifications(products, "update")
                        self.first_run = False
                    elif changes_detected:
                        print(f"🔔 检测到变化 (类型: {change_type})，发送通知")
                        self.notification_manager.send_notifications(products, change_type)
                    
                    # 更新缓存
                    cache = {}
                    for product in products:
                        cache[str(product['id'])] = {
                            'gd_name': product['gd_name'],
                            'actual_price': float(product['actual_price']),
                            'in_stock': product['in_stock']
                        }
                    DataManager.save_product_cache(cache)
                else:
                    print("⚠️  没有获取到商品数据")
                
                # 等待 30 秒
                time.sleep(30)
            
            except Exception as e:
                print(f"❌ 监听线程出错: {e}")
                time.sleep(30)
        
        print("🛑 库存监听线程已停止")
    
    def stop(self):
        """停止监听"""
        self.running = False
        print("正在停止库存监听线程...")

# ==================== 主函数 ====================
def main():
    """主程序入口"""
    print("=" * 60)
    print("🚀 独角数卡库存监控机器人启动")
    print("=" * 60)
    
    # 创建更新器
    updater = Updater(token=TELEGRAM_TOKEN, use_context=True)
    dispatcher = updater.dispatcher
    bot = updater.bot
    
    # 注册命令处理器
    dispatcher.add_handler(CommandHandler('start', start_handler))
    dispatcher.add_handler(CommandHandler('status', status_handler))
    
    # 注册聊天成员状态变化处理器（用于检测机器人加入/离开群组）
    dispatcher.add_handler(ChatMemberHandler(chat_member_handler, ChatMemberHandler.MY_CHAT_MEMBER))
    
    # 注册普通消息处理器
    dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, message_handler))
    
    # 启动库存监听线程
    monitor = InventoryMonitor(bot)
    monitor_thread = Thread(target=monitor.run, daemon=True)
    monitor_thread.start()
    
    # 启动机器人
    print("🤖 机器人开始轮询 Telegram 消息")
    try:
        updater.start_polling()
        updater.idle()
    except KeyboardInterrupt:
        print("收到关闭信号...")
    finally:
        # 关闭
        monitor.stop()
        print("🛑 机器人已关闭")

if __name__ == '__main__':
    main()
