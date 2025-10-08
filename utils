# utils/shutdown.py
import asyncio
import os

SHUTDOWN_TIME = int(os.getenv("SHUTDOWN_TIME", "300"))  # detik
_shutdown_task = None
_last_activity = None

async def shutdown_bot():
    print(f"⏹️ Bot Otomatis Mati Dikarenakan Tidak Digunakan Selama {SHUTDOWN_TIME} Detik")
    os._exit(0)

async def _countdown():
    """Loop yang terus memeriksa waktu aktivitas terakhir"""
    global _last_activity
    while True:
        await asyncio.sleep(5)
        if _last_activity is not None:
            elapsed = asyncio.get_event_loop().time() - _last_activity
            if elapsed > SHUTDOWN_TIME:
                await shutdown_bot()

async def reset_shutdown_timer():
    """Reset waktu aktivitas (dipanggil setiap ada perintah/pesan)"""
    global _shutdown_task, _last_activity
    _last_activity = asyncio.get_event_loop().time()
    
    print("⏳ Aktivitas terdeteksi — timer direset ulang.")

    if _shutdown_task is None:
        _shutdown_task = asyncio.create_task(_countdown())
