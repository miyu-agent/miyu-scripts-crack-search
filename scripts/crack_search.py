#!/usr/bin/env python3
# Display name: Game crack status
# 显示名称：游戏破解状态查询
# Description: Check whether a PC game has been cracked: status, DRM, release and crack dates and the scene group, from crackrelease.com and isitcracked.com. Chinese names are translated through Steam first.
# 描述：查一款 PC 游戏有没有被破解：破解状态、DRM、发行和破解日期、发布组；中文名先经 Steam 转成英文名再查。
# Timeout: 120
# Clicall: yes
# Parameters:
# {"type": "object", "properties": {
#   "query": {"type": "string", "description": "Game name. English works best; a Chinese name is looked up on Steam for its English name."},
#   "limit": {"type": "integer", "description": "Results per source, 1-20. Default 5."},
#   "format": {"type": "string", "enum": ["json", "md"], "description": "json (default): structured, one object per game, easy to read in code. md: the same facts as prose lines."}
# }, "required": ["query"]}
import html
import json
import re
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

UA = "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
TIMEOUT = 20
# isitcracked.com reads its database straight from Supabase with this public anonymous key (it ships in the site's JS).
SUPABASE_URL = "https://lhvknkrfhehcclzlabsl.supabase.co"
SUPABASE_KEY = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imxodmtua3JmaGVoY2NsemxhYnNsIiwicm9sZSI6"
                "ImFub24iLCJpYXQiOjE3Nzg4NzM0MTksImV4cCI6MjA5NDQ0OTQxOX0.B7YOW_hpn2zHxR-sfHgiNgqidpfESwJpixLrh-MevE8")
STATUS = {"CRACKED": "已破解", "UNCRACKED": "未破解", "UNRELEASED": "未发售", "HYPERVISOR": "Hypervisor 破解"}
# "已破解" 是有见证的主张（有具体发布），"未破解" 只是某站库里没有记录。两站打架时以 cracked 类为准。
CRACKED_LIKE = {"CRACKED", "HYPERVISOR"}
CJK = re.compile(r"[぀-ヿ㐀-鿿가-힯]")

# Search result card: <article class="crui-search-game ..."> ... <span class="crui-search-game__status"><i></i>CRACKED<b>D+15</b>
CARD_RE = re.compile(r'<article[^>]*class="[^"]*crui-search-game[^"]*"[^>]*>(.*?)</article>', re.S)
CARD_LINK_RE = re.compile(r'<h3[^>]*>\s*<a[^>]*href="(https://crackrelease\.com/[^"]+)"[^>]*>(.*?)</a>', re.S)
CARD_STATUS_RE = re.compile(r'crui-search-game__status[^>]*>\s*(?:<i[^>]*>\s*</i>\s*)?([A-Za-z]+)\s*(?:<b[^>]*>\s*([^<]*?)\s*</b>)?', re.S)
# Detail page: <div class="k">CRACK DATE</div><div class="v">Mar 12, 2022</div>
META_LABELS = {"RELEASE DATE": "release_date", "CRACK DATE": "crack_date", "DRM PROTECTION": "drm", "SCENE GROUP": "group"}


class Failed(Exception):
    pass


