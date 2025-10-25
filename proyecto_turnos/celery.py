import os
from celery import Celery
from celery.schedules import crontab

# Establecer el módulo de configuración Django por defecto
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'proyecto_turnos.settings')

app = Celery('planificador_turnos')

# Usar string aquí significa que el worker no necesita serializar
# el objeto de configuración a procesos hijos.
app.config_from_object('django.conf:settings', namespace='CELERY')

# Cargar módulos de tareas desde todas las apps registradas
app.autodiscover_tasks()

# Configuración de tareas periódicas
app.conf.beat_schedule = {
    'limpiar-ejecuciones-antiguas': {
        'task': 'turnos.tasks.limpiar_ejecuciones_antiguas',
        'schedule': crontab(hour=2, minute=0),  # Cada día a las 2:00 AM
    },
    'enviar-recordatorios': {
        'task': 'turnos.tasks.enviar_recordatorios_turnos',
        'schedule': crontab(hour=8, minute=0),  # Cada día a las 8:00 AM
    },
}

@app.task(bind=True)
def debug_task(self):
    print(f'Request: {self.request!r}')
