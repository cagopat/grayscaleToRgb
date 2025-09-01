# Grayscale → RGB
<p align="center">
  <img src="https://github.com/user-attachments/assets/2501ad5a-a175-492e-aae4-9a89a6e0eb33" alt="Grayscale" width="49%">
  <img src="https://github.com/user-attachments/assets/9bcfaeff-aa69-4a33-969b-3cd1162f613b" alt="Colorized" width="49%">
</p>



FastAPI service with a lightweight frontend to colorize grayscale images.  
Includes Redis-backed rate limiting, streaming uploads with size caps, Prometheus metrics, and per-session result browsing & downloads.

---

## Model

Uses a GAN-based colorizer derived from:  
**DDColor: Towards Photo-Realistic Image Colorization via Dual Decoders**  
*Xiaoyang Kang, Tao Yang, Wenqi Ouyang, Peiran Ren, Lingzhi Li, Xuansong Xie*  

The model is hosted on Hugging Face, and the backend calls it through a small client (`HFRemoteColorizer`) configured via `API_URL`.

---

## Features

### REST API
- `GET /health` – Service status & config  
- `POST /upload/check` – UX pre-check (count/rate hints; not enforcement)  
- `POST /api/colorize` – Colorize 1–5 images per request (PNG/JPEG/WEBP)  
- `GET /api/result/{session}/{filename}` – Fetch a colorized image  
- `GET /api/results/{session}` – List results for a session  
- `POST /api/predict` – Single image in → PNG out (raw bytes)  
- `GET /metrics` – Prometheus metrics  

### Client UX
- Drag-and-drop interface  
- Previews with size/type validation  
- Session tokens and fingerprinting  

### Safeguards
- Redis rate limiting (per IP + fingerprint)  
- 1 MB hard file cap  
- libmagic MIME type checks  
- Bounded concurrency  
- Temporary/session storage cleanup  



## Prerequisites

- Python 3.11+  
- Redis (recommended: Redis Cloud URL; no local install required)  
- libmagic (installed by Dockerfile; on macOS: `brew install libmagic`)  
