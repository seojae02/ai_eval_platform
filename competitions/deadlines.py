def deadline_for(project, started_at):
    """업로드 시작 시각이 속하는 마감일을 돌려준다. 없으면 None(최종 마감 후).

    규칙: "마감 전 시작" = 시작 시각 < 마감 시각 (R1).
    즉, 시작 시각보다 늦은 마감일 중 가장 이른 것.

    예) 마감 11/20 17:00(1차), 11/22 17:00(2차)
        11/20 16:59:59 시작 → 1차
        11/20 17:00:00 시작 → 2차 (정각은 1차가 아님)
        11/22 17:00:01 시작 → None → 접수만, 채점 안 함 (R2)
    """
    return project.deadlines.filter(due_at__gt=started_at).order_by("due_at").first()
