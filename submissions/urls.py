from django.urls import path

from . import review_views, views

app_name = "submissions"

P = "<int:competition_id>/projects/<int:project_id>/submissions"
S = f"{P}/<int:submission_id>"
B = "<int:competition_id>/projects/<int:project_id>"
R = f"{B}/review/<int:submission_id>"

urlpatterns = [
    path("<int:competition_id>/projects/<int:project_id>/leaderboard/", views.leaderboard, name="leaderboard"),
    path(f"{B}/status.json", views.status, name="status"),
    path(f"{P}/start/", views.start, name="start"),
    path(f"{S}/", views.upload, name="upload"),
    path(f"{S}/chunk/", views.chunk, name="chunk"),
    path(f"{S}/complete/", views.complete, name="complete"),
    path(f"{S}/cancel/", views.cancel, name="cancel"),
    # ── 관리자: 제출 목록·검토 (review_views.py) ──
    path(f"{B}/admin/submissions/", review_views.submission_list, name="admin_list"),
    path(f"{B}/review/", review_views.queue, name="review_queue"),
    path(f"{B}/review/bulk-approve/", review_views.bulk_approve, name="review_bulk_approve"),
    path(f"{B}/results/", review_views.results, name="results"),
    path(f"{B}/results.csv", review_views.results_csv, name="results_csv"),
    path(f"{R}/", review_views.detail, name="review_detail"),
    path(f"{R}/propose/", review_views.propose, name="review_propose"),
    path(f"{R}/decide/", review_views.decide, name="review_decide"),
    path(f"{R}/send-back/", review_views.send_back, name="review_send_back"),
    path(f"{R}/reopen/", review_views.reopen, name="review_reopen"),
    path(f"{R}/files/<str:kind>/", review_views.file_download, name="review_file"),
    path(f"{R}/notebook/", review_views.notebook, name="review_notebook"),
    path(f"{R}/reverify/", review_views.reverify, name="review_reverify"),
    path(f"{R}/progress/", review_views.progress, name="review_progress"),
]
