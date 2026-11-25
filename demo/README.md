# 예시 대회 자료

시연용 가상 대회 하나(손글씨 숫자 분류)의 자료입니다. 실제 사람의 정보는 없습니다.

| 경로 | 내용 |
|---|---|
| `data/digits/` | 관리자가 올리는 파일: `train.csv`(공개 학습), `test_features.csv`(공개 테스트 feature), `answer.csv`(비공개 정답) |
| `submissions/<사례>/` | 가상 참여자가 내는 파일: `pred.csv`, `work.ipynb`(실행된 노트북), `model.pkl`, 사례 설명 `README.txt` |
| `submissions/manifest.json` | 사례별 설명 |
| `build_demo.py` | 위 파일을 모두 다시 만드는 스크립트(샌드박스 이미지 안에서 실행) |

## 데이터

- scikit-learn 1.9.1 내장 `load_digits`(8×8 픽셀, 1,797행, 값 0~16). 인터넷 없이 만들어집니다.
- 값은 원본 그대로, 행 순서를 섞고(시드 2026) id를 새로 붙였습니다. train 1,437행 / test 360행, 컬럼은 `id`, `label`, `pixel0~63`.
- 수업 미니 프로젝트 #1(손글씨 숫자 분류, 10-fold CV 정확도) 형식을 이 플랫폼 방식(정답은 서버에만, 참여자는 예측 CSV 제출)으로 옮겼습니다.

## 가상 참여자 11명

정상 3명, RFP 8쪽 유형의 부정 7명, 모르고 한 방법 실수 1명입니다. 실제 검증 결과는 저장소 맨 위 [README](../README.md)의 "예시 대회 결과"에 있습니다.

| 계정 | 사례 폴더 | 한 일 |
|---|---|---|
| demo_stu01 | `honest_sgd` | 정상: 과제 PDF 코드 그대로(SGDClassifier + `cross_val_score(cv=10)`, 배열과 `np.mean`만 출력) |
| demo_stu02 | `honest_forest_features` | 정상: 행·열 합 16개 특성을 파이프라인 안에서 추가 + 랜덤 포레스트 |
| demo_stu03 | `honest_svc_pipeline` | 정상: 스케일러+SVC 파이프라인, `cross_val_predict` 혼동 행렬 |
| demo_stu04 | `cheat_copied_csv` | 부정: 노트북은 결정 트리, 제출 CSV는 demo_stu02의 것을 복사 |
| demo_stu05 | `cheat_excel_edit` | 부정: 저장된 CSV를 엑셀로 열어 '자신 없는' 20행을 눈으로 보고 고침(4행 바뀜) |
| demo_stu06 | `cheat_hand_labels` | 부정: 노트북 안에 `fixes = {id: 숫자}`로 라벨 20개를 직접 적어 예측을 덮어씀 |
| demo_stu07 | `cheat_pasted_column` | 부정: 다른 곳에서 만든 예측 열을 문자열로 붙여 넣어 CSV 생성(모델은 겉치레) |
| demo_stu08 | `cheat_original_lookup` | 부정: 노트북 안에서 `load_digits()`로 원본을 불러 test 정답 조회 |
| demo_stu09 | `cheat_edited_output` | 부정(노트북 CV 위조): 실행 뒤 `Scores`/`Mean` 출력 칸만 3%p 높게 고침 |
| demo_stu10 | `cheat_typed_mean` | 부정(노트북 CV 위조): CV 평균을 계산하지 않고 `print('Mean: 0.9900')` |
| demo_stu11 | `mistake_pca` | 방법 실수: 교차검증 전에 PCA를 전체 학습 데이터에 fit |

비밀번호는 아이디와 같습니다. **로컬 시연 전용**이므로 서버에 올린 곳에서는 `load_demo`를 실행하지 마세요.

## 다시 만들기·불러오기

```bash
docker compose build sandbox-image
docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp -v "$PWD/demo:/demo" -w /demo ai-eval-sandbox python build_demo.py
./dev.sh demo      # = docker compose exec web python manage.py load_demo (여러 번 실행해도 됨)
```

- 같은 시드라 몇 번을 돌려도 같은 파일이 나옵니다(노트북은 실행 시각을 남기지 않고 셀 id를 고정).
- `load_demo`는 CSV·노트북·모델 세 파일의 SHA-256이 같은 제출이 이미 있으면 건너뛰므로, 자료를 다시 만들면 바뀐 사례만 다시 제출됩니다.
- 빌드 끝에 사례끼리 CSV 일치율·같은 오답 비율을 출력합니다. 복사 사례 짝(demo_stu02 ↔ 04)만 같아야 합니다(우연히 같은 CSV가 생기면 V2가 오탐).
