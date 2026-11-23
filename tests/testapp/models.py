"""
Models of the test project.

* OrganizationType <- Company <- RegistrationRequest: an FK chain for
  template paths ({company.company_type.name}); all timestamped.
* Person: the "user" of a request (has an email).
* SoftDeletedItem: default manager hides soft-deleted rows.
* CustomManagerItem: no ``objects`` manager at all (``items`` only).
* InsertOnlyItem: created_at only (INSERT can be tracked, UPDATE cannot).
* PlainItem: no timestamps at all.
"""

from django.db import models
from django.utils import timezone

from webhook_outbox.models import TimeStampedModel


class OrganizationType(TimeStampedModel):
    name = models.CharField(max_length=100)

    def __str__(self):
        return self.name


class Company(TimeStampedModel):
    company_name = models.CharField(max_length=200)
    inn = models.CharField(max_length=12)
    company_type = models.ForeignKey(
        OrganizationType, null=True, blank=True, on_delete=models.SET_NULL
    )
    address = models.CharField(max_length=200, blank=True)

    class Meta:
        verbose_name_plural = "companies"

    def __str__(self):
        return self.company_name


class Person(TimeStampedModel):
    email = models.EmailField()

    def __str__(self):
        return self.email


class RegistrationRequest(TimeStampedModel):
    class Status(models.TextChoices):
        NEW = "new", "New"
        APPROVED = "approved", "Approved"

    company = models.ForeignKey(Company, on_delete=models.CASCADE)
    user = models.ForeignKey(Person, on_delete=models.CASCADE)
    status = models.CharField(max_length=16, choices=Status.choices, default="new")
    processed = models.BooleanField(default=False)


class SoftDeleteQuerySet(models.QuerySet):
    def delete(self):
        return super().update(deleted_at=timezone.now())


class SoftDeleteManager(models.Manager):
    def get_queryset(self):
        return SoftDeleteQuerySet(self.model, using=self._db).filter(
            deleted_at__isnull=True
        )


class SoftDeletedItem(TimeStampedModel):
    name = models.CharField(max_length=100)
    deleted_at = models.DateTimeField(null=True, blank=True)

    objects = SoftDeleteManager()
    all_objects = models.Manager()


class CustomManagerItem(TimeStampedModel):
    name = models.CharField(max_length=100)

    items = models.Manager()


class InsertOnlyItem(models.Model):
    name = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class PlainItem(models.Model):
    name = models.CharField(max_length=100)

    def __str__(self):
        return self.name
