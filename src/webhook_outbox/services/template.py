import logging
import re
from typing import Any

from django.db import models
from django.urls import reverse

from webhook_outbox.conf import get_setting

logger = logging.getLogger(__name__)

FIELD_PATTERN = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_.]*)\}")

# Special template variables
ADMIN_LINK_VAR = "admin_link"


def get_admin_url(instance: models.Model) -> str:
    """
    Generate the full admin URL for a model instance.

    Args:
        instance: Django model instance

    Returns:
        Full URL to the admin change page, e.g.:
        https://example.com/admin/shop/order/38/change/
    """
    meta = instance._meta
    app_label = meta.app_label
    model_name = meta.model_name

    try:
        admin_path = reverse(
            f"admin:{app_label}_{model_name}_change",
            args=[instance.pk],
        )
    except Exception:
        admin_path = f"/admin/{app_label}/{model_name}/{instance.pk}/"

    base_url = (get_setting("SITE_URL") or "").rstrip("/")
    return f"{base_url}{admin_path}"


def resolve_field_value(instance: models.Model, field_path: str) -> Any:
    """
    Resolve a dot-notation field path on a model instance.

    Examples:
        - "company_name" -> instance.company_name
        - "company.company_name" -> instance.company.company_name
        - "status.value" -> instance.status.value

    Every callable met on the way (except managers) is called without
    arguments: ``{get_status_display}`` works, and so would any other
    zero-argument method. See docs/security.md.

    Args:
        instance: Django model instance
        field_path: Dot-notation path to the field

    Returns:
        The resolved field value, or None if the path is invalid
    """
    parts = field_path.split(".")
    current = instance

    for part in parts:
        if current is None:
            return None

        try:
            current = getattr(current, part)
            if callable(current) and not isinstance(current, models.Manager):
                current = current()
        except AttributeError:
            logger.warning(
                f"Failed to resolve field '{part}' in path '{field_path}' "
                f"on {type(instance).__name__}"
            )
            return None

    return current


def render_template(template: str, instance: models.Model) -> str:
    """
    Render a message template by replacing {field.path} placeholders
    with actual values from the model instance.

    Special variables:
        - {admin_link}: Full URL to the admin change page for this instance

    A placeholder that resolves to None renders as ``[field.path]``. If
    rendering raises, the raw template is returned unchanged.

    Args:
        template: Message template with {field} placeholders
        instance: Django model instance to get values from

    Returns:
        Rendered message string

    Examples:
        >>> render_template("New order for {customer.name}", order)
        "New order for Acme Corp"

        >>> render_template("Status changed to {status.value}", order)
        "Status changed to Pending Review"

        >>> render_template("View: {admin_link}", order)
        "View: https://example.com/admin/shop/order/38/change/"
    """

    def replace_field(match: re.Match) -> str:
        field_path = match.group(1)

        # Handle special variables
        if field_path == ADMIN_LINK_VAR:
            return get_admin_url(instance)

        value = resolve_field_value(instance, field_path)

        if value is None:
            logger.warning(
                f"Template field '{field_path}' resolved to None for "
                f"{type(instance).__name__}#{instance.pk}"
            )
            return f"[{field_path}]"

        return str(value)

    try:
        return FIELD_PATTERN.sub(replace_field, template)
    except Exception as e:
        logger.exception(f"Failed to render template: {e}")
        return template


def validate_template(template: str, model_class: type[models.Model]) -> list[str]:
    """
    Validate that all field references in a template are valid for the given model.

    Args:
        template: Message template to validate
        model_class: Django model class to validate against

    Returns:
        List of error messages (empty if valid)
    """
    errors = []
    fields = FIELD_PATTERN.findall(template)

    for field_path in fields:
        parts = field_path.split(".")
        current_model = model_class

        for i, part in enumerate(parts):
            if current_model is None:
                errors.append(
                    f"Cannot traverse beyond '{'.'.join(parts[:i])}' - not a relation"
                )
                break

            try:
                field = current_model._meta.get_field(part)

                if i < len(parts) - 1:
                    if hasattr(field, "related_model"):
                        current_model = field.related_model
                    else:
                        current_model = None
            except Exception:
                if hasattr(current_model, part):
                    if i < len(parts) - 1:
                        current_model = None
                else:
                    errors.append(
                        f"Invalid field path: '{field_path}' - '{part}' not found"
                    )
                    break

    return errors


def extract_template_fields(template: str) -> list[str]:
    """
    Extract all field references from a template.

    Args:
        template: Message template

    Returns:
        List of field paths found in the template
    """
    return FIELD_PATTERN.findall(template)
