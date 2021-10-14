from typing import Optional, Dict
import dataclasses
import time
import pathlib

from .logger import getLogger

logger = getLogger('klippystate')


@dataclasses.dataclass
class PrintEvent:
    name: str
    job_state: Optional[Dict]


@dataclasses.dataclass
class PrinterJob:
    state: Optional[Dict] = None
    """
        {
            "end_time": null,
            "filament_used": 0.0,
            "filename": "thespaghettidetective/fast3.gcode",
            "metadata": {
                "size": 5231,
                "modified": 1634198743.233244,
                "slicer": "Slic3r",
                "slicer_version": "1.1.4",
                "layer_height": 0.24,
                "first_layer_height": 0.3,
                "first_layer_bed_temp": 90.0,
                "first_layer_extr_temp": 200.0,
                "gcode_start_byte": 249,
                "gcode_end_byte": 446
            },
            "print_duration": 0.0,
            "status": "in_progress",
            "start_time": 1634198743.6614738,
            "total_duration": 0.044489051011623815,
            "job_id": "000002",
            "exists": true
        }
    """

    # def is_printing(self) -> bool:
    #    return self.state.get('status') == 'in_progress'


@dataclasses.dataclass
class StateChange:
    prev_state_str: str
    next_state_str: str
    print_event_str: Optional[str]


@dataclasses.dataclass
class PrinterState:
    eventtime: float = 0.0
    status: Dict = dataclasses.field(default_factory=dict)

    def update(self, data: Dict) -> Optional[str]:
        prev_state_str = self.get_printer_state_str_from(self.status)
        next_state_str = self.get_printer_state_str_from(data['status'])
        print_event_str = None

        if next_state_str == 'Printing':
            if prev_state_str == 'Printing':
                pass
            elif prev_state_str == 'Paused':
                print_event_str = 'PrintResumed'
            else:
                print_event_str = 'PrintStarted'

        elif next_state_str == 'Paused':
            if prev_state_str != 'Paused':
                print_event_str = 'PrintPaused'

        elif next_state_str == 'Error':
            if prev_state_str in ('Paused', 'Printing'):
                print_event_str = 'PrintFailed'

        elif next_state_str == 'Operational':
            if prev_state_str in ('Paused', 'Printing'):
                _state = data['status'].get('print_stats', {}).get('state')
                if _state == 'cancelled':
                    print_event_str = 'PrintCancelled'
                elif _state == 'complete':
                    print_event_str = 'PrintDone'
                else:
                    # FIXME
                    self.logger.error(
                        f'unexpected state "{_state}", please report.')

        self.eventtime = data['eventtime']
        self.status = data['status']

        if next_state_str != prev_state_str:
            return StateChange(
                prev_state_str=prev_state_str,
                next_state_str=next_state_str,
                print_event_str=print_event_str,
            )

        return None

    def is_printing(self) -> bool:
        return self.status.get(
            'webhooks', {}
        ).get('state') == 'printing'

    def get_printer_state_str_from(self, data: Dict) -> str:
        klippy_state = data.get(
            'webhooks', {}
        ).get('state', 'disconnected')

        if klippy_state in ('disconnected', 'startup'):
            return 'Offline'
        elif klippy_state != 'ready':
            return 'Error'

        return {
            'standby': 'Operational',
            'printing': 'Printing',
            'paused': 'Paused',
            'complete': 'Operational',
            'cancelled': 'Operational',
        }.get(data.get('print_stats', {}).get('state', 'unknown'), 'Error')

    def to_tsd_state(
            self,
            job_state: Optional[Dict],
            print_event: Optional[PrintEvent] = None
    ) -> Dict:
        job_state = print_event.job_state if print_event else job_state
        current_print_ts = (
            int(job_state.get('start_time', -1)) if job_state else -1
        )
        data = {
            '_ts': time.time(),
            'current_print_ts': current_print_ts,
            'octoprint_data':
                self.to_octoprint_state(job_state) if self.status else {},
        }
        if print_event:
            data['octoprint_event'] = {'event_type': print_event.name}
        return data

    def to_octoprint_state(self, job_state: Dict) -> Dict:
        state = self.get_printer_state_str_from(self.status)
        print_stats = self.status.get('print_stats') or dict()
        # toolhead = self.status.get('toolhead') or dict()
        display_status = self.status.get('display_status') or dict()
        virtual_sdcard = self.status.get('virtual_sdcard') or dict()
        error_text = (
            print_stats.get('message', 'Unknown error')
            if state == 'Error'
            else ''
        )

        temps = {}
        heaters = self.status.get('heaters', {}).get('available_heaters', ())
        for heater in heaters:
            data = self.status.get(heater, {})
            if heater.startswith('extruder'):
                try:
                    tool_no = int(heater[8:])
                except ValueError:
                    tool_no = 0
                name = f'tool{tool_no}'
            elif heater == "heater_bed":
                name = 'bed'
            else:
                continue

            temps[name] = {
                'actual': round(data.get('temperature', 0.), 2),
                'offset': 0,
                'target': data.get('target', 0.),
            }

        filepath = (job_state or {}).get('filename', '')
        filename = pathlib.Path(filepath).name if filepath else ''
        return {
            'state': {
                'text': error_text or state,
                'flags': {
                    'operational': state not in ['Error', 'Offline'],
                    'paused': state == 'Paused',
                    'printing': state == 'Printing',
                    'cancelling': state == 'Cancelling',
                    'pausing': False,
                    'error': state == 'Error',
                    'ready': state == 'Operational',
                    'closedOrError': state in ['Error', 'Offline'],
                }
            },
            'currentZ': None,
            'job': {
                'file': {
                    'name': filename,
                    'path': filepath,
                    # 'display': "aa.gcode",
                    # 'origin': "local",
                    # 'size': 154006,
                    # 'date': 1628534143
                },
                'estimatedPrintTime': None,
                'filament': {'length': None, 'volume': None},
                'user': None,
            },
            'progress': {
                'completion': display_status.get('progress', 0.0),
                'filepos': virtual_sdcard.get('file_position', 0),
                'printTime': print_stats.get('total_duration', 0.0),
                'printTimeLeft': None,
                'printTimeOrigin': None,
            },
            'temperatures': temps,
            'file_metadata': {},
            '_from_klippy': True,
        }
