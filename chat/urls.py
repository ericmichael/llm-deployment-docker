from django.urls import path

from . import views
from . import views_courses
from . import views_models
from . import views_usage

urlpatterns = [
    path("settings/", views.developer_settings, name="settings"),
    path("settings/regenerate-key/", views.regenerate_key, name="regenerate_key"),

    # Models (all authenticated users)
    path("models/", views_models.model_list, name="model_list"),

    # Usage dashboard (staff only)
    path("usage/", views_usage.usage_dashboard, name="usage_dashboard"),
    path("usage/reset-all/", views_usage.usage_reset_all, name="usage_reset_all"),

    # Course management (staff only)
    path("courses/", views_courses.course_list, name="course_list"),
    path("courses/create/", views_courses.course_create, name="course_create"),
    path("courses/<int:course_id>/", views_courses.course_detail, name="course_detail"),
    path("courses/<int:course_id>/toggle-active/", views_courses.course_toggle_active, name="course_toggle_active"),
    path("courses/<int:course_id>/add-student/", views_courses.course_add_student, name="course_add_student"),
    path("courses/<int:course_id>/add-ta/", views_courses.course_add_ta, name="course_add_ta"),
    path("courses/<int:course_id>/import-csv/", views_courses.course_import_csv, name="course_import_csv"),
    path("courses/<int:course_id>/budget/", views_courses.course_set_budget, name="course_set_budget"),
    path("courses/<int:course_id>/reset-usage/", views_courses.course_reset_usage, name="course_reset_usage"),
    path("courses/<int:course_id>/revoke-keys/", views_courses.course_revoke_keys, name="course_revoke_keys"),
    path("courses/<int:course_id>/access/", views_courses.course_set_access, name="course_set_access"),
    path("enrollment/<int:enrollment_id>/remove/", views_courses.enrollment_remove, name="enrollment_remove"),
    path("enrollment/<int:enrollment_id>/budget/", views_courses.enrollment_set_budget, name="enrollment_set_budget"),
    path("enrollment/<int:enrollment_id>/reset-usage/", views_courses.enrollment_reset_usage, name="enrollment_reset_usage"),
]
