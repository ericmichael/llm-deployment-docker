import csv
import io

from .models import CustomUser, Course, Enrollment
from .forms import CSVImportForm, AddTAForm
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin
from django.db import transaction
from django.shortcuts import render, get_object_or_404, redirect
from django.urls import path, reverse
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from rest_framework.authtoken.models import Token


User = get_user_model()


class CustomUserAdmin(UserAdmin):
    # Define a custom UserAdmin
    model = CustomUser
    list_display = ('email', 'is_staff', 'is_active',)
    list_filter = ('email', 'is_staff', 'is_active',)
    fieldsets = (
        (None, {'fields': ('email', 'password')}),
        (_('Personal info'), {'fields': ()}),  # Add any additional fields here
        (_('Permissions'), {
            'fields': ('is_staff', 'is_active', 'is_superuser', 'groups', 'user_permissions'),
        }),
        (_('Important dates'), {'fields': ('last_login', 'date_joined')}),
    )
    add_fieldsets = (
        (None, {
            'classes': ('wide',),
            'fields': ('email', 'password1', 'password2', 'is_staff', 'is_active')}
        ),
    )
    search_fields = ('email',)
    ordering = ('email',)


class EnrollmentInline(admin.TabularInline):
    """Inline view of enrollments for a course."""
    model = Enrollment
    extra = 0
    readonly_fields = ('enrolled_at',)
    autocomplete_fields = ('user',)


