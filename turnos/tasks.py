"""
Celery tasks for turnos app
"""
from celery import shared_task
from django.core.mail import send_mail
from django.conf import settings
from django.utils import timezone
import logging

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3)
def ejecutar_planificacion_async(self, ejecucion_id):
    """
    Tarea asíncrona para ejecutar una planificación

    Args:
        ejecucion_id: ID de la ejecución a procesar
    """
    from .models import Ejecucion, Planilla, AsignacionTurno
    from .generador import GeneradorPlanificacion

    try:
        # Obtener ejecución
        ejecucion = Ejecucion.objects.get(id=ejecucion_id)
        ejecucion.estado = 'PROCESANDO'
        ejecucion.save()

        logger.info(f'Iniciando ejecución {ejecucion_id}')

        # Generar planificación
        generador = GeneradorPlanificacion(ejecucion.configuracion)
        resultado = generador.resolver()

        # Actualizar ejecución
        ejecucion.estado = 'COMPLETADA' if resultado['status'] in ['OPTIMAL', 'FEASIBLE'] else 'ERROR'
        ejecucion.fecha_fin = timezone.now()
        ejecucion.es_optima = resultado['es_optima']
        ejecucion.penalizacion_total = resultado['penalizacion']
        ejecucion.resultado = resultado
        ejecucion.save()

        # Crear planilla si fue exitosa
        if ejecucion.estado == 'COMPLETADA':
            planilla = crear_planilla_desde_resultado(ejecucion, resultado)
            ejecucion.planilla = planilla
            ejecucion.save()

            logger.info(f'Ejecución {ejecucion_id} completada con éxito')

            # Enviar notificación
            enviar_notificacion_ejecucion.delay(ejecucion_id, 'completada')
        else:
            logger.error(f'Ejecución {ejecucion_id} falló: {resultado["status"]}')
            enviar_notificacion_ejecucion.delay(ejecucion_id, 'error')

        return {'status': 'success', 'ejecucion_id': ejecucion_id}

    except Exception as exc:
        logger.error(f'Error en ejecución {ejecucion_id}: {str(exc)}', exc_info=True)

        # Actualizar estado a error
        try:
            ejecucion = Ejecucion.objects.get(id=ejecucion_id)
            ejecucion.estado = 'ERROR'
            ejecucion.fecha_fin = timezone.now()
            ejecucion.mensajes = {'error': str(exc)}
            ejecucion.save()
        except:
            pass

        # Reintentar hasta 3 veces
        raise self.retry(exc=exc, countdown=60)


def crear_planilla_desde_resultado(ejecucion, resultado):
    """
    Crea una planilla y sus asignaciones desde el resultado

    Args:
        ejecucion: Instancia de Ejecucion
        resultado: Diccionario con el resultado de la optimización

    Returns:
        Instancia de Planilla creada
    """
    from .models import Planilla, AsignacionTurno, TipoTurno, Enfermera
    from datetime import datetime, timedelta

    config = ejecucion.configuracion

    # Crear planilla
    planilla = Planilla.objects.create(
        nombre=f"{config.nombre} - {ejecucion.fecha_inicio.strftime('%d/%m/%Y')}",
        descripcion=f"Planilla generada automáticamente",
        ejecucion=ejecucion,
        fecha_inicio=config.fecha_inicio,
        fecha_fin=config.fecha_inicio + timedelta(days=config.num_dias - 1),
        num_dias=config.num_dias
    )

    # Crear asignaciones
    asignaciones_bulk = []
    for asignacion_data in resultado['asignaciones']:
        fecha = datetime.fromisoformat(asignacion_data['fecha']).date()
        enfermera = Enfermera.objects.get(id=asignacion_data['enfermera_id'])

        turno = None
        if asignacion_data['turno_id']:
            turno = TipoTurno.objects.get(id=asignacion_data['turno_id'])

        asignacion = AsignacionTurno(
            planilla=planilla,
            enfermera=enfermera,
            fecha=fecha,
            turno=turno,
            es_dia_libre=asignacion_data['es_dia_libre']
        )
        asignaciones_bulk.append(asignacion)

    # Crear todas las asignaciones en una sola operación
    AsignacionTurno.objects.bulk_create(asignaciones_bulk)

    logger.info(f'Planilla {planilla.id} creada con {len(asignaciones_bulk)} asignaciones')

    return planilla