def get(url, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.5", **(headers or {})})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read().decode("utf-8", "replace")


def describe(exc):
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"
    if isinstance(exc, urllib.error.URLError):
        return f"连不上：{exc.reason}"
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return f"{TIMEOUT} 秒没有回应"
    return f"{type(exc).__name__}: {exc}"


def text_of(fragment):
    return html.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()


# ---------------------------------------------------------------- Steam: Chinese name -> English name

def english_name(query):
    """Steam's store search matches Chinese names in the schinese locale; appdetails in English gives the English one.
    It only matches whole name fragments (怪物猎人荒野 finds nothing, 怪物猎人 finds Wilds), so the name is shortened
    from the end until something matches, and the match is reported as a guess."""
    term = query.strip()
    hits = []
    while len(term) >= 2 and not hits:
        quoted = urllib.parse.quote(term)
        hits = json.loads(get(f"https://store.steampowered.com/api/storesearch/?term={quoted}&l=schinese&cc=CN")).get("items") or []
        if not hits:
            term = term[:-1].rstrip(" ：:·-")
    if not hits:
        return None
    app = hits[0]
    details = json.loads(get(f"https://store.steampowered.com/api/appdetails?appids={app['id']}&l=english&filters=basic"))
    data = (details.get(str(app["id"])) or {}).get("data") or {}
    if not data.get("name"):
        return None
    return {"steam_name": app.get("name"), "english": data["name"], "appid": app["id"],
            "matched": term, "guess": term != query.strip(), "others": [h.get("name") for h in hits[1:4]]}


# ---------------------------------------------------------------- crackrelease.com

def crackrelease(query, limit):
    page = get(f"https://crackrelease.com/?s={urllib.parse.quote(query)}&cr_type=games")
    cards, seen = [], set()
    for block in CARD_RE.findall(page):
        link = CARD_LINK_RE.search(block)
        if not link or link.group(1) in seen:
            continue
        seen.add(link.group(1))
        status = CARD_STATUS_RE.search(block)
        cards.append({"title": text_of(link.group(2)), "url": link.group(1),
                      "status": status.group(1).upper() if status else "UNKNOWN",
                      "days": (status.group(2) or "").strip() if status else ""})
    if not cards and "crui-search-empty" not in page:
        # A page full of HTML with no cards and no "no results" block means the layout changed, not that
        # the game is missing; saying "not found" here is how this broke silently once before.
        raise Failed("crackrelease.com 的页面结构变了，解析不出结果（不代表没收录），脚本要更新解析规则。")
    cards = cards[:limit]
    # The list already carries status and D+N; detail pages only add DRM, dates and group.
    with ThreadPoolExecutor(max_workers=4) as pool:
        for card, meta in zip(cards, pool.map(detail, [c["url"] for c in cards])):
            card.update(meta)
            card["source"] = "crackrelease.com"
    return cards


def detail(url):
    try:
        page = get(url)
    except Exception:  # noqa: BLE001 - a missing detail page only loses the extra fields
        return {}
    meta = {}
    for label, key in META_LABELS.items():
        match = re.search(rf'>\s*{label}\s*</div>\s*<div[^>]*>(.*?)</div>', page, re.S | re.I)
        if match and text_of(match.group(1)):
            meta[key] = text_of(match.group(1))
    return meta


# ---------------------------------------------------------------- isitcracked.com

def isitcracked(query, limit):
    pattern = urllib.parse.quote(f"*{query}*")
    url = (f"{SUPABASE_URL}/rest/v1/games?select=title,slug,status,crack_date,release_date,drm_protection,scene_group,"
           f"secondary_protections&or=(title.ilike.{pattern},scene_group.ilike.{pattern})&order=title&limit={limit}")
    rows = json.loads(get(url, {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}))
    games = []
    for row in rows:
        drm = row.get("drm_protection") or ""
        if row.get("secondary_protections"):
            drm = " + ".join([drm] + row["secondary_protections"]) if drm else " + ".join(row["secondary_protections"])
        games.append({"title": row.get("title") or "?", "status": (row.get("status") or "unknown").upper(), "days": "",
                      "drm": drm, "release_date": row.get("release_date") or "", "crack_date": row.get("crack_date") or "",
                      "group": row.get("scene_group") or "", "source": "isitcracked.com",
                      "url": f"https://isitcracked.com/game/{row['slug']}" if row.get("slug") else ""})
    return games


# ---------------------------------------------------------------- run

def as_other(game):
    return {"source": game["source"], "status": game["status"], "group": game["group"],
            "crack_date": game["crack_date"], "url": game["url"]}


def search_all(query, limit, notes):
    games, failed = [], []
    try:
        games += crackrelease(query, limit)
    except Failed as exc:
        notes.append(str(exc))
        failed.append("crackrelease.com")
    except Exception as exc:  # noqa: BLE001
        notes.append(f"crackrelease.com 不可用（{describe(exc)}）")
        failed.append("crackrelease.com")
    try:
        known = {g["title"].lower(): g for g in games}
        for game in isitcracked(query, limit):
            twin = known.get(game["title"].lower())
            if twin is None:
                games.append(game)
            elif twin["status"] != game["status"]:
                # The two sites disagree now and then (hypervisor bypasses especially).
                # A cracked claim has a witness behind it, an uncracked one is just a gap in a database,
                # so the cracked side wins and the other is kept as a note.
                if game["status"] in CRACKED_LIKE and twin["status"] not in CRACKED_LIKE:
                    game["other_source"] = as_other(twin)
                    games[games.index(twin)] = game
                else:
                    twin["other_source"] = as_other(game)
    except Exception as exc:  # noqa: BLE001
        notes.append(f"isitcracked.com 不可用（{describe(exc)}）")
        failed.append("isitcracked.com")
    return games, failed


def run(args):
    if not isinstance(args, dict):
        raise Failed('参数要是一个 JSON 对象，例：{"query": "Elden Ring"}')
    query = str(args.get("query") or "").strip()
    if not query:
        raise Failed('要给 query（游戏名），例：{"query": "Elden Ring"}')
    limit = args.get("limit", 5)
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise Failed("limit 要是整数。")
    limit = max(1, min(limit, 20))
    fmt = args.get("format") or "json"
    if fmt not in ("md", "json"):
        raise Failed("format 只能是 md 或 json。")

    notes, searched, translated = [], query, None
    if CJK.search(query):
        try:
            translated = english_name(query)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"Steam 查英文名失败（{describe(exc)}），直接用原名查")
        if translated:
            searched = translated["english"]
        elif not notes:
            notes.append("Steam 上找不到这个中文名，直接用原名查；两个站都只认英文名，最好换英文名再试")
    games, failed = search_all(searched, limit, notes)
    if len(failed) == 2:
        raise Failed("两个数据源都不可用：" + "；".join(notes))
    result = {"query": query, "searched": searched, "steam": translated, "games": games,
              "sources_failed": failed, "notes": notes}
    return result if fmt == "json" else render(result)


