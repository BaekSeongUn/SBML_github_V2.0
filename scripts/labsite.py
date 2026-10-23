#!/usr/bin/env python3
"""연구 주제 논문 수집 파이프라인
fetch(OpenAlex) -> 점수(관련도 + JCR) -> 순위별 선정(상위 N 자동, 다음 M 검토) -> 주간 검토 Issue -> review -> export
"""
import argparse
import csv
import datetime as dt
import glob
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config"
DECISIONS = ROOT / "data" / "decisions.json"
LAST_RUN = ROOT / "data" / "last_run.json"
OPENALEX = "https://api.openalex.org/works"
SELECT = ("id,title,display_name,authorships,primary_location,publication_year,"
          "publication_date,doi,type,open_access,abstract_inverted_index,topics")
LABELS = {"weekly-review": "5319e7"}


# ---------- 입출력 ----------
def load_yaml(name):
    with open(CONFIG / name, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_decisions():
    if DECISIONS.exists():
        return json.loads(DECISIONS.read_text(encoding="utf-8"))
    return {}


def save_decisions(d):
    DECISIONS.parent.mkdir(parents=True, exist_ok=True)
    DECISIONS.write_text(json.dumps(d, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")


# ---------- JCR (Clarivate에서 내려받은 CSV) ----------
def norm_issn(x):
    x = re.sub(r"[^0-9Xx]", "", str(x or "")).upper()
    return f"{x[:4]}-{x[4:]}" if len(x) == 8 else ""


def norm_name(x):
    return re.sub(r"[^a-z0-9]", "", str(x or "").lower())


def to_float(x):
    try:
        return float(str(x).replace(",", "").strip())
    except ValueError:
        return None  # "N/A", "<0.1" 등


def load_jcr(pattern):
    """JCR CSV(여러 파일 가능)를 읽어 {("i", ISSN)|("n", 저널명): {jif, cats{카테고리: 분위}}} 사전을 만든다.
    파일이 없으면 None. 헤더는 이름으로 찾으므로 열 순서가 달라도 된다."""
    files = sorted(glob.glob(str(ROOT / pattern)))
    if not files:
        return None
    idx = {}
    for f in files:
        lines = Path(f).read_text(encoding="utf-8-sig").splitlines()
        start = next((i for i, l in enumerate(lines[:15]) if "journal name" in l.lower() and "issn" in l.lower()), None)
        if start is None:
            print(f"[경고] {f}: 'Journal name'/'ISSN' 헤더를 찾지 못해 건너뜀")
            continue
        rdr = csv.DictReader(lines[start:])
        cols = {c: (c or "").strip().lower() for c in (rdr.fieldnames or [])}

        def col(pred):
            return next((c for c, l in cols.items() if pred(l)), None)

        c_name, c_issn, c_eissn = col(lambda l: l == "journal name"), col(lambda l: l == "issn"), col(lambda l: l == "eissn")
        c_cat = col(lambda l: l.startswith("category"))
        c_jif = col(lambda l: "jif" in l and not any(k in l for k in ("quartile", "rank", "percentile", "without")))
        c_q = col(lambda l: "quartile" in l)
        for row in rdr:
            keys = [("i", norm_issn(row.get(c_issn))), ("i", norm_issn(row.get(c_eissn))),
                    ("n", norm_name(row.get(c_name)))]
            keys = [k for k in keys if k[1]]
            if not keys:
                continue
            entry = next((idx[k] for k in keys if k in idx), None) or {"jif": None, "cats": {}}
            for k in keys:
                idx[k] = entry
            jif = to_float(row.get(c_jif))
            if jif is not None:
                entry["jif"] = jif
            cat, q = (row.get(c_cat) or "").strip(), (row.get(c_q) or "").strip().upper()
            if cat:
                entry["cats"][cat.casefold()] = q if re.fullmatch(r"Q[1-4]", q) else ""
    return idx


def jcr_lookup(p, jcr):
    if not jcr:
        return None
    for i in p.get("issn", []):
        e = jcr.get(("i", norm_issn(i)))
        if e:
            return e
    return jcr.get(("n", norm_name(p.get("journal"))))


def best_quartile(qs):
    qs = [q for q in qs if q]
    return min(qs) if qs else ""  # "Q1" < "Q2" < ...


# ---------- OpenAlex ----------
def api_get(params, mailto, retries=4):
    url = f"{OPENALEX}?{urllib.parse.urlencode(dict(params, mailto=mailto))}"
    req = urllib.request.Request(url, headers={"User-Agent": f"lab-site/1.0 (mailto:{mailto})"})
    for i in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and i < retries - 1:
                time.sleep(2 ** (i + 1))
                continue
            raise
        except urllib.error.URLError:
            if i == retries - 1:
                raise
            time.sleep(2 ** (i + 1))


def rebuild_abstract(inv):
    if not inv:
        return ""
    pos = {i: w for w, idxs in inv.items() for i in idxs}
    return " ".join(pos[i] for i in sorted(pos))


def short_id(url):
    return (url or "").rsplit("/", 1)[-1]


def normalize(w):
    src = (w.get("primary_location") or {}).get("source") or {}
    oa = w.get("open_access") or {}
    auth = w.get("authorships") or []
    issns = [x for x in [src.get("issn_l"), *(src.get("issn") or [])] if x]
    return {
        "id": short_id(w.get("id")),
        "title": (w.get("title") or w.get("display_name") or "").replace("\n", " "),
        "authors": [a["author"].get("display_name", "") for a in auth[:12]],
        "journal": src.get("display_name") or "",
        "issn": list(dict.fromkeys(issns)),
        "year": w.get("publication_year"),
        "date": w.get("publication_date"),
        "doi": w.get("doi"),
        "type": w.get("type"),
        "oa": bool(oa.get("is_oa")),
        "oa_url": oa.get("oa_url"),
        "abstract": rebuild_abstract(w.get("abstract_inverted_index")),
        "oa_topics": [t.get("display_name", "") for t in (w.get("topics") or [])],
    }


def fetch_keyword(kw, since, until, cfg):
    kw = kw.replace(",", " ").replace('"', "")
    flt = f'title_and_abstract.search:"{kw}",from_publication_date:{since},type:article|review|preprint'
    if until:
        flt += f",to_publication_date:{until}"
    out = []
    for page in range(1, int(cfg.get("max_pages", 2)) + 1):
        data = api_get({"filter": flt, "sort": "publication_date:desc", "per-page": 50,
                        "page": page, "select": SELECT}, cfg["mailto"])
        res = data.get("results", [])
        out += res
        if len(res) < 50:
            break
        time.sleep(0.2)
    return out


# ---------- 점수 (100점 만점) ----------
def has_kw(text, kw):
    """단어 경계 기준 매칭 ('pla'가 'plant'에 걸리지 않도록). 복수형(s/es) 허용."""
    return re.search(r"(?<![a-z0-9])" + re.escape(kw.lower()) + r"(?:s|es)?(?![a-z0-9])", text) is not None


def score_paper(p, topics, scr, jcr):
    """(총점, 항목별 점수, 최적 분야명, JCR 정보) 반환. 여러 분야에 걸리면 가장 높은 분야를 채택."""
    w, pen = scr["weights"], scr["penalties"]
    if p.get("year") and p["year"] < scr.get("min_year", 0):
        return -999, {"hard_rule_min_year": -999}, "", {}
    common, info = {}, {}
    if p.get("year") and p["year"] >= dt.date.today().year - 1:
        common["recent_year"] = w["recent_year"]
    if p.get("type") not in scr.get("allowed_types", ["article", "review", "preprint"]):
        common["wrong_type"] = pen["wrong_type"]
    entry = jcr_lookup(p, jcr)
    if entry:
        info = {"jif": entry["jif"], "quartile": best_quartile(entry["cats"].values())}
        if entry["jif"] is not None:
            cap = float(scr["jcr"]["if_cap"])
            common["jcr_impact_factor"] = round(w["jcr_impact_factor"] * min(entry["jif"], cap) / cap, 1)

    title, abstract = p["title"].lower(), p["abstract"].lower()
    best = (-10 ** 6, {}, "")
    for t in topics:
        b = dict(common)
        kws = t.get("keywords", [])
        in_title = {k for k in kws if has_kw(title, k)}
        in_abs = {k for k in kws if has_kw(abstract, k)}
        if in_title:
            b["keyword_title"] = w["keyword_title"]
        if in_abs:
            b["keyword_abstract"] = w["keyword_abstract"]
        hits = len(in_title | in_abs)
        if hits > 1:
            b["keyword_hits_bonus"] = min((hits - 1) * w["keyword_hits_bonus_each"], w["keyword_hits_bonus_max"])
        if entry:  # 카테고리 일치 시 해당 카테고리의 분위를, 아니면 저널 전체 최고 분위를 사용
            want = cat_weights(t.get("jcr_categories"))
            matched = {c: want[c] for c in entry["cats"] if c in want}
            if matched:  # 여러 카테고리에 걸리면 가장 주제와 가까운 것(최대 가중치)만 반영
                b["jcr_category"] = round(w["jcr_category"] * max(matched.values()), 1)
            q = best_quartile([entry["cats"][c] for c in matched] or entry["cats"].values())
            if w["jcr_quartile"].get(q, 0):
                b["jcr_quartile"] = w["jcr_quartile"][q]
        if p["journal"] and p["journal"].lower() in [j.lower() for j in t.get("journals", [])]:
            b["journal_match"] = w["journal_match"]
        ots = [o.lower() for o in t.get("openalex_topics", [])]
        if any(o in x.lower() for o in ots for x in p.get("oa_topics", [])):
            b["topic_match"] = w["topic_match"]
        if any(has_kw(f"{title} {abstract}", e) for e in t.get("exclude_keywords", [])):
            b["exclude_keyword"] = pen["exclude_keyword"]
        total = sum(b.values())
        if total > best[0]:
            best = (total, b, t["name"])
    total, b, name = best
    return round(max(0, min(100, total)), 1), b, name, info


def cat_weights(v):
    """jcr_categories: {카테고리: 0~1 가중치} 또는 [카테고리, ...](전부 1.0). 소문자 키로 정규화."""
    if isinstance(v, dict):
        return {str(k).casefold(): float(x) for k, x in v.items()}
    return {str(c).casefold(): 1.0 for c in (v or [])}


def all_categories(jcr):
    seen = {}
    for e in {id(v): v for v in jcr.values()}.values():  # 같은 저널이 여러 키로 들어 있어 중복 제거
        for c in e["cats"]:
            seen[c] = seen.get(c, 0) + 1
    return seen


def check_categories(jcr, topics):
    if not jcr:
        return
    have = all_categories(jcr)
    for t in topics:
        for c in cat_weights(t.get("jcr_categories")):
            if c not in have:
                print(f"[경고] '{t['name']}'의 JCR 카테고리 '{c.upper()}'가 CSV에 없습니다 (표기 확인)")


def cmd_categories(args):
    cfg, topics = load_yaml("screening.yml"), load_yaml("topics.yml")["topics"]
    jcr = load_jcr(cfg["jcr"]["files"])
    if not jcr:
        sys.exit("JCR CSV가 없습니다 (config/jcr*.csv).")
    weights = {t["name"]: cat_weights(t.get("jcr_categories")) for t in topics}
    print(f"CSV에 있는 카테고리 {len(all_categories(jcr))}개 (저널 수 / 분야별 가중치)")
    for c, n in sorted(all_categories(jcr).items()):
        ws = ", ".join(f"{name} {w[c]}" for name, w in weights.items() if c in w)
        print(f"  {c.upper():55s} {n:4d}  {ws}")


def rank_batch(decisions, batch, scr):
    """한 번의 수집(batch) 안에서 점수순으로 순위를 매겨 상위 N 자동 선정, 다음 M 검토, 나머지 반려.
    사람이 이미 판정한 항목은 순위 자리만 차지하고 상태는 유지한다."""
    sel = scr["selection"]
    ids = sorted((i for i, p in decisions.items() if p.get("batch") == batch),
                 key=lambda i: (-decisions[i]["score"], decisions[i]["title"]))
    for rank, i in enumerate(ids, 1):
        p = decisions[i]
        p["rank"] = rank
        if p.get("decided_by") == "human":
            continue
        s = p["score"]
        if rank <= sel["auto_select"] and s >= sel["auto_min_score"]:
            p["status"] = "approved"
        elif rank <= sel["auto_select"] + sel["review"] and s >= sel["review_min_score"]:
            p["status"] = "pending"
        else:
            p["status"] = "rejected"


# ---------- 명령: fetch / rescore ----------
def cmd_fetch(args):
    cfg, topics = load_yaml("screening.yml"), load_yaml("topics.yml")["topics"]
    jcr, decisions = load_jcr(cfg["jcr"]["files"]), load_decisions()
    if jcr is None:
        print("[경고] JCR CSV가 없어 IF/분위/카테고리 점수가 0점 처리됩니다 (config/jcr.csv).")
    check_categories(jcr, topics)
    days = args.lookback_days or int(cfg.get("lookback_days", 14))
    today = dt.date.today()
    since = (today - dt.timedelta(days=days)).isoformat()
    until = (today - dt.timedelta(days=args.until_days_ago)).isoformat() if args.until_days_ago else ""
    found = {}
    for t in topics:
        for kw in (t.get("search") or t.get("keywords", [])):
            for w in fetch_keyword(kw, since, until, cfg):
                p = normalize(w)
                if p["id"] and p["id"] not in decisions:
                    found[p["id"]] = p
            print(f"[{t['name']}] '{kw}' 처리 (누적 신규 {len(found)}건)")
    run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    for pid, p in found.items():
        total, breakdown, topic, info = score_paper(p, topics, cfg, jcr)
        decisions[pid] = {**p, "topic": topic, "score": total, "breakdown": breakdown,
                          "jif": info.get("jif"), "quartile": info.get("quartile", ""),
                          "status": "rejected", "rank": 0, "decided_by": "auto",
                          "found": today.isoformat(), "batch": run_id}
    rank_batch(decisions, run_id, cfg)
    save_decisions(decisions)
    LAST_RUN.write_text(json.dumps({"batch": run_id}), encoding="utf-8")
    cnt = {s: sum(1 for p in decisions.values() if p.get("batch") == run_id and p["status"] == s)
           for s in ("approved", "pending", "rejected")}
    print(f"신규 {len(found)}건 -> 자동 선정 {cnt['approved']}, 검토 {cnt['pending']}, 반려 {cnt['rejected']}")


def cmd_rescore(args):
    cfg, topics = load_yaml("screening.yml"), load_yaml("topics.yml")["topics"]
    jcr, decisions = load_jcr(cfg["jcr"]["files"]), load_decisions()
    check_categories(jcr, topics)
    before = {i: p["status"] for i, p in decisions.items()}
    for p in decisions.values():
        total, breakdown, topic, info = score_paper(p, topics, cfg, jcr)
        p.update(score=total, breakdown=breakdown, topic=topic,
                 jif=info.get("jif"), quartile=info.get("quartile", ""))
    for batch in {p.get("batch") for p in decisions.values()}:
        rank_batch(decisions, batch, cfg)
    changed = sum(1 for i, p in decisions.items() if p["status"] != before[i])
    save_decisions(decisions)
    print(f"재채점 완료, 상태 변경 {changed}건 (사람이 판정한 항목은 유지)")


# ---------- GitHub Issue (주간 검토 1건) ----------
def gh(method, path, body=None):
    req = urllib.request.Request(
        f"https://api.github.com/repos/{os.environ['GITHUB_REPOSITORY']}{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
                 "Accept": "application/vnd.github+json", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        txt = r.read()
        return json.loads(txt) if txt else {}


def ensure_labels():
    for name, color in LABELS.items():
        try:
            gh("POST", "/labels", {"name": name, "color": color})
        except urllib.error.HTTPError as e:
            if e.code != 422:  # 이미 존재
                raise


def item_line(p, checked):
    link = f"https://doi.org/{p['doi'].split('doi.org/')[-1]}" if p.get("doi") else f"https://openalex.org/{p['id']}"
    jcr = f" · IF {p['jif']} {p.get('quartile', '')}".rstrip() if p.get("jif") is not None else ""
    abstract = p["abstract"][:260] + ("..." if len(p["abstract"]) > 260 else "")
    line = (f"- [{'x' if checked else ' '}] **{p['title'][:150]}** — {p['journal'] or '-'}{jcr}"
            f" · **{p['score']}점** · [링크]({link}) <!-- paper:{p['id']} -->")
    return line + (f"\n  > {abstract}" if abstract else "")


def build_review_body(items, rejected, date):
    auto = [p for p in items if p["status"] == "approved"]
    review = [p for p in items if p["status"] == "pending"]
    out = [f"## 주간 검토 {date}",
           f"이번 수집의 상위 {len(items)}건입니다. 점수는 100점 만점(키워드 관련도 + JCR IF·분위·카테고리)입니다.", "",
           "**사용 방법**",
           "1. **체크된 항목 = 게시, 체크 해제 = 제외**입니다.",
           f"2. 자동 선정 {len(auto)}건은 미리 체크되어 있고 (이미 사이트에 게시됨), 검토 대상 {len(review)}건은 체크가 비어 있습니다. 게시할 논문만 체크하세요.",
           "3. 끝나면 이 Issue를 **Close**하세요. 체크 상태가 그대로 반영됩니다.",
           "4. 나중에 바꾸려면 Issue를 다시 열어 체크를 수정하고 Close하면 됩니다.", "",
           f"### 자동 선정 (1~{len(auto)}위)"]
    out += [item_line(p, True) for p in auto] or ["(없음)"]
    out += ["", f"### 검토 대상 ({len(auto) + 1}~{len(items)}위)"]
    out += [item_line(p, False) for p in review] or ["(없음)"]
    if rejected:
        out += ["", "<details><summary>자동 반려 중 점수 상위 (참고용)</summary>", ""]
        out += [f"- `{p['id']}` {p['score']}점 · {p['title'][:90]}" for p in rejected]
        out += ["", "되돌리려면 Actions → Review decision을 수동 실행하세요.", "</details>"]
    return "\n".join(out)


def cmd_issues(args):
    decisions = load_decisions()
    batch = (json.loads(LAST_RUN.read_text()) if LAST_RUN.exists() else {}).get("batch")
    pool = sorted((p for p in decisions.values() if p.get("batch") == batch), key=lambda p: p["rank"])
    items = [p for p in pool if p["status"] in ("approved", "pending")]
    if not items:
        print("검토할 항목이 없습니다.")
        return
    body = build_review_body(items, [p for p in pool if p["status"] == "rejected"][:10], dt.date.today())
    if "GITHUB_TOKEN" not in os.environ:
        print(f"[dry-run] 주간 검토 Issue 1건 ({len(items)}개 항목)\n{body[:600]}")
        return
    ensure_labels()
    res = gh("POST", "/issues", {"title": f"주간 검토 {dt.date.today()} ({len(items)}건)",
                                 "body": body, "labels": ["weekly-review"]})
    for p in items:
        p["review_issue"] = res["number"]
    save_decisions(decisions)
    print(f"Issue #{res['number']} 생성")


# ---------- 검토 결과 반영 ----------
def parse_checklist(body):
    return re.findall(r"^- \[([ xX])\] .*?<!-- paper:(\S+) -->\s*$", body or "", flags=re.M)


def cmd_review(args):
    decisions = load_decisions()
    pid, status = os.environ.get("PAPER_ID", ""), os.environ.get("STATUS", "")
    issue = os.environ.get("ISSUE_NUMBER", "")
    today = dt.date.today().isoformat()
    if pid:  # 수동 실행: 논문 1건 직접 지정
        if status not in ("approved", "rejected", "pending") or pid not in decisions:
            sys.exit(f"잘못된 입력: {pid} / {status}")
        decisions[pid].update(status=status, decided_by="human", decided_at=today)
        n_ok, n_no = int(status == "approved"), int(status != "approved")
    else:  # Issue 닫힘: 체크리스트 전체 반영
        pairs = parse_checklist(os.environ.get("ISSUE_BODY", ""))
        if not pairs:
            sys.exit("체크리스트를 찾지 못했습니다.")
        n_ok = n_no = 0
        for mark, i in pairs:
            if i in decisions:
                ok = mark.lower() == "x"
                decisions[i].update(status="approved" if ok else "rejected", decided_by="human", decided_at=today)
                n_ok, n_no = n_ok + ok, n_no + (not ok)
    save_decisions(decisions)
    print(f"반영: 게시 {n_ok}건, 제외 {n_no}건")
    if issue and "GITHUB_TOKEN" in os.environ:
        gh("POST", f"/issues/{issue}/comments", {"body": f"반영 완료: 게시 {n_ok}건, 제외 {n_no}건. 사이트가 곧 갱신됩니다."})


# ---------- 사이트용 내보내기 ----------
def cmd_export(args):
    cfg, decisions, topics = load_yaml("screening.yml"), load_decisions(), load_yaml("topics.yml")["topics"]
    keep = ["id", "title", "authors", "journal", "year", "date", "doi", "oa", "oa_url", "abstract", "topic"]
    if cfg.get("site", {}).get("show_jcr"):  # JCR 지표 공개 여부 (라이선스 확인 후 켜기)
        keep += ["jif", "quartile"]
    papers = sorted((p for p in decisions.values() if p["status"] == "approved"),
                    key=lambda p: p.get("date") or "", reverse=True)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "papers.json").write_text(json.dumps({
        "updated": dt.date.today().isoformat(), "topics": [t["name"] for t in topics],
        "papers": [{k: p.get(k) for k in keep} for p in papers]}, ensure_ascii=False), encoding="utf-8")
    print(f"승인된 논문 {len(papers)}건 내보냄 -> {out / 'papers.json'}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--lookback-days", type=int)
    f.add_argument("--until-days-ago", type=int, default=0)
    for name in ("rescore", "issues", "review", "categories"):
        sub.add_parser(name)
    e = sub.add_parser("export")
    e.add_argument("--out", default="_site")
    args = ap.parse_args()
    {"fetch": cmd_fetch, "rescore": cmd_rescore, "issues": cmd_issues,
     "review": cmd_review, "export": cmd_export, "categories": cmd_categories}[args.cmd](args)


if __name__ == "__main__":
    main()
