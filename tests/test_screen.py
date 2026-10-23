"""네트워크 없이 점수/순위/JCR/체크리스트 파싱을 확인: python tests/test_screen.py"""
import sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import labsite as L

cfg, topics = L.load_yaml("screening.yml"), L.load_yaml("topics.yml")["topics"]
assert sum(v for k, v in cfg["weights"].items() if k not in ("keyword_hits_bonus_each", "jcr_quartile")
           and k != "keyword_hits_bonus_max") + cfg["weights"]["keyword_hits_bonus_max"] + max(cfg["weights"]["jcr_quartile"].values()) == 100

# --- JCR CSV 로딩 (안내 줄이 앞에 있어도, 열 순서가 달라도 동작) ---
CSV = '''Journal Citation Reports 내보내기
Journal name,JCR Abbreviation,Publisher,ISSN,eISSN,Category,Total Citations,2024 JIF,JIF Quartile,2024 JCI
POLYMER DEGRADATION AND STABILITY,POLYM DEGRAD STABIL,ELSEVIER,0141-3910,1873-2321,POLYMER SCIENCE,50000,6.3,Q1,1.0
BIOCHAR,BIOCHAR,SPRINGER,2524-7867,2524-7867,"ENGINEERING, ENVIRONMENTAL",9000,12.4,Q1,2.0
BIOCHAR,BIOCHAR,SPRINGER,2524-7867,2524-7867,"AGRICULTURE, MULTIDISCIPLINARY",9000,12.4,Q1,2.0
SOME LOW JOURNAL,SLJ,X,1111-2222,N/A,POLYMER SCIENCE,10,0.8,Q4,0.1
'''
tmp = Path(tempfile.mkdtemp()) / "jcr_test.csv"
tmp.write_text(CSV, encoding="utf-8")
L.ROOT = tmp.parent
jcr = L.load_jcr("jcr_test.csv")
assert jcr[("i", "0141-3910")]["jif"] == 6.3 and jcr[("i", "2524-7867")]["cats"]["agriculture, multidisciplinary"] == "Q1"

def paper(**kw):
    base = {"id": "W1", "title": "", "abstract": "", "journal": "", "issn": [], "year": 2026, "type": "article", "oa_topics": []}
    return {**base, **kw}

# 주제·JCR이 모두 맞으면 높은 점수 (계산값: 20+10+10+12.6+10+10+10+5+5 = 92.6)
good = paper(title="Biodegradable polymer blends of polylactic acid", abstract="Compostable films and their biodegradation.",
             journal="Polymer Degradation and Stability", issn=["0141-3910"], oa_topics=["Biodegradable Polymers"])
total, b, topic, info = L.score_paper(good, topics, cfg, jcr)
assert total == 92.6 and topic == "생분해성 고분자" and info == {"jif": 6.3, "quartile": "Q1"}, (total, b, topic, info)

# 같은 내용이라도 저널 점수가 낮으면 점수가 내려감, JCR에 없으면 JCR 점수 0
low = paper(title=good["title"], abstract=good["abstract"], issn=["1111-2222"], oa_topics=["Biodegradable Polymers"])
assert L.score_paper(low, topics, cfg, jcr)[0] < total
nojcr = L.score_paper(good, topics, cfg, None)[0]
assert nojcr == round(total - 12.6 - 10 - 10 - 5, 1) + 5 or nojcr < total

# --- 카테고리 가중치: 주제에 가까운 카테고리일수록 높은 점수, 여러 개 걸리면 최대값만 반영 ---
assert L.cat_weights(["A", "b"]) == {"a": 1.0, "b": 1.0} and L.cat_weights({"A": 0.5}) == {"a": 0.5}
bio = paper(title="Biochar from pyrolysis of biomass", journal="Biochar", issn=["2524-7867"])
total, b, topic, info = L.score_paper(bio, topics, cfg, jcr)
# 바이오차 분야: ENGINEERING, ENVIRONMENTAL(0.9)과 AGRICULTURE, MULTIDISCIPLINARY(0.6) 중 큰 값 0.9 -> 10 * 0.9
assert topic == "바이오차" and b["jcr_category"] == 9.0 and total == 79.0, (topic, b, total)
# 같은 저널이라도 고분자 분야에서는 ENGINEERING, ENVIRONMENTAL 가중치가 0.4라 4점만 받음
poly_only = [t for t in topics if t["name"] == "생분해성 고분자"]
assert L.score_paper(bio, poly_only, cfg, jcr)[1]["jcr_category"] == 4.0
assert "POLYMER SCIENCE" not in L.all_categories(jcr) and "polymer science" in L.all_categories(jcr)

# 하드 규칙 / 제외 키워드 / 단어 경계
assert L.score_paper(paper(title="biodegradable polymer", year=2010), topics, cfg, jcr)[0] == -999
assert not L.has_kw("plant growth under plasma", "PLA") and L.has_kw("biodegradable polymers", "polymer")

# --- 순위 선정: 30건 중 상위 5 자동, 다음 15 검토, 나머지 반려 / 사람 판정은 유지 ---
dec = {f"W{i}": {"title": f"t{i}", "score": 95 - i, "batch": "b1", "decided_by": "auto", "status": "rejected"} for i in range(30)}
dec["W2"].update(decided_by="human", status="rejected")
L.rank_batch(dec, "b1", cfg)
st = [dec[f"W{i}"]["status"] for i in range(30)]
assert st[:2] == ["approved"] * 2 and st[2] == "rejected" and st[3:5] == ["approved"] * 2   # 3번째는 사람이 반려한 상태 유지
assert st[5:20] == ["pending"] * 15 and set(st[20:]) == {"rejected"}, st

# --- 점수 미달이면 선정 수가 줄어듦 ---
few = {f"W{i}": {"title": f"t{i}", "score": s, "batch": "b2", "decided_by": "auto", "status": ""} for i, s in enumerate([70, 50, 30])}
L.rank_batch(few, "b2", cfg)
assert [few[f"W{i}"]["status"] for i in range(3)] == ["approved", "pending", "rejected"]

# --- 검토 Issue 본문 <-> 체크리스트 파싱 ---
items = [dict(dec[f"W{i}"], id=f"W{i}", jif=6.3, quartile="Q1", journal="J", abstract="abc", doi=None, rank=i + 1) for i in (0, 1, 5, 6)]
body = L.build_review_body(items, [], "2026-10-22")
pairs = L.parse_checklist(body)
assert pairs == [("x", "W0"), ("x", "W1"), (" ", "W5"), (" ", "W6")], pairs
print("OK")
