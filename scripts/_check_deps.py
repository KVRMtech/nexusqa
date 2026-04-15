"""Quick check of available ML dependencies."""
import shutil

checks = {
    "whisper": False,
    "cv2 (opencv)": False,
    "easyocr": False,
    "pyannote": False,
    "playwright": False,
    "httpx": False,
    "ffmpeg (system)": shutil.which("ffmpeg") is not None,
}

try:
    import whisper
    checks["whisper"] = True
except Exception:
    pass

try:
    import cv2
    checks["cv2 (opencv)"] = True
except Exception:
    pass

try:
    import easyocr
    checks["easyocr"] = True
except Exception:
    pass

try:
    import pyannote
    checks["pyannote"] = True
except Exception:
    pass

try:
    from playwright.sync_api import sync_playwright
    checks["playwright"] = True
except Exception:
    pass

try:
    import httpx
    checks["httpx"] = True
except Exception:
    pass

print("=" * 40)
print("Nexus QA Dependency Check")
print("=" * 40)
for name, available in checks.items():
    status = "YES" if available else "NO"
    print(f"  {name:20s} : {status}")
print("=" * 40)
