# 연구실 논문 사이트

연구 주제(키워드·저널·분류)와 관련된 논문을 OpenAlex에서 분야별로 수집 → 점수로 자동 검열 → 애매한 것만 GitHub Issue로 사람이 판단 → 승인된 논문만 GitHub Pages에 게시합니다.

## 처음 설정
1. 이 폴더를 새 GitHub 저장소(main 브랜치)로 푸시
2. **Settings → Pages → Source: GitHub Actions**
3. **Settings → Actions → General → Workflow permissions: Read and write permissions**
4. `config/screening.yml`의 `mailto`를 본인 이메일로 변경
5. `config/topics.yml`을 연구 주제에 맞게 수정 (search는 좁은 구문, keywords는 점수용 넓은 단어)
6. **Actions → Fetch papers → Run workflow** (첫 실행은 `lookback_days`에 365 등 큰 값 입력)

## 동작
| 점수 | 처리 |
|---|---|
| `auto_approve` 이상 | 자동 승인 (닫힌 Issue로 기록) |
| 중간 | Issue(`pending`) 생성 → 라벨 `approve` / `reject`로 판정 |
| `auto_reject` 미만 | 자동 반려 (주간 요약 Issue에 상위 15건 표시) |

- 사람이 판정한 항목(`decided_by: human`)은 재채점해도 덮어쓰지 않습니다.
- 기준을 바꾼 뒤: **Fetch papers → mode: rescore**
- 자동 승인 항목 되돌리기: 해당 Issue에 `reject` 라벨, 또는 **Review decision**을 수동 실행(논문 ID + 상태)

## 로컬 테스트
```
pip install pyyaml
python tests/test_screen.py
```
