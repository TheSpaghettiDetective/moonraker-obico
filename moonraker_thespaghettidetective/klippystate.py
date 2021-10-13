from typing import Optional, Dict
import dataclasses
import time

from .logger import getLogger

logger = getLogger('klippystate')


@dataclasses.dataclass
class KlippyState:
    eventtime: float = 0.0
    status: Dict = dataclasses.field(default_factory=dict)
    current_print_ts: int = -1

    logger = logger

    def update(self, data: Dict) -> Optional[str]:
        cur_status = self.get_printer_state_from(self.status)
        next_status = self.get_printer_state_from(data['status'])
        print_event = None
        current_print_ts = None

        if next_status == 'Printing':
            if cur_status == 'Printing':
                pass
            elif cur_status == 'Paused':
                print_event = 'PrintResumed'
            else:
                print_event = 'PrintStarted'
                current_print_ts = time.time()

        elif next_status == 'Paused':
            if cur_status != 'Paused':
                print_event = 'PrintPaused'

        elif next_status == 'Error':
            if cur_status in ('Paused', 'Printing'):
                print_event = 'PrintFailed'
                current_print_ts = -1

        elif next_status == 'Operational':
            if cur_status in ('Paused', 'Printing'):
                _state = data['status'].get('print_stats', {}).get('state')
                if _state == 'cancelled':
                    print_event = 'PrintCancelled'
                elif _state == 'complete':
                    print_event = 'PrintDone'
                else:
                    self.logger.error(
                        'unexpected state "{_state}", please report.')
                    print_event = 'PrintFailed'

                current_print_ts = -1

        if next_status != cur_status:
            print_event_disp = f'({print_event})' if print_event else ''
            self.logger.info(
                'klipper status changed: '
                f'{cur_status} -> {next_status} {print_event_disp}'
            )

        self.eventtime = data['eventtime']
        self.status = data['status']
        if current_print_ts is not None:
            self.current_print_ts = current_print_ts
        return print_event

    def is_printing(self):
        return self.status.get(
            'webhooks', {}
        ).get('state') == 'printing'

    def get_printer_state_from(self, data):
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

    def to_tsd_state(self):
        data = {
            '_ts': time.time(),
            'current_print_ts': self.current_print_ts,
            'octoprint_data': self.to_octoprint_state() if self.status else {},
        }
        return data

    def to_octoprint_state(self):
        state = self.get_printer_state_from(self.status)
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
                    'name': print_stats.get('filename', '').rsplit('/', 1)[1],
                    'path': print_stats.get('filename', ''),
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
