#!/usr/bin/env python3
"""연구실 논문 수집 파이프라인: fetch -> (자동 검열) -> issues -> review -> export"""
import argparse
import datetime as dt
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
LABELS = {"pending": "fbca04", "auto-approved": "0e8a16", "approve": "0e8a16",
          "reject": "b60205", "weekly-summary": "5319e7"}


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
    DECISIONS.write_text(json.dumps(d, ensure_ascii=False, indent=1, sort_keys=True),
                         encoding="utf-8")


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
    return {
        "id": short_id(w.get("id")),
        "title": w.get("title") or w.get("display_name") or "",
        "authors": [a["author"].get("display_name", "") for a in auth[:12]],
        "journal": src.get("display_name") or "",
        "year": w.get("publication_year"),
        "date": w.get("publication_date"),
        "doi": w.get("doi"),
        "type": w.get("type"),
        "oa": bool(oa.get("is_oa")),
        "oa_url": oa.get("oa_url"),
        "abstract": rebuild_abstract(w.get("abstract_inverted_index")),
        "oa_topics": [t.get("display_name", "") for t in (w.get("topics") or [])],
    }


def fetch_keyword(kw, since, cfg):
    kw = kw.replace(",", " ").replace('"', "")
    out = []
    for page in range(1, int(cfg.get("max_pages", 2)) + 1):
        data = api_get({
            "filter": f'title_and_abstract.search:"{kw}",from_publication_date:{since},type:article|review|preprint',
            "sort": "publication_date:desc", "per-page": 50, "page": page, "select": SELECT,
        }, cfg["mailto"])
        res = data.get("results", [])
        out += res
        if len(res) < 50:
            break
        time.sleep(0.2)
    return out


# ---------- 점수 / 분류 ----------
def has_kw(text, kw):
    """단어 경계 기준 매칭 (예: 'pla'가 'plant'에 걸리지 않도록). 복수형(s/es)은 허용."""
    return re.search(r"(?<![a-z0-9])" + re.escape(kw.lower()) + r"(?:s|es)?(?![a-z0-9])", text) is not None


def score_paper(p, topics, scr):
    """(총점, 항목별 점수, 최적 분야명) 반환. 여러 분야에 걸리면 가장 높은 분야를 채택."""
    w, pen = scr["weights"], scr["penalties"]
    if p.get("year") and p["year"] < scr.get("min_year", 0):
        return -999, {"hard_rule_min_year": -999}, ""
    common = {}
    if p.get("year") and p["year"] >= dt.date.today().year - 1:
        common["recent_year"] = w["recent_year"]
    if p.get("type") not in scr.get("allowed_types", ["article", "review", "preprint"]):
        common["wrong_type"] = pen["wrong_type"]

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
        hits = len(in_title | in_abs)  # 서로 다른 키워드가 많이 걸릴수록 관련도 높음
        if hits > 1:
            b["keyword_hits_bonus"] = min((hits - 1) * w["keyword_hits_bonus_each"], w["keyword_hits_bonus_max"])
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
    return best


def classify(total, thr):
    if total >= thr["auto_approve"]:
        return "approved"
    if total < thr["auto_reject"]:
        return "rejected"
    return "pending"


# ---------- 명령: fetch / rescore ----------
def cmd_fetch(args):
    cfg, topics = load_yaml("screening.yml"), load_yaml("topics.yml")["topics"]
    decisions = load_decisions()
    days = args.lookback_days or int(cfg.get("lookback_days", 14))
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    found = {}
    for t in topics:
        for kw in (t.get("search") or t.get("keywords", [])):
            for w in fetch_keyword(kw, since, cfg):
                p = normalize(w)
                if p["id"] and p["id"] not in decisions:
                    found[p["id"]] = p
            print(f"[{t['name']}] '{kw}' 처리 (누적 신규 {len(found)}건)")
    new_ids = []
    for pid, p in found.items():
        total, breakdown, topic = score_paper(p, topics, cfg)
        decisions[pid] = {**p, "topic": topic, "score": total, "breakdown": breakdown,
                          "status": classify(total, cfg["thresholds"]), "decided_by": "auto",
                          "found": dt.date.today().isoformat()}
        new_ids.append(pid)
    save_decisions(decisions)
    LAST_RUN.write_text(json.dumps({"new": new_ids}), encoding="utf-8")
    counts = {s: sum(decisions[i]["status"] == s for i in new_ids) for s in ("approved", "pending", "rejected")}
    print(f"신규 {len(new_ids)}건: {counts}")


def cmd_rescore(args):
    cfg, topics = load_yaml("screening.yml"), load_yaml("topics.yml")["topics"]
    decisions = load_decisions()
    changed = 0
    for pid, p in decisions.items():
        if p.get("decided_by") == "human":
            continue  # 사람이 정한 항목은 덮어쓰지 않음
        total, breakdown, topic = score_paper(p, topics, cfg)
        status = classify(total, cfg["thresholds"])
        if status != p["status"]:
            changed += 1
            print(f"{pid}: {p['status']} -> {status} ({total})")
        p.update(score=total, breakdown=breakdown, topic=topic, status=status)
    save_decisions(decisions)
    print(f"재채점 완료, 상태 변경 {changed}건")


# ---------- GitHub Issues ----------
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
            if e.code != 422:  # 422 = 이미 존재
                raise


