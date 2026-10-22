"""네트워크 없이 점수 계산/분류를 확인하는 간단한 테스트: python tests/test_screen.py"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import labsite as L

cfg, topics = L.load_yaml("screening.yml"), L.load_yaml("topics.yml")["topics"]

def paper(**kw):
    base = {"id": "W1", "title": "", "abstract": "", "journal": "", "year": 2026, "type": "article", "oa_topics": []}
    return {**base, **kw}

def run(p):
    total, b, topic = L.score_paper(p, topics, cfg)
    return total, L.classify(total, cfg["thresholds"]), topic

# 주제·저널·분류가 모두 맞으면 자동 승인
total, status, topic = run(paper(title="Biodegradable polymer blends of polylactic acid",
                                 abstract="Compostable films and their biodegradation.",
                                 journal="Polymer Degradation and Stability", oa_topics=["Biodegradable Polymers"]))
assert status == "approved" and topic == "생분해성 고분자", (total, status, topic)

# 바이오차 논문은 바이오차 분야로 분류
total, status, topic = run(paper(title="Biochar from pyrolysis of biomass", journal="Biochar", oa_topics=["Biochar"]))
assert topic == "바이오차" and status == "approved", (total, status, topic)

# 키워드가 제목에만 걸리면 사람 검토
assert run(paper(title="Polymer film for packaging", abstract="unrelated"))[1] == "pending"

# 오래된 논문은 하드 규칙으로 반려, 제외 키워드는 감점
assert run(paper(title="biodegradable polymer", year=2010))[1] == "rejected"
assert run(paper(title="biodegradable polymer", abstract="drug delivery tissue engineering"))[0] < 40

# 단어 경계: 'PLA'가 'plant'에 걸리면 안 됨, 복수형은 허용
assert not L.has_kw("plant growth under plasma", "PLA")
assert L.has_kw("biodegradable polymers are", "polymer")
assert L.rebuild_abstract({"Hello": [0], "world": [1]}) == "Hello world"
print("OK")
