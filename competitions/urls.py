from django.urls import path

from . import views

app_name = "competitions"

P = "<int:competition_id>/projects/<int:project_id>"

urlpatterns = [
    path("new/", views.create, name="create"),
    path("<int:competition_id>/", views.detail, name="detail"),
    path("<int:competition_id>/code/reissue/", views.reissue_code, name="reissue_code"),
    path("<int:competition_id>/members/", views.members, name="members"),
    path("<int:competition_id>/members/<int:member_id>/promote/", views.promote, name="promote"),
    path("<int:competition_id>/members/<int:member_id>/revoke/", views.revoke, name="revoke"),
    path("<int:competition_id>/members/<int:member_id>/reset-password/", views.member_reset_password, name="member_reset_password"),
    path("<int:competition_id>/members/<int:member_id>/unlock/", views.member_unlock, name="member_unlock"),
    path("<int:competition_id>/members/<int:member_id>/suspend/", views.member_suspend, name="member_suspend"),
    path("<int:competition_id>/members/<int:member_id>/reinstate/", views.member_reinstate, name="member_reinstate"),
    # 프로젝트
    path("<int:competition_id>/projects/new/", views.project_create, name="project_create"),
    path(f"{P}/", views.project_detail, name="project_detail"),
    path(f"{P}/edit/", views.project_edit, name="project_edit"),
    path(f"{P}/deadlines/new/", views.deadline_add, name="deadline_add"),
    path(f"{P}/deadlines/<int:deadline_id>/delete/", views.deadline_delete, name="deadline_delete"),
    path(f"{P}/files/upload/", views.file_upload, name="file_upload"),
    path(f"{P}/files/<str:kind>/", views.file_download, name="file_download"),
    path(f"{P}/sample.csv", views.sample_csv, name="sample_csv"),
]
