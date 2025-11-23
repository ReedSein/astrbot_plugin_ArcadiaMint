import aiohttp
import asyncio
import aiofiles
import base64
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from astrbot.api import logger

class ImageGeneratorState:
    """
    并发状态管理器 (移植自 OpenAI-Img)
    确保在高并发下 API Key 轮询的线程安全
    """
    def __init__(self):
        self.api_key_index = 0
        self._lock = asyncio.Lock()
    
    async def get_next_api_key(self, api_keys):
        async with self._lock:
            if not api_keys: return None
            return api_keys[self.api_key_index % len(api_keys)]
    
    async def rotate_key(self, api_keys):
        async with self._lock:
            if len(api_keys) > 1:
                self.api_key_index = (self.api_key_index + 1) % len(api_keys)
                logger.info(f"[ArcadiaMint] API Key 轮换至索引 {self.api_key_index}")

# 全局状态单例
_state = ImageGeneratorState()

async def cleanup_old_images():
    """
    文件清理服务 (移植自 OpenAI-Img)
    自动清理 15 分钟前生成的临时图片，防止硬盘爆炸
    """
    try:
        # 定位到插件根目录下的 images 文件夹
        data_dir = Path(__file__).parent.parent / "images"
        if not data_dir.exists(): return

        cutoff = datetime.now() - timedelta(minutes=15)
        for f in data_dir.glob("*.png"):
            try:
                if datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
                    f.unlink()
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"[ArcadiaMint] 清理旧图片失败: {e}")

async def save_base64_image(b64_str):
    """
    解码 Base64 并保存为本地文件
    """
    try:
        data_dir = Path(__file__).parent.parent / "images"
        data_dir.mkdir(exist_ok=True)
        
        # 每次保存前顺手清理过期文件
        await cleanup_old_images()

        img_data = base64.b64decode(b64_str)
        filename = f"mint_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{str(uuid.uuid4())[:8]}.png"
        path = data_dir / filename
        
        async with aiofiles.open(path, "wb") as f:
            await f.write(img_data)
        
        logger.info(f"[ArcadiaMint] 图片保存成功: {path.name}")
        return str(path.absolute())
    except Exception as e:
        logger.error(f"[ArcadiaMint] 保存图片失败: {e}")
        return None

async def generate_image(prompt, api_keys, model, api_base, input_images=None):
    """
    核心生成逻辑 (针对 Tiantianai.pro 优化)
    
    Args:
        prompt: 提示词
        api_keys: 密钥列表
        model: 模型名称
        api_base: API 基础地址
        input_images: Base64 图片列表 (用于图生图)
    """
    base_url = api_base.rstrip("/")
    
    # 设置总超时 60s
    timeout = aiohttp.ClientTimeout(total=60)
    
    async with aiohttp.ClientSession(timeout=timeout) as session:
        # 最多重试 3 次 (自动换 Key)
        for attempt in range(3):
            key = await _state.get_next_api_key(api_keys)
            if not key: return None, "未配置 API Key"

            headers = {
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json"
            }

            try:
                # --- 路由逻辑 ---
                if input_images:
                    # 场景 A: 图生图 (Edit) -> /v1/images/edits
                    url = f"{base_url}/v1/images/edits"
                    
                    # 处理输入图片格式，确保是 data URI 格式
                    img_b64 = input_images[0]
                    if not img_b64.startswith("data:image"):
                        img_b64 = f"data:image/png;base64,{img_b64}"
                        
                    # Tiantianai 特供 payload: 支持直接传 base64 json，无需 form-data
                    payload = {
                        "model": model,
                        "prompt": prompt,
                        "image": img_b64,
                        "n": 1,
                        "response_format": "b64_json"
                    }
                    logger.info(f"[ArcadiaMint] 调用 Edit 接口 (Try {attempt+1}): {model}")
                else:
                    # 场景 B: 文生图 (Gen) -> /v1/images/generations
                    url = f"{base_url}/v1/images/generations"
                    payload = {
                        "model": model,
                        "prompt": prompt,
                        "n": 1,
                        "response_format": "b64_json"
                    }
                    logger.info(f"[ArcadiaMint] 调用 Gen 接口 (Try {attempt+1}): {model}")

                # --- 发送请求 ---
                async with session.post(url, json=payload, headers=headers) as resp:
                    if resp.status != 200:
                        err_text = await resp.text()
                        logger.warning(f"[ArcadiaMint] API Error {resp.status}: {err_text[:200]}")
                        # 遇到错误，轮换 Key 并重试
                        await _state.rotate_key(api_keys)
                        continue

                    data = await resp.json()
                    
                    # --- 解析响应 (OpenAI 标准格式) ---
                    # 格式通常为: { "data": [ { "b64_json": "..." } ] }
                    target_b64 = None
                    
                    if "data" in data and data["data"]:
                        item = data["data"][0]
                        if "b64_json" in item:
                            target_b64 = item["b64_json"]
                        elif "url" in item:
                            # 如果返回 URL，下载它
                            logger.info("[ArcadiaMint] API 返回了 URL，正在下载...")
                            async with session.get(item["url"]) as img_resp:
                                if img_resp.status == 200:
                                    target_b64 = base64.b64encode(await img_resp.read()).decode()
                        
                        if target_b64:
                            path = await save_base64_image(target_b64)
                            if path:
                                return path, None
                            else:
                                return None, "图片保存失败"
                    
                    logger.warning(f"[ArcadiaMint] 响应中未找到图片数据: {str(data)[:100]}")
            
            except Exception as e:
                logger.error(f"[ArcadiaMint] 请求异常: {e}")
                await _state.rotate_key(api_keys)
    
    return None, "重试次数耗尽或 API 持续报错，请检查日志喵"
