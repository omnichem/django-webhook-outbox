"""Ported from the original implementation, re-pointed to the test models."""

from django.test import TestCase, override_settings

from tests.factories import create_company, create_person
from tests.testapp.models import PlainItem, RegistrationRequest
from webhook_outbox.services.template import (
    extract_template_fields,
    get_admin_url,
    render_template,
    resolve_field_value,
    validate_template,
)


class ResolveFieldValueTest(TestCase):
    def setUp(self):
        self.user = create_person("test@example.com")
        self.company = create_company("Acme Corp", "1234567890")
        self.request = RegistrationRequest.objects.create(
            company=self.company, user=self.user
        )

    def test_resolve_direct_field(self):
        value = resolve_field_value(self.company, "company_name")
        self.assertEqual(value, "Acme Corp")

    def test_resolve_fk_field(self):
        value = resolve_field_value(self.request, "company")
        self.assertEqual(value, self.company)

    def test_resolve_fk_traversal(self):
        value = resolve_field_value(self.request, "company.company_name")
        self.assertEqual(value, "Acme Corp")

    def test_resolve_deep_traversal(self):
        value = resolve_field_value(self.request, "company.company_type.name")
        self.assertIsNotNone(value)

    def test_resolve_invalid_field(self):
        value = resolve_field_value(self.company, "nonexistent_field")
        self.assertIsNone(value)

    def test_resolve_invalid_path(self):
        value = resolve_field_value(self.request, "company.fake.field")
        self.assertIsNone(value)

    # --- added: behaviour pinned for 1.0 ------------------------------------

    def test_resolve_calls_callables(self):
        """Zero-argument callables on the path are called (see docs/security.md)."""
        value = resolve_field_value(self.request, "get_status_display")
        self.assertEqual(value, "New")

    def test_resolve_none_in_the_middle(self):
        self.company.company_type = None
        self.company.save()
        value = resolve_field_value(self.request, "company.company_type.name")
        self.assertIsNone(value)


class RenderTemplateTest(TestCase):
    def setUp(self):
        self.user = create_person("test@example.com")
        self.company = create_company("Test Company", "9876543210")
        self.request = RegistrationRequest.objects.create(
            company=self.company, user=self.user
        )

    def test_render_simple_field(self):
        template = "Company: {company_name}"
        result = render_template(template, self.company)
        self.assertEqual(result, "Company: Test Company")

    def test_render_fk_traversal(self):
        template = "New registration for {company.company_name}"
        result = render_template(template, self.request)
        self.assertEqual(result, "New registration for Test Company")

    def test_render_multiple_fields(self):
        template = "Request #{pk} for {company.company_name} by {user.email}"
        result = render_template(template, self.request)
        self.assertIn(str(self.request.pk), result)
        self.assertIn("Test Company", result)
        self.assertIn("test@example.com", result)

    def test_render_invalid_field_placeholder(self):
        template = "Value: {nonexistent_field}"
        result = render_template(template, self.company)
        self.assertEqual(result, "Value: [nonexistent_field]")

    def test_render_no_placeholders(self):
        template = "Static message with no placeholders"
        result = render_template(template, self.company)
        self.assertEqual(result, "Static message with no placeholders")

    def test_render_mixed_valid_invalid(self):
        template = "Company {company_name} has {fake_field}"
        result = render_template(template, self.company)
        self.assertIn("Test Company", result)
        self.assertIn("[fake_field]", result)

    def test_render_admin_link(self):
        """Should render {admin_link} as full admin URL."""
        template = "View: {admin_link}"
        result = render_template(template, self.request)
        self.assertIn("/admin/testapp/registrationrequest/", result)
        self.assertIn(str(self.request.pk), result)

    def test_render_admin_link_with_other_fields(self):
        """Should render {admin_link} alongside other fields."""
        template = "New request for {company.company_name}\n{admin_link}"
        result = render_template(template, self.request)
        self.assertIn("Test Company", result)
        self.assertIn("/admin/testapp/registrationrequest/", result)

    # --- added: behaviour pinned for 1.0 ------------------------------------

    @override_settings(SITE_URL="https://legacy.example.com/")
    def test_admin_link_uses_legacy_site_url(self):
        result = render_template("{admin_link}", self.request)
        self.assertEqual(
            result,
            f"https://legacy.example.com/admin/testapp/registrationrequest/{self.request.pk}/change/",
        )

    @override_settings(
        SITE_URL="https://legacy.example.com",
        WEBHOOK_OUTBOX={"SITE_URL": "https://shop.example.com/"},
    )
    def test_admin_link_prefers_webhook_outbox_site_url(self):
        result = render_template("{admin_link}", self.request)
        self.assertTrue(result.startswith("https://shop.example.com/admin/"))

    def test_admin_link_fallback_for_unregistered_model(self):
        item = PlainItem.objects.create(name="x")
        self.assertEqual(get_admin_url(item), f"/admin/testapp/plainitem/{item.pk}/")

    def test_raw_template_returned_when_rendering_raises(self):
        """A value whose str() raises makes the whole render fall back to the raw template."""

        class Exploding:
            pk = 1

            @property
            def boom(self):
                return self

            def __str__(self):
                raise RuntimeError("no str for you")

        template = "Value: {boom} and {pk}"
        self.assertEqual(render_template(template, Exploding()), template)

    def test_placeholder_syntax(self):
        """Only {identifier.path} is a placeholder; other braces are left alone."""
        result = render_template("{1abc} {} {company_name} {{x}}", self.company)
        self.assertEqual(result, "{1abc} {} Test Company {[x]}")


class ValidateTemplateTest(TestCase):
    def test_valid_paths(self):
        errors = validate_template(
            "{company.company_type.name} {user.email} {pk}", RegistrationRequest
        )
        self.assertEqual(errors, [])

    def test_invalid_path(self):
        errors = validate_template("{company.nope}", RegistrationRequest)
        self.assertEqual(
            errors, ["Invalid field path: 'company.nope' - 'nope' not found"]
        )

    def test_traverse_beyond_non_relation(self):
        errors = validate_template("{company.company_name.upper}", RegistrationRequest)
        self.assertEqual(len(errors), 1)


class ExtractTemplateFieldsTest(TestCase):
    def test_extract_single_field(self):
        template = "Hello {name}"
        fields = extract_template_fields(template)
        self.assertEqual(fields, ["name"])

    def test_extract_multiple_fields(self):
        template = "{first} and {second} with {third}"
        fields = extract_template_fields(template)
        self.assertEqual(fields, ["first", "second", "third"])

    def test_extract_nested_fields(self):
        template = "Company: {company.name}, User: {user.email}"
        fields = extract_template_fields(template)
        self.assertEqual(fields, ["company.name", "user.email"])

    def test_extract_deep_nested(self):
        template = "Value: {a.b.c.d}"
        fields = extract_template_fields(template)
        self.assertEqual(fields, ["a.b.c.d"])

    def test_extract_no_fields(self):
        template = "No placeholders here"
        fields = extract_template_fields(template)
        self.assertEqual(fields, [])

    def test_extract_with_underscores(self):
        template = "Field: {company_name}"
        fields = extract_template_fields(template)
        self.assertEqual(fields, ["company_name"])
