import aiohttp
import asyncio
import aiofiles
import base64
import uuid
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from astrbot.api import logger

class ImageGeneratorState:
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

_state = ImageGeneratorState()

async def cleanup_old_images():
    try:
        data_dir = Path(__file__).parent.parent / "images"
        if not data_dir.exists(): return
        cutoff = datetime.now() - timedelta(minutes=15)
        for f in data_dir.glob("*.png"):
            try:
                if datetime.fromtimestamp(f.stat().st_mtime) < cutoff: f.unlink()
            except: pass
    except Exception as e:
        logger.warning(f"[ArcadiaMint] 清理旧图片失败: {e}")

async def save_base64_image(b64_str):
    try:
        data_dir = Path(__file__).parent.parent / "images"
        data_dir.mkdir(exist_ok=True)
        await cleanup_old_images()

        b64_str = b64_str.replace("\n", "").replace(" ", "")
        img_data = base64.b64decode(b64_str)
        filename = f"mint_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{str(uuid.uuid4())[:8]}.png"
        path = data_dir / filename
        
        async with aiofiles.open(path, "wb") as f:
            await f.write(img_data)
        
        return str(path.absolute())
    except Exception as e:
        logger.error(f"[ArcadiaMint] 保存图片失败: {traceback.format_exc()}")
        return None

async def generate_image(prompt, api_keys, model, api_base, timeout_seconds=60, input_images=None):
    """
    核心生成逻辑 (支持自定义超时)
    """
    base_url = api_base.rstrip("/")
    if base_url.endswith("/v1"): base_url = base_url[:-3]
    
    # 使用传入的超时时间
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for attempt in range(3):
            key = await _state.get_next_api_key(api_keys)
            if not key: return None, "未配置 API Key"

            headers = {
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json"
            }

            try:
                if input_images:
                    url = f"{base_url}/v1/images/edits"
                    img_b64 = input_images[0]
                    if not img_b64.startswith("data:image"):
                        img_b64 = f"data:image/png;base64,{img_b64}"
                    payload = {
                        "model": model, "prompt": prompt,
                        "image": img_b64, "n": 1, "response_format": "b64_json"
                    }
                    action_name = "Edit"
                else:
                    url = f"{base_url}/v1/images/generations"
                    payload = {
                        "model": model, "prompt": prompt,
                        "n": 1, "response_format": "b64_json"
                    }
                    action_name = "Gen"

                logger.info(f"[ArcadiaMint] {action_name} 请求 -> {url} (Model: {model}, Timeout: {timeout_seconds}s)")

                async with session.post(url, json=payload, headers=headers) as resp:
                    if resp.status != 200:
                        err_text = await resp.text()
                        logger.warning(f"[ArcadiaMint] API 错误 HTTP {resp.status}: {err_text}")
                        await _state.rotate_key(api_keys)
                        continue

                    try:
                        data = await resp.json()
                    except:
                        raw = await resp.text()
                        logger.error(f"[ArcadiaMint] 非 JSON 响应: {raw[:200]}")
                        await _state.rotate_key(api_keys)
                        continue
                    
                    target_b64 = None
                    if "data" in data and isinstance(data["data"], list) and len(data["data"]) > 0:
                        item = data["data"][0]
                        if "b64_json" in item:
                            target_b64 = item["b64_json"]
                        elif "url" in item:
                            logger.info("[ArcadiaMint] 下载图片 URL...")
                            async with session.get(item["url"]) as img_resp:
                                if img_resp.status == 200:
                                    target_b64 = base64.b64encode(await img_resp.read()).decode()
                    
                    if target_b64:
                        path = await save_base64_image(target_b64)
                        if path: return path, None
                        else: return None, "保存失败"
                    else:
                        logger.warning(f"[ArcadiaMint] 无图片数据: {str(data)[:200]}")
            
            except asyncio.TimeoutError:
                logger.warning(f"[ArcadiaMint] 请求超时 (Try {attempt+1})")
                await _state.rotate_key(api_keys)
            except Exception:
                logger.error(f"[ArcadiaMint] 未知异常: {traceback.format_exc()}")
                await _state.rotate_key(api_keys)
    
    return None, "作图失败了，请检查日志喵"
