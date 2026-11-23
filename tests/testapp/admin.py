from django.contrib import admin

from .models import Company, RegistrationRequest

# Registered: {admin_link} reverses to .../<pk>/change/. Other models are not
# registered and use the /admin/<app>/<model>/<pk>/ fallback.
admin.site.register(Company)
admin.site.register(RegistrationRequest)
