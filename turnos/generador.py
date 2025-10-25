"""
Generador de planificaciones usando OR-Tools
"""
from ortools.sat.python import cp_model
from datetime import datetime, timedelta
from typing import Dict, List, Tuple
import json


class GeneradorPlanificacion:
    """Clase para generar planificaciones de turnos usando CP-SAT"""

    def __init__(self, configuracion):
        """
        Inicializa el generador con una configuración

        Args:
            configuracion: Instancia de ConfiguracionPlanificacion
        """
        self.configuracion = configuracion
        self.model = cp_model.CpModel()
        self.solver = cp_model.CpSolver()

        # Parámetros
        self.num_dias = configuracion.num_dias
        self.num_enfermeras = configuracion.enfermeras.count()
        self.enfermeras = list(configuracion.enfermeras.all())
        self.turnos = list(configuracion.turnos.all())
        self.num_turnos = len(self.turnos)

        # Variables de decisión
        self.shifts = {}
        self.resultado = None

        # Configurar solver
        if configuracion.tiempo_maximo_segundos:
            self.solver.parameters.max_time_in_seconds = configuracion.tiempo_maximo_segundos

        if configuracion.num_trabajadores:
            self.solver.parameters.num_search_workers = configuracion.num_trabajadores

        if configuracion.seed:
            self.solver.parameters.random_seed = configuracion.seed

    def crear_variables(self):
        """Crea las variables de decisión del modelo"""
        # shifts[e, d, t] = 1 si enfermera e trabaja turno t en día d
        for e in range(self.num_enfermeras):
            for d in range(self.num_dias):
                for t in range(self.num_turnos):
                    self.shifts[(e, d, t)] = self.model.NewBoolVar(
                        f'shift_e{e}_d{d}_t{t}'
                    )

    def aplicar_restricciones_duras(self):
        """Aplica las restricciones duras (obligatorias)"""
        restricciones = self.configuracion.restricciones_duras or []

        for restriccion in restricciones:
            nombre = restriccion.get('nombre')
            params = restriccion.get('parametros', {})

            if nombre == 'un_turno_por_dia':
                self._restriccion_un_turno_por_dia()

            elif nombre == 'cobertura_minima':
                self._restriccion_cobertura_minima(params)

            elif nombre == 'cobertura_maxima':
                self._restriccion_cobertura_maxima(params)

            elif nombre == 'descanso_minimo':
                self._restriccion_descanso_minimo(params)

            elif nombre == 'turnos_consecutivos_max':
                self._restriccion_turnos_consecutivos_max(params)

            elif nombre == 'turnos_semanales_max':
                self._restriccion_turnos_semanales_max(params)

    def _restriccion_un_turno_por_dia(self):
        """Una enfermera solo puede trabajar un turno por día"""
        for e in range(self.num_enfermeras):
            for d in range(self.num_dias):
                self.model.Add(
                    sum(self.shifts[(e, d, t)] for t in range(self.num_turnos)) <= 1
                )

    def _restriccion_cobertura_minima(self, params):
        """Cobertura mínima de enfermeras por turno"""
        demanda = self.configuracion.demanda_por_turno or {}

        for d in range(self.num_dias):
            for t, turno in enumerate(self.turnos):
                turno_demanda = demanda.get(turno.nombre, {})
                minimo = turno_demanda.get('min', params.get('min', 1))

                self.model.Add(
                    sum(self.shifts[(e, d, t)] for e in range(self.num_enfermeras)) >= minimo
                )

    def _restriccion_cobertura_maxima(self, params):
        """Cobertura máxima de enfermeras por turno"""
        demanda = self.configuracion.demanda_por_turno or {}

        for d in range(self.num_dias):
            for t, turno in enumerate(self.turnos):
                turno_demanda = demanda.get(turno.nombre, {})
                maximo = turno_demanda.get('max', params.get('max', 10))

                self.model.Add(
                    sum(self.shifts[(e, d, t)] for e in range(self.num_enfermeras)) <= maximo
                )

    def _restriccion_descanso_minimo(self, params):
        """Descanso mínimo entre turnos"""
        horas_minimas = params.get('horas', 11)

        for e in range(self.num_enfermeras):
            for d in range(self.num_dias - 1):
                # Si trabaja de noche, no puede trabajar mañana al día siguiente
                for t1 in range(self.num_turnos):
                    for t2 in range(self.num_turnos):
                        if self._requiere_descanso(t1, t2, horas_minimas):
                            self.model.Add(
                                self.shifts[(e, d, t1)] + self.shifts[(e, d + 1, t2)] <= 1
                            )

    def _restriccion_turnos_consecutivos_max(self, params):
        """Máximo de turnos consecutivos"""
        max_consecutivos = params.get('max', 5)

        for e in range(self.num_enfermeras):
            for d in range(self.num_dias - max_consecutivos):
                # No puede trabajar más de max_consecutivos días seguidos
                total_trabajados = sum(
                    self.shifts[(e, d + i, t)]
                    for i in range(max_consecutivos + 1)
                    for t in range(self.num_turnos)
                )
                self.model.Add(total_trabajados <= max_consecutivos)

    def _restriccion_turnos_semanales_max(self, params):
        """Máximo de turnos por semana"""
        max_semanales = params.get('max', 5)

        for e in range(self.num_enfermeras):
            for semana in range(self.num_dias // 7):
                inicio = semana * 7
                fin = min(inicio + 7, self.num_dias)

                total_semana = sum(
                    self.shifts[(e, d, t)]
                    for d in range(inicio, fin)
                    for t in range(self.num_turnos)
                )
                self.model.Add(total_semana <= max_semanales)

    def aplicar_restricciones_blandas(self):
        """Aplica las restricciones blandas (preferencias) como objetivos"""
        restricciones = self.configuracion.restricciones_blandas or []
        objetivo_terminos = []

        for restriccion in restricciones:
            nombre = restriccion.get('nombre')
            peso = restriccion.get('peso', 1.0)
            params = restriccion.get('parametros', {})

            if nombre == 'equidad_turnos':
                terminos = self._objetivo_equidad_turnos(peso)
                objetivo_terminos.extend(terminos)

            elif nombre == 'preferencias_turno':
                terminos = self._objetivo_preferencias_turno(peso, params)
                objetivo_terminos.extend(terminos)

            elif nombre == 'minimizar_noches':
                terminos = self._objetivo_minimizar_noches(peso)
                objetivo_terminos.extend(terminos)

        # Minimizar la suma de todos los términos del objetivo
        if objetivo_terminos:
            self.model.Minimize(sum(objetivo_terminos))

    def _objetivo_equidad_turnos(self, peso):
        """Objetivo: distribuir turnos equitativamente"""
        terminos = []

        # Variables para el mínimo y máximo de turnos
        min_turnos = self.model.NewIntVar(0, self.num_dias, 'min_turnos')
        max_turnos = self.model.NewIntVar(0, self.num_dias, 'max_turnos')

        for e in range(self.num_enfermeras):
            total_turnos = sum(
                self.shifts[(e, d, t)]
                for d in range(self.num_dias)
                for t in range(self.num_turnos)
            )
            self.model.AddMinEquality(min_turnos, [total_turnos])
            self.model.AddMaxEquality(max_turnos, [total_turnos])

        # Minimizar la diferencia
        diferencia = self.model.NewIntVar(0, self.num_dias, 'diferencia_turnos')
        self.model.Add(diferencia == max_turnos - min_turnos)

        terminos.append(int(peso * 100) * diferencia)
        return terminos

    def _objetivo_preferencias_turno(self, peso, params):
        """Objetivo: respetar preferencias de turno"""
        terminos = []

        for e, enfermera in enumerate(self.enfermeras):
            preferencias = enfermera.preferencias or {}
            turnos_preferidos = preferencias.get('turnos_preferidos', [])

            if turnos_preferidos:
                for d in range(self.num_dias):
                    for t, turno in enumerate(self.turnos):
                        if turno.nombre not in turnos_preferidos:
                            # Penalizar turnos no preferidos
                            terminos.append(int(peso * 10) * self.shifts[(e, d, t)])

        return terminos

    def _objetivo_minimizar_noches(self, peso):
        """Objetivo: minimizar turnos de noche"""
        terminos = []

        # Buscar el índice del turno de noche
        idx_noche = next(
            (i for i, t in enumerate(self.turnos) if t.nombre == 'NOCHE'),
            None
        )

        if idx_noche is not None:
            for e in range(self.num_enfermeras):
                for d in range(self.num_dias):
                    terminos.append(int(peso * 20) * self.shifts[(e, d, idx_noche)])

        return terminos

    def _requiere_descanso(self, turno1_idx, turno2_idx, horas_minimas):
        """Verifica si dos turnos requieren descanso entre ellos"""
        turno1 = self.turnos[turno1_idx]
        turno2 = self.turnos[turno2_idx]

        # Si turno1 es noche y turno2 es mañana, requiere descanso
        if turno1.nombre == 'NOCHE' and turno2.nombre == 'MANANA':
            return True

        return False

    def resolver(self) -> Dict:
        """
        Resuelve el modelo y retorna el resultado

        Returns:
            Dict con el resultado de la optimización
        """
        # Crear variables
        self.crear_variables()

        # Aplicar restricciones
        self.aplicar_restricciones_duras()
        self.aplicar_restricciones_blandas()

        # Resolver
        status = self.solver.Solve(self.model)

        resultado = {
            'status': self._get_status_string(status),
            'es_optima': status == cp_model.OPTIMAL,
            'penalizacion': self.solver.ObjectiveValue() if status in [cp_model.OPTIMAL, cp_model.FEASIBLE] else None,
            'tiempo': self.solver.WallTime(),
            'asignaciones': []
        }

        if status in [cp_model.OPTIMAL, cp_model.FEASIBLE]:
            resultado['asignaciones'] = self._extraer_asignaciones()

        self.resultado = resultado
        return resultado

    def _get_status_string(self, status):
        """Convierte el status del solver a string"""
        status_map = {
            cp_model.OPTIMAL: 'OPTIMAL',
            cp_model.FEASIBLE: 'FEASIBLE',
            cp_model.INFEASIBLE: 'INFEASIBLE',
            cp_model.MODEL_INVALID: 'MODEL_INVALID',
            cp_model.UNKNOWN: 'UNKNOWN'
        }
        return status_map.get(status, 'UNKNOWN')

    def _extraer_asignaciones(self) -> List[Dict]:
        """Extrae las asignaciones del modelo resuelto"""
        asignaciones = []
        fecha_inicio = self.configuracion.fecha_inicio

        for e in range(self.num_enfermeras):
            for d in range(self.num_dias):
                fecha = fecha_inicio + timedelta(days=d)

                for t in range(self.num_turnos):
                    if self.solver.Value(self.shifts[(e, d, t)]) == 1:
                        asignaciones.append({
                            'enfermera_id': self.enfermeras[e].id,
                            'enfermera_nombre': self.enfermeras[e].nombre,
                            'turno_id': self.turnos[t].id,
                            'turno_nombre': self.turnos[t].get_nombre_display(),
                            'fecha': fecha.isoformat(),
                            'dia': d,
                            'es_dia_libre': False
                        })
                        break
                else:
                    # Día libre
                    asignaciones.append({
                        'enfermera_id': self.enfermeras[e].id,
                        'enfermera_nombre': self.enfermeras[e].nombre,
                        'turno_id': None,
                        'turno_nombre': 'Libre',
                        'fecha': fecha.isoformat(),
                        'dia': d,
                        'es_dia_libre': True
                    })

        return asignaciones
