import json
import random
from datetime import datetime

from . import ncm_client as ncm

PLAYLIST_NAME = "魔理推荐的歌单"
TARGET_SIZE = 30
STATE_FILE = ncm.NCM_DATA_DIR / "marisa_daily.json"


def _load_state() -> dict:
    try:
        if STATE_FILE.exists():
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        print(f"[ncm_recommend] state load error: {e}")
    return {}


def _save_state(state: dict):
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[ncm_recommend] state save error: {e}")


def _dedupe(songs):
    seen = set()
    out = []
    for s in songs:
        sid = s.get("id")
        if not sid or sid in seen:
            continue
        seen.add(sid)
        out.append(s)
    return out


def _fmt(song) -> str:
    name = song.get("name") or ""
    artist = song.get("artist") or ""
    return f"{name} - {artist}".rstrip(" -")


def _build_playlist(dry_run: bool = False) -> dict:
    """合成 30 首并覆写固定歌单。dry_run=True 只合成不上传。"""
    logged_in, _ = ncm.check_login()
    if not logged_in:
        return {"success": False, "message": "未登录网易云，无法做魔理推荐（去设置或对我说扫码登录）"}

    freq = ncm.frequent_songs(50) or []
    liked_pl = ncm.liked_playlist()
    liked = ncm.playlist_songs(liked_pl["id"]) if liked_pl else []
    all_pls = ncm.user_playlists()
    # 目标歌单从全量列表找（不能预先排除同名！）
    target = next((p for p in all_pls if p.get("name") == PLAYLIST_NAME), None)
    pls = [
        p for p in all_pls
        if p.get("name") != PLAYLIST_NAME and p.get("id") != liked_pl.get("id")
    ]
    pls.sort(key=lambda p: p.get("count", 0), reverse=True)
    extras = []
    for p in pls[:2]:
        extras += ncm.playlist_songs(p["id"])[:15]

    daily_res = ncm.daily_recommendations()
    daily = daily_res.get("songs") if daily_res.get("success") else []

    freq_n = liked_n = extras_n = 0
    pool = []
    for s in _dedupe(freq):
        if freq_n >= 10 or len(pool) >= TARGET_SIZE:
            break
        pool.append(s)
        freq_n += 1
    for s in _dedupe(liked):
        if liked_n >= 10 or len(pool) >= TARGET_SIZE:
            break
        pool.append(s)
        liked_n += 1
    for s in _dedupe(extras):
        if extras_n >= 8 or len(pool) >= TARGET_SIZE:
            break
        pool.append(s)
        extras_n += 1
    for s in _dedupe(daily):
        if len(pool) >= TARGET_SIZE:
            break
        pool.append(s)

    if len(pool) < 5:
        return {"success": False, "message": f"能用的歌太少了（只凑到 {len(pool)} 首），稍后再试试だぜ"}

    seed = int(datetime.now().strftime("%Y%m%d"))
    rng = random.Random(seed)
    rng.shuffle(pool)
    picked = pool[:TARGET_SIZE]

    source_note = (
        f"常听{freq_n} + 我喜欢的音乐{liked_n} + 收藏歌单{extras_n}"
        f" + 今日推荐{max(0, TARGET_SIZE - freq_n - liked_n - extras_n)}"
    )

    if dry_run:
        return {
            "success": True, "dry_run": True, "count": len(picked),
            "source_note": source_note,
            "songs": [{"id": s["id"], "name": s.get("name", ""), "artist": s.get("artist", "")} for s in picked],
        }

    picked_ids = [s["id"] for s in picked]
    try:
        if target is None:
            pid = ncm.create_playlist(PLAYLIST_NAME)
            if not pid:
                return {"success": False, "message": "创建「魔理推荐的歌单」失败了だぜ"}
            target = {"id": pid}
        else:
            pid = target["id"]
            old = ncm.playlist_songs(pid)
            old_ids = [s["id"] for s in old]
            if old_ids and not ncm.remove_songs_from_playlist(pid, old_ids):
                return {"success": False, "message": "更新歌单时清空旧曲失败，请稍后再试だぜ"}
        # 清理历史遗留的同名歌单（只保留正在更新的这个），避免重复堆积
        for dup in all_pls:
            if dup.get("id") == pid or not dup.get("is_mine"):
                continue
            if (dup.get("name") or "").strip() == PLAYLIST_NAME:
                try:
                    ncm._api_apis().playlist.SetRemovePlaylist([dup["id"]])
                    print(f"[ncm_recommend] removed duplicate playlist {dup['id']}")
                except Exception as e:
                    print(f"[ncm_recommend] cleanup dup playlist {dup.get('id')}: {e}")
        if not ncm.add_songs_to_playlist(pid, picked_ids):
            return {"success": False, "message": "往「魔理推荐的歌单」写入歌曲失败，请稍后再试だぜ"}
    except Exception as e:
        print(f"[ncm_recommend] upsert error: {e}")
        return {"success": False, "message": f"魔理推荐写到歌单时出错：{str(e)[:80]}"}

    return {
        "success": True, "playlist_id": pid, "count": len(picked),
        "source_note": source_note,
        "first": {"id": picked[0]["id"], "name": picked[0].get("name", ""), "artist": picked[0].get("artist", "")},
    }


def ensure_daily(dry_run: bool = False) -> dict:
    """每日一次：当天已生成则直接返回现成歌单；未生成才合成刷新。
    返回 {success, created, playlist_id, count, source_note?, first?}"""
    today = datetime.now().strftime("%Y-%m-%d")
    state = _load_state()
    if state.get("date") == today and state.get("playlist_id"):
        try:
            exists = any(
                (p.get("id") == state["playlist_id"] and p.get("name") == PLAYLIST_NAME)
                for p in ncm.user_playlists()
            )
        except Exception:
            exists = False
        if exists:
            return {
                "success": True, "created": False,
                "playlist_id": state["playlist_id"], "count": state.get("count", 0),
            }

    res = _build_playlist(dry_run=dry_run)
    if not res.get("success"):
        return res
    if res.get("dry_run"):
        res["created"] = False
        return res
    _save_state({"date": today, "playlist_id": res["playlist_id"], "count": res.get("count", 0)})
    res["created"] = True
    return res


def marisa_recommend_now() -> str:
    """切到魔理推荐歌单连播（当天未生成则先生成）。返回给模型/UI 的汇报文本。"""
    res = ensure_daily()
    if not res.get("success"):
        return res.get("message", "魔理推荐失败了だぜ")
    pid = res["playlist_id"]
    label = ncm.play_playlist(pid, PLAYLIST_NAME)
    if not label:
        # 协议调起失败降级：播歌单第一首
        songs = ncm.playlist_songs(pid)
        if songs:
            first = songs[0]
            ncm.play_song(first["id"], _fmt(first))
            return (
                f"已用{res.get('source_note', '')}为你刷新了歌单「{PLAYLIST_NAME}」"
                f"（{res.get('count', 0)}首），并开播第一首《{_fmt(first)}》だぜ！♪"
            )
        return f"歌单「{PLAYLIST_NAME}」已就绪但切播失败，去网易云里点开听吧"
    count = res.get("count", 0)
    if res.get("created"):
        return (
            f"刚为你做好今日魔理推荐（{res.get('source_note', '')}，共 {count} 首）！"
            f"已切到{label}开始整单连播，切歌时我会报歌名だぜ！♪"
        )
    return (
        f"今日的{label}已就绪（共 {count} 首），直接切过去开始整单连播了だぜ！"
        "切歌时我会报歌名♪（每天会自动更新一次）"
    )
