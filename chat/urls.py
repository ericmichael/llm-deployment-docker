from django.urls import path

from . import views
from . import views_courses
from . import views_models
from . import views_usage

urlpatterns = [
    path("settings/", views.developer_settings, name="settings"),

    # Models (all authenticated users)
    path("models/", views_models.model_list, name="model_list"),

    # Usage dashboard (staff only)
    path("usage/", views_usage.usage_dashboard, name="usage_dashboard"),

    # Course management (staff only)
    path("courses/", views_courses.course_list, name="course_list"),
    path("courses/create/", views_courses.course_create, name="course_create"),
    path("courses/<int:course_id>/", views_courses.course_detail, name="course_detail"),
    path("courses/<int:course_id>/toggle-active/", views_courses.course_toggle_active, name="course_toggle_active"),
    path("courses/<int:course_id>/add-student/", views_courses.course_add_student, name="course_add_student"),
    path("courses/<int:course_id>/add-ta/", views_courses.course_add_ta, name="course_add_ta"),
    path("courses/<int:course_id>/import-csv/", views_courses.course_import_csv, name="course_import_csv"),
    path("enrollment/<int:enrollment_id>/remove/", views_courses.enrollment_remove, name="enrollment_remove"),
    path("enrollment/<int:enrollment_id>/reset-password/", views_courses.enrollment_reset_password, name="enrollment_reset_password"),
]
