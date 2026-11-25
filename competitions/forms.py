from django import forms

from accounts.forms import NoColonMixin

from .models import DEFAULT_DIRECTION, Deadline, Project, ProjectFile


class CompetitionForm(NoColonMixin, forms.Form):
    name = forms.CharField(label="대회명", max_length=100, help_text="예: 2026-2 머신러닝 과제")


class ProjectForm(NoColonMixin, forms.ModelForm):
    # 빈 값 = 지표에 맞게 자동. 자바스크립트 없이 '자동 채움'을 구현한다(나중에 CSP를 엄격하게 걸기 위해).
    direction = forms.ChoiceField(
        label="점수 방향", required=False,
        choices=[("", "지표에 맞게 자동")] + list(Project.Direction.choices),
    )
    confirm_direction = forms.BooleanField(
        label="지표의 일반적인 방향과 반대로 설정하는 것이 맞습니다", required=False,
    )

    # 정답 파일을 올린 뒤에는 바꾸면 안 되는 칸 (행 수·레이블이 이 값들로 계산되었으므로)
    LOCK_AFTER_ANSWER = ("metric", "id_column", "target_column")
    # 채점 대기·채점된 제출이 생긴 뒤에는 바꾸면 안 되는 칸 (바꾸면 리더보드 순위·검토 순서가 뒤집힌다)
    LOCK_AFTER_SCORING = ("direction",)

    # 확인란을 점수 방향 바로 아래에 둔다
    field_order = ["name", "description", "metric", "direction", "confirm_direction"]

    class Meta:
        model = Project
        fields = ["name", "description", "metric", "direction", "id_column", "target_column"]
        help_texts = {
            "id_column": "제출 CSV에서 행을 구분하는 컬럼 이름",
            "target_column": "제출 CSV에서 예측값이 들어갈 컬럼 이름",
        }

    def __init__(self, *args, locked=False, scored=False, **kwargs):
        super().__init__(*args, **kwargs)
        locks = {}
        if locked:
            locks.update(dict.fromkeys(self.LOCK_AFTER_ANSWER, "정답 파일을 등록한 뒤에는 바꿀 수 없습니다"))
        if scored:
            locks.update(dict.fromkeys(self.LOCK_AFTER_SCORING, "채점된 제출이 있어 바꿀 수 없습니다(바꾸면 순위가 뒤집힘)"))
        for name, reason in locks.items():
            # disabled: 화면에서 못 바꾸고, 요청에 값을 넣어 보내도 Django가 무시한다
            self.fields[name].disabled = True
            self.fields[name].help_text = reason
        # 확인란은 '지표와 반대 방향으로 바꾸려 할 때'만 보인다. 평소에는 아예 그리지 않는다(자바스크립트 없이).
        if not self._is_unusual_change(self.data.get("metric"), self.data.get("direction")):
            del self.fields["confirm_direction"]

    def _is_unusual_change(self, metric, direction):
        """지표의 보통 방향과 반대이면서, 저장된 (지표, 방향) 조합과 다른가 (= 새로 반대 조합을 만드는 중인가).

        예) 저장: RMSE+낮을수록 → 입력: RMSE+높을수록      → True (확인 필요)
            저장: RMSE+높을수록(확인됨) → 입력: 그대로     → False (이름만 고칠 때 다시 묻지 않음)
            저장: RMSE+낮을수록 → 입력: Accuracy+낮을수록  → True (지표만 바꿔도 반대 조합이 됨)
        """
        if self.fields["metric"].disabled or not metric:
            metric = self.instance.metric
        if self.fields["direction"].disabled:
            return False
        if metric not in DEFAULT_DIRECTION or not direction:
            return False
        saved = (self.instance.metric, self.instance.direction)
        return direction != DEFAULT_DIRECTION[metric] and (metric, direction) != saved

    def clean(self):
        cleaned = super().clean()
        metric = cleaned.get("metric")
        if not metric:
            return cleaned
        default = DEFAULT_DIRECTION[metric]
        direction = cleaned.get("direction") or default
        cleaned["direction"] = direction
        # 확인은 '반대로 바꿀 때' 한 번만 묻는다. 이미 확인해 저장된 반대 방향은 다시 묻지 않는다.
        if self._is_unusual_change(metric, direction) and not cleaned.get("confirm_direction"):
            self.add_error(
                "direction",
                f"{Project.Metric(metric).label} 지표는 보통 '{Project.Direction(default).label}'이니, "
                "반대로 설정하려면 아래 확인란을 체크하세요",
            )
        return cleaned


class DeadlineForm(NoColonMixin, forms.ModelForm):
    due_at = forms.DateTimeField(
        label="마감 시각",
        input_formats=["%Y-%m-%dT%H:%M"],
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
        help_text="한국 시각, 이 시각 '전에' 시작한 업로드까지 이 마감에 포함됩니다",
    )

    class Meta:
        model = Deadline
        fields = ["due_at", "max_submissions"]

    def __init__(self, *args, project=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.project = project

    def clean_due_at(self):
        due_at = self.cleaned_data["due_at"]
        if Deadline.objects.filter(project=self.project, due_at=due_at).exists():
            raise forms.ValidationError("같은 시각의 마감일이 이미 있습니다.")
        return due_at


class ProjectFileForm(NoColonMixin, forms.Form):
    kind = forms.ChoiceField(label="종류", choices=ProjectFile.Kind.choices)
    file = forms.FileField(label="CSV 파일")
