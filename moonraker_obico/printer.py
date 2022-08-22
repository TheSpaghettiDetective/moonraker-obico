from typing import Optional, Dict, Any
import threading
import time
import pathlib

from .config import Config
from .version import VERSION

class PrinterState:
    STATE_OFFLINE = 'Offline'
    STATE_OPERATIONAL = 'Operational'
    STATE_PRINTING = 'Printing'
    STATE_PAUSED = 'Paused'
    STATE_ERROR = 'Error'

    ACTIVE_STATES = [STATE_PRINTING, STATE_PAUSED]

    def __init__(self, app_config: Config):
        self._mutex = threading.RLock()
        self.app_config = app_config
        self.status = {}
        self.current_print_ts = None

    def has_active_job(self) -> bool:
        return PrinterState.get_state_from_status(self.status) in PrinterState.ACTIVE_STATES

    def is_printing(self) -> bool:
        with self._mutex:
            return self.status.get('print_stats', {}).get('state') == 'printing'

    # Return: The old status.
    def update_status(self, new_status: Dict) -> Dict:
        with self._mutex:
            old_status = self.status
            self.status = new_status
        return old_status

    # Return: The old current_print_ts.
    def set_current_print_ts(self, new_current_print_ts):
        with self._mutex:
            old_current_print_ts = self.current_print_ts
            self.current_print_ts = new_current_print_ts
        return old_current_print_ts

    @classmethod
    def get_state_from_status(cls, data: Dict) -> str:
        klippy_state = data.get(
            'webhooks', {}
        ).get('state', 'disconnected')

        if klippy_state in ('disconnected', 'startup'):
            return PrinterState.STATE_OFFLINE
        elif klippy_state != 'ready':
            return PrinterState.STATE_ERROR

        return {
            'standby': PrinterState.STATE_OPERATIONAL,
            'printing': PrinterState.STATE_PRINTING,
            'paused': PrinterState.STATE_PAUSED,
            'complete': PrinterState.STATE_OPERATIONAL,
            'cancelled': PrinterState.STATE_OPERATIONAL
        }.get(data.get('print_stats', {}).get('state', 'unknown'), PrinterState.STATE_ERROR)

    def to_dict(
        self, print_event: Optional[str] = None, with_config: Optional[bool] = False,
    ) -> Dict:
        with self._mutex:
            data = {
                'current_print_ts': self.current_print_ts,
                'status': self.to_status(),
            } if self.current_print_ts is not None else {}      # Print status is un-deterministic when current_print_ts is None

            if print_event:
                data['event'] = {'event_type': print_event}

            if with_config:
                config = self.app_config
                data["settings"] = dict(
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

    def to_status(self) -> Dict:
        with self._mutex:
            state = self.get_state_from_status(self.status)
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

                temps[self.app_config.get_mapped_server_heater_name(heater)] = {
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
                        'closedOrError': False,  # OctoPrint uses this flag to indicate the printer is connectable. It should always be false until we support connecting moonraker to printer
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