@shared_task
def enviar_notificacion_ejecucion(ejecucion_id, tipo='completada'):
    """
    Envía notificación por email sobre el estado de una ejecución

    Args:
        ejecucion_id: ID de la ejecución
        tipo: Tipo de notificación ('completada', 'error')
    """
    from .models import Ejecucion

    try:
        ejecucion = Ejecucion.objects.get(id=ejecucion_id)
        config = ejecucion.configuracion

        if tipo == 'completada':
            subject = f'Planificación completada: {config.nombre}'
            message = f'''
            La planificación "{config.nombre}" ha sido completada con éxito.

            Estado: {'Óptima' if ejecucion.es_optima else 'Factible'}
            Penalización: {ejecucion.penalizacion_total:.2f}
            Duración: {ejecucion.duracion:.2f} segundos

            Puedes ver el resultado en: {settings.SITE_URL}{ejecucion.get_absolute_url()}
            '''
        else:
            subject = f'Error en planificación: {config.nombre}'
            message = f'''
            Ha ocurrido un error al generar la planificación "{config.nombre}".

            Por favor, revisa la configuración e intenta nuevamente.
            '''

        # Enviar email al creador
        if config.creado_por and config.creado_por.email:
            send_mail(
                subject=subject,
                message=message,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[config.creado_por.email],
                fail_silently=True
            )

            logger.info(f'Notificación enviada a {config.creado_por.email}')

    except Exception as exc:
        logger.error(f'Error al enviar notificación para ejecución {ejecucion_id}: {str(exc)}')


@shared_task
def limpiar_ejecuciones_antiguas(dias=30):
    """
    Limpia ejecuciones antiguas (más de X días)

    Args:
        dias: Número de días de antigüedad
    """
    from .models import Ejecucion
    from django.utils import timezone
    from datetime import timedelta

    fecha_limite = timezone.now() - timedelta(days=dias)

    # Eliminar ejecuciones antiguas en estado ERROR
    eliminadas = Ejecucion.objects.filter(
        estado='ERROR',
        fecha_inicio__lt=fecha_limite
    ).delete()

    logger.info(f'Eliminadas {eliminadas[0]} ejecuciones antiguas')

    return {'eliminadas': eliminadas[0]}