@admin.register(Course)
class CourseAdmin(admin.ModelAdmin):
    list_display = ('code', 'name', 'semester', 'is_active', 'student_count', 'ta_count', 'import_link', 'created_at')
    list_filter = ('is_active', 'semester')
    search_fields = ('code', 'name', 'semester')
    ordering = ('-created_at',)
    inlines = [EnrollmentInline]
    actions = ['activate_courses', 'deactivate_courses']

    @admin.display(description='Students')
    def student_count(self, obj):
        return obj.enrollments.filter(role=Enrollment.Role.STUDENT).count()

    @admin.display(description='TAs')
    def ta_count(self, obj):
        return obj.enrollments.filter(role=Enrollment.Role.TA).count()

    @admin.display(description='Actions')
    def import_link(self, obj):
        import_url = reverse('admin:chat_course_import_csv', args=[obj.pk])
        add_ta_url = reverse('admin:chat_course_add_ta', args=[obj.pk])
        return format_html(
            '<a href="{}">Import CSV</a> | <a href="{}">Add TA</a>',
            import_url,
            add_ta_url,
        )

    @admin.action(description='Activate selected courses')
    def activate_courses(self, request, queryset):
        updated = queryset.update(is_active=True)
        self.message_user(request, f'{updated} course(s) activated.')

    @admin.action(description='Deactivate selected courses')
    def deactivate_courses(self, request, queryset):
        updated = queryset.update(is_active=False)
        self.message_user(request, f'{updated} course(s) deactivated.')

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                '<int:course_id>/import-csv/',
                self.admin_site.admin_view(self.import_csv_view),
                name='chat_course_import_csv',
            ),
            path(
                '<int:course_id>/add-ta/',
                self.admin_site.admin_view(self.add_ta_view),
                name='chat_course_add_ta',
            ),
        ]
        return custom_urls + urls

    def import_csv_view(self, request, course_id):
        """Handle CSV import for a specific course."""
        course = get_object_or_404(Course, pk=course_id)
        results = None

        if request.method == 'POST':
            form = CSVImportForm(request.POST, request.FILES)
            if form.is_valid():
                results = self._process_csv_import(
                    form.cleaned_data['csv_file'],
                    course,
                    form.cleaned_data['role'],
                )
                # Reset form after successful import
                form = CSVImportForm()
        else:
            form = CSVImportForm()

        context = {
            **self.admin_site.each_context(request),
            'form': form,
            'course': course,
            'results': results,
            'opts': self.model._meta,
            'title': f'Import Users - {course.code}',
        }
        return render(request, 'admin/chat/course/import_csv.html', context)

    def _process_csv_import(self, csv_file, course, role):
        """Process the CSV file and create users/enrollments."""
        results = {
            'created_users': 0,
            'created_enrollments': 0,
            'moved_enrollments': 0,
            'skipped': 0,
            'errors': [],
        }

        # Decode and parse CSV
        try:
            decoded = csv_file.read().decode('utf-8')
            reader = csv.DictReader(io.StringIO(decoded))
            rows = list(reader)
        except Exception as e:
            results['errors'].append(f'Error reading CSV: {e}')
            return results

        if not rows:
            results['errors'].append('CSV file is empty')
            return results

        # Validate columns
        required = {'email', 'student_id'}
        if not required.issubset(rows[0].keys()):
            results['errors'].append(
                f"CSV must have columns: email, student_id. Found: {', '.join(rows[0].keys())}"
            )
            return results

        is_student = (role == Enrollment.Role.STUDENT)

        with transaction.atomic():
            for i, row in enumerate(rows, start=2):  # Start at 2 (header is row 1)
                email = row.get('email', '').strip().lower()
                student_id = row.get('student_id', '').strip()

                if not email or not student_id:
                    results['errors'].append(f'Row {i}: Missing email or student_id')
                    results['skipped'] += 1
                    continue

                # Get or create user
                user, user_created = User.objects.get_or_create(
                    email=email,
                    defaults={'is_active': True},
                )

                if user_created:
                    user.set_password(f'ai_{student_id}')
                    user.save()
                    Token.objects.get_or_create(user=user)
                    results['created_users'] += 1

                # Check existing enrollment
                if is_student:
                    existing = Enrollment.objects.filter(
                        user=user, role=Enrollment.Role.STUDENT
                    ).first()
                    if existing:
                        if existing.course == course:
                            results['skipped'] += 1
                            continue
                        else:
                            # Move student to new course
                            existing.delete()
                            results['moved_enrollments'] += 1
                else:
                    # TA - check if already in this course
                    if Enrollment.objects.filter(course=course, user=user).exists():
                        results['skipped'] += 1
                        continue

                # Create enrollment
                Enrollment.objects.create(
                    course=course,
                    user=user,
                    student_id=student_id,
                    role=role,
                )
                results['created_enrollments'] += 1

        return results

    def add_ta_view(self, request, course_id):
        """Handle adding a single TA to a course."""
        course = get_object_or_404(Course, pk=course_id)
        message = None
        message_type = None

        if request.method == 'POST':
            form = AddTAForm(request.POST)
            if form.is_valid():
                email = form.cleaned_data['email'].strip().lower()
                student_id = form.cleaned_data['student_id'].strip()

                # Get or create user
                user, user_created = User.objects.get_or_create(
                    email=email,
                    defaults={'is_active': True},
                )

                if user_created:
                    user.set_password(f'ai_{student_id}')
                    user.save()
                    Token.objects.get_or_create(user=user)

                # Check if already enrolled in this course
                if Enrollment.objects.filter(course=course, user=user).exists():
                    message = f'{email} is already enrolled in this course.'
                    message_type = 'warning'
                else:
                    Enrollment.objects.create(
                        course=course,
                        user=user,
                        student_id=student_id,
                        role=Enrollment.Role.TA,
                    )
                    if user_created:
                        message = f'Created user and added {email} as TA.'
                    else:
                        message = f'Added {email} as TA.'
                    message_type = 'success'
                    # Clear form for next entry
                    form = AddTAForm()
        else:
            form = AddTAForm()

        context = {
            **self.admin_site.each_context(request),
            'form': form,
            'course': course,
            'message': message,
            'message_type': message_type,
            'opts': self.model._meta,
            'title': f'Add TA - {course.code}',
        }
        return render(request, 'admin/chat/course/add_ta.html', context)


@admin.register(Enrollment)
class EnrollmentAdmin(admin.ModelAdmin):
    list_display = ('user', 'course', 'student_id', 'role', 'enrolled_at')
    list_filter = ('role', 'course', 'course__is_active')
    search_fields = ('user__email', 'student_id', 'course__code', 'course__name')
    ordering = ('course', 'user__email')
    autocomplete_fields = ('user', 'course')
    actions = ['reset_passwords', 'remove_enrollments']

    @admin.action(description='Reset passwords to ai_<student_id>')
    def reset_passwords(self, request, queryset):
        count = 0
        for enrollment in queryset:
            enrollment.user.set_password(f'ai_{enrollment.student_id}')
            enrollment.user.save()
            count += 1
        self.message_user(request, f'Reset passwords for {count} user(s).')

    @admin.action(description='Remove selected enrollments')
    def remove_enrollments(self, request, queryset):
        count = queryset.count()
        queryset.delete()
        self.message_user(request, f'Removed {count} enrollment(s).')


# Register your models here
admin.site.register(CustomUser, CustomUserAdmin)
