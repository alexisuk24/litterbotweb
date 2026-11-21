"""Cat profile photos: the whole body is saved even when it arrives in many chunks."""
import asyncio

import aiohttp
from aiohttp import web as aioweb

import lr_clips
import lr_db

BODY = bytes(range(256)) * 2000  # 512 KB


async def _serve_in_chunks(request):
    resp = aioweb.StreamResponse(headers={"Content-Type": "image/jpeg"})
    await resp.prepare(request)
    for i in range(0, len(BODY), 7000):
        await resp.write(BODY[i:i + 7000])
        await asyncio.sleep(0.001)
    return resp


def test_photo_saved_whole_when_sent_in_chunks(env, tmp_path):
    env.pet("PET-1", "Ida")

    async def run():
        app = aioweb.Application()
        app.router.add_get("/p.jpg", _serve_in_chunks)
        runner = aioweb.AppRunner(app)
        await runner.setup()
        site = aioweb.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            async with aiohttp.ClientSession() as http:
                await lr_clips.save_pet_photo(env.db, http, tmp_path / "photos", "PET-1",
                                              f"http://127.0.0.1:{port}/p.jpg?sig=1")
        finally:
            await runner.cleanup()

    asyncio.run(run())
    saved = list((tmp_path / "photos").iterdir())
    assert len(saved) == 1 and saved[0].read_bytes() == BODY


def test_truncated_photos_are_forgotten_by_the_migration(env):
    env.pet("PET-1", "Ida")
    env.db.execute("UPDATE pets SET photo_file = 'PET-1.jpg', photo_key = 'k'")
    env.db.execute("PRAGMA user_version = 0")
    env.db.commit()
    conn = lr_db.connect()
    row = conn.execute("SELECT photo_file, photo_key FROM pets").fetchone()
    assert (row["photo_file"], row["photo_key"]) == (None, None)