def issue_body(p):
    link = f"https://doi.org/{p['doi'].split('doi.org/')[-1]}" if p.get("doi") else f"https://openalex.org/{p['id']}"
    bd = ", ".join(f"{k} {v:+d}" for k, v in p["breakdown"].items()) or "해당 없음"
    abstract = (p["abstract"][:900] + "...") if len(p["abstract"]) > 900 else p["abstract"]
    return (f"<!-- paper:{p['id']} -->\n"
            f"**{p['title']}**\n\n"
            f"- 저자: {', '.join(p['authors'][:6])}{' 외' if len(p['authors']) > 6 else ''}\n"
            f"- 저널/연도: {p['journal'] or '-'} / {p['year']}\n"
            f"- 분야: {p['topic'] or '-'} | OA: {'예' if p['oa'] else '아니오'}\n"
            f"- 링크: {link}\n"
            f"- 점수: **{p['score']}** ({bd})\n\n"
            f"> {abstract or '(초록 없음)'}\n\n"
            f"---\n라벨 `approve` 또는 `reject`를 달면 반영됩니다. "
            f"자동 승인 항목도 `reject` 라벨로 되돌릴 수 있습니다.")


def cmd_issues(args):
    cfg, decisions = load_yaml("screening.yml"), load_decisions()
    new = set(json.loads(LAST_RUN.read_text()).get("new", [])) if LAST_RUN.exists() else set()
    todo = [i for i, p in decisions.items() if not p.get("issue") and
            (p["status"] == "pending" or (p["status"] == "approved" and i in new and p["decided_by"] == "auto"))]
    todo = sorted(todo, key=lambda i: -decisions[i]["score"])[: int(cfg.get("max_issues_per_run", 40))]
    if "GITHUB_TOKEN" not in os.environ:
        print(f"[dry-run] Issue 생성 대상 {len(todo)}건")
        return
    ensure_labels()
    for i in todo:
        p = decisions[i]
        auto = p["status"] == "approved"
        res = gh("POST", "/issues", {"title": f"[{p['topic'] or '미분류'}] {p['title'][:100]}",
                                     "body": issue_body(p), "labels": ["auto-approved" if auto else "pending"]})
        p["issue"] = res["number"]
        if auto:
            gh("PATCH", f"/issues/{res['number']}", {"state": "closed", "state_reason": "completed"})
        time.sleep(1)
    near = sorted((p for i, p in decisions.items() if i in new and p["status"] == "rejected"),
                  key=lambda p: -p["score"])[:15]
    if new:
        lines = [f"- 신규 {len(new)}건: 자동 승인 {sum(decisions[i]['status']=='approved' for i in new)}, "
                 f"검토 대기 {sum(decisions[i]['status']=='pending' for i in new)}, "
                 f"자동 반려 {sum(decisions[i]['status']=='rejected' for i in new)}", "", "### 자동 반려 중 점수 상위 (되돌리려면 review 워크플로 수동 실행)"]
        lines += [f"- `{p['id']}` ({p['score']}점) {p['title'][:90]}" for p in near]
        gh("POST", "/issues", {"title": f"주간 요약 {dt.date.today()}", "body": "\n".join(lines),
                               "labels": ["weekly-summary"]})
    save_decisions(decisions)
    print(f"Issue {len(todo)}건 생성")


# ---------- 검열 결과 반영 ----------
def cmd_review(args):
    decisions = load_decisions()
    pid, status = os.environ.get("PAPER_ID", ""), os.environ.get("STATUS", "")
    issue = os.environ.get("ISSUE_NUMBER", "")
    if not pid:  # 라벨 이벤트로 실행된 경우: Issue 본문에서 논문 ID 추출
        m = re.search(r"<!-- paper:(\S+) -->", os.environ.get("ISSUE_BODY", ""))
        if not m:
            sys.exit("논문 ID를 찾지 못했습니다.")
        pid = m.group(1)
        status = {"approve": "approved", "reject": "rejected"}[os.environ["LABEL"]]
    if status not in ("approved", "rejected", "pending") or pid not in decisions:
        sys.exit(f"잘못된 입력: {pid} / {status}")
    decisions[pid].update(status=status, decided_by="human", decided_at=dt.date.today().isoformat())
    save_decisions(decisions)
    print(f"{pid} -> {status} (사람 판정)")
    if issue and "GITHUB_TOKEN" in os.environ:
        gh("POST", f"/issues/{issue}/comments", {"body": f"`{status}`로 반영했습니다."})
        gh("PATCH", f"/issues/{issue}", {"state": "closed", "state_reason": "completed"})


# ---------- 사이트용 내보내기 ----------
def cmd_export(args):
    decisions, topics = load_decisions(), load_yaml("topics.yml")["topics"]
    keep = ("id", "title", "authors", "journal", "year", "date", "doi", "oa", "oa_url", "abstract", "topic")
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
    sub.add_parser("rescore")
    sub.add_parser("issues")
    sub.add_parser("review")
    e = sub.add_parser("export")
    e.add_argument("--out", default="_site")
    args = ap.parse_args()
    {"fetch": cmd_fetch, "rescore": cmd_rescore, "issues": cmd_issues,
     "review": cmd_review, "export": cmd_export}[args.cmd](args)


if __name__ == "__main__":
    main()
