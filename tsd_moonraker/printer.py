from typing import Optional, Dict
import dataclasses
import time
import pathlib

from .version import VERSION
from .logger import getLogger

logger = getLogger('klippystate')


@dataclasses.dataclass
class PrinterState:
    eventtime: float = 0.0
    status: Dict = dataclasses.field(default_factory=dict)
    current_print_ts: int = -1

    def is_printing(self) -> bool:
        return self.status.get(
            'webhooks', {}
        ).get('state') == 'printing'

    def get_state_str_from(self, data: Dict) -> str:
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
            print_event: Optional[str] = None
    ) -> Dict:
        data = {
            'current_print_ts': self.current_print_ts,
            'octoprint_data': self.to_octoprint_state(),
            '_from': {
                'plugin': 'tsd_moonraker',
                'version': VERSION,
            }
        }
        if print_event:
            data['octoprint_event'] = {'event_type': print_event}
        return data

    def to_octoprint_state(self) -> Dict:
        state = self.get_state_str_from(self.status)
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

        filepath = print_stats.get('filename', '')
        filename = pathlib.Path(filepath).name if filepath else ''

        if state == 'Offline':
            return {}

        return {
            '_ts': time.time(),
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
        }
