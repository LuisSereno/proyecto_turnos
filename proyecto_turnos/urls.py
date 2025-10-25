from django.contrib import admin
from django.urls import path, include
from django.views.generic import RedirectView
from django.conf.urls.i18n import i18n_patterns

urlpatterns = [
    path('i18n/', include('django.conf.urls.i18n')),  # Para cambiar idioma
]

urlpatterns += i18n_patterns(
    path('admin/', admin.site.urls),
    path('', RedirectView.as_view(url='/turnos/dashboard/', permanent=False), name='home'),
    path('turnos/', include('turnos.urls', namespace='turnos')),
    path('accounts/', include('django.contrib.auth.urls')),  # Login/Logout
)
