from typing import Optional, Dict, Any
import dataclasses
import threading
import time
import pathlib

from .config import Config
from .version import VERSION

@dataclasses.dataclass
class PrinterState:
    eventtime: float = 0.0
    status: Dict = dataclasses.field(default_factory=dict)
    current_print_ts: int = -1
    last_print: Optional[Dict[str, Any]] = None

    def __post_init__(self):
        self._mutex = threading.RLock()

    def is_printing(self) -> bool:
        with self._mutex:
            return self.status.get('print_stats', {}).get('state') == 'printing'

    def got_metadata(self) -> bool:
        with self._mutex:
            filepath = self.status.get('print_stats', {}).get('filename', '')
            return (
                filepath != '' and
                self.last_print is not None and
                filepath == self.last_print.get('filename')
            )

    def get_file_size(self) -> Optional[int]:
        with self._mutex:
            if self.got_metadata() and self.last_print:
                return self.last_print.get('metadata', {}).get('size')
        return None

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

    def to_dict(
        self, print_event: Optional[str] = None, config: Optional[Config] = None
    ) -> Dict:
        with self._mutex:
            data = {
                'current_print_ts': self.current_print_ts,
                'octoprint_data': self.to_octoprint_state(),
            }
            if print_event:
                data['octoprint_event'] = {'event_type': print_event}

            if config:
                data["octoprint_settings"] = dict(
                    webcam=dict(
                        flipV=config.webcam.flip_v,
                        flipH=config.webcam.flip_h,
                        rotate90=config.webcam.rotate_90,
                        streamRatio="16:9" if config.webcam.aspect_ratio_169 else "4:3",
                    ),
                    agent=dict(
                        name="moonraker_obico",
                        version=VERSION,
                    ),
                )
            return data

    def to_octoprint_state(self) -> Dict:
        with self._mutex:
            state = self.get_state_str_from(self.status)
            print_stats = self.status.get('print_stats') or dict()
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

            completion = self.status.get('virtual_sdcard', {}).get('progress')
            print_time = print_stats.get('print_duration')
            estimated_time = print_time / completion if print_time is not None and completion is not None and completion > 0.001 else None
            print_time_left = estimated_time - print_time if estimated_time is not None and print_time is not None else None
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
                    'completion': completion * 100,
                    'filepos': virtual_sdcard.get('file_position', 0),
                    'printTime': print_time,
                    'printTimeLeft': print_time_left,
                },
                'temperatures': temps,
                'file_metadata': {},
            }
