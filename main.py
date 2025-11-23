import os
import json
import time
import random
import asyncio
from pathlib import Path

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.message_components import Image, Plain, Reply
from .utils import ttp

@register("astrbot_plugin_ArcadiaMint", "Architect", "缝合怪：工业级绘图与经济系统", "1.0.0")
class ArcadiaMint(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        
        # --- 数据持久化初始化 (Shouban 风格) ---
        self.data_dir = Path(__file__).parent / "data"
        self.data_dir.mkdir(exist_ok=True)
        self.user_data_file = self.data_dir / "user_data.json"
        
        # 内存缓存: {user_id: {"points": 100, "last_checkin": "2023-01-01"}}
        self.user_data = {}
        self._load_data()
        
        # --- 限流器初始化 (OpenAI-Img 风格) ---
        self.rate_limit_lock = asyncio.Lock()
        # {group_id: (window_start_timestamp, count)}
        self.rate_limit_state = {} 

    # ==========================
    #      数据与经济系统
    # ==========================
    def _load_data(self):
        if self.user_data_file.exists():
            try:
                with open(self.user_data_file, "r", encoding="utf-8") as f:
                    self.user_data = json.load(f)
            except Exception as e:
                logger.error(f"加载用户数据失败: {e}")
                self.user_data = {}

    def _save_data(self):
        try:
            with open(self.user_data_file, "w", encoding="utf-8") as f:
                json.dump(self.user_data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"保存用户数据失败: {e}")

    def _get_points(self, user_id: str) -> int:
        return self.user_data.get(str(user_id), {}).get("points", 0)

    def _add_points(self, user_id: str, amount: int):
        uid = str(user_id)
        if uid not in self.user_data: self.user_data[uid] = {}
        curr = self.user_data[uid].get("points", 0)
        self.user_data[uid]["points"] = curr + amount
        self._save_data()
        
    def _deduct_points(self, user_id: str, amount: int) -> bool:
        uid = str(user_id)
        curr = self._get_points(uid)
        if curr >= amount:
            self.user_data[uid]["points"] = curr - amount
            self._save_data()
            return True
        return False

    # ==========================
    #      风控与辅助功能
    # ==========================
    async def _collect_images(self, event: AstrMessageEvent) -> list[str]:
        """收集消息中的图片，转为 Base64"""
        images = []
        msg_obj = event.message_obj
        # 1. 检查直接发送的消息
        for comp in msg_obj.message:
            if isinstance(comp, Image):
                try:
                    images.append(await comp.convert_to_base64())
                except: pass
        # 2. 检查回复引用中的图片
        for comp in msg_obj.message:
            if isinstance(comp, Reply) and comp.chain:
                for node in comp.chain:
                    if isinstance(node, Image):
                        try:
                            images.append(await node.convert_to_base64())
                        except: pass
        return images

    async def _check_permission(self, event: AstrMessageEvent) -> tuple[bool, str]:
        """
        核心风控逻辑：白名单 -> 限流 -> 余额
        """
        gid = event.get_group_id()
        uid = event.get_sender_id()
        
        # 1. 白名单检查
        whitelist = self.config.get("group_whitelist", [])
        if whitelist and gid and str(gid) not in whitelist:
            return False, "" # 非白名单群，静默忽略

        # 2. 群组限流检查
        limit = self.config.get("rate_limit_max", 5)
        if gid and limit > 0:
            async with self.rate_limit_lock:
                now = time.time()
                start, count = self.rate_limit_state.get(str(gid), (now, 0))
                
                # 如果窗口已过 (60秒)，重置
                if now - start > 60: 
                    start, count = now, 0 
                
                if count >= limit:
                    return False, "🚫 本群调用太频繁 (Rate Limit)，请稍后再试。"
                
                # 更新计数
                self.rate_limit_state[str(gid)] = (start, count + 1)

        # 3. 余额检查
        cost = self.config.get("cost_per_image", 10)
        current_pts = self._get_points(uid)
        if current_pts < cost:
            return False, f"💸 积分不足！\n本次需要 {cost}，当前余额 {current_pts}。\n请发送 /签到 获取积分。"
            
        return True, None

    # ==========================
    #        指令处理
    # ==========================

    @filter.command("签到")
    async def checkin(self, event: AstrMessageEvent):
        """每日签到领取积分"""
        uid = str(event.get_sender_id())
        today = time.strftime("%Y-%m-%d")
        
        if uid not in self.user_data: self.user_data[uid] = {}
        last = self.user_data[uid].get("last_checkin", "")
        
        if last == today:
            yield event.plain_result(f"📅 今天已签到！\n当前积分: {self._get_points(uid)}")
            return

        reward = random.randint(
            self.config.get("checkin_reward_min", 20),
            self.config.get("checkin_reward_max", 100)
        )
        self._add_points(uid, reward)
        self.user_data[uid]["last_checkin"] = today
        self._save_data()
        
        yield event.plain_result(f"🎉 签到成功 +{reward} 积分！\n当前余额: {self._get_points(uid)}")

    @filter.command("gen")
    async def gen_image(self, event: AstrMessageEvent, prompt: str = ""):
        """文生图指令"""
        if not prompt:
            yield event.plain_result("请提供描述，例如: /gen 赛博朋克猫猫")
            return

        allow, msg = await self._check_permission(event)
        if not allow:
            if msg: yield event.plain_result(msg)
            return

        yield event.plain_result(f"🎨 正在绘制 (gen) ...")
        
        # 【修复】使用 async for 迭代生成器
        async for res in self._execute_generation(event, prompt, None):
            yield res

    @filter.command("alt")
    async def alt_image(self, event: AstrMessageEvent, prompt: str = ""):
        """图生图 / 改图指令"""
        images = await self._collect_images(event)
        if not images:
            yield event.plain_result("请发送图片并使用 /alt 指令，或回复一张图片使用该指令。")
            return
            
        if not prompt: prompt = "Enhance this image"

        allow, msg = await self._check_permission(event)
        if not allow:
            if msg: yield event.plain_result(msg)
            return

        yield event.plain_result(f"🎨 正在重绘 (alt) ...")
        
        # 【修复】使用 async for 迭代生成器
        async for res in self._execute_generation(event, prompt, images):
            yield res

    @filter.command("met")
    async def met_image(self, event: AstrMessageEvent, style: str = ""):
        """预设变身指令"""
        images = await self._collect_images(event)
        if not images:
            yield event.plain_result("请发送图片并使用 /met [风格] 指令。")
            return

        # 解析预设映射
        preset_map = {}
        for item in self.config.get("prompt_map", []):
            if ":" in item:
                k, v = item.split(":", 1)
                preset_map[k.strip()] = v.strip()

        if not style or style not in preset_map:
            styles = "、".join(preset_map.keys())
            yield event.plain_result(f"请指定风格: /met [风格]\n可用风格: {styles}")
            return

        final_prompt = preset_map[style]
        
        allow, msg = await self._check_permission(event)
        if not allow:
            if msg: yield event.plain_result(msg)
            return

        yield event.plain_result(f"🎨 正在变身 (met: {style})...")
        
        # 【修复】使用 async for 迭代生成器
        async for res in self._execute_generation(event, final_prompt, images):
            yield res

    async def _execute_generation(self, event, prompt, images):
        """统一的生成执行与扣费逻辑 (这是一个异步生成器)"""
        # 1. 调用 TTP 驱动
        path, err = await ttp.generate_image(
            prompt=prompt,
            api_keys=self.config.get("api_keys", []),
            model=self.config.get("model", "gemini-3-pro-image-preview"),
            api_base=self.config.get("api_base", "https://tiantianai.pro"),
            input_images=images
        )

        if path:
            # 2. 成功后扣费
            cost = self.config.get("cost_per_image", 10)
            uid = event.get_sender_id()
            if self._deduct_points(uid, cost):
                # 3. 发送图片
                yield event.chain_result([
                    Image.fromFileSystem(path),
                    Plain(f" | ✅ 消耗 {cost} 积分")
                ])
            else:
                yield event.plain_result("❌ 扣费失败，可能是并发导致余额不足。")
        else:
            yield event.plain_result(f"❌ 生成失败: {err}")

    @filter.command("积分")
    async def query_points(self, event: AstrMessageEvent):
        """查询当前积分"""
        uid = event.get_sender_id()
        pts = self._get_points(uid)
        yield event.plain_result(f"💰 当前积分: {pts}")

    @filter.command("Arcadia添加Key")
    async def add_key(self, event: AstrMessageEvent, key: str):
        """管理员添加 Key"""
        admin_ids = self.context.get_config().get("admins_id", [])
        if event.get_sender_id() not in admin_ids:
            return 
            
        keys = self.config.get("api_keys", [])
        if key not in keys:
            keys.append(key)
            await self.config.save_config()
            yield event.plain_result(f"✅ Key 添加成功，当前共有 {len(keys)} 个 Key。")
        else:
            yield event.plain_result("Key 已存在。")
