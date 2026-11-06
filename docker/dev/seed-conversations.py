"""Create local-only accounts once, preserving later password and rights changes."""

import os

from configurations import importer

importer.install()
import django  # noqa: E402

django.setup()
from core.models import MinistryGrant, User  # noqa: E402
from django.contrib.auth.hashers import make_password  # noqa: E402
from django.contrib.auth.models import Group  # noqa: E402
from django.db import transaction  # noqa: E402

for email, administrator in (("dev@example.invalid", False), ("admin@example.invalid", True)):
    # An interrupted seed must not leave an account that later runs consider already provisioned.
    with transaction.atomic():
        user, created = User.objects.get_or_create(
            admin_email=email,
            defaults={
                "email": email,
                "password": make_password(os.environ["LOCAL_DEV_PASSWORD"]),
                "assistant_rh_admitted": True,
                "is_staff": administrator,
                "is_superuser": administrator,
            },
        )
        if created:
            if not administrator:
                for ministry in ("matte", "mso", "mi", "masa"):
                    group, _ = Group.objects.get_or_create(name="Local " + ministry.upper())
                    MinistryGrant.objects.get_or_create(group=group, ministry=ministry)
                    user.groups.add(group)