@shared_task
def exportar_planilla_excel(planilla_id, email_destino):
    """
    Exporta una planilla a Excel y envía por email

    Args:
        planilla_id: ID de la planilla
        email_destino: Email al que enviar el archivo
    """
    from .models import Planilla
    from openpyxl import Workbook
    from django.core.mail import EmailMessage
    import tempfile

    try:
        planilla = Planilla.objects.get(id=planilla_id)

        # Crear workbook
        wb = Workbook()
        ws = wb.active
        ws.title = "Planilla"

        # Headers
        ws.append(['Enfermera', 'Fecha', 'Turno', 'Horario'])

        # Datos
        for asignacion in planilla.asignaciones.all().select_related('enfermera', 'turno'):
            if asignacion.es_dia_libre:
                turno_info = 'Libre'
                horario = '-'
            else:
                turno_info = asignacion.turno.get_nombre_display()
                horario = f"{asignacion.turno.hora_inicio.strftime('%H:%M')} - {asignacion.turno.hora_fin.strftime('%H:%M')}"

            ws.append([
                asignacion.enfermera.nombre,
                asignacion.fecha.strftime('%d/%m/%Y'),
                turno_info,
                horario
            ])

        # Guardar en archivo temporal
        with tempfile.NamedTemporaryFile(suffix='.xlsx', delete=False) as tmp:
            wb.save(tmp.name)
            tmp_path = tmp.name

        # Enviar por email
        email = EmailMessage(
            subject=f'Planilla: {planilla.nombre}',
            body='Adjunto encontrarás la planilla de turnos exportada.',
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[email_destino]
        )

        with open(tmp_path, 'rb') as f:
            email.attach(f'planilla_{planilla.id}.xlsx', f.read(),
                         'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

        email.send()

        logger.info(f'Planilla {planilla_id} exportada y enviada a {email_destino}')

        return {'status': 'success'}

    except Exception as exc:
        logger.error(f'Error al exportar planilla {planilla_id}: {str(exc)}')
        raise


@shared_task
def calcular_estadisticas_dashboard():
    """
    Calcula y cachea las estadísticas del dashboard
    """
    from .models import ConfiguracionPlanificacion, Ejecucion, Enfermera
    from django.core.cache import cache
    from django.db.models import Count, Avg
    from django.utils import timezone
    from datetime import timedelta

    try:
        # Estadísticas generales
        stats = {
            'total_configuraciones': ConfiguracionPlanificacion.objects.filter(activa=True).count(),
            'total_ejecuciones': Ejecucion.objects.count(),
            'total_enfermeras': Enfermera.objects.filter(activa=True).count(),
            'ejecuciones_completadas': Ejecucion.objects.filter(estado='COMPLETADA').count(),
        }

        # Estadísticas del último mes
        fecha_mes = timezone.now() - timedelta(days=30)
        stats['ejecuciones_mes'] = Ejecucion.objects.filter(
            fecha_inicio__gte=fecha_mes
        ).count()

        # Penalización promedio
        penalizacion_avg = Ejecucion.objects.filter(
            estado='COMPLETADA',
            penalizacion_total__isnull=False
        ).aggregate(Avg('penalizacion_total'))
        stats['penalizacion_promedio'] = penalizacion_avg['penalizacion_total__avg'] or 0

        # Cachear por 1 hora
        cache.set('dashboard_stats', stats, 3600)

        logger.info('Estadísticas del dashboard actualizadas')

        return stats

    except Exception as exc:
        logger.error(f'Error al calcular estadísticas: {str(exc)}')
        raise


@shared_task
def generar_reporte_mensual():
    """
    Genera un reporte mensual de ejecuciones
    """
    from .models import Ejecucion
    from django.utils import timezone
    from datetime import timedelta
    from django.core.mail import send_mail

    try:
        # Obtener ejecuciones del último mes
        fecha_inicio = timezone.now() - timedelta(days=30)
        ejecuciones = Ejecucion.objects.filter(
            fecha_inicio__gte=fecha_inicio
        )

        total = ejecuciones.count()
        completadas = ejecuciones.filter(estado='COMPLETADA').count()
        errores = ejecuciones.filter(estado='ERROR').count()
        optimas = ejecuciones.filter(es_optima=True).count()

        # Generar mensaje
        mensaje = f'''
        REPORTE MENSUAL DE PLANIFICACIONES
        ==================================

        Período: {fecha_inicio.strftime('%d/%m/%Y')} - {timezone.now().strftime('%d/%m/%Y')}

        Ejecuciones totales: {total}
        Completadas: {completadas}
        Con errores: {errores}
        Soluciones óptimas: {optimas}

        Tasa de éxito: {(completadas / total * 100):.1f}%
        Tasa de optimalidad: {(optimas / completadas * 100 if completadas > 0 else 0):.1f}%
        '''

        # Enviar a administradores
        admin_emails = [admin[1] for admin in settings.ADMINS]
        if admin_emails:
            send_mail(
                subject='Reporte Mensual - Sistema de Planificación de Turnos',
                message=mensaje,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=admin_emails,
                fail_silently=True
            )

            logger.info('Reporte mensual enviado')

        return {'total': total, 'completadas': completadas}

    except Exception as exc:
        logger.error(f'Error al generar reporte mensual: {str(exc)}')
        raise