def known(value):
    return bool(value) and value.strip().upper() not in ("TBD", "N/A", "UNKNOWN")


def days_text(status, days):
    """crackrelease's D+N counts from release: days to crack when cracked, days so far when not."""
    match = re.fullmatch(r"D([+-])(\d+)", (days or "").strip(), re.I)
    if not match:
        return ""
    n = int(match.group(2))
    if status == "UNCRACKED":
        return f"发售 {n} 天仍未破解" if match.group(1) == "+" else f"距发售还有 {n} 天"
    if match.group(1) == "-":
        return f"发售前 {n} 天就破解"
    return "发售当天破解" if n == 0 else f"发售后 {n} 天破解"


def render(result):
    lines = []
    if result["steam"]:
        s = result["steam"]
        lines.append(f"「{result['query']}」→ Steam：{s['steam_name']} → 英文名 **{s['english']}**（appid {s['appid']}）")
        if s["guess"]:
            lines.append(f"（Steam 搜不到全名，按「{s['matched']}」匹配的第一个，可能不是你要的；"
                         f"其他候选：{'、'.join(s['others']) or '无'}。不对就直接给英文名重查）")
    if not result["games"]:
        lines.append(f"搜「{result['searched']}」：两个站都没有收录。换个写法（完整英文名、去掉副标题）再试。")
    else:
        lines.append(f"搜「{result['searched']}」：{len(result['games'])} 条")
    for i, g in enumerate(result["games"], 1):
        status = STATUS.get(g["status"], g["status"])
        when = days_text(g["status"], g.get("days"))
        facts = [f"状态：{status}" + (f"（{when}）" if when else "")]
        facts += [f"{label}：{g[key]}" for key, label in (("drm", "DRM"), ("release_date", "发行"),
                                                          ("crack_date", "破解"), ("group", "发布组")) if known(g.get(key))]
        lines += ["", f"{i}. **{g['title']}** — {' · '.join(facts)}", f"   {g['source']}：{g['url']}"]
        other = g.get("other_source")
        if other:
            extra = [f"{label}：{other[key]}" for key, label in (("crack_date", "破解"), ("group", "发布组")) if known(other.get(key))]
            lines.append(f"   两站说法不一：{other['source']} 记为「{STATUS.get(other['status'], other['status'])}」"
                         + (f"（{' · '.join(extra)}）" if extra else "") + f"：{other['url']}")
    if result["notes"]:
        lines += ["", "注意：" + "；".join(result["notes"])]
    return "\n".join(lines)


def main():
    try:
        args = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        print(f"错误：参数不是 JSON：{exc}")
        sys.exit(1)
    try:
        result = run(args)
    except Failed as exc:
        print(f"错误：{exc}")
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001 - the caller wants a reason, not a traceback
        print(f"错误：{type(exc).__name__}: {exc}")
        sys.exit(1)
    print(result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
