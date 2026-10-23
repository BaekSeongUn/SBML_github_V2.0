# 연구실 논문 사이트

연구 주제(키워드·JCR 카테고리·저널)와 관련된 논문을 OpenAlex에서 수집하고, **100점 만점 점수**로 순위를 매겨 **상위 5건은 자동 게시, 그 아래 15건만 사람이 판별**합니다. 승인된 논문만 GitHub Pages에 게시됩니다.

## 점수 (100점)
| 항목 | 점수 |
|---|---|
| 제목 키워드 / 초록 키워드 / 서로 다른 키워드 추가 | 20 / 10 / 최대 10 |
| JCR 임팩트팩터 (IF ÷ `if_cap`) | 최대 20 |
| JCR 분위 (Q1 10, Q2 7, Q3 3) | 최대 10 |
| JCR 카테고리 일치 — 주제와 가까울수록 높은 가중치 (`topics.yml`의 `jcr_categories`, 0~1 × 10점) | 최대 10 |
| OpenAlex 분류 일치 | 10 |
| 주제 저널 목록 일치 / 최근 2년 | 5 / 5 |

제외 키워드는 -40점, 논문 유형 불일치는 -30점이며, 설정은 `config/screening.yml`에서 바꿉니다.

## JCR 데이터 준비 (필수)
Clarivate JCR은 무료 API가 없어서 **JCR 사이트에서 직접 내려받은 CSV**를 `config/jcr.csv`로 올립니다 (여러 파일이면 `config/jcr_*.csv`).
- JCR에서 대상 카테고리(Polymer Science 등)의 저널 목록을 CSV로 내보냅니다.
- 필요한 열: `Journal name`, `ISSN`, `eISSN`, `Category`, `...JIF`, `JIF Quartile` (열 순서는 상관없음)
- CSV가 없으면 JCR 점수(최대 40점)가 0점이 되고 경고가 출력됩니다.
- ⚠️ JCR 데이터는 Clarivate 저작물입니다. **공개 저장소에 CSV를 올리는 것이 구독 라이선스에 허용되는지** 학교 도서관에 확인하세요. 사이트의 IF 표시는 기본으로 꺼져 있습니다(`site.show_jcr`).

## 카테고리 가중치 정하기
`topics.yml`의 분야마다 `jcr_categories`에 `{카테고리: 가중치}`를 적습니다. 주제와 가장 가까운 카테고리는 1.0, 멀수록 작게 줍니다. 저널이 여러 카테고리에 속하면 가장 큰 가중치 하나만 반영됩니다.
JCR CSV를 `config/`에 넣은 뒤 `python scripts/labsite.py categories`를 실행하면 CSV에 실제 있는 카테고리 이름과 저널 수, 지금 설정된 가중치가 표로 나옵니다. 오타가 있으면 수집할 때 경고도 출력됩니다.

## 처음 설정
1. 새 GitHub 저장소에 푸시, **Settings → Pages → Source: GitHub Actions**
2. **Settings → Actions → General → Workflow permissions: Read and write**
3. `config/screening.yml`의 `mailto` 변경, `config/topics.yml` 수정, `config/jcr.csv` 추가
4. **Actions → Fetch papers → Run workflow**

## 검토 방법 (한 곳에서 체크만)
수집할 때마다 **"주간 검토" Issue가 1개** 생깁니다.
1. 1~5위는 이미 체크되어 있고(게시됨), 6~20위는 비어 있습니다.
2. **게시할 논문은 체크, 제외할 논문은 체크 해제**합니다.
3. Issue를 **Close**하면 반영됩니다. 고치려면 다시 열어서 체크를 바꾸고 Close합니다.

## 기타
- 기준을 바꾼 뒤: **Fetch papers → mode: rescore** (사람이 판정한 항목은 유지, 순위 자리는 그대로)
- 과거 논문 나눠 가져오기: `lookback_days: 60`, `until_days_ago: 30`처럼 구간을 지정해 여러 번 실행 (한 번 수집 = 상위 20건)
- 논문 1건 번복: **Actions → Review decision** 수동 실행 (논문 ID + 상태)

## 로컬 테스트
```
pip install pyyaml
python tests/test_screen.py
```
