import httpx
import numpy as np
import cv2
from typing import Optional

def _encode_png(img_bgr: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", img_bgr)
    if not ok:
        raise ValueError("Failed to encode image as PNG")
    return buf.tobytes()

def _decode_png(png_bytes: bytes) -> np.ndarray:
    arr = np.frombuffer(png_bytes, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("Failed to decode PNG from response")
    return img

class HFRemoteColorizer:
    """
    Client for your HF Space endpoint:
      - URL must be .../predict.bin
      - Request: multipart 'image' field
      - Response: binary image/png
    """
    def __init__(self, api_url: str, timeout: float = 60.0):
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout
        self._async_client: Optional[httpx.AsyncClient] = None
        self._sync_client: Optional[httpx.Client] = None

    # ---------- sync ----------
    def _get_sync(self) -> httpx.Client:
        if self._sync_client is None:
            self._sync_client = httpx.Client(
                timeout=self.timeout,
                headers={"User-Agent": "HFRemoteColorizer/1.2", "Accept": "image/png"},
            )
        return self._sync_client

    def process(self, image_bgr: np.ndarray) -> Optional[np.ndarray]:
        try:
            png = _encode_png(image_bgr)
            files = {"image": ("input.png", png, "image/png")}
            resp = self._get_sync().post(self.api_url, files=files)
            resp.raise_for_status()
            return _decode_png(resp.content)  # <-- read binary content
        except Exception as e:
            print(f"[HFRemoteColorizer] sync error: {e}")
            return None

    # ---------- async ----------
    async def _get_async(self) -> httpx.AsyncClient:
        if self._async_client is None:
            self._async_client = httpx.AsyncClient(
                timeout=self.timeout,
                headers={"User-Agent": "HFRemoteColorizer/1.2", "Accept": "image/png"},
            )
        return self._async_client

    async def process_async(self, image_bgr: np.ndarray) -> Optional[np.ndarray]:
        try:
            png = _encode_png(image_bgr)
            files = {"image": ("input.png", png, "image/png")}
            client = await self._get_async()
            resp = await client.post(self.api_url, files=files)
            resp.raise_for_status()
            return _decode_png(resp.content)  # <-- read binary content
        except Exception as e:
            print(f"[HFRemoteColorizer] async error: {e}")
            return None

    # ---------- cleanup ----------
    def close(self):
        if self._sync_client is not None:
            self._sync_client.close()
            self._sync_client = None

    async def aclose(self):
        if self._async_client is not None:
            await self._async_client.aclose()
            self._async_client = None
